# SPDX-License-Identifier: Apache-2.0
"""Held texts in the audit trail, and their review (ADR-0027, points 4 to 6).

A decision that carries a text its agent wrote and awaits review keeps each text for the person
who reads it: as the note of a chained event (`output.held`), sealed under PHI mode `encrypt`,
and only where the decision's input is kept. The event holds the output's name and a salted
commitment, never the text. An automated or a blocked decision keeps nothing of its texts.

The review gives each held text a verdict (`output.resolved`), and releases by name only: a held
action whose arguments can hold free text, and every held action once a text is withheld.

A mark is planted in every text the agent writes and searched for in every table, under each
PHI mode. Every message and text here is synthetic. SQLite and Postgres."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import AuditStore, Decision, load_agent
from mendari.core import tenancy
from mendari.core.audit import DECISION_KINDS
from mendari.core.decision import DecisionResult
from mendari.core.phi import PhiPolicy, key_id
from mendari.core.spec import Outcome
from mendari.errors import ReviewError
from mendari.standards import audit_fhir
from tests.conftest import INPUT_HASH_KEY, PHI_KEY, ROOT, TRIAGE_DIR, message

ACTIONS_MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"
WRITTEN_MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini"
# A mark in each text the agent writes, by what becomes of its decision: held for a person (the
# reply and the chart note), blocked, and automated. And one in an action's free-text argument.
REPLY, NOTE, BLOCKED, AUTOMATED = "ZQREPLYMARK", "ZQNOTEMARK", "ZQBLOCKEDMARK", "ZQAUTOMARK"
ARGUMENT = "ZQARGUMENTMARK"
HELD_MARKS = (REPLY, NOTE)
NEVER_KEPT = (BLOCKED, AUTOMATED)
# What a caller might send where a verdict or an output's name belongs: a text, by mistake.
SENT_AS_NAME, SENT_AS_VERDICT = "ZQSENTASNAME", "ZQSENTASVERDICT"
LETTER = "Please write the letter about my refill."  # two texts, an action of each kind
VAGUE = "Something vague, I am not sure."  # ABSTAIN, with a text
RUDE = "This message is abusive."  # BLOCK, with a text
PLAIN = "Hello there."  # no text and no action
RELEASE = {"chart_note": "release", "reply_draft": "release"}
MODES: dict[str, PhiPolicy] = {
    "off": PhiPolicy(),
    "encrypt": PhiPolicy(mode="encrypt", key=PHI_KEY, hash_key=INPUT_HASH_KEY),
    "hash": PhiPolicy(mode="hash", hash_key=INPUT_HASH_KEY),
}


def writing(base: Path, *, store_input: str = "full") -> Path:
    """A copy of the actions fixture that also writes: two outputs (`chart_note`,
    `reply_draft`), and one more action, whose arguments are enumerated (`assign_queue`: no
    free text). Its canned agent writes both texts and proposes a read, that action and a
    free-text contact for LETTER, writes a reply with too little confidence for VAGUE, and
    writes one for the message its rule blocks (RUDE). Each text holds a mark."""
    folder = base / f"writes-and-acts-{store_input}"
    shutil.copytree(ACTIONS_MINI, folder)
    raw = yaml.safe_load((folder / "agent.yaml").read_text())
    raw["audit"] = {"store_input": store_input}
    raw["outputs"] = {
        name: {"description": description, "slot": "reader", "max_chars": 600}
        for name, description in (
            ("reply_draft", "A short reply to the patient. Staff read it first."),
            ("chart_note", "A note of the request for the chart."),
        )
    }
    raw["actions"]["assign_queue"] = {
        "description": "Put the message in a queue.",
        "effect": "write",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["queue"],
            "properties": {"queue": {"type": "string", "enum": ["refill", "front_desk"]}},
        },
    }
    (folder / "agent.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    canned = folder / "fixtures" / "mock_agent.json"
    fixture = json.loads(canned.read_text())
    calm = {"value": "no", "confidence": 0.97}
    fixture["cases"][:0] = [
        {
            "match": "letter",
            "response": {
                "answers": {"request": {"value": "refill", "confidence": 0.95}, "abusive": calm},
                # In another order than the chain keeps them (by name).
                "outputs": {
                    "reply_draft": f"Thank you. {REPLY} The pharmacy will reply.",
                    "chart_note": f"{NOTE} Asked for a refill.",
                },
                "actions": [
                    {"name": "look_up_medications"},
                    {"name": "assign_queue", "arguments": {"queue": "refill"}},
                    {"name": "send_reply", "arguments": {"message": f"{ARGUMENT} Received."}},
                ],
            },
        },
        {
            "match": "vague",
            "response": {
                "answers": {"request": {"value": "other", "confidence": 0.4}, "abusive": calm},
                "outputs": {"reply_draft": f"Thank you. {REPLY} The front desk will reply."},
            },
        },
    ]
    for case in fixture["cases"]:
        if case["match"] == "abusive":
            case["response"]["outputs"] = {"reply_draft": f"{BLOCKED} We read your message."}
    canned.write_text(json.dumps(fixture))
    return folder


@pytest.fixture
def writer(tmp_path: Path) -> Path:
    return writing(tmp_path)


@pytest.fixture
def store(db_target: str) -> Iterator[AuditStore]:
    with AuditStore(db_target) as opened:
        yield opened


def held(store: AuditStore, folder: Path) -> Decision:
    """The agent under an automation hold: a decision that would be automated waits for a
    person, with its recommendation, whatever else the policy does with a text."""
    return Decision.from_path(folder, store=store, hold_source=lambda agent: "a drill")


def automated(store: AuditStore, result: DecisionResult, keeps: str = "full") -> DecisionResult:
    """`result` recorded once more as an automated decision that carries a text, made by hand:
    whichever policy serves, a decision that automates keeps nothing of what it carries."""
    made = replace(
        result,
        id=str(uuid.uuid4()),
        outcome=Outcome.AUTO,
        actions=None,
        outputs={"reply_draft": f"{AUTOMATED} The pharmacy will reply."},
    )
    store.record(made, message(LETTER), store_input="full" if keeps == "full" else "hash")
    return made


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
    """Every table of the database as text, by name: what a search for a mark reads."""
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


def holding(store: AuditStore, mark: str) -> list[str]:
    """The tables that hold `mark`."""
    return sorted(name for name, text in tables(store).items() if mark in text)


def shown(store: AuditStore, decision_id: str, **options: Any) -> dict[str, Any]:
    found = store.get(decision_id, **options)
    assert found is not None
    return found


def held_actions(store: AuditStore, decision_id: str) -> list[dict[str, Any]]:
    return [a for a in shown(store, decision_id).get("actions", []) if a["disposition"] == "held"]


def resolved(answer: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(a["name"], a["disposition"], a["reason"]) for a in answer["actions"]]


# -- keeping: what is chained, and what is kept apart ---------------------------------------------


def test_each_text_of_a_decision_that_awaits_review_is_chained_by_name_after_it(
    store: AuditStore, writer: Path
) -> None:
    """One `output.held` per text, in the transaction that records the decision, by the
    output's name in sorted order, before its proposed actions: the output's name, a
    commitment to the text and the organization, and nothing else."""
    result = held(store, writer)(message(LETTER))
    assert result.outcome is Outcome.HUMAN_REVIEW and list(result.outputs) == [
        "reply_draft",
        "chart_note",
    ]
    events = of(store, result.id)
    assert [e["event"] for e in events] == [
        "decision.created",
        "output.held",
        "output.held",
        *["action.proposed"] * 3,
    ]
    assert [e["seq"] for e in events] == list(range(events[0]["seq"], events[0]["seq"] + 6))
    kept = [e["payload"] for e in events[1:3]]
    assert [payload["output"] for payload in kept] == ["chart_note", "reply_draft"]
    for payload in kept:
        assert set(payload) == {"output", "note_ref", "org"}
        assert set(payload["note_ref"]) == {"id", "sha256"}
        assert payload["org"] == tenancy.DEFAULT_ORG
    assert kept[0]["note_ref"] != kept[1]["note_ref"]
    assert store.verify()["ok"]
    notes = store.verify(notes=True)
    assert notes["ok"] and notes["notes"]["checked"] == 5  # two texts, three proposals


def test_an_abstention_keeps_its_text_for_the_person_who_decides(
    store: AuditStore, writer: Path
) -> None:
    """A decision with no recommendation waits for a person too: its text is held."""
    result = held(store, writer)(message(VAGUE))
    assert result.outcome is Outcome.ABSTAIN and list(result.outputs) == ["reply_draft"]
    assert [e["event"] for e in of(store, result.id)] == ["decision.created", "output.held"]
    (text,) = shown(store, result.id)["outputs"]
    assert (text["name"], text["state"]) == ("reply_draft", "held") and REPLY in text["text"]


def test_an_automated_or_a_blocked_decision_chains_and_keeps_nothing_of_its_texts(
    store: AuditStore, writer: Path
) -> None:
    """Nobody reads a text of a decision that waits for nobody: no event, no note, and no
    `outputs` in what the trail shows of it. The result still holds the text in memory, for an
    evaluation to grade; no answer carries it (tests/test_agent_outputs.py)."""
    decision = held(store, writer)
    blocked = decision(message(RUDE))
    assert blocked.outcome is Outcome.BLOCK and BLOCKED in blocked.outputs["reply_draft"]
    auto = automated(store, decision(message(LETTER)))
    for result in (blocked, auto):
        kinds = {e["event"] for e in of(store, result.id)}
        assert kinds <= {"decision.created", "action.proposed"}, kinds
        assert "outputs" not in shown(store, result.id)
    for mark in NEVER_KEPT:
        assert holding(store, mark) == [], mark
    assert holding(store, REPLY) == ["notes"]  # the search finds a text that is kept
    assert store.verify()["ok"] and store.verify(notes=True)["ok"]


def test_a_decision_that_carries_no_text_is_recorded_and_shown_as_it_always_was(
    store: AuditStore, writer: Path
) -> None:
    """No `output.held`, and no `outputs` key anywhere: every answer about a decision without a
    held text is unchanged, for an agent that writes and for one that declares no output."""
    quiet = held(store, writer)(message(PLAIN))
    assert quiet.outputs == {} and quiet.outcome is Outcome.HUMAN_REVIEW
    triaged = Decision(load_agent(TRIAGE_DIR), store=store)(message("Crushing chest pain"))
    for result in (quiet, triaged):
        assert [e["event"] for e in of(store, result.id)] == ["decision.created"]
        assert "outputs" not in shown(store, result.id)
    assert all("outputs" not in item for item in store.pending_reviews())
    answer = store.resolve_review(quiet.id, final_label="other", reviewer="rn-1")
    assert set(answer) == {
        "id", "resolved_at", "final_label", "human_override", "reviewer", "note", "note_ref",
    }  # fmt: skip


def test_a_text_event_names_its_organization_as_its_decision_s_events_do(
    store: AuditStore, writer: Path
) -> None:
    """Both kinds are a decision's, bound to its organization, which `verify` checks; and
    another organization reads neither the decision nor its held texts, and resolves nothing."""
    assert {"output.held", "output.resolved"} <= DECISION_KINDS
    with tenancy.acting_for("clinic-a"):
        result = held(store, writer)(message(LETTER))
    with tenancy.acting_for("clinic-b"):
        assert store.get(result.id) is None and store.get(result.id, reader="rn-7") is None
        assert store.pending_reviews(reader="rn-7") == []
        with pytest.raises(ReviewError, match="unknown decision"):
            store.resolve_review(
                result.id, final_label="refill", reviewer="rn-b", texts=dict(RELEASE)
            )
    with tenancy.acting_for("clinic-a"):
        store.resolve_review(result.id, final_label="refill", reviewer="rn-1", texts=dict(RELEASE))
    events = of(store, result.id)
    kinds = [e["event"] for e in events]
    assert kinds.count("output.held") == 2 and kinds.count("output.resolved") == 2
    assert {event["payload"]["org"] for event in events} == {"clinic-a"}
    assert store.verify()["ok"]


# -- what is never chained: a mark in every text, searched for everywhere ------------------------


@pytest.mark.parametrize("mode", list(MODES))
@pytest.mark.parametrize("keeps", ["full", "hash"])
def test_no_text_is_in_the_chain_or_a_record_and_a_held_one_is_only_a_note(
    db_target: str, tmp_path: Path, mode: str, keeps: str
) -> None:
    """Under every PHI mode, for an agent that keeps its input and one that keeps its hash: a
    held text is in the `notes` table only (in clear under `off`, sealed under `encrypt`, not
    at all where the input is not kept), a text of a blocked or an automated decision is
    nowhere, and no text is in an event, reviewed or not."""
    folder = writing(tmp_path, store_input=keeps)
    with AuditStore(db_target, phi=MODES[mode]) as store:
        decision = held(store, folder)
        made = [decision(message(body)) for body in (LETTER, VAGUE, RUDE, PLAIN)]
        assert [result.outcome.value for result in made] == [
            "HUMAN_REVIEW",
            "ABSTAIN",
            "BLOCK",
            "HUMAN_REVIEW",
        ]
        automated(store, made[0], keeps)
        store.resolve_review(
            made[0].id,
            final_label="refill",
            reviewer="rn-1",
            texts={"chart_note": "release", "reply_draft": "withhold"},
        )
        store.resolve_review(
            made[1].id, final_label="other", reviewer="rn-1", texts={"reply_draft": "release"}
        )
        found = tables(store)
        assert len(found) > 10 and "audit_events" in found and "decisions" in found
        kept = keeps == "full" and mode != "hash"
        for mark in HELD_MARKS:
            where = sorted(name for name, text in found.items() if mark in text)
            if kept and mode == "off":
                assert where == ["notes"], (mark, where)  # apart from the chain, only
            else:  # sealed (encrypt), or kept nowhere at all
                assert where == [], (mark, where)
        for mark in NEVER_KEPT:
            assert [name for name, text in found.items() if mark in text] == [], mark
        events = [e for e in chained(store) if e["event"].startswith("output.")]
        assert [e["event"] for e in events].count("output.held") == 3
        assert [e["event"] for e in events].count("output.resolved") == 3
        for event in events:
            payload = event["payload"]
            if event["event"] == "output.held":
                assert set(payload) == {"output", "note_ref", "org"}, event
                assert (payload["note_ref"] is not None) is kept, event
            else:
                assert set(payload) == {"output", "verdict", "reviewer", "org"}, event
                assert payload["verdict"] in ("released", "withheld"), event
            assert payload["output"] in decision.agent.manifest.outputs, event
        assert store.verify()["ok"]
        notes = store.verify(notes=True)
        assert notes["ok"] and (notes["notes"]["checked"] > 0) is kept
        # What the trail shows of the held texts: to a reader where they are sealed, to whoever
        # reads the decision where they are in clear, and to nobody where none is kept.
        plain, reader = shown(store, made[0].id), shown(store, made[0].id, reader="rn-7")
        assert [o["name"] for o in plain["outputs"]] == ["chart_note", "reply_draft"]
        if not kept:
            for seen in (plain, reader):
                assert [o["text_status"] for o in seen["outputs"]] == ["not_kept", "not_kept"]
                assert all("text" not in o for o in seen["outputs"])
        elif mode == "encrypt":
            assert [o["text_status"] for o in plain["outputs"]] == ["sealed", "sealed"]
            assert all("text" not in o for o in plain["outputs"])
            assert NOTE in reader["outputs"][0]["text"] and REPLY in reader["outputs"][1]["text"]
        else:
            assert NOTE in plain["outputs"][0]["text"] and REPLY in plain["outputs"][1]["text"]


# -- reading: as the input and an action's arguments are read ------------------------------------


def test_a_reader_opens_a_held_text_and_the_read_is_chained(db_target: str, writer: Path) -> None:
    """Under PHI mode `encrypt` a held text opens for whoever may read inputs, in the decision
    and in the review queue, and each such read is chained as the decision's `input.read`,
    naming the notes read. Nobody else is shown it, and their reading chains nothing."""
    with AuditStore(db_target, phi=MODES["encrypt"]) as store:
        result = held(store, writer)(message(LETTER))
        before = len(of(store, result.id))
        viewer = shown(store, result.id)
        assert len(of(store, result.id)) == before  # nothing was read
        assert viewer["outputs"] == [
            {"name": "chart_note", "state": "held", "text_status": "sealed"},
            {"name": "reply_draft", "state": "held", "text_status": "sealed"},
        ]
        reader = shown(store, result.id, reader="rn-7")
        assert reader["outputs"] == [
            {
                "name": "chart_note",
                "state": "held",
                "text_status": "open",
                "text": result.outputs["chart_note"],
            },
            {
                "name": "reply_draft",
                "state": "held",
                "text_status": "open",
                "text": result.outputs["reply_draft"],
            },
        ]
        (read,) = of(store, result.id, "input.read")
        refs = [
            e["payload"]["note_ref"]["id"]
            for e in of(store, result.id)
            if e["event"] in ("output.held", "action.proposed")
        ]
        assert read["payload"]["reader"] == "rn-7" and read["payload"]["notes"] == refs
        # The queue shows a reader the same, and chains the read once more.
        (item,) = store.pending_reviews(reader="rn-7")
        assert item["outputs"] == reader["outputs"] and "events" not in item
        assert len(of(store, result.id, "input.read")) == 2
        (hidden,) = store.pending_reviews()
        assert hidden["outputs"] == viewer["outputs"]
        assert len(of(store, result.id, "input.read")) == 2
        assert store.verify(notes=True)["ok"]


def test_without_a_phi_mode_a_held_text_is_read_as_the_input_is(
    store: AuditStore, writer: Path
) -> None:
    """Under PHI mode `off` whoever may read the decision reads its input, and so its held
    texts: no reader is needed and no read is chained, as for the input."""
    result = held(store, writer)(message(LETTER))
    note, reply = shown(store, result.id)["outputs"]
    assert (note["text_status"], note["text"]) == ("open", result.outputs["chart_note"])
    assert (reply["text_status"], reply["text"]) == ("open", result.outputs["reply_draft"])
    (item,) = store.pending_reviews()
    assert [o["text"] for o in item["outputs"]] == [note["text"], reply["text"]]
    assert of(store, result.id, "input.read") == []


# -- review: each text gets a verdict -------------------------------------------------------------


def test_a_review_gives_each_held_text_a_verdict_and_chains_it(
    store: AuditStore, writer: Path
) -> None:
    """One `output.resolved` per text, in the review's transaction, after `review.resolved` and
    before the actions' resolutions: the output's name, the verdict and the reviewer. The
    review's answer and the decision afterwards say where each text stands."""
    result = held(store, writer)(message(LETTER))
    answer = store.resolve_review(
        result.id,
        final_label="refill",
        reviewer="rn-1",
        texts={"reply_draft": "withhold", "chart_note": "release"},
    )
    assert answer["human_override"] is False
    assert answer["outputs"] == [
        {"name": "chart_note", "verdict": "released"},
        {"name": "reply_draft", "verdict": "withheld"},
    ]
    events = of(store, result.id)
    assert [e["event"] for e in events][-5:] == [
        "review.resolved",
        "output.resolved",
        "output.resolved",
        "action.resolved",
        "action.resolved",
    ]
    assert [e["payload"] for e in events[-4:-2]] == [
        {
            "output": "chart_note",
            "verdict": "released",
            "reviewer": "rn-1",
            "org": tenancy.DEFAULT_ORG,
        },
        {
            "output": "reply_draft",
            "verdict": "withheld",
            "reviewer": "rn-1",
            "org": tenancy.DEFAULT_ORG,
        },
    ]
    found = shown(store, result.id)
    assert [(o["name"], o["state"]) for o in found["outputs"]] == [
        ("chart_note", "released"),
        ("reply_draft", "withheld"),
    ]
    assert store.pending_reviews() == []
    assert store.verify()["ok"] and store.verify(notes=True)["ok"]


