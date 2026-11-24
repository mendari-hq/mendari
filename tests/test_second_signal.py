# SPDX-License-Identifier: Apache-2.0
"""A second signal that only lowers automation (ADR-0024): repeated runs
(`providers/repeat.py`), the two-model check (`providers/check.py`), what the policy does with
either (`policy.lowered`), what `mendari eval` reports of them (`evals/confidence.py`), and the
operating point, which stays exact with them on."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mendari import AuditStore, Decision, load_pack
from mendari.cli import main
from mendari.core.card import render_card
from mendari.core.decision import CallBudget
from mendari.core.policy import PERSON_DECIDES, GovernanceOverride, PolicyResult, lowered
from mendari.core.spec import Outcome, Priority
from mendari.errors import PackValidationError, ProviderError
from mendari.evals import operating_point as op
from mendari.evals.harness import build_evaluation, evaluation, load_dataset
from mendari.providers import PROVIDER_CONFIG_MODELS, PROVIDER_TYPES, build_provider
from mendari.providers.agent import CANNED, ENDPOINT_ENV
from mendari.providers.agent_calibration import MIN_CASES, changed_reason
from mendari.providers.check import CheckConfig, CheckProvider, disagree_reason
from mendari.providers.composite import CompositeProvider
from mendari.providers.repeat import CANNED_NOTE as REPEAT_CANNED
from mendari.providers.repeat import agreed_reason
from mendari.providers.signals import Cap, Checked, Signals, deciding_questions
from tests.conftest import INBOX_DIR, ROOT
from tests.support.confidence_agents import (
    REFILL,
    VERSION,
    Simulated,
    answer,
    calibration_file,
    edit_manifest,
    live_member,
    scaffold,
    serve,
    variant,
)
from tests.test_llm import MODEL, STATE_FIELDS, completion, install

TIMEOUT = ProviderError("timeout", "the agent took too long")


def repeating(tmp_path: Path, runs: int = 3, **more: Any) -> Path:
    def add(manifest: dict[str, Any]) -> None:
        variant(manifest).update({"repeat": runs, **more})

    return scaffold(tmp_path, edit=add)


def decide(root: Path, **kwargs: Any) -> Any:
    return Decision(load_pack(root), **kwargs)(REFILL)


# -- repeated runs ---------------------------------------------------------------------------------


def test_runs_that_agree_answer_with_the_lowest_confidence_among_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = serve(monkeypatch, [answer("refill", c) for c in (0.97, 0.93, 0.99)])
    result = decide(repeating(tmp_path))
    assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
    said = result.answers["request"]
    assert (said.label, said.confidence) == ("refill", 0.93)  # never the 0.99 of one run
    assert said.evidence == ("repeat: 3 of 3 runs agreed",)
    # Agreement changes nothing else: the policy decides on that confidence, with no cap.
    assert result.outcome is Outcome.AUTO
    assert result.reasons == ("confidence 0.93 >= auto threshold 0.90",)
    assert result.signals.caps == ()
    assert result.signals.checked == (Checked("reader", "agent", "repeat", 3, 2),)


def test_every_run_is_counted_in_the_cost_and_the_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, [answer("refill", 0.97, cost=0.002)])
    result = decide(repeating(tmp_path))
    assert result.cost_usd == pytest.approx(0.006)
    assert (result.input_tokens, result.output_tokens) == (30, 15)
    metered = result.usage["agent"]
    assert (metered.calls, metered.priced) == (3, 3)
    assert metered.cost_usd == pytest.approx(0.006)
    assert (metered.input_tokens, metered.output_tokens) == (30, 15)
    once = decide(scaffold(tmp_path, "once"))
    assert once.cost_usd == pytest.approx(0.002) and once.usage["agent"].calls == 1


def test_runs_that_disagree_on_the_primary_question_are_not_automated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(
        monkeypatch,
        [answer("refill", 0.97), answer("appointment", 0.95), answer("refill", 0.99)],
    )
    result = decide(repeating(tmp_path))
    said = result.answers["request"]
    assert (said.label, said.confidence) == ("refill", 0.97)  # the majority's, at its lowest
    assert said.evidence == ("repeat: 2 of 3 runs agreed (others: appointment)",)
    assert result.outcome is Outcome.HUMAN_REVIEW and result.recommendation == "refill"
    assert result.reasons == (
        "confidence 0.97 >= auto threshold 0.90",
        "2 of 3 runs agreed on request; a person decides",
    )
    assert result.to_dict()["reasons"][-1] == "2 of 3 runs agreed on request; a person decides"
    assert result.signals.checked[0].disagreed == ("request",)


def test_runs_that_disagree_on_a_critical_question_are_not_automated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`urgent_symptoms` is a critical question (and one a rule reads): two runs see nothing,
    one sees symptoms, and the two do not outvote it."""
    runs = [answer(), answer(urgent="yes", urgent_confidence=0.9), answer()]
    serve(monkeypatch, runs)
    result = decide(repeating(tmp_path))
    assert result.answers["urgent_symptoms"].label == "no"
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.reasons[-1] == "2 of 3 runs agreed on urgent_symptoms; a person decides"


