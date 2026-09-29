from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from openhealth import Pack, load_pack
from openhealth.core.spec import Question
from openhealth.errors import PackValidationError
from openhealth.providers import ProviderInfo
from openhealth.providers.criteria import CriteriaProvider

FIXTURE = Path(__file__).parent / "fixtures" / "packs" / "trial-mini"
AS_OF = date(2026, 9, 1)
YNU = {"yes": "Met.", "no": "Not met.", "unknown": "Cannot tell."}

CONFIG = {
    "questions": {
        "age_18_75": {"checks": [{"number": {"field": "patient.age", "min": 18, "max": 75}}]},
        "hba1c_in_range": {
            "checks": [
                {
                    "observation": {
                        "codes": ["4548-4"],
                        "within_days": 90,
                        "min": 7.0,
                        "max": 10.5,
                        "unit": "%",
                        "convert": {"mmol/mol": {"multiply": 0.0915, "add": 2.15}},
                    }
                }
            ]
        },
        "t2d_diagnosis": {
            "checks": [
                {"condition": {"codes": ["E11*", "44054006"], "then": "yes"}},
                {"condition": {"codes": ["E10*"], "then": "no"}},
            ]
        },
        "no_recent_cv_event": {
            "checks": [
                {"condition": {"codes": ["I21*", "I63*"], "within_days": 180, "then": "no"}}
            ],
            "default": "yes",
        },
        "stable_metformin": {
            "checks": [{"medication": {"names": ["metformin"], "min_days": 90, "then": "yes"}}],
            "default": "no",
        },
        "no_insulin": {
            "checks": [{"medication": {"names": ["insulin"], "within_days": 90, "then": "no"}}],
            "default": "yes",
        },
    }
}


def day(offset: int) -> str:
    return (AS_OF - timedelta(days=offset)).isoformat()


@pytest.fixture
def pack(tmp_path: Path) -> Pack:
    root = tmp_path / "pack"
    shutil.copytree(FIXTURE, root)
    (root / "logic" / "criteria.yaml").write_text(yaml.safe_dump(CONFIG))
    return load_pack(root)


def provider(pack: Pack, config: dict[str, Any] | None = None) -> CriteriaProvider:
    if config is not None:
        (pack.root / "logic" / "criteria.yaml").write_text(yaml.safe_dump(config))
    questions = {
        key: Question.model_validate({"instructions": key, "criteria": YNU})
        for key in (config or CONFIG)["questions"]
    }
    info = ProviderInfo(slot="structured", variant="criteria", type="criteria")
    return CriteriaProvider(
        pack, info, questions, {"type": "criteria", "config": "logic/criteria.yaml"}
    )


def ask(pack: Pack, **record: Any) -> dict[str, tuple[str, str]]:
    output = provider(pack).answer({"as_of": AS_OF.isoformat(), **record})
    return {key: (a.label, a.evidence[0]) for key, a in output.answers.items()}


def test_a_complete_record(pack: Pack) -> None:
    result = ask(
        pack,
        patient={"age": 54},
        conditions=[{"code": "E11.9", "text": "Type 2 diabetes", "status": "active"}],
        observations=[{"code": "4548-4", "value": 8.1, "unit": "%", "date": day(58)}],
        medications=[{"name": "metformin 500 mg", "status": "active", "start": day(400)}],
    )
    assert {k: label for k, (label, _) in result.items()} == {
        "age_18_75": "yes",
        "hba1c_in_range": "yes",
        "t2d_diagnosis": "yes",
        "no_recent_cv_event": "yes",
        "stable_metformin": "yes",
        "no_insulin": "yes",
    }
    assert result["hba1c_in_range"][1] == (
        "4548-4 8.1 % on 2026-07-05 (58 d before screening), within [7, 10.5]"
    )
    assert result["stable_metformin"][1].endswith("(400 d)")


def test_missing_data_is_unknown_never_a_guess(pack: Pack) -> None:
    result = ask(pack)
    assert result["age_18_75"][0] == "unknown"
    assert result["hba1c_in_range"][0] == "unknown"
    assert result["t2d_diagnosis"][0] == "unknown"
    assert result["hba1c_in_range"][1].startswith("no data decides")


@pytest.mark.parametrize(
    ("observations", "label", "evidence"),
    [
        ([{"code": "4548-4", "value": 11.2, "unit": "%", "date": day(10)}], "no", "outside"),
        ([{"code": "4548-4", "value": 8.0, "unit": "%", "date": day(120)}], "unknown", "no data"),
        (
            [{"code": "4548-4", "value": 64, "unit": "mmol/mol", "date": day(5)}],
            "yes",
            "(= 8.01 %)",
        ),
        (
            [{"code": "4548-4", "value": 8.0, "unit": "mg/dL", "date": day(5)}],
            "unknown",
            "not recognised",
        ),
        (
            [
                {"code": "4548-4", "value": 12.0, "unit": "%", "date": day(80)},
                {"code": "4548-4", "value": 8.4, "unit": "%", "date": day(3)},
            ],
            "yes",
            "8.4",
        ),  # the latest value decides
        ([{"code": "4548-4", "value": 8.0, "unit": "%", "date": day(-5)}], "unknown", "no data"),
    ],
)
def test_lab_values_in_a_window(
    pack: Pack, observations: list[dict[str, Any]], label: str, evidence: str
) -> None:
    got_label, got_evidence = ask(pack, observations=observations)["hba1c_in_range"]
    assert got_label == label and evidence in got_evidence


