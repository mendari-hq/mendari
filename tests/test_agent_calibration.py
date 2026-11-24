# SPDX-License-Identifier: Apache-2.0
"""An external agent's confidence, calibrated (ADR-0024, `providers/agent_calibration.py`): the
fit against scipy and statsmodels, that it never changes which answer wins, `mendari calibrate`
and what its file records, the calibration on every path a decision is made by, the version cap,
and what the evaluation and the Decision Card say."""

from __future__ import annotations

import json
import math
import random
import shutil
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import statsmodels.api as sm
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy.optimize import minimize_scalar

from mendari import AuditStore, Decision, load_pack
from mendari.cli import main
from mendari.core.card import render_card
from mendari.core.policy import GovernanceOverride
from mendari.core.spec import Outcome
from mendari.errors import MendariError, PackValidationError
from mendari.evals.harness import evaluation, load_dataset
from mendari.evals.render import report_text
from mendari.mcp import McpServer
from mendari.providers import ProviderInfo, ProviderOutput, build_provider
from mendari.providers import agent_calibration as fitted
from mendari.providers.agent import CANNED, ENDPOINT_ENV, AgentConfig, AgentProvider
from mendari.providers.base import Answer
from mendari.providers.calibration import (
    fit_temperature,
    log_probabilities,
    nll,
    rescale,
    temperature_grid,
)
from mendari.server.app import create_app
from tests.conftest import INBOX_DIR
from tests.support.confidence_agents import (
    AGENT_ID,
    LABELS,
    REFILL,
    VERSION,
    Simulated,
    answer,
    calibrated_agent,
    calibration_file,
    edit_manifest,
    right_probability,
    scaffold,
    serve,
    variant,
)
from tests.test_cds_cards import inbox_request
from tests.test_fhir import BUNDLE

# One step of the search grid: the fitted temperature is within it of the best one.
GRID_STEP = temperature_grid()[1] / temperature_grid()[0] - 1
CHANGED = "the agent changed since its confidence was calibrated (was 2.1.0, is 2.2.0)"


def implied(confidence: float, top: str, labels: tuple[str, ...]) -> dict[str, float]:
    """The distribution the provider gives an answer that states only its confidence."""
    share = (1 - confidence) / (len(labels) - 1)
    return {label: confidence if label == top else share for label in labels}


# -- the fit, against scipy and statsmodels --------------------------------------------------------


def test_the_temperature_of_a_reported_distribution_is_scipys() -> None:
    """Temperature scaling of a label distribution: the temperature that minimises the true
    label's negative log-likelihood, as scipy finds it."""
    rng = random.Random(7)
    scored = []
    for _ in range(600):
        weights = [rng.random() ** 3 for _ in LABELS]
        stated = {label: w / sum(weights) for label, w in zip(LABELS, weights, strict=True)}
        truth = rng.choices(LABELS, weights=list(rescale(stated, 2.0).values()))[0]
        scored.append((log_probabilities(stated), truth))
    temperature, before, after = fit_temperature(scored)
    best = minimize_scalar(lambda t: nll(scored, t), bounds=(0.25, 200.0), method="bounded")
    assert temperature == pytest.approx(best.x, rel=GRID_STEP)
    assert after == pytest.approx(best.fun, abs=1e-4) and after < before
    assert temperature == pytest.approx(2.0, rel=0.2)  # what the answers were drawn with


def test_the_temperature_of_a_two_label_confidence_is_statsmodels_logit() -> None:
    """An answer that states only its confidence, on a yes/no question: the fit is a logistic
    regression of "was it right" on the log-odds of the confidence, with no intercept, and the
    temperature is one over its coefficient."""
    rng = random.Random(11)
    labels = ("yes", "no")
    scored, log_odds, right = [], [], []
    for _ in range(800):
        confidence = rng.uniform(0.55, 0.995)
        top = rng.choice(labels)
        correct = rng.random() < right_probability(confidence, 3.0, labels=2)
        truth = top if correct else next(label for label in labels if label != top)
        scored.append((log_probabilities(implied(confidence, top, labels)), truth))
        log_odds.append(math.log(confidence / (1 - confidence)))
        right.append(float(correct))
    temperature, _, _ = fit_temperature(scored)
    logit = sm.Logit(np.array(right), np.array(log_odds)).fit(disp=0)
    assert temperature == pytest.approx(1 / logit.params[0], rel=GRID_STEP)