def test_a_disagreement_on_a_question_that_decides_nothing_is_evidence_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def with_a_note(manifest: dict[str, Any]) -> None:
        manifest["questions"]["language"] = {
            "instructions": "Which language is the message written in?",
            "criteria": {"english": "English.", "spanish": "Spanish."},
        }
        reader = manifest["providers"]["reader"]
        reader["answers"].append("language")
        reader["variants"] = {"agent": {**live_member(manifest), "repeat": 3}}
        del manifest["tests"]

    root = scaffold(tmp_path, edit=with_a_note)
    pack = load_pack(root)
    assert deciding_questions(pack.manifest) == {"request", "urgent_symptoms"}

    def said(language: str) -> dict[str, Any]:
        body = answer("refill", 0.97)
        body["answers"]["language"] = {"value": language, "confidence": 0.8}
        return body

    serve(monkeypatch, [said("english"), said("spanish"), said("english")])
    result = Decision(pack)(REFILL)
    assert result.outcome is Outcome.AUTO and result.signals.caps == ()
    assert result.answers["language"].evidence == ("repeat: 2 of 3 runs agreed (others: spanish)",)
    assert result.signals.checked[0].disagreed == ()


def test_a_tie_goes_to_the_earliest_run_and_is_a_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, [answer("appointment", 0.99), answer("refill", 0.99)])
    result = decide(repeating(tmp_path, runs=2))
    assert result.recommendation == "appointment" and result.outcome is Outcome.HUMAN_REVIEW
    assert result.reasons[-1] == "1 of 2 runs agreed on request; a person decides"


def test_a_run_that_fails_counts_as_a_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = serve(monkeypatch, [answer("refill", 0.97), TIMEOUT, answer("refill", 0.98)])
    result = decide(repeating(tmp_path))
    assert len(requests) == 3
    assert result.outcome is Outcome.HUMAN_REVIEW and result.recommendation == "refill"
    assert result.reasons[1:] == (
        "2 of 3 runs agreed on request (1 failed: timeout); a person decides",
        "2 of 3 runs agreed on urgent_symptoms (1 failed: timeout); a person decides",
    )
    assert result.answers["request"].evidence == ("repeat: 2 of 3 runs agreed (1 failed)",)
    # A call that was sent and did not answer may still be billed: the cost is unknown.
    assert result.cost_usd is None and result.usage["agent"].calls == 3
    assert result.usage["agent"].priced == 2


def test_runs_that_all_fail_fail_the_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = serve(monkeypatch, [TIMEOUT])
    result = decide(repeating(tmp_path))
    assert len(requests) == 3
    assert result.outcome is Outcome.ABSTAIN and result.recommendation is None
    assert result.errors == {"request": "timeout", "urgent_symptoms": "timeout"}


def test_every_run_is_charged_to_the_daily_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five calls a day and three runs a case: the first case uses three, and the second gets
    two answers and a refusal, which is a disagreement."""
    requests = serve(monkeypatch, [answer("refill", 0.97)])
    with AuditStore(tmp_path / "audit.db") as store:
        pack = load_pack(repeating(tmp_path))
        decision = Decision(pack, store=store, budget=CallBudget(store, {"agent": 5}))
        first, second, third = decision(REFILL), decision(REFILL), decision(REFILL)
    assert first.outcome is Outcome.AUTO and len(requests) == 5
    assert second.outcome is Outcome.HUMAN_REVIEW
    assert second.reasons[1] == (
        "2 of 3 runs agreed on request (1 failed: budget_exhausted); a person decides"
    )
    assert third.outcome is Outcome.ABSTAIN  # no run of it was made
    assert third.errors["request"] == "budget_exhausted"


def test_every_run_is_charged_to_the_organizations_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = serve(monkeypatch, [answer("refill", 0.97)])
    with AuditStore(tmp_path / "audit.db") as store:
        budget = CallBudget(store, {}, org_limits={"agent": 4})
        decision = Decision(load_pack(repeating(tmp_path)), store=store, budget=budget)
        first, second = decision(REFILL), decision(REFILL)
    assert first.outcome is Outcome.AUTO and len(requests) == 4
    assert second.outcome is Outcome.HUMAN_REVIEW
    assert "(2 failed: quota_exhausted)" in second.reasons[1]


def test_the_runs_share_the_decisions_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run takes the whole deadline: the others are never sent, and a run that was
    not made is not agreement."""

    def slow(body: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.08)
        return answer("refill", 0.97)

    requests = serve(monkeypatch, slow)
    result = decide(repeating(tmp_path), deadline_s=0.05)
    assert len(requests) == 1
    assert result.outcome is Outcome.HUMAN_REVIEW and result.recommendation == "refill"
    assert result.reasons[1] == "1 of 3 runs agreed on request (2 failed: timeout)" + PERSON_DECIDES
    assert result.usage["agent"].calls == 1