@pytest.mark.parametrize(
    "texts",
    [
        None,
        {},
        {"reply_draft": "release"},  # one left out
        {**RELEASE, SENT_AS_NAME: "release"},  # a name it does not hold
        {SENT_AS_NAME: "release", "reply_draft": "withhold"},
        {"chart_note": "release", "reply_draft": SENT_AS_VERDICT},  # another value
        {"chart_note": "release", "reply_draft": "released"},
        {"chart_note": "release", "reply_draft": ["release"]},
        {"chart_note": "release", "reply_draft": None},
    ],
)
def test_a_review_without_a_verdict_on_exactly_the_held_texts_is_refused_whole(
    store: AuditStore, writer: Path, texts: Any
) -> None:
    """A text left out, a name the decision does not hold or a verdict that is neither
    `release` nor `withhold` refuses the review before anything is written. The refusal names
    the outputs the decision holds, and never repeats what the caller sent: it may be a text."""
    result = held(store, writer)(message(LETTER))
    before = chained(store)
    with pytest.raises(ReviewError) as refused:
        store.resolve_review(result.id, final_label="refill", reviewer="rn-1", texts=texts)
    said = str(refused.value)
    assert "'chart_note', 'reply_draft'" in said and "release or withhold" in said
    for mark in (*HELD_MARKS, SENT_AS_NAME, SENT_AS_VERDICT):
        assert mark not in said, mark
    assert chained(store) == before
    assert [item["id"] for item in store.pending_reviews()] == [result.id]


