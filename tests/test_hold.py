# SPDX-License-Identifier: Apache-2.0
"""Automation holds (`AuditStore.hold_automation`): one switch that sends to a person every
decision it covers that would have been automated, set and lifted without a redeploy, chained in
the audit trail. A hold only ever lowers automation, and changes nothing else about a decision."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Agent
from mendari.cli import main
from mendari.core.audit import AuditStore
from mendari.core.decision import Outcome
from mendari.core.policy import apply_policy
from mendari.providers import Answer, ProviderInfo
from mendari.server import app as app_module
from mendari.server.app import create_app
from tests.conftest import message
from tests.support.platform import COMMUNICATION, PATIENT

INBOX = "mendari-test/triage"  # the agent these tests decide with
DECIDE = f"/api/agents/{INBOX}/decide"
REFILL = "Hi, I need a refill on my lisinopril. I have about three pills left. Thanks"
CHEST = "Crushing chest pain for 30 minutes, going down my left arm."
# The triage fixture's providers that automate (its policy's `automation_requires`): its screen's
# canned agent, which automates without sign-in, as a live one does.
SCREENED = {"screen": "agent"}
INFO = ProviderInfo(slot="test", variant="test", type="test")


def answered(team: str, confidence: float) -> dict[str, Answer]:
    """The triage fixture's answers: `team` at `confidence`, nothing urgent and nothing
    abusive, each surely."""

    def one(question: str, label: str, sure: float) -> Answer:
        return Answer(question, label, sure, {label: sure}, INFO)

    return {
        "destination": one("destination", team, confidence),
        "urgency": one("urgency", "no", 0.95),
        "abusive": one("abusive", "no", 0.95),
    }


def _id(hold: dict[str, Any] | None) -> str | None:
    return None if hold is None else str(hold["id"])


def test_a_hold_covers_what_it_names_and_nothing_else(db_target: str) -> None:
    with AuditStore(db_target) as store:
        agent = store.hold_automation(
            org="a", agent_id=INBOX, agent_digest=None, reason="checking", set_by="ops"
        )
        assert _id(store.hold_for(INBOX, "sha256:x", "a")) == agent["id"]
        assert store.hold_for(INBOX, "sha256:x", "b") is None  # another organization
        assert store.hold_for("mendari-test/clinical-review", "sha256:x", "a") is None
        digest = store.hold_automation(
            org="b", agent_id=INBOX, agent_digest="sha256:" + "1" * 64, reason="v", set_by="ops"
        )
        assert _id(store.hold_for(INBOX, "sha256:" + "1" * 64, "b")) == digest["id"]
        assert store.hold_for(INBOX, "sha256:" + "2" * 64, "b") is None  # another version
        everywhere = store.hold_automation(
            org=None, agent_id=None, agent_digest=None, reason="all", set_by="cli"
        )
        assert _id(store.hold_for("x/y", "sha256:z", "c")) == everywhere["id"]
        assert [h["id"] for h in store.holds("a")] == [agent["id"], everywhere["id"]]

        assert not store.lift_hold(everywhere["id"], lifted_by="a admin", org="a")  # not its own
        assert store.lift_hold(everywhere["id"], lifted_by="cli", org=None)
        assert store.lift_hold(agent["id"], lifted_by="a admin", org="a")
        assert not store.lift_hold(agent["id"], lifted_by="a admin", org="a")  # already lifted
        assert store.hold_for(INBOX, "sha256:x", "a") is None
        assert store.verify()["ok"] is True
        with store._db.session(exclusive=False) as db:
            events = [r["event"] for r in db.all("SELECT event FROM audit_events ORDER BY seq")]
        assert events.count("automation.held") == 3 and events.count("automation.lifted") == 2


def test_a_hold_needs_a_reason_and_a_digest_its_agent(tmp_path: Path) -> None:
    with AuditStore(tmp_path / "h.db") as store:
        for reason in ("", " ", "x" * 201):
            with pytest.raises(Exception, match="reason"):
                store.hold_automation(
                    org="a", agent_id=None, agent_digest=None, reason=reason, set_by="ops"
                )
        with pytest.raises(Exception, match="names its agent"):
            store.hold_automation(
                org="a", agent_id=None, agent_digest="sha256:" + "1" * 64, reason="r", set_by="o"
            )


@pytest.mark.parametrize("confidence", [0.99, 0.7, 0.3])
def test_a_hold_only_ever_lowers_automation(triage_agent: Agent, confidence: float) -> None:
    answers = answered("pharmacy", confidence)
    running = {"screen": "agent", "router": "rules"}
    free = apply_policy(triage_agent.manifest, answers, {}, None, running)
    held = apply_policy(triage_agent.manifest, answers, {}, None, running, hold="hold abc")
    expected = Outcome.HUMAN_REVIEW if free.outcome is Outcome.AUTO else free.outcome
    assert held.outcome is expected
    assert (held.recommendation, held.priority) == (free.recommendation, free.priority)
    if free.outcome is Outcome.AUTO:
        assert "automation is on hold: hold abc" in held.reasons


def test_holding_and_lifting_over_http_takes_effect_at_once(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "h.db", providers=SCREENED))
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "AUTO"
    hold = client.post("/api/holds", json={"agent_id": INBOX, "reason": "a missed emergency"})
    assert hold.status_code == 201, hold.text
    held = client.post(DECIDE, json=message(REFILL)).json()
    assert held["outcome"] == "HUMAN_REVIEW" and held["recommendation"] == "pharmacy"
    assert f"automation is on hold: hold {hold.json()['id']}" in held["reasons"]
    emergency = client.post(DECIDE, json=message(CHEST)).json()  # rules still decide priority
    assert (emergency["outcome"], emergency["priority"]) == ("HUMAN_REVIEW", "stat")
    assert client.get("/api/holds").json()[0]["reason"] == "a missed emergency"
    assert client.get("/api/health").json()["holds"] == 1
    other = client.post("/api/agents/mendari-test/clinical-review/examples")  # unrelated: unheld
    assert other.status_code in (200, 405)
    assert client.delete(f"/api/holds/{hold.json()['id']}").status_code == 204
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "AUTO"
    assert client.delete(f"/api/holds/{hold.json()['id']}").status_code == 404


def test_a_hold_on_an_unknown_agent_is_refused(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "u.db"))
    answer = client.post("/api/holds", json={"agent_id": "mendari/nope", "reason": "typo"})
    assert answer.status_code == 404


def test_the_command_line_holds_every_organization_with_the_server_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app_module, "HOLD_SECONDS", 0.0)
    db = str(tmp_path / "cli.db")
    client = TestClient(create_app(db_path=db, providers=SCREENED))
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "AUTO"
    assert main(["automation", "hold", "--all-orgs", "--reason", "incident 7", "--db", db]) == 0
    with AuditStore(db) as store:
        [hold] = store.holds(everyone=True)
    assert hold["org"] is None and hold["agent_id"] is None and hold["reason"] == "incident 7"
    assert hold["id"] in capsys.readouterr().out
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "HUMAN_REVIEW"
    # A deployment's hold is lifted where it was set, never from one organization's API.
    assert client.delete(f"/api/holds/{hold['id']}").status_code == 404
    assert main(["automation", "list", "--db", db]) == 0
    assert hold["id"] in capsys.readouterr().out
    assert main(["automation", "lift", hold["id"], "--db", db]) == 0
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "AUTO"
    assert main(["automation", "lift", hold["id"], "--db", db]) == 1


def test_fhir_and_cds_hooks_decisions_are_held_too(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "f.db", providers=SCREENED))
    client.post("/api/holds", json={"reason": "every agent"})
    refill = {**COMMUNICATION, "payload": [{"contentString": REFILL}]}  # AUTO, but for the hold
    bundle = {"communication": refill, "patient": PATIENT}
    fhir = client.post(f"/api/agents/{INBOX}/fhir/decide", json=bundle).json()
    assert fhir["decision"]["outcome"] != "AUTO"
    cards = client.post(
        "/cds-services/mendari-test.triage",
        json={
            "hook": "mendari-message-received",
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {"communicationId": "msg-1", "patientId": "pat-1"},
            "prefetch": bundle,
        },
    )
    assert cards.status_code == 200, cards.text
    assert "AUTO" not in cards.json()["cards"][0]["detail"].splitlines()[2]


def test_a_held_decision_keeps_the_other_reasons_it_went_to_a_person(
    triage_agent: Agent,
) -> None:
    answers = answered("pharmacy", 0.99)
    rules_only = {"screen": "rules", "router": "rules"}
    held = apply_policy(triage_agent.manifest, answers, {}, None, rules_only, hold="hold x")
    assert held.outcome is Outcome.HUMAN_REVIEW
    assert any(reason.startswith("automation requires screen") for reason in held.reasons)
    assert "automation is on hold: hold x" in held.reasons


def test_the_api_refuses_a_digest_it_does_not_serve(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "d.db"))
    wrong = {"agent_id": INBOX, "agent_digest": "sha256:" + "0" * 64, "reason": "typo"}
    assert client.post("/api/holds", json=wrong).status_code == 422
    alone = {"agent_digest": "sha256:" + "0" * 64, "reason": "no agent"}
    assert client.post("/api/holds", json=alone).status_code == 422
    served = client.get(f"/api/agents/{INBOX}").json()["digest"]
    right = {"agent_id": INBOX, "agent_digest": served, "reason": "this version"}
    assert client.post("/api/holds", json=right).status_code == 201


def test_the_break_glass_refuses_a_database_that_is_not_the_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sqlite3

    monkeypatch.delenv("DATABASE_URL", raising=False)
    missing = str(tmp_path / "nowhere.db")
    args = ["automation", "hold", "--all-orgs", "--reason", "incident"]
    assert main(args) == 1  # no --db, no DATABASE_URL
    assert main([*args, "--db", missing]) == 1
    assert not Path(missing).exists()  # nothing created
    other = tmp_path / "other.db"  # another application's database: left as it was
    with closing(sqlite3.connect(other)) as connection:  # closed: Python 3.13 warns otherwise
        connection.execute("CREATE TABLE things (id INTEGER)")
        connection.commit()
    assert main([*args, "--db", str(other)]) == 1
    assert "is not a Mendari database" in capsys.readouterr().err
    with closing(sqlite3.connect(other)) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert tables == {"things"}
    text = tmp_path / "notes.txt"
    text.write_text("not a database")
    assert main([*args, "--db", str(text)]) == 1
    assert "cannot read" in capsys.readouterr().err
    empty = tmp_path / "empty.db"
    AuditStore(empty).close()
    assert main([*args, "--db", str(empty)]) == 1  # Mendari's, but never used
    assert "never been used" in capsys.readouterr().err
    assert main(["automation", "list", "--db", str(empty)]) == 0  # reading it is fine

    db = str(tmp_path / "server.db")
    TestClient(create_app(db_path=db, providers=SCREENED)).post(DECIDE, json=message(REFILL))
    assert main([*args, "--agent", "mendari-test/triag", "--db", db]) == 1  # a typo
    assert main([*args, "--agent", INBOX, "--digest", "nonsense", "--db", db]) == 1
    unserved = "sha256:" + "0" * 64
    assert main([*args, "--agent", INBOX, "--digest", unserved, "--db", db]) == 1
    assert "never served" in capsys.readouterr().err
    assert main([*args, "--db", db]) == 0
    assert f"held in {db}" in capsys.readouterr().err


def test_a_database_url_is_shown_without_its_password() -> None:
    from mendari.cli import _shown

    for url, shown in [
        ("postgresql://u:s3cret@host:5432/db", "postgresql://u@host:5432/db"),
        ("postgresql://:onlypw@host/db", "postgresql://host/db"),
        (
            "postgresql://u@host/db?password=s3cret&sslmode=require",
            "postgresql://u@host/db?password=***&sslmode=require",
        ),
        (
            "postgresql://u:p%40ss@ep-x.neon.tech/db?sslmode=require",
            "postgresql://u@ep-x.neon.tech/db?sslmode=require",
        ),
        ("var/mendari.db", "var/mendari.db"),
    ]:
        assert _shown(url) == shown, url


def test_no_command_prints_its_database_argument_as_given() -> None:
    """`--db` may be a URL with its password in it: a message names it through `_shown`
    (the operator's token command once printed the whole URL, password included)."""
    import mendari.cli

    source = Path(mendari.cli.__file__).read_text(encoding="utf-8")
    assert "{args.db}" not in source


def test_a_database_used_only_by_the_command_line_can_be_held(tmp_path: Path) -> None:
    """`mendari decide --db` records decisions without a deployment row: still the server's."""
    db = str(tmp_path / "cli.db")
    assert main(["decide", INBOX, "--text", REFILL, "--db", db]) == 0
    assert main(["automation", "hold", "--all-orgs", "--reason", "r", "--db", db]) == 0


def test_mendari_decide_with_a_database_honours_its_holds(tmp_path: Path) -> None:
    db = str(tmp_path / "server.db")
    TestClient(create_app(db_path=db, providers=SCREENED)).post(DECIDE, json=message(REFILL))
    with AuditStore(db) as store:
        store.hold_automation(org=None, agent_id=None, agent_digest=None, reason="r", set_by="t")
    chosen = [f"--provider={slot}={variant}" for slot, variant in SCREENED.items()]
    assert main(["decide", INBOX, "--text", REFILL, "--db", db, *chosen]) == 0
    with AuditStore(db) as store, store._db.session(exclusive=False) as session:
        rows = session.all("SELECT outcome FROM decisions ORDER BY created_at")
    assert [r["outcome"] for r in rows] == ["AUTO", "HUMAN_REVIEW"]
