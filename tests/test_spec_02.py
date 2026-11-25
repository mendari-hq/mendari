# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import Decision, Outcome, Pack, load_pack
from mendari.core.card import render_card
from mendari.core.policy import apply_policy, matches
from mendari.core.spec import (
    LICENSED_CODE_SYSTEMS,
    Condition,
    PackManifest,
    Question,
    is_spdx_expression,
    licensed_systems,
)
from mendari.providers import Answer, ProviderInfo
from tests.conftest import CLINICAL_DIR

INFO = ProviderInfo(slot="t", variant="t", type="t")


def answer(question: str, probabilities: dict[str, float]) -> Answer:
    label = max(probabilities, key=lambda k: probabilities[k])
    return Answer(question, label, probabilities[label], probabilities, INFO)


def manifest_dict() -> dict[str, Any]:
    """The clinical review fixture's manifest (risk level 2, a score question a rule reads)."""
    raw = yaml.safe_load((CLINICAL_DIR / "agent.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


# -- question types ----------------------------------------------------------------------------


def test_boolean_questions_always_have_yes_and_no() -> None:
    question = Question.model_validate({"type": "boolean", "instructions": "Adult?"})
    assert question.labels == ["yes", "no"]
    with pytest.raises(ValidationError, match="only has labels yes/no"):
        Question.model_validate(
            {"type": "boolean", "instructions": "Adult?", "criteria": {"maybe": "?"}}
        )


def test_score_questions_are_ordered_and_have_expected_values() -> None:
    question = Question.model_validate(
        {
            "type": "score",
            "instructions": "Severity?",
            "levels": {
                "severe": {"score": 3, "criterion": "Bad."},
                "mild": {"score": 1, "criterion": "Fine."},
            },
        }
    )
    assert question.labels == ["mild", "severe"]
    assert question.expected_score({"mild": 0.25, "severe": 0.75}) == pytest.approx(2.5)
    with pytest.raises(ValidationError, match="levels"):
        Question.model_validate(
            {"type": "score", "instructions": "x", "criteria": {"a": "a", "b": "b"}}
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        # A score bound on a question that is not a score.
        (lambda m: m["policy"]["rules"][0]["when"].update(question="disposition"), "score bounds"),
    ],
)
def test_derive_and_score_validation(mutate: Any, message: str) -> None:
    raw = manifest_dict()
    mutate(raw)
    with pytest.raises(ValidationError, match=message):
        PackManifest.model_validate(raw)


# -- runtime, policy ---------------------------------------------------------------------------


def test_fixture_pack_runs_end_to_end() -> None:
    decision = Decision.from_path(CLINICAL_DIR)
    result = decision({"note": {"text": "Patient reports mild throat pain for two days."}})
    assert result.primary == "disposition"
    assert result.answers["disposition"].label == "self_care"
    assert result.outcome is Outcome.HUMAN_REVIEW  # risk level 2 caps AUTO
    assert result.answers["severity"].value == pytest.approx(
        decision.pack.manifest.questions["severity"].expected_score(
            result.answers["severity"].probabilities
        )
    )
    assert "value" in result.to_dict()["answers"]["severity"]


def test_score_conditions() -> None:
    severe = Answer("severity", "severe", 0.9, {"mild": 0.05, "severe": 0.95}, INFO, value=2.9)
    assert matches(Condition(question="severity", min_score=2.5), severe)
    assert not matches(Condition(question="severity", max_score=2.0), severe)
    assert not matches(
        Condition(question="severity", min_score=2.5), answer("severity", {"mild": 1.0})
    )
    with pytest.raises(ValidationError, match="needs `labels`"):
        Condition(question="severity")


def test_decision_card_shows_types_levels_and_tables(triage_agent: Pack) -> None:
    card = render_card(load_pack(CLINICAL_DIR))
    assert "| `severe` | 3 | A severe symptom, such as pain the patient cannot bear. |" in card
    assert "Rule **moderate-or-worse**: `severity` expected score ≥ 1.5 ⇒ `HUMAN_REVIEW`" in card
    assert "**`urgency`** *(boolean)*" in render_card(triage_agent)


# -- min_probability ---------------------------------------------------------------------------


def test_min_probability_acts_on_a_labels_probability_not_on_the_winner() -> None:
    maybe_urgent = answer("urgency", {"urgent": 0.40, "routine": 0.60})  # routine wins
    condition = Condition(question="urgency", labels=["urgent"], min_probability=0.35)
    assert matches(condition, maybe_urgent)
    assert not matches(condition, answer("urgency", {"urgent": 0.30, "routine": 0.70}))
    # The argmax test it replaces would have missed this case.
    assert not matches(
        Condition(question="urgency", labels=["urgent"], min_confidence=0.35), maybe_urgent
    )
    with pytest.raises(ValidationError, match="needs the `labels`"):
        Condition(question="urgency", min_probability=0.3, min_score=1)
    with pytest.raises(ValidationError, match="not both"):
        Condition(question="urgency", labels=["urgent"], min_probability=0.3, min_confidence=0.2)


def test_inbox_escalates_at_the_documented_threshold(triage_agent: Pack) -> None:
    """The triage fixture's possible-emergency rule, at P(same-day symptoms) 0.35."""
    answers = {
        "destination": answer("destination", {"pharmacy": 0.97, "nurse": 0.03}),
        "urgency": answer("urgency", {"yes": 0.40, "no": 0.60}),
        "abusive": answer("abusive", {"yes": 0.02, "no": 0.98}),
    }
    result = apply_policy(triage_agent.manifest, answers, {}, None)
    assert (result.outcome, result.priority.value, result.rule) == (
        Outcome.HUMAN_REVIEW,
        "stat",
        "possible-emergency",
    )
    card = render_card(triage_agent)
    assert "P(`urgency` is `yes`) ≥ 0.35" in card


# -- licence and code systems (LP-14) -----------------------------------------------------------


@pytest.mark.parametrize(
    "licence",
    [
        "Apache-2.0",
        "MIT OR Apache-2.0",
        "(MIT AND BSD-3-Clause)",
        "LicenseRef-site-terms",
        "DocumentRef-spdx-tool-1.2:LicenseRef-MIT-Style-2",
        "GPL-2.0-or-later WITH Classpath-exception-2.0",
        "GPL-2.0+",
    ],
)
def test_a_licence_is_an_spdx_expression(licence: str) -> None:
    PackManifest.model_validate(manifest_dict() | {"license": licence})


@pytest.mark.parametrize(
    "licence",
    [
        "Apache 2.0",
        "apache license",
        "MIT OR",
        "(MIT",
        "",
        "AND",
        "MIT WITH OR",
        "MIT WITH",
        "MIT)",
    ],
)
def test_a_licence_that_is_not_an_spdx_expression_is_refused(licence: str) -> None:
    with pytest.raises(ValidationError, match="SPDX license expression"):
        PackManifest.model_validate(manifest_dict() | {"license": licence})


def test_a_licensed_code_system_needs_its_licence_terms() -> None:
    snomed = {"url": "http://snomed.info/sct", "name": "SNOMED CT"}
    for url in ("http://snomed.info/sct", "urn:oid:2.16.840.1.113883.6.96",
                "http://www.ama-assn.org/go/cpt", "urn:oid:2.16.840.1.113883.6.12"):  # fmt: skip
        with pytest.raises(ValidationError, match="whose codes are licensed"):
            PackManifest.model_validate(manifest_dict() | {"code_systems": [snomed | {"url": url}]})
    noted = snomed | {"licence_note": "Used in a SNOMED International member country."}
    free = {"url": "http://loinc.org", "name": "LOINC"}
    manifest = PackManifest.model_validate(manifest_dict() | {"code_systems": [noted, free]})
    assert [use.url for use in manifest.code_systems] == [
        "http://snomed.info/sct",
        "http://loinc.org",
    ]
    with pytest.raises(ValidationError, match="more than once"):
        PackManifest.model_validate(manifest_dict() | {"code_systems": [free, free]})
    with pytest.raises(ValidationError):
        PackManifest.model_validate(manifest_dict() | {"code_systems": [free | {"url": "loinc"}]})


def test_a_pack_whose_files_name_a_licensed_system_must_declare_it(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    shutil.copytree(CLINICAL_DIR, root)
    data = root / "logic" / "codes.json"
    data.write_text('{"note": "coded as urn:oid:2.16.840.1.113883.6.96"}\n')
    with pytest.raises(Exception, match=r"logic/codes\.json names SNOMED CT .* declare it"):
        load_pack(root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["code_systems"] = [
        {"url": "http://snomed.info/sct", "name": "SNOMED CT", "licence_note": "Member country."}
    ]
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    card = render_card(load_pack(root))
    assert "- SNOMED CT (`http://snomed.info/sct`). Licence: Member country." in card


def test_a_licence_is_short_and_read_without_recursion() -> None:
    nested = "(" * 600 + "MIT" + ")" * 600  # parsed iteratively: no RecursionError
    assert is_spdx_expression(nested)
    with pytest.raises(ValidationError, match="at most 200 characters"):
        PackManifest.model_validate(manifest_dict() | {"license": nested})


@pytest.mark.parametrize(
    ("text", "system"),
    [
        ('"system": "http://snomed.info/sct"', "http://snomed.info/sct"),
        ('"system": "http:\\/\\/snomed.info\\/sct"', "http://snomed.info/sct"),  # JSON-escaped
        ('codeSystem="2.16.840.1.113883.6.96"', "http://snomed.info/sct"),  # a bare OID
        ("http://snomed.info/id/22298006", "http://snomed.info/sct"),
        ("coded in urn:oid:2.16.840.1.113883.6.12.", "http://www.ama-assn.org/go/cpt"),
        ("https://www.meddra.org", "http://terminology.hl7.org/CodeSystem/mdr"),
        ("https://codesystem.x12.org/005010/1365", "https://codesystem.x12.org"),
        ("http://www.ada.org/cdt", "http://www.ada.org/cdt"),
        # As HL7's terminology registry (THO) lists them, and escaped as files may:
        ("https://x12.org/codes/claim-adjustment-reason-codes", "https://codesystem.x12.org"),
        ("http://terminology.hl7.org/CodeSystem/X12.3", "https://codesystem.x12.org"),
        ("http://terminology.hl7.org/CodeSystem/CD2", "http://www.ada.org/cdt"),
        (
            "http://terminology.hl7.org/CodeSystem/MDRSPA",
            "http://terminology.hl7.org/CodeSystem/mdr",
        ),
        ("urn:oid:2.16.840.1.113883.6.163.1", "http://terminology.hl7.org/CodeSystem/mdr"),
        ("snomed.info%2Fsct", "http://snomed.info/sct"),
        ("snomed.info&#x2F;sct", "http://snomed.info/sct"),
    ],
)
def test_licensed_systems_are_found_however_a_file_spells_them(text: str, system: str) -> None:
    assert licensed_systems(text) == {system}


def test_other_oids_and_free_systems_name_no_licensed_system() -> None:
    for text in (
        "2.16.840.1.113883.6.121",
        "2.16.840.1.113883.6.12.5",
        "2.16.840.1.113883.6.1634",
        "http://terminology.hl7.org/CodeSystem/v3-ActCode",
        "http://terminology.hl7.org/CodeSystem/CD2-other",
        "http://loinc.org",
        "http://hl7.org/fhir/sid/icd-10-cm",
        "https://mendari.co/fhir/CodeSystem/fictional-procedures",
    ):
        assert licensed_systems(text) == set(), text


def test_a_code_system_may_be_any_absolute_uri() -> None:
    for url in ("urn:ietf:bcp:47", "urn:iso:std:iso:3166", "urn:oid:2.16.840.1.113883.6.1"):
        PackManifest.model_validate(manifest_dict() | {"code_systems": [{"url": url, "name": "x"}]})
    with pytest.raises(ValidationError):
        PackManifest.model_validate(
            manifest_dict() | {"code_systems": [{"url": "loinc.org", "name": "x"}]}
        )


@pytest.mark.parametrize("url", sorted(LICENSED_CODE_SYSTEMS))
def test_a_pack_naming_a_licensed_system_with_escaped_slashes_must_declare_it(
    tmp_path: Path, url: str
) -> None:
    """The load check reads every licensed system's url as a JSON serializer may write it, its
    slashes escaped (`http:\\/\\/...`), not only the unit under test."""
    root = tmp_path / "pack"
    shutil.copytree(CLINICAL_DIR, root)
    escaped = url.replace("/", "\\/")
    (root / "logic" / "codes.json").write_text(f'{{"system": "{escaped}"}}\n')
    with pytest.raises(Exception, match="declare it in code_systems with a licence_note"):
        load_pack(root)