def test_a_decision_that_holds_no_text_takes_no_verdict(store: AuditStore, writer: Path) -> None:
    """`texts` on a decision with nothing held refuses the review, and writes nothing: a
    verdict is about a text that waits, never one a caller names."""
    quiet = held(store, writer)(message(PLAIN))
    before = chained(store)
    with pytest.raises(ReviewError) as refused:
        store.resolve_review(
            quiet.id, final_label="other", reviewer="rn-1", texts={SENT_AS_NAME: "release"}
        )
    assert "holds no written output" in str(refused.value)
    assert SENT_AS_NAME not in str(refused.value) and chained(store) == before
    answer = store.resolve_review(quiet.id, final_label="other", reviewer="rn-1", texts={})
    assert "outputs" not in answer


@pytest.mark.parametrize(("mode", "keeps"), [("hash", "full"), ("off", "hash")])
def test_the_verdict_is_required_where_the_text_is_not_kept(
    db_target: str, tmp_path: Path, mode: str, keeps: str
) -> None:
    """Under PHI mode `hash`, and for an agent that keeps only its input's hash, no text is
    kept and none is shown: the reviewer read the draft where the caller shows it, and still
    says what becomes of each."""
    folder = writing(tmp_path, store_input=keeps)
    with AuditStore(db_target, phi=MODES[mode]) as store:
        result = held(store, folder)(message(LETTER))
        assert [e["payload"]["note_ref"] for e in of(store, result.id, "output.held")] == [
            None,
            None,
        ]
        (item,) = store.pending_reviews(reader="rn-7")
        assert item["outputs"] == [
            {"name": "chart_note", "state": "held", "text_status": "not_kept"},
            {"name": "reply_draft", "state": "held", "text_status": "not_kept"},
        ]
        with pytest.raises(ReviewError, match="'chart_note', 'reply_draft'"):
            store.resolve_review(result.id, final_label="refill", reviewer="rn-1")
        answer = store.resolve_review(
            result.id, final_label="refill", reviewer="rn-1", texts=dict(RELEASE)
        )
        assert [o["verdict"] for o in answer["outputs"]] == ["released", "released"]
        assert len(of(store, result.id, "output.resolved")) == 2 and store.verify()["ok"]
        for mark in HELD_MARKS:
            assert holding(store, mark) == [], mark