def test_the_lowest_confidence_is_the_lowest_calibrated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def add(manifest: dict[str, Any]) -> None:
        variant(manifest).update({"repeat": 3, "calibration": "models/c.json"})

    root = scaffold(tmp_path, edit=add)
    calibration_file(root, {"request": 2.0}, relative="models/c.json")
    serve(monkeypatch, [answer("refill", c) for c in (0.99, 0.97, 0.995)])
    result = decide(root)
    said = result.answers["request"]
    assert said.confidence == pytest.approx(0.8008, abs=1e-4)  # 0.97 at T=2
    assert said.evidence == ("repeat: 3 of 3 runs agreed", "calibrated T=2.00")
    assert result.signals.reported["request"]["refill"] == pytest.approx(0.97)
    # And a changed agent is capped once, however many runs said so.
    serve(monkeypatch, [answer("refill", 0.99, version="2.2.0")])
    changed = decide(root)
    assert changed.outcome is Outcome.HUMAN_REVIEW
    assert [reason for reason in changed.reasons if "agent changed" in reason] == [
        "the agent changed since its confidence was calibrated (was 2.1.0, is 2.2.0)"
        + PERSON_DECIDES
    ]


def test_canned_answers_are_asked_once_and_claim_no_agreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canned answers cannot vary: repeating them would prove nothing, and says nothing."""
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    root = repeating(tmp_path)
    result, plain = decide(root), decide(scaffold(tmp_path, "plain"))
    said = result.answers["request"]
    assert said.evidence[0] == (
        "repeat: asked once, canned answers cannot vary, so no agreement is claimed"
    )
    assert not any("agreed" in item for item in said.evidence)
    assert not any("agreed" in reason for reason in result.reasons)
    assert (result.outcome, result.confidence) == (plain.outcome, plain.confidence)
    assert result.signals.checked == (Checked("reader", "agent", "repeat", 3, 0, canned=True),)
    assert decide(root, canned_cap=True).reasons[-1].startswith(CANNED)


@pytest.mark.parametrize("runs", [1, 6, 0, -3, "three", 2.5, True])
def test_a_variant_repeats_two_to_five_times(tmp_path: Path, runs: Any) -> None:
    with pytest.raises(PackValidationError, match="repeat"):
        build_provider(load_pack(repeating(tmp_path, runs=runs)), "reader", "agent")


def test_the_second_signals_need_spec_0_4(tmp_path: Path) -> None:
    def earlier(manifest: dict[str, Any]) -> None:
        manifest["spec_version"] = "0.3"

    root = repeating(tmp_path)
    edit_manifest(root, earlier)
    with pytest.raises(PackValidationError, match=r"variants\.agent\.repeat'\] use spec 0\.4"):
        load_pack(root)
    checked = checking(tmp_path, "checked")
    edit_manifest(checked, earlier)
    with pytest.raises(PackValidationError) as refused:
        load_pack(checked)
    assert "['providers.reader.variants.checked (type check)'] use spec 0.4" in str(refused.value)


# -- repeated runs of an `llm` variant -------------------------------------------------------------


def inbox_with_a_repeated_llm(tmp_path: Path) -> Path:
    root = tmp_path / "inbox"
    shutil.copytree(INBOX_DIR, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["spec_version"] = "0.4"
    for slot in ("router", "safety_screen"):
        manifest["providers"][slot]["variants"]["llm"] = {
            "type": "llm",
            "state_fields": STATE_FIELDS,
            **({"repeat": 3} if slot == "router" else {}),
        }
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    policy = yaml.safe_load((root / "policy.yaml").read_text())
    del policy["automation_requires"]
    (root / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False))
    return root


def test_an_llm_variant_repeats_too_and_shares_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each run is a call of its own (a repeated variant never shares one with another slot),
    and a run that names another team sends the message to a person."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("LLM_MODEL", MODEL)
    asked: list[list[str]] = []
    tops = ["medical_assistant", "front_desk", "medical_assistant"]

    def handler(request: httpx.Request) -> httpx.Response:
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
        content = {}
        for question, spec in schema["properties"].items():
            labels = list(spec["properties"])
            top = tops[len(asked) % 3] if question == "destination" else "routine"
            content[question] = {label: 0.96 if label == top else 0.01 for label in labels}
            if question == "urgency":
                content[question] = {"urgent": 0.02, "routine": 0.98}
        asked.append(list(schema["properties"]))
        return httpx.Response(200, json=completion(content))

    install(monkeypatch, handler)
    pack = load_pack(inbox_with_a_repeated_llm(tmp_path))
    both = Decision(pack, providers={"router": "llm", "safety_screen": "llm"})
    result = both({"message": {"body": "A synthetic question about a refill."}})
    assert asked.count(["destination"]) == 3 and asked.count(["urgency"]) == 1
    assert result.recommendation == "medical_assistant"
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.reasons[-1] == "2 of 3 runs agreed on destination; a person decides"
    assert result.usage["llm"].calls == 4
    assert result.cost_usd == pytest.approx(4 * 0.0012)


# -- answer with one variant, check with another ---------------------------------------------------


def checking(tmp_path: Path, name: str = "agent", **second: Any) -> Path:
    """The scaffold with a `checked` variant: its agent answers, and a second agent checks."""

    def add(manifest: dict[str, Any]) -> None:
        reader = manifest["providers"]["reader"]
        reader["variants"]["checked"] = {
            "type": "check",
            "members": {
                "agent": live_member(manifest, agent_id="first-agent"),
                "second": live_member(manifest, agent_id="second-agent", **second),
            },
        }
        reader["use"] = "checked"

    return scaffold(tmp_path, name, edit=add)


def two(first: Any, second: Any) -> Any:
    """A service that answers the member that answers with `first`, the one that checks with
    `second`."""

    def respond(body: dict[str, Any]) -> Any:
        return first if body["agent_id"] == "first-agent" else second

    return respond


def test_a_check_that_agrees_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer, its confidence and its distribution are the first member's, as it gave
    them: a second member that agrees, however sure it is, raises nothing."""
    requests = serve(monkeypatch, two(answer("refill", 0.91), answer("refill", 0.999)))
    root = checking(tmp_path)
    result = decide(root)
    assert [request["agent_id"] for request in requests] == ["first-agent", "second-agent"]
    said = result.answers["request"]
    assert (said.label, said.confidence) == ("refill", 0.91)
    assert said.evidence == ("check: second agrees",)
    assert said.provider.variant == "checked" and said.provider.type == "check"
    assert said.provider.model == f"agent:first-agent@{VERSION}"
    assert result.outcome is Outcome.AUTO
    assert result.reasons == ("confidence 0.91 >= auto threshold 0.90",)
    # The same decision as the first member alone would make, at the cost of both.
    assert result.cost_usd == pytest.approx(0.002) and result.usage["agent"].calls == 2
    assert result.signals.checked == (Checked("reader", "checked", "check", 2, 1),)