def test_the_temperature_of_a_confidence_over_three_labels_is_a_logistic_regression() -> None:
    """With more than two labels the log-odds are those of the answer against each other label,
    log(c (K - 1) / (1 - c)), and the fit is the same regression with the offset -log(K - 1):
    fitting the true label's likelihood and fitting "was it right" give one temperature."""
    rng = random.Random(13)
    scored, log_odds, right = [], [], []
    for _ in range(800):
        confidence = rng.uniform(0.4, 0.995)
        top = rng.choice(LABELS)
        correct = rng.random() < right_probability(confidence, 2.5)
        truth = top if correct else rng.choice([label for label in LABELS if label != top])
        scored.append((log_probabilities(implied(confidence, top, LABELS)), truth))
        log_odds.append(math.log(confidence * (len(LABELS) - 1) / (1 - confidence)))
        right.append(float(correct))
    temperature, _, _ = fit_temperature(scored)
    offset = np.full(len(right), -math.log(len(LABELS) - 1))
    model = sm.GLM(
        np.array(right), np.array(log_odds), family=sm.families.Binomial(), offset=offset
    )
    assert temperature == pytest.approx(1 / model.fit().params[0], rel=GRID_STEP)


# -- it never changes which answer wins ------------------------------------------------------------

PROBABILITY = st.floats(min_value=1e-6, max_value=1.0, allow_nan=False)
TEMPERATURE = st.floats(min_value=0.25, max_value=200.0, allow_nan=False)


@cache
def inbox_manifest() -> Any:
    return load_pack(INBOX_DIR).manifest  # any question with five labels will do


@settings(max_examples=300, deadline=None)
@given(st.lists(PROBABILITY, min_size=2, max_size=6), TEMPERATURE)
def test_a_temperature_keeps_the_order_of_the_labels(weights: list[float], t: float) -> None:
    total = sum(weights)
    stated = {f"l{i}": w / total for i, w in enumerate(weights)}
    scaled = rescale(stated, t)
    assert sum(scaled.values()) == pytest.approx(1.0)
    for one in stated:
        for other in stated:
            if stated[one] > stated[other]:
                assert scaled[one] >= scaled[other]
            if stated[one] == stated[other]:
                assert scaled[one] == pytest.approx(scaled[other])


@settings(max_examples=300, deadline=None)
@given(st.lists(PROBABILITY, min_size=2, max_size=3), TEMPERATURE)
def test_a_calibrated_answer_is_the_answer_the_agent_gave(weights: list[float], t: float) -> None:
    """Whatever the temperature and whatever the agent reported: the label is the agent's, its
    confidence is its calibrated probability, and no other label has more."""
    manifest = inbox_manifest()
    labels = manifest.labels("destination")[: len(weights)]
    total = sum(weights)
    stated = dict(zip(labels, (w / total for w in weights), strict=True))
    top = max(stated, key=lambda label: stated[label])
    info = ProviderInfo("router", "agent", "agent")
    said = Answer("destination", top, stated[top], stated, info)
    output = ProviderOutput(answers={"destination": said}, extra={"agent_version": VERSION})
    calibration = fitted.Calibration(
        AGENT_ID, VERSION, None, {"destination": t}, {}, {}, "calibration", None, 400, "2026-11-24"
    )
    questions = {"destination": manifest.questions["destination"]}
    found = fitted.calibrated(calibration, output, questions, AGENT_ID).answers["destination"]
    assert found.label == top
    assert found.confidence == found.probabilities[top] == max(found.probabilities.values())
    assert set(found.probabilities) == set(stated)


@settings(max_examples=200, deadline=None)
@given(st.floats(min_value=0.34, max_value=1.0), TEMPERATURE)
def test_a_confidence_alone_never_falls_under_another_labels_share(c: float, t: float) -> None:
    """The log-odds are taken against the other labels, so however a confidence is rescaled it
    stays the largest probability (the plain log-odds of 0.4 over three labels would not)."""
    scaled = rescale(implied(c, "refill", LABELS), t)
    assert scaled["refill"] >= max(scaled["appointment"], scaled["other"])
    assert scaled["refill"] >= 1 / len(LABELS) - 1e-12