# -- review: free text is released by name -------------------------------------------------------


def test_a_held_action_that_can_hold_free_text_is_released_only_by_name(
    store: AuditStore, writer: Path
) -> None:
    """Confirming the label releases a held action of enumerated arguments, as before, and no
    longer one whose arguments can hold free text: that one is denied (`not_named`) unless the
    review names it in `release_actions`. Refusing by leaving out is no override."""
    decision = held(store, writer)
    result = decision(message(LETTER))
    queue, reply = held_actions(store, result.id)
    assert (queue["name"], reply["name"]) == ("assign_queue", "send_reply")
    assert "free_text" not in queue and reply["free_text"] is True
    answer = store.resolve_review(
        result.id, final_label="refill", reviewer="rn-1", texts=dict(RELEASE)
    )
    assert answer["human_override"] is False
    assert resolved(answer) == [
        ("assign_queue", "released", "confirmed"),
        ("send_reply", "denied", "not_named"),
    ]
    denied = of(store, result.id, "action.resolved")[-1]["payload"]
    assert denied == {
        "action_id": reply["id"],
        "name": "send_reply",
        "effect": "contact",
        "disposition": "denied",
        "reason": "not_named",
        "reviewer": "rn-1",
        "org": tenancy.DEFAULT_ORG,
    }
    assert store.stats()["human_override_rate"] == 0.0
    # Named, it is released: the reviewer read what it says.
    other = decision(message(LETTER))
    _, reply = held_actions(store, other.id)
    named = store.resolve_review(
        other.id,
        final_label="refill",
        reviewer="rn-1",
        texts=dict(RELEASE),
        release_actions=[reply["id"]],
    )
    assert resolved(named) == [
        ("assign_queue", "released", "confirmed"),
        ("send_reply", "released", "confirmed"),
    ]
    assert held_actions(store, other.id) == [] and store.verify()["ok"]