def test_coded_conditions(pack: Pack) -> None:
    assert ask(pack, conditions=[{"code": "44054006"}])["t2d_diagnosis"][0] == "yes"
    assert ask(pack, conditions=[{"code": "E10.9"}])["t2d_diagnosis"][0] == "no"
    recent = [{"code": "I21.4", "onset": day(60)}]
    remote = [{"code": "I21.4", "onset": day(900)}]
    undated = [{"code": "I63.9"}]
    assert ask(pack, conditions=recent)["no_recent_cv_event"][0] == "no"
    assert ask(pack, conditions=remote)["no_recent_cv_event"][0] == "yes"
    assert ask(pack, conditions=undated)["no_recent_cv_event"] == (
        "unknown",
        "condition I63.9 has no onset date",
    )


def test_medication_durations_and_windows(pack: Pack) -> None:
    new = [{"name": "Metformin ER", "status": "active", "start": day(30)}]
    assert ask(pack, medications=new)["stable_metformin"] == (
        "no",
        f"medication Metformin ER (active) started {day(30)}, 30 d ago",
    )
    undated = [{"name": "metformin", "status": "active"}]
    assert ask(pack, medications=undated)["stable_metformin"][0] == "unknown"
    stopped = [{"name": "metformin", "status": "stopped", "start": day(900), "end": day(200)}]
    assert ask(pack, medications=stopped)["stable_metformin"][0] == "no"  # default
    recent_insulin = [{"name": "insulin glargine", "status": "completed", "end": day(30)}]
    old_insulin = [{"name": "insulin glargine", "status": "completed", "end": day(200)}]
    assert ask(pack, medications=recent_insulin)["no_insulin"][0] == "no"
    assert ask(pack, medications=old_insulin)["no_insulin"][0] == "yes"


REFERRAL = yaml.safe_load(
    """
    questions:
      ecg_on_file:
        checks: [{exists: {in: documents, codes: ["11524-6"], within_days: 365, then: "yes"}}]
        default: "no"
      any_note:
        checks: [{exists: {in: documents, codes: ["115*"], code_field: type, then: "yes"}}]
        default: "no"
      marked_urgent:
        checks: [{value: {field: referral.priority, in: [urgent, asap, stat], then: "yes"}}]
        default: "no"
    """
)


def ask_referral(pack: Pack, **record: Any) -> dict[str, tuple[str, str]]:
    output = provider(pack, REFERRAL).answer({"as_of": AS_OF.isoformat(), **record})
    return {key: (a.label, a.evidence[0]) for key, a in output.answers.items()}


def test_documents_on_file_within_a_window(pack: Pack) -> None:
    recent = {"code": "11524-6", "type": "11524-6", "date": day(40)}
    assert ask_referral(pack, documents=[recent])["ecg_on_file"] == (
        "yes",
        f"documents: 11524-6 on {day(40)} (40 d ago)",
    )
    stale = {"code": "11524-6", "date": day(400)}
    assert ask_referral(pack, documents=[stale])["ecg_on_file"][0] == "no"  # default
    undated = {"code": "11524-6"}
    assert ask_referral(pack, documents=[undated])["ecg_on_file"] == (
        "unknown",
        "documents: 11524-6 has no date",
    )
    assert ask_referral(pack)["ecg_on_file"][0] == "no"  # nothing on file
    # Another code field and a prefix pattern; no window, so the date does not matter.
    assert ask_referral(pack, documents=[{"type": "11506-3"}])["any_note"] == (
        "yes",
        "documents: 11506-3 on file",
    )


def test_field_values(pack: Pack) -> None:
    assert ask_referral(pack, referral={"priority": "asap"})["marked_urgent"] == (
        "yes",
        "referral.priority = asap",
    )
    assert ask_referral(pack, referral={"priority": "routine"})["marked_urgent"][0] == "no"
    assert ask_referral(pack)["marked_urgent"][0] == "no"


def test_answers_are_deterministic_distributions(pack: Pack) -> None:
    answer = provider(pack).answer({"as_of": AS_OF.isoformat()}).answers["age_18_75"]
    assert answer.probabilities == pytest.approx({"yes": 0.01, "no": 0.01, "unknown": 0.98})
    assert answer.provider.model is not None and answer.provider.model.startswith("criteria@")


