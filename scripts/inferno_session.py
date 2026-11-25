# SPDX-License-Identifier: Apache-2.0
"""Run an Inferno suite through its web app's REST API, doing the client's part of each wait in
the engine's container (LP-23l, sandbox/inferno/README.md, "The scripted suites").

`inferno execute` runs a suite in one process with no web server, so it stops at the first test
that waits for a call: a SMART launch, a Bulk export. This driver works a
session as a person at Inferno's web app would, from this machine (Inferno's nginx on
`127.0.0.1:4580`, the scripted compose project's):

1. create a session (`POST /api/test_sessions`), with the suite's options;
2. write the run's list of tests (`--tests`) from the session: each test's id, short id, title
   and the requirement ids Inferno shows for it, for the groups the run starts and the options
   it chose (`scripts/inferno_check.py` reads it beside the results);
3. start each group in turn (`POST /api/test_runs`), each input from the preset (`--inputs`,
   `scripts/inferno_scripted_inputs.py`), else what the session holds, else its default;
4. poll each run (`GET /api/test_runs/<id>`) until it is done. A test that waits for the
   client gets the client's part, once (`CLIENT_STEPS`): a SMART launch from the launch URL
   Inferno made (`mendari sandbox smart-launch --launch-url`) or a Bulk export
   (`mendari bulk export`), each run in the engine's container with `docker compose exec`, then
   the test's resume URL. A wait the driver has no part in is left to Inferno;
5. write the session's results (`GET /api/test_sessions/<id>/results`) as `--out`, in the form
   `inferno execute --outputter json` prints them, for `scripts/inferno_check.py`, the verdict.

It exits 0 once every run is done, whatever the results: the check decides. It exits 1 when a
run cannot finish (Inferno unreachable or refusing an input, a run that outlasts `--timeout`)
or when a client step failed, after writing the results it has. The Bulk client's key is made
here for the run, an EC P-384 key: its public half is the client's registration in Inferno
(`smart_jwk_set`), and its private half goes to the export through the environment only,
never a file or a command line, and dies with the run.

    uv run python scripts/inferno_session.py smart_client_stu2_2 \\
      --inputs var/inferno/smart_client_stu2_2_inputs.json \\
      --option client_type=SMART,authorization_code,public \\
      --out var/inferno/smart_client_stu2_2_results.json \\
      --tests var/inferno/smart_client_stu2_2_tests.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

INFERNO = "http://127.0.0.1:4580"
# Inferno's URLs (INFERNO_HOST in the compose file) are on this host, which only the compose
# network reaches: the driver calls them on its own way in instead.
INFERNO_HOST = "http://nginx"
COMPOSE_FILE = "sandbox/inferno/scripted/docker-compose.yml"
ENGINE_SERVICE = "engine"
OPEN_ENGINE = "http://engine:8000"
POLL_SECONDS = 2.0
RUN_TIMEOUT = 1800.0  # seconds a run may take
UP_TIMEOUT = 300.0  # seconds Inferno's web app may take to answer after `up`
BULK_KEY_ID = "mendari-bulk-run"
BULK_ENV = "MENDARI_BULK_PRIVATE_KEY"
DONE = "done"
WAITING = "waiting"


class SessionError(RuntimeError):
    """Inferno refused a request, or a run did not finish: no complete results."""


# -- the client's part of a wait ---------------------------------------------------------------


@dataclass
class Engine:
    """Commands in the engine's container (`docker compose exec -T`), each with the names of
    environment variables it is given, by name only (`-e NAME`), so their values stay off the
    command line."""

    compose_file: str = COMPOSE_FILE
    service: str = ENGINE_SERVICE

    def command(self, args: Sequence[str], env: Sequence[str] = ()) -> list[str]:
        named = [part for name in env for part in ("-e", name)]
        return [
            "docker",
            "compose",
            "-f",
            self.compose_file,
            "exec",
            "-T",
            *named,
            self.service,
            *args,
        ]

    def run(self, args: Sequence[str], env: Mapping[str, str] | None = None) -> int:
        given = dict(env or {})
        completed = subprocess.run(  # a fixed command, no shell
            self.command(args, sorted(given)),
            env={**os.environ, **given},
            check=False,
        )
        return completed.returncode


@dataclass
class BulkKey:
    """The Bulk client's key for one run: an EC P-384 key in memory."""

    private: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP384R1())
    )
    kid: str = BULK_KEY_ID

    def pem(self) -> str:
        return self.private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()

    def jwks(self) -> dict[str, Any]:
        numbers = self.private.public_key().public_numbers()

        def b64(value: int) -> str:
            return base64.urlsafe_b64encode(value.to_bytes(48, "big")).rstrip(b"=").decode()

        key = {"kty": "EC", "crv": "P-384", "x": b64(numbers.x), "y": b64(numbers.y)}
        return {"keys": [{**key, "kid": self.kid, "alg": "ES384", "use": "sig"}]}


