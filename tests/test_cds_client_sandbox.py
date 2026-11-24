# SPDX-License-Identifier: Apache-2.0
"""The sandbox's signed CDS client (`sandbox/cds_client.py`, `mendari sandbox cds-client`,
LP-23e): it plays an EHR that calls the CDS Hooks services over HTTP with its own ES384 tokens.
Here the engine runs in this process, with its FHIR reads answered from the vendor-shaped
profile (`tests/test_ehr_profile.py`, the fixture agents' standard hooks:
`tests/support/sandbox.py`) or from the sandbox's own seeded messages (the bundled demo agent's
hook, as the command runs it), and fetches the client's keys from the client's own JWK Set
server over real HTTP on this machine, as the compose sandbox's engines fetch them from the
`cds-client` container. The key is made at run time and never leaves memory. CI's `sandbox-e2e`
runs the same client from another container against both compose engines, on both."""

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
from mendari.core.pack import load_pack
from mendari.errors import MendariError
from mendari.sandbox import DEMO_AGENT, cds_client, fhir_workflow
from mendari.sandbox.cds_client import ALGORITHM, LIFETIME, HookCall, SandboxClient
from mendari.sandbox.code_systems import SANDBOX_TAG
from mendari.server.app import create_app
from mendari.server.cds_clients import cds_clients
from mendari.standards import cds_hooks
from mendari.standards.fhir_client import FhirClient, allowed_servers
from mendari.standards.smart import smart_ehrs
from tests.conftest import DEMO_AGENT_DIR, security_lines
from tests.jwt_signers import PUBLIC_URL
from tests.support import ehr_profile
from tests.support import sandbox as fixtures
from tests.support.ehr_profile import BASE
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
        result = fixtures.run_profile(
            client, engine=PUBLIC_URL, fhir=BASE, today=TODAY, http=engine, fhir_http=fhir
        )
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
        refused = fixtures.run_profile(
            client, engine=PUBLIC_URL, fhir=BASE, today=TODAY, http=other, fhir_http=fhir
        )
    assert refused["discovery"]["status"] == 401
    assert refused["calls"] == [] and refused["problems"]
    assert refused["problems"][0].startswith("discovery answered 401")


