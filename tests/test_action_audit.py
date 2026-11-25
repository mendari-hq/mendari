# SPDX-License-Identifier: Apache-2.0
"""Actions in the audit trail, and their review (spec 0.4 sections 6 and 7, ADR-0023).

The chain records every proposal and its resolution: ids, the declared name, the effect, the
disposition, a reason code and a hash of the arguments. It never holds the arguments, nor a name
the manifest does not declare: both are the agent's text and are kept apart, as a decision's
input is. Recognizable text is seeded in both and searched for in every table, under each PHI
mode. A review releases or denies the held actions, and a caller may ask again. SQLite and
Postgres."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import AuditStore, Decision, load_pack
from mendari.core import actions, tenancy
from mendari.core.audit import DECISION_KINDS
from mendari.core.phi import PhiPolicy
from mendari.errors import MendariError, PackNotFoundError, ReviewError
from mendari.standards import audit_fhir
from tests.conftest import INPUT_HASH_KEY, PHI_KEY, ROOT, TRIAGE_DIR, message

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"
REFILL = "Could you refill my lisinopril?"  # AUTO: a draft and a contact released
ORDER = "Please renew my prescription."  # HUMAN_REVIEW: a read released, an order held
QUEUE = "Put it in the queue, please."  # HUMAN_REVIEW: a write the manifest tightens, held
VISIT = "I need to move my appointment."  # HUMAN_REVIEW: a write and a contact held
UNDECLARED = "Try an undeclared thing."  # an undeclared action with seeded text
MALFORMED = "Send malformed arguments."  # arguments with a seeded key
UNCLEAR = "It is unclear what I want."  # ABSTAIN
ABUSIVE = "This message is abusive."  # BLOCK
# Text seeded in what must never be chained: an argument's value, an undeclared action's name,
# an argument's key, and the arguments of declared actions.
SEEDED = (
    "SENTINEL-ARGUMENT-TEXT",
    "delete_chart_sentinel",
    "SENTINEL-EXTRA-KEY",
    "Your refill request is with the pharmacy.",
    "lisinopril 10 mg",
)
MODES: dict[str, PhiPolicy] = {
    "off": PhiPolicy(),
    "encrypt": PhiPolicy(mode="encrypt", key=PHI_KEY, hash_key=INPUT_HASH_KEY),
    "hash": PhiPolicy(mode="hash", hash_key=INPUT_HASH_KEY),
}
EVERY = (REFILL, ORDER, QUEUE, VISIT, UNDECLARED, MALFORMED, UNCLEAR, ABUSIVE)


@pytest.fixture
def store(db_target: str) -> Iterator[AuditStore]:
    with AuditStore(db_target) as opened:
        yield opened


def agent(store: AuditStore, **options: Any) -> Decision:
    return Decision.from_path(MINI, store=store, **options)


def chained(store: AuditStore) -> list[dict[str, Any]]:
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT seq, decision_id, event, payload FROM audit_events ORDER BY seq")
    return [{**row, "payload": json.loads(row["payload"])} for row in rows]


def of(store: AuditStore, decision_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    return [
        event
        for event in chained(store)
        if event["decision_id"] == decision_id and kind in (None, event["event"])
    ]


def tables(store: AuditStore) -> dict[str, str]:
    """Every table of the database as text, by name: what a search for seeded text reads."""
    with store._db.session(exclusive=False) as db:
        if store.backend == "sqlite":
            names = [r["name"] for r in db.all("SELECT name FROM sqlite_master WHERE type='table'")]
        else:
            names = [
                r["table_name"]
                for r in db.all(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                )
            ]
        return {
            name: json.dumps([dict(row) for row in db.all(f'SELECT * FROM "{name}"')], default=str)
            for name in names
        }


def held_of(store: AuditStore, decision_id: str) -> list[dict[str, Any]]:
    found = store.get(decision_id)
    assert found is not None
    return [a for a in found.get("actions", []) if a["disposition"] == "held"]


# -- what is chained ------------------------------------------------------------------------------


def test_each_proposal_is_chained_after_its_decision_in_order(store: AuditStore) -> None:
    result = agent(store)(message(VISIT))
    events = of(store, result.id)
    assert [e["event"] for e in events] == ["decision.created", *["action.proposed"] * 3]
    assert [e["seq"] for e in events] == list(range(events[0]["seq"], events[0]["seq"] + 4))
    proposed = [e["payload"] for e in events[1:]]
    assert [(p["name"], p["effect"], p["disposition"], p["reason"]) for p in proposed] == [
        ("look_up_medications", "read", "released", "with_decision"),
        ("file_note", "write", "held", "awaiting_review"),
        ("send_reply", "contact", "held", "awaiting_review"),
    ]
    for payload, action in zip(proposed, result.actions or (), strict=True):
        assert set(payload) == {
            "action_id", "name", "effect", "disposition", "reason", "arguments_hash",
            "source", "slot", "note_ref", "org",
        }  # fmt: skip
        assert payload["action_id"] == action.id
        assert payload["arguments_hash"] == action.arguments_hash
        assert (payload["source"], payload["slot"]) == ("answer", "reader")
        assert set(payload["note_ref"]) == {"id", "sha256"}
    assert store.verify()["ok"] and store.verify(notes=True)["ok"]


def test_a_decision_without_proposals_chains_only_itself(store: AuditStore) -> None:
    quiet = agent(store)(message("Hello there."))
    assert [e["event"] for e in of(store, quiet.id)] == ["decision.created"]
    triaged = Decision(load_pack(TRIAGE_DIR), store=store)(message("Crushing chest pain"))
    assert [e["event"] for e in of(store, triaged.id)] == ["decision.created"]
    found = store.get(triaged.id)
    assert found is not None and "actions" not in found  # an agent that declares none: none
    assert all("actions" not in item for item in store.pending_reviews())


def test_an_action_event_names_its_organization_as_its_decision_s_events_do(
    store: AuditStore,
) -> None:
    assert {"action.proposed", "action.resolved"} <= DECISION_KINDS
    with tenancy.acting_for("clinic-a"):
        result = agent(store)(message(ORDER))
        agent(store).authorize_action(result.id, "file_note", {"note": "asked"})
        store.resolve_review(result.id, final_label="refill", reviewer="rn-1")
    events = of(store, result.id)
    kinds = [e["event"] for e in events]
    assert kinds.count("action.proposed") == 3 and kinds.count("action.resolved") == 2
    assert {event["payload"]["org"] for event in events} == {"clinic-a"}
    assert store.verify()["ok"]


# -- what is never chained: seeded text, searched for everywhere ----------------------------------


@pytest.mark.parametrize("mode", list(MODES))
@pytest.mark.parametrize("keeps", ["full", "hash"])
def test_no_argument_and_no_undeclared_name_is_in_the_chain_or_a_record(
    db_target: str, tmp_path: Path, mode: str, keeps: str
) -> None:
    folder = MINI
    if keeps == "hash":  # an agent that keeps only its input's hash keeps no arguments either
        folder = tmp_path / "hash-only"
        shutil.copytree(MINI, folder)
        raw = yaml.safe_load((folder / "agent.yaml").read_text())
        raw["audit"] = {"store_input": "hash"}
        (folder / "agent.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    with AuditStore(db_target, phi=MODES[mode]) as store:
        decision = Decision.from_path(folder, store=store)
        made = [decision(message(body)) for body in EVERY]
        for result in made:  # the second way in, with seeded text of its own
            decision.authorize_action(result.id, "send_reply", {"message": "SENTINEL-ASKED-TEXT"})
            decision.authorize_action(result.id, "sentinel_asked_name", {"why": "SENTINEL-ASKED"})
        for item in store.pending_reviews(limit=50):
            label = item["recommendation"] or "other"
            store.resolve_review(item["id"], final_label=label, reviewer="rn-1")
        found = tables(store)
        seeded = (*SEEDED, "SENTINEL-ASKED-TEXT", "sentinel_asked_name", "SENTINEL-ASKED")
        assert len(found) > 10 and "audit_events" in found and "decisions" in found
        kept = keeps == "full" and mode != "hash"
        for sentinel in seeded:
            holding = sorted(name for name, text in found.items() if sentinel in text)
            if kept and mode == "off":
                assert holding == ["notes"], (sentinel, holding)  # apart from the chain, only
            else:  # sealed (encrypt), or kept nowhere at all
                assert holding == [], (sentinel, holding)
        events = [e for e in chained(store) if e["event"].startswith("action.")]
        assert len(events) > 20
        for event in events:
            payload = event["payload"]
            assert set(payload) <= {
                "action_id", "name", "effect", "disposition", "reason", "arguments_hash",
                "source", "slot", "note_ref", "org", "requested_by", "reviewer",
            }, event  # fmt: skip
            if event["event"] == "action.proposed":
                assert (payload["note_ref"] is not None) is kept, event
                keyed = payload["arguments_hash"].startswith("hmac-sha256:")
                assert keyed is (mode != "off"), event  # keyed wherever the deployment has a key
            assert payload["name"] in (None, *decision.pack.manifest.actions), event
        assert store.verify()["ok"]
        notes = store.verify(notes=True)
        assert notes["ok"] and (notes["notes"]["checked"] > 0) is kept
        if mode == "encrypt" and kept:  # sealed: it opens for a reader, and nobody else
            undeclared = made[EVERY.index(UNDECLARED)].id
            sealed = store.get(undeclared)
            assert sealed is not None
            assert {a["arguments_status"] for a in sealed["actions"]} == {"sealed"}
            assert all(a["arguments"] is None for a in sealed["actions"])
            opened = store.get(undeclared, reader="rn-7")
            assert opened is not None
            blocked = next(a for a in opened["actions"] if a["reason"] == "undeclared")
            assert blocked["proposed_name"] == "delete_chart_sentinel"
            assert blocked["arguments"] == {"why": "SENTINEL-ARGUMENT-TEXT"}


def test_a_reader_opens_the_arguments_and_the_read_is_chained(db_target: str) -> None:
    with AuditStore(db_target, phi=MODES["encrypt"]) as store:
        result = agent(store)(message(QUEUE))
        before = len(of(store, result.id))
        viewer = store.get(result.id)
        assert viewer is not None and len(of(store, result.id)) == before  # nothing was read
        assert [a["arguments_status"] for a in viewer["actions"]] == ["sealed", "sealed"]
        reader = store.get(result.id, reader="rn-7")
        assert reader is not None
        assert [a["arguments"] for a in reader["actions"]] == [
            {"message": "Your refill request is with the pharmacy."},
            {"medication": "lisinopril 10 mg", "days_supply": 30},
        ]
        (read,) = of(store, result.id, "input.read")
        refs = [e["payload"]["note_ref"]["id"] for e in of(store, result.id, "action.proposed")]
        assert read["payload"]["reader"] == "rn-7" and read["payload"]["notes"] == refs
        # The queue shows a reader the same, and chains the read once more.
        (item,) = store.pending_reviews(reader="rn-7")
        assert item["actions"][1]["arguments"]["medication"] == "lisinopril 10 mg"
        assert "events" not in item and len(of(store, result.id, "input.read")) == 2
        (hidden,) = store.pending_reviews()
        assert {a["arguments_status"] for a in hidden["actions"]} == {"sealed"}
        assert store.verify(notes=True)["ok"]


def test_without_a_phi_mode_the_arguments_are_read_as_inputs_are(store: AuditStore) -> None:
    result = agent(store)(message(UNDECLARED))
    found = store.get(result.id)
    assert found is not None
    draft, undeclared = found["actions"]
    assert (draft["arguments_status"], undeclared["arguments_status"]) == ("open", "open")
    assert undeclared["name"] is None and undeclared["proposed_name"] == "delete_chart_sentinel"
    assert "proposed_name" not in draft
    assert of(store, result.id, "input.read") == []  # mode off chains no read, as for inputs


def test_a_changed_or_missing_kept_note_breaks_verify_notes_at_its_event(
    store: AuditStore,
) -> None:
    result = agent(store)(message(QUEUE))
    events = of(store, result.id, "action.proposed")
    second = events[1]
    note_id = second["payload"]["note_ref"]["id"]
    assert store.verify(notes=True)["ok"]
    with store._db.session(exclusive=True) as db:
        row = db.one("SELECT sealed FROM notes WHERE id = ?", (note_id,))
        assert row is not None and "lisinopril 10 mg" in row["sealed"]
        changed = row["sealed"].replace("lisinopril 10 mg", "oxycodone 80 mg")
        db.all("UPDATE notes SET sealed = ? WHERE id = ?", (changed, note_id))
    broken = store.verify(notes=True)
    assert (broken["ok"], broken["broken_at"]) == (False, second["seq"])
    assert store.verify()["ok"]  # the chain itself holds: the note is apart from it
    with store._db.session(exclusive=True) as db:
        db.all("DELETE FROM notes WHERE id = ?", (note_id,))
    gone = store.verify(notes=True)
    assert (gone["ok"], gone["broken_at"]) == (False, second["seq"])


def test_erasing_a_decisions_input_erases_its_actions_arguments(db_target: str) -> None:
    with AuditStore(db_target, phi=MODES["encrypt"]) as store:
        result = agent(store)(message(QUEUE))
        erased = store.erase_input(result.id, erased_by="dpo", reason="request")
        refs = [e["payload"]["note_ref"]["id"] for e in of(store, result.id, "action.proposed")]
        assert erased["input"] is True and sorted(erased["notes"]) == sorted(refs)
        erasures = [e["payload"]["note_id"] for e in of(store, result.id, "note.erased")]
        assert sorted(erasures) == sorted(refs)
        found = store.get(result.id, reader="rn-7")
        assert found is not None
        assert [(a["arguments_status"], a["arguments"]) for a in found["actions"]] == [
            ("erased", None),
            ("erased", None),
        ]
        # What the chain kept of each is still there: the name, the effect, the hash.
        assert [a["name"] for a in found["actions"]] == ["draft_reply", "queue_refill"]
        assert all(a["arguments_hash"].startswith("hmac-sha256:") for a in found["actions"])
        assert "lisinopril" not in json.dumps(tables(store))
        counts = store.verify(notes=True)
        assert counts["ok"] and counts["notes"]["erased"] == 2


def test_retention_removes_the_arguments_with_the_inputs(store: AuditStore) -> None:
    result = agent(store)(message(QUEUE))
    assert "lisinopril 10 mg" in tables(store)["notes"]
    summary = store.purge(0, source="command")
    assert summary["notes"] == 2
    assert "lisinopril 10 mg" not in json.dumps(tables(store))
    found = store.get(result.id)
    assert found is not None
    assert {a["arguments_status"] for a in found["actions"]} == {"erased"}
    assert len(of(store, result.id, "note.erased")) == 2
    assert store.verify(notes=True)["ok"]


# -- review: held actions are released or denied ------------------------------------------------


def test_confirming_the_recommendation_releases_each_held_action(store: AuditStore) -> None:
    result = agent(store)(message(VISIT))
    held = held_of(store, result.id)
    assert [a["name"] for a in held] == ["file_note", "send_reply"]
    answer = store.resolve_review(result.id, final_label="appointment", reviewer="rn-1")
    assert answer["human_override"] is False
    assert answer["actions"] == [
        {"id": a["id"], "name": a["name"], "effect": a["effect"], "disposition": "released",
         "reason": "confirmed"}
        for a in held
    ]  # fmt: skip
    events = of(store, result.id)
    assert [e["event"] for e in events][-3:] == ["review.resolved", *["action.resolved"] * 2]
    for event, action in zip(events[-2:], held, strict=True):
        assert event["payload"] == {
            "action_id": action["id"],
            "name": action["name"],
            "effect": action["effect"],
            "disposition": "released",
            "reason": "confirmed",
            "reviewer": "rn-1",
            "org": tenancy.DEFAULT_ORG,
        }
    found = store.get(result.id)
    assert found is not None
    read, note, reply = found["actions"]
    assert (read["disposition"], read["resolved_at"], read["resolved_by"]) == (
        "released",
        None,
        None,
    )
    for action in (note, reply):
        assert (action["disposition"], action["reason"]) == ("released", "confirmed")
        assert action["resolved_by"] == "rn-1" and action["resolved_at"] is not None
    assert held_of(store, result.id) == [] and store.verify()["ok"]


def test_overriding_the_recommendation_denies_each_held_action(store: AuditStore) -> None:
    result = agent(store)(message(VISIT))
    answer = store.resolve_review(result.id, final_label="refill", reviewer="rn-1")
    assert answer["human_override"] is True
    assert [(a["name"], a["disposition"], a["reason"]) for a in answer["actions"]] == [
        ("file_note", "denied", "overridden"),
        ("send_reply", "denied", "overridden"),
    ]
    found = store.get(result.id)
    assert found is not None
    assert [a["disposition"] for a in found["actions"]] == ["released", "denied", "denied"]
    # Even one named in `deny_actions`: an override denies them all, and says so.
    other = agent(store)(message(VISIT))
    (first, _) = held_of(store, other.id)
    denied = store.resolve_review(
        other.id, final_label="refill", reviewer="rn-1", deny_actions=[first["id"]]
    )
    assert {a["reason"] for a in denied["actions"]} == {"overridden"}


def test_a_reviewer_denies_one_action_without_recording_a_false_override(
    store: AuditStore,
) -> None:
    result = agent(store)(message(VISIT))
    note, reply = held_of(store, result.id)
    answer = store.resolve_review(
        result.id, final_label="appointment", reviewer="rn-1", deny_actions=[reply["id"]]
    )
    assert answer["human_override"] is False  # the label was confirmed: monitoring counts that
    assert [(a["id"], a["disposition"], a["reason"]) for a in answer["actions"]] == [
        (note["id"], "released", "confirmed"),
        (reply["id"], "denied", "reviewer_denied"),
    ]
    with store._db.session(exclusive=False) as db:
        row = db.one("SELECT human_override, final_label FROM decisions WHERE id = ?", (result.id,))
    assert row is not None and (bool(row["human_override"]), row["final_label"]) == (
        False,
        "appointment",
    )
    assert store.stats()["human_override_rate"] == 0.0


def test_an_id_that_is_no_held_action_refuses_the_review_and_chains_nothing(
    store: AuditStore,
) -> None:
    result = agent(store)(message(VISIT))
    other = agent(store)(message(ORDER))
    (theirs,) = held_of(store, other.id)
    released = (result.actions or ())[0]  # this decision's, and not held: a read, released
    before = chained(store)
    for wrong in ("no-such-action", theirs["id"], released.id):
        with pytest.raises(ReviewError, match="does not hold"):
            store.resolve_review(
                result.id, final_label="appointment", reviewer="rn-1", deny_actions=[wrong]
            )
    assert chained(store) == before
    assert [item["id"] for item in store.pending_reviews()] == [result.id, other.id]
    # Still to do, and done once: a second resolve is refused as it always was.
    store.resolve_review(result.id, final_label="appointment", reviewer="rn-1")
    with pytest.raises(ReviewError, match="not awaiting review"):
        store.resolve_review(result.id, final_label="appointment", reviewer="rn-1")
    assert len(of(store, result.id, "action.resolved")) == 2


def test_a_review_with_nothing_held_answers_as_it_always_did(store: AuditStore) -> None:
    abstained = agent(store)(message(UNCLEAR))  # nothing held: side effects were blocked at once
    assert held_of(store, abstained.id) == []
    answer = store.resolve_review(abstained.id, final_label="other", reviewer="rn-1")
    assert "actions" not in answer and answer["human_override"] is None
    assert of(store, abstained.id, "action.resolved") == []
    triaged = Decision(load_pack(TRIAGE_DIR), store=store)(message("Crushing chest pain"))
    plain = store.resolve_review(triaged.id, final_label="nurse", reviewer="rn-1", note="a note")
    assert set(plain) == {
        "id", "resolved_at", "final_label", "human_override", "reviewer", "note", "note_ref",
    }  # fmt: skip


# -- the second way in: asking for a recorded decision --------------------------------------------


def ask(decision: Decision, decision_id: str, name: str, **arguments: Any) -> tuple[str, str]:
    found = decision.authorize_action(decision_id, name, arguments)
    return found["disposition"], found["reason"]


def test_asking_is_gated_by_the_decision_as_it_stands(store: AuditStore) -> None:
    decision = agent(store)
    automated = decision(message(REFILL)).id
    # An automated decision: what writes or contacts is released, an order has nobody to wait
    # for, and what reads is released as always.
    assert ask(decision, automated, "look_up_medications") == ("released", "with_decision")
    assert ask(decision, automated, "file_note", note="asked") == ("released", "automated")
    assert ask(decision, automated, "renew_prescription") == ("blocked", "no_reviewer")
    assert ask(decision, automated, "queue_refill", medication="x") == ("blocked", "no_reviewer")
    # A decision that awaits review: held, and released when its reviewer confirms it.
    pending = decision(message(VISIT)).id
    assert ask(decision, pending, "look_up_medications") == ("released", "with_decision")
    assert ask(decision, pending, "queue_refill", medication="x") == ("held", "awaiting_review")
    assert ask(decision, pending, "renew_prescription") == ("held", "awaiting_review")
    store.resolve_review(pending, final_label="appointment", reviewer="rn-1")
    assert ask(decision, pending, "renew_prescription") == ("released", "confirmed")  # as it stands
    # After the review a new action is not released: the reviewer never saw it.
    assert ask(decision, pending, "file_note", note="later") == ("blocked", "review_closed")
    assert ask(decision, pending, "look_up_medications", why="later") == (
        "released",
        "with_decision",
    )
    # An abstention and a block.
    abstained = decision(message(UNCLEAR)).id
    assert ask(decision, abstained, "send_reply", message="x") == ("blocked", "no_recommendation")
    assert ask(decision, abstained, "draft_reply", message="x") == ("released", "with_decision")
    blocked = decision(message(ABUSIVE)).id
    assert ask(decision, blocked, "look_up_medications", why="x") == ("blocked", "decision_blocked")
    assert store.verify()["ok"] and store.verify(notes=True)["ok"]


def test_asking_again_answers_how_it_stands_and_chains_nothing_new(store: AuditStore) -> None:
    decision = agent(store)
    result = decision(message(VISIT))
    proposed = (result.actions or ())[2]  # send_reply, held, proposed with the answer
    before = chained(store)
    again = decision.authorize_action(result.id, "send_reply", dict(proposed.arguments))
    assert again == {
        "id": proposed.id,
        "decision_id": result.id,
        "name": "send_reply",
        "effect": "contact",
        "disposition": "held",
        "reason": "awaiting_review",
        "arguments_hash": proposed.arguments_hash,
        "repeated": True,
    }
    assert chained(store) == before  # nothing new: it is the check before running a tool
    # Other arguments are another action: checked, gated and chained once, then repeated.
    first = decision.authorize_action(result.id, "send_reply", {"message": "Another."})
    assert (first["repeated"], first["disposition"]) == (False, "held")
    assert first["id"] != proposed.id and "arguments" not in first
    second = decision.authorize_action(result.id, "send_reply", {"message": "Another."})
    assert second == {**first, "repeated": True}
    assert len(chained(store)) == len(before) + 1
    asked = chained(store)[-1]["payload"]
    assert (asked["source"], asked["slot"], asked["name"]) == ("request", None, "send_reply")
    # The reviewer confirms: the caller that asks again learns the action was released, and a
    # caller about to run it checks that these are the arguments that were.
    store.resolve_review(result.id, final_label="appointment", reviewer="rn-1")
    for arguments in (dict(proposed.arguments), {"message": "Another."}):
        released = decision.authorize_action(result.id, "send_reply", arguments)
        assert (released["disposition"], released["reason"], released["repeated"]) == (
            "released",
            "confirmed",
            True,
        )
    changed = decision.authorize_action(result.id, "send_reply", {"message": "Not what was sent."})
    assert (changed["disposition"], changed["reason"]) == ("blocked", "review_closed")


def test_who_asked_is_chained_with_sign_in_on(store: AuditStore) -> None:
    decision = agent(store)
    result = decision(message(REFILL))
    with tenancy.acting_for(None, "key service-1"):
        decision.authorize_action(result.id, "file_note", {"note": "asked"})
    assert chained(store)[-1]["payload"]["requested_by"] == "key service-1"
    decision.authorize_action(result.id, "file_note", {"note": "asked by nobody named"})
    assert "requested_by" not in chained(store)[-1]["payload"]


def test_an_undeclared_or_malformed_action_asked_for_is_blocked_and_stops_later_releases(
    store: AuditStore,
) -> None:
    decision = agent(store)
    result = decision(message(REFILL))  # AUTO
    assert ask(decision, result.id, "file_note", note="first") == ("released", "automated")
    assert ask(decision, result.id, "send_reply", message=5) == ("blocked", "invalid_arguments")
    refused = chained(store)[-1]["payload"]
    assert (refused["name"], refused["effect"]) == ("send_reply", "contact")  # declared: named
    # An agent that asked for what it may not is not trusted to automate more on this decision.
    assert ask(decision, result.id, "file_note", note="second") == ("blocked", "earlier_refusal")
    assert ask(decision, result.id, "look_up_medications") == ("released", "with_decision")
    assert ask(decision, result.id, "file_note", note="first") == (
        "released",
        "automated",
    )  # stands
    other = decision(message(REFILL))
    unknown = decision.authorize_action(other.id, "wipe_the_chart", {"why": "SENTINEL-ASKED"})
    assert (unknown["name"], unknown["effect"], unknown["disposition"], unknown["reason"]) == (
        None,
        None,
        "blocked",
        "undeclared",
    )
    assert chained(store)[-1]["payload"]["name"] is None
    assert "wipe_the_chart" not in json.dumps(chained(store))
    # Asking for it again is the same question: answered, and chained once.
    count = len(chained(store))
    repeated = decision.authorize_action(other.id, "wipe_the_chart", {"why": "SENTINEL-ASKED"})
    assert repeated["repeated"] is True and repeated["id"] == unknown["id"]
    assert len(chained(store)) == count
    assert ask(decision, other.id, "file_note", note="x") == ("blocked", "earlier_refusal")


def test_a_hold_set_after_an_automated_decision_stops_new_releases(store: AuditStore) -> None:
    holds: list[str] = []
    decision = agent(store, hold_source=lambda pack: holds[0] if holds else None)
    result = decision(message(REFILL))
    assert ask(decision, result.id, "file_note", note="before") == ("released", "automated")
    holds.append("an operator's hold")
    assert ask(decision, result.id, "file_note", note="after") == ("blocked", "not_automated")
    assert ask(decision, result.id, "look_up_medications") == ("released", "with_decision")
    # A release is final: what was released before the hold still reads as released.
    assert ask(decision, result.id, "file_note", note="before") == ("released", "automated")


def test_a_decision_of_another_version_of_the_agent_gets_nothing(
    store: AuditStore, tmp_path: Path
) -> None:
    result = agent(store)(message(REFILL))
    newer = tmp_path / "newer"
    shutil.copytree(MINI, newer)
    raw = yaml.safe_load((newer / "agent.yaml").read_text())
    raw["version"] = "0.2.0"
    raw["actions"]["look_up_medications"]["description"] = "Read the medication list."
    (newer / "agent.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    changed = Decision.from_path(newer, store=store)
    assert changed.pack.id == agent(store).pack.id and changed.pack.digest != result.pack_digest
    found = changed.authorize_action(result.id, "look_up_medications", {})
    assert (found["disposition"], found["reason"]) == ("blocked", "agent_changed")
    assert (found["name"], found["effect"]) == (None, None)  # what it declared is not this


def test_a_decision_that_is_unknown_anothers_or_another_agents_is_one_answer(
    store: AuditStore,
) -> None:
    decision = agent(store)
    with tenancy.acting_for("clinic-a"):
        theirs = decision(message(REFILL)).id
    triaged = Decision(load_pack(TRIAGE_DIR), store=store)(message("Crushing chest pain")).id
    before = chained(store)
    with tenancy.acting_for("clinic-b"):
        for unknown in ("no-such-decision", theirs, triaged):
            with pytest.raises(PackNotFoundError) as refused:
                decision.authorize_action(unknown, "look_up_medications", {})
            assert str(refused.value) == f"no decision {unknown} of mendari-test/actions-mini"
    with tenancy.acting_for("clinic-a"):
        assert ask(decision, theirs, "look_up_medications") == ("released", "with_decision")
    assert len(chained(store)) == len(before) + 1  # only the one that was theirs to ask


def test_authorizing_needs_a_store_and_a_well_formed_question() -> None:
    alone = Decision.from_path(MINI)
    with pytest.raises(MendariError, match="an audit store recorded"):
        alone.authorize_action("d-1", "look_up_medications", {})
    with AuditStore(":memory:") as store:
        decision = agent(store)
        result = decision(message(REFILL))
        for name, arguments in (("Send Reply", {}), ("send_reply", {"a": float("nan")})):
            with pytest.raises(MendariError):
                decision.authorize_action(result.id, name, arguments)
        assert len(of(store, result.id, "action.proposed")) == 2  # only the answer's own


def test_an_agent_older_than_0_4_authorizes_nothing(store: AuditStore) -> None:
    """An agent that declares no actions, as none did before spec 0.4 (the triage fixture):
    whatever is asked of it is blocked, as undeclared."""
    triaged = Decision(load_pack(TRIAGE_DIR), store=store)
    result = triaged(message("Can you send a refill for metformin to my pharmacy?"))
    found = triaged.authorize_action(result.id, "send_reply", {})
    assert (found["disposition"], found["reason"], found["name"]) == ("blocked", "undeclared", None)


# -- the FHIR export ------------------------------------------------------------------------------


def test_the_fhir_export_names_both_kinds_and_holds_no_argument(store: AuditStore) -> None:
    decision = agent(store)
    result = decision(message(UNDECLARED))
    with tenancy.acting_for(None, "key service-1"):
        decision.authorize_action(result.id, "send_reply", {"message": "SENTINEL-ASKED-TEXT"})
    store.resolve_review(result.id, final_label="refill", reviewer="rn-1")
    item = store.get(result.id)
    assert item is not None
    exported = list(audit_fhir.resources(audit_fhir.joined(item)))
    events = [r for r in exported if r["resourceType"] == "AuditEvent"]
    said = json.dumps(exported)
    for sentinel in (*SEEDED, "SENTINEL-ASKED-TEXT"):
        assert sentinel not in said, sentinel
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_kind.setdefault(event["subtype"][0]["code"], []).append(event)
    assert len(by_kind["action.proposed"]) == 3 and len(by_kind["action.resolved"]) == 1
    assert {e["action"] for e in by_kind["action.proposed"]} == {"C"}
    assert {e["action"] for e in by_kind["action.resolved"]} == {"U"}
    assert audit_fhir._EVENTS["action.proposed"] == ("C", "Action proposed")

    def details(event: dict[str, Any]) -> dict[str, str]:
        return {d["type"]: d["valueString"] for d in event["entity"][0]["detail"]}

    def who(event: dict[str, Any]) -> tuple[str, bool]:
        agent_of = event["agent"][0]
        return str(agent_of["who"].get("display")), bool(agent_of["requestor"])

    draft, undeclared, asked = by_kind["action.proposed"]
    assert details(draft)["name"] == "draft_reply" and details(draft)["effect"] == "draft"
    assert details(draft)["source"] == "answer" and not who(draft)[1]  # the engine's own
    assert "name" not in details(undeclared) and details(undeclared)["reason"] == "undeclared"
    assert details(asked)["source"] == "request" and who(asked) == ("key service-1", True)
    (resolved,) = by_kind["action.resolved"]
    assert details(resolved)["disposition"] == "released" and who(resolved) == ("rn-1", True)
    assert details(resolved)["action_id"] == details(asked)["action_id"]


def asked_twice(store: AuditStore, ask_again: Callable[[], dict[str, Any]]) -> int:
    """How many events two identical questions chain."""
    before = len(chained(store))
    first, second = ask_again(), ask_again()
    assert first["repeated"] is False and second["repeated"] is True
    return len(chained(store)) - before


def test_two_identical_questions_chain_one_event_whatever_the_disposition(
    store: AuditStore,
) -> None:
    decision = agent(store)
    for body, name, arguments in (
        (REFILL, "file_note", {"note": "x"}),  # released
        (VISIT, "file_note", {"note": "y"}),  # held
        (UNCLEAR, "file_note", {"note": "z"}),  # blocked
        (REFILL, "send_reply", {"message": 5}),  # refused, declared
        (REFILL, "never_declared", {}),  # refused, undeclared
    ):
        result = decision(message(body))
        count = asked_twice(
            store,
            lambda: decision.authorize_action(result.id, name, arguments),  # noqa: B023
        )
        assert count == 1, (body, name)
    assert actions.PROPOSED == "action.proposed" and actions.RESOLVED == "action.resolved"
