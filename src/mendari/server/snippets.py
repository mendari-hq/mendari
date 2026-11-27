# SPDX-License-Identifier: Apache-2.0
"""The API examples, from one source: `dashboard/snippets/*.sh`.

Each file is a runnable bash script, split into display steps by `# step: <name> — <title>`
comments. The README shows steps verbatim (`tests/test_doc_snippets.py` keeps them equal), and
`scripts/check_doc_snippets.py` runs every file in CI against a local server, in open mode and
with a service key, so an example that stops working fails the build instead of a reader. The
developer landing at `/docs` shows every step (`server/landing.py`) and the API guide
(`docs/guides/api.md`) includes the files whole; they live under `src/` because the server reads
them (Vercel's function excludes `docs/`).

A script uses `$MENDARI` (the server) and sends `Authorization: Bearer $MENDARI_KEY` on every
call that needs a credential once sign-in is on; open mode ignores the header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent.parent / "dashboard" / "snippets"
FILES = ("quickstart", "fhir", "cds", "auth")  # in reading order

STEP = re.compile(r"^# step: (?P<name>[a-z][a-z0-9-]*) — (?P<title>\S.*)$")
# A call's URL is always the quoted "$MENDARI/…", so a check can find it and a reader can point
# the same text at any server.
_URL = re.compile(r'"\$MENDARI(?P<path>/[^"?]*)(?:\?[^"]*)?"')
_METHOD = re.compile(r"(?:^|\s)-X\s+(?P<method>[A-Z]+)\b")
_CURL = re.compile(r"(?<![\w-])curl(?=\s)")
# A shell variable in a path ("$id"), but not an escaped "\$" (FHIR's "$decide").
_VARIABLE = re.compile(r"(?<!\\)\$(?:\{[A-Za-z_]\w*\}|[A-Za-z_]\w*)")


@dataclass(frozen=True)
class Step:
    file: str  # "quickstart", "fhir", "cds" or "auth"
    name: str
    title: str
    text: str  # the commands, as shown and run (no header, no surrounding blank lines)

    def calls(self) -> list[tuple[str, str]]:
        """`(method, path)` of every curl in the step, with a variable in a path as `x`: enough
        to ask the route rules what the step needs (`server/auth.py` `permission_for`)."""
        found = []
        for command in _CURL.split(self.text)[1:]:
            url = _URL.search(command)
            if url is None:
                raise ValueError(f'{self.file}.sh {self.name}: a curl without "$MENDARI/…"')
            method = _METHOD.search(command)
            path = _VARIABLE.sub("x", url["path"]).replace("\\$", "$")
            found.append((method["method"] if method else "GET", path))
        return found


def load(file: str, directory: Path = DIRECTORY) -> list[Step]:
    """The steps of one snippet file (`<file>.sh`), in order. What comes before the first step
    (the shebang and the file's comments) is not a step."""
    steps: list[Step] = []
    header: re.Match[str] | None = None
    lines: list[str] = []
    for line in [*(directory / f"{file}.sh").read_text(encoding="utf-8").splitlines(), None]:
        if line is None or line.startswith("# step:"):
            if header is not None:
                text = "\n".join(lines).strip()
                steps.append(Step(file, header["name"], header["title"], text))
            if line is None:
                break
            header = STEP.match(line)
            if header is None:
                raise ValueError(f"{file}.sh: write a step as '# step: <name> — <title>': {line}")
            lines = []
        else:
            lines.append(line)
    return steps


def every(directory: Path = DIRECTORY) -> list[Step]:
    """Every step of every file, in reading order."""
    return [step for file in FILES for step in load(file, directory)]
