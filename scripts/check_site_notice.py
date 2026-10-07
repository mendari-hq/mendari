# SPDX-License-Identifier: Apache-2.0
"""Every page of the built documentation site shows the notice (`mendari.notices.NOTICE`), as
every Mendari surface does (Definition of shipped, §7). Exit 1, naming each page without it.

    uv run mkdocs build --strict && python3 scripts/check_site_notice.py site
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def notice() -> str:
    """The notice's text, read from its source without importing the package."""
    source = (ROOT / "src" / "mendari" / "notices.py").read_text(encoding="utf-8")
    found = re.search(r'^NOTICE = "([^"]+)"$', source, re.M)
    if found is None:
        raise SystemExit("src/mendari/notices.py: NOTICE not found")
    return found.group(1)


def pages_without(site: Path, text: str) -> list[str]:
    """Every page, 404.html too (Pages serves it for any missing address)."""
    return [
        page.relative_to(site).as_posix()
        for page in sorted(site.rglob("*.html"))
        if text not in html.unescape(page.read_text(encoding="utf-8"))
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    site = Path(argv[0])
    pages = sorted(site.rglob("*.html"))
    if not pages:
        print(f"no pages in {site}: build the site first", file=sys.stderr)
        return 1
    missing = pages_without(site, notice())
    for page in missing:
        print(f"{page}: no notice", file=sys.stderr)
    if not missing:
        print(f"ok  {len(pages)} pages, each with the notice")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
