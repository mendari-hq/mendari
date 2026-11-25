# SPDX-License-Identifier: Apache-2.0
"""Key presets (LP-12i, server/presets.py): one per documented integration path, in one table
that drives the API, the deployment guide and the doc-snippet checks. Each documented
integration key works exactly as the docs say: a key made from a preset gets past the guard on
every call its documentation describes, and on nothing the table's computed reach leaves out."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari.core.audit import KEY_DAYS, KEY_MAX_DAYS, MACHINE_ROLES
from mendari.server import presets, snippets
from mendari.server.api_docs import FHIR_EXAMPLE
from mendari.server.app import create_app
from mendari.server.auth import (
    PEOPLE_ONLY,
    PERMISSIONS,
    Auth,
    AuthSettings,
    key_reaches,
    permission_for,
)
from mendari.server.scopes import parse
from tests.conftest import ROOT, encrypt_mode, message, signed_in
from tests.support.platform import COMMUNICATION, PATIENT
from tests.test_auth import REMOVED_ROUTES, attempt, expected

IDP = "https://idp.example.org"
# Da Vinci's preset (G4), which wave 6 removes with the routes it calls: set aside until its
# code goes, as those routes are (`tests/test_auth.py` `REMOVED_ROUTES`).
REMOVED_PRESETS = {"da-vinci"}
INBOX = "mendari-test/triage"  # the agent these tests decide with
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"
HOOK = "/cds-services/mendari-test.triage"
# Its hook's request, with the message prefetched.
HOOK_REQUEST = {
    "hook": "mendari-message-received",
    "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
    "context": {"communicationId": "msg-1", "patientId": "pat-1"},
    "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
}


@pytest.fixture
def served(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str]]:
    """Sign-in on (the identity provider is never called) on SQLite or Postgres."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    with TestClient(create_app(db_path=db_target), base_url="https://mendari.test") as client:
        yield client, db_target


def admin(db: str) -> dict[str, str]:
    with AuditStore(db) as store:
        return signed_in(store, role="admin")


def make(client: TestClient, db: str, preset_id: str, **extra: Any) -> Any:
    chosen = presets.PRESETS[preset_id]
    body = {"name": f"doc {preset_id}", "role": chosen.role, "preset": preset_id}
    return client.post("/api/auth/keys", json={**body, **extra}, headers=admin(db))


