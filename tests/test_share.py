"""Shareable evaluation reports (evals/share.py): metrics leave the site, cases never do.

A shared report is `mendari.site-report/1`, a closed schema the client builds and the registry
checks: names and rounded numbers only, small bins dropped, no subgroup, no case id, no dataset
identity, no approver."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from mendari import Decision
from mendari.core.policy import GovernanceOverride
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals import evaluate, resolve_dataset
from mendari.evals.share import MIN_GROUP, SiteReport, shareable, summary

BENCHMARK = json.loads(
    (resolve_pack("mendari/referral-router").root / "evals" / "benchmark.json").read_text()
)
FREE_TEXT = "Jane Doe, DOB 1960-02-01, called about chest pain"


def _cell(n: int, accuracy: float) -> dict[str, Any]:
    return {
        "n": n,
        "accuracy": accuracy,
        "auto_rate": 0.0,
        "abstention_rate": 0.0,
        "false_automation_rate": None,
    }


def test_case_ids_and_dataset_identity_stay_home() -> None:
    shared = shareable(BENCHMARK)
    text = json.dumps(shared)
    assert shared["format"] == "mendari.site-report/1"
    assert "misclassified" not in shared
    assert all(case["id"] not in text for case in BENCHMARK["misclassified"])
    assert shared["dataset"] == {"n": BENCHMARK["dataset"]["n"]}  # no name, no digest
    assert "id" not in shared and "created_at" not in shared  # links to the site's store


def test_missed_critical_cases_leave_no_id() -> None:
    """The inbox pack's rules miss 16 of the 20 held-out emergencies: their ids were sent as
    `missed_ids` before. Only the counts leave now."""
    pack = resolve_pack("mendari/patient-inbox-router")
    dataset = resolve_dataset(pack, "red_flags_holdout")
    report = evaluate(Decision(pack), dataset)
    missed = report["decisions"]["critical"][0]["missed_ids"]
    assert len(missed) >= 10
    shared = shareable(report)
    text = json.dumps(shared)
    assert all(record["id"] not in text for record in dataset.records)
    critical = shared["decisions"]["critical"][0]
    assert critical == {
        "question": "urgency",
        "label": "urgent",
        "n": 20,
        "detected": 20 - len(missed),
        "detection_rate": (20 - len(missed)) / 20,
        "auto_missed": 0,
    }


def test_an_override_is_shared_as_a_fact_not_a_name() -> None:
    pack = resolve_pack("mendari/clinical-trial-prescreener")
    approval = GovernanceOverride(approved_by="Dr. A. Person", reference="IRB-2026-0042")
    report = evaluate(Decision(pack, governance=approval), resolve_dataset(pack, "exclusions"))
    shared = shareable(report)
    assert shared["policy"]["governance_override"] is True
    assert "Person" not in json.dumps(shared) and "IRB-2026" not in json.dumps(shared)


def test_subgroups_never_leave_and_small_bins_are_left_out() -> None:
    """A subgroup's values come from a site's data: a clinic, a clinician, a patient seen often
    enough, a date. None of them leave, whatever the cell's size."""
    report = {
        **BENCHMARK,
        "subgroups": {
            "patient": {"Jane Doe MRN 00123 DOB 1985.03.14": _cell(12, 0.9)},
            "admit_date": {"2026-03-14": _cell(12, 0.9)},
            "age": {"90+": _cell(2, 0.5), "40-64": _cell(50, 0.9)},
        },
    }
    shared = shareable(report)
    assert "subgroups" not in shared
    assert "Jane" not in json.dumps(shared) and "2026-03-14" not in json.dumps(shared)
    bins = shared["questions"]["next_step"]["calibration"]["bins"]
    assert bins and all(b["n"] >= MIN_GROUP for b in bins)
    assert shared["questions"]["next_step"]["calibration"]["ece"] == round(
        BENCHMARK["questions"]["next_step"]["calibration"]["ece"], 4
    )


def test_numbers_are_rounded_on_both_sides() -> None:
    report = copy.deepcopy(BENCHMARK)
    report["questions"]["next_step"]["brier"] = 0.1234567890123456
    report["latency_ms"]["max"] = 123456789.12345678
    shared = shareable(report)
    assert shared["questions"]["next_step"]["brier"] == 0.123
    assert shared["latency_ms"]["max"] == 123456789.123
    exact = copy.deepcopy(shared)
    exact["decisions"]["recommendation_accuracy"] = 0.987654321
    received = SiteReport.model_validate(exact).model_dump(mode="json")
    assert received["decisions"]["recommendation_accuracy"] == 0.9877