# -- `fit`, through the agent's own provider -------------------------------------------------------


def a_fit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: Any, n: int = 400) -> Any:
    root = scaffold(tmp_path)
    simulated = service if isinstance(service, Simulated) else Simulated()
    records = simulated.cases("cal", n, seed=1)
    serve(monkeypatch, service)
    provider = build_provider(load_pack(root), "reader", "agent")
    assert isinstance(provider, AgentProvider)
    return fitted.fit(provider, records, dataset="calibration", sha256="ab" * 32, date="2026-11-24")


def test_an_agent_is_fitted_on_its_own_confidence_for_the_version_it_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    simulated = Simulated(temperature=2.5)
    found = a_fit(tmp_path, monkeypatch, simulated)
    assert found["format"] == "mendari.agent-calibration/1"
    assert found["agent"] == {"id": AGENT_ID, "version": VERSION, "model": None}
    assert found["fitted_on"] == {
        "dataset": "calibration",
        "sha256": "ab" * 32,
        "cases": 400,
        "date": "2026-11-24",
    }
    # The temperature is the fit of the same answers, and near the one they were drawn with.
    scored = [
        (log_probabilities(implied(conf, said, LABELS)), truth)
        for said, conf, truth, _ in simulated.table.values()
    ]
    assert found["temperatures"]["request"] == round(fit_temperature(scored)[0], 4)
    assert found["temperatures"]["request"] == pytest.approx(2.5, rel=0.25)
    figures = found["questions"]["request"]
    assert figures["cases"] == 400 and figures["nll_after"] < figures["nll_before"]
    assert figures["ece_after"] < figures["ece_before"]
    # Right on every case, the other question has no temperature: it is left as reported.
    assert "urgent_symptoms" not in found["temperatures"]
    assert "place no temperature" in found["not_fitted"]["urgent_symptoms"]
    assert found["failures"] == {} and found["cost_usd"] == pytest.approx(0.4)


def test_a_question_with_too_few_cases_is_not_fitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = a_fit(tmp_path, monkeypatch, Simulated(), n=fitted.MIN_CASES - 1)
    assert found["temperatures"] == {}
    assert found["not_fitted"]["request"] == "99 labelled cases answered; 100 needed"


def test_canned_answers_are_not_fitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    provider = build_provider(load_pack(scaffold(tmp_path)), "reader", "agent")
    assert isinstance(provider, AgentProvider)
    with pytest.raises(MendariError, match="canned examples"):
        fitted.fit(provider, Simulated().cases("cal", 200, 1), dataset="c", sha256="", date="d")


def test_a_service_that_reports_no_version_is_not_fitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(MendariError, match="reports no agent_version"):
        a_fit(tmp_path, monkeypatch, lambda body: answer(version=None))


def test_a_service_that_changes_version_while_asked_is_not_fitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    versions = iter(["2.1.0", "2.2.0"] * 200)
    with pytest.raises(MendariError, match=r"more than one version .*2\.1\.0, 2\.2\.0"):
        a_fit(tmp_path, monkeypatch, lambda body: answer(version=next(versions)))


# -- `mendari calibrate` ---------------------------------------------------------------------------


def a_calibrated_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **more: Any
) -> tuple[Path, Simulated]:
    """An agent whose service over-states its confidence, calibrated by the command on its
    `calibration` set and then told to use the file; it also has a `test` set of 600 cases."""
    root = scaffold(tmp_path)
    simulated = Simulated(temperature=2.5, **more)
    simulated.write(root, "calibration", 400, seed=1)
    simulated.write(root, "test", 600, seed=2)
    serve(monkeypatch, simulated)
    arguments = ["calibrate", str(root), "--slot", "reader", "--variant", "agent", "--limit", "0"]
    assert main(arguments) == 0
    relative = "models/reader-agent-calibration.json"

    def use_it(manifest: dict[str, Any]) -> None:
        variant(manifest)["calibration"] = relative

    edit_manifest(root, use_it)
    return root, simulated