# A client step: given the waiting test's result (its outputs) and the run's inputs, do the
# client's part, saying any failure (`Session.problem`); returns the URL that resumes the test,
# which is called even after a failure, so the run ends and its tests say what failed (None:
# Inferno gave no such URL).
ClientStep = Callable[["Session", Mapping[str, Any], Mapping[str, str]], str | None]


def smart_launch(
    session: Session, waiting: Mapping[str, Any], inputs: Mapping[str, str]
) -> str | None:
    """SMART App Launch: open the launch URL Inferno made, in the engine's container, as a
    browser does, and read the patient with the launch's token (`--launch-url`)."""
    outputs = outputs_of(waiting)
    urls = [url for url in outputs.get("launch_urls", "").split(",") if url]
    if not urls or "continuation_url" not in outputs:
        session.problem("the SMART launch test gave no launch URL to open")
        return outputs.get("continuation_url")
    args = ["mendari", "sandbox", "smart-launch", "--engine", OPEN_ENGINE, "--launch-url", urls[0]]
    if session.engine.run(args) != 0:
        session.problem("the SMART launch in the engine failed")
    return outputs["continuation_url"]


def bulk_export(
    session: Session, waiting: Mapping[str, Any], inputs: Mapping[str, str]
) -> str | None:
    """Bulk Data: a patient-level export by the engine's client from Inferno's server, signed in
    with Backend Services (the run's key, by its environment), every file downloaded and the
    export deleted after; then the test's resume URL (`<suite>/resume_pass?id=<client id>`)."""
    client_id = inputs["client_id"]
    base = f"{INFERNO_HOST}/custom/{session.suite}"
    args = [
        "mendari", "bulk", "export", "--fhir", f"{base}/fhir", "--out", "/tmp/inferno-bulk-export",
        "--client-id", client_id, "--key-id", session.bulk_key.kid, "--scope", "system/*.rs",
    ]  # fmt: skip
    if session.engine.run(args, {BULK_ENV: session.bulk_key.pem()}) != 0:
        session.problem("the Bulk export in the engine failed")
    return f"{base}/resume_pass?id={client_id}"


# The test ids (their last part) that wait for the client, and the client's part of each.
CLIENT_STEPS: dict[str, ClientStep] = {
    "smart_client_access_alp_interaction": smart_launch,
    "bulk_data_client_wait": bulk_export,
}
# Inputs the driver makes for a suite, over the preset's.
SUITE_INPUTS: dict[str, Callable[[Session], dict[str, str]]] = {
    "bulk_data_v200_client": lambda session: {"smart_jwk_set": json.dumps(session.bulk_key.jwks())},
}


# -- the session --------------------------------------------------------------------------------