def test_screening_date_defaults_to_the_local_day(
    pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `as_of`, "today" is the deployment's calendar day (`core.clock`), not UTC's."""
    monkeypatch.setattr("openhealth.core.clock.today", lambda: date(2026, 9, 29))

    def hba1c(days_ago: int) -> str:
        taken = (date(2026, 9, 29) - timedelta(days=days_ago)).isoformat()
        obs = [{"code": "4548-4", "value": 8.0, "unit": "%", "date": taken}]
        return provider(pack).answer({"observations": obs}).answers["hba1c_in_range"].label

    assert hba1c(90) == "yes"  # within 90 days of the local day
    assert hba1c(91) != "yes"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {"questions": {"age_18_75": {"checks": [{"number": {"field": "a"}}]}}},
            "exactly the slot",
        ),
        (
            {
                "questions": {
                    **CONFIG["questions"],
                    "age_18_75": {"checks": [{"number": {"field": "a", "in_range": "maybe"}}]},
                }
            },
            r"answers \['maybe'\], not labels of the question",
        ),
        (
            {
                "questions": {
                    **CONFIG["questions"],
                    "age_18_75": {
                        "checks": [
                            {"number": {"field": "a"}, "condition": {"codes": ["X"], "then": "no"}}
                        ]
                    },
                }
            },
            "exactly one of",
        ),
        (
            {
                "questions": {
                    **CONFIG["questions"],
                    "no_insulin": {"checks": [{"medication": {"then": "no"}}]},
                }
            },
            "`codes` or `names`",
        ),
    ],
)
def test_config_validation(pack: Pack, config: dict[str, Any], message: str) -> None:
    questions = {
        key: Question.model_validate({"instructions": key, "criteria": YNU})
        for key in CONFIG["questions"]
    }
    (pack.root / "logic" / "criteria.yaml").write_text(yaml.safe_dump(config))
    info = ProviderInfo(slot="structured", variant="criteria", type="criteria")
    with pytest.raises(PackValidationError, match=message):
        CriteriaProvider(
            pack, info, questions, {"type": "criteria", "config": "logic/criteria.yaml"}
        )


def test_labels_must_exist_on_the_question(pack: Pack) -> None:
    questions = {
        key: Question.model_validate(
            {"instructions": key, "criteria": {"yes": "y", "no": "n"}}  # no `unknown`
        )
        for key in CONFIG["questions"]
    }
    info = ProviderInfo(slot="structured", variant="criteria", type="criteria")
    with pytest.raises(PackValidationError, match="not labels of the question"):
        CriteriaProvider(
            pack, info, questions, {"type": "criteria", "config": "logic/criteria.yaml"}
        )


def test_choice_labels_and_field_gates(pack: Pack) -> None:
    config = yaml.safe_load(
        """
        questions:
          service:
            checks:
              - value: {field: order.code, in: ["72148", "72149"], then: lumbar_mri}
              - value: {field: order.code, in: ["29881"], then: knee_arthroscopy}
            default: other
          prior_imaging:
            checks:
              - exists: {in: procedures, codes: ["72100"], within_days: 180, then: "yes"}
                when: {field: order.code, in: ["72148", "72149"]}
              - exists: {in: procedures, codes: ["73721"], within_days: 180, then: "yes"}
                when: {field: order.code, in: ["29881"]}
            default: "no"
        """
    )
    (pack.root / "logic" / "criteria.yaml").write_text(yaml.safe_dump(config))
    labels = {"lumbar_mri": "l", "knee_arthroscopy": "k", "other": "o"}
    questions = {
        "service": Question.model_validate({"instructions": "s", "criteria": labels}),
        "prior_imaging": Question.model_validate({"instructions": "p", "criteria": YNU}),
    }
    info = ProviderInfo(slot="structured", variant="criteria", type="criteria")
    provider = CriteriaProvider(
        pack, info, questions, {"type": "criteria", "config": "logic/criteria.yaml"}
    )
    xray = [{"code": "72100", "date": day(30)}]

    def ask(code: str, procedures: list[dict[str, Any]]) -> dict[str, str]:
        record = {"as_of": AS_OF.isoformat(), "order": {"code": code}, "procedures": procedures}
        return {k: a.label for k, a in provider.answer(record).answers.items()}

    assert ask("72148", xray) == {"service": "lumbar_mri", "prior_imaging": "yes"}
    # A lumbar X-ray is not the prior imaging a knee arthroscopy needs.
    assert ask("29881", xray) == {"service": "knee_arthroscopy", "prior_imaging": "no"}
    assert ask("99213", xray) == {"service": "other", "prior_imaging": "no"}
    answer = provider.answer({"order": {"code": "72148"}}).answers["service"]
    assert answer.probabilities["lumbar_mri"] == pytest.approx(0.98)
    assert answer.probabilities["other"] == pytest.approx(0.01)
