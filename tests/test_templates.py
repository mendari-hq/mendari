from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mendari import Decision
from mendari.core.spec import PackManifest
from mendari.core.templates import fill, unknown_placeholders
from mendari.errors import PackValidationError
from mendari.standards import cds_hooks
from mendari.standards.fhir import FhirAdapter
from tests.test_fhir_lists import BUNDLE, MINI, PATIENT_9, RECORD, cds_request, searchset


def test_placeholders_are_built_ins_or_questions() -> None:
    questions = ["specialty", "urgency"]
    assert (
        unknown_placeholders("Route to {specialty} ({priority}): {recommendation}", questions) == []
    )
    assert unknown_placeholders("{nope} {0} {specialty.x} {}", questions) == [
        "",
        "0",
        "nope",
        "specialty.x",
    ]
    assert unknown_placeholders("unbalanced {", questions) == ["unbalanced {"]


def test_answers_fill_templates_and_missing_ones_read_unknown() -> None:
    result = Decision.from_path(MINI).decide_fhir(BUNDLE)[0]
    text = "{recommendation}: age {age_18_75}, {priority}, {note_pancreatitis}, {nope}"
    assert fill(text, result, recommendation="candidate") == (
        "candidate: age yes, routine, no, unknown"
    )


def test_task_and_card_templates_name_answers(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    shutil.copytree(MINI, root)
    mapping = yaml.safe_load((root / "fhir" / "mapping.yaml").read_text())
    mapping["task"]["performer"] = "trial-team-{t2d_diagnosis}"
    mapping["task"]["description"] = "Prescreen {recommendation} (HbA1c {hba1c_in_range})"
    (root / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping))
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["cds_hooks"]["card"]["summaries"]["candidate"] = "Candidate (T2D {t2d_diagnosis})"
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    decision = Decision.from_path(root)
    _, task = decision.decide_fhir(BUNDLE)
    assert task["performerType"][0]["coding"][0]["code"] == "trial-team-yes"
    assert task["description"] == "Prescreen candidate (HbA1c yes)"
    prefetch = {
        "patient": PATIENT_9,
        "conditions": searchset([r for r in RECORD if r["resourceType"] == "Condition"]),
        "observations": searchset([r for r in RECORD if r["resourceType"] == "Observation"]),
        "notes": searchset([r for r in RECORD if r["resourceType"] == "DocumentReference"]),
    }
    card = cds_hooks.invoke(decision, cds_request(prefetch))["cards"][0]
    assert card["summary"] == "Candidate (T2D yes)"


def test_unknown_placeholders_are_refused_when_the_pack_loads(tmp_path: Path) -> None:
    raw = yaml.safe_load((MINI / "pack.yaml").read_text())
    raw["cds_hooks"]["card"]["summaries"]["candidate"] = "Candidate for {study}"
    with pytest.raises(ValidationError, match=r"unknown placeholders \['study'\]"):
        PackManifest.model_validate(raw)

    root = tmp_path / "pack"
    shutil.copytree(MINI, root)
    mapping = yaml.safe_load((root / "fhir" / "mapping.yaml").read_text())
    mapping["task"]["performer"] = "{team}"
    (root / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping))
    with pytest.raises(PackValidationError, match=r"task.performer uses unknown placeholders"):
        FhirAdapter.for_pack(Decision.from_path(root).pack)
