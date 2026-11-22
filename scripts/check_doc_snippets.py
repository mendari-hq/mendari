# SPDX-License-Identifier: Apache-2.0
"""Run the API examples (`src/mendari/dashboard/snippets/*.sh`) against a local server, and the
Python examples (`*.py`) as programs (CI jobs `test` and `quickstart`, one step).

    uv run python scripts/check_doc_snippets.py           # --show: print what each file printed

Three passes, each on its own local server (the real app on a free port, with a new SQLite file):

1. **Open mode**: every step of every file, with `MENDARI_KEY` empty.
2. **Sign-in on** (an identity provider is configured but never called), so PHI mode `encrypt`
   under two keys made for the pass: a `service` key is made
   in the store, as an administrator would make it, and every step runs with it, except the
   steps whose calls the key may not make by the route rules (`server/auth.py`): today the
   whole-trail export, which needs an administrator. A request without the key must be refused,
   so the pass proves sign-in was on.
3. **Each key preset with a snippet** (`server/presets.py`, LP-12i: `rest`, `fhir` and
   `cds-hooks` today), on one server with sign-in on (`encrypt`, as in pass 2): a key made from
   the preset, as an administrator would make it, runs the steps its documentation describes
   (`Preset.examples`), and only those; a step of the same file it does not run is named. The
   presets no snippet documents are proven by tests/test_key_presets.py.

The servers listen on 127.0.0.1 and the scripts call them there, which is this machine, so open
mode's CDS Hooks services answer them without `MENDARI_CDS_SANDBOX` (LP-23c).

Each file runs under `bash -euo pipefail`, and every curl in it uses `--fail-with-body`, so a
refusal or an error fails the run with the server's answer printed. The server runs the bundled
demo agent on its offline fixture, with every `MENDARI_*` setting cleared (so no live agent) and
no Jev or LLM calls allowed: the check costs nothing and sends nothing anywhere. Needs curl and jq.
"""

from __future__ import annotations

import base64
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

sys.path.insert(0, str(Path(__file__).resolve().parent))  # doc_blocks, a script beside this one

from mendari.core.audit import AuditStore
from mendari.server import presets, security_log, snippets
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


def _throwaway_key() -> str:
    """32 random bytes, base64: a PHI key for one pass, never kept."""
    return base64.b64encode(os.urandom(32)).decode()


def _sign_in_on() -> None:
    """Sign-in on (the identity provider is never called, since nobody signs in), so PHI mode
    `encrypt`, never `off`, under two keys made for this pass alone."""
    os.environ["MENDARI_OIDC_ISSUER"] = "https://idp.example.org"
    os.environ["MENDARI_OIDC_CLIENT_ID"] = "mendari"
    os.environ["MENDARI_PHI_MODE"] = "encrypt"
    os.environ["MENDARI_PHI_KEY"] = _throwaway_key()
    os.environ["MENDARI_INPUT_HASH_KEY"] = _throwaway_key()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _server(db: Path, *, show: bool = False) -> Iterator[str]:
    """The app on a free port, built from the current environment. Its security log (a JSON line
    per request on stdout) is shown only with --show, so a failure is easy to find."""
    app = create_app(db_path=db, budget_limits=NO_REMOTE_CALLS)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    # After uvicorn's logging setup, which enables existing loggers; create_app turns it back on.
    security_log.logger.disabled = not show
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
    if key:
        _sign_in_on()
    db = work / f"{name.split()[0]}.db"
    secret = ""
    if key:
        made = AuditStore(db).create_api_key(
            name="doc-snippets", role=KEY_ROLE, org=DEFAULT_ORG, created_by="check_doc_snippets"
        )
        secret = made["key"]
    ok = True
    with _server(db, show=show) as base:
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
    if not key:  # the documented commands on a server's database: the one this pass wrote
        ok = run_server_db(db, work, base_env, show=show) and ok
    return ok


def run_presets(work: Path, base_env: dict[str, str], *, show: bool = False) -> bool:
    """Each preset's documented steps with a key made from it; False if a step failed."""
    print("sign-in on, a key from each preset:")
    os.environ.clear()
    os.environ.update(base_env)
    _sign_in_on()
    db = work / "presets.db"
    store = AuditStore(db)
    ok = True
    with _server(db, show=show) as base:
        for preset in presets.PRESETS.values():
            if not preset.examples:
                print(f"  skip {preset.id}: no snippet documents its path (tests do)")
                continue
            made = store.create_api_key(
                name=f"doc-snippets {preset.id}",
                role=preset.role,
                org=DEFAULT_ORG,
                created_by="check_doc_snippets",
                scopes=preset.scope,
                preset=preset.id,
            )
            env = {**base_env, "MENDARI": base, "MENDARI_KEY": made["key"]}
            for file in snippets.FILES:
                steps = snippets.load(file)
                chosen = [s for s in steps if f"{file}/{s.name}" in preset.examples]
                if not chosen:
                    continue
                for step in steps:
                    if step not in chosen:
                        print(f"  skip {preset.id}: {file}.sh {step.name} ({step.title})")
                if _run(file, chosen, env, work, show=show):
                    names = ", ".join(step.name for step in chosen)
                    print(f"  ok   {preset.id} ({preset.role}): {file}.sh {names}")
                else:
                    ok = False
    return ok


def run_server_db(db: Path, work: Path, base_env: dict[str, str], *, show: bool = False) -> bool:
    """The docs' blocks marked `run server-db` (`scripts/doc_blocks.py`), run as documented in a
    folder where `var/mendari.db` and `$DATABASE_URL` are the database a server wrote."""
    import doc_blocks

    (work / "var").mkdir(exist_ok=True)
    shutil.copyfile(db, work / "var" / "mendari.db")
    env = {**base_env, "DATABASE_URL": "var/mendari.db"}
    ok = True
    for block in doc_blocks.marked("server-db"):
        done = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", block.text],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if done.returncode != 0:
            print(f"  FAIL {block.where} (exit {done.returncode})")
            for line in (done.stdout + done.stderr).strip().splitlines()[-20:]:
                print(f"       {line}")
            ok = False
        else:
            if show:
                print(done.stdout.rstrip())
            print(f"  ok   {block.where}: {block.text.split()[1]} {block.text.split()[2]}")
    return ok


def run_python(base_env: dict[str, str], *, show: bool = False) -> bool:
    """The Python examples, each a program run as it is, without a server; False if one failed."""
    print("Python:")
    ok = True
    for file in snippets.PYTHON:
        done = subprocess.run(
            [sys.executable, str(snippets.DIRECTORY / f"{file}.py")],
            env=base_env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        steps = snippets.load(file, language="python")
        if done.returncode != 0:
            print(f"  FAIL {file}.py (exit {done.returncode})")
            for line in (done.stdout + done.stderr).strip().splitlines()[-40:]:
                print(f"       {line}")
            ok = False
            continue
        if show:
            print(done.stdout.rstrip())
        print(f"  ok   {file}.py: {', '.join(step.name for step in steps)}")
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
                run_presets(work, base_env, show=show),
                run_python(base_env, show=show),
            ]
    finally:
        os.environ.clear()
        os.environ.update(saved)
    print("every snippet works" if all(passes) else "a snippet failed")
    return 0 if all(passes) else 1


if __name__ == "__main__":
    sys.exit(main(show="--show" in sys.argv[1:]))