@dataclass
class Session:
    suite: str
    http: httpx.Client
    engine: Engine = field(default_factory=Engine)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    timeout: float = RUN_TIMEOUT
    bulk_key: BulkKey = field(default_factory=BulkKey)
    problems: list[str] = field(default_factory=list)
    id: str = ""
    tree: dict[str, Any] = field(default_factory=dict)

    def problem(self, text: str) -> None:
        print(f"inferno session: {text}", file=sys.stderr, flush=True)
        self.problems.append(text)

    def _json(self, response: httpx.Response, what: str) -> Any:
        if not response.is_success:
            raise SessionError(f"{what}: HTTP {response.status_code} {_errors(response)}")
        return response.json()

    def wait_until_up(self, limit: float = UP_TIMEOUT) -> None:
        started = self.clock()
        while True:
            try:
                if self.http.get("/api/version").is_success:
                    return
            except httpx.HTTPError:
                pass
            if self.clock() - started > limit:
                raise SessionError(f"Inferno did not answer within {int(limit)} s")
            self.sleep(POLL_SECONDS)

    def create(self, options: Mapping[str, str]) -> None:
        body = {
            "test_suite_id": self.suite,
            "suite_options": [{"id": key, "value": value} for key, value in options.items()],
        }
        created = self._json(self.http.post("/api/test_sessions", json=body), "a new session")
        self.id = str(created["id"])
        self.tree = created["test_suite"]
        chosen = {o["id"]: o["value"] for o in created.get("suite_options") or []}
        if any(chosen.get(key) != value for key, value in options.items()):
            raise SessionError(f"the session did not take the options {dict(options)}")

    def runnables(self, groups: Sequence[str]) -> list[dict[str, Any]]:
        """The groups to start, by short id (a nested one such as 3.4 too), or the suite."""
        if not groups:
            return [self.tree]
        found = []
        for short in groups:
            matches = [g for g in _groups(self.tree) if g.get("short_id") == short]
            if len(matches) != 1:
                raise SessionError(f"no group {short} in {self.suite}")
            found.append(matches[0])
        return found

    def session_values(self) -> dict[str, str]:
        data = self._json(
            self.http.get(f"/api/test_sessions/{self.id}/session_data"), "the session's inputs"
        )
        return {item["name"]: item["value"] for item in data if item.get("value") is not None}

    def inputs_for(
        self, runnable: Mapping[str, Any], given: Mapping[str, str]
    ) -> list[dict[str, str]]:
        """Each input of the runnable, as Inferno's own command line sends it: the preset's
        value, else the session's (an earlier test's output), else the input's default."""
        held = self.session_values()
        sent = []
        for definition in runnable.get("inputs") or []:
            name = definition["name"]
            value = given.get(name, held.get(name, definition.get("default")))
            if value is not None:
                sent.append({"name": name, "value": value})
        return sent

    def start(self, runnable: Mapping[str, Any], given: Mapping[str, str]) -> str:
        key = "test_suite_id" if runnable is self.tree else "test_group_id"
        body = {
            "test_session_id": self.id,
            key: runnable["id"],
            "inputs": self.inputs_for(runnable, given),
        }
        started = self._json(
            self.http.post("/api/test_runs", json=body), f"starting {runnable['id']}"
        )
        return str(started["id"])

    def follow(self, run_id: str, given: Mapping[str, str]) -> None:
        """Poll the run until it is done, doing the client's part of each wait once."""
        started = self.clock()
        stepped: set[str] = set()
        while True:
            run = self._json(
                self.http.get(f"/api/test_runs/{run_id}", params={"include_results": "true"}),
                "the run",
            )
            if run.get("status") == DONE:
                return
            if run.get("status") == WAITING:
                for waiting in (r for r in run.get("results") or [] if r.get("result") == "wait"):
                    if waiting["id"] in stepped:
                        continue
                    stepped.add(waiting["id"])
                    self.client_step(waiting, given)
            if self.clock() - started > self.timeout:
                self.http.delete(f"/api/test_runs/{run_id}")
                raise SessionError(f"run {run_id} did not finish within {int(self.timeout)} s")
            self.sleep(POLL_SECONDS)

    def client_step(self, waiting: Mapping[str, Any], given: Mapping[str, str]) -> None:
        test = str(waiting.get("test_id") or "")
        step = CLIENT_STEPS.get(test.rsplit("-", 1)[-1])
        if step is None:
            return  # a wait the driver has no part in: Inferno resumes it, or the run times out
        resume = step(self, waiting, given)
        if resume is not None:
            # Inferno answers a resume with a redirect to the session's page, which is not
            # followed: the run goes on whatever a browser would show.
            answer = self.http.get(_on_this_machine(resume, str(self.http.base_url)))
            if answer.status_code >= 400:
                self.problem(
                    f"resuming {test.rsplit('-', 1)[-1]} answered HTTP {answer.status_code}"
                )

    def results(self) -> list[dict[str, Any]]:
        found = self._json(self.http.get(f"/api/test_sessions/{self.id}/results"), "the results")
        if not isinstance(found, list):
            raise SessionError("the session's results are not a list")
        return found


