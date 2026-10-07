# SPDX-License-Identifier: Apache-2.0
"""Every reference pack's benchmark is reproducible (LP-10), checked after
scripts/build-reference-pack.sh in CI's reference-pack job:

- the rebuilt benchmark's result block is the committed one, byte for byte (its run block, the id,
  time and latency of the run, may differ);
- the committed result is what its committed ledger gives (mendari.evals.result), and its
  result_sha256 is its hash.

    uv run python scripts/check_eval_results.py            # against HEAD's benchmarks
    uv run python scripts/check_eval_results.py --base REF # against another commit's

Exit 1, naming each pack that differs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from mendari.evals.ledger import Ledger, canonical
from mendari.evals.result import FORMAT, result_from_ledger, result_of, result_sha256

ROOT = Path(__file__).resolve().parent.parent


def committed(path: Path, base: str) -> dict[str, object] | None:
    relative = path.relative_to(ROOT).as_posix()
    shown = subprocess.run(
        ["git", "show", f"{base}:{relative}"], cwd=ROOT, capture_output=True, text=True
    )
    return json.loads(shown.stdout) if shown.returncode == 0 else None


def problems(benchmark: Path, base: str) -> list[str]:
    name = benchmark.parent.parent.name
    old = committed(benchmark, base)
    new = json.loads(benchmark.read_text(encoding="utf-8"))
    if old is None or old.get("format") != FORMAT:
        return [f"{name}: no committed {FORMAT} benchmark at {base} to compare with"]
    found = []
    if canonical(result_of(old)) != canonical(result_of(new)):
        found.append(f"{name}: the rebuilt result differs from the committed one")
    ledger_id = str(old["ledger"]["id"])  # type: ignore[index]
    ledger_file = benchmark.parent / "ledgers" / (ledger_id.removeprefix("sha256:") + ".jsonl")
    if not ledger_file.is_file():
        return [*found, f"{name}: the ledger {ledger_file.name} is missing"]
    ledger = Ledger.loads(ledger_file.read_bytes(), where=f"{name}'s ledger")
    seeds = old["seeds"]
    again = result_from_ledger(ledger, seed=seeds["bootstrap"], resamples=seeds["resamples"])  # type: ignore[index]
    if canonical(again) != canonical(result_of(old)):
        found.append(f"{name}: the committed result is not what its ledger gives")
    if result_sha256(result_of(old)) != old.get("result_sha256"):
        found.append(f"{name}: result_sha256 is not the committed result's hash")
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="HEAD", help="the commit to compare with (HEAD)")
    args = parser.parse_args(argv)
    benchmarks = sorted(ROOT.glob("packs/*/evals/benchmark.json"))
    found = [problem for path in benchmarks for problem in problems(path, args.base)]
    for problem in found:
        print(problem, file=sys.stderr)
    if not found:
        print(f"ok  {len(benchmarks)} benchmarks: results reproduced from their ledgers")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
