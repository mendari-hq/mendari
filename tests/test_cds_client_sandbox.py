# SPDX-License-Identifier: Apache-2.0
"""The sandbox's signed CDS client (`sandbox/cds_client.py`, `mendari sandbox cds-client`,
LP-23e): it plays an EHR that calls the CDS Hooks services over HTTP with its own ES384 tokens.
Here the engine runs in this process, with its FHIR reads answered from the vendor-shaped
profile (`tests/test_ehr_profile.py`), and fetches the client's keys from the client's own JWK
Set server over real HTTP on this machine, as the compose sandbox's engines fetch them from the
`cds-client` container. The key is made at run time and never leaves memory. CI's `sandbox-e2e`
runs the same client from another container against both compose engines."""

from __future__ import annotations

import functools
import inspect
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import cli
from mendari.core import clock
from mendari.core.audit import AuditStore
from mendari.errors import MendariError
from mendari.sandbox import cds_client, ehr_profile
from mendari.sandbox.cds_client import ALGORITHM, LIFETIME, SandboxClient
from mendari.sandbox.ehr_profile import BASE
from mendari.server.app import create_app
from mendari.server.cds_clients import cds_clients
from mendari.standards import cds_hooks
from mendari.standards.fhir_client import FhirClient, allowed_servers
from mendari.standards.smart import smart_ehrs
from tests.conftest import security_lines
from tests.jwt_signers import PUBLIC_URL
from tests.test_cds_auth import sign_in_on
from tests.test_ehr_profile import TODAY, Handler, profile_server

ROOT = Path(__file__).resolve().parents[1]
EHR = "https://ehr.example.org"
CLIENT_ID = "sandbox-client"
PUBLIC_MEMBERS = {"crv", "kty", "x", "y", "kid", "use", "alg"}


@pytest.fixture
def client() -> Iterator[SandboxClient]:
    """The client on a free port of this machine: its key made, its JWK Set served."""
    with SandboxClient(listen=("127.0.0.1", 0)) as made:
        yield made


def engine_for(
    client: SandboxClient,
    db: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    signed_in: bool,
    registered: bool = True,
    fhir: Handler | None = None,
) -> TestClient:
    """An engine at `PUBLIC_URL` with the client registered as the entry it prints (`registered`:
    else another client is), its JWK Set's origin and the profile's FHIR server on the allow-list,
    and its FHIR reads answered from the profile (or by `fhir`)."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    if signed_in:
        sign_in_on(monkeypatch)
    entry = client.entry(client_id=CLIENT_ID, org="default", fhir_server=BASE)
    if not registered:
        entry = {**entry, "id": "another", "iss": f"{client.iss}/another"}
    monkeypatch.setenv("MENDARI_CDS_CLIENTS", json.dumps([entry]))
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", f"{EHR},{client.iss}")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", PUBLIC_URL)
    transport = httpx.MockTransport(fhir or profile_server(ehr_profile.resources(TODAY)))
    monkeypatch.setattr(cds_hooks, "FhirClient", functools.partial(FhirClient, transport=transport))
    return TestClient(create_app(db_path=db), base_url=PUBLIC_URL)


def fhir_reader() -> httpx.Client:
    """What the client reads the profile's FHIR server with (to check it holds the patients)."""
    return httpx.Client(transport=httpx.MockTransport(profile_server(ehr_profile.resources(TODAY))))


