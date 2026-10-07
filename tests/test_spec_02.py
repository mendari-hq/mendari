# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import itertools
import random
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import Decision, Outcome, load_pack
from mendari.core.card import render_card
from mendari.core.derive import complete_answers, derive_answer, explain
from mendari.core.policy import matches
from mendari.core.spec import Condition, PackManifest, Question
from mendari.evals import evaluate, resolve_dataset
from mendari.providers import Answer, ProviderInfo
from tests.conftest import INBOX_DIR

TRIAL_MINI = Path(__file__).parent / "fixtures" / "packs" / "trial-mini"
INFO = ProviderInfo(slot="t", variant="t", type="t")


def answer(question: str, probabilities: dict[str, float]) -> Answer:
    label = max(probabilities, key=lambda k: probabilities[k])
    return Answer(question, label, probabilities[label], probabilities, INFO)


def manifest_dict() -> dict[str, Any]:
    raw = yaml.safe_load((TRIAL_MINI / "pack.yaml").read_text())
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


def test_spec_01_packs_cannot_use_spec_02_features() -> None:
    raw = manifest_dict()
    raw["spec_version"] = "0.1"
    with pytest.raises(ValidationError, match=r"spec 0\.2 features"):
        PackManifest.model_validate(raw)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m["questions"]["eligibility"]["derive"]["table"].reverse(), "last row"),
        (
            lambda m: m["questions"]["eligibility"]["derive"]["from"].append("eligibility"),
            "non-derived",
        ),
        (lambda m: m["questions"]["eligibility"]["derive"]["from"].append("nope"), "non-derived"),
        (
            lambda m: m["questions"]["eligibility"]["derive"]["from"].append("inc_adult"),
            "more than once",
        ),
        (lambda m: m["providers"]["screen"]["answers"].append("eligibility"), "cannot be answered"),
        (
            lambda m: m["questions"]["eligibility"]["derive"]["table"][0].update(any="maybe"),
            "row label",
        ),
        (
            lambda m: m["questions"]["eligibility"]["derive"]["table"][0].update(then="gone"),
            "row result",
        ),
        (lambda m: m["policy"]["rules"][0]["when"].update(question="inc_t2d"), "score bounds"),
    ],
)
def test_derive_and_score_validation(mutate: Any, message: str) -> None:
    raw = manifest_dict()
    mutate(raw)
    with pytest.raises(ValidationError, match=message):
        PackManifest.model_validate(raw)


# -- derived answers ---------------------------------------------------------------------------


def eligibility() -> Question:
    return PackManifest.model_validate(manifest_dict()).questions["eligibility"]


def test_derived_probabilities_by_hand() -> None:
    answers = {
        "inc_adult": answer("inc_adult", {"yes": 0.9, "no": 0.1}),
        "inc_t2d": answer("inc_t2d", {"yes": 0.8, "no": 0.1, "unknown": 0.1}),
        "not_pregnant": answer("not_pregnant", {"yes": 0.7, "no": 0.2, "unknown": 0.1}),
    }
    derived = derive_answer("eligibility", eligibility(), answers)
    # candidate = all yes; not_eligible = any no; needs_review = no "no" but some unknown.
    assert derived.probabilities["candidate"] == pytest.approx(0.9 * 0.8 * 0.7)
    assert derived.probabilities["not_eligible"] == pytest.approx(1 - 0.9 * 0.9 * 0.8)
    assert derived.probabilities["needs_review"] == pytest.approx(0.9 * 0.9 * 0.8 - 0.9 * 0.8 * 0.7)
    assert derived.label == "candidate"
    assert derived.evidence[0] == "table: else -> candidate"
    assert derived.provider.type == "derived"


def brute_force(question: Question, answers: dict[str, Answer]) -> dict[str, float]:
    spec = question.derive
    assert spec is not None
    totals = dict.fromkeys(question.criteria, 0.0)
    distributions = [list(answers[m].probabilities.items()) for m in spec.members]
    for combo in itertools.product(*distributions):
        weight = 1.0
        for _, p in combo:
            weight *= p
        chosen = {m: label for m, (label, _) in zip(spec.members, combo, strict=True)}
        for row in spec.table:
            if any(chosen[m] not in allowed for m, allowed in row.gates.items()):
                continue
            labels = [label for m, label in chosen.items() if row.applies_to(m)]
            if row.otherwise is not None or (
                (row.any is not None and any(label in row.labels for label in labels))
                or (row.all is not None and all(label in row.labels for label in labels))
            ):
                totals[row.result] += weight
                break
    return totals


