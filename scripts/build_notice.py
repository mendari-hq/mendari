"""Write NOTICE from licenses/notice.toml: the project, its copyright line, and every third-party
work the repository carries or its packages bundle, with the licence and the notice each requires.

A vendored package's version comes from its vendoring manifest (scripts/vendor_docs_ui.py), so a
new release vendored changes NOTICE with it; an upstream NOTICE file (Apache-2.0 section 4(d)) is
carried as it is.

Usage:

    uv run python scripts/build_notice.py          # write NOTICE
    uv run python scripts/build_notice.py --check  # NOTICE is what the manifest says (offline)
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "licenses" / "notice.toml"
NOTICE = ROOT / "NOTICE"


def manifest() -> dict[str, Any]:
    return tomllib.loads(MANIFEST.read_text())


def version(material: dict[str, Any]) -> str | None:
    """The version a vendored package is vendored at (its vendoring manifest), else None."""
    if "vendored" not in material:
        return None
    vendored = json.loads((ROOT / material["vendored"] / "manifest.json").read_text())
    return str(vendored["version"])


def render(data: dict[str, Any] | None = None) -> str:
    data = data or manifest()
    project = data["project"]
    lines = [project["name"], project["copyright"], "", project["licence"], ""]
    sections = [
        ("This product includes the following third-party material:", True),
        ("The source repository also carries, in no package:", False),
    ]
    for heading, distributed in sections:
        materials = [m for m in data["material"] if bool(m["distributed"]) is distributed]
        if not materials:
            continue
        lines += [heading, ""]
        for material in materials:
            release = version(material)
            where = material["where"].replace("<version>", release or "")
            lines.append(f"{material['name']}{f' {release}' if release else ''}")
            lines.append(f"  {where}")
            lines.append(f"  Licence: {material['licence']}.")
            if "notice_file" in material:
                carried = ROOT / material["notice_file"].replace("<version>", release or "")
                lines += [
                    f"  {line}" if line else "" for line in carried.read_text().strip().split("\n")
                ]
            if "notice" in material:
                lines += [f"  {line}" for line in material["notice"].strip().split("\n")]
            if "also" in material:
                lines.append(f"  {material['also']}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        if NOTICE.read_text() != render():
            print(
                "NOTICE is not what licenses/notice.toml says: run scripts/build_notice.py",
                file=sys.stderr,
            )
            return 1
        return 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    NOTICE.write_text(render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