def slug(heading: str) -> str:
    """A Markdown heading's anchor as the docs site makes it (lower case, punctuation gone)."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", text)


# -- the table --------------------------------------------------------------------------------


def test_each_preset_is_a_machine_key_the_role_allows_with_a_page_that_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sign-in on, so every route it mounts is in the reach. A file, not ":memory:": nothing
    # closes an app's store, and a file's connections close after each session.
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    app = create_app(db_path=tmp_path / "routes.db")
    routes = presets.served(app.routes)
    for preset in presets.PRESETS.values():
        assert presets.ID.fullmatch(preset.id), preset.id
        assert preset.role in MACHINE_ROLES, preset.id
        if preset.scope is not None:  # canonical, and one the role can use
            assert parse(preset.scope, permissions=PERMISSIONS[preset.role]).scope == preset.scope
        assert 1 <= preset.days <= KEY_MAX_DAYS
        page, _, anchor = preset.doc.partition("#")
        text = (ROOT / "docs" / page).read_text()
        headings = {slug(h) for h in re.findall(r"^#{1,4} (.+)$", text, flags=re.MULTILINE)}
        assert anchor in headings, (preset.id, preset.doc)
        reached = set(key_reaches(preset.role, preset.scope, routes))
        assert set(preset.calls) <= reached, (preset.id, set(preset.calls) - reached)


def test_each_example_ci_runs_with_a_preset_is_one_of_its_documented_calls() -> None:
    """The snippets a preset's key runs (scripts/check_doc_snippets.py) call only what its
    documentation describes, public calls aside."""
    steps = {f"{file}/{step.name}": step for file in snippets.FILES for step in snippets.load(file)}
    for preset in presets.PRESETS.values():
        for example in preset.examples:
            assert example in steps, (preset.id, example)
            for method, path in steps[example].calls():
                if permission_for(method, path) == "public":
                    continue
                assert any(
                    m == method and presets.template(p).match(path) for m, p in preset.calls
                ), (preset.id, example, method, path)


def test_the_guide_shows_the_table_as_it_is() -> None:
    guide = (ROOT / "docs" / "deploying-with-phi.md").read_text()
    shown = (
        guide.split("<!-- presets:start", 1)[1].split("-->", 1)[1].split("<!-- presets:end -->")[0]
    )
    assert shown.strip() == presets.markdown(), "re-render the table: presets.markdown()"


def test_the_api_names_every_preset(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "schema.db"))
    schema = client.get("/openapi.json").json()["components"]["schemas"]["KeyRequest"]
    assert schema["properties"]["preset"]["enum"] == [*presets.PRESETS, None]
    assert schema["required"] == ["name", "role"]  # as before: a preset never makes role optional


# -- each documented integration key works exactly as the docs say --------------------------------


@pytest.mark.parametrize("preset_id", [p for p in presets.PRESETS if p not in REMOVED_PRESETS])
def test_a_key_from_each_preset_gets_past_the_guard_on_its_calls_and_nothing_more(
    served: tuple[TestClient, str], preset_id: str
) -> None:
    client, db = served
    preset = presets.PRESETS[preset_id]
    made = make(client, db, preset_id)
    assert made.status_code == 201, made.text
    key = made.json()
    assert (key["role"], key["scope"], key["preset"]) == (preset.role, preset.scope, preset_id)
    headers = {"authorization": f"Bearer {key['key']}"}
    every = presets.served(client.app.routes)  # type: ignore[attr-defined]
    routes = [route for route in every if route not in REMOVED_ROUTES]
    settings = AuthSettings(IDP, "mendari", None, "roles", {}, None, None)
    with AuditStore(db) as store:
        guard = Auth(settings, store)
        passed = {r for r in routes if attempt(guard, *r, headers)[0] == 200}
    oracle = {r for r in routes if expected("key", preset.role, preset.scope, *r)[0] == 200}
    public = {r for r in routes if permission_for(*r) == "public"}
    assert passed - public == oracle - public == set(key_reaches(preset.role, preset.scope, routes))
    assert set(preset.calls) <= passed  # every documented call


def test_the_documented_calls_answer_with_the_presets_keys(served: tuple[TestClient, str]) -> None:
    """End to end, on synthetic data: each path's key makes its documented calls, and answers
    as the docs say."""
    client, db = served
    keys = {
        name: {"authorization": f"Bearer {make(client, db, name).json()['key']}"}
        for name in presets.PRESETS
    }
    rest = keys["rest"]
    assert INBOX in {p["id"] for p in client.get("/api/agents", headers=rest).json()}
    decided = client.post(f"/api/agents/{INBOX}/decide", json=message(ROUTINE), headers=rest)
    assert decided.status_code == 200
    decision = decided.json()["id"]
    assert client.get(f"/api/audit/{decision}", headers=rest).status_code == 200
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "pack", "valueString": INBOX},
            {"name": "data", "resource": FHIR_EXAMPLE},
        ],
    }
    fhir = keys["fhir"]
    assert client.post("/fhir/$decide", json=parameters, headers=fhir).status_code == 200
    for path in ("/fhir/Task", "/fhir/AuditEvent", "/fhir/Provenance"):
        assert client.get(path, headers=fhir).status_code == 200, path
    hooks = keys["cds-hooks"]
    assert client.get("/cds-services", headers=hooks).status_code == 200
    assert client.post(HOOK, json=HOOK_REQUEST, headers=hooks).status_code == 200
    outcome = {"final_label": "pharmacy", "source": "ehr"}
    waiting = client.post(f"/api/audit/{decision}/outcome", json=outcome, headers=keys["outcomes"])
    assert (
        waiting.status_code == 409
    )  # past the guard: a decision waiting for review takes it first
    review = {"final_label": "pharmacy", "reviewer": "admin@default.example.org"}
    assert client.post(f"/api/review/{decision}", json=review, headers=admin(db)).status_code == 200
    recorded = client.post(f"/api/audit/{decision}/outcome", json=outcome, headers=keys["outcomes"])
    assert recorded.status_code == 200, recorded.text
    reader = keys["fhir-reader"]
    assert client.get("/fhir/Task", headers=reader).status_code == 200
    assert (
        client.post(
            f"/api/agents/{INBOX}/decide", json=message(ROUTINE), headers=reader
        ).status_code
        == 403
    )


# -- creating keys from presets ---------------------------------------------------------------


def test_a_preset_names_the_role_takes_no_scope_and_exists(served: tuple[TestClient, str]) -> None:
    client, db = served
    for extra, refused in (
        ({"preset": "nothing"}, "no such preset"),
        ({"scope": "system/Task.rs"}, "a scope or a preset, not both"),
        ({"role": "viewer"}, "send role service"),
    ):
        answer = make(client, db, "cds-hooks", **extra)
        assert answer.status_code == 422 and refused in answer.text, (extra, answer.text)
    assert make(client, db, "cds-hooks", scope=None).status_code == 201  # null is no scope


def test_a_presets_key_is_labelled_rotated_with_its_label_and_never_widened(
    served: tuple[TestClient, str],
) -> None:
    client, db = served
    made = make(client, db, "outcomes").json()
    assert made["preset"] == "outcomes"
    with AuditStore(db) as store, store._db.session(exclusive=False) as session:
        payload = json.loads(
            session.one(
                "SELECT payload FROM audit_events WHERE decision_id = ? AND event = 'key.created'",
                (f"key:{made['id']}",),
            )["payload"]
        )
    assert payload["preset"] == "outcomes" and payload["scope"] == presets.PRESETS["outcomes"].scope
    shorter = make(client, db, "outcomes", days=7).json()
    assert shorter["expires_at"] < made["expires_at"]  # `days` sent wins over the preset's
    rotated = client.post(f"/api/auth/keys/{made['id']}/rotate", headers=admin(db)).json()
    assert (rotated["preset"], rotated["scope"]) == ("outcomes", made["scope"])
    listed = {k["id"]: k for k in client.get("/api/auth/keys", headers=admin(db)).json()}
    assert listed[made["id"]]["preset"] == "outcomes"


def test_a_key_made_without_a_preset_is_as_before(served: tuple[TestClient, str]) -> None:
    client, db = served
    body = {"name": "plain", "role": "service", "scope": "https://mendari.co/fhir/scope/decide"}
    made = client.post("/api/auth/keys", json=body, headers=admin(db)).json()
    assert "preset" not in made
    with AuditStore(db) as store, store._db.session(exclusive=False) as session:
        payload = json.loads(
            session.one(
                "SELECT payload FROM audit_events WHERE decision_id = ? AND event = 'key.created'",
                (f"key:{made['id']}",),
            )["payload"]
        )
    assert "preset" not in payload
    listed = {k["id"]: k for k in client.get("/api/auth/keys", headers=admin(db)).json()}
    assert listed[made["id"]]["preset"] is None
    assert made["expires_at"] and KEY_DAYS == 90


# -- the list ----------------------------------------------------------------------------------


def test_the_presets_list_is_an_administrators_with_each_keys_reach(
    served: tuple[TestClient, str],
) -> None:
    client, db = served
    listed = client.get("/api/auth/keys/presets", headers=admin(db))
    assert listed.status_code == 200
    routes = presets.served(client.app.routes)  # type: ignore[attr-defined]
    assert listed.json() == [
        p.as_dict(key_reaches(p.role, p.scope, routes)) for p in presets.PRESETS.values()
    ]
    shape = listed.json()[0]
    assert set(shape) == {
        "id", "title", "purpose", "role", "scope", "days", "min_days", "max_days", "calls",
        "reaches", "doc",
    }  # fmt: skip
    assert (shape["min_days"], shape["max_days"]) == (1, KEY_MAX_DAYS)
    key = make(client, db, "rest").json()["key"]
    as_key = client.get("/api/auth/keys/presets", headers={"authorization": f"Bearer {key}"})
    assert as_key.status_code == 403  # people only, as managing keys is
    assert client.get("/api/auth/keys/presets").status_code == 401
    assert not PEOPLE_ONLY & {permission_for(*r) for r in presets.PRESETS["rest"].calls}


def _decided() -> tuple[Any, Any]:
    from mendari import Decision
    from mendari.core.registry import resolve_pack

    inputs = message(ROUTINE)
    return Decision(resolve_pack(INBOX))(inputs), inputs