def test_a_check_that_disagrees_sends_the_decision_to_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, two(answer("refill", 0.99), answer("appointment", 0.6)))
    result = decide(checking(tmp_path))
    assert result.outcome is Outcome.HUMAN_REVIEW and result.recommendation == "refill"
    assert result.confidence == 0.99
    assert result.reasons == (
        "confidence 0.99 >= auto threshold 0.90",
        "agent and second disagree on request (agent: refill, second: appointment)"
        "; a person decides",
    )
    assert result.answers["request"].evidence == ("check: second answered appointment",)
    assert result.answers["urgent_symptoms"].evidence == ("check: second agrees",)


def test_a_check_that_fails_is_a_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As a composite's member that fails always was: recorded on each question, and the
    policy never automates over it."""
    serve(monkeypatch, two(answer("refill", 0.99), TIMEOUT))
    result = decide(checking(tmp_path))
    assert result.outcome is Outcome.HUMAN_REVIEW and result.recommendation == "refill"
    assert result.errors == {"request": "second:timeout", "urgent_symptoms": "second:timeout"}
    assert result.reasons[-1] == (
        "provider failure (second:timeout); safety checks may be incomplete"
    )
    assert result.answers["request"].evidence == ("check: second failed (timeout)",)


def test_the_member_that_checks_never_answers_in_place_of_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = serve(monkeypatch, two(TIMEOUT, answer("refill", 0.99)))
    result = decide(checking(tmp_path))
    assert len(requests) == 1  # the second member was not asked
    assert result.outcome is Outcome.ABSTAIN and result.answers == {}
    assert result.errors["request"] == "timeout"


def test_a_check_is_a_composite_of_exactly_two_members(tmp_path: Path) -> None:
    assert PROVIDER_TYPES["check"] is CheckProvider and issubclass(CheckProvider, CompositeProvider)
    assert PROVIDER_CONFIG_MODELS["check"] is CheckConfig
    for count in (1, 3):

        def members(manifest: dict[str, Any], count: int = count) -> None:
            names = ["agent", "second", "third"][:count]
            variant(manifest, "checked")["members"] = {
                name: live_member(manifest, agent_id=f"{name}-agent") for name in names
            }

        root = checking(tmp_path, f"with-{count}")
        edit_manifest(root, members)
        with pytest.raises(PackValidationError, match="members"):
            build_provider(load_pack(root), "reader", "checked")

    def nested(manifest: dict[str, Any]) -> None:
        inner = json.loads(json.dumps(variant(manifest, "checked")))  # a copy, not an alias
        variant(manifest, "checked")["members"]["second"] = inner

    root = checking(tmp_path, "nested")
    edit_manifest(root, nested)
    with pytest.raises(PackValidationError, match="unknown member type 'check'"):
        build_provider(load_pack(root), "reader", "checked")


def test_a_canned_member_proves_nothing_by_agreeing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def canned_members(manifest: dict[str, Any]) -> None:
        for member in variant(manifest, "checked")["members"].values():
            member["mock_file"] = "fixtures/mock_agent.json"

    root = checking(tmp_path)
    edit_manifest(root, canned_members)
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    result = decide(root)
    assert result.answers["request"].evidence[0] == (
        "check: canned answers, so agreement is not evidence"
    )
    assert result.signals.checked[0].canned is True
    assert decide(root, canned_cap=True).outcome is Outcome.HUMAN_REVIEW


def test_a_member_may_repeat_and_be_calibrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The members are ordinary variants: the one that answers may be calibrated and asked
    three times, and its caps hold beside the check's."""

    def add(manifest: dict[str, Any]) -> None:
        first = variant(manifest, "checked")["members"]["agent"]
        first.update({"repeat": 3, "calibration": "models/c.json"})

    root = checking(tmp_path)
    calibration_file(root, {"request": 1.2}, agent_id="first-agent", relative="models/c.json")
    edit_manifest(root, add)
    requests = serve(monkeypatch, two(answer("refill", 0.99), answer("refill", 0.9)))
    result = decide(root)
    assert len(requests) == 4 and result.outcome is Outcome.AUTO
    assert result.answers["request"].evidence == (
        "check: second agrees",
        "agent:repeat: 3 of 3 runs agreed",
        "agent:calibrated T=1.20",
    )
    assert [found.kind for found in result.signals.checked] == ["repeat", "check"]
    serve(monkeypatch, two(answer("refill", 0.99, version="9"), answer("other", 0.9)))
    capped = decide(root)
    assert capped.outcome is Outcome.HUMAN_REVIEW
    assert capped.reasons[1:] == (
        "the agent changed since its confidence was calibrated (was 2.1.0, is 9)" + PERSON_DECIDES,
        "agent and second disagree on request (agent: refill, second: other)" + PERSON_DECIDES,
    )


