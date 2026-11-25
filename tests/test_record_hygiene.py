# SPDX-License-Identifier: Apache-2.0
"""Record hygiene and the keyed input hash (Phase 4b, stage I; ADR-0005).

What the audit trail keeps for good is the record whose digest is chained. Under PHI modes `hash`
and `encrypt` it must hold no value of the input: an answer's evidence (a lab value and its date)
is dropped, or sealed beside a kept input; a resource an input issue names is stored by type and
position, never by its logical id; and the input hash is an HMAC under its own key, which a
dictionary of guesses cannot match.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import itertools
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack, load_pack
from mendari.core.phi import PhiMode, PhiPolicy, key_id, phi_policy
from mendari.errors import MendariError
from mendari.sandbox import pilot_check
from mendari.server.app import create_app
from mendari.standards import audit_fhir
from mendari.standards.terminology import NAMING_SYSTEMS
from tests.conftest import HASH_KEY_B64, INPUT_HASH_KEY, ROOT, message

FIXTURES = ROOT / "tests" / "fixtures" / "us-core"
TRIAL = "mendari/clinical-trial-prescreener"
PHI_KEY = os.urandom(32)
# Marks planted in a synthetic case: a Patient id shaped like a medical record number, and an
# HbA1c value and date no other part of a record could hold. The date is 30 days before today,
# inside the trial pack's 90-day HbA1c window: a fixed date ages out of it, and the HbA1c
# criterion then answers "unknown" and shows no evidence (the fixed 2026-07-29 aged out in
# late October 2026).
MRN = "MRN4821937"
LAB_VALUE = "8.37"
LAB_DATE = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=30)).isoformat()
KEYED = re.compile(r"^hmac-sha256:[0-9a-f]{16}:[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def trial() -> Pack:
    return load_pack(
        ROOT / "tests" / "fixtures" / "reference-agents" / "clinical-trial-prescreener"
    )


def marked_bundle(*, conforming: bool = True) -> dict[str, Any]:
    """The trial fixture with the marks planted; without `gender`, the Patient breaks US Core."""
    text = (FIXTURES / "trial.json").read_text(encoding="utf-8").replace("tr-p1", MRN)
    bundle: dict[str, Any] = json.loads(text)
    lab = bundle["entry"][2]["resource"]
    lab["valueQuantity"]["value"] = float(LAB_VALUE)
    lab["effectiveDateTime"] = LAB_DATE
    if not conforming:
        bundle["entry"][0]["resource"].pop("gender")
    return bundle


def planted(mark: str, text: str) -> bool:
    """Whether `text` holds `mark` as a value of its own: not inside a longer number, such as a
    timestamp's `…:38.371` or a latency of `18.375`, which hold "8.37" by chance."""
    return re.search(rf"(?<![\d.]){re.escape(mark)}(?!\d)", text) is not None


def policy(mode: PhiMode) -> PhiPolicy:
    return PhiPolicy(mode=mode, key=PHI_KEY, hash_key=INPUT_HASH_KEY)


def stored_text(store: AuditStore) -> str:
    """Everything the audit store keeps, as text."""
    with store._db.session(exclusive=False) as db:
        tables = ("decisions", "audit_events", "decision_inputs", "notes")
        return json.dumps([db.all(f"SELECT * FROM {table}") for table in tables], default=str)


def evidence(record: dict[str, Any]) -> str:
    return json.dumps([answer["evidence"] for answer in record["answers"].values()])


# -- evidence and issue references ---------------------------------------------------------------


@pytest.mark.parametrize("mode", ["hash", "encrypt"])
def test_no_lab_value_date_or_patient_id_is_stored(
    db_target: str, trial: Pack, mode: PhiMode
) -> None:
    store = AuditStore(db_target, phi=policy(mode))
    decide = Decision(trial, store=store, fhir_validation="warn")
    result, _ = decide.decide_fhir(marked_bundle(conforming=False))
    answered = json.dumps(result.to_dict())  # what the caller gets back is unchanged
    assert LAB_VALUE in answered and LAB_DATE in answered and f"Patient/{MRN}" in answered
    kept = stored_text(store)
    for mark in (LAB_VALUE, LAB_DATE, MRN):
        assert not planted(mark, kept), mark
    item = store.item(result.id)
    assert item is not None
    assert [issue["resource"] for issue in item["record"]["input_issues"]] == ["Patient (entry 1)"]
    assert all(answer["evidence"] == [] for answer in item["record"]["answers"].values())
    # The data-coverage record (LP-23d) counts what the mapping found, in every mode: the same
    # for the caller and the trail, and never a value, a date or an id.
    coverage = item["record"]["data_coverage"]
    assert coverage == result.data_coverage and coverage["lists"]["observations"]["resources"] == 1
    for mark in (LAB_VALUE, LAB_DATE, MRN):
        assert not planted(mark, json.dumps(coverage)), mark
    assert store.verify()["ok"] is True
    store.close()


