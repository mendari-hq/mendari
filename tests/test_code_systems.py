# SPDX-License-Identifier: Apache-2.0
"""What code systems the repository's data carries (LP-14). CPT, whose codes the American Medical
Association licenses, appears in no tracked pack, dataset or fixture: the prior-authorization
pack's procedure codes, everywhere it or its data uses one, are fictional. A licensed system
(`LICENSED_CODE_SYSTEMS`) named by its url or OID appears only in a pack that declares it with its
licence terms (`code_systems`, checked at load) or in a US Core fixture listed here with why. A
bare code names no system, so the scan cannot see one: datasets/trial-local-site-example.jsonl
carries bare SNOMED CT codes (the trial pack's local-site example). Whether SNOMED CT may stay is
recorded for counsel (tasks/todo.md, LP-14)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from mendari.core.pack import load_pack
from mendari.core.spec import LICENSED_CODE_SYSTEMS, licensed_systems
from mendari.sandbox import synthetic_prior_auth
from mendari.sandbox.synthetic_prior_auth import PROCEDURE_SYSTEM as FICTIONAL_PROCEDURES
from mendari.sandbox.synthetic_prior_auth import PROCEDURES

ROOT = Path(__file__).resolve().parents[1]
TREES = ("packs", "community-packs", "datasets", "tests/fixtures")
CPT = "http://www.ama-assn.org/go/cpt"
SNOMED = "http://snomed.info/sct"
# Test inputs in US Core, which codes procedures and referrals in SNOMED CT. None is in a package.
FIXTURE_ALLOWANCES = {
    "tests/fixtures/us-core/referral.json": "the referral router's input: the referral is "
    "SNOMED CT 3457005, which the pack declares",
    "tests/fixtures/us-core/invalid/referral.json": "the same referral, made invalid on purpose",
    "tests/fixtures/us-core/prior-auth.json": "the physical therapy the patient had, a US Core "
    "Procedure the prior-auth pack never reads the system of",
    "tests/fixtures/us-core/invalid/records.json": "a Procedure and a ServiceRequest made invalid "
    "on purpose, as US Core codes them",
}


def tracked() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", *TREES], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [ROOT / path for path in listed.split("\0") if path]


def named() -> dict[str, set[str]]:
    """Each tracked data file that names a licensed code system, with the systems it names."""
    found = {}
    for path in tracked():
        if path.is_file():
            systems = licensed_systems(path.read_text(encoding="utf-8", errors="replace"))
            if systems:
                found[path.relative_to(ROOT).as_posix()] = systems
    return found


def test_cpt_is_in_no_pack_dataset_or_fixture() -> None:
    assert [path for path, systems in named().items() if CPT in systems] == []


def test_a_licensed_system_is_only_where_it_is_declared_or_allowed() -> None:
    found = named()
    assert found  # SNOMED CT is still carried, under its licence terms
    for path, systems in found.items():
        if path.startswith("tests/fixtures/us-core/"):
            assert path in FIXTURE_ALLOWANCES, f"{path} names {systems}: allow it here with why"
            assert systems == {SNOMED}, path
            continue
        assert path.startswith("packs/"), f"{path} names {systems} outside a pack"
        manifest = load_pack(ROOT / "/".join(path.split("/")[:2])).manifest
        declared = {use.url: use.licence_note for use in manifest.code_systems}
        for system in systems:
            assert declared.get(system), f"{path}: {system} without a licence_note"
    assert set(FIXTURE_ALLOWANCES) <= set(found), "an allowance no file needs: drop it"


@pytest.mark.parametrize(
    "pack", sorted(p.parent.name for p in (ROOT / "packs").glob("*/pack.yaml"))
)
def test_every_reference_pack_says_its_licence_and_loads(pack: str) -> None:
    manifest = load_pack(ROOT / "packs" / pack).manifest
    assert manifest.license == "Apache-2.0"
    for use in manifest.code_systems:
        assert (use.licence_note is not None) == bool(licensed_systems(use.url)), use


def test_the_prior_auth_codes_are_fictional_and_complete() -> None:
    """Every procedure code the pack, its data and its tests use is in the fictional system, and
    the system is declared, named as fictional, and published to the validator."""
    pack = load_pack(ROOT / "packs" / "prior-auth-preflight")
    (fictional,) = [use for use in pack.manifest.code_systems if use.url == FICTIONAL_PROCEDURES]
    assert "fictional" in fictional.name and "not CPT" in fictional.name
    text = "\n".join(
        (ROOT / "packs" / "prior-auth-preflight" / name).read_text()
        for name in ("logic/structured.yaml", "fhir/mapping.yaml", "tests/cases.yaml")
    )
    used = set(re.findall(r"\bFX\d{3}\b", text))
    assert used and used <= set(PROCEDURES)
    assert all(re.fullmatch(r"FX\d{3}", code) for code in PROCEDURES)
    assert not set(LICENSED_CODE_SYSTEMS) & {FICTIONAL_PROCEDURES}


def test_every_procedure_code_the_prior_auth_pack_or_its_data_uses_is_fictional() -> None:
    """Read as values, not spellings: every order and prior-procedure code in the pack's data,
    the local-site dataset and the behaviour tests, every code its rules and FHIR mapping list,
    and every code the generator writes is one of the fictional codes. A CPT code coming back
    (a bare `72148`, which names no system) fails here."""
    pack = ROOT / "packs" / "prior-auth-preflight"
    found: set[str] = set()
    for path in [
        *sorted((pack / "data").glob("*.jsonl")),
        ROOT / "datasets" / "prior-auth-local-site-example.jsonl",
    ]:
        for line in path.read_text().splitlines():
            record = json.loads(line).get("input", {})
            found.add(record["order"]["code"])
            found |= {procedure["code"] for procedure in record.get("procedures", [])}
    for case in yaml.safe_load((pack / "tests" / "cases.yaml").read_text()):
        found.add(case["input"]["order"]["code"])
        found |= {procedure["code"] for procedure in case["input"].get("procedures", [])}
    rules = yaml.safe_load((pack / "logic" / "structured.yaml").read_text())
    for question in rules["questions"].values():
        for check in question["checks"]:
            for kind in ("value", "exists"):
                if kind in check and check[kind].get("field") == "order.code":
                    found |= set(check[kind]["in"])
                if kind in check and check[kind].get("in") == "procedures":
                    found |= set(check[kind]["codes"])
            when = check.get("when", {})
            if when.get("field") == "order.code":
                found |= set(when["in"])
    found |= set(yaml.safe_load((pack / "fhir" / "mapping.yaml").read_text())["focus_where"]["in"])
    generated = [code for orders in synthetic_prior_auth.ORDERS.values() for code, _ in orders]
    generated += [code for code, _ in synthetic_prior_auth.IMAGING.values()]
    generated.append(synthetic_prior_auth.LOCAL_LUMBAR_XRAY[0])
    found |= set(generated)
    assert len(found) >= 15
    assert found <= set(PROCEDURES), sorted(found - set(PROCEDURES))