def test_mendari_calibrate_writes_the_agents_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = a_calibrated_run(tmp_path, monkeypatch)
    out, err = capsys.readouterr()
    assert "asking starter-portal-agent about 400 calibration cases..." in out
    assert "fitted for starter-portal-agent version 2.1.0" in out
    assert "add `calibration: models/reader-agent-calibration.json` to the reader/agent" in out
    assert "note:" not in err  # `calibration` is a fitting set
    written = json.loads((root / "models" / "reader-agent-calibration.json").read_text())
    dataset = load_dataset(root / "data" / "calibration.jsonl")
    assert written["fitted_on"]["dataset"] == "calibration"
    assert written["fitted_on"]["sha256"] == dataset.sha256
    assert written["fitted_on"]["cases"] == 400
    assert written["agent"]["version"] == VERSION
    # The file is one of the agent's: its digest covers it, so a signature does.
    before = load_pack(root).digest
    (root / "models" / "reader-agent-calibration.json").write_text(
        json.dumps({**written, "temperatures": {"request": 1.0}})
    )
    assert load_pack(root).digest != before


def test_mendari_calibrate_says_when_it_is_given_an_evaluation_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fit is honest only on cases an evaluation does not report on: fitting on one of the
    agent's own evaluation sets is said, for an `agent` variant as for any other."""
    root, _ = a_calibrated_run(tmp_path, monkeypatch)
    capsys.readouterr()
    on_test = ["calibrate", str(root), "--slot", "reader", "--variant", "agent"]
    assert main([*on_test, "--dataset", "test", "--limit", "0"]) == 0
    _, err = capsys.readouterr()
    assert err.startswith("note: test is one of this agent's evaluation sets.")


def test_mendari_calibrate_refuses_canned_answers_and_other_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = scaffold(tmp_path)
    Simulated().write(root, "calibration", 200, seed=1)
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    assert main(["calibrate", str(root), "--slot", "reader", "--variant", "agent"]) == 1
    assert "answers from its canned examples" in capsys.readouterr().err
    assert main(["calibrate", str(root), "--slot", "reader", "--variant", "rules"]) == 1
    assert "is not an llm or an agent variant" in capsys.readouterr().err
    assert not (root / "models").exists()  # nothing was written


def test_nothing_is_written_when_no_question_has_the_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = scaffold(tmp_path)
    simulated = Simulated()
    simulated.write(root, "calibration", 60, seed=1)
    serve(monkeypatch, simulated)
    assert main(["calibrate", str(root), "--slot", "reader", "--variant", "agent"]) == 1
    assert "nothing was fitted, so nothing was written" in capsys.readouterr().err
    assert not (root / "models").exists()


# -- the manifest ----------------------------------------------------------------------------------


def test_an_agents_calibration_needs_spec_0_4(tmp_path: Path) -> None:
    root = calibrated_agent(tmp_path, {"request": 2.0})
    assert load_pack(root).manifest.spec_version == "0.4"

    def earlier(manifest: dict[str, Any]) -> None:
        manifest["spec_version"] = "0.3"

    edit_manifest(root, earlier)
    with pytest.raises(PackValidationError) as refused:
        load_pack(root)
    assert (
        "['providers.reader.variants.agent.calibration'] use spec 0.4 features; "
        "set spec_version: '0.4'"
    ) in str(refused.value)


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ({"format": "mendari.llm-calibration/1"}, "not an agent calibration file"),
        ({"agent": {"id": AGENT_ID}}, "does not say which agent version it is for"),
        ({"temperatures": {"request": 0}}, "temperatures are not positive numbers"),
        ({"temperatures": {"request": "hot"}}, "temperatures are not positive numbers"),
        ({"fitted_on": {"dataset": "calibration"}}, "does not say what it was fitted on"),
    ],
)
def test_a_file_that_is_not_a_calibration_is_refused(
    tmp_path: Path, content: dict[str, Any], problem: str
) -> None:
    root = calibrated_agent(tmp_path, {"request": 2.0})
    path = root / "models" / "reader-agent-calibration.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **content}))
    pack = load_pack(root)  # the deployment's own agent loads, and says so
    assert any(problem in warning for warning in pack.warnings)
    with pytest.raises(PackValidationError, match=problem):
        build_provider(pack, "reader", "agent")