def test_the_card_names_each_second_signal(tmp_path: Path) -> None:
    card = render_card(load_pack(repeating(tmp_path)))
    assert (
        "- `reader` / `agent`: asks each case 3 times; the answer is the majority's with the "
        "lowest confidence among the runs that agree, and a disagreement is never automated."
    ) in card
    checked = render_card(load_pack(checking(tmp_path, "checked")))
    assert (
        "- `reader` / `checked`: `agent` answers and `second` checks; where they disagree, the "
        "decision is never automated."
    ) in checked


# -- it only lowers automation ---------------------------------------------------------------------

OUTCOMES = st.sampled_from(list(Outcome))
REASONS = st.lists(st.text(min_size=1, max_size=30), max_size=3)


@settings(max_examples=300, deadline=None)
@given(OUTCOMES, REASONS, REASONS, st.sampled_from(list(Priority)))
def test_a_cap_never_turns_another_outcome_into_auto(
    outcome: Outcome, reasons: list[str], caps: list[str], priority: Priority
) -> None:
    recommendation = None if outcome is Outcome.ABSTAIN else "refill"
    before = PolicyResult(outcome, recommendation, priority, tuple(reasons), rule="a-rule")
    after = lowered(before, caps)
    if not caps:
        assert after == before
        return
    if outcome is Outcome.AUTO:
        assert after.outcome is Outcome.HUMAN_REVIEW
    else:
        assert after.outcome is outcome  # ABSTAIN stays ABSTAIN, and a BLOCK stays final
    assert after.outcome is not Outcome.AUTO
    assert (after.recommendation, after.priority, after.rule) == (
        recommendation,
        priority,
        "a-rule",
    )
    assert after.reasons[: len(reasons)] == tuple(reasons)  # what the policy said is kept


RUN = st.one_of(
    st.none(),  # a run that fails
    st.tuples(
        st.sampled_from(["refill", "appointment", "other"]),
        st.sampled_from([0.4, 0.6, 0.89, 0.91, 0.95, 0.99]),
        st.sampled_from(["no", "no", "no", "yes"]),
        st.sampled_from([VERSION, VERSION, "2.2.0"]),
    ),
)


def an_answer(run: Any) -> Any:
    if run is None:
        return TIMEOUT
    request, confidence, urgent, version = run
    return answer(request, confidence, urgent=urgent, version=version)


