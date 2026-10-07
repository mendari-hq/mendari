# SPDX-License-Identifier: Apache-2.0
"""Every source file under src/, tests/, scripts/ and hub/ names its licence in its first lines:
`SPDX-License-Identifier: Apache-2.0` (CONTRIBUTING.md, "Licence"), after a shebang or an HTML
doctype. Vendored third-party files keep their own headers, and the API snippets are shown to
readers verbatim (the /docs landing), so neither carries one."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = "SPDX-License-Identifier: Apache-2.0"
COMMENTS = {
    ".py": "# {}",
    ".sh": "# {}",
    ".js": "// {}",
    ".mjs": "// {}",
    ".cjs": "// {}",
    ".ts": "// {}",
    ".tsx": "// {}",
    ".css": "/* {} */",
    ".html": "<!-- {} -->",
}
NOT_OURS = ("src/mendari/dashboard/vendor/", "tests/browser/vendor/")
VERBATIM = ("src/mendari/dashboard/snippets/",)


def sources() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "src", "tests", "scripts", "hub"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode()  # fmt: skip
    return [
        path
        for path in listed.split("\0")
        if Path(path).suffix in COMMENTS and not path.startswith(NOT_OURS + VERBATIM)
    ]


def expected_line(path: str) -> str:
    return COMMENTS[Path(path).suffix].format(IDENTIFIER)


def test_the_files_are_found() -> None:
    found = sources()
    assert len(found) > 300
    for kind in (".py", ".js", ".css", ".html", ".sh", ".tsx", ".ts", ".mjs"):
        assert any(path.endswith(kind) for path in found), kind


@pytest.mark.parametrize("path", sources())
def test_every_source_file_names_its_licence(path: str) -> None:
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    first = (
        1 if lines and (lines[0].startswith("#!") or lines[0].lower() == "<!doctype html>") else 0
    )
    assert lines[first : first + 1] == [expected_line(path)], f"{path}: add `{expected_line(path)}`"


def test_vendored_and_verbatim_files_are_left_as_they_are() -> None:
    listed = subprocess.run(
        ["git", "ls-files", *NOT_OURS, *VERBATIM], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    assert listed  # swagger-ui, axe-core, the snippets
    for path in listed:
        if Path(path).suffix in COMMENTS:
            assert IDENTIFIER not in (ROOT / path).read_text(encoding="utf-8")[:500], path