def test_derived_probabilities_match_brute_force_enumeration() -> None:
    rng = random.Random(7)
    member_labels = ["yes", "no", "unknown"]
    for _ in range(200):
        n_members, n_rows = rng.randint(1, 4), rng.randint(1, 3)
        members = [f"m{i}" for i in range(n_members)]
        rows: list[dict[str, Any]] = []
        for _ in range(n_rows):
            # One label or a list of labels.
            matched = rng.sample(member_labels, rng.randint(1, 2))
            row: dict[str, Any] = {
                rng.choice(["any", "all"]): matched[0] if len(matched) == 1 else matched,
                "then": rng.choice(["a", "b", "c"]),
            }
            if rng.random() < 0.5:  # scope the row to a random non-empty subset of members
                row["in"] = rng.sample(members, rng.randint(1, n_members))
            if rng.random() < 0.5:  # gate the row on one or more members' answers
                gated = rng.sample(members, rng.randint(1, n_members))
                row["when"] = {m: rng.sample(member_labels, rng.randint(1, 2)) for m in gated}
            rows.append(row)
        question = Question.model_validate(
            {
                "instructions": "derived",
                "criteria": {"a": "A", "b": "B", "c": "C"},
                "derive": {
                    "from": members,
                    "table": [*rows, {"else": rng.choice(["a", "b", "c"])}],
                },
            }
        )
        answers = {}
        for member in members:
            weights = [rng.random() for _ in member_labels]
            total = sum(weights)
            answers[member] = answer(
                member, {label: w / total for label, w in zip(member_labels, weights, strict=True)}
            )
        derived = derive_answer("q", question, answers)
        expected = brute_force(question, answers)
        for label in question.criteria:
            assert derived.probabilities[label] == pytest.approx(expected[label], abs=1e-9)


def test_rows_can_be_scoped_to_members() -> None:
    # Criteria fail on `no`; note exclusions fail on `yes`: one table, rows scoped with `in`.
    question = Question.model_validate(
        {
            "instructions": "Eligibility.",
            "criteria": {"candidate": "c", "not_eligible": "n", "needs_review": "r"},
            "derive": {
                "from": ["hba1c", "note_pancreatitis"],
                "table": [
                    {"any": "no", "in": ["hba1c"], "then": "not_eligible"},
                    {"any": "yes", "in": ["note_pancreatitis"], "then": "not_eligible"},
                    {"any": "unknown", "then": "needs_review"},
                    {"else": "candidate"},
                ],
            },
        }
    )
    answers = {
        "hba1c": answer("hba1c", {"yes": 0.9, "no": 0.05, "unknown": 0.05}),
        "note_pancreatitis": answer("note_pancreatitis", {"yes": 0.2, "no": 0.8}),
    }
    derived = derive_answer("eligibility", question, answers)
    # not_eligible = P(hba1c no) + P(hba1c not no) * P(pancreatitis yes)
    assert derived.probabilities["not_eligible"] == pytest.approx(0.05 + 0.95 * 0.2)
    assert derived.probabilities["needs_review"] == pytest.approx(0.05 * 0.8)
    assert derived.probabilities["candidate"] == pytest.approx(0.9 * 0.8)
    assert derived.label == "candidate"  # a `no` note answer does not exclude anyone
    assert derived.evidence[0] == "table: else -> candidate"