@pytest.mark.parametrize("signed_in", [False, True], ids=["open-mode", "sign-in-on"])
def test_the_sandbox_client_signs_what_the_engine_verifies(
    client: SandboxClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    signed_in: bool,
) -> None:
    """Registered by the entry it prints, the client reads discovery and calls every standard
    hook for every profile patient, each call with a new ES384 token (its `jku` the JWK Set's
    URL, `aud` the URL called, five minutes, a new `jti`): every answer is a `200`, the engine
    fetched the keys once over HTTP and kept them, every call is the principal `cds:<id>`, and
    every decision is chained as asked for by `cds <id>`. An engine that registers another client
    refuses every call, and the run fails."""
    tokens: list[str] = []
    made = client.token

    def kept(audience: str) -> str:  # each token the client sends, kept to look at after
        tokens.append(made(audience))
        return tokens[-1]

    monkeypatch.setattr(client, "token", kept)
    db = tmp_path / "engine.db"
    with engine_for(client, db, monkeypatch, signed_in=signed_in) as engine, fhir_reader() as fhir:
        capsys.readouterr()
        result = client.run(engine=PUBLIC_URL, fhir=BASE, today=TODAY, http=engine, fhir_http=fhir)
    assert result["problems"] == [], result["problems"]
    assert result["discovery"]["status"] == 200
    services = {spec.pack_id.replace("/", ".") for spec in ehr_profile.PACKS}
    assert services <= set(result["discovery"]["services"])
    patients = ehr_profile.patients(TODAY)
    assert [c["patient"] for c in result["calls"]] == [p.id for p in patients]
    assert {c["status"] for c in result["calls"]} == {200}
    assert {(c["service"], c["hook"]) for c in result["calls"]} == {
        ("mendari.clinical-trial-prescreener", "patient-view"),
        ("mendari.prior-auth-preflight", "order-sign"),
        ("mendari.referral-router", "order-sign"),
    }
    assert result["jwks_fetches"] == client.fetches == 1
    lines = security_lines(capsys.readouterr().out)
    actors = [line["actor"] for line in lines if line["event"] == "request"]
    assert actors and set(actors) == {f"cds:{CLIENT_ID}"}
    with AuditStore(db) as store:
        asked = [
            e["payload"].get("requested_by")
            for e in store.events()
            if e["event"] == "decision.created"
        ]
    assert asked and set(asked) == {f"cds {CLIENT_ID}"}
    assert len(tokens) == 1 + len(patients)
    assert len({jwt.decode(t, options={"verify_signature": False})["jti"] for t in tokens}) == len(
        tokens
    )
    public = jwt.PyJWK(client.jwk).key
    for token, path in zip(
        tokens,
        ["/cds-services", *(f"/cds-services/{c['service']}" for c in result["calls"])],
        strict=True,
    ):
        header = jwt.get_unverified_header(token)
        assert header == {"alg": ALGORITHM, "kid": client.kid, "typ": "JWT", "jku": client.jwks_uri}
        claims = jwt.decode(token, public, algorithms=[ALGORITHM], audience=f"{PUBLIC_URL}{path}")
        assert claims["iss"] == client.iss and claims["exp"] - claims["iat"] == LIFETIME
    with (
        engine_for(
            client, tmp_path / "other.db", monkeypatch, signed_in=signed_in, registered=False
        ) as other,
        fhir_reader() as fhir,
    ):
        refused = client.run(engine=PUBLIC_URL, fhir=BASE, today=TODAY, http=other, fhir_http=fhir)
    assert refused["discovery"]["status"] == 401
    assert refused["calls"] == [] and refused["problems"]
    assert refused["problems"][0].startswith("discovery answered 401")