def tests_of(runnables: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The tests under the runnables, in order: id, short id, title and requirement ids."""
    listed = []
    for runnable in runnables:
        for test in _tests(runnable):
            listed.append(
                {
                    "id": test["id"],
                    "short_id": test.get("short_id"),
                    "title": test.get("title"),
                    "requirements": list(test.get("verifies_requirements") or []),
                }
            )
    return listed


def outputs_of(result: Mapping[str, Any]) -> dict[str, str]:
    return {
        o["name"]: o["value"] for o in result.get("outputs") or [] if o.get("value") is not None
    }


def _groups(runnable: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    for group in runnable.get("test_groups") or []:
        yield group
        yield from _groups(group)


def _tests(runnable: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    for group in runnable.get("test_groups") or []:
        yield from _tests(group)
    yield from runnable.get("tests") or []


def _on_this_machine(url: str, inferno: str) -> str:
    """A URL on Inferno's host inside the compose network, as the driver reaches it."""
    if not url.startswith(f"{INFERNO_HOST}/"):
        raise SessionError(f"a resume URL off Inferno's host: {urlsplit(url).netloc}")
    ours = urlsplit(inferno)
    parts = urlsplit(url)
    return urlunsplit((ours.scheme, ours.netloc, parts.path, parts.query, ""))


def _errors(response: httpx.Response) -> str:
    """Inferno's own error text (its API's, about the inputs or the run; never a payload)."""
    try:
        body = response.json()
    except ValueError:
        return ""
    errors = body.get("errors") or body.get("error") if isinstance(body, dict) else None
    return f"({str(errors)[:300]})" if errors else ""


def read_inputs(path: Path) -> dict[str, str]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return {item["name"]: item["value"] for item in loaded.get("inputs") or []}


def run(
    session: Session,
    *,
    inputs: Mapping[str, str],
    groups: Sequence[str],
    options: Mapping[str, str],
    out: Path,
    tests: Path,
) -> int:
    session.wait_until_up()
    session.create(options)
    runnables = session.runnables(groups)
    # The groups the run covers: those started, or the suite's own for the whole suite (those
    # its options chose).
    shown = list(groups) or [g["short_id"] for g in session.tree.get("test_groups") or []]
    listing = {"suite": session.suite, "groups": shown, "tests": tests_of(runnables)}
    tests.parent.mkdir(parents=True, exist_ok=True)
    tests.write_text(json.dumps(listing, indent=2) + "\n", encoding="utf-8")
    given = {**inputs, **SUITE_INPUTS.get(session.suite, lambda _s: {})(session)}
    try:
        for runnable in runnables:
            print(f"inferno session {session.id}: {runnable.get('short_id') or 'suite'} "
                  f"{runnable.get('title', '')}", flush=True)  # fmt: skip
            session.follow(session.start(runnable, given), given)
    finally:  # what the session holds, even when a run did not finish
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(session.results(), indent=2) + "\n", encoding="utf-8")
    print(f"inferno session {session.id}: results written to {out}")
    return 1 if session.problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("suite", help="the suite's id, e.g. smart_client_stu2_2")
    parser.add_argument("--inputs", type=Path, required=True, help="the preset to take inputs from")
    parser.add_argument("--groups", nargs="*", default=[], help="short ids (default: the suite)")
    parser.add_argument(
        "--option", action="append", default=[], metavar="ID=VALUE", help="a suite option"
    )
    parser.add_argument("--out", type=Path, required=True, help="the results to write")
    parser.add_argument("--tests", type=Path, required=True, help="the run's tests to write")
    parser.add_argument(
        "--inferno", default=INFERNO, help="Inferno's web app (default: %(default)s)"
    )
    parser.add_argument("--compose-file", default=COMPOSE_FILE, help="for the engine's container")
    parser.add_argument("--timeout", type=float, default=RUN_TIMEOUT, help="seconds per run")
    args = parser.parse_args(argv)
    options = dict(option.split("=", 1) for option in args.option)
    with httpx.Client(base_url=args.inferno, timeout=60) as http:
        session = Session(
            args.suite, http, engine=Engine(compose_file=args.compose_file), timeout=args.timeout
        )
        try:
            return run(
                session,
                inputs=read_inputs(args.inputs),
                groups=args.groups,
                options=options,
                out=args.out,
                tests=args.tests,
            )
        except (SessionError, httpx.HTTPError) as error:
            print(f"inferno session: {error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
