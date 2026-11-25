# SPDX-License-Identifier: Apache-2.0
"""An organization's administrative events (LP-12g, ADR-0008).

An organization's administrators read, and export as FHIR, the chained events of what it owns
(its keys, clients, approvals, holds, evaluation reports, record, memberships, invitations and
requests to join), attributed by the row each is about, never another organization's nor the
deployment's own. In memberships mode the deployment-wide views are the operator's: the whole
chain's verification, the totals in health, the deployment's records and its holds' reasons.
Claims mode keeps them as before. On SQLite and Postgres (`db_target`), in both modes."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core import tenancy
from mendari.core.audit import ADMINISTRATIVE_KINDS, OFF_DECISION_KINDS
from mendari.core.evalsets import EvalsetStore
from mendari.core.pack import load_pack
from mendari.server.app import create_app
from mendari.standards import audit_fhir, fhir_structure
from mendari.standards.terminology import Terminology
from tests.conftest import (
    INBOX_DIR,
    encrypt_mode,
    evalset_keys_for_test,
    hand_written,
    member,
    message,
    signed_in,
)
from tests.test_chain_orgs import jwks

IDP = "https://idp.example.org"
BASE = "https://mendari.test"
INBOX = "mendari/patient-inbox-router"
ORGS = ("clinic-zqalpha", "clinic-zqbravo")
REPORT = {"created_at": "2026-10-01T00:00:00+00:00", "dataset": {"name": "local"}}
EVALSETS = evalset_keys_for_test()  # hidden evaluation sets' throwaway keys (LP-11e)


def serving(db: str, monkeypatch: pytest.MonkeyPatch, accounts: str) -> TestClient:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    if accounts == "memberships":
        monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    return TestClient(create_app(db_path=db), base_url=BASE)


def admin_of(store: AuditStore, accounts: str, org: str, role: str = "admin") -> dict[str, str]:
    if accounts == "memberships":
        return member(store, org=org, roles=[role], sub=f"{role}-{org}")
    return signed_in(store, role=role, org=org)


def seed(store: AuditStore, org: str) -> dict[str, str]:
    """One of each administrative row of `org`, each with its marker in its free text; the
    chain ids made, by kind of subject."""
    marker = org.removeprefix("clinic-")
    if store.organization(org) is None:
        store.create_organization(org=org, name=f"Clinic {marker}", created_by="ops")
    key = store.create_api_key(name=f"key {marker}", role="service", org=org, created_by="ops")
    store.rotate_api_key(key["id"], rotated_by="ops")
    store.revoke_api_key(key["id"], revoked_by="ops")
    client = store.register_client(
        name=f"client {marker}", role="viewer", org=org, created_by="ops",
        scopes="system/Task.rs", jwks=jwks(),
    )  # fmt: skip
    store.replace_client_keys(client["client_id"], org, replaced_by="keeper", jwks=jwks())
    store.revoke_client(client["client_id"], revoked_by="ops")
    hold = store.hold_automation(
        org=org, pack_id=None, pack_digest=None, reason=f"hold {marker}", set_by="ops"
    )
    store.lift_hold(hold["id"], lifted_by="ops", org=org)
    with tenancy.acting_for(org):
        approval = store.approve_automation(
            pack_id=INBOX, pack_digest="d" * 64, risk_level=2, approver="cmo",
            reference=f"minutes {marker}", expires_at="2999-01-01T00:00:00+00:00",
        )  # fmt: skip
        store.revoke_approval(approval["id"], revoked_by="cmo")
        store.save_report({**REPORT, "id": f"r-{org}", "pack": {"id": INBOX}})
    invitation = store.create_invitation(
        org=org, email=f"lee@{marker}.example.org", roles=["viewer"], created_by="ops"
    )
    store.revoke_invitation(invitation["id"], org=org, revoked_by="ops")
    # A hidden evaluation set (LP-11e): its name and its case are sealed, never in an event.
    hidden = EvalsetStore(store, EVALSETS)
    made_set = hidden.create_set(org, load_pack(INBOX_DIR), f"set {marker}", created_by="ops")
    hidden.add_cases(made_set["set"], [hand_written(f"A hidden case {marker}", author=marker)])
    return {
        "evalset": f"evalset:{made_set['set']}",
        "org": f"org:{org}",
        "key": f"key:{key['id']}",
        "client": f"client:{client['client_id']}",
        "hold": f"hold:{hold['id']}",
        "governance": f"governance:{approval['id']}",
        "eval": f"eval:r-{org}",
        "invitation": f"invitation:{invitation['id']}",
    }


@pytest.fixture(params=["claims", "memberships"])
def two(
    request: pytest.FixtureRequest, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    """A server with two organizations' administrative rows, and the deployment's own: a hold
    for every organization, a key rotation's batch, a purge."""
    accounts = request.param
    with serving(db_target, monkeypatch, accounts) as client, AuditStore(db_target) as store:
        headers = {org: admin_of(store, accounts, org) for org in ORGS}
        made = {org: seed(store, org) for org in ORGS}
        everyone = store.hold_automation(
            org=None, pack_id=None, pack_digest=None, reason="operator drill", set_by="ops"
        )
        store.purge(30, source="command")
        yield {
            "client": client,
            "store": store,
            "accounts": accounts,
            "headers": headers,
            "made": made,
            "everyone": everyone,
        }