def test_a_card_for_a_read_that_failed_fails_the_run(
    client: SandboxClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every hook answers `200` (LP-23d), so a read of the FHIR server that failed shows only in
    a card that says part of the record could not be read. The client reads which key and why:
    a linked note (`notes: not_fetched`) is what the profile leaves unread, and passes (the run
    above); anything else fails the run, naming the patient, the key and the reason, and so does
    such a card for a patient with no note. Here the engine's FHIR server answers every search
    with a `400`, as HAPI could a prefetch."""
    held = profile_server(ehr_profile.resources(TODAY))

    def searches_fail(request: httpx.Request) -> httpx.Response:
        if request.url.query:
            return httpx.Response(400, json={"resourceType": "OperationOutcome", "issue": []})
        return held(request)

    db = tmp_path / "engine.db"
    with (
        engine_for(client, db, monkeypatch, signed_in=False, fhir=searches_fail) as engine,
        fhir_reader() as fhir,
    ):
        result = client.run(engine=PUBLIC_URL, fhir=BASE, today=TODAY, http=engine, fhir_http=fhir)
    assert {c["status"] for c in result["calls"]} == {200}
    failed = result["problems"]
    assert failed and all(": could not read " in problem for problem in failed), failed
    assert all("(only a linked note may be unread)" in problem for problem in failed)
    named = {problem.split(": ")[0].rsplit(" for ", 1)[1] for problem in failed}
    without_a_note = {"ehr-trial-4", "ehr-trial-5", "ehr-pa-4", "ehr-pa-5"}
    assert without_a_note <= named
    assert not any("notes: not_fetched" in problem for problem in failed)
    assert all(re.search(r"could not read \w+: http_status ", problem) for problem in failed)


def test_only_a_linked_note_may_be_left_unread() -> None:
    """What the client takes as unread, card by card: `notes: not_fetched` for a patient with a
    note, nothing for one without; and what a card names (its detail's `- key: reason` lines),
    or None for a card that is not an unreadable ABSTAIN's."""
    by_id = {p.id: p for p in ehr_profile.patients(TODAY)}
    assert cds_client.expected_unread(by_id["ehr-trial-1"]) == {("notes", "not_fetched")}
    assert cds_client.expected_unread(by_id["ehr-pa-4"]) == set()
    detail = f"*status* *notice*\n\n{cds_hooks.UNREADABLE_DETAIL}\n\n- notes: not_fetched\n"
    card = {"summary": cds_hooks.UNREADABLE_SUMMARY, "detail": detail + "- conditions: not_r4"}
    assert cds_client.unread_of(card) == {("notes", "not_fetched"), ("conditions", "not_r4")}
    assert cds_client.unread_of({"summary": "Routed to cardiology", "detail": detail}) is None
    problems: list[str] = []
    answer = httpx.Response(
        200, json={"cards": [{**card, "indicator": "info", "source": {"label": "x"}}]}
    )
    cds_client._called("svc", "order-sign", by_id["ehr-pa-4"], answer, problems)
    assert problems == [
        "svc for ehr-pa-4: could not read conditions: not_r4 (only a linked note may be unread)",
        "svc for ehr-pa-4: could not read notes: not_fetched (only a linked note may be unread)",
    ]
    problems.clear()
    cds_client._called("svc", "order-sign", by_id["ehr-pa-1"], answer, problems)
    assert problems == [
        "svc for ehr-pa-1: could not read conditions: not_r4 (only a linked note may be unread)"
    ]
    problems.clear()
    unnamed = {
        "cards": [{**card, "detail": "nothing", "indicator": "info", "source": {"label": "x"}}]
    }
    cds_client._called(
        "svc", "order-sign", by_id["ehr-pa-1"], httpx.Response(200, json=unnamed), problems
    )
    assert problems == ["svc for ehr-pa-1: card 1 says part of the record was not read, not which"]


def test_it_keeps_its_key_in_memory_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each start makes a new EC P-384 key, which never leaves the process: the JWK Set it serves
    and the registry entry `mendari sandbox cds-client` prints hold the public key or where to get
    it, never a private member or a PEM; nothing is written to disk, and the module has no way to
    write or export a key. Its JWK Set server stops with it. A run with problems exits 1."""
    monkeypatch.chdir(tmp_path)
    with (
        SandboxClient(listen=("127.0.0.1", 0)) as first,
        SandboxClient(listen=("127.0.0.1", 0)) as second,
    ):
        assert first.kid != second.kid and first.jwk["x"] != second.jwk["x"]
        served = httpx.get(first.jwks_uri).json()
        assert served == {"keys": [first.jwk]}
        assert set(first.jwk) == PUBLIC_MEMBERS and first.jwk["crv"] == "P-384"
        assert httpx.get(f"{first.iss}/anything-else").status_code == 404
        uri = first.jwks_uri
    with pytest.raises(httpx.ConnectError):
        httpx.get(uri, timeout=2)
    source = inspect.getsource(cds_client)
    for call in ("private_bytes", "open(", "write_text", "write_bytes", "PrivateFormat", "pem"):
        assert call not in source, call

    def run(self: SandboxClient, **_: Any) -> dict[str, Any]:
        return {"discovery": {"status": 401}, "calls": [], "jwks_fetches": 0, "problems": ["no"]}

    monkeypatch.setattr(SandboxClient, "run", run)
    code = cli.main(
        ["sandbox", "cds-client", "--engine", PUBLIC_URL, "--fhir", BASE, "--listen", "127.0.0.1:0"]
    )
    out, err = capsys.readouterr()
    assert code == 1 and "cds-client: no" in err
    printed = out.splitlines()[0]
    assert printed.startswith("MENDARI_CDS_CLIENTS entry: ")
    entry = json.loads(printed.removeprefix("MENDARI_CDS_CLIENTS entry: "))
    assert set(entry) == {"id", "title", "iss", "org", "fhir_servers", "jwks_uri"}
    assert entry["jwks_uri"] == f"{entry['iss']}/jwks.json"
    assert '"d"' not in out and "PRIVATE KEY" not in out + err
    assert list(tmp_path.iterdir()) == []


# -- the compose sandbox and CI --------------------------------------------------------------------


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return loaded


def _default(value: str) -> str:
    """A compose value as it stands when the shell does not set it: `${NAME:-default}`'s
    default, or `${NAME-default}`'s."""
    found = re.fullmatch(r"\$\{[A-Z_]+:?-(.*)\}", value)
    return found.group(1) if found else value


@pytest.mark.parametrize(
    ("engine", "signed_in", "org"),
    [("engine", False, "default"), ("engine-signin", True, "pilot")],
)
def test_both_compose_engines_register_the_sandbox_client(
    monkeypatch: pytest.MonkeyPatch, engine: str, signed_in: bool, org: str
) -> None:
    """Each engine's `MENDARI_CDS_CLIENTS` is a registry it starts with, under its own settings:
    the client at `http://cds-client:8300`, its keys from that origin (on the engine's
    allow-list), sending the sandbox's HAPI as `fhirServer`, in the engine's organization, its
    tokens addressed to the engine's public URL."""
    settings = {
        k: _default(str(v)) for k, v in _compose()["services"][engine]["environment"].items()
    }
    for name in ("MENDARI_CDS_CLIENTS", "MENDARI_PUBLIC_URL", "MENDARI_FHIR_ALLOWED_SERVERS"):
        monkeypatch.setenv(name, settings[name])
    monkeypatch.setenv("MENDARI_SMART_EHRS", settings.get("MENDARI_SMART_EHRS", ""))
    registry = cds_clients(signed_in=signed_in, ehrs=smart_ehrs(), allowed=allowed_servers())
    assert registry is not None and registry.public_url == f"http://{engine}:8000"
    (registered,) = registry.clients
    assert (registered.id, registered.iss, registered.org) == (
        CLIENT_ID,
        "http://cds-client:8300",
        org,
    )
    assert registered.jwks_uri == "http://cds-client:8300/jwks.json"
    assert registered.fhir_servers == ("http://fhir:8080/fhir",)


def test_the_open_engine_takes_your_own_public_url_with_the_registry_set_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The open engine's public URL and allow-list are yours to set (`${...:-default}`), and with
    the registry on, a URL it does not take (plain http on a LAN address) stops startup, so the
    registry is yours to empty too: `${MENDARI_CDS_CLIENTS-...}`, which only an unset variable
    defaults. Set empty, there is no registry and no URL check (as before LP-23e). The signed-in
    engine's settings are fixed, so its registry is too."""
    environment = _compose()["services"]["engine"]["environment"]
    assert str(environment["MENDARI_CDS_CLIENTS"]).startswith("${MENDARI_CDS_CLIENTS-[")
    assert str(environment["MENDARI_PUBLIC_URL"]).startswith("${MENDARI_PUBLIC_URL:-")
    monkeypatch.setenv("MENDARI_CDS_CLIENTS", _default(str(environment["MENDARI_CDS_CLIENTS"])))
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "http://192.168.1.5:8000")
    monkeypatch.setenv(
        "MENDARI_FHIR_ALLOWED_SERVERS", _default(environment["MENDARI_FHIR_ALLOWED_SERVERS"])
    )
    with pytest.raises(MendariError, match="MENDARI_PUBLIC_URL must be an https URL"):
        cds_clients(signed_in=False, ehrs={}, allowed=allowed_servers())
    monkeypatch.setenv("MENDARI_CDS_CLIENTS", "")
    assert cds_clients(signed_in=False, ehrs={}, allowed=allowed_servers()) is None
    signin = _compose()["services"]["engine-signin"]["environment"]
    assert "${" not in str(signin["MENDARI_CDS_CLIENTS"]) + str(signin["MENDARI_PUBLIC_URL"])


def test_ci_runs_the_signed_client_against_both_engines() -> None:
    """WC-4a, CE-3a: CI's `sandbox-e2e` runs the compose service `cds-client` from its own
    container, named `cds-client` on the network (`--use-aliases`), after both engines are up,
    with no condition and nothing allowed to fail. The service seeds the vendor-shaped profile,
    then runs the client against the open engine and the signed-in one, serving its JWK Set at
    the URL both register. Its tokens for the open engine are addressed to that engine's own
    `MENDARI_PUBLIC_URL` setting (`--audience`), so an override of it changes both."""
    service = _compose()["services"]["cds-client"]
    assert service["profiles"] == ["signin"]
    assert service["image"] == _compose()["services"]["engine"]["image"]
    assert {
        name
        for name, wait in service["depends_on"].items()
        if wait["condition"] == "service_healthy"
    } == {
        "engine",
        "engine-signin",
    }
    command = " ".join(" ".join(service["command"]).split())
    seed = "mendari sandbox seed --fhir http://fhir:8080/fhir --pack ehr-profile"
    runs = [
        f"mendari sandbox cds-client --engine http://{engine}:8000 --fhir http://fhir:8080/fhir "
        "--listen 0.0.0.0:8300 --iss http://cds-client:8300"
        for engine in ("engine", "engine-signin")
    ]
    assert seed in command and all(run in command for run in runs)
    assert command.index(seed) < command.index(runs[0]) < command.index(runs[1])
    assert " && " in command  # one failure stops the rest, and fails the step
    public = _compose()["services"]["engine"]["environment"]["MENDARI_PUBLIC_URL"]
    assert f"{runs[0]} --audience {public} &&" in command
    assert _default(public) == "http://engine:8000"
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["sandbox-e2e"]["steps"]
    runs_of = [" ".join(str(step.get("run", "")).split()) for step in steps]
    called = "docker compose --profile signin run --rm -T --use-aliases cds-client"
    up = "docker compose --profile signin up -d --build --wait engine launcher engine-signin"
    assert called in runs_of and up in runs_of
    assert runs_of.index(up) < runs_of.index(called)
    step = steps[runs_of.index(called)]
    assert "if" not in step and "continue-on-error" not in step
