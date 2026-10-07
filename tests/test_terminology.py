# SPDX-License-Identifier: Apache-2.0
"""Mendari's CodeSystems and NamingSystems, generated from the code and the packs served
(standards/terminology.py)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Decision, Outcome, Pack, __version__, load_pack
from mendari.core.registry import discover
from mendari.errors import MendariError
from mendari.sandbox.fhir_records import CHANNELS
from mendari.standards import audit_fhir, davinci, terminology
from mendari.standards.fhir import (
    DISPLAY_LIMIT,
    OUTCOME_SYSTEM,
    PACK_ID_SYSTEM,
    PACK_TAG_SYSTEM,
    FhirAdapter,
    display,
)
from mendari.standards.terminology import Terminology, resource_id
from tests.conftest import INBOX_DIR, ROOT
from tests.test_fhir import BUNDLE

ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
NAME = re.compile(r"^[A-Z][A-Za-z0-9_]{0,254}$")
# The fixed resources' content for each Mendari version: a url|version must never name two
# contents. Changing one of them means a new Mendari version (or, before its release, a new pin).
# 0.1.0 is not released yet (no tag, not on PyPI): until it is, its fixed resources may still
# change, and the pin moves with them (`input.erased`, LP-08). From the first tag, a change needs a
# new version.
PINNED = {
    "0.1.0": {
        "CodeSystem/decision-outcome": "39fd51215c2eb221",
        "CodeSystem/audit-event": "69760b971e953d00",
        "CodeSystem/sandbox": "8a63d292423bdf66",
        "CodeSystem/message-channel": "0c3b329c01cfcc58",
        # Added in LP-14, a new url (no earlier content): the prior-auth pack's fictional codes.
        "CodeSystem/fictional-procedures": "506d396c0a77d1af",
        "NamingSystem/decision-id": "a3ac1c38e9476b4f",
        "NamingSystem/input-hash": "fb7c0678a0c9fdbf",
        "NamingSystem/decision-pack": "677f5f91c8577ec0",
    }
}


@pytest.fixture(scope="module")
def packs() -> dict[str, Pack]:
    return {pack.id: pack for pack in discover([ROOT / "packs"]).packs}


@pytest.fixture(scope="module")
def served(packs: dict[str, Pack]) -> Terminology:
    return Terminology.for_packs(packs.values())


def inbox_variant(tmp_path: Path, pack_id: str, edit: Callable[[dict[str, Any]], None]) -> Pack:
    """The inbox pack under another id, with its FHIR mapping edited."""
    root = tmp_path / pack_id.replace("/", "-")
    shutil.copytree(INBOX_DIR, root)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["id"] = pack_id
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    mapping = yaml.safe_load((root / "fhir" / "mapping.yaml").read_text())
    edit(mapping["task"])
    (root / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping))
    return load_pack(root)


def code_system(found: Terminology, url: str) -> dict[str, Any]:
    resource = found.by_url(url)
    assert resource is not None, url
    return resource


def codes(resource: dict[str, Any]) -> list[str]:
    return [concept["code"] for concept in resource["concept"]]


def content(resource: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()


def test_outcomes_come_from_the_outcome_enum(served: Terminology) -> None:
    assert set(terminology.OUTCOMES) == set(Outcome)
    outcomes = code_system(served, OUTCOME_SYSTEM)
    assert codes(outcomes) == [outcome.value for outcome in Outcome]
    assert (outcomes["id"], outcomes["version"], outcomes["content"]) == (
        "decision-outcome",
        __version__,
        "complete",
    )
    definitions = {c["code"]: c["definition"] for c in outcomes["concept"]}
    for withheld in ("BLOCK", "ABSTAIN"):
        assert "fallback" in definitions[withheld]
    assert "never the blocked action" in definitions["BLOCK"]


def test_audit_events_are_the_ones_the_export_emits(served: Terminology) -> None:
    events = code_system(served, audit_fhir.EVENT_SYSTEM)
    assert codes(events) == list(audit_fhir._EVENTS)
    assert events["version"] == __version__
    for concept in events["concept"]:
        emitted = audit_fhir.audit_event(
            {
                "event": concept["code"],
                "decision_id": "d1",
                "seq": 1,
                "prev_hash": "0",
                "hash": "1",
                "created_at": "2026-10-01T12:00:00+00:00",
                "payload": {},
                "pack_id": "mendari/x",
                "pack_version": "1",
                "record": {},
            }
        )
        coding = emitted["subtype"][0]
        assert (coding["code"], coding["display"]) == (concept["code"], concept["display"])


def test_packs_served_are_a_fragment(packs: dict[str, Pack], served: Terminology) -> None:
    listed = code_system(served, PACK_TAG_SYSTEM)
    assert listed["content"] == "fragment" and "count" not in listed
    assert codes(listed) == sorted(pack.ref for pack in packs.values())


def test_performer_codes_follow_the_task_template(
    packs: dict[str, Pack], served: Terminology
) -> None:
    inbox = packs["mendari/patient-inbox-router"]
    destinations = code_system(served, FhirAdapter.for_pack(inbox).mapping.task.performer_system)
    assert codes(destinations) == [*inbox.manifest.labels("destination"), "manual-triage"]
    referral = packs["mendari/referral-router"]
    queues = code_system(served, FhirAdapter.for_pack(referral).mapping.task.performer_system)
    assert codes(queues) == [*referral.manifest.labels("specialty"), "unknown"]  # "{specialty}"
    assert queues["concept"][0]["definition"] == " ".join(
        referral.manifest.questions["specialty"].criteria["cardiology"].split()
    )
    prior_auth = FhirAdapter.for_pack(packs["mendari/prior-auth-preflight"])
    assert codes(code_system(served, prior_auth.mapping.task.performer_system)) == [
        "prior-auth-team"  # a literal
    ]
    assert not any(r["url"].endswith("ai-guard") for r in served.resources if "url" in r)


def test_a_task_performer_is_a_code_of_its_system(router: Decision, served: Terminology) -> None:
    _, task = router.decide_fhir(BUNDLE)
    coding = task["performerType"][0]["coding"][0]
    assert coding["code"] in codes(code_system(served, coding["system"]))
    outcome = task["businessStatus"]["coding"][0]
    assert outcome["code"] in codes(code_system(served, outcome["system"]))
    tag = task["meta"]["tag"][0]
    assert tag["code"] in codes(code_system(served, tag["system"]))


def test_packs_sharing_a_performer_system_share_one_code_system(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    twin = inbox_variant(tmp_path, "test/inbox-twin", lambda task: task.update(performer="desk"))
    system = FhirAdapter.for_pack(inbox_pack).mapping.task.performer_system

    alone = code_system(Terminology.for_packs([inbox_pack]), system)
    shared = code_system(Terminology.for_packs([inbox_pack, twin]), system)
    assert codes(shared) == [*codes(alone), "desk"]
    assert "`test/inbox-twin@" in shared["description"]
    assert shared["version"] != alone["version"]


def test_question_systems_define_labels_by_their_criteria(
    packs: dict[str, Pack], served: Terminology
) -> None:
    for pack in packs.values():
        version = f"{pack.version}+{pack.digest.removeprefix('sha256:')[:8]}"
        for key, question in pack.manifest.questions.items():
            system = code_system(served, davinci.question_system(pack.id, key))
            assert system["version"] == version
            assert codes(system) == question.labels
            for concept in system["concept"]:
                assert concept["display"] == display(question.criteria[concept["code"]])


def test_da_vinci_answers_display_what_their_code_system_says(
    packs: dict[str, Pack], served: Terminology
) -> None:
    pack = packs["mendari/prior-auth-preflight"]
    form = davinci.questionnaire(pack, {"service": "lumbar_mri", "payer": "acme"})
    assert form is not None
    for item in form["item"]:
        for option in item["answerOption"]:
            coding = option["valueCoding"]
            defined = code_system(served, coding["system"])
            shown = {c["code"]: c["display"] for c in defined["concept"]}
            assert coding["display"] == shown[coding["code"]]


def test_naming_systems(served: Terminology) -> None:
    engine = served.get("NamingSystem", "decision-pack")
    assert engine is not None
    assert engine["uniqueId"] == [{"type": "uri", "value": PACK_ID_SYSTEM, "preferred": True}]
    assert engine["kind"] == "identifier"
    assert served.by_url(audit_fhir.INPUT_HASH_SYSTEM) == served.get("NamingSystem", "input-hash")
    assert {r["id"] for r in served.resources if r["resourceType"] == "NamingSystem"} == {
        "decision-id",
        "input-hash",
        "decision-pack",
    }


def test_lookup_by_id_and_url(served: Terminology) -> None:
    outcomes = served.get("CodeSystem", "decision-outcome")
    assert outcomes is not None and outcomes["url"] == OUTCOME_SYSTEM
    assert served.by_url(OUTCOME_SYSTEM, __version__) is outcomes
    assert served.by_url(OUTCOME_SYSTEM, "0.0.0-other") is None
    assert served.by_url("https://example.org/none") is None
    assert served.get("CodeSystem", "nope") is None
    assert served.get("ValueSet", "decision-outcome") is None


def test_versions_change_with_the_packs_served(packs: dict[str, Pack]) -> None:
    inbox, referral, guard = (
        packs[f"mendari/{name}"] for name in ("patient-inbox-router", "referral-router", "ai-guard")
    )
    one = Terminology.for_packs([inbox])
    two = Terminology.for_packs([inbox, referral])
    three = Terminology.for_packs([inbox, referral, guard])
    version = lambda found, url: code_system(found, url)["version"]  # noqa: E731
    assert version(one, PACK_TAG_SYSTEM) != version(two, PACK_TAG_SYSTEM)
    assert version(one, PACK_TAG_SYSTEM).startswith(f"{__version__}+")
    assert version(one, OUTCOME_SYSTEM) == version(three, OUTCOME_SYSTEM) == __version__
    # A performer system's version follows only the packs that route to it.
    destinations = FhirAdapter.for_pack(inbox).mapping.task.performer_system
    assert version(one, destinations) == version(three, destinations)


def test_no_url_and_version_names_two_contents(packs: dict[str, Pack]) -> None:
    """Across deployments that serve different sets of the bundled packs."""
    every = sorted(packs.values(), key=lambda pack: pack.id)
    seen: dict[str, str] = {}
    for count in range(len(every) + 1):
        for start in range(len(every)):
            subset = every[start : start + count] if count else []
            for resource in [*terminology.resources(subset), *terminology.sandbox_resources()]:
                if resource["resourceType"] != "CodeSystem":
                    continue
                key = f"{resource['url']}|{resource['version']}"
                assert seen.setdefault(key, content(resource)) == content(resource), key


def test_fixed_resources_are_pinned_to_the_mendari_version(served: Terminology) -> None:
    fixed = [
        *(r for r in served.resources if r["id"] in ("decision-outcome", "audit-event")),
        *(r for r in served.resources if r["resourceType"] == "NamingSystem"),
        *terminology.sandbox_resources(),
    ]
    found = {f"{r['resourceType']}/{r['id']}": content(r)[:16] for r in fixed}
    assert __version__ in PINNED, "a new Mendari version: pin its fixed resources"
    assert found == PINNED[__version__], "changed a fixed resource: it needs a new version"


def test_every_resource_is_well_formed(served: Terminology) -> None:
    resources = [*served.resources, *terminology.sandbox_resources()]
    ids = [f"{r['resourceType']}/{r['id']}" for r in resources]
    assert len(set(ids)) == len(ids)
    for resource in resources:
        assert ID.match(resource["id"]), resource["id"]
        assert NAME.match(resource["name"]), resource["name"]
        assert resource["status"] == "active"
        assert resource["text"]["status"] == "generated"
        assert resource["text"]["div"].startswith('<div xmlns="http://www.w3.org/1999/xhtml">')
        if resource["resourceType"] == "CodeSystem":
            assert resource["caseSensitive"] is True
            sandbox = ("sandbox", "message-channel", "fictional-procedures")
            assert resource["experimental"] is (resource["id"] in sandbox)
            for concept in resource["concept"]:
                assert f"<td>{concept['code']}</td>" in resource["text"]["div"]


def test_sandbox_systems_are_experimental_and_never_served(served: Terminology) -> None:
    sandbox = terminology.sandbox_resources()
    assert all(r["experimental"] for r in sandbox)
    assert not any(served.by_url(r["url"]) for r in sandbox)
    schema = json.loads((ROOT / "packs/patient-inbox-router/schemas/input.schema.json").read_text())
    channels = schema["properties"]["message"]["properties"]["channel"]["enum"]
    assert codes(sandbox[1]) == list(CHANNELS) == channels


def test_one_version_of_each_pack(packs: dict[str, Pack]) -> None:
    inbox = packs["mendari/patient-inbox-router"]
    with pytest.raises(MendariError, match="one version of each pack"):
        terminology.resources([inbox, inbox])


def test_a_pack_cannot_redefine_a_fixed_system(tmp_path: Path) -> None:
    rogue = inbox_variant(
        tmp_path, "test/rogue", lambda task: task.update(performer_system=OUTCOME_SYSTEM)
    )
    with pytest.raises(MendariError, match="two terminology resources share"):
        terminology.resources([rogue])


def test_resource_ids_are_valid_fhir_ids() -> None:
    assert resource_id("mendari/patient-inbox-router-destination") == (
        "mendari-patient-inbox-router-destination"
    )
    long_a, long_b = "x/" + "a" * 80, "x/" + "a" * 79 + "b"
    for text in (long_a, long_b, "Ünïcode ünd spaces", "///"):
        assert ID.match(resource_id(text)), text
    assert len(resource_id(long_a)) <= 64
    assert resource_id(long_a) != resource_id(long_b)


def test_display_shortens_on_a_word_boundary() -> None:
    assert display("  Chest pain,\n  trouble breathing.  ") == "Chest pain, trouble breathing."
    words = " ".join(["word"] * 100)
    short = display(words)
    assert len(short) <= DISPLAY_LIMIT and short.endswith("word…")
    assert words.startswith(short.removesuffix("…"))
    unbroken = display("x" * 400)
    assert len(unbroken) == DISPLAY_LIMIT and unbroken.endswith("x…")
    exact = "y" * DISPLAY_LIMIT
    assert display(exact) == exact