def test_no_key_of_a_manifest_switches_the_version_cap_off(tmp_path: Path) -> None:
    """The variant's config is closed: a key that would name the cap is not one it reads, and
    a variant that sets one does not run."""
    root = calibrated_agent(tmp_path, {"request": 2.0}, version_cap=False)
    with pytest.raises(PackValidationError, match="version_cap"):
        build_provider(load_pack(root), "reader", "agent")
    assert AgentConfig.model_config["extra"] == "forbid"
    about_it = [name for name in AgentConfig.model_fields if "calibrat" in name or "cap" in name]
    assert about_it == ["calibration"]  # the file, and nothing that says how it is enforced


# -- at decision time ------------------------------------------------------------------------------


def decide(root: Path, **kwargs: Any) -> Any:
    return Decision(load_pack(root), **kwargs)(REFILL)


def test_the_policy_sees_the_calibrated_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = calibrated_agent(tmp_path, {"request": 2.0})
    serve(monkeypatch, [answer("refill", 0.97)])
    result = decide(root)
    expected = rescale(implied(0.97, "refill", LABELS), 2.0)
    said = result.answers["request"]
    assert said.label == "refill" and said.confidence == pytest.approx(expected["refill"])
    assert said.probabilities == pytest.approx(expected)
    assert said.evidence == ("calibrated T=2.00",)
    # 0.97 would have been automated; what it is worth, 0.80, is not.
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.reasons == ("confidence 0.80 is below the auto threshold 0.90 for 'refill'",)
    # The other question has no temperature and is left as reported.
    assert result.answers["urgent_symptoms"].confidence == pytest.approx(0.99)
    # What the agent reported is kept in memory, for an evaluation's "before".
    assert result.signals.reported == {"request": pytest.approx(implied(0.97, "refill", LABELS))}
    assert result.signals.caps == ()


def test_a_reported_distribution_is_temperature_scaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = calibrated_agent(tmp_path, {"request": 0.5})
    stated = {"refill": 0.7, "appointment": 0.2, "other": 0.1}
    serve(monkeypatch, [answer("refill", probabilities=stated)])
    said = decide(root).answers["request"]
    assert said.probabilities == pytest.approx(rescale(stated, 0.5))
    assert said.confidence == pytest.approx(0.49 / (0.49 + 0.04 + 0.01))  # sharpened: T < 1


def test_the_same_version_is_not_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = calibrated_agent(tmp_path, {"request": 1.2})
    serve(monkeypatch, [answer("refill", 0.99)])
    result = decide(root)
    assert result.outcome is Outcome.AUTO
    assert all("calibrated" not in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("version", "now"), [("2.2.0", "2.2.0"), (None, "not reported")], ids=["another", "none"]
)
def test_another_version_is_capped_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str | None, now: str
) -> None:
    """A calibration fitted for one version does not bless another: nothing is rescaled, and
    what the policy would have automated goes to a person, with why."""
    root = calibrated_agent(tmp_path, {"request": 1.2})
    serve(monkeypatch, [answer("refill", 0.99, version=version)])
    result = decide(root)
    reason = f"the agent changed since its confidence was calibrated (was 2.1.0, is {now})"
    assert result.outcome is Outcome.HUMAN_REVIEW and result.recommendation == "refill"
    assert result.reasons == (
        "confidence 0.99 >= auto threshold 0.90",
        f"{reason}; a person decides",
    )
    said = result.answers["request"]
    assert said.confidence == pytest.approx(0.99)  # as reported: not rescaled
    assert said.evidence == (f"not calibrated: {reason}",)
    assert result.to_dict()["reasons"][-1] == f"{reason}; a person decides"  # in the record


def test_another_agent_id_is_capped_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = calibrated_agent(tmp_path, {"request": 1.2}, agent_id="another-agent")
    serve(monkeypatch, [answer("refill", 0.99)])
    result = decide(root)
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.reasons[-1] == (
        "the agent changed since its confidence was calibrated "
        "(was starter-portal-agent 2.1.0, is another-agent 2.1.0); a person decides"
    )