def test_once_a_text_is_withheld_no_held_action_is_released_unless_named(
    store: AuditStore, writer: Path
) -> None:
    """A manifest does not say which action uses which text: with one text withheld, a held
    action of enumerated arguments is denied too (`not_named`) unless the review names it."""
    decision = held(store, writer)
    withheld = {"chart_note": "release", "reply_draft": "withhold"}
    result = decision(message(LETTER))
    answer = store.resolve_review(result.id, final_label="refill", reviewer="rn-1", texts=withheld)
    assert resolved(answer) == [
        ("assign_queue", "denied", "not_named"),
        ("send_reply", "denied", "not_named"),
    ]
    other = decision(message(LETTER))
    queue, _ = held_actions(store, other.id)
    named = store.resolve_review(
        other.id,
        final_label="refill",
        reviewer="rn-1",
        texts=withheld,
        release_actions=[queue["id"]],
    )
    assert resolved(named) == [
        ("assign_queue", "released", "confirmed"),
        ("send_reply", "denied", "not_named"),
    ]


def test_an_override_denies_every_held_action_whatever_the_review_names(
    store: AuditStore, writer: Path
) -> None:
    """A label the agent did not recommend releases nothing it asked for, named or not; each
    text still gets the verdict the reviewer gave it."""
    result = held(store, writer)(message(LETTER))
    queue, reply = held_actions(store, result.id)
    answer = store.resolve_review(
        result.id,
        final_label="appointment",
        reviewer="rn-1",
        texts={"chart_note": "withhold", "reply_draft": "release"},
        release_actions=[queue["id"], reply["id"]],
    )
    assert answer["human_override"] is True
    assert resolved(answer) == [
        ("assign_queue", "denied", "overridden"),
        ("send_reply", "denied", "overridden"),
    ]
    assert answer["outputs"] == [
        {"name": "chart_note", "verdict": "withheld"},
        {"name": "reply_draft", "verdict": "released"},
    ]