def test_a_card_for_a_read_that_failed_fails_the_run(
    client: SandboxClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every hook answers `200` (LP-23d), so a read of the FHIR server that failed shows only in
    a card that says part of the record could not be read. The client reads which key and why:
    an RTF note (`notes: unsupported_type`) is what the profile leaves unread since LP-23k, and
    passes (the run above); anything else fails the run, naming the patient, the key and the
    reason, and so does such a card for a patient with no note. Here the engine's FHIR server
    answers every search with a `400`, as HAPI could a prefetch."""
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
        result = fixtures.run_profile(
            client, engine=PUBLIC_URL, fhir=BASE, today=TODAY, http=engine, fhir_http=fhir
        )
    assert {c["status"] for c in result["calls"]} == {200}
    failed = result["problems"]
    assert failed and all(": could not read " in problem for problem in failed), failed
    assert all("(only an RTF note may be unread)" in problem for problem in failed)
    named = {problem.split(": ")[0].rsplit(" for ", 1)[1] for problem in failed}
    without_a_note = {"ehr-trial-4", "ehr-trial-5", "ehr-pa-4", "ehr-pa-5"}
    assert without_a_note <= named
    assert not any("notes: not_fetched" in problem for problem in failed)
    assert all(re.search(r"could not read \w+: http_status ", problem) for problem in failed)


def test_only_an_rtf_note_may_be_left_unread() -> None:
    """What the client takes as unread, card by card: `notes: unsupported_type` for a patient
    whose linked note is RTF (since LP-23k the packs read a text/plain or text/html one, so
    nothing for those), nothing for one without a note; and what a card names (its detail's
    `- key: reason` lines), or None for a card that is not an unreadable ABSTAIN's."""
    by_id = {p.id: p for p in ehr_profile.patients(TODAY)}
    assert fixtures.expected_unread(by_id["ehr-trial-3"]) == {("notes", "unsupported_type")}
    assert fixtures.expected_unread(by_id["ehr-pa-3"]) == {("notes", "unsupported_type")}
    assert fixtures.expected_unread(by_id["ehr-trial-1"]) == set()  # a text/plain note
    assert fixtures.expected_unread(by_id["ehr-pa-2"]) == set()  # a text/html note
    assert fixtures.expected_unread(by_id["ehr-pa-4"]) == set()  # no note

    def called(patient: str, answer: httpx.Response) -> None:
        call = fixtures.profile_call("svc", "order-sign", by_id[patient])
        cds_client._called(call, "order-sign", answer, problems)

    detail = f"*status* *notice*\n\n{cds_hooks.UNREADABLE_DETAIL}\n\n- notes: unsupported_type\n"
    card = {"summary": cds_hooks.UNREADABLE_SUMMARY, "detail": detail + "- conditions: not_r4"}
    assert cds_client.unread_of(card) == {("notes", "unsupported_type"), ("conditions", "not_r4")}
    assert cds_client.unread_of({"summary": "Routed to cardiology", "detail": detail}) is None
    problems: list[str] = []
    answer = httpx.Response(
        200, json={"cards": [{**card, "indicator": "info", "source": {"label": "x"}}]}
    )
    called("ehr-pa-4", answer)
    assert problems == [
        "svc for ehr-pa-4: could not read conditions: not_r4 (only an RTF note may be unread)",
        "svc for ehr-pa-4: could not read notes: unsupported_type (only an RTF note may be unread)",
    ]
    problems.clear()
    called("ehr-pa-3", answer)
    assert problems == [
        "svc for ehr-pa-3: could not read conditions: not_r4 (only an RTF note may be unread)"
    ]
    problems.clear()
    linked = {
        "summary": cds_hooks.UNREADABLE_SUMMARY,
        "detail": f"{detail}".replace("unsupported_type", "not_fetched"),
    }
    called(
        "ehr-pa-1",
        httpx.Response(
            200, json={"cards": [{**linked, "indicator": "info", "source": {"label": "x"}}]}
        ),
    )
    assert problems == [  # a readable note the hook did not read
        "svc for ehr-pa-1: could not read notes: not_fetched (only an RTF note may be unread)"
    ]
    problems.clear()
    unnamed = {
        "cards": [{**card, "detail": "nothing", "indicator": "info", "source": {"label": "x"}}]
    }
    called("ehr-pa-1", httpx.Response(200, json=unnamed))
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
    monkeypatch.setattr(cds_client, "sandbox_calls", lambda agent, fhir, **_: [])
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
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


# -- the sandbox's own cases: what `mendari sandbox cds-client` calls ---------------------------


def stored(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A seed transaction's resources as a FHIR server holds them: each under an id, every
    reference to another resolved to its `<type>/<id>`."""
    ids = {e["fullUrl"]: f"{e['resource']['resourceType']}/s{n}" for n, e in enumerate(entries)}
    held = []
    for n, entry in enumerate(entries):
        resource = json.loads(json.dumps(entry["resource"]))
        resource["id"] = f"s{n}"
        for name in ("subject", "sender"):
            if name in resource:
                resource[name] = {"reference": ids[resource[name]["reference"]]}
        held.append(resource)
    return held


def sandbox_server(held: list[dict[str, Any]]) -> Handler:
    """A FHIR server holding the sandbox's seeded resources: `profile_server`'s reads and
    patient searches, and the sandbox's own search for what it tagged, by type."""
    reads = profile_server(held)
    tag = f"{SANDBOX_TAG['system']}|{SANDBOX_TAG['code']}"

    def answer(request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        if query.get("_tag") != tag:
            return reads(request)
        kind = request.url.path.rsplit("/", 1)[-1]
        found = [r for r in held if r["resourceType"] == kind]
        if "category" in query:  # a token search, as HAPI answers one
            system, _, code = query["category"].partition("|")
            found = [
                r
                for r in found
                if any(
                    (c.get("system"), c.get("code")) == (system, code)
                    for concept in r.get("category", [])
                    for c in concept.get("coding", [])
                )
            ]
        entries = [{"resource": r} for r in found[: int(query["_count"])]]
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": entries})

    return answer


@pytest.mark.parametrize("signed_in", [False, True], ids=["open-mode", "sign-in-on"])
def test_the_sandbox_client_calls_the_demo_agents_hook_on_the_seeded_messages(
    client: SandboxClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: bool
) -> None:
    """As `mendari sandbox cds-client` runs with no agent named: the bundled demo agent's
    service, once for each seeded message it finds (the tagged Communications), the context
    naming the patient and the message as the service's prefetch reads them by id, with no
    prefetch sent. The engine reads each from the FHIR server and answers a card: no problem."""
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    demo = load_pack(DEMO_AGENT_DIR)
    server = sandbox_server(stored(fhir_workflow.seed_bundle(demo, 6)["entry"]))
    with httpx.Client(transport=httpx.MockTransport(server)) as fhir:
        calls = cds_client.sandbox_calls(demo, BASE, limit=4, http=fhir)
        with pytest.raises(MendariError, match="run `mendari sandbox seed` first"):
            cds_client.sandbox_calls(load_pack(fixtures_dir("referral-router")), BASE, http=fhir)
    assert [call.service for call in calls] == ["mendari.portal-message-agent"] * 4
    assert [sorted(call.context) for call in calls] == [["communicationId", "userId"]] * 4
    assert len({call.patient for call in calls}) == 4 and not any(call.unread for call in calls)
    db = tmp_path / "engine.db"
    with engine_for(client, db, monkeypatch, signed_in=signed_in, fhir=server) as engine:
        result = client.run(engine=PUBLIC_URL, fhir=BASE, calls=calls, http=engine)
    assert result["problems"] == [], result["problems"]
    assert DEMO_AGENT.replace("/", ".") in result["discovery"]["services"]
    assert [(c["hook"], c["status"]) for c in result["calls"]] == [
        ("mendari-message-received", 200)
    ] * 4
    assert all(len(c["cards"]) == 1 for c in result["calls"])
    assert result["jwks_fetches"] == 1


def test_the_sandbox_client_calls_an_order_hook_with_the_case_as_the_draft_order(
    client: SandboxClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another agent is named with `--agent`: for one with an order hook, each seeded case's
    order is the hook's draft order, and the rest of the record is read from the FHIR server.
    Discovery is held to the hooks a call allows, and a service it does not list is a problem
    said once."""
    referrals = load_pack(fixtures_dir("referral-router"))
    server = sandbox_server(stored(fixtures.seed_bundle(fixtures.REFERRAL, 3)["entry"]))
    with httpx.Client(transport=httpx.MockTransport(server)) as fhir:
        calls = cds_client.sandbox_calls(referrals, BASE, http=fhir)
    assert len(calls) == 3
    for call in calls:
        (draft,) = call.context["draftOrders"]["entry"]
        assert draft["resource"]["resourceType"] == "ServiceRequest"
        assert draft["resource"]["status"] == "draft"
        assert draft["resource"]["subject"] == {"reference": f"Patient/{call.patient}"}
    db = tmp_path / "engine.db"
    unlisted = [HookCall("a.b", "p-1"), HookCall("a.b", "p-2")]
    wrong_hook = [HookCall(calls[0].service, calls[0].patient, hooks=("patient-view",))]
    with engine_for(client, db, monkeypatch, signed_in=False, fhir=server) as engine:
        result = client.run(
            engine=PUBLIC_URL, fhir=BASE, calls=[*calls, *unlisted, *wrong_hook], http=engine
        )
    assert [(c["service"], c["hook"], c["status"]) for c in result["calls"]] == [
        ("mendari.referral-router", "order-sign", 200)
    ] * 3
    assert result["problems"] == [
        "discovery lists no a hook for a.b",
        "discovery lists no patient-view hook for mendari.referral-router",
    ]
    with pytest.raises(MendariError, match="mendari/ai-guard declares no CDS Hooks service"):
        cds_client.sandbox_calls(load_pack(fixtures_dir("ai-guard")), BASE)


def fixtures_dir(name: str) -> Path:
    return ROOT / "tests" / "fixtures" / "reference-agents" / name


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
    with no condition and nothing allowed to fail: once as the sandbox ships it, and once with
    the test fixtures' agents (sandbox/fixtures.compose.yml).

    As shipped, the service loads the bundled demo agent's messages (which `workflow` may have
    loaded already), then runs the client against the open engine and the signed-in one,
    serving its JWK Set at the URL both register. Its tokens for the open engine are addressed
    to that engine's own `MENDARI_PUBLIC_URL` setting (`--audience`), so an override of it
    changes both. With the fixtures, the same service seeds the vendor-shaped EHR profile and
    calls the fixture agents' standard hooks on it (`tests/support/sandbox.py`), after the
    engines have been started again: each then fetches the new run's key afresh."""
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
    public = _compose()["services"]["engine"]["environment"]["MENDARI_PUBLIC_URL"]
    assert _default(public) == "http://engine:8000"
    override = yaml.safe_load((ROOT / "sandbox" / "fixtures.compose.yml").read_text())
    with_fixtures = override["services"]["cds-client"]
    assert with_fixtures["volumes"] == ["./tests:/home/mendari/tests:ro"]  # the support code
    assert set(with_fixtures) == {"environment", "volumes", "command"}  # the rest as shipped
    for given, program, seed in (
        (service, "mendari sandbox", "seed --fhir http://fhir:8080/fhir --count 40"),
        (
            with_fixtures,
            "python -m tests.support.sandbox",
            "seed --fhir http://fhir:8080/fhir --ehr-profile",
        ),
    ):
        command = " ".join(" ".join(given["command"]).split())
        seed = f"{program} {seed}"
        runs = [
            f"{program} cds-client --engine http://{engine}:8000 --fhir http://fhir:8080/fhir "
            "--listen 0.0.0.0:8300 --iss http://cds-client:8300"
            for engine in ("engine", "engine-signin")
        ]
        assert seed in command and all(run in command for run in runs), program
        assert command.index(seed) < command.index(runs[0]) < command.index(runs[1])
        assert command.count(" && ") == 2  # one failure stops the rest, and fails the step
        assert f"{runs[0]} --audience {public} &&" in command
        assert command.endswith(f"{runs[1]} --org pilot")
        # Each part is a command line its own parser takes: the product's, or the support
        # module's (which runs from the mounted checkout, `python -m tests.support.sandbox`).
        for part in command.removeprefix("sh -c ").split(" && "):
            argv = part.replace(public, _default(public)).removeprefix(f"{program} ").split()
            if given is service:
                assert cli.build_parser().parse_args(["sandbox", *argv]).pack == DEMO_AGENT
            else:
                assert fixtures.build_parser().parse_args(argv).fhir == "http://fhir:8080/fhir"
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["sandbox-e2e"]["steps"]
    runs_of = [" ".join(str(step.get("run", "")).split()) for step in steps]
    called = "docker compose --profile signin run --rm -T --use-aliases cds-client"
    profile = "docker compose $FIXTURES --profile signin run --rm -T --use-aliases cds-client"
    up = "docker compose --profile signin up -d --build --wait engine launcher engine-signin"
    again = "docker compose $FIXTURES --profile signin up -d --wait engine engine-signin"
    order = [runs_of.index(run) for run in (up, called, again, profile)]
    assert order == sorted(order)
    for run in (called, profile):
        step = steps[runs_of.index(run)]
        assert "if" not in step and "continue-on-error" not in step
