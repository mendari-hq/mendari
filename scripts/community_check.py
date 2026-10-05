"""The community-pack half of the `pack-check` job (workflow `packs`): every pull request runs it.

    uv run python scripts/community_check.py --base BASE_SHA --actor NAME --owner NAME \\
        [--summary "$GITHUB_STEP_SUMMARY"]

- A pull request that changes no community pack (nor `MAINTAINERS.yaml`) passes at once;
  `community-packs/README.md` is documentation.
- One that does, and anything outside one `community-packs/<name>/` folder (its
  `MAINTAINERS.yaml` aside), fails, unless the repository's owner made it (`--actor` is
  `--owner`, compared as GitHub compares names, ignoring case).
- Each changed community pack is checked (`mendari.community.check`), against the base for
  regressions and the version bump. A removed pack needs its tombstone in `MAINTAINERS.yaml`.

The metrics table and the rendered Decision Card of each pack go to the job summary. Exit 1 if
anything fails. Every path comes from `git diff -z` and reaches nothing but `pathlib`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mendari.community import maintainers  # noqa: E402
from mendari.community.check import check, pr_scope_problems  # noqa: E402
from mendari.community.gitbase import COMMUNITY_DIR, Base  # noqa: E402
from mendari.errors import MendariError  # noqa: E402


def run(repo: Path, base: str, *, owner: bool, summary: Path | None = None) -> int:
    changed = Base(repo, base).changed()
    community = [path for path in changed if path.startswith(COMMUNITY_DIR + "/")]
    folders = sorted({path.split("/")[1] for path in community if path.count("/") >= 2})
    out: list[str] = []
    if not folders and f"{COMMUNITY_DIR}/{maintainers.FILE}" not in changed:
        _emit("no community pack changes", ["No community pack changes."], summary)
        return 0
    failed = False
    problems = pr_scope_problems(changed, owner=owner)
    if problems:
        failed = True
        print("\n".join(f"error  [scope] {problem}" for problem in problems))
        out += ["## Scope", "", *(f"- **Error**: {problem}" for problem in problems), ""]
    for folder in folders:
        root = repo / COMMUNITY_DIR / folder
        if not root.exists():
            problem = _removal_problem(repo, f"{COMMUNITY_DIR}/{folder}")
            line = f"`{COMMUNITY_DIR}/{folder}` was removed" + (f": {problem}" if problem else "")
            print(("error  " if problem else "") + line)
            out += [f"## {line}", ""]
            failed = failed or problem is not None
            continue
        report = check(root, base=base, scope=False, owner=owner)
        print(report.text())
        out.append(report.markdown())
        failed = failed or not report.passed
    if not folders:
        print("no community pack changed (only MAINTAINERS.yaml)")
    _emit(None, out, summary)
    return 1 if failed else 0


def _removal_problem(repo: Path, folder: str) -> str | None:
    """Why a removed pack's removal is incomplete: it needs a tombstone."""
    path = repo / COMMUNITY_DIR / maintainers.FILE
    try:
        entries = maintainers.parse(path.read_bytes()) if path.is_file() else {}
    except MendariError as error:
        return str(error)
    pack_id = f"community/{folder.rsplit('/', 1)[-1]}"
    entry = entries.get(pack_id)
    if entry is None or not entry.tombstone:
        return (
            f"keep {pack_id} in {COMMUNITY_DIR}/{maintainers.FILE} as a tombstone "
            '(`removed: "<date>"`, `reason: ...`), so the id is never used again'
        )
    return None


def _emit(line: str | None, markdown: list[str], summary: Path | None) -> None:
    if line:
        print(line)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(markdown) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--base", required=True, help="the commit the pull request is compared with"
    )
    parser.add_argument("--actor", default="", help="who made the change (github.actor)")
    parser.add_argument("--owner", default="", help="the repository's owner")
    parser.add_argument("--summary", type=Path, help="append Markdown here (the job summary)")
    args = parser.parse_args()
    owner = bool(args.actor) and args.actor.casefold() == args.owner.casefold()
    try:
        return run(ROOT, args.base, owner=owner, summary=args.summary)
    except MendariError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