def listed(client: TestClient, headers: dict[str, str], **query: Any) -> list[dict[str, Any]]:
    """Every item of the list, page by page."""
    items: list[dict[str, Any]] = []
    after = None
    while True:
        params = {**query, **({"after": after} if after else {})}
        answer = client.get("/api/audit/administrative", params=params, headers=headers)
        assert answer.status_code == 200, answer.text
        items += answer.json()["items"]
        after = answer.json()["next"]
        if after is None:
            return items


def test_an_organization_reads_only_its_own_administrative_events(two: dict[str, Any]) -> None:
    client, made = two["client"], two["made"]
    alpha, bravo = ORGS
    items = listed(client, two["headers"][alpha])
    subjects = {item["subject"] for item in items}
    assert set(made[alpha].values()) <= subjects
    assert {subject.split(":")[0] for subject in subjects} <= {
        "org", "key", "client", "hold", "governance", "eval", "invitation", "membership",
        "access", "evalset",
    }  # fmt: skip
    text = json.dumps(items)
    assert "zqbravo" not in text and "operator drill" not in text
    assert not set(made[bravo].values()) & subjects
    assert f"hold:{two['everyone']['id']}" not in subjects
    assert {item["event"] for item in items} <= set(ADMINISTRATIVE_KINDS)
    assert all(set(item) == {"subject", "event", "payload", "created_at", "hash"} for item in items)
    assert all(item["payload"]["org"] == alpha for item in items if "org" in item["payload"])
    # Paging by two gives the same list, in chain order.
    assert listed(client, two["headers"][alpha], limit=2) == items
    # Narrowed: by kind, by subject, by time.
    revoked = listed(client, two["headers"][alpha], kind=["key.revoked", "client.revoked"])
    assert {item["event"] for item in revoked} == {"key.revoked", "client.revoked"}
    one = listed(client, two["headers"][alpha], subject=made[alpha]["key"])
    assert [item["event"] for item in one] == ["key.created", "key.rotated", "key.revoked"]
    assert listed(client, two["headers"][alpha], subject=made[bravo]["key"]) == []
    assert listed(client, two["headers"][alpha], since="2999-01-01T00:00:00Z") == []
    assert listed(client, two["headers"][alpha], until="2000-01-01T00:00:00+00:00") == []
    # The same instant written at another offset is compared as stored (UTC), never as text.
    first = datetime.fromisoformat(items[0]["created_at"])
    last = datetime.fromisoformat(items[-1]["created_at"])
    west, east = timezone(timedelta(hours=-4)), timezone(timedelta(hours=14))
    after_last = (last + timedelta(seconds=1)).astimezone(west).isoformat()
    before_first = (first - timedelta(seconds=1)).astimezone(east).isoformat()
    assert listed(client, two["headers"][alpha], since=after_last) == []
    assert listed(client, two["headers"][alpha], until=before_first) == []
    assert listed(client, two["headers"][alpha], since=first.astimezone(west).isoformat()) == items