def test_a_reviewer_denies_one_action_and_names_another(store: AuditStore, writer: Path) -> None:
    """`deny_actions` still refuses a held action that would be released, and says so
    (`reviewer_denied`), beside one released by name."""
    result = held(store, writer)(message(LETTER))
    queue, reply = held_actions(store, result.id)
    answer = store.resolve_review(
        result.id,
        final_label="refill",
        reviewer="rn-1",
        texts=dict(RELEASE),
        deny_actions=[queue["id"]],
        release_actions=[reply["id"]],
    )
    assert resolved(answer) == [
        ("assign_queue", "denied", "reviewer_denied"),
        ("send_reply", "released", "confirmed"),
    ]


def test_an_action_asked_for_after_the_decision_is_released_only_by_name(
    store: AuditStore, writer: Path
) -> None:
    """A caller may ask for an action after the decision was made (`source: request`), and so
    after its reviewer read the queue. Confirming the label does not release that one, whatever
    its arguments: the reviewer may never have been shown it. It is denied (`not_named`) unless
    the review names it, and what the agent proposed with its answer is released as before."""
    decision = held(store, writer)
    result = decision(message(LETTER))
    shown_first = held_actions(store, result.id)  # what a reviewer who reads the queue now sees
    assert [(a["name"], a["source"]) for a in shown_first] == [
        ("assign_queue", "answer"),
        ("send_reply", "answer"),
    ]
    asked = decision.authorize_action(result.id, "assign_queue", {"queue": "front_desk"})
    assert (asked["disposition"], asked["reason"]) == ("held", "awaiting_review")
    later = held_actions(store, result.id)[-1]
    assert (later["id"], later["source"]) == (asked["id"], "request")
    assert "free_text" not in later  # enumerated arguments: named for when it was asked
    answer = store.resolve_review(
        result.id, final_label="refill", reviewer="rn-1", texts=dict(RELEASE)
    )
    assert answer["human_override"] is False
    assert resolved(answer) == [
        ("assign_queue", "released", "confirmed"),
        ("send_reply", "denied", "not_named"),
        ("assign_queue", "denied", "not_named"),
    ]
    # Named, it is released: whoever names an action was shown it.
    other = decision(message(LETTER))
    asked = decision.authorize_action(other.id, "assign_queue", {"queue": "front_desk"})
    named = store.resolve_review(
        other.id,
        final_label="refill",
        reviewer="rn-1",
        texts=dict(RELEASE),
        release_actions=[asked["id"]],
    )
    assert resolved(named) == [
        ("assign_queue", "released", "confirmed"),
        ("send_reply", "denied", "not_named"),
        ("assign_queue", "released", "confirmed"),
    ]
    assert held_actions(store, other.id) == [] and store.verify()["ok"]


