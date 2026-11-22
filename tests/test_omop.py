# SPDX-License-Identifier: Apache-2.0
"""OMOP CDM as pack inputs (standards/omop.py), the round-trip check and the CLI."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from mendari.cli import main
from mendari.errors import MendariError
from mendari.sandbox import omop_check
from mendari.standards import omop

TRIAL = "mendari/clinical-trial-prescreener"
RECORD: dict[str, Any] = {
    "as_of": "2026-09-01",
    "patient": {"age": 62, "sex": "female"},
    "conditions": [
        {"code": "E11.9", "text": "Type 2 diabetes", "status": "active", "onset": "2015-03-02"},
        {"code": "22298006", "text": "Myocardial infarction", "status": "resolved"},
    ],
    "observations": [
        {"code": "4548-4", "name": "HbA1c", "value": 8.1, "unit": "%", "date": "2026-08-20"},
    ],
    "medications": [
        {"name": "metformin 500 mg", "status": "active", "start": "2024-01-10"},
        {"name": "insulin", "status": "stopped", "start": "2020-01-01", "end": "2021-06-01"},
    ],
    "notes": "Doing well.",
}


def test_a_record_makes_the_round_trip(db_target: str) -> None:
    db = omop.open_database(db_target, create=True)
    try:
        assert omop.write(db, [("case-1", RECORD), ("case-2", {"as_of": "2026-09-01"})]) == 2
        people = dict(omop.read(db))
    finally:
        db.close()
    back = people["case-1"]
    assert back["as_of"] == "2026-09-01"
    assert back["patient"] == {"sex": "female", "age": 62}
    assert back["conditions"] == RECORD["conditions"]
    assert back["observations"] == RECORD["observations"]
    assert back["medications"] == RECORD["medications"]
    assert back["notes"] == "Doing well."
    assert people["case-2"] == {
        "as_of": "2026-09-01",
        "conditions": [],
        "observations": [],
        "medications": [],
    }


def test_the_screening_date_can_be_given(db_target: str) -> None:
    db = omop.open_database(db_target, create=True)
    try:
        omop.write(db, [("case-1", RECORD)])
        [(_, later)] = list(omop.read(db, as_of=date(2031, 9, 1)))
    finally:
        db.close()
    assert later["as_of"] == "2031-09-01" and later["patient"]["age"] == 67
    # the metformin has no end, so it is still active; the insulin stopped long ago
    assert [m["status"] for m in later["medications"]] == ["active", "stopped"]


def test_each_condition_is_written_in_its_codes_vocabulary(tmp_path: Path) -> None:
    """ICD-10-CM, the local-site example's fictional codes, and any other code as SNOMED."""
    fictional = {"code": "FC101", "text": "Type 2 diabetes mellitus"}
    record = {**RECORD, "conditions": [*RECORD["conditions"], fictional]}
    db = omop.open_database(str(tmp_path / "cdm.db"), create=True)
    try:
        omop.write(db, [("case-1", record)])
        with db.session(exclusive=False) as s:
            rows = s.all(
                "SELECT concept_code, vocabulary_id FROM concept WHERE domain_id = ?", ["Condition"]
            )
        [(_, back)] = list(omop.read(db))
    finally:
        db.close()
    assert {(row["concept_code"], row["vocabulary_id"]) for row in rows} == {
        ("E11.9", "ICD10CM"),
        ("22298006", "SNOMED"),
        ("FC101", "Mendari fictional"),
    }
    assert back["conditions"] == record["conditions"]


def test_reading_creates_nothing_and_needs_a_screening_date(tmp_path: Path) -> None:
    empty = omop.open_database(str(tmp_path / "empty.db"))
    with empty.session(exclusive=False) as s:
        assert s.all("SELECT name FROM sqlite_master WHERE type = 'table'") == []
    empty.close()
    db = omop.open_database(str(tmp_path / "omop.db"), create=True)
    omop.write(db, [("case-1", RECORD)])
    with db.session(exclusive=True) as s:
        s.all("DELETE FROM observation_period")
    with pytest.raises(MendariError, match="no observation period"):
        list(omop.read(db))
    db.close()
    with pytest.raises(MendariError, match="schema name"):
        omop.open_database(str(tmp_path / "x.db"), schema="cdm; DROP TABLE person")


def test_the_population_report_suppresses_small_cells() -> None:
    decided = [{"outcome": "HUMAN_REVIEW", "answers": {"q": {"label": "yes"}}}] * 12 + [
        {"outcome": "AUTO", "answers": {"q": {"label": "no"}}}
    ] * 3
    report = omop.population(decided, ["q"])
    assert report["people"] == 15
    assert report["outcomes"] == {"AUTO": None, "HUMAN_REVIEW": 12}
    assert report["answers"]["q"] == {"no": None, "yes": 12}


def test_a_dataset_evaluates_the_same_through_omop(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "tests/fixtures/reference-packs/clinical-trial-prescreener/data"
    )
    result = omop_check.run(source / "exclusions.jsonl", TRIAL)
    assert result["identical"] is True and result["cases"] == result["people"] > 0


def test_cases_it_gets_wrong_are_the_same_cases_through_omop() -> None:
    """The local-site example has misclassified and missed critical cases: each file keys its
    own case hashes, so the check compares them by id (CI's reference-pack job runs it)."""
    local = Path(__file__).resolve().parents[1] / "datasets" / "trial-local-site-example.jsonl"
    result = omop_check.run(local, TRIAL)
    assert result["identical"] is True, result["differences"]


def test_the_cli_writes_datasets_and_reports_populations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "records.jsonl"
    row = {"id": "case-0", "input": RECORD, "labels": {"eligibility": "candidate"}}
    dataset.write_text(json.dumps(row) + "\n")
    db = tmp_path / "omop.db"
    assert main(["sandbox", "omop", "--dataset", str(dataset), "--db", str(db)]) == 0
    out = tmp_path / "people.jsonl"
    args = ["omop", "dataset", "--db", str(db), "--pack", TRIAL, "--out", str(out)]
    assert main([*args, "--labels", str(dataset)]) == 0
    [person] = [json.loads(line) for line in out.read_text().splitlines()]
    assert person["id"] == "case-0" and person["labels"] == {"eligibility": "candidate"}
    capsys.readouterr()
    assert main(["omop", "run", "--db", str(db), "--pack", TRIAL, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["people"] == 1 and set(report["outcomes"].values()) == {None}  # one person
    assert "Doing well" not in json.dumps(report)
    inbox = ["omop", "dataset", "--db", str(db), "--pack", "mendari/patient-inbox-router"]
    assert main([*inbox, "--out", str(tmp_path / "x.jsonl")]) == 1  # not a record pack