def test_a_cursor_of_another_organization_is_refused_as_any_other(two: dict[str, Any]) -> None:
    client, headers = two["client"], two["headers"]
    alpha, bravo = ORGS
    theirs = client.get("/api/audit/administrative", params={"limit": 1}, headers=headers[bravo])
    foreign = theirs.json()["next"]
    assert foreign
    unknown = audit_fhir_cursor("key:nobody", "0" * 64)
    answers = [
        client.get(
            "/api/audit/administrative", params={"after": after, **order}, headers=headers[alpha]
        )
        for after in (foreign, unknown, "not-a-cursor")
        for order in ({}, {"order": "desc"})
    ]
    assert {answer.status_code for answer in answers} == {400}
    assert len({answer.json()["detail"] for answer in answers}) == 1


def test_newest_first_is_the_same_list_reversed_page_by_page(two: dict[str, Any]) -> None:
    """`order=desc` (the Activity tab's) lists the chain's events newest first: paged by any
    size, narrowed by kind or time, it is the oldest-first list reversed; the default and its
    cursor are as they were."""
    client, headers = two["client"], two["headers"][ORGS[0]]
    items = listed(client, headers)
    assert len(items) > 6
    assert listed(client, headers, order="asc") == items
    for limit in (1, 2, 3, 500):
        assert listed(client, headers, order="desc", limit=limit) == items[::-1]
    middle = items[len(items) // 2]["created_at"]
    for query in ({"kind": ["key.revoked", "client.revoked"]}, {"since": middle}):
        oldest_first = listed(client, headers, **query)
        assert (
            oldest_first
            and listed(client, headers, order="desc", limit=1, **query) == (oldest_first[::-1])
        )
    # The first page is the newest; its `next` goes on to older ones.
    first = client.get(
        "/api/audit/administrative", params={"order": "desc", "limit": 2}, headers=headers
    ).json()
    assert first["items"] == items[:-3:-1] and first["next"]
    refused = client.get("/api/audit/administrative", params={"order": "up"}, headers=headers)
    assert refused.status_code == 422


def test_newest_first_pages_through_events_of_one_instant(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events written in the same millisecond keep their chain order, newest first too: no
    page repeats or skips one, whatever the page size and wherever a page ends."""
    import mendari.core.audit as audit_module

    org = ORGS[0]
    with AuditStore(db_target) as store:
        for stamp in ("2026-10-01T00:00:00.000+00:00", "2026-10-02T00:00:00.000+00:00"):
            monkeypatch.setattr(audit_module, "_now", lambda stamp=stamp: stamp)
            if store.organization(org) is None:
                store.create_organization(org=org, name="Clinic", created_by="ops")
            for n in range(4):
                key = store.create_api_key(name=f"k{n}", role="viewer", org=org, created_by="ops")
                store.revoke_api_key(key["id"], revoked_by="ops")
        monkeypatch.undo()
        chain = store.administrative_events(org, limit=500).rows
        assert len(chain) == 17  # the organization's record, then 8 keys made and revoked
        assert len({event["created_at"] for event in chain}) == 2  # 9 events, then 8, alike

        def pages(limit: int, **filters: Any) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            after = None
            while True:
                page = store.administrative_events(
                    org, after=after, limit=limit, newest_first=True, **filters
                )
                found += page.rows
                if page.after is None:
                    return found
                after = page.after

        for limit in (1, 2, 3, 4, 5, 16, 17, 18):
            assert pages(limit) == chain[::-1]
        since = "2026-10-02T00:00:00.000+00:00"
        assert pages(3, since=since) == chain[:8:-1]
        assert pages(3, until=since) == chain[8::-1]
        assert pages(2, kinds=["key.revoked"]) == [
            event for event in chain[::-1] if event["event"] == "key.revoked"
        ]
        with pytest.raises(ValueError, match="not a cursor of this list"):
            store.administrative_events(org, after=("key:nobody", "0" * 64), newest_first=True)


def audit_fhir_cursor(subject: str, digest: str) -> str:
    from mendari.standards.search import encode_cursor

    return encode_cursor({"subject": subject, "hash": digest})


def test_the_administrative_export_is_r4_audit_events_of_ids_and_codes(
    two: dict[str, Any],
) -> None:
    client, made = two["client"], two["made"]
    alpha = ORGS[0]
    answer = client.get("/api/audit/administrative/export/fhir", headers=two["headers"][alpha])
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("application/fhir+ndjson")
    resources = [json.loads(line) for line in answer.text.splitlines()]
    assert resources and {r["resourceType"] for r in resources} == {"AuditEvent"}
    for resource in resources:
        assert fhir_structure.check(resource) == [], resource
        assert len(resource["id"]) == 64
    exported = {r["entity"][0]["what"]["identifier"]["value"] for r in resources}
    assert set(made[alpha].values()) <= exported
    # Ids and codes only: no name, reason or reference, the organization's own included.
    assert "zqalpha" not in answer.text.replace(alpha, "")
    assert "zqbravo" not in answer.text and "seq" not in {
        detail["type"] for r in resources for detail in r["entity"][0]["detail"]
    }
    listed_hashes = [item["hash"] for item in listed(client, two["headers"][alpha])]
    assert [r["id"] for r in resources] == listed_hashes
    # Who acted is the agent: `replaced_by` names a person on a key set's replacement, and the
    # replacement key on a rotation, where it stays a detail.
    replaced = next(r for r in resources if r["subtype"][0]["code"] == "client.keys_replaced")
    assert replaced["agent"] == [{"who": {"display": "keeper"}, "requestor": True}]
    assert "replaced_by" not in {d["type"] for d in replaced["entity"][0]["detail"]}
    rotated = next(r for r in resources if r["subtype"][0]["code"] == "key.rotated")
    replacement = next(d for d in rotated["entity"][0]["detail"] if d["type"] == "replaced_by")
    assert f"key:{replacement['valueString']}" in exported
    # Narrowed as the list is: by kind, by time.
    for query in (
        {"kind": ["key.revoked", "client.revoked"]},
        {"since": "2999-01-01T00:00:00Z"},
        {"until": "2000-01-01T00:00:00+00:00"},
    ):
        narrowed = client.get(
            "/api/audit/administrative/export/fhir", params=query, headers=two["headers"][alpha]
        )
        ids = [json.loads(line)["id"] for line in narrowed.text.splitlines()]
        assert ids == [item["hash"] for item in listed(client, two["headers"][alpha], **query)]


def test_the_export_reads_every_page(two: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    store, alpha = two["store"], ORGS[0]
    every = store.administrative_events(alpha, limit=500).rows
    paged = AuditStore.administrative_events

    def by_two(self: AuditStore, org: str | None, **filters: Any) -> Any:
        return paged(self, org, **{**filters, "limit": 2})

    monkeypatch.setattr(AuditStore, "administrative_events", by_two)
    assert len(every) > 2 and list(store.iter_administrative_events(alpha)) == every
    assert paged(store, alpha, limit=0).rows == [] and paged(store, alpha, limit=-1).after is None


def item(kind: str, payload: dict[str, Any], subject: str = "x:1") -> dict[str, Any]:
    """An administrative event as the list gives it, to render."""
    created = "2026-10-01T00:00:00.000+00:00"
    return {"subject": subject, "event": kind, "payload": payload, "created_at": created,
            "hash": "a" * 64}  # fmt: skip


def test_each_administrative_event_renders_as_what_happened() -> None:
    """Codes that claim no more than happened; what an R4 string may be; a key rotation's ids."""
    handled = audit_fhir.administrative_event(
        item("access.handled", {"id": "r1", "org": "c", "outcome": "refused", "handled_by": "u"})
    )
    assert [code["code"] for code in handled["subtype"]] == ["access.handled"]  # no role changed
    granted = audit_fhir.administrative_event(item("membership.created", {"id": "m", "org": "c"}))
    assert "110136" in {code["code"] for code in granted["subtype"]}
    for kind in ("key.created", "automation.held"):  # the "" organization, an empty list
        event = audit_fhir.administrative_event(item(kind, {"id": "k", "org": "", "roles": []}))
        assert fhir_structure.check(event) == [], kind
        assert "org" not in {d["type"] for d in event["entity"][0]["detail"]}
    payload = {"to": "k2", "from": {"k1": 3, "k0": 1}, "inputs": 3, "details": 1, "notes": 0}
    rotated = audit_fhir.administrative_event(item("phi.rotated", payload, "keys"))
    details = {d["type"]: d["valueString"] for d in rotated["entity"][0]["detail"]}
    assert (details["to"], details["from"], details["details"]) == ("k2", "k1=3,k0=1", "1")
    assert fhir_structure.check(rotated) == []


def test_the_reader_leaves_out_an_event_whose_payload_names_another_organization(
    db_target: str,
) -> None:
    with AuditStore(db_target, bind_org=True) as store:
        key = store.create_api_key(name="k", role="service", org=ORGS[0], created_by="ops")
        with store._db.session(exclusive=True) as db:
            store._append(db, f"key:{key['id']}", "key.revoked", {"id": key["id"], "org": ORGS[1]})
        events = [item["event"] for item in store.administrative_events(ORGS[0]).rows]
        assert events == ["key.created"]
        assert store.verify()["reason"] == "organization"  # and verify says why


def test_the_list_refuses_what_it_cannot_read(two: dict[str, Any]) -> None:
    client, headers = two["client"], two["headers"][ORGS[0]]

    def asked(**params: Any) -> int:
        answer = client.get("/api/audit/administrative", params=params, headers=headers)
        return answer.status_code

    assert asked(kind=["decision.created"]) == 400  # on a decision: /api/audit
    assert asked(kind=["user.recorded"]) == 400  # the deployment's
    assert asked(since="2026-10-01") == 400  # not an instant
    assert asked(until="2026-10-01T00:00:00") == 400  # no offset
    assert asked(subject="x" * 201) == 400
    assert asked(since="0001-01-01T00:00:00+01:00") == 400  # before year 1 in UTC
    assert asked(until="9999-12-31T23:59:59.9999+00:00") == 400  # rounds past year 9999
    assert asked(after=base64.urlsafe_b64encode(b"[" * 10_000).decode()) == 400
    exported = client.get(
        "/api/audit/administrative/export/fhir",
        params={"since": "0001-01-01T00:00:00+01:00"},
        headers=headers,
    )
    assert exported.status_code == 400
    assert asked(limit=0) == asked(limit=10_000) == 200  # clamped to 1 and 500


def test_only_administrators_read_it(two: dict[str, Any]) -> None:
    client, store, accounts = two["client"], two["store"], two["accounts"]
    for role in ("viewer", "reviewer", "governance"):
        headers = admin_of(store, accounts, ORGS[0], role=role)
        for path in ("/api/audit/administrative", "/api/audit/administrative/export/fhir"):
            assert client.get(path, headers=headers).status_code == 403, (role, path)
    made = store.create_api_key(name="k", role="service", org=ORGS[0], created_by="ops")
    bearer = {"authorization": f"Bearer {made['key']}"}
    assert client.get("/api/audit/administrative", headers=bearer).status_code == 403


def test_memberships_mode_leaves_the_deployment_wide_views_to_the_operator(
    two: dict[str, Any],
) -> None:
    """Owner decision 5 (todo.md): the whole chain's verification, the totals and the
    deployment's holds are the operator's in memberships mode; claims mode is unchanged."""
    client, headers = two["client"], two["headers"][ORGS[0]]
    own = two["store"].hold_automation(
        org=ORGS[0], pack_id=None, pack_digest=None, reason="clinic drill", set_by="lead"
    )
    verified = client.get("/api/audit/verify", headers=headers)
    health = client.get("/api/health", headers=headers).json()
    holds = client.get("/api/holds", headers=headers).json()
    deployments = client.get("/api/deployments", headers=headers)
    everyone = next(hold for hold in holds if hold["id"] == two["everyone"]["id"])
    mine = next(hold for hold in holds if hold["id"] == own["id"])
    assert (mine["reason"], mine["set_by"]) == ("clinic drill", "lead")  # its own, in full
    if two["accounts"] == "claims":
        assert verified.status_code == 200 and verified.json()["ok"]
        assert "calls_today" in health["jev"] and "calls_today" in health["llm"]
        assert {"inputs", "notes"} <= set(health["last_purge"])
        assert everyone["reason"] == "operator drill" and everyone["set_by"] == "ops"
        assert deployments.status_code == 200
        return
    assert verified.status_code == 404
    assert "mendari audit verify" in verified.json()["detail"]
    assert "calls_today" not in health["jev"] and "calls_today" not in health["llm"]
    assert set(health["last_purge"]) == {"at", "retention_days", "source"}
    assert set(everyone) == {"id", "org", "pack_id", "pack_digest", "set_at"}
    assert "operator drill" not in json.dumps(holds)
    assert deployments.status_code == 404


def test_open_mode_reads_every_event_on_no_decision(tmp_path: Path) -> None:
    """A local sandbox, no one signed in: the operator's view, the deployment's own included."""
    db = str(tmp_path / "open.db")
    with AuditStore(db) as store:
        store.create_api_key(name="k", role="service", org="default", created_by="ops")
        store.purge(30, source="command")
    client = TestClient(create_app(db_path=db))
    items = client.get("/api/audit/administrative").json()["items"]
    assert [item["event"] for item in items] == ["key.created", "retention.purged"]
    every = client.get("/api/audit/administrative", params={"kind": "retention.purged"})
    assert every.status_code == 200 and len(every.json()["items"]) == 1


def test_every_kind_on_no_decision_has_an_administrative_code() -> None:
    assert set(audit_fhir._ADMINISTRATIVE) == set(OFF_DECISION_KINDS)
    published = Terminology.for_packs([]).get("CodeSystem", "audit-administrative-event")
    assert published is not None
    assert {concept["code"] for concept in published["concept"]} == set(OFF_DECISION_KINDS)


def test_the_operator_exports_one_organization_or_every_event_on_no_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], inbox_pack: Pack
) -> None:
    db = str(tmp_path / "cli.db")
    with AuditStore(db, bind_org=True) as store:
        made = {org: seed(store, org) for org in ORGS}
        store.purge(30, source="command")
        decided = {}
        for org in ORGS:
            with tenancy.acting_for(org):
                decided[org] = Decision(inbox_pack, store=store)(message("Refill please.")).id

    def exported(*options: str) -> list[dict[str, Any]]:
        assert main(["audit", "export", "--db", db, *options]) == 0
        return [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    alpha = exported("--administrative", "--org", ORGS[0])
    subjects = {r["entity"][0]["what"]["identifier"]["value"] for r in alpha}
    assert set(made[ORGS[0]].values()) <= subjects and not set(made[ORGS[1]].values()) & subjects
    every = exported("--administrative")
    codes = {r["subtype"][0]["code"] for r in every}
    assert "retention.purged" in codes and len(every) > len(alpha)
    assert main(["audit", "export", "--db", db, "--administrative", "--agent", INBOX]) == 2
    capsys.readouterr()
    trail = exported("--org", ORGS[0])  # a decision trail: that organization's only
    events = [r for r in trail if r["resourceType"] == "AuditEvent"]
    assert {r["entity"][0]["what"]["identifier"]["value"] for r in events} == {decided[ORGS[0]]}
    assert main(["usage", "--db", db]) == 0
    usage = json.loads(capsys.readouterr().out)
    assert usage["jev"] == {"calls_today": 0, "daily_limit": usage["jev"]["daily_limit"]}
    assert {"inputs", "notes"} <= set(usage["last_purge"]) and "llm" in usage
