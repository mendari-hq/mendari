# SPDX-License-Identifier: Apache-2.0
"""The Prior Authorization Preflight reference pack and its synthetic data."""

from __future__ import annotations

import base64
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision
from mendari.cli import main
from mendari.core.policy import Outcome
from mendari.sandbox import synthetic_prior_auth
from mendari.sandbox.synthetic_prior_auth import procedure_codings
from mendari.standards import cds_hooks

ROOT = Path(__file__).parents[1]
PACK_DIR = ROOT / "packs" / "prior-auth-preflight"
STRUCTURED = ("service", "payer", "diagnosis_supports", "prior_imaging")
TODAY = date.today()


@pytest.fixture(scope="module")
def preflight() -> Decision:
    return Decision.from_path(PACK_DIR)


# -- synthetic data ----------------------------------------------------------------------------


def test_generation_is_deterministic_and_sized() -> None:
    splits = synthetic_prior_auth.public_splits()
    assert {name: len(records) for name, records in splits.items()} == {
        "calibration": 300,
        "test": 600,
    }
    assert splits == synthetic_prior_auth.public_splits()
    assert len(synthetic_prior_auth.local_site()) == 300


@pytest.mark.parametrize("dataset", ["documentation_notes", "documentation_notes_holdout"])
def test_every_dataset_follows_the_decision_table(dataset: str) -> None:
    lines = (PACK_DIR / "data" / f"{dataset}.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    records += synthetic_prior_auth.public_splits()["test"]
    for record in records:
        labels = record["labels"]
        assert labels["next_step"] == synthetic_prior_auth.next_step(labels), record["id"]
    steps = Counter(r["labels"]["next_step"] for r in synthetic_prior_auth.public_splits()["test"])
    assert set(steps) == {"submit_ready", "needs_attention", "not_required", "not_checked"}


def test_the_structured_answers_agree_with_ground_truth_on_public_data(
    preflight: Decision,
) -> None:
    provider = preflight.providers["structured"]
    for record in synthetic_prior_auth.public_splits()["calibration"]:
        answers = provider.answer(record["input"]).answers
        for question in STRUCTURED:
            assert answers[question].label == record["labels"][question], (record["id"], question)


def test_the_local_site_differs_the_way_the_card_says() -> None:
    local = synthetic_prior_auth.local_site()
    codes = Counter(p["code"] for r in local for p in r["input"]["procedures"])
    assert codes["FX112"] > 0  # lumbar X-rays under a code the pack does not list
    assert sum(r["attributes"]["style"] == "shorthand" for r in local) > 100
    payers = Counter(r["labels"]["payer"] for r in local)
    assert payers["beacon"] > payers["acme"]


def test_sandbox_generate_writes_the_prior_auth_data(tmp_path: Path) -> None:
    local = tmp_path / "local.jsonl"
    argv = ["sandbox", "generate", "--kind", "prior-auth", "--pack-dir", str(tmp_path / "pack")]
    assert main([*argv, "--local-out", str(local)]) == 0
    test = (tmp_path / "pack" / "data" / "test.jsonl").read_text().splitlines()
    assert len(test) == 600 and json.loads(test[0])["id"] == "pa-0300"
    assert len(local.read_text().splitlines()) == 300


# -- decisions ---------------------------------------------------------------------------------


def test_reasons_list_the_gaps_and_nothing_is_automated_on_rules(preflight: Decision) -> None:
    record = {
        "as_of": "2026-09-01",
        "order": {"code": "FX101"},
        "coverage": {"payer": "acme"},
        "conditions": [{"code": "M54.16"}],
        "procedures": [],
        "notes": "Back pain. Started PT 2 weeks ago.",
    }
    result = preflight.decide(record)
    assert result.recommendation == "needs_attention"
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert (
        "needs_attention: conservative_therapy is no (service is lumbar_mri, red_flag is no)"
        in result.reasons
    )
    assert (
        "needs_attention: prior_imaging is no (service is lumbar_mri, payer is acme)"
        in result.reasons
    )


# -- FHIR and CDS Hooks ------------------------------------------------------------------------


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _order(id_: str, code: str) -> dict[str, Any]:
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "code": {"coding": procedure_codings(code)},  # fictional, then a payer's ICD-10-PCS
        "subject": {"reference": "Patient/p1"},
    }


def _prefetch(payer: str, note: str) -> dict[str, Any]:
    def bundle(*resources: dict[str, Any]) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": r} for r in resources],
        }

    def coded(kind: str, id_: str, code: str, **extra: Any) -> dict[str, Any]:
        return {"resourceType": kind, "id": id_, "code": {"coding": [{"code": code}]}, **extra}

    data = base64.b64encode(note.encode()).decode()
    return {
        "patient": {"resourceType": "Patient", "id": "p1", "birthDate": f"{TODAY.year - 50}-01-01"},
        "coverage": bundle(
            {"resourceType": "Coverage", "id": "c1", "payor": [{"identifier": {"value": payer}}]}
        ),
        "conditions": bundle(
            coded("Condition", "d1", "M54.16"), coded("Condition", "d2", "S83.241A")
        ),
        "procedures": bundle(
            coded("Procedure", "x1", "FX111", performedDateTime=_ago(40)),
            coded("Procedure", "x2", "FX211", performedDateTime=_ago(40)),
        ),
        "notes": bundle(
            {
                "resourceType": "DocumentReference",
                "id": "n1",
                "content": [{"attachment": {"data": data}}],
            }
        ),
    }


def test_order_sign_gives_a_card_per_covered_order(preflight: Decision) -> None:
    note = "Completed 8 weeks of physical therapy. The knee locks when climbing stairs."
    request = {
        "hook": "order-sign",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "context": {
            "userId": "Practitioner/1",
            "patientId": "p1",
            "draftOrders": {
                "resourceType": "Bundle",
                "entry": [
                    {"resource": _order("mri", "FX101")},
                    {"resource": _order("scope", "FX202")},
                    {"resource": _order("lab", "FX904")},  # a metabolic panel: not covered
                ],
            },
        },
        "prefetch": _prefetch("acme", note),
    }
    cards = cds_hooks.invoke(preflight, request)["cards"]
    assert [c["summary"] for c in cards] == [
        "Prior auth for Lumbar MRI (Acme): ready to submit",  # display names (LP-23f)
        "Prior auth for Knee arthroscopy (Acme): ready to submit",
    ]
    task = cards[0]["suggestions"][0]["actions"][0]["resource"]
    assert task["focus"] == {"reference": "ServiceRequest/mri"}
    assert task["description"] == "Prior auth for lumbar_mri (acme): submit_ready"


def test_beacon_sleep_study_needs_no_prior_auth(preflight: Decision) -> None:
    result, _ = preflight.decide_fhir(
        _prefetch("beacon", "Loud snoring."), focus=_order("psg", "FX301")
    )
    assert result.recommendation == "not_required"
    assert result.answers["payer"].label == "beacon"
