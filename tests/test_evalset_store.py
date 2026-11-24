# SPDX-License-Identifier: Apache-2.0
"""Hidden evaluation sets, stored (LP-11e, `core/evalsets.py`, ADR-0011).

A set's header and each case's input and attributes are sealed in the deployment's database, so
its bytes hold none of them; the chain holds ids, keyed case hashes and commitments only. A case
is hand-written: anything else (another source, a template, a model's or people's answers, a
public case's input, exactly or under `norm/v1`) is refused, naming the line only. The hidden-case
index matches both forms. `audit verify --evalsets` checks each sealed row against its chained
commitment: it holds, finds an altered row, cannot open what a missing key sealed, and counts
rows unchecked without keys. A ring that holds no key the database knows writes nothing, nor
does a retired key or a PHI key. Retention, erasure and PHI rotation leave every `evalset_` row
alone. On SQLite and Postgres (`db_target`); keys are made per test, cases are synthetic."""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core import evalsets as evalsets_module
from mendari.core.audit import EVALSET_KNOWN, EVALSET_RETIRED, HASH_EPOCH, KNOWN
from mendari.core.evalset_keys import AAD, CASE_PREFIX, INDEX_PREFIX, EvalsetKeys
from mendari.core.evalsets import (
    AGGREGATES_REF_PREFIX,
    CASE_REF_PREFIX,
    FREEZE_PREFIX,
    LABEL_REF_PREFIX,
    WEIGHT_REF_PREFIX,
    EvalsetStore,
    case_ref,
)
from mendari.core.phi import DETAILS_AAD, INPUT_HASH_PREFIX, NOTE_AAD, PhiPolicy, key_id
from mendari.errors import MendariError
from mendari.evals import ledger
from mendari.server.app import create_app
from tests.conftest import (
    DEMO_AGENT_DIR,
    ROOT,
    encrypt_mode,
    evalset_keys_for_test,
    evalset_on,
    hand_written,
    message,
)
from tests.test_hosted_data import sign_in_env

INBOX = "mendari/patient-inbox-router"
CLINIC = "clinic-a"
# Planted in what is sealed: none may appear in the database's bytes, the chain or any output.
MARKS = {
    "input": "zq-input-mark",
    "author": "zq-author-mark",
    "stratum": "zq-stratum-mark",
    "language": "zq-language-mark",
    "author_id": "zq-author-id-mark",
    "site": "zq-site-mark",
    "name": "zq-set-name-mark",
}
CASE_HASH = re.compile(r"^hmac-sha256:[0-9a-f]{16}:[0-9a-f]{64}$")
TABLES = ("evalset_sets", "evalset_cases", "evalset_index", "audit_events", "settings")


def planted(count: int) -> list[dict[str, Any]]:
    """Hand-written cases holding every mark, each with its author's id and an intended label."""
    return [
        {
            **hand_written(
                f"{MARKS['input']} {number}: my knee aches after the long walk",
                author=MARKS["author"],
                stratum=MARKS["stratum"],
                language=MARKS["language"],
                site=MARKS["site"],
            ),
            "id": f"{MARKS['author_id']}-{number}",
            "labels": {"urgency": "routine"},
        }
        for number in range(count)
    ]


def dump(store: AuditStore) -> str:
    """Every row of the evaluation-set tables, the chain and the marks, as text."""
    with store._db.session(exclusive=False) as db:
        return json.dumps(
            {table: db.all(f"SELECT * FROM {table} ORDER BY 1") for table in TABLES}, default=str
        )


def chained(store: AuditStore) -> list[dict[str, Any]]:
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT seq, decision_id, event, payload FROM audit_events ORDER BY seq")
    return [{**row, "payload": json.loads(row["payload"])} for row in rows]


def tamper(store: AuditStore, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Edit the database behind the store's back, as someone with the database could."""
    with store._db.session(exclusive=True) as db:
        db.all(sql, params)


def insert_row(table: str, row: dict[str, Any]) -> str:
    return f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})"


def cases_of(store: AuditStore) -> list[dict[str, Any]]:
    """The case rows, in the order their events were chained."""
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT * FROM evalset_cases")
    order = [e["payload"]["case"] for e in chained(store) if e["event"] == "evalset.case_added"]
    return sorted(rows, key=lambda row: order.index(row["case_hash"]))


@pytest.fixture
def keys() -> EvalsetKeys:
    return evalset_keys_for_test()


@pytest.fixture
def held(db_target: str, keys: EvalsetKeys) -> Iterator[tuple[AuditStore, EvalsetStore]]:
    with AuditStore(db_target) as store:
        yield store, EvalsetStore(store, keys)


def a_set(hidden: EvalsetStore, pack: Pack, org: str = CLINIC) -> str:
    return str(hidden.create_set(org, pack, MARKS["name"], created_by="ops")["set"])


def seq_of(store: AuditStore, case: str) -> int:
    return next(
        e["seq"]
        for e in chained(store)
        if e["event"] == "evalset.case_added" and e["payload"]["case"] == case
    )


# -- what is stored and chained -------------------------------------------------------------------


def test_raw_database_bytes_hold_no_planted_mark(
    held: tuple[AuditStore, EvalsetStore], db_target: str, inbox_pack: Pack
) -> None:
    store, hidden = held
    set_id = a_set(hidden, inbox_pack)
    added = hidden.add_cases(set_id, planted(3))
    hidden.remove_case(set_id, added["cases"][0]["case"])
    text = dump(store)
    for mark in MARKS.values():
        assert mark not in text, mark
    if not db_target.startswith("postgresql"):  # the file itself, and its journal if any
        raw = b"".join(
            Path(f"{db_target}{suffix}").read_bytes()
            for suffix in ("", "-wal")
            if Path(f"{db_target}{suffix}").exists()
        )
        for mark in MARKS.values():
            assert mark.encode() not in raw, mark
    assert "routine" not in text  # the intended label, sealed with the attributes