def test_no_second_signal_and_no_version_cap_ever_raises_automation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever three runs answer, whatever a second model answers and whatever version the
    agent reports: a decision that is AUTO with a feature on is AUTO for each of those answers
    on its own, with the feature off. So no feature makes AUTO of anything that was not."""

    def calibrated(manifest: dict[str, Any]) -> None:
        variant(manifest)["calibration"] = "models/c.json"

    plain = Decision(load_pack(scaffold(tmp_path, "plain")))
    repeated = Decision(load_pack(repeating(tmp_path)))
    checked = Decision(load_pack(checking(tmp_path, "checked")))
    capped_root = scaffold(tmp_path, "capped", edit=calibrated)
    calibration_file(capped_root, {"request": 1.0}, relative="models/c.json")
    capped = Decision(load_pack(capped_root))
    approved = GovernanceOverride(approved_by="Dr. Synthetic", reference="GOV-1")

    def alone(run: Any, **kwargs: Any) -> Outcome:
        serve(monkeypatch, [an_answer(run)])
        decision = plain if not kwargs else Decision(plain.pack, **kwargs)
        return decision(REFILL).outcome

    @settings(
        max_examples=120,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(st.lists(RUN, min_size=3, max_size=3))
    def check(runs: list[Any]) -> None:
        serve(monkeypatch, [an_answer(run) for run in runs])
        with_repeat = repeated(REFILL)
        serve(monkeypatch, two(an_answer(runs[0]), an_answer(runs[1])))
        with_check = checked(REFILL)
        serve(monkeypatch, [an_answer(runs[0])])
        with_calibration = capped(REFILL)
        each = [alone(run) for run in runs]
        if with_repeat.outcome is Outcome.AUTO:
            assert each == [Outcome.AUTO] * 3
            assert len({run[0] for run in runs}) == 1  # and they agreed
        if with_check.outcome is Outcome.AUTO:
            assert each[0] is Outcome.AUTO and runs[1] is not None
            assert runs[0][0] == runs[1][0] and runs[0][2] == runs[1][2]
        if with_calibration.outcome is Outcome.AUTO:  # T = 1: the cap is all it can change
            assert each[0] is Outcome.AUTO and runs[0][3] == VERSION
        elif runs[0] is not None:
            assert with_calibration.outcome is (
                Outcome.HUMAN_REVIEW if each[0] is Outcome.AUTO else each[0]
            )
        # A governance approval lifts none of them.
        if with_repeat.signals.caps:
            serve(monkeypatch, [an_answer(run) for run in runs])
            again = Decision(repeated.pack, governance=approved)(REFILL)
            assert again.outcome is not Outcome.AUTO

    check()


# -- what `mendari eval` reports -------------------------------------------------------------------

_TOKEN = re.compile(r"\b(k[a-f]-\d{3})\b")
# What each kind of case is labelled, and what its three runs answer: (request, confidence,
# urgent_symptoms). The agent's majority answer decides the kind's fate.
Run = tuple[str, float, str]
RIGHT: Run = ("refill", 0.97, "no")
OTHER: Run = ("appointment", 0.9, "no")
UNSURE: list[Run] = [("refill", 0.7, "no"), ("other", 0.6, "no"), ("refill", 0.8, "no")]
SYMPTOMS: Run = ("refill", 0.97, "yes")
REFILL_CASE = {"request": "refill", "urgent_symptoms": "no"}
VISIT_CASE = {"request": "appointment", "urgent_symptoms": "no"}
URGENT_CASE = {"request": "refill", "urgent_symptoms": "yes"}
KINDS: dict[str, tuple[int, dict[str, str], list[Run]]] = {
    # 40 the runs agree on, rightly: automated.
    "ka": (40, REFILL_CASE, [RIGHT] * 3),
    # 10 they disagree on though the majority is right: an extra review.
    "kb": (10, REFILL_CASE, [RIGHT, OTHER, RIGHT]),
    # 6 they disagree on and the majority is wrong: caught.
    "kc": (6, VISIT_CASE, [RIGHT, OTHER, RIGHT]),
    # 4 they disagree on that were going to a person anyway (the majority is unsure, and right).
    "kd": (4, REFILL_CASE, UNSURE),
    # 3 with symptoms that one run of three sees: critical, and caught.
    "ke": (3, URGENT_CASE, [RIGHT, SYMPTOMS, RIGHT]),
    # 5 they agree on, wrongly: agreement is not correctness, and these are automated.
    "kf": (5, VISIT_CASE, [RIGHT] * 3),
}


def constructed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **more: Any) -> Path:
    """An agent asked three times a case, a set of 68 cases whose runs are scripted, and the
    service that plays the script."""
    root = repeating(tmp_path, **more)
    lines = []
    for kind, (count, labels, _) in KINDS.items():
        for number in range(count):
            body = f"A synthetic message, {kind}-{number:03d}."
            lines.append(
                {"id": f"{kind}-{number}", "input": {"message": {"body": body}}, "labels": labels}
            )
    path = root / "data" / "scripted.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    asked: dict[str, int] = {}

    def respond(body: dict[str, Any]) -> dict[str, Any]:
        found = _TOKEN.search(body["input"]["message"]["body"])
        assert found is not None
        token = found.group(1)
        asked[token] = asked.get(token, 0) + 1
        request, confidence, urgent = KINDS[token[:2]][2][(asked[token] - 1) % 3]
        return answer(request, confidence, urgent=urgent)

    serve(monkeypatch, respond)
    return root


def test_the_evaluation_reports_what_the_second_signal_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = constructed(tmp_path, monkeypatch)
    dataset = load_dataset(root / "data" / "scripted.jsonl")
    run = evaluation(Decision(load_pack(root), origin_cap=False), dataset)
    block = run.report["run"]["second_signal"]
    assert block["format"] == "mendari.second-signal/1" and block["cases"] == 68
    assert block["signals"] == [
        {
            "slot": "reader",
            "variant": "agent",
            "kind": "repeat",
            "runs": 3,
            "cases": 68,
            "canned": 0,
            "disagreed": 23,  # 10 + 6 + 4 + 3
            "caught": 9,  # 6 wrong, 3 critical
            "extra_review": 14,  # 10 + 4 right
            "unlabelled": 0,
            "extra_calls": 136,  # two more for each of 68
        }
    ]
    # Of the 23, the 4 unsure ones were going to a person anyway.
    assert block["kept_from_automation"] == {
        "k": 19,
        "caught": 9,
        "extra_review": 10,
        "unlabelled": 0,
    }
    assert block["lines"] == [
        "reader/agent: each case asked 3 times (136 extra calls)",
        "  disagreed on 23 of 68 cases: 9 wrong or critical (caught), 14 right (extra review)",
        "kept 19 of 68 cases from automation: 9 wrong or critical (caught), 10 right (extra "
        "review)",
    ]
    counts = run.report["decisions"]["outcome_counts"]
    assert counts == {"AUTO": 45, "HUMAN_REVIEW": 23, "ABSTAIN": 0, "BLOCK": 0}
    # What it did not catch is in the report's own rows: 5 agreed on, wrongly.
    wrong = run.report["proportions"]["false_automation"]
    assert (wrong["k"], wrong["n"]) == (5, 68)
    assert run.report["decisions"]["critical"][0]["auto_missed"] == 0
    # Without the signal (one run a case), the 19 are automated: 6 wrongly, 3 critical.
    once = Decision(load_pack(root), origin_cap=False)
    for provider in once.agent_providers():
        provider.settings = provider.settings.model_copy(update={"repeat": None})
    single = evaluation(once, dataset).report
    assert "second_signal" not in single["run"]
    assert single["decisions"]["outcome_counts"]["AUTO"] == 64
    assert single["proportions"]["false_automation"]["k"] == 11
    assert single["decisions"]["critical"][0]["auto_missed"] == 3
    assert run.report["cost"]["total_usd"] == pytest.approx(3 * single["cost"]["total_usd"])


def test_mendari_eval_prints_the_second_signal_under_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = constructed(tmp_path, monkeypatch)
    data = str(root / "data" / "scripted.jsonl")
    assert main(["eval", str(root), "--dataset", data, "--baseline", "none"]) == 0
    out, _ = capsys.readouterr()
    assert out.endswith(
        "\nSecond signal\n"
        "  reader/agent: each case asked 3 times (136 extra calls)\n"
        "    disagreed on 23 of 68 cases: 9 wrong or critical (caught), 14 right (extra review)\n"
        "  kept 19 of 68 cases from automation: 9 wrong or critical (caught), 10 right (extra "
        "review)\n"
    )
    assert "ka-0" not in out and "synthetic message" not in out  # no case id, no case text
    assert main(["eval", str(root), "--dataset", data, "--baseline", "none", "--markdown"]) == 0
    summary, _ = capsys.readouterr()
    assert "### Confidence and second signal\n\n    Second signal\n" in summary
    assert main(["eval", str(root), "--dataset", data, "--baseline", "none", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run"]["second_signal"]["kept_from_automation"]["k"] == 19
    assert "second_signal" not in report  # the run's, never the result's


def test_a_repeat_of_canned_answers_is_reported_as_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    root = repeating(tmp_path)
    dataset = load_dataset(root / "data" / "examples.jsonl")
    run = evaluation(Decision(load_pack(root), origin_cap=False), dataset)
    block = run.report["run"]["second_signal"]
    (signal,) = block["signals"]
    cases = len(dataset.records)
    assert (signal["cases"], signal["canned"], signal["extra_calls"]) == (cases, cases, 0)
    assert signal["disagreed"] == 0
    assert block["lines"] == [
        "reader/agent: each case asked 3 times (0 extra calls)",
        f"  not checked on {cases} cases: their answers are canned and cannot vary, so no "
        "agreement is claimed",
        f"kept 0 of {cases} cases from automation",
    ]


def test_the_evaluation_reports_a_check_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checking(tmp_path)
    simulated = Simulated(temperature=2.5)
    simulated.write(root, "test", 120, seed=3)

    def respond(body: dict[str, Any]) -> dict[str, Any]:
        said = simulated(body)
        if body["agent_id"] == "second-agent":  # a second reader, right where the first is not
            token = re.search(r"\b([a-z]+-\d{5})\b", body["input"]["message"]["body"])
            assert token is not None
            truth = simulated.table[token.group(1)][2]
            return answer(truth, 0.8)
        return said

    serve(monkeypatch, respond)
    run = evaluation(
        Decision(load_pack(root), origin_cap=False), load_dataset(root / "data" / "test.jsonl")
    )
    (signal,) = run.report["run"]["second_signal"]["signals"]
    wrong = sum(said != truth for said, _, truth, _ in simulated.table.values())
    assert (signal["kind"], signal["variant"], signal["extra_calls"]) == ("check", "checked", 120)
    assert signal["disagreed"] == signal["caught"] == wrong > 0 and signal["extra_review"] == 0
    assert run.report["run"]["second_signal"]["lines"][0] == (
        "reader/checked: each case checked by a second variant (120 extra calls)"
    )
    assert run.report["proportions"]["false_automation"]["k"] == 0
    # The calibration block reads the member that answers.
    (first, second) = run.report["run"]["calibration"]["variants"]
    assert (first["variant"], second["variant"]) == ("checked.agent", "checked.second")
    assert "questions" in first and "questions" not in second


# -- the operating point stays exact ---------------------------------------------------------------


def test_applying_the_policy_again_gives_the_run_back_with_every_feature_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caps are part of what each case's policy decided from, so the policy applied again
    at the manifest's threshold gives the run's outcomes back case for case: with repeated runs
    that disagree, a calibration, and a service that changes version part of the way through."""

    def add(manifest: dict[str, Any]) -> None:
        variant(manifest).update({"repeat": 3, "calibration": "models/c.json"})

    root = scaffold(tmp_path, edit=add)
    calibration_file(root, {"request": 1.3}, relative="models/c.json")
    simulated = Simulated(temperature=1.3, flaky=0.6)
    simulated.write(root, "test", 300, seed=5)
    upgraded = {f"test-{number:05d}" for number in range(250, 300)}

    def respond(body: dict[str, Any]) -> dict[str, Any]:
        said = simulated(body)
        token = re.search(r"\b(test-\d{5})\b", body["input"]["message"]["body"])
        assert token is not None
        if token.group(1) in upgraded:
            said["agent_version"] = "2.2.0"
        return said

    serve(monkeypatch, respond)
    pack = load_pack(root)
    dataset = load_dataset(root / "data" / "test.jsonl")
    run = evaluation(
        Decision(pack, origin_cap=False), dataset, targets=op.Targets(false_automation=0.05)
    )
    results_block = run.report["run"]
    assert results_block["second_signal"]["signals"][0]["disagreed"] > 10
    assert results_block["calibration"]["agent_changed"] == 50
    # The run again, keeping each decision, to replay it.
    simulated.asked.clear()
    decision = Decision(pack, origin_cap=False)
    results = [decision(record["input"]) for record in dataset.records]
    again = build_evaluation(decision, dataset, results)
    cases = op.cases_of(again.ledger, results)
    recorded = [(result.outcome.value, result.recommendation) for result in results]
    assert all(case.replays for case in cases)
    assert op.decided(pack.manifest, cases) == recorded
    assert op.decided(op.at_threshold(pack.manifest, 0.90), cases) == recorded  # its own, again
    assert op.tally(cases, recorded, 1) == op.tally_of(again.report)
    assert recorded == [(case["o"], case["r"]) for case in run.ledger.cases]  # the same run
    automated = sum(outcome == "AUTO" for outcome, _ in recorded)
    assert 0 < automated < 250
    # A capped case is automated at no threshold: the cap is not the threshold's to lift.
    capped = [index for index, result in enumerate(results) if result.signals.caps]
    assert len(capped) > 50
    for threshold in op.sequence(pack.manifest.policy.thresholds.review):
        at = op.decided(op.at_threshold(pack.manifest, threshold), cases)
        assert all(at[index][0] != "AUTO" for index in capped)
    # And the block the run printed is the one those counts give.
    point = results_block["operating_point"]
    assert point["current"]["automated"] == {"k": automated, "n": 300}


