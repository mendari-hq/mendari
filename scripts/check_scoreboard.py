# SPDX-License-Identifier: Apache-2.0
"""The results store is append-only and every result in it is what its inputs give (LP-17f,
ADR-0013; the grading method's section 11), checked in CI's required `demo-agent` job
against the commit a change is made on (`mendari.evals.results_store.check`):

- each file's name is its content's hash (a result's body, a ledger's id, a run record's bytes);
- no result of the base is removed or changed, except a valid signature added to an unsigned
  result whose body stays the same;
- `supersedes` links name earlier results of the same pack, set, arm and configuration, and
  form a forest, with one current result for each;
- each current result's signature verifies against scoreboard/keys.json, and none is a
  publisher's or a registry's key this machine pinned (`mendari pull` pins them; CI pins none,
  so there the review of each change to keys.json is what keeps such a key out);
- each public result recomputes byte for byte from its stored ledger, its dataset (SHA-256
  checked), its run record, its price table and the method, and each local ledger on its pack's
  current digest re-runs to the same bytes; a result of an older digest is recomputed and kept,
  one whose set or method changed is kept as it is; a new one must be on the current ones and
  measured no later than today, and a new remote one (LP-17h) must hold 3 to 5 replicates and
  cite its pack's declaration (scoreboard/declared/), which declares it; its ledgers are
  recomputed from, never re-run;
- each declaration names configurations its pack has, in their arms, remote or local as it says;
- no canary of the base is removed or changed, and each canary is what its ledger, its run
  record and the result it was compared with give;
- each hidden set's result (the grading route's `mendari.hidden_scoreboard/1` answer, LP-17k)
  fits its closed schema, field by field, and its name is its body's SHA-256 (its run block
  left out); it is of a pack Mendari maintains; under the current method it agrees with its
  own figures (intervals, evidence, grades, banner, ranking, draft); and, when new, it is under
  the current method, measured no later than today, in effect as the method is that day, and
  cites its pack's declaration as the store holds it, grading the `local` configurations it
  declares. It
  is kept as it is: no ledger of a hidden set is kept, so its k and n are never recomputed.

The packs are the repository's (packs/), never `MENDARI_PACKS_PATH` or the registry cache.

    uv run python scripts/check_scoreboard.py --base origin/main   # against another commit
    uv run python scripts/check_scoreboard.py --base HEAD          # what is not committed yet

It needs no secret and writes nothing. Exit 1, naming each problem, never a case.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from mendari.core.reference import bundled_dir
from mendari.evals import results_store, signing
from mendari.hub.client import config_dir

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / results_store.ROOT
PACKS = bundled_dir() or ROOT / "agents"


def base_results(
    base: str, store: Path = STORE, kind: str = results_store.RESULTS
) -> dict[str, bytes]:
    """The result files under the store's `results/` (or its `kind` folder: `canaries/`) in the
    commit `base`, by name (the file's name without `.json`), with their bytes. A commit without
    a store has none."""
    relative = (store / kind).relative_to(ROOT).as_posix()
    known = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if known.returncode != 0:
        raise SystemExit(f"the base {base!r} is not a commit in this clone (fetch it first)")
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--name-only", base, "--", f"{relative}/"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    found = {}
    for path in filter(None, listed.stdout.decode("utf-8").split("\0")):
        file_name = path.rsplit("/", 1)[-1]
        shown = subprocess.run(
            ["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True, check=True
        )
        found[file_name.removesuffix(".json")] = shown.stdout
    return found


RETIRED = STORE / "retired.json"


def unretired(found: dict[str, bytes], kind: str) -> tuple[dict[str, bytes], list[str]]:
    """The base's files less those of a retired agent (scoreboard/retired.json), and a problem for
    each retired file that is not kept, byte for byte, in the retirement's archive: a retired
    agent's results leave the store only to be kept there, never dropped."""
    if not RETIRED.is_file():
        return found, []
    retired = json.loads(RETIRED.read_text(encoding="utf-8"))
    agents, archive = set(retired.get("agents", {})), ROOT / retired["archive"] / kind
    kept, problems = {}, []
    for name, body in found.items():
        try:
            agent = json.loads(body)["scope"]["pack"]["id"]
        except (ValueError, KeyError, TypeError):
            agent = None
        if agent not in agents:
            kept[name] = body
            continue
        copy = archive / f"{name}.json"
        if not copy.is_file() or copy.read_bytes() != body:
            problems.append(f"{kind}/{name}.json: of retired {agent}, but not kept in {archive}")
    return kept, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the commit the change is made on")
    args = parser.parse_args(argv)
    store = results_store.Store.at(STORE)
    base, retired = unretired(base_results(args.base), results_store.RESULTS)
    canaries, retired_canaries = unretired(
        base_results(args.base, kind=results_store.CANARIES), results_store.CANARIES
    )
    found = results_store.check(
        store,
        base=base,
        base_canaries=canaries,
        publisher_ids=signing.read_pins(config_dir() / "trusted_keys.json"),
        packs=lambda ref: results_store.maintained_pack(ref, PACKS),
    )
    for line in found.kept:
        print(f"kept: {line}")
    problems = [*retired, *retired_canaries, *found.problems]
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        print(f"{len(problems)} problems in {store.root.name}/", file=sys.stderr)
        return 1
    print(f"ok  {found.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