def test_chained_payloads_hold_only_ids_case_hashes_and_commitments(
    held: tuple[AuditStore, EvalsetStore], inbox_pack: Pack
) -> None:
    store, hidden = held
    set_id = a_set(hidden, inbox_pack)
    added = hidden.add_cases(set_id, planted(2))
    hidden.remove_case(set_id, added["cases"][1]["case"])
    events = [e for e in chained(store) if e["event"].startswith("evalset")]
    allowed = {
        "evalset.created": {"set", "org"},
        "evalset.case_added": {"set", "org", "case", "case_ref"},
        "evalset.case_removed": {"set", "org", "case"},
        "evalset_index.epoch": {"key_id"},
    }
    assert [e["event"] for e in events] == [
        "evalset.created",
        "evalset_index.epoch",
        "evalset.case_added",
        "evalset.case_added",
        "evalset.case_removed",
    ]
    for event in events:
        payload = event["payload"]
        assert set(payload) == allowed[event["event"]], event
        if event["event"] == "evalset_index.epoch":
            assert event["decision_id"] == "keys" and re.fullmatch(
                r"[0-9a-f]{16}", payload["key_id"]
            )
            continue
        assert event["decision_id"] == f"evalset:{set_id}" and payload["set"] == set_id
        assert payload["org"] == CLINIC
        if "case" in payload:
            assert CASE_HASH.fullmatch(payload["case"])
        if "case_ref" in payload:
            assert re.fullmatch(r"[0-9a-f]{64}", payload["case_ref"])
    text = json.dumps(chained(store))
    for row in cases_of(store):
        assert row["id"] not in text  # a case's id is never chained in the clear
    for mark in MARKS.values():
        assert mark not in text
    assert [c["case"] for c in added["cases"]] == [
        e["payload"]["case"] for e in events if e["event"] == "evalset.case_added"
    ]