def test_signals_merge_each_cap_once() -> None:
    cap = Cap("repeat", "2 of 3 runs agreed on request")
    one = Signals(caps=(cap,), reported={"request": {"refill": 0.9}})
    other = Signals(caps=(cap, Cap("check", "a and b disagree on request")))
    both = one.merge(other)
    assert both.reasons() == ("2 of 3 runs agreed on request", "a and b disagree on request")
    assert both.reasons(("check",)) == ("a and b disagree on request",)
    assert both.reported == {"request": {"refill": 0.9}}
    assert not Signals() and Signals().merge(one) is one and one.merge(Signals()) is one


# -- the record, the spec and the guide ------------------------------------------------------------


def test_the_record_the_spec_and_the_guide_say_what_the_engine_says() -> None:
    """The reasons a reader is told to expect are the ones the engine writes, the floor of a
    fit is the one the fitter holds to, and the record is decided."""
    docs = ROOT / "docs"
    record = (
        docs / "decisions" / "ADR-0024-a-checked-confidence-and-a-second-signal.md"
    ).read_text(encoding="utf-8")
    spec = (docs / "spec" / "agent-manifest-0.4.md").read_text(encoding="utf-8")
    assert not (docs / "spec" / "agent-manifest-0.4-second-signal.md").exists()  # one 0.4 page
    guide = (docs / "guides" / "calibration-and-a-second-signal.md").read_text(encoding="utf-8")
    assert record.startswith(
        "# ADR-0024: A confidence that was checked, and a second signal that only lowers "
        "automation\n\n## Status\nAccepted\n\nDecided by the maintainer on 2026-11-24, before "
        "the repository was public, so no comment period ran; open to revision when it is.\n"
    )
    template = changed_reason("X", "Y")
    assert template in record and template in spec
    assert changed_reason("2.1.0", "2.2.0") + PERSON_DECIDES in guide
    said = disagree_reason("agent", "second", "request", "refill", "appointment")
    assert said + PERSON_DECIDES in guide
    assert disagree_reason("<first>", "<second>", "<question>", "<label>", "<label>") in spec
    assert (
        agreed_reason(2, 3, "request", [])
        .replace("2 of 3", "A of N")
        .replace("request", "<question>")
        in spec
    )
    assert REPEAT_CANNED in spec
    assert f"fewer than {MIN_CASES} labelled cases" in spec
    assert f"at least {MIN_CASES} labelled cases" in record
    assert f"needs {MIN_CASES} labelled cases" in guide
    for page in (record, spec, guide):
        assert 'spec_version: "0.4"' in page