def test_naming_what_is_not_held_or_what_is_also_denied_refuses_the_review(
    store: AuditStore, writer: Path
) -> None:
    """An id in `release_actions` that is no held action of this decision, or one the same
    review denies, refuses the whole review before anything is written."""
    decision = held(store, writer)
    result, other = decision(message(LETTER)), decision(message(LETTER))
    _, reply = held_actions(store, result.id)
    (theirs, _) = held_actions(store, other.id)
    read = (result.actions or ())[0]  # this decision's, and not held: released with it
    before = chained(store)
    for wrong in ("no-such-action", theirs["id"], read.id):
        with pytest.raises(
            ReviewError, match="release_actions names an action this decision does not hold"
        ):
            store.resolve_review(
                result.id,
                final_label="refill",
                reviewer="rn-1",
                texts=dict(RELEASE),
                release_actions=[wrong],
            )
    with pytest.raises(ReviewError, match="both"):
        store.resolve_review(
            result.id,
            final_label="refill",
            reviewer="rn-1",
            texts=dict(RELEASE),
            release_actions=[reply["id"]],
            deny_actions=[reply["id"]],
        )
    assert chained(store) == before
    assert [item["id"] for item in store.pending_reviews()] == [result.id, other.id]


def test_a_text_only_agent_is_reviewed_with_its_verdict(store: AuditStore) -> None:
    """The written fixture, which declares no action: its held reply is kept and shown, the
    review needs its verdict, and its answer has `outputs` and no `actions`."""
    result = held(store, WRITTEN_MINI)(message("Could you refill my lisinopril?"))
    assert result.outcome is Outcome.HUMAN_REVIEW
    (text,) = shown(store, result.id)["outputs"]
    assert text == {
        "name": "reply_draft",
        "state": "held",
        "text_status": "open",
        "text": result.outputs["reply_draft"],
    }
    with pytest.raises(ReviewError, match="'reply_draft'"):
        store.resolve_review(result.id, final_label="refill", reviewer="rn-1")
    answer = store.resolve_review(
        result.id, final_label="refill", reviewer="rn-1", texts={"reply_draft": "release"}
    )
    assert answer["outputs"] == [{"name": "reply_draft", "verdict": "released"}]
    assert "actions" not in answer
    assert shown(store, result.id)["outputs"][0]["state"] == "released"


# -- retention, erasure and verification ----------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "encrypt"])
def test_erasing_a_decisions_input_erases_its_held_texts(
    db_target: str, writer: Path, mode: str
) -> None:
    """A held text goes with the decision's input on request: its note is emptied, the erasure
    chained, and what the chain kept of it (the name, where it stands) is still there."""
    with AuditStore(db_target, phi=MODES[mode]) as store:
        result = held(store, writer)(message(LETTER))
        refs = [e["payload"]["note_ref"]["id"] for e in of(store, result.id, "output.held")]
        erased = store.erase_input(result.id, erased_by="dpo", reason="request")
        assert erased["input"] is (mode == "encrypt") and set(refs) <= set(erased["notes"])
        erasures = {e["payload"]["note_id"] for e in of(store, result.id, "note.erased")}
        assert set(refs) <= erasures and len(erasures) == 5
        found = shown(store, result.id, reader="rn-7")
        assert found["outputs"] == [
            {"name": "chart_note", "state": "held", "text_status": "erased"},
            {"name": "reply_draft", "state": "held", "text_status": "erased"},
        ]
        for mark in (*HELD_MARKS, ARGUMENT):
            assert holding(store, mark) == [], mark
        counts = store.verify(notes=True)
        assert counts["ok"] and counts["notes"]["erased"] == 5
        # The verdict is still owed: the reviewer read the draft before it was erased.
        store.resolve_review(result.id, final_label="refill", reviewer="rn-1", texts=dict(RELEASE))
        assert store.verify()["ok"]


@pytest.mark.parametrize("mode", ["off", "encrypt"])
def test_retention_removes_held_texts_with_the_inputs(
    db_target: str, writer: Path, mode: str
) -> None:
    """The purge that deletes kept inputs erases held texts with them, each erasure chained."""
    with AuditStore(db_target, phi=MODES[mode]) as store:
        result = held(store, writer)(message(LETTER))
        if mode == "off":
            assert holding(store, REPLY) == ["notes"] and holding(store, NOTE) == ["notes"]
        summary = store.purge(0, source="command")
        assert summary["notes"] == 5 and summary["inputs"] == (1 if mode == "encrypt" else 0)
        for mark in (*HELD_MARKS, ARGUMENT):
            assert holding(store, mark) == [], mark
        found = shown(store, result.id, reader="rn-7")
        assert {o["text_status"] for o in found["outputs"]} == {"erased"}
        assert all("text" not in o for o in found["outputs"])
        assert len(of(store, result.id, "note.erased")) == 5
        assert store.verify()["ok"] and store.verify(notes=True)["ok"]