def test_the_header_holds_the_pack_s_questions_and_critical_labels(
    held: tuple[AuditStore, EvalsetStore], keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    store, hidden = held
    set_id = a_set(hidden, inbox_pack)
    with store._db.session(exclusive=False) as db:
        row = db.one("SELECT * FROM evalset_sets WHERE id = ?", (set_id,))
    assert row is not None and row["state"] == "draft" and row["org"] == CLINIC
    header, opened_by = keys.open("header", row["header"], set_id)
    assert opened_by == key_id(keys.key)
    assert header == {
        "pack": INBOX,
        "name": MARKS["name"],
        "questions": {
            "destination": ["front_desk", "medical_assistant", "nurse", "physician", "billing"],
            "urgency": ["urgent", "routine"],
        },
        "derived": {},
        "critical": [{"question": "urgency", "label": "urgent"}],
    }
    freeze = ("frozen_at", "commitment", "freeze_record", "max_runs", "max_age_days")
    for column in (*freeze, "retired_at", "aggregates"):
        assert row[column] is None  # filled at freeze (LP-11i)


def test_a_case_is_stored_with_minted_ids_and_its_author_s_id_sealed(
    held: tuple[AuditStore, EvalsetStore], keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    store, hidden = held
    set_id = a_set(hidden, inbox_pack)
    record = planted(1)[0]
    added = hidden.add_cases(set_id, [record], lines=[7])
    assert added["added"] == 1 and added["cases"][0]["line"] == 7
    (row,) = cases_of(store)
    assert str(uuid.UUID(row["id"], version=4)) == row["id"]  # minted, never the author's
    assert row["case_hash"] == keys.case_hash(set_id, row["id"]) == added["cases"][0]["case"]
    value, _ = keys.open("input", row["input"], set_id, row["id"])
    body, _ = keys.open("attributes", row["attributes"], set_id, row["id"])
    assert value == record["input"]
    assert body["attributes"] == {
        **record["attributes"],
        "author_case_id": record["id"],
        "intended": {"urgency": "routine"},
    }
    salt = base64.b64decode(body["salt"])
    assert len(salt) == 32
    event = next(e for e in chained(store) if e["event"] == "evalset.case_added")
    assert event["payload"]["case_ref"] == case_ref(
        salt, set_id, row["id"], value, body["attributes"]
    )
    with store._db.session(exclusive=False) as db:
        index = db.all("SELECT * FROM evalset_index ORDER BY variant")
    hashes = keys.index_hashes(record["input"])
    assert [(r["variant"], r["hash"]) for r in index] == sorted(hashes.items())
    assert {(r["set_id"], r["case_id"], r["org"], r["key_id"]) for r in index} == {
        (set_id, row["id"], CLINIC, key_id(keys.index_key))
    }


# -- refusals -------------------------------------------------------------------------------------


def first_input(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text().splitlines()[0])["input"])


def public_input() -> dict[str, Any]:
    """A case of the agents that stand here where a release's bundled agents are (one written
    by hand, as it happens: the red flags)."""
    return first_input(
        ROOT
        / "tests"
        / "fixtures"
        / "reference-packs"
        / "patient-inbox-router"
        / "data"
        / "red_flags.jsonl"
    )


def shouted(value: dict[str, Any]) -> dict[str, Any]:
    """The same input under `norm/v1`: its text upper-cased, its punctuation changed."""
    body = value["message"]["body"]
    return {**value, "message": {**value["message"], "body": body.upper() + " !!"}}


def without(record: dict[str, Any], name: str) -> dict[str, Any]:
    return {**record, "attributes": {k: v for k, v in record["attributes"].items() if k != name}}


REFUSED: dict[str, tuple[Any, str]] = {
    "not JSON": (
        hand_written("x") | {"input": {"message": {"body": float("nan")}}},
        "not JSON, it holds a number that is not finite",
    ),
    "de-identified": (
        hand_written("a case", source="de_identified"),
        "source must be hand_written",
    ),
    "no source": (hand_written("a case", source=None), "source must be hand_written"),
    "a template": (hand_written("a case", template="tpl-1"), "template must be null"),
    "no author": (hand_written("a case", author=None), "attributes.author is missing"),
    "no language": (hand_written("a case", language=None), "attributes.language is missing"),
    "no stratum": (hand_written("a case", stratum=None), "attributes.stratum is missing"),
    "an empty author": (hand_written("a case", author=" "), "attributes.author is missing"),
    "a system block": (
        hand_written("a case") | {"system": {"outcome": "AUTO"}},
        "a `system` block is refused",
    ),
    "a feedback block": (
        hand_written("a case") | {"feedback": [{"rating": "down"}]},
        "a `feedback` block is refused",
    ),
    "another key": (hand_written("a case") | {"note": "x"}, "a case holds only"),
    "no input": ({"attributes": hand_written("x")["attributes"]}, "needs an 'input' object"),
    "no attributes": ({"input": {"message": {"body": "x"}}}, "`attributes` must be a mapping"),
    "an id of another kind": (hand_written("a case") | {"id": [1]}, "id is a string"),
    "the store's attribute": (
        hand_written("a case", intended="routine"),
        "attributes.intended is the store's own",
    ),
    "another question": (
        hand_written("a case") | {"labels": {"mood": "calm"}},
        "labels name a question the agent does not have",
    ),
    "another label": (
        hand_written("a case") | {"labels": {"urgency": "whenever"}},
        "labels.urgency is not one of its labels",
    ),
    "a public case": (
        {**hand_written("x"), "input": public_input()},
        "its input is a public case's",
    ),
    "a public case, folded": (
        {**hand_written("x"), "input": shouted(public_input())},
        "its input is a public case's",
    ),
    "not an object": ([1, 2], "a case is a JSON object"),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_each_refusal_names_the_line_only_and_writes_nothing(
    held: tuple[AuditStore, EvalsetStore], inbox_pack: Pack, name: str
) -> None:
    store, hidden = held
    set_id = a_set(hidden, inbox_pack)
    before = dump(store)
    record, said = REFUSED[name]
    good = hand_written("A hand-written case about a sore ankle")
    with pytest.raises(MendariError) as refused:
        hidden.add_cases(set_id, [good, record], lines=[3, 9])
    text = str(refused.value)
    assert text.startswith("line 9: ") and said in text, text
    assert "(and" not in text
    for leaked in ("sore ankle", "tpl-1", "whenever", "calm", "Question for the doctor"):
        assert leaked not in text
    if name.startswith("a public case"):
        assert public_input()["message"]["body"][:20] not in text
    assert dump(store) == before  # all or none: the good case was not added either


def test_every_refused_line_is_counted(
    held: tuple[AuditStore, EvalsetStore], inbox_pack: Pack
) -> None:
    _, hidden = held
    set_id = a_set(hidden, inbox_pack)
    bad = hand_written("a case", source="de_identified")
    with pytest.raises(MendariError, match=r"^record 2: .* \(and 1 more\)$"):
        hidden.add_cases(set_id, [hand_written("A fine case"), bad, bad])


def test_a_release_s_public_cases_are_its_bundled_agent_s(
    held: tuple[AuditStore, EvalsetStore], inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As a release has it: the public cases are the bundled demo agent's files, and a case of
    it is refused. The product carries no generator of any case: what it does not bundle (here,
    the test fixtures' data) it does not know, and with nothing bundled nothing is public."""
    from mendari.core import reference

    _, hidden = held
    set_id = a_set(hidden, inbox_pack)
    demo = first_input(DEMO_AGENT_DIR / "data" / "test.jsonl")
    monkeypatch.setattr(reference, "bundled_dir", lambda: DEMO_AGENT_DIR.parent)
    monkeypatch.setattr("mendari.sandbox.example_datasets_dir", lambda: None)
    evalsets_module._public_forms.cache_clear()
    try:
        for value in (demo, shouted(demo)):
            with pytest.raises(MendariError, match="its input is a public case's"):
                hidden.add_cases(set_id, [{**hand_written("x"), "input": value}])
        added = hidden.add_cases(set_id, [{**hand_written("x"), "input": public_input()}])
        assert added["added"] == 1
        monkeypatch.setattr(reference, "bundled_dir", lambda: None)
        evalsets_module._public_forms.cache_clear()
        assert evalsets_module._public_forms() == (frozenset(), frozenset())
        assert hidden.add_cases(set_id, [{**hand_written("y"), "input": demo}])["added"] == 1
    finally:
        evalsets_module._public_forms.cache_clear()
    source = Path(evalsets_module.__file__).read_text(encoding="utf-8")
    assert "synthetic_" not in source  # the generators are the tests' (tests/support)


def test_the_bundled_data_and_the_example_datasets_are_refused(
    held: tuple[AuditStore, EvalsetStore], inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundled agents' data and the example datasets count as public, each by its own
    files: a case of either is refused, exactly or folded."""
    _, hidden = held
    set_id = a_set(hidden, inbox_pack)
    example = first_input(ROOT / "tests" / "fixtures" / "datasets" / "local-site-example.jsonl")
    evalsets_module._public_forms.cache_clear()
    try:
        for value in (public_input(), shouted(public_input()), example):
            with pytest.raises(MendariError, match="its input is a public case's"):
                hidden.add_cases(set_id, [{**hand_written("x"), "input": value}])
        # And it is the example datasets' directory that refuses the last one.
        monkeypatch.setattr("mendari.sandbox.example_datasets_dir", lambda: None)
        evalsets_module._public_forms.cache_clear()
        assert hidden.add_cases(set_id, [{**hand_written("x"), "input": example}])["added"] == 1
    finally:
        evalsets_module._public_forms.cache_clear()


def test_only_a_draft_takes_cases_and_ids_are_checked(
    held: tuple[AuditStore, EvalsetStore], inbox_pack: Pack
) -> None:
    store, hidden = held
    set_id = a_set(hidden, inbox_pack)
    added = hidden.add_cases(set_id, [hand_written("A case about a stiff neck")])
    tamper(store, "UPDATE evalset_sets SET state = 'frozen' WHERE id = ?", (set_id,))
    before = dump(store)
    for write in (
        lambda: hidden.add_cases(set_id, [hand_written("Another stiff neck")]),
        lambda: hidden.remove_case(set_id, added["cases"][0]["case"]),
    ):
        with pytest.raises(MendariError, match="is frozen: only a draft changes"):
            write()
    assert dump(store) == before
    with pytest.raises(MendariError, match="no evaluation set"):
        hidden.add_cases(str(uuid.uuid4()), [hand_written("x")])
    with pytest.raises(MendariError, match="a set is named by its id"):
        hidden.add_cases("not-a-set", [hand_written("x")])
    with pytest.raises(MendariError, match="a case is named by its case hash"):
        hidden.remove_case(set_id, "rfh-u00")
    with pytest.raises(MendariError, match="an organization id"):
        hidden.create_set("Clinic A", inbox_pack, "a set", created_by="ops")
    with pytest.raises(MendariError, match="a set's name is one line"):
        hidden.create_set(CLINIC, inbox_pack, " ", created_by="ops")
    with pytest.raises(MendariError, match="hidden evaluation sets are off"):
        EvalsetStore(store).add_cases(set_id, [hand_written("x")])


# -- the hidden-case index ------------------------------------------------------------------------


def test_the_index_matches_both_canonical_and_normalised_inputs(
    held: tuple[AuditStore, EvalsetStore], inbox_pack: Pack
) -> None:
    store, hidden = held
    first, second = a_set(hidden, inbox_pack), a_set(hidden, inbox_pack, org="clinic-b")
    record = hand_written("Chest pain since Monday, 3 days, getting worse?")
    hidden.add_cases(first, [record])
    added = hidden.add_cases(second, [record])
    ids = {row["set_id"]: row["id"] for row in cases_of(store)}
    every = [{"set": s, "case": ids[s]} for s in sorted(ids)]
    assert hidden.hidden_matches(record["input"]) == every  # every organization's
    folded = {
        "message": {"body": "chest PAIN since monday 3 days getting worse", "channel": "portal"}
    }
    assert hidden.hidden_matches(folded) == every
    assert hidden.hidden_matches({"message": {"body": "Chest pain since Monday, 4 days"}}) == []
    hidden.remove_case(second, added["cases"][0]["case"])
    assert hidden.hidden_matches(record["input"]) == [{"set": first, "case": ids[first]}]


def test_index_and_commitment_prefixes_never_collide() -> None:
    """No hash or binding the deployment makes starts as another does: the index key's two
    (case hashes, index hashes), the unkeyed commitments (a case's, a label's, a frozen set's, a
    drawn case's weight's, a set's stored aggregates', a note's) and the associated data of
    sealed values."""
    prefixes = [
        AAD,
        CASE_PREFIX,
        INDEX_PREFIX + b"canonical\x00",
        INDEX_PREFIX + b"text\x00",
        CASE_REF_PREFIX,
        LABEL_REF_PREFIX,
        FREEZE_PREFIX,  # a frozen set's commitment (LP-11i)
        WEIGHT_REF_PREFIX,  # a drawn case's weight's (LP-11p)
        AGGREGATES_REF_PREFIX,  # a set's stored aggregates' (LP-11p)
        NOTE_AAD,
        DETAILS_AAD,
        INPUT_HASH_PREFIX,
        ledger._CASE_PREFIX,
    ]
    assert len(set(prefixes)) == len(prefixes)
    for one in prefixes:
        assert one.endswith(b"\x00")
        for other in prefixes:
            assert one == other or not other.startswith(one), (one, other)


# -- keys -----------------------------------------------------------------------------------------


def test_a_writer_whose_ring_holds_no_known_key_refuses(
    db_target: str, keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    stray = evalset_keys_for_test()
    with AuditStore(db_target) as store:
        before_any = EvalsetStore(store, stray)
        before_any.create_set(CLINIC, inbox_pack, "a first try", created_by="ops")
    with AuditStore(db_target) as store:  # once a set exists, a stray ring writes nothing
        state = dump(store)
        for ring in (keys, evalset_keys_for_test()):
            with pytest.raises(MendariError, match="none of this ring's evaluation-set keys"):
                EvalsetStore(store, ring).create_set(CLINIC, inbox_pack, "x", created_by="ops")
        assert dump(store) == state
        # The documented order passes: the next key as a read key first, then swapped in.
        reading = EvalsetKeys(stray.key, stray.index_key, (keys.key,))
        set_id = a_set(EvalsetStore(store, reading), inbox_pack)
        swapped = EvalsetKeys(keys.key, keys.index_key, (stray.key,))
        EvalsetStore(store, swapped).add_cases(set_id, [hand_written("A case after the swap")])
        alone = EvalsetStore(store, keys)  # the read key dropped: its own sets only (until 11f)
        alone.add_cases(a_set(alone, inbox_pack), [hand_written("A case with the new key only")])
        with pytest.raises(MendariError, match=r"sealed under key .* which this deployment lacks"):
            alone.add_cases(set_id, [hand_written("A case for the earlier set")])
        with store._db.session(exclusive=False) as db:
            marked = db.all("SELECT name FROM settings WHERE name LIKE ?", (EVALSET_KNOWN + "%",))
        assert {row["name"] for row in marked} == {
            EVALSET_KNOWN + key_id(k) for k in (stray.key, keys.key)
        }


def test_a_retired_key_or_a_phi_key_seals_nothing(
    db_target: str, keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    phi_key, hash_key, fresh, other = (evalset_keys_for_test().key for _ in range(4))
    with AuditStore(db_target, phi=PhiPolicy("encrypt", phi_key, hash_key)) as store:
        Decision(inbox_pack, store=store)(message("Refill please."))  # the hash key's epoch
        set_id = a_set(EvalsetStore(store, keys), inbox_pack)
        EvalsetStore(store, keys).add_cases(set_id, [hand_written("A case, a sprain")])
        before = dump(store)
        phi = "a PHI key or input hash key"
        for ring, variable, known_as in (
            # a key this database knows as a PHI key, in any place
            (EvalsetKeys(phi_key, keys.index_key, (keys.key,)), "MENDARI_EVALSET_KEY", phi),
            (EvalsetKeys(keys.key, keys.index_key, (phi_key,)), "MENDARI_EVALSET_READ_KEYS", phi),
            (EvalsetKeys(fresh, hash_key, (keys.key,)), "MENDARI_EVALSET_INDEX_KEY", phi),
            # a key it knows in the other role: the old index key to seal or open, the sealing
            # key to index
            (
                EvalsetKeys(keys.index_key, fresh, (keys.key,)),
                "MENDARI_EVALSET_KEY",
                "an evaluation-set index key",
            ),
            (
                EvalsetKeys(keys.key, fresh, (keys.index_key,)),
                "MENDARI_EVALSET_READ_KEYS",
                "an evaluation-set index key",
            ),
            (
                EvalsetKeys(other, keys.key, (fresh,)),
                "MENDARI_EVALSET_INDEX_KEY",
                "an evaluation-set sealing key",
            ),
        ):
            pattern = f"^{variable} holds key [0-9a-f]+, which this database knows as {known_as}"
            with pytest.raises(MendariError, match=pattern):  # every write checks so, first
                EvalsetStore(store, ring).create_set(CLINIC, inbox_pack, "x", created_by="ops")
        assert dump(store) == before  # refused before anything, its keys unmarked
        retired = EVALSET_RETIRED + key_id(keys.key)
        tamper(store, "INSERT INTO settings (name, value) VALUES (?, ?)", (retired, "x"))
        with pytest.raises(MendariError, match=f"evaluation-set key {key_id(keys.key)} was ret"):
            EvalsetStore(store, keys).add_cases(set_id, [hand_written("A case, a cramp")])
    # And the other way: a PHI key this database knows as an evaluation-set key.
    reused = AuditStore(db_target, phi=PhiPolicy("encrypt", keys.key, hash_key))
    with reused, pytest.raises(MendariError, match=f"PHI key {key_id(keys.key)} is an evaluation"):
        reused.ready_to_keep()


def test_a_refused_phi_key_marks_nothing_and_leaves_the_sets_writable(
    db_target: str, keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    """A PHI key or input hash key this database knows as an evaluation-set key is refused
    before it is marked a PHI key (`phi_key_known:`) or starts a hash epoch (`hash_epoch:`):
    one mistyped key, in any process, never locks the sets out of their own keys."""
    phi_key, hash_key = (evalset_keys_for_test().key for _ in range(2))
    with AuditStore(db_target) as store:
        hidden = EvalsetStore(store, keys)
        set_id = a_set(hidden, inbox_pack)
        hidden.add_cases(set_id, [hand_written("A case, a sprain")])  # the index key's epoch
    sealing, index = key_id(keys.key), key_id(keys.index_key)
    for policy, refused in (
        (PhiPolicy("encrypt", keys.key, hash_key), f"PHI key {sealing}"),
        (PhiPolicy("encrypt", phi_key, hash_key, (keys.key,)), f"PHI key {sealing}"),
        (PhiPolicy("encrypt", keys.index_key, hash_key), f"PHI key {index}"),
        (PhiPolicy("encrypt", phi_key, keys.index_key), f"Input hash key {index}"),
        (PhiPolicy("hash", b"", keys.key), f"Input hash key {sealing}"),
    ):
        with AuditStore(db_target, phi=policy) as store:
            with pytest.raises(MendariError, match=f"^{refused} is an evaluation-set key"):
                store.ready_to_keep()  # a server with it does not start
            with pytest.raises(MendariError, match=f"^{refused} is an evaluation-set key"):
                Decision(inbox_pack, store=store)(message("Refill please."))
    # The write itself refuses too, where nothing asked first (a record keeping no input).
    with AuditStore(db_target, phi=PhiPolicy("encrypt", phi_key, keys.index_key)) as store:
        result = Decision(inbox_pack)(message("Refill please."))
        with pytest.raises(MendariError, match=f"^Input hash key {index} is an evaluation-set"):
            store.record(result, message("Refill please."), store_input="hash")
    with AuditStore(db_target) as store:
        with store._db.session(exclusive=False) as db:
            marked = {str(row["name"]) for row in db.all("SELECT name FROM settings")}
        assert not {mark + key for key in (sealing, index) for mark in (KNOWN, HASH_EPOCH)} & marked
        assert KNOWN + key_id(phi_key) in marked  # the PHI key itself was marked, as before
        EvalsetStore(store, keys).add_cases(set_id, [hand_written("A case, a cramp")])
        assert store.verify(evalsets=keys)["ok"]


def test_a_server_with_the_keys_checks_them_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_pack: Pack
) -> None:
    sign_in_env(monkeypatch)
    encrypt_mode(monkeypatch)
    key, index = evalset_on(monkeypatch)
    db = tmp_path / "a.db"
    create_app(db_path=db)  # marks its keys known
    with AuditStore(db) as store:
        a_set(EvalsetStore(store, EvalsetKeys(key, index)), inbox_pack)
    evalset_on(monkeypatch)  # a stray pair of keys
    with pytest.raises(MendariError, match="none of this ring's evaluation-set keys"):
        create_app(db_path=db)


# -- verify ---------------------------------------------------------------------------------------


def test_verify_holds_and_counts_the_sets(
    held: tuple[AuditStore, EvalsetStore], keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    store, hidden = held
    set_id = a_set(hidden, inbox_pack)
    added = hidden.add_cases(set_id, planted(3))
    hidden.remove_case(set_id, added["cases"][1]["case"])
    events = len(chained(store))
    plain = store.verify()
    checked = store.verify(evalsets=keys)
    assert checked == {
        **plain,
        "evalsets": {
            "sets": 1,
            "checked": 2,
            "removed": 1,
            "unchecked": 0,
            "assignments": 0,
            "labels": 0,
            "frozen": 0,
            "predating": 0,
        },
    }
    unchecked = store.verify(evalsets=True)  # without keys: each counted, never a break
    assert unchecked == {
        **plain,
        "evalsets": {
            "sets": 1,
            "checked": 0,
            "removed": 1,
            "unchecked": 2,
            "assignments": 0,
            "labels": 0,
            "frozen": 0,
            "predating": 0,
        },
    }
    assert plain["ok"] and plain["events"] == events
    with pytest.raises(ValueError, match="full verification only"):
        store.verify(evalsets=keys, since_checkpoint=True)


def test_verify_finds_an_altered_row(
    held: tuple[AuditStore, EvalsetStore], keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    store, hidden = held
    set_id = a_set(hidden, inbox_pack)
    hidden.add_cases(
        set_id, [hand_written("A case about a rash"), hand_written("Another, a cough")]
    )
    first, second = cases_of(store)
    resealed = keys.seal("input", {"message": {"body": "something else"}}, set_id, first["id"])
    changes = {
        # sealed again under the held key, with its binding: its commitment no longer matches
        "a new input": ("UPDATE evalset_cases SET input = ? WHERE id = ?", (resealed, first["id"])),
        # another case's token: it does not open as this case's
        "a swapped input": (
            "UPDATE evalset_cases SET input = ? WHERE id = ?",
            (second["input"], first["id"]),
        ),
        "swapped attributes": (
            "UPDATE evalset_cases SET attributes = ? WHERE id = ?",
            (second["attributes"], first["id"]),
        ),
        "another id": (
            "UPDATE evalset_cases SET id = ? WHERE id = ?",
            (str(uuid.uuid4()), first["id"]),
        ),
        "a forged key": (
            "UPDATE evalset_cases SET input = ? WHERE id = ?",
            ("v2:0123456789abcdef:" + first["input"].split(":", 2)[2], first["id"]),
        ),
        "gone": ("DELETE FROM evalset_cases WHERE id = ?", (first["id"],)),
        "another set": (
            "UPDATE evalset_cases SET set_id = ? WHERE id = ?",
            (str(uuid.uuid4()), first["id"]),
        ),
    }
    seq = seq_of(store, first["case_hash"])
    for name, (sql, params) in changes.items():
        with store._db.session(exclusive=True) as db:
            saved = db.all("SELECT * FROM evalset_cases")
            db.all(sql, params)
        result = store.verify(evalsets=keys)
        assert not result["ok"] and result["broken_at"] == seq, (name, result)
        with store._db.session(exclusive=True) as db:  # put it back
            db.all("DELETE FROM evalset_cases")
            for row in saved:
                db.all(insert_row("evalset_cases", row), tuple(row.values()))
        assert store.verify(evalsets=keys)["ok"], name
    tamper(store, "UPDATE evalset_cases SET org = 'clinic-z' WHERE id = ?", (first["id"],))
    assert store.verify(evalsets=keys) == {
        "ok": False,
        "events": len(chained(store)),
        "broken_at": seq,
        "reason": "organization",
        "evalsets": {
            "sets": 1,
            "checked": 0,
            "removed": 0,
            "unchecked": 0,
            "assignments": 0,
            "labels": 0,
            "frozen": 0,
            "predating": 0,
        },
    }


def test_verify_opens_each_header_under_its_own_set(
    held: tuple[AuditStore, EvalsetStore], keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    """Another set's header does not open as this set's: the verification stops at the set's
    event. Until the freeze chains a commitment of the header (LP-11i), a header sealed again
    under the held key opens and passes, as the runbook says."""
    store, hidden = held
    first, second = a_set(hidden, inbox_pack), a_set(hidden, inbox_pack)
    with store._db.session(exclusive=False) as db:
        headers = {row["id"]: row["header"] for row in db.all("SELECT * FROM evalset_sets")}
    created = next(
        e["seq"]
        for e in chained(store)
        if e["event"] == "evalset.created" and e["payload"]["set"] == first
    )
    tamper(store, "UPDATE evalset_sets SET header = ? WHERE id = ?", (headers[second], first))
    result = store.verify(evalsets=keys)
    assert not result["ok"] and result["broken_at"] == created
    resealed = keys.seal("header", {"pack": "someone/else"}, first)
    tamper(store, "UPDATE evalset_sets SET header = ? WHERE id = ?", (resealed, first))
    assert store.verify(evalsets=keys)["ok"]  # nothing chained to hold it to, yet


def test_verify_finds_a_row_without_its_event(
    held: tuple[AuditStore, EvalsetStore], keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    store, hidden = held
    set_id = a_set(hidden, inbox_pack)
    added = hidden.add_cases(set_id, [hand_written("A case about a cut finger")])
    (row,) = cases_of(store)
    hidden.remove_case(set_id, added["cases"][0]["case"])
    tamper(store, insert_row("evalset_cases", row), tuple(row.values()))  # kept after removal
    result = store.verify(evalsets=keys)
    assert not result["ok"] and result["unchained"] == 1 and "broken_at" not in result
    assert store.verify(evalsets=True)["unchained"] == 1  # without keys too
    assert store.verify()["ok"]  # the chain itself holds


def test_verify_counts_what_a_missing_key_sealed_as_unopenable(
    db_target: str, keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    with AuditStore(db_target) as store:
        hidden = EvalsetStore(store, keys)
        set_id = a_set(hidden, inbox_pack)
        hidden.add_cases(set_id, [hand_written("A case about a sprain")])
        nxt = evalset_keys_for_test()
        # The next key, added as a read key and swapped in, seals what comes after.
        store.evalset_ready(EvalsetKeys(keys.key, keys.index_key, (nxt.key,)))
        both = EvalsetKeys(nxt.key, keys.index_key, (keys.key,))
        EvalsetStore(store, both).add_cases(set_id, [hand_written("A case about a bruise")])
        assert store.verify(evalsets=both)["evalsets"]["checked"] == 2
        later = EvalsetKeys(nxt.key, keys.index_key)
        result = store.verify(evalsets=later)  # the previous key dropped too early
    assert result["ok"] is False and "broken_at" not in result
    assert result["unopenable"] == {key_id(keys.key): 3}  # the header, an input, its attributes
    assert result["evalsets"] == {
        "sets": 1,
        "checked": 1,
        "removed": 0,
        "unchecked": 0,
        "assignments": 0,
        "labels": 0,
        "frozen": 0,
        "predating": 0,
    }


def test_verify_marks_nothing(
    held: tuple[AuditStore, EvalsetStore], db_target: str, keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    store, hidden = held
    hidden.add_cases(a_set(hidden, inbox_pack), [hand_written("A case about an earache")])
    before = dump(store)
    stranger = evalset_keys_for_test()
    with AuditStore(db_target, read_only=True) as reading:
        for ring in (keys, stranger, True):
            reading.verify(evalsets=ring)
    assert dump(store) == before


# -- what PHI tools leave alone -------------------------------------------------------------------


def test_retention_erasure_and_phi_rotation_leave_evalset_rows_alone(
    db_target: str, keys: EvalsetKeys, inbox_pack: Pack
) -> None:
    old, new, hash_key = (evalset_keys_for_test().key for _ in range(3))
    with AuditStore(db_target, phi=PhiPolicy("encrypt", old, hash_key)) as store:
        urgent = message("Crushing chest pain and my left arm is numb")  # waits for a reviewer
        decided = [Decision(inbox_pack, store=store)(urgent).id for _ in "ab"]
        store.resolve_review(decided[0], final_label="nurse", reviewer="rn-1", note="a note")
        hidden = EvalsetStore(store, keys)
        set_id = a_set(hidden, inbox_pack)
        hidden.add_cases(set_id, planted(2))
        before = {t: v for t, v in json.loads(dump(store)).items() if t.startswith("evalset_")}
    with AuditStore(db_target, phi=PhiPolicy("encrypt", new, hash_key, (old,))) as store:
        store.rotate_phi(to=key_id(new))
        store.erase_input(decided[1], erased_by="dpo", reason="request")
        store.purge(0, source="command")
        after = {t: v for t, v in json.loads(dump(store)).items() if t.startswith("evalset_")}
        assert after == before
        assert store.verify(evalsets=keys)["ok"]


# -- the command line -----------------------------------------------------------------------------


def served(db: str, pack: Pack) -> None:
    """A server's database: it has decided once."""
    with AuditStore(db) as store:
        Decision(pack, store=store)(message("Can I get a refill of my lisinopril?"))


def cases_file(tmp_path: Path, records: list[Any]) -> Path:
    path = tmp_path / "hidden" / "cases.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n\n")
    return path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    for mark in MARKS.values():
        assert mark not in out + err, (argv, mark)
    return code, out, err


def test_audit_verify_passes_after_cases_added(
    db_target: str,
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LP-11e's acceptance: the key holder makes a set, adds hand-written cases and removes one
    from the command line, and `mendari audit verify --evalsets` passes, every case checked."""
    evalset_on(monkeypatch)
    served(db_target, inbox_pack)
    db = ("--db", db_target)
    code, out, err = run(
        capsys, "evalset", "create", INBOX, "--org", CLINIC, "--name", MARKS["name"], *db
    )
    assert code == 0 and "created in" in err
    made = json.loads(out)
    assert set(made) == {"set", "org", "state", "created_at"} and made["state"] == "draft"
    code, out, _ = run(
        capsys, "evalset", "add", made["set"], str(cases_file(tmp_path, planted(4))), *db
    )
    added = json.loads(out)
    assert code == 0 and added["added"] == 4 and [c["line"] for c in added["cases"]] == [1, 2, 3, 4]
    code, out, _ = run(
        capsys, "evalset", "remove-case", made["set"], added["cases"][2]["case"], *db
    )
    assert code == 0 and json.loads(out) == {
        "set": made["set"],
        "removed": added["cases"][2]["case"],
    }
    code, out, _ = run(capsys, "evalset", "list", *db)
    assert code == 0 and [(s["set"], s["cases"]) for s in json.loads(out)] == [(made["set"], 3)]
    code, out, _ = run(capsys, "audit", "verify", "--evalsets", *db)
    status = json.loads(out)
    assert code == 0 and status["ok"]
    assert status["evalsets"] == {
        "sets": 1,
        "checked": 3,
        "removed": 1,
        "unchecked": 0,
        "assignments": 0,
        "labels": 0,
        "frozen": 0,
        "predating": 0,
    }
    assert run(capsys, "audit", "verify", *db)[0] == 0


def test_the_commands_refuse_while_the_keys_are_unset(
    tmp_path: Path, inbox_pack: Pack, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "mendari.db")
    served(db, inbox_pack)
    before = Path(db).read_bytes()
    path = cases_file(tmp_path, planted(1))
    for argv in (
        ("evalset", "create", INBOX, "--org", CLINIC, "--name", "x"),
        ("evalset", "add", str(uuid.uuid4()), str(path)),
        ("evalset", "remove-case", str(uuid.uuid4()), "c"),
    ):
        code, _, err = run(capsys, *argv, "--db", db)
        assert code == 1 and err.startswith("error: hidden evaluation sets are off"), err
    assert Path(db).read_bytes() == before
    code, out, _ = run(capsys, "evalset", "list", "--db", db)  # read-only: no key needed
    assert code == 0 and json.loads(out) == []
    code, out, _ = run(capsys, "audit", "verify", "--evalsets", "--db", db)
    assert code == 0 and json.loads(out)["evalsets"] == {
        "sets": 0, "checked": 0, "removed": 0, "unchecked": 0, "assignments": 0, "labels": 0,
 "frozen": 0, "predating": 0,
    }  # fmt: skip
    assert Path(db).read_bytes() == before


def test_a_cases_file_in_the_package_or_a_checkout_is_refused(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evalset_on(monkeypatch)
    db = str(tmp_path / "mendari.db")
    served(db, inbox_pack)
    checkout = tmp_path / "elsewhere" / "mendari-clone"
    (checkout / "notes").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text('[project]\nname = "mendari"\n')
    inside = checkout / "notes" / "cases.jsonl"
    inside.write_text(json.dumps(planted(1)[0]) + "\n")
    import mendari
    import mendari.cli

    refused = "kept outside Mendari's package and any checkout"
    for path in (
        inside,
        ROOT
        / "tests"
        / "fixtures"
        / "datasets"
        / "local-site-example.jsonl",  # this repository's checkout
    ):
        code, _, err = run(capsys, "evalset", "add", str(uuid.uuid4()), str(path), "--db", db)
        assert code == 1 and refused in err, path
        assert str(path) not in err
    # The installed package on its own (a wheel's, under site-packages): here it sits inside
    # this checkout, so the checkout rule is set aside to show the package rule refuses it.
    monkeypatch.setattr(mendari.cli, "_is_checkout", lambda directory: False)
    path = Path(mendari.__file__).parent / "cases.jsonl"  # never written
    code, _, err = run(capsys, "evalset", "add", str(uuid.uuid4()), str(path), "--db", db)
    assert code == 1 and refused in err
    assert str(path) not in err
    code, _, err = run(capsys, "evalset", "add", str(uuid.uuid4()), str(inside), "--db", db)
    assert refused not in err  # the checkout rule is what refused it above


def test_the_command_line_refuses_what_it_cannot_do(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evalset_on(monkeypatch)
    never_used = str(tmp_path / "fresh.db")
    AuditStore(never_used).close()
    code, _, err = run(
        capsys, "evalset", "create", INBOX, "--org", CLINIC, "--name", "x", "--db", never_used
    )
    assert code == 1 and "has never been used" in err
    code, out, _ = run(capsys, "evalset", "list", "--db", never_used)  # reads: lists nothing
    assert code == 0 and json.loads(out) == []
    other = tmp_path / "other.db"
    other.write_bytes(b"")
    code, _, err = run(capsys, "evalset", "list", "--db", str(other))
    assert code == 1 and "not a Mendari database" in err
    with pytest.raises(SystemExit):
        main(["audit", "verify", "--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "with $MENDARI_EVALSET_KEY and $MENDARI_EVALSET_INDEX_KEY; with neither" in shown
    db = str(tmp_path / "mendari.db")
    served(db, inbox_pack)
    code, _, err = run(capsys, "audit", "verify", "--evalsets", "--since-checkpoint", "--db", db)
    assert code == 1 and "--evalsets needs a full verification" in err
    with AuditStore(db) as store:
        store.create_organization(org="clinic-b", name="Clinic B", created_by="ops")
    code, _, err = run(
        capsys, "evalset", "create", INBOX, "--org", CLINIC, "--name", "x", "--db", db
    )
    assert code == 1 and f"no organization {CLINIC}" in err
    code, out, _ = run(
        capsys, "evalset", "create", INBOX, "--org", "clinic-b", "--name", "x", "--db", db
    )
    set_id = json.loads(out)["set"]
    bad = cases_file(tmp_path, [planted(1)[0], {**planted(1)[0], "system": {"outcome": "AUTO"}}])
    code, _, err = run(capsys, "evalset", "add", set_id, str(bad), "--db", db)
    assert code == 1 and err.startswith("error: line 2: a `system` block is refused"), err
    garbled = tmp_path / "hidden" / "garbled.jsonl"
    garbled.write_text(json.dumps(planted(1)[0]) + "\n{" + MARKS["input"] + "\n")
    code, _, err = run(capsys, "evalset", "add", set_id, str(garbled), "--db", db)
    assert code == 1 and err.strip() == "error: line 2: not JSON"
    code, out, _ = run(capsys, "evalset", "list", "--org", "clinic-b", "--db", db)
    assert [s["cases"] for s in json.loads(out)] == [0]


def test_a_database_holding_only_sets_is_a_server_s(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Its only rows are hidden sets (and their events): the evaluation-set commands accept it."""
    key, index = evalset_on(monkeypatch)
    db = str(tmp_path / "sets-only.db")
    with AuditStore(db) as store:
        set_id = a_set(EvalsetStore(store, EvalsetKeys(key, index)), inbox_pack)
    path = cases_file(tmp_path, [hand_written("A case about a headache")])
    assert run(capsys, "evalset", "add", set_id, str(path), "--db", db)[0] == 0