def test_nothing_lifts_the_version_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a governance approval, and not a risk level that needs none: the cap is the last
    word, as a hold is."""
    root = calibrated_agent(tmp_path, {"request": 1.2})
    serve(monkeypatch, [answer("refill", 0.99, version="2.2.0")])
    approved = GovernanceOverride(approved_by="Dr. Synthetic", reference="GOV-1")
    for kwargs in ({}, {"governance": approved}, {"origin_cap": False}):
        result = decide(root, **kwargs)
        assert result.outcome is Outcome.HUMAN_REVIEW
        assert result.reasons[-1] == f"{CHANGED}; a person decides"


@pytest.mark.parametrize(
    ("respond", "outcome"),
    [
        (answer("refill", 0.6, version="2.2.0"), Outcome.HUMAN_REVIEW),  # under the threshold
        (answer("refill", 0.4, version="2.2.0"), Outcome.ABSTAIN),  # under the review threshold
        (answer("other", 0.99, version="2.2.0"), Outcome.HUMAN_REVIEW),  # never automated
        (answer("refill", 0.99, urgent="yes", version="2.2.0"), Outcome.HUMAN_REVIEW),  # a rule
    ],
)
def test_the_version_cap_changes_no_outcome_that_is_not_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, respond: dict[str, Any], outcome: Outcome
) -> None:
    """It never raises automation: a decision that was going to a person still does, with the
    same recommendation and priority, and the same reason added for whoever reads it."""
    capped = calibrated_agent(tmp_path, {"request": 1.0})
    plain = scaffold(tmp_path, "plain")
    serve(monkeypatch, [respond])
    with_cap, without = decide(capped), decide(plain)
    assert with_cap.outcome is without.outcome is outcome
    assert with_cap.recommendation == without.recommendation
    assert with_cap.priority == without.priority and with_cap.rule == without.rule
    assert with_cap.reasons == (*without.reasons, f"{CHANGED}; a person decides")


def test_canned_answers_are_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Canned answers are not the agent's confidence: nothing is rescaled, and nothing is
    capped for a version the canned file happens to name."""
    root = calibrated_agent(tmp_path, {"request": 5.0, "urgent_symptoms": 5.0})
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    plain = scaffold(tmp_path, "plain")
    result, as_it_was = decide(root), decide(plain)
    assert result.outcome is as_it_was.outcome
    assert result.confidence == as_it_was.confidence and result.reasons == as_it_was.reasons
    assert result.signals.reported == {} and result.signals.caps == ()
    on_a_server = decide(root, canned_cap=True)
    assert on_a_server.reasons[-1].startswith(CANNED)


# -- on every path a decision is made by -----------------------------------------------------------

DESTINATIONS = ("front_desk", "medical_assistant", "nurse", "physician", "billing")
CALIBRATED_AT = 0.97


def inbox_with_an_agent(pack_copy: Callable[..., Path]) -> Path:
    """The inbox fixture (JSON, FHIR and a CDS hook), its router an `agent` variant with a
    calibration: every answer to `destination` is stated at 0.97 and worth less."""

    def manifest(data: dict[str, Any]) -> None:
        data["spec_version"] = "0.4"
        for left_out in ("red_flags", "red_flags_holdout"):  # the copy holds three small sets
            del data["evaluation"]["datasets"][left_out]
        data["providers"]["router"]["use"] = "agent"
        data["providers"]["router"]["variants"]["agent"] = {
            "type": "agent",
            "agent_id": AGENT_ID,
            "state_fields": ["message.subject", "message.body"],
            "calibration": "models/router-agent-calibration.json",
        }

    def policy(data: dict[str, Any]) -> None:
        data.pop("automation_requires")

    root = pack_copy(manifest, policy)
    calibration_file(root, {"destination": 2.0}, relative="models/router-agent-calibration.json")
    return root


def routed(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "answers": {"destination": {"value": "medical_assistant", "confidence": CALIBRATED_AT}},
        "agent_version": VERSION,
    }


def is_calibrated(said: dict[str, Any]) -> None:
    expected = rescale(implied(CALIBRATED_AT, "medical_assistant", DESTINATIONS), 2.0)
    assert said["label"] == "medical_assistant"
    assert said["confidence"] == pytest.approx(expected["medical_assistant"], abs=1e-6)
    if "evidence" in said:  # an MCP tool's answer is the label and its confidence
        assert "calibrated T=2.00" in said["evidence"]