def test_rows_can_depend_on_another_answer() -> None:
    # An ECG is required only when the referral is for cardiology.
    question = Question.model_validate(
        {
            "instructions": "Next step.",
            "criteria": {"return_for_info": "r", "schedule": "s"},
            "derive": {
                "from": ["specialty", "ecg_on_file"],
                "table": [
                    {
                        "any": "no",
                        "in": ["ecg_on_file"],
                        "when": {"specialty": ["cardiology"]},
                        "then": "return_for_info",
                    },
                    {"else": "schedule"},
                ],
            },
        }
    )
    specialty = answer("specialty", {"cardiology": 0.7, "dermatology": 0.3})
    no_ecg = answer("ecg_on_file", {"yes": 0.02, "no": 0.98})
    derived = derive_answer("next_step", question, {"specialty": specialty, "ecg_on_file": no_ecg})
    assert derived.probabilities["return_for_info"] == pytest.approx(0.7 * 0.98)
    assert derived.label == "return_for_info"
    assert derived.evidence[0] == (
        "table: any no in [ecg_on_file] when specialty in [cardiology] -> return_for_info"
    )
    assert explain(question, {"specialty": specialty, "ecg_on_file": no_ecg}) == [
        "return_for_info: ecg_on_file is no (specialty is cardiology)"
    ]
    skin = answer("specialty", {"cardiology": 0.1, "dermatology": 0.9})
    assert explain(question, {"specialty": skin, "ecg_on_file": no_ecg}) == []
    other = derive_answer("next_step", question, {"specialty": skin, "ecg_on_file": no_ecg})
    assert other.label == "schedule"


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"any": "no", "in": ["nope"], "then": "candidate"}, "not in `from`"),
        ({"else": "candidate", "in": ["inc_adult"]}, "takes no `in`"),
        ({"else": "candidate", "when": {"inc_adult": ["yes"]}}, "takes no `when`"),
        ({"any": "no", "when": {"inc_adult": []}, "then": "candidate"}, "one or more labels"),
        ({"any": "no", "when": {"nope": ["yes"]}, "then": "candidate"}, "not in `from`"),
    ],
)
def test_scoped_row_validation(row: dict[str, Any], message: str) -> None:
    raw = manifest_dict()
    raw["questions"]["eligibility"]["derive"]["table"].insert(0, row)
    with pytest.raises(ValidationError, match=message):
        PackManifest.model_validate(raw)


def test_rows_with_several_gates_and_label_lists() -> None:
    # Conservative therapy is required for a lumbar MRI, unless there is a red flag.
    question = Question.model_validate(
        {
            "instructions": "Next step.",
            "criteria": {"needs_attention": "n", "submit_ready": "s"},
            "derive": {
                "from": ["service", "red_flag", "conservative_therapy"],
                "table": [
                    {
                        "any": ["no", "unknown"],
                        "in": ["conservative_therapy"],
                        "when": {"service": ["lumbar_mri"], "red_flag": ["no"]},
                        "then": "needs_attention",
                    },
                    {"else": "submit_ready"},
                ],
            },
        }
    )
    service = answer("service", {"lumbar_mri": 0.9, "sleep_study": 0.1})
    no_flag = answer("red_flag", {"yes": 0.2, "no": 0.8})
    therapy = answer("conservative_therapy", {"yes": 0.1, "no": 0.3, "unknown": 0.6})
    members = {"service": service, "red_flag": no_flag, "conservative_therapy": therapy}
    derived = derive_answer("next_step", question, members)
    assert derived.probabilities["needs_attention"] == pytest.approx(0.9 * 0.8 * 0.9)
    assert derived.evidence[0] == (
        "table: any no|unknown in [conservative_therapy] when service in [lumbar_mri] "
        "and red_flag in [no] -> needs_attention"
    )
    assert explain(question, members) == [
        "needs_attention: conservative_therapy is unknown (service is lumbar_mri, red_flag is no)"
    ]
    flagged = answer("red_flag", {"yes": 0.9, "no": 0.1})
    assert explain(question, {**members, "red_flag": flagged}) == []  # a red flag bypasses it


