# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import Decision, load_pack
from mendari.core.spec import PackManifest
from mendari.core.templates import expansions, fill, template_problems, unknown_placeholders
from mendari.errors import PackValidationError
from mendari.evals.pack_tests import validate_pack
from mendari.standards import cds_hooks
from mendari.standards.fhir import FhirAdapter
from tests.conftest import DEMO_AGENT_DIR, TRIAGE_DIR, fixture_agents

# A synthetic portal message as the triage fixture's FHIR mapping reads it: a refill, which its
# keyword rules send to the pharmacy, nothing urgent or abusive in it.
COMMUNICATION: dict[str, Any] = {
    "resourceType": "Communication",
    "id": "msg-1",
    "status": "in-progress",
    "subject": {"reference": "Patient/pat-1"},
    "topic": {"text": "Refill"},
    "medium": [{"coding": [{"system": "x", "code": "portal"}]}],
    "sent": "2026-09-25T10:00:00Z",
    "payload": [{"contentString": "Please renew my prescription for metformin."}],
}
PATIENT: dict[str, Any] = {"resourceType": "Patient", "id": "pat-1", "birthDate": "1950-03-02"}
BUNDLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [{"resource": COMMUNICATION}, {"resource": PATIENT}],
}


def cds_request() -> dict[str, Any]:
    """The triage fixture's CDS hook, with the message and its sender as prefetch."""
    return {
        "hook": "mendari-message-received",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "context": {"communicationId": "msg-1", "patientId": "pat-1"},
        "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
    }


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


def test_expansions_are_every_text_fill_can_give() -> None:
    choices = {"specialty": ["cardiology", "unknown"], "priority": ["routine", "stat"]}
    assert expansions("{specialty}-{priority}-{specialty}", choices) == [
        ("cardiology-routine-cardiology", {"specialty": "cardiology", "priority": "routine"}),
        ("cardiology-stat-cardiology", {"specialty": "cardiology", "priority": "stat"}),
        ("unknown-routine-unknown", {"specialty": "unknown", "priority": "routine"}),
        ("unknown-stat-unknown", {"specialty": "unknown", "priority": "stat"}),
    ]
    assert expansions("prior-auth-team", choices) == [("prior-auth-team", {})]


def test_answers_fill_templates_and_missing_ones_read_unknown() -> None:
    result = Decision.from_path(TRIAGE_DIR).decide_fhir(BUNDLE)[0]
    text = "{recommendation}: same-day {urgency}, {priority}, abusive {abusive}, {nope}"
    assert fill(text, result, recommendation="pharmacy") == (
        "pharmacy: same-day no, routine, abusive no, unknown"
    )


def test_task_and_card_templates_name_answers(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE_DIR, root)
    mapping = yaml.safe_load((root / "fhir" / "mapping.yaml").read_text())
    mapping["task"]["performer"] = "triage-team-{urgency}"
    mapping["task"]["description"] = "Route to {recommendation} (abusive {abusive})"
    (root / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping))
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["cds_hooks"]["card"]["summaries"]["pharmacy"] = "A refill (same-day {urgency})"
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    decision = Decision.from_path(root)
    _, task = decision.decide_fhir(BUNDLE)
    assert task["performerType"][0]["coding"][0]["code"] == "triage-team-no"
    assert task["description"] == "Route to pharmacy (abusive no)"
    card = cds_hooks.invoke(decision, cds_request())["cards"][0]
    assert card["summary"] == "A refill (same-day No)"  # a card reads display names (LP-23f)


def test_unknown_placeholders_are_refused_when_the_pack_loads(tmp_path: Path) -> None:
    raw = yaml.safe_load((TRIAGE_DIR / "agent.yaml").read_text())
    raw["cds_hooks"]["card"]["summaries"]["pharmacy"] = "Refill for {study}"
    with pytest.raises(ValidationError, match=r"unknown placeholders \['study'\]"):
        PackManifest.model_validate(raw)

    root = tmp_path / "pack"
    shutil.copytree(TRIAGE_DIR, root)
    mapping = yaml.safe_load((root / "fhir" / "mapping.yaml").read_text())
    mapping["task"]["performer"] = "{team}"
    (root / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping))
    with pytest.raises(PackValidationError, match=r"task.performer uses unknown placeholders"):
        FhirAdapter.for_pack(Decision.from_path(root).pack)


# -- bare placeholders (LP-16e) ------------------------------------------------------------------

SHAPED = ["{priority:>100000000}", "{priority!r}", "{priority.x}", "{priority[0]}"]
BARE = "a placeholder is a bare name, with no conversion, format spec, attribute or index"


