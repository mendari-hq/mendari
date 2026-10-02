"""Run the API examples (`src/mendari/dashboard/snippets/*.sh`) against a local server (CI job
`test`, one step).

    uv run python scripts/check_doc_snippets.py           # --show: print what each file printed

Two passes, each on its own local server (the real app on a free port, with a new SQLite file):

1. **Open mode**: every step of every file, with `MENDARI_KEY` empty.
2. **Sign-in on** (an identity provider is configured but never called): a `service` key is made
   in the store, as an administrator would make it, and every step runs with it, except the
   steps whose calls the key may not make by the route rules (`server/auth.py`): today the
   whole-trail export, which needs an administrator. A request without the key must be refused,
   so the pass proves sign-in was on.

Each file runs under `bash -euo pipefail`, and every curl in it uses `--fail-with-body`, so a
refusal or an error fails the run with the server's answer printed. The server runs the packs'
default providers (rules, naive Bayes, criteria) with every `MENDARI_*` setting cleared and no
Jev or LLM calls allowed: the check costs nothing and sends nothing anywhere. Needs curl and jq.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from mendari.core.audit import AuditStore
from mendari.server import snippets
from mendari.server.app import create_app
from mendari.server.auth import DEFAULT_ORG, PEOPLE_ONLY, PERMISSIONS, permission_for

# The key pass: a machine with the `service` role, as an integration would hold.
KEY_ROLE = "service"
NO_REMOTE_CALLS = {"jev": 0, "llm": 0}  # a provider that would call out is refused instead


def runnable_with_key(step: snippets.Step) -> bool:
    """Whether a `service` key may make every call of the step (public calls need nothing)."""
    may = PERMISSIONS[KEY_ROLE] - PEOPLE_ONLY
    needed = {permission_for(method, path) for method, path in step.calls()}
    return all(permission == "public" or permission in may for permission in needed)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _server(db: Path) -> Iterator[str]:
    """The app on a free port, built from the current environment."""
    app = create_app(db_path=db, budget_limits=NO_REMOTE_CALLS)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the local server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code


def _run(
    file: str, steps: list[snippets.Step], env: dict[str, str], cwd: Path, *, show: bool = False
) -> bool:
    script = "\n\n".join(step.text for step in steps)
    done = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if done.returncode != 0:
        print(f"  FAIL {file}.sh (exit {done.returncode})")
        for line in (done.stdout + done.stderr).strip().splitlines()[-40:]:
            print(f"       {line}")
        return False
    if show:
        print(done.stdout.rstrip())
    return True


def run_pass(
    name: str, *, key: bool, work: Path, base_env: dict[str, str], show: bool = False
) -> bool:
    """One pass over every file; False if a step failed."""
    print(f"{name}:")
    os.environ.clear()
    os.environ.update(base_env)
    if key:  # sign-in on; the identity provider is never called, since nobody signs in
        os.environ["MENDARI_OIDC_ISSUER"] = "https://idp.example.org"
        os.environ["MENDARI_OIDC_CLIENT_ID"] = "mendari"
    db = work / f"{name.split()[0]}.db"
    secret = ""
    if key:
        made = AuditStore(db).create_api_key(
            name="doc-snippets", role=KEY_ROLE, org=DEFAULT_ORG, created_by="check_doc_snippets"
        )
        secret = made["key"]
    ok = True
    with _server(db) as base:
        if key and _status(f"{base}/api/packs") != 401:
            print("  FAIL sign-in is not on: a request without the key was not refused")
            return False
        env = {**base_env, "MENDARI": base, "MENDARI_KEY": secret}
        for file in snippets.FILES:
            steps = snippets.load(file)
            chosen = [step for step in steps if not key or runnable_with_key(step)]
            for step in steps:
                if step not in chosen:
                    print(f"  skip {file}.sh {step.name}: a {KEY_ROLE} key may not ({step.title})")
            if _run(file, chosen, env, work, show=show):
                print(f"  ok   {file}.sh: {', '.join(step.name for step in chosen)}")
            else:
                ok = False
    return ok


def main(show: bool = False) -> int:
    missing = [tool for tool in ("bash", "curl", "jq") if shutil.which(tool) is None]
    if missing:
        print(f"install {', '.join(missing)} to run the snippets")
        return 1
    # Nothing from this machine's settings: no provider override, PHI mode or sign-in leaks in.
    base_env = {name: value for name, value in os.environ.items() if not name.startswith("MENDARI")}
    saved = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory(prefix="mendari-snippets-") as directory:
            work = Path(directory)
            passes = [
                run_pass("open mode", key=False, work=work, base_env=base_env, show=show),
                run_pass(
                    f"sign-in on, a {KEY_ROLE} key",
                    key=True,
                    work=work,
                    base_env=base_env,
                    show=show,
                ),
            ]
    finally:
        os.environ.clear()
        os.environ.update(saved)
    print("every snippet works" if all(passes) else "a snippet failed")
    return 0 if all(passes) else 1


if __name__ == "__main__":
    sys.exit(main(show="--show" in sys.argv[1:]))
