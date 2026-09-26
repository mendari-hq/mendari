from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from openhealth import AuditStore, Decision, Outcome, Pack
from openhealth.errors import PackValidationError
from tests.conftest import message


def test_from_hub(router: Decision) -> None:
    decision = Decision.from_hub("openhealth/patient-inbox-router")
    assert decision.pack.digest == router.pack.digest
    assert decision.provider_info["router"].variant == "naive_bayes"


def test_result_matches_generated_output_schema(router: Decision) -> None:
    result = router(message("Can I get a refill on my atorvastatin?", channel="portal"))
    Draft202012Validator(router.pack.output_schema).validate(result.to_dict())
    assert result.pack_digest == router.pack.digest
    assert result.input_hash.startswith("sha256:")


def test_invalid_input_is_blocked_without_calling_providers(router: Decision) -> None:
    result = router({"message": {"body": "hi"}, "unexpected": True})
    assert result.outcome is Outcome.BLOCK
    assert result.answers == {}
    assert result.fallback is not None
    assert any("unexpected" in reason for reason in result.reasons)


def test_unknown_provider_slot_is_rejected(inbox_pack: Pack) -> None:
    with pytest.raises(PackValidationError, match="unknown provider slots"):
        Decision(inbox_pack, providers={"nope": "rules"})


def test_swapping_providers_keeps_the_contract(inbox_pack: Pack) -> None:
    decision = Decision(inbox_pack, providers={"router": "rules"})
    result = decision(message("I got a bill for $120 and my insurance should have covered it"))
    assert result.answers["destination"].provider.type == "rules"
    assert result.recommendation == "billing"


def test_provider_failure_falls_back_safely(
    inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    decision = Decision(inbox_pack, providers={"router": "jev"})
    result = decision(message("I need a refill"))
    assert result.outcome is Outcome.ABSTAIN
    assert result.errors == {"destination": "credential_unavailable"}
    assert result.fallback and "manual triage" in result.fallback


def test_decisions_are_recorded_when_a_store_is_attached(inbox_pack: Pack) -> None:
    with AuditStore(":memory:") as store:
        decision = Decision(inbox_pack, store=store)
        result = decision(message("My A1c came back at 7.8, what does that mean?"))
        stored = store.get(result.id)
    assert stored is not None
    assert stored["record"]["input"]["message"]["body"].startswith("My A1c")
    assert stored["events"][0]["event"] == "decision.created"