def test_when_labels_and_members_are_checked() -> None:
    raw = manifest_dict()
    table = raw["questions"]["eligibility"]["derive"]["table"]
    table.insert(0, {"any": "no", "when": {"inc_t2d": ["maybe"]}, "then": "not_eligible"})
    with pytest.raises(ValidationError, match=r"`when` labels \['maybe'\] are not labels"):
        PackManifest.model_validate(raw)
    # Rows may gate on different members, and one row on several (all must hold).
    table[0] = {"any": "no", "when": {"inc_t2d": ["yes"]}, "then": "not_eligible"}
    both = {"inc_t2d": ["yes"], "not_pregnant": ["yes", "unknown"]}
    table.insert(0, {"any": ["no", "unknown"], "when": both, "then": "not_eligible"})
    PackManifest.model_validate(raw)
    table[0]["any"] = ["no", "maybe"]
    with pytest.raises(ValidationError, match="row label 'maybe' is not a label"):
        PackManifest.model_validate(raw)


def test_scoped_row_labels_must_belong_to_their_members() -> None:
    raw = manifest_dict()
    table = raw["questions"]["eligibility"]["derive"]["table"]
    table.insert(0, {"any": "unknown", "in": ["inc_adult"], "then": "needs_review"})  # boolean
    with pytest.raises(ValidationError, match="not a label of its members"):
        PackManifest.model_validate(raw)


# -- runtime, policy, evals --------------------------------------------------------------------


def test_fixture_pack_runs_end_to_end() -> None:
    decision = Decision.from_path(TRIAL_MINI)
    result = decision({"summary": "age 54, type 2 diabetes, male"})
    assert result.primary == "eligibility"
    assert result.answers["eligibility"].label == "candidate"
    assert result.outcome is Outcome.HUMAN_REVIEW  # risk level 2 caps AUTO
    assert result.answers["severity"].value == pytest.approx(
        decision.pack.manifest.questions["severity"].expected_score(
            result.answers["severity"].probabilities
        )
    )
    assert "value" in result.to_dict()["answers"]["severity"]


def test_missing_member_makes_the_derived_answer_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = Decision.from_path(TRIAL_MINI)
    from mendari.errors import ProviderError

    def broken(_inputs: Any) -> Any:
        raise ProviderError("provider_unavailable")

    monkeypatch.setattr(decision.providers["screen"], "answer", broken)
    result = decision({"summary": "age 54"})
    assert result.errors["eligibility"] == "derived_input_unavailable"
    assert result.outcome is Outcome.ABSTAIN


def test_derived_score_questions_get_expected_values() -> None:
    raw = manifest_dict()
    raw["questions"]["eligibility"] = {
        "type": "score",
        "instructions": "Eligibility score.",
        "levels": {
            "low": {"score": 0, "criterion": "Excluded."},
            "high": {"score": 1, "criterion": "Candidate."},
        },
        "derive": {
            "from": ["inc_adult", "inc_t2d", "not_pregnant"],
            "table": [{"all": "yes", "then": "high"}, {"else": "low"}],
        },
    }
    raw["policy"]["labels"] = {}
    manifest = PackManifest.model_validate(raw)
    answers = {
        "inc_adult": answer("inc_adult", {"yes": 0.5, "no": 0.5}),
        "inc_t2d": answer("inc_t2d", {"yes": 1.0}),
        "not_pregnant": answer("not_pregnant", {"yes": 1.0}),
    }
    complete_answers(manifest, answers, {})
    assert answers["eligibility"].value == pytest.approx(0.5)


def test_score_conditions() -> None:
    severe = Answer("severity", "severe", 0.9, {"mild": 0.05, "severe": 0.95}, INFO, value=2.9)
    assert matches(Condition(question="severity", min_score=2.5), severe)
    assert not matches(Condition(question="severity", max_score=2.0), severe)
    assert not matches(
        Condition(question="severity", min_score=2.5), answer("severity", {"mild": 1.0})
    )
    with pytest.raises(ValidationError, match="needs `labels`"):
        Condition(question="severity")


def test_evals_cover_derived_questions() -> None:
    decision = Decision.from_path(TRIAL_MINI)
    report = evaluate(decision, resolve_dataset(decision.pack, "sample"))
    assert report["questions"]["eligibility"]["accuracy"] == 1.0
    assert report["questions"]["eligibility"]["n"] == 5