def test_a_reviewer_sees_the_evidence_under_encrypt(db_target: str, trial: Pack) -> None:
    store = AuditStore(db_target, phi=policy("encrypt"))
    result, _ = Decision(trial, store=store).decide_fhir(marked_bundle())
    hidden = store.get(result.id)
    assert hidden is not None and hidden["input_restricted"] is True
    assert LAB_VALUE not in evidence(hidden["record"])
    shown = store.get(result.id, reader="Nurse Kim")
    assert shown is not None and f"{LAB_VALUE} % on {LAB_DATE}" in evidence(shown["record"])
    given = {q: list(a.evidence) for q, a in result.answers.items()}
    assert {q: a["evidence"] for q, a in shown["record"]["answers"].items()} == given
    assert shown["events"][-1]["event"] == "input.read"
    assert store.purge_inputs(0) == 1  # retention deletes the evidence with the input
    purged = store.get(result.id, reader="Nurse Kim")
    assert purged is not None and LAB_VALUE not in evidence(purged["record"])
    assert store.verify()["ok"] is True
    store.close()


def test_sealed_details_open_only_for_their_own_decision(tmp_path: Path, trial: Pack) -> None:
    store = AuditStore(tmp_path / "a.db", phi=policy("encrypt"))
    decide = Decision(trial, store=store)
    first, _ = decide.decide_fhir(marked_bundle())
    second, _ = decide.decide_fhir(marked_bundle())
    with store._db.session(exclusive=True) as db:
        stolen = db.one("SELECT details FROM decision_inputs WHERE decision_id = ?", (first.id,))
        assert stolen is not None and stolen["details"]
        db.all(
            "UPDATE decision_inputs SET details = ? WHERE decision_id = ?",
            (stolen["details"], second.id),
        )
    with pytest.raises(MendariError, match="does not decrypt"):
        store.get(second.id, reader="Nurse Kim")
    with pytest.raises(MendariError, match="does not decrypt"):  # nor as an input
        PhiPolicy(mode="encrypt", key=PHI_KEY).decrypt(first.id, stolen["details"])


def test_off_mode_keeps_evidence_unless_the_pack_keeps_only_the_hash(
    tmp_path: Path, trial: Pack
) -> None:
    store = AuditStore(tmp_path / "a.db")
    result, _ = Decision(trial, store=store).decide_fhir(marked_bundle())
    kept = store.item(result.id)
    assert kept is not None and f"{LAB_VALUE} % on {LAB_DATE}" in evidence(kept["record"])
    assert kept["record"]["input"] is not None  # as before
    hashed, _ = Decision(trial).decide_fhir(marked_bundle())
    store.record(hashed, {"case": "synthetic"}, store_input="hash")
    dropped = store.item(hashed.id)
    assert dropped is not None and dropped["record"]["input"] is None
    assert LAB_VALUE not in evidence(dropped["record"])


@pytest.mark.parametrize("validation", ["warn", "enforce"])
def test_issues_name_a_resource_by_position_in_the_record_and_by_id_to_the_caller(
    tmp_path: Path, validation: str
) -> None:
    app = create_app(db_path=tmp_path / "a.db", fhir_validation=validation)  # type: ignore[arg-type]
    client = TestClient(app)
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "pack", "valueString": TRIAL},
            {"name": "data", "resource": marked_bundle(conforming=False)},
        ],
    }
    bundle = client.post("/fhir/$decide", json=parameters).json()
    found = {e["resource"]["resourceType"]: e["resource"] for e in bundle["entry"]}
    returned = found["OperationOutcome"]["issue"]
    assert any(i["diagnostics"].startswith(f"Patient/{MRN}: gender") for i in returned)
    decision_id = found["GuidanceResponse"]["identifier"][0]["value"]
    record = client.get(f"/api/audit/{decision_id}").json()["record"]
    assert MRN not in json.dumps(record)
    assert [i["resource"] for i in record["input_issues"]] == ["Patient (entry 1)"]
    if validation == "enforce":
        assert record["reasons"] == [
            "input rejected: not US Core 6.1.0: Patient (entry 1) Patient.gender: gender is "
            "required (US Core Patient Profile)"
        ]


# -- the keyed input hash ------------------------------------------------------------------------


