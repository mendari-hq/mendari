# SPDX-License-Identifier: Apache-2.0
"""The results store is append-only and every result in it is what its inputs give (LP-17f,
ADR-0013; the grading method's section 11), checked in CI's required `reference-pack` job
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
  one whose set or method changed is kept as it is; a new one must be of a local configuration,
  on the current ones, and measured no later than today;
- each hidden set's result (the grading route's `mendari.hidden_scoreboard/1` answer, LP-17k)
  fits its closed schema, field by field, and its name is its body's SHA-256 (its run block
  left out); it is of a pack Mendari maintains and, when new, under the current method and
  measured no later than today. It is kept as it is: no ledger of a hidden set is kept, so it
  is never recomputed.

The packs are the repository's (packs/), never `MENDARI_PACKS_PATH` or the registry cache.

    uv run python scripts/check_scoreboard.py --base origin/main   # against another commit
    uv run python scripts/check_scoreboard.py --base HEAD          # what is not committed yet

It needs no secret and writes nothing. Exit 1, naming each problem, never a case.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from mendari.evals import results_store, signing
from mendari.hub.client import config_dir

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / results_store.ROOT
PACKS = ROOT / "packs"


def base_results(base: str, store: Path = STORE) -> dict[str, bytes]:
    """The result files under the store's `results/` in the commit `base`, by name (the file's
    name without `.json`), with their bytes. A commit without a store has none."""
    relative = (store / results_store.RESULTS).relative_to(ROOT).as_posix()
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the commit the change is made on")
    args = parser.parse_args(argv)
    store = results_store.Store.at(STORE)
    found = results_store.check(
        store,
        base=base_results(args.base),
        publisher_ids=signing.read_pins(config_dir() / "trusted_keys.json"),
        packs=lambda ref: results_store.maintained_pack(ref, PACKS),
    )
    for line in found.kept:
        print(f"kept: {line}")
    for problem in found.problems:
        print(f"error: {problem}", file=sys.stderr)
    if found.problems:
        print(f"{len(found.problems)} problems in {store.root.name}/", file=sys.stderr)
        return 1
    print(f"ok  {found.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