def test_spec_01_manifests_still_load() -> None:
    raw = yaml.safe_load((INBOX_DIR / "pack.yaml").read_text())
    raw["spec_version"] = "0.1"
    raw["policy"] = yaml.safe_load((INBOX_DIR / "policy.yaml").read_text())
    rule = raw["policy"]["rules"][0]["when"]
    rule["min_confidence"] = rule.pop("min_probability")  # the 0.1 shape of the rule
    raw["card"].pop("provenance")  # 0.2 fields
    for question in raw["questions"].values():
        question.pop("display", None)
        question.pop("names", None)
    manifest = PackManifest.model_validate(raw)
    assert manifest.spec_version == "0.1" and manifest.derived == {}


def test_decision_card_shows_types_levels_and_tables() -> None:
    card = render_card(load_pack(TRIAL_MINI))
    assert "**`inc_adult`** *(boolean)*" in card
    assert "| `severe` | 3 | Uncontrolled or complicated. |" in card
    assert "1. any member is `no` → `not_eligible`" in card
    assert "3. otherwise → `candidate`" in card
    assert "Rule **severe-disease**: `severity` expected score ≥ 2.5 ⇒ `HUMAN_REVIEW`" in card


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


def test_inbox_escalates_at_the_documented_threshold(inbox_pack: Any) -> None:
    from mendari.core.policy import apply_policy

    answers = {
        "destination": answer("destination", {"medical_assistant": 0.97, "nurse": 0.03}),
        "urgency": answer("urgency", {"urgent": 0.40, "routine": 0.60}),
    }
    result = apply_policy(inbox_pack.manifest, answers, {}, None)
    assert (result.outcome, result.priority.value, result.rule) == (
        Outcome.HUMAN_REVIEW,
        "stat",
        "possible-emergency",
    )
    card = render_card(inbox_pack)
    assert "P(`urgency` is `urgent`) ≥ 0.35" in card


def test_spec_01_packs_cannot_use_min_probability() -> None:
    raw = yaml.safe_load((INBOX_DIR / "pack.yaml").read_text())
    raw["spec_version"] = "0.1"
    raw["policy"] = yaml.safe_load((INBOX_DIR / "policy.yaml").read_text())
    with pytest.raises(ValidationError, match="rule possible-emergency"):
        PackManifest.model_validate(raw)


def test_decisions_list_the_criteria_that_decided() -> None:
    decision = Decision.from_path(TRIAL_MINI)
    excluded = decision({"summary": "age 34, type 2 diabetes, pregnant"})
    assert "not_eligible: not_pregnant is no" in excluded.reasons
    open_criteria = decision({"summary": "age 61, type 2 diabetes"})  # pregnancy not stated
    assert "needs_review: not_pregnant is unknown" in open_criteria.reasons
    both = decision({"summary": "age 15, type 1 diabetes"})
    assert "not_eligible: inc_adult, inc_t2d are no" in both.reasons
    assert "needs_review: not_pregnant is unknown" in both.reasons  # every holding row is listed
    candidate = decision({"summary": "age 54, type 2 diabetes, male"})
    assert not any(
        reason.startswith(("not_eligible", "needs_review")) for reason in candidate.reasons
    )


# -- licence and code systems (LP-14) -----------------------------------------------------------


@pytest.mark.parametrize(
    "licence",
    ["Apache-2.0", "MIT OR Apache-2.0", "(MIT AND BSD-3-Clause)", "LicenseRef-site-terms"],
)
def test_a_licence_is_an_spdx_expression(licence: str) -> None:
    PackManifest.model_validate(manifest_dict() | {"license": licence})


@pytest.mark.parametrize("licence", ["Apache 2.0", "apache license", "MIT OR", "(MIT", "", "AND"])
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
    shutil.copytree(TRIAL_MINI, root)
    data = root / "logic" / "codes.json"
    data.write_text('{"note": "coded as urn:oid:2.16.840.1.113883.6.96"}\n')
    with pytest.raises(Exception, match=r"logic/codes\.json names SNOMED CT .* declare it"):
        load_pack(root)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["code_systems"] = [
        {"url": "http://snomed.info/sct", "name": "SNOMED CT", "licence_note": "Member country."}
    ]
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    card = render_card(load_pack(root))
    assert "- SNOMED CT (`http://snomed.info/sct`). Licence: Member country." in card
