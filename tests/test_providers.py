# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Pack, load_pack
from mendari.errors import PackValidationError
from mendari.providers import Provider, build_provider
from mendari.providers.rules import within_one_edit
from tests.conftest import TRIAGE_DIR, message

SCREEN_RULES = "logic/screen_rules.yaml"  # the triage fixture's same-day symptoms and insults

# -- rules -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def red_flags(triage_agent: Pack) -> Provider:
    """The triage fixture's screen: same-day symptoms as keyword rules."""
    return build_provider(triage_agent, "screen", "rules")


@pytest.fixture(scope="module")
def tolerant(tmp_path_factory: pytest.TempPathFactory) -> Provider:
    """The same screen, with typo tolerance on (words of five letters or more)."""
    root = tmp_path_factory.mktemp("tolerant") / "triage"
    shutil.copytree(TRIAGE_DIR, root)
    rules = root / SCREEN_RULES
    config = yaml.safe_load(rules.read_text())
    config["typo_tolerance"] = {"min_length": 5}
    rules.write_text(yaml.safe_dump(config, sort_keys=False))
    return build_provider(load_pack(root), "screen", "rules")


def urgency(provider: Provider, body: str) -> tuple[str, float, tuple[str, ...]]:
    result = provider.answer(message(body)).answers["urgency"]
    return result.label, result.probabilities["yes"], result.evidence


def test_red_flag_escalates_with_named_evidence(red_flags: Provider) -> None:
    label, p_urgent, evidence = urgency(red_flags, "Crushing chest pain since this morning")
    assert label == "yes" and p_urgent > 0.9
    assert "yes:chest-pain" in evidence


def test_negated_red_flag_is_ignored(red_flags: Provider) -> None:
    label, p_urgent, _ = urgency(red_flags, "No chest pain or trouble breathing, just a cough.")
    assert label == "no" and p_urgent < 0.35


def test_negation_scope_ends_at_contrast(red_flags: Provider) -> None:
    label, _, _ = urgency(red_flags, "No fever, but now I have chest pain.")
    assert label == "yes"


def test_typos_in_red_flags_are_normalised(tolerant: Provider) -> None:
    label, _, evidence = urgency(tolerant, "having itght chset pressure right now")
    assert label == "yes"
    assert "typo:chset->chest" in evidence


def test_typo_normalisation_keeps_first_letter(tolerant: Provider) -> None:
    # "painted" is one edit from "fainted", but a different first letter blocks the rewrite.
    label, _, evidence = urgency(tolerant, "I painted the kitchen yesterday.")
    assert label == "no" and not evidence


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("chest", "chest", True),
        ("chset", "chest", True),  # transposition
        ("chst", "chest", True),  # deletion
        ("chesst", "chest", True),  # insertion
        ("chext", "chest", True),  # substitution
        ("cheats", "chest", False),
        ("tsehc", "chest", False),
    ],
)
def test_within_one_edit(a: str, b: str, expected: bool) -> None:
    assert within_one_edit(a, b) is expected


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("weight: 6, negatable", "weight: .inf, negatable"),
        ("default_score: 2.0", "default_score: .nan"),
        ("negation_window: 30\n", "negation_window: 30\ntemperature: .inf\n"),
    ],
    ids=["weight", "default-score", "temperature"],
)
def test_a_rules_file_with_a_number_that_is_not_finite_is_refused_when_built(
    tmp_path: Path, old: str, new: str
) -> None:
    # Each one made every decision that ran the rules raise (the softmax refuses a score or a
    # temperature that is not finite), so the decision had no outcome and no audit record.
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE_DIR, root)
    rules = root / SCREEN_RULES
    text = rules.read_text()
    assert old in text
    rules.write_text(text.replace(old, new, 1))
    pack = load_pack(root)
    assert any("screen_rules.yaml" in warning for warning in pack.warnings)
    with pytest.raises(PackValidationError, match=r"screen_rules\.yaml"):
        build_provider(pack, "screen", "rules")


def test_scores_that_add_up_past_a_float_fail_the_slot_closed_never_raise() -> None:
    # Each weight is finite, so the file loads; two of them on one label add up to infinity,
    # which the softmax refuses. It is the provider's failure (the decision goes to a person).
    from mendari.errors import ProviderError
    from mendari.providers.base import scored

    with pytest.raises(ProviderError) as raised:
        scored({"yes": 1e308 + 1e308, "no": 0.0}, 1.0)
    assert raised.value.code == "invalid_response"
    assert scored({"yes": 0.0, "no": 0.0}, 1.0) == {"yes": 0.5, "no": 0.5}


# -- the deployment owns the variables a remote provider reads ----------------------------------


@pytest.mark.parametrize(
    ("variant", "refused"),
    [
        (
            {"type": "agent", "agent_id": "x", "state_fields": ["message.body"],
             "endpoint_env": "PACK_SAFETY_URL"},
            "endpoint_env",
        ),
        ({"type": "llm", "base_url_env": "X", "model_env": "Y"}, "base_url_env, model_env"),
        (
            {
                "type": "check",
                "members": {
                    "rules": {"type": "rules", "config": "logic/router_rules.yaml"},
                    "llm": {"type": "llm", "api_key_env": "PACK_SAFETY_CANARY"},
                },
            },
            "api_key_env",
        ),
    ],
    ids=["agent", "llm", "check-member"],
)  # fmt: skip
def test_a_variant_or_member_naming_the_deployment_s_variables_is_refused_at_load(
    tmp_path: Path, variant: dict[str, Any], refused: str
) -> None:
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE_DIR, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["providers"]["router"]["variants"]["outside"] = variant
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(PackValidationError) as error:
        load_pack(root)
    assert error.value.problems == [
        "providers.router.variants.outside: an agent may not choose the environment variables a "
        f"provider reads its endpoint, model or key from; these are the deployment's. "
        f"Remove: {refused}"
    ]
    # The same keys on another provider type are that provider's business (rules refuses them as
    # keys it does not have: a warning for an agent the deployment owns).
    manifest["providers"]["router"]["variants"]["outside"] = {
        "type": "rules",
        "config": "logic/router_rules.yaml",
        "x_env": 1,
    }
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    assert load_pack(root).id == "mendari-test/triage"