def test_template_problems_name_each_placeholder_that_is_not_bare() -> None:
    questions = ["specialty"]
    assert template_problems("Route to {specialty} ({priority}): {recommendation}", questions) == []
    assert template_problems("{nope} {specialty!s:>3} {specialty:{priority}}", questions) == [
        "uses unknown placeholders ['nope']",
        f"uses {{specialty!s:>3}}: {BARE}",
        f"uses {{specialty:{{priority}}}}: {BARE}",
    ]
    assert template_problems("{specialty.x} {specialty[0]}", questions) == [
        f"uses {{specialty.x}}: {BARE}",
        f"uses {{specialty[0]}}: {BARE}",
    ]
    assert template_problems("unbalanced {", questions) == [
        "uses unknown placeholders ['unbalanced {']"
    ]
    assert template_problems("{priority:" + "9" * 100 + "}", questions) == [
        f"uses {{priority:{'9' * 26}...}}: {BARE}"  # never more of it than 40 characters
    ]


def with_wording(tmp_path: Path, *, summary: str | None = None, task: str | None = None) -> Path:
    """An outsider's copy of the triage fixture (its own id, so from a registry it is bounded)
    with its `pharmacy` card summary or its Task description replaced."""
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE_DIR, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["id"] = "outsider/triage"
    if summary is not None:
        manifest["cds_hooks"]["card"]["summaries"]["pharmacy"] = summary
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    if task is not None:
        mapping = yaml.safe_load((root / "fhir" / "mapping.yaml").read_text())
        mapping["task"]["description"] = task
        (root / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping))
    return root


@pytest.mark.parametrize("shape", SHAPED)
def test_a_wording_template_takes_bare_placeholders_only(tmp_path: Path, shape: str) -> None:
    """A card summary or a Task text whose placeholder is not bare. From a registry: refused at
    load. Owned: the card summary loads warned; the Task text is read as it always was, so the
    pack serves; `mendari validate` refuses both. An attribute or an index names no question,
    so it was refused already wherever the pack comes from, and still is."""
    reaching = "." in shape or "[" in shape
    card = with_wording(tmp_path / "card", summary=f"A refill ({shape})")
    if reaching:
        for origin in ("hub", "local"):
            with pytest.raises(PackValidationError, match="uses unknown placeholders"):
                load_pack(card, origin=origin)
    else:
        problem = f"cds_hooks.card.summaries.pharmacy uses {shape}: {BARE}"
        with pytest.raises(PackValidationError) as refused:
            load_pack(card, origin="hub")
        assert refused.value.problems == [problem]
        owned = load_pack(card)
        assert owned.warnings == (problem,)
        assert Decision(owned).decide_fhir(BUNDLE)[0].recommendation == "pharmacy"
        with pytest.raises(PackValidationError, match=re.escape(problem)):
            validate_pack(owned)

    task = with_wording(tmp_path / "task", task=f"Route to {shape}")
    problem = f"fhir/mapping.yaml: task.description uses {shape}: {BARE}"
    with pytest.raises(PackValidationError) as refused:
        load_pack(task, origin="hub")
    assert refused.value.problems == [problem]
    owned = load_pack(task)
    assert owned.warnings == ()
    if reaching:
        with pytest.raises(
            PackValidationError, match=r"task\.description uses unknown placeholders"
        ):
            FhirAdapter.for_pack(owned)
    else:
        FhirAdapter.for_pack(owned)  # it serves, as before
        with pytest.raises(PackValidationError, match=re.escape(problem)):
            validate_pack(owned)


def test_fill_renders_a_template_as_it_always_did() -> None:
    """`fill` is unchanged: what an owned pack's wording renders today, it renders still."""
    result = Decision.from_path(TRIAGE_DIR).decide_fhir(BUNDLE)[0]
    assert fill("{priority!r} {priority:>9}", result, recommendation="x") == "'routine'   routine"


def test_every_shipped_template_is_bare() -> None:
    """Every card summary and Task text the bundled demo agent and the test fixtures ship (the
    triage fixture's Task text is the default description) is bare: none is warned of, and none
    would be refused from a registry."""
    roots = [DEMO_AGENT_DIR, *fixture_agents()]
    checked = 0
    for root in roots:
        pack = load_pack(root)
        assert pack.warnings == (), root
        hooks, wording = pack.manifest.cds_hooks, []
        if hooks is not None and hooks.card is not None:
            wording += list(hooks.card.summaries.values())
        if pack.manifest.inputs.fhir is not None:
            task = FhirAdapter.for_pack(pack, bare=True).mapping.task
            wording += [text for text in (task.description, task.performer) if text]
        for text in wording:
            assert template_problems(text, pack.manifest.questions) == [], (root, text)
        checked += len(wording)
    assert checked >= 10  # the chart and triage fixtures' own
