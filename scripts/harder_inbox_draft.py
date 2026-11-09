# SPDX-License-Identifier: Apache-2.0
"""The harder inbox benchmark's draft (draft, not clinically reviewed): writes it, or checks it.

    uv run python scripts/harder_inbox_draft.py           # write it (datasets/drafts/...)
    uv run python scripts/harder_inbox_draft.py --check   # the committed files are the generator's

Both run the generator (`mendari.sandbox.synthetic_inbox_harder`, local and seeded: no network),
its leakage check against the shipped Patient Inbox Router's data and templates, and its coverage
check against the pack's labels. `--check` exits 1 when a committed file differs from what the
generator writes, byte for byte, or either check has a problem; writing exits 1 on a problem too,
after writing. Neither touches the pack: its data, model, benchmark and digest stay as shipped.
How the owner reviews the draft: notes/clinician-review-harder-inbox-benchmark.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mendari.sandbox import synthetic_inbox_harder as harder

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="check the committed files")
    parser.add_argument("--out", type=Path, default=ROOT / harder.DRAFT_DIR)
    parser.add_argument("--seed", type=int, default=harder.DEFAULT_SEED)
    args = parser.parse_args(argv)

    draft = harder.build(ROOT / "packs" / harder.PACK_NAME, seed=args.seed)
    if args.check:
        problems = harder.check_files(args.out, draft)
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, content in draft.files.items():
            (args.out / name).write_bytes(content)
        problems = draft.problems
    for split, records in draft.splits.items():
        print(f"{len(records):>5} {split} cases ({harder.REVIEW_STATUS.lower()})")
    for check in draft.leakage:
        print(f"leakage: {check.name}: {check.compared} compared, {len(check.problems)} problems")
    print(f"coverage: {len(draft.coverage)} problems")
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    print(f"{'checked' if args.check else 'wrote'} {args.out}: {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
