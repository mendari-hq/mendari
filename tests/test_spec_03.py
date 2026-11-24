# SPDX-License-Identifier: Apache-2.0
"""Spec 0.3 (LP-23g): only the FHIR mapping changes. A 0.1 or 0.2 pack that uses a 0.3 mapping
key is refused at load, wherever it comes from, and so by `mendari validate`; every 0.1 and 0.2
pack still loads and maps as the 23a golden says; and a 0.3 manifest is a 0.2 manifest."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import load_pack
from mendari.cli import main
from mendari.core.decision import input_hash
from mendari.core.origin import Origin
from mendari.core.spec import SPEC_VERSION, PackManifest
from mendari.errors import PackValidationError
from mendari.evals.harness import load_dataset
from mendari.standards.fhir import FhirAdapter, newer_keys
from tests.conftest import INBOX_DIR, ROOT, fixture_agents
from tests.test_fhir_decisions import script

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "prescreen-mini"
ICD = "http://hl7.org/fhir/sid/icd-10-cm"
SITE = "https://site.example/fhir/CodeSystem/conditions"  # a fictional site's own codes
PATIENT = {"resourceType": "Patient", "id": "p1", "birthDate": "1970-01-01"}

# Each 0.3 key, as one change to the prescreen fixture's mapping, and where `newer_keys` finds it.
CONDITIONS = "lists.conditions"
NEWER: dict[str, tuple[dict[str, Any], list[str]]] = {
    "paths": (
        {"fields": {"patient.age": {"paths": ["Patient.birthDate"], "transform": "age_years"}}},
        ["fields.patient.age.paths"],
    ),
    "predicate": (
        {"lists": {"conditions": {"resource": "Condition", "fields": {
            "code": {"path": f"code.coding[system={ICD}].code"}}}}},
        [f"{CONDITIONS}.fields.code.path"],
    ),
    "predicate in focus_where": (
        {"focus_where": {"path": f"identifier[system={SITE}].value", "in": ["x"]}},
        ["focus_where.path"],
    ),
    "code_set in focus_where": (
        {"code_sets": {"local": {"system": SITE, "codes": ["P1"]}},
         "focus_where": {"path": "meta.tag", "code_set": "local"}},
        ["code_sets", "focus_where.code_set"],
    ),
    "follow": (
        {"fields": {"patient.age": {"follow": "Patient.link.other",
            "path": "Patient.birthDate", "transform": "age_years"}}},
        ["fields.patient.age.follow"],
    ),
    "attachment_text": (
        {"fields": {"notes": {
            "path": "DocumentReference.content.attachment", "transform": "attachment_text"}}},
        ["fields.notes.transform"],
    ),
    "code_sets": (
        {"code_sets": {"t2d": {"system": ICD, "codes": ["E11.9"]}}},
        ["code_sets"],
    ),
    "code_sets, empty": ({"code_sets": {}}, ["code_sets"]),  # a key written, whatever it holds
    "follow, null": (
        {"fields": {"patient.age": {"follow": None,
            "path": "Patient.birthDate", "transform": "age_years"}}},
        ["fields.patient.age.follow"],
    ),
    "code_set": (
        {"code_sets": {"t2d": {"system": ICD, "codes": ["E11.9"]}},
         "lists": {"conditions": {"resource": "Condition", "fields": {
             "code": {"path": "code.coding", "code_set": "t2d"}}}}},
        ["code_sets", f"{CONDITIONS}.fields.code.code_set"],
    ),
    "resources": (
        {"lists": {"conditions": {"resources": {"Condition": {"fields": {
            "code": {"path": "code.coding[0].code"}}}}}}},
        [f"{CONDITIONS}.resources"],
    ),
    "medication_period": (
        {"lists": {"conditions": {"resources": {"MedicationRequest": {
            "medication_period": {"start": "start", "end": "end"}}}}}},
        [f"{CONDITIONS}.resources"],
    ),
}  # fmt: skip


def mini(
    tmp_path: Path, changes: dict[str, Any], *, spec: str = "0.2", systems: tuple[str, ...] = ()
) -> Path:
    """The prescreen fixture (not a reference pack: from a registry, it is bounded) at `spec`,
    declaring ICD-10-CM, a fictional site's code system and `systems`, its mapping changed by
    `changes` (a top-level mapping's keys merged, any other value replaced)."""
    root = tmp_path / "pack"
    shutil.copytree(MINI, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["spec_version"] = spec
    manifest["code_systems"] = [
        {"url": ICD, "name": "ICD-10-CM"},
        {"url": SITE, "name": "A fictional site's condition codes"},
        *({"url": url, "name": f"Fictional: {url}"} for url in systems),
    ]
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    mapping = yaml.safe_load((root / "fhir" / "mapping.yaml").read_text())
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(mapping.get(key), dict):
            mapping[key] = {**mapping[key], **value}
        else:
            mapping[key] = value
    (root / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping, sort_keys=False))
    return root


@pytest.mark.parametrize("key", sorted(NEWER))
def test_0_3_mapping_keys_in_a_0_2_pack_are_refused_at_load(
    tmp_path: Path, key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each 0.3 key in a 0.2 pack's mapping, named where it is: refused by `load_pack` wherever
    the pack comes from, as a 0.2 feature in a 0.1 pack is, so an owned pack with one is never
    served to fail every FHIR decision; and by `mendari validate`. A key counts when it is
    written, whatever it holds. As a 0.3 pack, the same mapping loads from anywhere."""
    changes, where = NEWER[key]
    root = mini(tmp_path / "02", changes)
    problem = f"fhir/mapping.yaml: {where} use spec 0.3 mapping features; set spec_version: '0.3'"
    origins: tuple[Origin, ...] = ("hub", "local")
    for origin in origins:
        with pytest.raises(PackValidationError) as refused:
            load_pack(root, origin=origin)
        assert refused.value.problems == [problem], origin
    assert main(["validate", str(root)]) != 0
    assert problem in capsys.readouterr().err
    adopted = mini(tmp_path / "03", changes, spec="0.3")
    for origin in origins:
        mapping = FhirAdapter.for_pack(load_pack(adopted, origin=origin)).mapping
        assert newer_keys(mapping) == where, origin


@pytest.mark.parametrize("window", [365, None])
def test_a_0_3_criteria_key_in_a_0_2_pack_is_refused_at_load(
    tmp_path: Path, window: int | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`current_within_days` on a medication check is a 0.3 key (spec 0.3 §3): in a 0.2 pack's
    criteria file, a plain variant's or a composite member's, it is refused by `load_pack`
    wherever the pack comes from, naming the file and each key, and by `mendari validate`. A key
    counts when it is written, whatever it holds (`null` too). As a 0.3 pack it loads."""
    check = {"medication": {"names": ["metformin"], "current_within_days": window, "then": "yes"}}
    where = "questions.t2d_diagnosis.checks[2].medication.current_within_days"

    def written(spec: str, *, member: bool) -> Path:
        root = mini(tmp_path / f"{spec}-{member}", {}, spec=spec)
        criteria = yaml.safe_load((root / "logic" / "criteria.yaml").read_text())
        criteria["questions"]["t2d_diagnosis"]["checks"].append(check)
        (root / "logic" / "criteria.yaml").write_text(yaml.safe_dump(criteria, sort_keys=False))
        if member:  # the criteria variant only as both members of a composite
            manifest = yaml.safe_load((root / "agent.yaml").read_text())
            slot = manifest["providers"]["structured"]
            plain = slot["variants"].pop("criteria")
            escalate = dict.fromkeys(slot["answers"], "unknown")
            members = {"a": plain, "b": dict(plain)}
            slot["variants"]["both"] = {"type": "any_of", "escalate": escalate, "members": members}
            slot["use"] = "both"
            (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
        return root

    problem = f"logic/criteria.yaml: [{where!r}] use spec 0.3 criteria features"
    origins: tuple[Origin, ...] = ("hub", "local")
    for member in (False, True):
        root = written("0.2", member=member)
        for origin in origins:
            with pytest.raises(PackValidationError) as refused:
                load_pack(root, origin=origin)
            assert any(problem in found for found in refused.value.problems), (member, origin)
        assert main(["validate", str(root)]) != 0
        assert problem in capsys.readouterr().err
        adopted = written("0.3", member=member)
        for origin in origins:
            assert load_pack(adopted, origin=origin).warnings == ()


def test_a_0_1_pack_with_a_0_3_key_is_refused_too(tmp_path: Path) -> None:
    """The inbox pack without its 0.2 features is a 0.1 pack: its mapping reads every golden case
    as the 0.2 pack does, byte for byte, and with a 0.3 key it is refused at load as a 0.2 pack
    is, though the deployment owns it."""
    root = inbox_at_0_1(tmp_path / "plain")
    pack = load_pack(root)
    assert pack.manifest.spec_version == "0.1"
    adapter = FhirAdapter.for_pack(pack)
    pinned = json.loads(script.GOLDEN.read_text(encoding="utf-8"))["packs"]
    want = {
        case["id"]: case["input_hash"] for case in pinned["mendari/patient-inbox-router"]["cases"]
    }
    _, dataset, build = script.CASES["mendari/patient-inbox-router"]
    with script.on_the_day():
        for record in load_dataset(ROOT / "tests" / "fixtures" / "datasets" / dataset).records:
            bundle = {"resourceType": "Bundle", "type": "collection", "entry": build(record)}
            assert input_hash(adapter.to_input(adapter.index(bundle))) == want[record["id"]]

    newer = inbox_at_0_1(tmp_path / "newer")
    mapping = yaml.safe_load((newer / "fhir" / "mapping.yaml").read_text())
    mapping["fields"]["message.body"]["paths"] = [mapping["fields"]["message.body"].pop("path")]
    (newer / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping, sort_keys=False))
    problem = "['fields.message.body.paths'] use spec 0.3 mapping features"
    with pytest.raises(PackValidationError, match=re.escape(problem)):
        load_pack(newer)


def inbox_at_0_1(root: Path) -> Path:
    """A copy of the inbox pack as spec 0.1: its 0.2 features (a `min_probability` rule,
    question `display` and `names`, `card.provenance`) left out."""
    shutil.copytree(INBOX_DIR, root, ignore=shutil.ignore_patterns("data", "evals"))
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["spec_version"] = "0.1"
    manifest["card"].pop("provenance")
    for question in manifest["questions"].values():
        question.pop("display", None)
        question.pop("names", None)
    manifest.pop("evaluation", None)
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    policy = yaml.safe_load((root / "policy.yaml").read_text())
    policy["rules"] = [r for r in policy["rules"] if "min_probability" not in r["when"]]
    (root / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False))
    return root


# The reference packs on spec 0.3 since LP-23k: their mappings read real EHR shapes.
ON_0_3 = {
    "mendari/clinical-trial-prescreener",
    "mendari/prior-auth-preflight",
    "mendari/referral-router",
}


def test_every_0_1_and_0_2_pack_still_loads_and_maps_identically() -> None:
    """Every pack Mendari ships or tests with is 0.1 or 0.2 and loads as it did, its mapping
    held to bare placeholders and finding no 0.3 key, but the three reference packs LP-23k
    moved to spec 0.3, which load held to its bounds and bare placeholders too; and the four
    reference packs with a FHIR mapping decide every case of the 23a golden as it says, input
    hash included (the mapping read the same input, byte for byte)."""
    roots = fixture_agents()
    mapped = 0
    newer = set()
    for root in roots:
        origin: Origin = "community" if root.parent.name == "community-agents" else "local"
        pack = load_pack(root, origin=origin)
        if pack.manifest.spec_version == "0.3":
            newer.add(pack.id)
            assert newer_keys(FhirAdapter.for_pack(pack, bare=True).mapping), root
            mapped += 1
            continue
        if pack.id == "mendari-test/actions-mini":  # the 0.4 fixture (tests/test_spec_04.py)
            assert pack.manifest.spec_version == "0.4" and pack.manifest.inputs.fhir is None
            continue
        assert pack.manifest.spec_version in ("0.1", "0.2"), root
        if pack.manifest.inputs.fhir is not None:
            assert newer_keys(FhirAdapter.for_pack(pack, bare=True).mapping) == [], root
            mapped += 1
    assert newer == ON_0_3
    assert mapped == 6  # the four reference packs and two fixtures
    pinned = script.GOLDEN.read_text(encoding="utf-8")
    assert script.render(script.golden()) == pinned


def test_a_0_3_manifest_is_a_0_2_manifest() -> None:
    """0.3 adds no manifest key: the inbox's manifest is valid as 0.3, and an engine refuses a
    version it does not know, as an engine that reads only 0.2 refuses a 0.3 pack."""
    raw = yaml.safe_load((INBOX_DIR / "agent.yaml").read_text())
    raw["policy"] = yaml.safe_load((INBOX_DIR / "policy.yaml").read_text())
    assert PackManifest.model_validate({**raw, "spec_version": "0.3"}).spec_version == "0.3"
    assert SPEC_VERSION == "0.4"  # the newest this engine reads (tests/test_spec_04.py)
    with pytest.raises(ValidationError, match="spec_version"):
        PackManifest.model_validate({**raw, "spec_version": "0.5"})