def test_a_changed_or_missing_held_text_breaks_verify_notes_at_its_event(
    store: AuditStore, writer: Path
) -> None:
    """The commitment chained on `output.held` is checked as any note's: a held text changed in
    the database, or its row gone with no chained erasure, breaks `verify --notes` there. The
    chain itself still holds: the text is apart from it."""
    result = held(store, writer)(message(LETTER))
    second = of(store, result.id, "output.held")[1]
    note_id = second["payload"]["note_ref"]["id"]
    assert store.verify(notes=True)["ok"]
    with store._db.session(exclusive=True) as db:
        row = db.one("SELECT event, sealed FROM notes WHERE id = ?", (note_id,))
        assert row is not None and row["event"] == "output.held" and REPLY in row["sealed"]
        changed = row["sealed"].replace("The pharmacy will reply.", "Take two tablets now.")
        db.all("UPDATE notes SET sealed = ? WHERE id = ?", (changed, note_id))
    broken = store.verify(notes=True)
    assert (broken["ok"], broken["broken_at"]) == (False, second["seq"])
    assert store.verify()["ok"]
    with store._db.session(exclusive=True) as db:
        db.all("DELETE FROM notes WHERE id = ?", (note_id,))
    gone = store.verify(notes=True)
    assert (gone["ok"], gone["broken_at"]) == (False, second["seq"])


def test_a_key_rotation_re_seals_a_held_text_as_any_note(db_target: str, writer: Path) -> None:
    """A held text is a note, so `mendari phi rotate` re-seals it under the current key with the
    others: once the old key is dropped its reader still opens it, and its commitment still
    verifies."""
    old, new = os.urandom(32), os.urandom(32)

    def ring(current: bytes, *read: bytes) -> PhiPolicy:
        return PhiPolicy(mode="encrypt", key=current, hash_key=INPUT_HASH_KEY, read_keys=read)

    with AuditStore(db_target, phi=ring(old)) as before:
        result = held(before, writer)(message(LETTER))
    with AuditStore(db_target, phi=ring(new, old)) as rotating:
        assert not rotating.phi_check()["ready"]  # sealed under the old key: not yet
        rotated = rotating.rotate_phi(to=key_id(new))
        assert (rotated["inputs"], rotated["notes"], rotated["unopenable"]) == (1, 5, [])
        assert rotating.phi_check()["ready"] is True
    with AuditStore(db_target, phi=ring(new)) as alone:  # the read key dropped
        found = shown(alone, result.id, reader="rn-7")
        assert [o["text"] for o in found["outputs"]] == [
            result.outputs["chart_note"],
            result.outputs["reply_draft"],
        ]
        assert alone.verify()["ok"] and alone.verify(notes=True)["ok"]


# -- the FHIR export ------------------------------------------------------------------------------


def test_the_fhir_export_names_both_kinds_and_holds_no_text(
    store: AuditStore, writer: Path
) -> None:
    """Each kind is exported as an AuditEvent with the output's name and, once reviewed, the
    verdict and the reviewer: codes and ids, never a text."""
    result = held(store, writer)(message(LETTER))
    store.resolve_review(
        result.id,
        final_label="refill",
        reviewer="rn-1",
        texts={"chart_note": "release", "reply_draft": "withhold"},
    )
    exported = list(audit_fhir.resources(audit_fhir.joined(shown(store, result.id))))
    said = json.dumps(exported)
    for mark in (*HELD_MARKS, ARGUMENT):
        assert mark not in said, mark
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for event in (r for r in exported if r["resourceType"] == "AuditEvent"):
        by_kind.setdefault(event["subtype"][0]["code"], []).append(event)
    assert len(by_kind["output.held"]) == 2 and len(by_kind["output.resolved"]) == 2
    assert {e["action"] for e in by_kind["output.held"]} == {"C"}
    assert {e["action"] for e in by_kind["output.resolved"]} == {"U"}

    def details(event: dict[str, Any]) -> dict[str, str]:
        return {d["type"]: d["valueString"] for d in event["entity"][0]["detail"]}

    def who(event: dict[str, Any]) -> tuple[str, bool]:
        agent_of = event["agent"][0]
        return str(agent_of["who"].get("display")), bool(agent_of["requestor"])

    note, reply = by_kind["output.held"]
    assert (details(note)["output"], details(reply)["output"]) == ("chart_note", "reply_draft")
    assert "verdict" not in details(note) and not who(note)[1]  # the engine's own
    released, withheld = by_kind["output.resolved"]
    assert (details(released)["output"], details(released)["verdict"]) == (
        "chart_note",
        "released",
    )
    assert details(withheld)["verdict"] == "withheld" and who(withheld) == ("rn-1", True)
    # No Provenance of its own: a verdict changes no result of the decision's Task.
    assert len([r for r in exported if r["resourceType"] == "Provenance"]) == 2