def test_the_hash_is_keyed_named_and_stable_under_one_key() -> None:
    case = {"patient": {"age": 57, "sex": "female"}}
    keyed = PhiPolicy(hash_key=INPUT_HASH_KEY).input_hash(case)
    assert KEYED.match(keyed) and keyed.split(":")[1] == key_id(INPUT_HASH_KEY)
    assert PhiPolicy(mode="hash", hash_key=INPUT_HASH_KEY).input_hash(dict(case)) == keyed
    canonical = json.dumps(case, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(INPUT_HASH_KEY, b"mendari-input-v1\x00" + canonical, hashlib.sha256)
    assert keyed.endswith(":" + expected.hexdigest())
    other = PhiPolicy(hash_key=os.urandom(32)).input_hash(case)
    assert other != keyed and other.split(":")[1] != keyed.split(":")[1]
    unkeyed = PhiPolicy().input_hash(case)  # mode off without a key: as before
    assert unkeyed == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert PhiPolicy(hash_key=INPUT_HASH_KEY).hash_kind == f"hmac-sha256:{key_id(INPUT_HASH_KEY)}"
    assert PhiPolicy().hash_kind == "sha256"


def test_a_decision_records_the_keyed_hash_and_provenance_carries_it(
    db_target: str, trial: Pack
) -> None:
    store = AuditStore(db_target, phi=policy("hash"))
    result, _ = Decision(trial, store=store).decide_fhir(marked_bundle())
    assert KEYED.match(result.input_hash)
    item = store.item(result.id)
    assert item is not None and item["record"]["input_hash"] == result.input_hash
    created = {**item["events"][0], "decision_id": result.id, "record": item["record"]}
    created |= {"pack_id": result.pack_id, "pack_version": result.pack_version}
    provenance = audit_fhir.provenance(created)
    assert provenance["entity"][0]["what"]["identifier"]["value"] == result.input_hash
    store.close()


def test_a_dictionary_of_guesses_finds_nothing_against_a_keyed_hash() -> None:
    """The chain privacy report's attack: a structured input (an age, a sex, an order, a payer)
    is found among a few thousand guesses of its plain SHA-256, and never under the key."""

    def guesses() -> Any:
        orders = [("FX103", "MRI lumbar spine"), ("FX211", "MRI knee"), ("FX901", "MRI brain")]
        sexes = ("female", "male", "other", "unknown")
        for age, sex, (code, name), payer in itertools.product(
            range(111), sexes, orders, ("acme", "beacon", "cobalt")
        ):
            yield {
                "patient": {"age": age, "sex": sex},
                "order": {"code": code, "name": name},
                "coverage": {"payer": payer},
            }

    secret = {
        "patient": {"age": 57, "sex": "male"},
        "order": {"code": "FX103", "name": "MRI lumbar spine"},
        "coverage": {"payer": "beacon"},
    }
    plain, keyed = PhiPolicy(), PhiPolicy(hash_key=INPUT_HASH_KEY)
    unkeyed_target, keyed_target = plain.input_hash(secret), keyed.input_hash(secret)
    found_plain = [g for g in guesses() if plain.input_hash(g) == unkeyed_target]
    found_keyed = [g for g in guesses() if plain.input_hash(g) == keyed_target]
    assert found_plain == [secret]  # the search works on the unkeyed form
    assert found_keyed == []
    digest = keyed_target.rsplit(":", 1)[1]
    assert not any(
        hashlib.sha256(json.dumps(g, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        == digest
        for g in guesses()
    )


# -- settings, health, pilot-check, terminology --------------------------------------------------


@pytest.mark.parametrize("mode", ["hash", "encrypt"])
def test_startup_is_refused_without_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", mode)
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(PHI_KEY).decode())
    monkeypatch.delenv("MENDARI_INPUT_HASH_KEY", raising=False)
    with pytest.raises(MendariError, match=f"MENDARI_PHI_MODE={mode} needs MENDARI_INPUT_HASH_KEY"):
        create_app(db_path=tmp_path / "a.db")
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(MendariError, match="MENDARI_INPUT_HASH_KEY must be 32 random bytes"):
        create_app(db_path=tmp_path / "a.db")
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    assert phi_policy().hash_key == INPUT_HASH_KEY


def test_the_two_keys_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    with pytest.raises(MendariError, match="must differ from MENDARI_PHI_KEY"):
        phi_policy()


def test_health_names_the_hash_kind_and_key_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MENDARI_PHI_MODE", raising=False)
    monkeypatch.delenv("MENDARI_INPUT_HASH_KEY", raising=False)
    plain = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert plain.get("/api/health").json()["input_hash"] == "sha256"  # mode off, no key
    monkeypatch.setenv("MENDARI_PHI_MODE", "hash")
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    keyed = TestClient(create_app(db_path=tmp_path / "b.db"))
    kind = f"hmac-sha256:{key_id(INPUT_HASH_KEY)}"
    assert keyed.get("/api/health").json()["input_hash"] == kind
    decided = keyed.post(
        "/api/agents/mendari/patient-inbox-router/decide", json=message("refill please")
    ).json()
    assert decided["input_hash"].startswith(kind + ":")


def test_pilot_check_requires_keyed_hashes() -> None:
    keyed = PhiPolicy(hash_key=INPUT_HASH_KEY).input_hash({"a": 1})
    pilot_check.require_keyed_hashes([{"input_hash": keyed}])
    with pytest.raises(MendariError, match="set MENDARI_INPUT_HASH_KEY"):
        pilot_check.require_keyed_hashes([{"input_hash": keyed}, {"input_hash": "sha256:00"}])


def test_the_naming_system_describes_both_forms() -> None:
    (description,) = [spec[3] for spec in NAMING_SYSTEMS if spec[0] == "input-hash"]
    assert "`hmac-sha256:`" in description and "`sha256:`" in description
