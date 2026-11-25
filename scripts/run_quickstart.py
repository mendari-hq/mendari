# SPDX-License-Identifier: Apache-2.0
"""Run the docs' quickstart as a new user would, and time it (CI job `quickstart`).

The commands are the blocks marked `<!-- check: run quickstart -->` in the README and the docs
home (`scripts/doc_blocks.py`), so the docs and the check can't drift apart. Each block runs in a
shell in the repository root; the run fails if a command fails or the whole takes 10 minutes or
more (the v1.0 exit: from the README to your own local validation in under 10 minutes).

    python3 scripts/run_quickstart.py

With `--wheel`, the first five minutes of a developer who installed the package instead (CI job
`quickstart-wheel`): one install command, then the demo, an agent of their own (scaffolded,
validated, tested, evaluated and probed) and the server.

    uv build --out-dir /tmp/dist && python3 scripts/run_quickstart.py --wheel /tmp/dist/*.whl

The wheel is installed alone, with no extra, into a new virtual environment outside the
repository, and the commands run in an empty folder with a home of their own and none of this
machine's Mendari or model settings: whatever they need (the bundled agent, the templates
`mendari new` writes, the console's files) comes from the wheel or is missing. The run fails if a
command fails, if the server does not answer or does not serve the console, or if the whole,
install included, takes five minutes or more.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from doc_blocks import ROOT, _clean_env, run  # a script beside this one, not a package

WHEEL_LIMIT_S = 300  # one install command and a five-minute path; it takes well under one
AGENT = "my-agent"
# What a new developer types after the install, in order, in an empty folder.
FIRST_COMMANDS = (
    ("demo",),
    ("new", AGENT, "--id", "my-clinic/my-agent"),
    ("validate", AGENT),
    ("test", AGENT),
    ("eval", AGENT, "--dataset", "examples"),
    ("probe", AGENT),
)
# What `mendari new` must have written from the wheel's templates: the agent, and the workflow
# that evaluates it on every change (docs/guides/evaluate-in-ci.md).
SCAFFOLDED = ("agent.yaml", ".github/workflows/mendari.yml")
_STATIC = re.compile(r'(?:href|src)="(/static/[^"]+)"')


def static_files(page: str) -> list[str]:
    """The console's own files a page names (`/static/...`), each once, in reading order."""
    return list(dict.fromkeys(_STATIC.findall(page)))


def _env(home: Path, venv: Path) -> dict[str, str]:
    """A new machine's environment: no Mendari or model settings (`_clean_env`), no Python
    path or environment of the clone's, a home nothing has written to, and the new environment's
    commands first on the path, as after activating it."""
    env = _clean_env()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        env.pop(name, None)
    env["HOME"] = str(home)
    env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env


def _step(shown: str, command: list[str], *, env: dict[str, str], cwd: Path) -> bool:
    print(f"$ {shown}", flush=True)
    start = time.monotonic()
    code = subprocess.run(command, env=env, cwd=cwd).returncode
    print(f"  ({time.monotonic() - start:.1f} s)", flush=True)
    if code:
        print(f"`{shown}` failed with exit {code}")
    return code == 0


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as answer:
        return answer.status, answer.read()


def _served(mendari: Path, *, env: dict[str, str], cwd: Path) -> bool:
    """`mendari serve` answers `/api/health` within a minute, serving at least the bundled
    agent, and the console's page with every file of its own that it names; then it stops."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    print(f"$ mendari serve --port {port}", flush=True)
    start = time.monotonic()
    server = subprocess.Popen([str(mendari), "serve", "--port", str(port)], env=env, cwd=cwd)
    try:
        health = None
        for _ in range(120):
            try:
                health = json.loads(_get(f"{base}/api/health")[1])
                break
            except OSError:
                if server.poll() is not None:
                    break
                time.sleep(0.5)
        if health is None:
            print("mendari serve did not answer /api/health")
            return False
        # `packs`: the count of agents served, under the name the answer has had from the start.
        if health.get("status") != "ok" or not health.get("packs"):
            print(f"mendari serve answered, but serves no agent: {health.get('status')!r}")
            return False
        page = _get(f"{base}/")[1].decode("utf-8")
        files = static_files(page)
        if not files:
            print("the console's page names none of its files")
            return False
        for path in files:
            try:
                _get(base + path)
            except OSError as error:
                print(f"the console's page names {path}, which the server does not serve: {error}")
                return False
        print(
            f"  /api/health ok, agents served: {health['packs']}; the console and "
            f"{len(files)} files of its own ({time.monotonic() - start:.1f} s)",
            flush=True,
        )
        return True
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()


def run_wheel(wheel: Path) -> int:
    """The first five minutes from `wheel`, timed (module docstring). 0 when every step passed
    within `WHEEL_LIMIT_S`."""
    wheel = wheel.resolve()
    if wheel.suffix != ".whl" or not wheel.is_file():
        print(f"{wheel}: not a wheel (build one with `uv build --out-dir DIR`)")
        return 1
    with tempfile.TemporaryDirectory(prefix="mendari-wheel-") as directory:
        scratch = Path(directory).resolve()
        if scratch.is_relative_to(ROOT):
            print(f"{scratch} is inside the repository: point TMPDIR somewhere else")
            return 1
        venv, home, work = scratch / "venv", scratch / "home", scratch / "work"
        home.mkdir()
        work.mkdir()
        env = _env(home, venv)
        python, mendari = venv / "bin" / "python", venv / "bin" / "mendari"
        start = time.monotonic()
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        install = [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
        if not _step(f"pip install {wheel.name}", [*install, str(wheel)], env=env, cwd=work):
            return 1
        # What runs below is the wheel's code, not the clone's.
        where = subprocess.run(
            [str(python), "-c", "import mendari; print(mendari.__file__)"],
            env=env,
            cwd=work,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not Path(where).resolve().is_relative_to(venv):
            print(f"mendari was imported from {where}, not from the new environment")
            return 1
        for arguments in FIRST_COMMANDS:
            shown = " ".join(("mendari", *arguments))
            if not _step(shown, [str(mendari), *arguments], env=env, cwd=work):
                return 1
            if arguments[0] == "new":
                missing = [name for name in SCAFFOLDED if not (work / AGENT / name).is_file()]
                if missing:
                    print(f"`mendari new` did not write {', '.join(missing)}")
                    return 1
        if not _served(mendari, env=env, cwd=work):
            return 1
        took = time.monotonic() - start
    print(f"the install and the first commands took {took:.0f} s (limit {WHEEL_LIMIT_S} s)")
    return 0 if took < WHEEL_LIMIT_S else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--wheel",
        type=Path,
        help="a built wheel: run the first five minutes after installing it, instead of the "
        "docs' quickstart in the clone",
    )
    args = parser.parse_args(argv)
    return run("quickstart") if args.wheel is None else run_wheel(args.wheel)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
