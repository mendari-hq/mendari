# SPDX-License-Identifier: Apache-2.0
"""Spec 0.3 (LP-23g): only the FHIR mapping changes. A 0.1 or 0.2 pack that uses a 0.3 mapping
key is refused where its mapping is read (at load for a pack the deployment does not own, where
it is used for an owned one, and by `mendari validate`); every 0.1 and 0.2 pack still loads and
maps as the 23a golden says; and a 0.3 manifest is a 0.2 manifest."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import Decision, load_pack
from mendari.core.decision import input_hash
from mendari.core.origin import Origin
from mendari.core.spec import SPEC_VERSION, PackManifest
from mendari.errors import PackValidationError
from mendari.evals.harness import load_dataset
from mendari.evals.pack_tests import validate_pack
from mendari.standards.fhir import FhirAdapter, newer_keys
from tests.conftest import INBOX_DIR, ROOT
from tests.test_fhir_decisions import script

MINI = ROOT / "tests" / "fixtures" / "packs" / "prescreen-mini"
ICD = "http://hl7.org/fhir/sid/icd-10-cm"
SITE = "https://site.example/fhir/CodeSystem/conditions"  # a fictional site's own codes
PATIENT = {"resourceType": "Patient", "id": "p1", "birthDate": "1970-01-01"}
BUNDLE = {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": PATIENT}]}

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
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["spec_version"] = spec
    manifest["code_systems"] = [
        {"url": ICD, "name": "ICD-10-CM"},
        {"url": SITE, "name": "A fictional site's condition codes"},
        *({"url": url, "name": f"Fictional: {url}"} for url in systems),
    ]
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    mapping = yaml.safe_load((root / "fhir" / "mapping.yaml").read_text())
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(mapping.get(key), dict):
            mapping[key] = {**mapping[key], **value}
        else:
            mapping[key] = value
    (root / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping, sort_keys=False))
    return root


@pytest.mark.parametrize("key", sorted(NEWER))
def test_0_3_mapping_keys_in_a_0_2_pack_are_refused_at_load(tmp_path: Path, key: str) -> None:
    """Each 0.3 key in a 0.2 pack's mapping, named where it is: refused by `load_pack` for a pack
    the deployment does not own; an owned one loads, as an owned pack's mapping is read where it
    is used, and is refused there (the adapter, a FHIR decision) and by `mendari validate`. As a
    0.3 pack, the same mapping loads."""
    changes, where = NEWER[key]
    root = mini(tmp_path / "02", changes)
    problem = f"fhir/mapping.yaml: {where} use spec 0.3 mapping features; set spec_version: '0.3'"
    with pytest.raises(PackValidationError) as refused:
        load_pack(root, origin="hub")
    assert refused.value.problems == [problem]
    owned = load_pack(root)
    assert owned.warnings == ()
    for read in (
        lambda: FhirAdapter.for_pack(owned),
        lambda: Decision(owned).decide_fhir(BUNDLE),
        lambda: validate_pack(owned),
    ):
        with pytest.raises(PackValidationError, match=re.escape(problem)):
            read()
    adopted = mini(tmp_path / "03", changes, spec="0.3")
    assert newer_keys(FhirAdapter.for_pack(load_pack(adopted, origin="hub")).mapping) == where


def test_a_0_1_pack_with_a_0_3_key_is_refused_too(tmp_path: Path) -> None:
    """The inbox pack without its 0.2 features is a 0.1 pack: its mapping reads every golden case
    as the 0.2 pack does, byte for byte, and with a 0.3 key it is refused as a 0.2 pack is."""
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
        for record in load_dataset(ROOT / "datasets" / dataset).records:
            bundle = {"resourceType": "Bundle", "type": "collection", "entry": build(record)}
            assert input_hash(adapter.to_input(adapter.index(bundle))) == want[record["id"]]

    newer = inbox_at_0_1(tmp_path / "newer")
    mapping = yaml.safe_load((newer / "fhir" / "mapping.yaml").read_text())
    mapping["fields"]["message.body"]["paths"] = [mapping["fields"]["message.body"].pop("path")]
    (newer / "fhir" / "mapping.yaml").write_text(yaml.safe_dump(mapping, sort_keys=False))
    problem = "['fields.message.body.paths'] use spec 0.3 mapping features"
    with pytest.raises(PackValidationError, match=re.escape(problem)):
        FhirAdapter.for_pack(load_pack(newer))


def inbox_at_0_1(root: Path) -> Path:
    """A copy of the inbox pack as spec 0.1: its 0.2 features (a `min_probability` rule,
    question `display` and `names`, `card.provenance`) left out."""
    shutil.copytree(INBOX_DIR, root, ignore=shutil.ignore_patterns("data", "evals"))
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["spec_version"] = "0.1"
    manifest["card"].pop("provenance")
    for question in manifest["questions"].values():
        question.pop("display", None)
        question.pop("names", None)
    manifest.pop("evaluation", None)
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    policy = yaml.safe_load((root / "policy.yaml").read_text())
    policy["rules"] = [r for r in policy["rules"] if "min_probability" not in r["when"]]
    (root / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False))
    return root


def test_every_0_1_and_0_2_pack_still_loads_and_maps_identically() -> None:
    """Every pack Mendari ships or tests with is 0.1 or 0.2 and loads as it did, its mapping
    held to bare placeholders and finding no 0.3 key; and the four reference packs with a FHIR
    mapping decide every case of the 23a golden as it says, input hash included (the mapping
    read the same input, byte for byte)."""
    roots = [p.parent for p in sorted((ROOT / "packs").glob("*/pack.yaml"))]
    roots += sorted(p.parent for p in (ROOT / "tests" / "fixtures").glob("**/pack.yaml"))
    mapped = 0
    for root in roots:
        origin: Origin = "community" if root.parent.name == "community-packs" else "local"
        pack = load_pack(root, origin=origin)
        assert pack.manifest.spec_version in ("0.1", "0.2"), root
        if pack.manifest.inputs.fhir is not None:
            assert newer_keys(FhirAdapter.for_pack(pack, bare=True).mapping) == [], root
            mapped += 1
    assert mapped == 6  # the four reference packs and two fixtures
    pinned = script.GOLDEN.read_text(encoding="utf-8")
    assert script.render(script.golden()) == pinned


def test_a_0_3_manifest_is_a_0_2_manifest() -> None:
    """0.3 adds no manifest key: the inbox's manifest is valid as 0.3, and an engine refuses a
    version it does not know, as an engine that reads only 0.2 refuses a 0.3 pack."""
    raw = yaml.safe_load((INBOX_DIR / "pack.yaml").read_text())
    raw["policy"] = yaml.safe_load((INBOX_DIR / "policy.yaml").read_text())
    assert PackManifest.model_validate({**raw, "spec_version": "0.3"}).spec_version == "0.3"
    assert SPEC_VERSION == "0.3"
    with pytest.raises(ValidationError, match="spec_version"):
        PackManifest.model_validate({**raw, "spec_version": "0.4"})