def test_the_receiver_refuses_subgroups_and_small_bins() -> None:
    shared = shareable(BENCHMARK)
    with pytest.raises(ValidationError):
        SiteReport.model_validate({**shared, "subgroups": {"age": {"40-64": _cell(50, 0.9)}}})
    small = copy.deepcopy(shared)
    small["questions"]["next_step"]["calibration"]["bins"].append(
        {"low": 0.0, "high": 0.1, "n": 3, "confidence": 0.05, "accuracy": 0.0}
    )
    with pytest.raises(ValidationError):
        SiteReport.model_validate(small)


def _objects(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(value, dict):
        yield path
        for key, item in value.items():
            yield from _objects(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _objects(item, (*path, index))


def _strings(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(value, str):
        yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*path, index))


def _at(value: Any, path: tuple[Any, ...]) -> Any:
    for part in path:
        value = value[part]
    return value


SHARED = shareable(BENCHMARK)
OBJECTS = list(_objects(SHARED))
STRINGS = list(_strings(SHARED))


@pytest.mark.parametrize("path", OBJECTS, ids=lambda p: ".".join(map(str, p)) or "root")
def test_an_unknown_key_anywhere_is_refused(path: tuple[Any, ...]) -> None:
    payload = copy.deepcopy(SHARED)
    target = _at(payload, path)
    target["note"] = FREE_TEXT
    # Where the keys are names (questions, labels, subgroup values), a key of free text is
    # refused by its pattern; everywhere else any extra key is.
    target[FREE_TEXT] = target[next(iter(target))] if target else 1
    with pytest.raises(ValidationError):
        SiteReport.model_validate(payload)
    # The sender either leaves the key out (fields it rebuilds) or refuses the report.
    try:
        sent = json.dumps(shareable({**BENCHMARK, **payload}))
    except MendariError:
        return
    assert FREE_TEXT not in sent and '"note"' not in sent


@pytest.mark.parametrize("path", STRINGS, ids=lambda p: ".".join(map(str, p)))
def test_free_text_in_any_string_is_refused(path: tuple[Any, ...]) -> None:
    payload = copy.deepcopy(SHARED)
    parent = _at(payload, path[:-1])
    parent[path[-1]] = FREE_TEXT
    with pytest.raises(ValidationError):
        SiteReport.model_validate(payload)


def test_a_report_that_cannot_be_shared_names_fields_never_values() -> None:
    report = copy.deepcopy(BENCHMARK)
    report["providers"]["triage_screen"]["model"] = FREE_TEXT
    with pytest.raises(MendariError) as error:
        shareable(report)
    assert "providers.<key>.model" in str(error.value)
    assert "Jane" not in str(error.value)
    keyed = copy.deepcopy(BENCHMARK)
    keyed["questions"][FREE_TEXT] = keyed["questions"]["next_step"]
    with pytest.raises(MendariError) as error:
        shareable(keyed)
    assert "questions.<key>" in str(error.value) and "Jane" not in str(error.value)
    coded = copy.deepcopy(BENCHMARK)
    coded["errors"] = {FREE_TEXT: "not a count"}
    with pytest.raises(MendariError) as error:
        shareable(coded)
    assert "errors.<key>" in str(error.value) and "Jane" not in str(error.value)


def test_the_summary_names_the_numbers_a_comparison_shows() -> None:
    numbers = summary(shareable(BENCHMARK))
    assert numbers["primary"] == "next_step" and numbers["n"] == BENCHMARK["dataset"]["n"]
    assert numbers["accuracy"] == BENCHMARK["questions"]["next_step"]["accuracy"]
    assert numbers["automation_rate"] == 0.0
    assert numbers["ece"] is not None


@pytest.mark.parametrize(
    "pack_id",
    [
        p
        for p in (
            "ai-guard",
            "clinical-trial-prescreener",
            "patient-inbox-router",
            "prior-auth-preflight",
            "referral-router",
        )
    ],
)
def test_every_shipped_benchmark_can_be_shared(pack_id: str) -> None:
    benchmark = json.loads(
        (resolve_pack(f"mendari/{pack_id}").root / "evals" / "benchmark.json").read_text()
    )
    shared = SiteReport.model_validate(shareable(benchmark))
    assert shared.pack.id == f"mendari/{pack_id}"
