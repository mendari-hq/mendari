# SPDX-License-Identifier: Apache-2.0
"""The eval gate for one changed pack (workflow `packs`): its metrics against `main`'s benchmark
as Markdown for the job summary; exit 1 on a regression (mendari.evals.gate).

    uv run python scripts/pack_gate.py packs/NAME --report new.json [--main main.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mendari.core.registry import resolve_pack  # noqa: E402
from mendari.evals.gate import gate, markdown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pack", help="the pack's directory")
    parser.add_argument("--report", required=True, type=Path, help="this change's evaluation")
    parser.add_argument("--main", type=Path, help="the benchmark published on main")
    args = parser.parse_args()
    pack = resolve_pack(args.pack)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    base = json.loads(args.main.read_text(encoding="utf-8")) if args.main else None
    rows, failures = gate(report, base, pack.manifest.evaluation.degradation_tolerance)
    print(markdown(pack.id, rows, failures, new=base is None))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
