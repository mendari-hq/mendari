# SPDX-License-Identifier: Apache-2.0
"""Every shell and Python block the docs show is checked (LP-15): run in CI on a clean runner, the
same text as a snippet CI runs, or skipped for a reason a reviewer accepted. A marker on the line
before the block's opening fence says which (`tests/test_doc_blocks.py` fails on a block without
one):

    <!-- check: run quickstart -->        the quickstart, in order, timed (CI job `quickstart`)
    <!-- check: run cli -->               the command-line tour, after the quickstart
    <!-- check: run python <group> -->    a Python example; a group's blocks run as one program
    <!-- check: run install -->           a new virtual environment installs from the clone
    <!-- check: run server-db -->         a command on a server's database
                                          (scripts/check_doc_snippets.py, after its open pass)
    <!-- check: snippet <file>/<step> --> a step of src/mendari/dashboard/snippets/<file>,
                                          which check_doc_snippets.py runs
    <!-- check: include <file> -->        the whole snippet file, included (docs/guides/api.md)
    <!-- check: skip <reason>: <why> -->  not run, for one of SKIP_REASONS

    python3 scripts/doc_blocks.py run quickstart     # also: cli, install
    uv run python scripts/doc_blocks.py run python   # needs mendari importable
    python3 scripts/doc_blocks.py list               # every block and its marker

What a run leaves in the clone that git neither tracks nor ignores (the packs `mendari new`
writes) is removed afterwards, so a second run starts as the first did.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ("README.md", "docs/index.md", "docs/guides/api.md")
# Languages a block may be written in; the ones CHECKED must carry a marker.
CHECKED = frozenset({"bash", "sh", "shell", "zsh", "console", "python", "py", "pycon"})
ALLOWED = CHECKED | {"text", "json", "yaml", "toml", "cql", "html", "http", "jsonc", "mermaid"}
RUNNERS = ("quickstart", "cli", "python", "install", "server-db")
SKIP_REASONS = {
    "private-repo": "the repository is not public yet",
    "docker": "needs the Docker sandbox",
    "ehr": "needs an EHR's FHIR server and credentials",
    "database": "needs an outside database",
    "registry": "needs a registry and a publisher token",
    "secrets": "needs credentials or a deployment",
    "settings": "settings, not commands",
    "long-running": "runs until stopped",
    "placeholder": "names something the reader supplies",
    "ci": "each line is a step of a CI job",
}
QUICKSTART_LIMIT_S = 600  # the v1.0 exit: from the README to your own validation in 10 minutes

_MARKER = re.compile(r"^<!-- check: (?P<kind>run|snippet|include|skip) (?P<arg>[^>]*?) -->$")
# A fence of three or more backticks or tildes (CommonMark), at any indentation (MkDocs puts
# admonition content four spaces in), after any `>` of a block quote.
_QUOTE = re.compile(r"^(\s*>\s?)*")
_FENCE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*(?P<lang>[\w+-]*)[^`]*$")


@dataclass(frozen=True)
class Block:
    file: str
    line: int  # of the opening fence, 1-based
    lang: str
    text: str
    kind: str | None  # run, snippet, include or skip; None without a marker
    arg: str  # the runner, the snippet's file/step, the included file or the skip's reason

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"


def blocks(path: str, text: str | None = None) -> list[Block]:
    """Every fenced block of a Markdown file, with the marker on the line before it (if any)."""
    raw = (text if text is not None else (ROOT / path).read_text(encoding="utf-8")).split("\n")
    lines = [_QUOTE.sub("", line) for line in raw]
    found = []
    index = 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if opening is None:
            index += 1
            continue
        fence = opening["fence"]
        end = index + 1
        # Closed by a fence of the same character, at least as long, with nothing after it.
        while end < len(lines) and not (
            set(lines[end].strip()) == {fence[0]} and len(lines[end].strip()) >= len(fence)
        ):
            end += 1
        marker = _MARKER.match(lines[index - 1].strip()) if index else None
        found.append(
            Block(
                file=path,
                line=index + 1,
                lang=opening["lang"].lower(),
                text="\n".join(lines[index + 1 : end]).strip("\n"),
                kind=marker["kind"] if marker else None,
                arg=marker["arg"].strip() if marker else "",
            )
        )
        index = end + 1
    return found


def every() -> list[Block]:
    return [block for path in DOCS for block in blocks(path)]


def marked(runner: str) -> list[Block]:
    """The blocks a runner runs, in reading order (`python` takes every `python <group>`)."""
    return [
        block
        for block in every()
        if block.kind == "run" and (block.arg == runner or block.arg.startswith(f"{runner} "))
    ]


def _clean_env() -> dict[str, str]:
    """This machine's environment without Mendari's, Jev's or a model's settings: a documented
    command runs as on a new machine, and nothing calls out."""
    prefixes = ("MENDARI_", "JEV_", "LLM_", "AI_GATEWAY_", "VERCEL_", "DATABASE_URL")
    return {name: value for name, value in os.environ.items() if not name.startswith(prefixes)}


def _strays() -> set[Path]:
    """What in the clone git neither tracks nor ignores: each untracked file, or an untracked
    folder whole (never a tracked folder that holds one)."""
    listed = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--directory", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split("\0")
    return {ROOT / entry.rstrip("/") for entry in listed if entry}


def _shell(block: Block, *, env: dict[str, str], cwd: Path) -> bool:
    print(f"$ {block.where}\n{block.text}", flush=True)
    done = subprocess.run(["bash", "-euo", "pipefail", "-c", block.text], cwd=cwd, env=env)
    return done.returncode == 0


def run(runner: str) -> int:
    before = _strays()
    try:
        return _run(runner)
    finally:
        import shutil

        for stray in _strays() - before:  # what the documented commands wrote into the clone
            shutil.rmtree(stray) if stray.is_dir() else stray.unlink()


def _run(runner: str) -> int:
    chosen = marked(runner)
    if not chosen:
        print(f"no block is marked `run {runner}`")
        return 1
    env = _clean_env()
    start = time.monotonic()
    if runner == "python":
        groups: dict[str, list[Block]] = {}
        for block in chosen:
            groups.setdefault(block.arg.split(" ", 1)[1], []).append(block)
        for group, members in groups.items():
            program = "\n\n".join(block.text for block in members)
            print(f"$ python ({group}: {', '.join(b.where for b in members)})", flush=True)
            if subprocess.run([sys.executable, "-c", program], cwd=ROOT, env=env).returncode:
                return 1
        return 0
    if runner == "install":
        with tempfile.TemporaryDirectory(prefix="mendari-install-") as directory:
            venv = Path(directory) / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(venv)
            if not all(_shell(block, env=env, cwd=ROOT) for block in chosen):
                return 1
            # The one install brings the server: the documented `mendari serve` starts and answers.
            return _serves(venv / "bin" / "mendari", Path(directory), env)
    if not all(_shell(block, env=env, cwd=ROOT) for block in chosen):
        print(f"the {runner} blocks failed after {time.monotonic() - start:.0f} s")
        return 1
    took = time.monotonic() - start
    if runner == "quickstart":
        print(f"the quickstart took {took:.0f} s (limit {QUICKSTART_LIMIT_S} s)")
        return 0 if took < QUICKSTART_LIMIT_S else 1
    return 0


def _serves(mendari: Path, work: Path, env: dict[str, str]) -> int:
    """`mendari serve` from the new environment answers /api/health within a minute."""
    import socket
    import urllib.request

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(
        [str(mendari), "serve", "--port", str(port), "--db", str(work / "serve.db")], env=env
    )
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):
                    print(f"mendari serve answered on port {port}")
                    return 0
            except OSError:
                if server.poll() is not None:
                    break
                time.sleep(1)
        print("mendari serve did not answer /api/health")
        return 1
    finally:
        server.terminate()
        server.wait(timeout=30)


def main(argv: list[str]) -> int:
    if argv == ["list"]:
        for block in every():
            print(f"{block.where:28} {block.lang:8} {block.kind or '-'} {block.arg}")
        return 0
    if len(argv) == 2 and argv[0] == "run" and argv[1] in RUNNERS and argv[1] != "server-db":
        return run(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