def test_the_calibration_is_applied_on_every_path(
    pack_copy: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision made in Python, over REST, from FHIR, by a CDS hook, by an MCP tool and by an
    evaluation all ask the same provider: each sees the calibrated confidence, never 0.97."""
    root = inbox_with_an_agent(pack_copy)
    serve(monkeypatch, routed)
    routine = {"message": {"body": "A synthetic question about a refill."}}
    pack = load_pack(root)
    # In Python, on JSON and on FHIR.
    is_calibrated(Decision(pack)(routine).to_dict()["answers"]["destination"])
    is_calibrated(Decision(pack).decide_fhir(BUNDLE)[0].to_dict()["answers"]["destination"])
    # The server: REST, FHIR, and a CDS hook (its decision is in the audit trail).
    agents = tmp_path / "served"
    agents.mkdir()
    shutil.copytree(root, agents / "patient-inbox-router")
    db = tmp_path / "served.db"
    client = TestClient(create_app(db_path=db, packs_paths=[agents]))
    route = "/api/agents/mendari/patient-inbox-router"
    is_calibrated(client.post(f"{route}/decide", json=routine).json()["answers"]["destination"])
    over_fhir = client.post(f"{route}/fhir/decide", json=BUNDLE).json()
    is_calibrated(over_fhir["decision"]["answers"]["destination"])
    hook = client.post("/cds-services/mendari.patient-inbox-router", json=inbox_request())
    assert hook.status_code == 200
    (card,) = hook.json()["cards"]
    with AuditStore(db) as store:
        records = store.query(pack_id=pack.id)
        assert len(records) == 3 and card["uuid"] in {record["id"] for record in records}
        for record in records:
            kept = store.get(record["id"])
            assert kept is not None
            is_calibrated(kept["record"]["answers"]["destination"])
    # An MCP tool.
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(agents))
    with AuditStore(":memory:") as store:
        params = {"name": "decide", "arguments": {"agent": pack.id, "input": routine}}
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        answered = McpServer(store).handle(call)
        assert answered is not None
        is_calibrated(answered["result"]["structuredContent"]["answers"]["destination"])
    # An evaluation: its ledger holds the calibrated confidence.
    run = evaluation(Decision(pack, origin_cap=False), load_dataset(root / "data" / "test.jsonl"))
    for case in run.ledger.cases:
        _, predicted, confidence, _, _, _ = case["q"]["destination"]
        assert predicted == "medical_assistant" and confidence < 0.9


# -- what the evaluation says ----------------------------------------------------------------------


def test_the_evaluation_reports_the_calibration_before_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, simulated = a_calibrated_run(tmp_path, monkeypatch)
    pack = load_pack(root)
    dataset = load_dataset(root / "data" / "test.jsonl")
    run = evaluation(Decision(pack, origin_cap=False), dataset)
    block = run.report["run"]["calibration"]
    assert block["format"] == "mendari.calibration-report/1" and block["cases"] == 600
    (entry,) = block["variants"]
    assert (entry["slot"], entry["variant"], entry["status"]) == ("reader", "agent", "calibrated")
    assert entry["fitted"]["cases"] == 400 and entry["fitted"]["dataset"] == "calibration"
    assert entry["fitted"]["agent"] == {"id": AGENT_ID, "version": VERSION}
    assert entry["same_cases"] is False
    request = entry["questions"]["request"]
    assert request["n"] == 600 and request["rescaled"] == 600
    # Before: what the agent stated, measured here on the table the service answers from.
    stated = [(conf, said == truth) for said, conf, truth, _ in simulated.table.values()][400:]
    gap = abs(sum(c for c, _ in stated) / 600 - sum(ok for _, ok in stated) / 600)
    assert request["before"]["ece"] >= round(gap, 4) - 1e-4 > 0.2
    assert request["after"]["ece"] < 0.1 < request["before"]["ece"]
    for when in ("before", "after"):
        assert request[when]["equal_mass_ece"] is not None
        assert request[when]["brier_reliability"] is not None
        assert sum(row["n"] for row in request[when]["bins"]) == 600
    assert request["after"]["brier_reliability"] < request["before"]["brier_reliability"]
    # After is what the report's own calibration row says: the policy saw it.
    assert request["after"]["ece"] == pytest.approx(
        run.report["questions"]["request"]["calibration"]["ece"], abs=1e-4
    )
    # The question with no temperature has one column.
    assert "before" not in entry["questions"]["urgent_symptoms"]
    assert block["agent_changed"] == 0
    # The block is the run's, never the result's: no stored or hashed format changes.
    assert "calibration" not in {k for k in run.report if k != "run"}
    said = report_text(run.report)
    assert (
        "Confidence".ljust(26) + "reader/agent: calibrated on 400 cases of calibration "
        "(2026-11-24) for starter-portal-agent version 2.1.0"
    ) in said
    lines = block["lines"]
    assert lines[0].startswith("reader/agent: calibrated on 400 cases of calibration")
    assert any(line.startswith("  request (n=600): ECE 0.") and " -> 0." in line for line in lines)
    assert any("before: cases" in line and "after: cases" in line for line in lines)


def test_an_evaluation_on_the_fitting_cases_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = a_calibrated_run(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["eval", str(root), "--dataset", "calibration", "--baseline", "none"]) == 0
    out, _ = capsys.readouterr()
    assert "these are the cases it was fitted on, so the figures after it are optimistic" in out
    assert out.index("\nOperating point\n") < out.index("\nConfidence\n")  # under the report


def test_an_agent_with_no_calibration_says_not_calibrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = scaffold(tmp_path)
    simulated = Simulated()
    simulated.write(root, "test", 120, seed=2)
    serve(monkeypatch, simulated)
    pack = load_pack(root)
    run = evaluation(Decision(pack, origin_cap=False), load_dataset(root / "data" / "test.jsonl"))
    (entry,) = run.report["run"]["calibration"]["variants"]
    assert entry["status"] == "not_calibrated" and "fitted" not in entry
    assert "before" not in entry["questions"]["request"]
    assert run.report["run"]["calibration"]["lines"] == []
    row = "Confidence".ljust(26) + "reader/agent: not calibrated"
    assert row in report_text(run.report).splitlines()
    assert main(["eval", str(root), "--dataset", "test", "--baseline", "none", "--markdown"]) == 0
    assert "| Confidence of `reader/agent` | `not calibrated` |" in capsys.readouterr().out
    # An agent with no model variant has no row at all.
    rules = run_with(pack, {"reader": "rules"}, root)
    assert "calibration" not in rules["run"] and "Confidence" not in report_text(rules)


def run_with(pack: Any, providers: dict[str, str], root: Path) -> dict[str, Any]:
    decision = Decision(pack, providers=providers, origin_cap=False)
    return evaluation(decision, load_dataset(root / "data" / "test.jsonl")).report


def test_the_evaluation_counts_the_cases_of_a_changed_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, simulated = a_calibrated_run(tmp_path, monkeypatch)
    simulated.version = "2.2.0"  # the service was upgraded after its calibration
    run = evaluation(
        Decision(load_pack(root), origin_cap=False), load_dataset(root / "data" / "test.jsonl")
    )
    block = run.report["run"]["calibration"]
    assert block["agent_changed"] == 600
    assert block["lines"][-1] == (
        "600 of 600 cases: the agent reported another version than its calibration's, so "
        "nothing was rescaled and none was automated"
    )
    assert run.report["decisions"]["outcome_counts"]["AUTO"] == 0
    assert block["variants"][0]["questions"]["request"]["rescaled"] == 0


# -- the Decision Card -----------------------------------------------------------------------------


def test_the_card_says_whether_the_agents_confidence_is_calibrated(tmp_path: Path) -> None:
    plain = render_card(load_pack(scaffold(tmp_path, "plain")))
    assert "## Confidence\n\n- `reader` / `agent`: **not calibrated**." in plain
    assert plain.index("## Providers") < plain.index("## Confidence") < plain.index("## Validation")
    card = render_card(load_pack(calibrated_agent(tmp_path, {"request": 2.0})))
    assert (
        "- `reader` / `agent`: confidence **calibrated** on 400 cases of `calibration` on "
        "2026-11-24, for `starter-portal-agent` version `2.1.0` (questions: `request`). When "
        "the agent's service reports another version, no decision is automated until it is "
        "calibrated again."
    ) in card
    # An agent with no agent variant has no such section.
    assert "## Confidence" not in render_card(load_pack(INBOX_DIR))
