# SPDX-License-Identifier: Apache-2.0
"""What code systems the repository's data carries (LP-14). CPT, whose codes the American Medical
Association licenses, appears in no tracked pack, dataset or fixture: the prior-authorization
pack's procedure codes, everywhere it or its data uses one, are fictional. Nothing a package or
an image is built from (src/, packs/, datasets/, NOTICE, licenses/image) carries SNOMED CT: the
trial prescreener's condition codes, its local-site example's and the referral router's referral
code are fictional too, and SNOMED CT's url or OID is named only where Mendari's code must name
the system (scripts/check_snomed.py, which CI also runs on the built wheel and sdist). A
licensed system (`LICENSED_CODE_SYSTEMS`)
named by its url or OID appears only in a pack that declares it with its licence terms
(`code_systems`, checked at load) or in a US Core fixture listed here with why. Whether the
repository's tests may keep SNOMED CT is recorded for counsel (tasks/todo.md, LP-14)."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import zipfile
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from mendari.core.pack import load_pack
from mendari.core.spec import LICENSED_CODE_SYSTEMS, licensed_systems
from mendari.sandbox.code_systems import (
    CONDITION_SYSTEM,
    CONDITIONS,
    PROCEDURES,
    REFERRAL,
    REFERRAL_SYSTEM,
    REFERRALS,
)
from mendari.sandbox.code_systems import PROCEDURE_SYSTEM as FICTIONAL_PROCEDURES
from mendari.standards import record_fhir
from mendari.standards.record_fhir import _ICD10_CODE
from mendari.standards.terminology import sandbox_resources
from tests.support import synthetic_prior_auth, synthetic_trial

ROOT = Path(__file__).resolve().parents[1]
TREES = ("agents", "community-packs", "tests/fixtures")
# What the wheel, the sdist and the images are built from: the wheel from src/, packs/ and
# datasets/; the sdist adds README.md, LICENSE, NOTICE and pyproject.toml; the images copy the
# same trees and carry /licenses (#180): LICENSE, NOTICE and what the build writes from
# licenses/image (its SOURCE.md, the components' and crates' licence texts).
SHIPPED = (
    "src",
    "agents",
    "README.md",
    "LICENSE",
    "NOTICE",
    "pyproject.toml",
    "licenses/image",
)
CPT = "http://www.ama-assn.org/go/cpt"
SNOMED = "http://snomed.info/sct"
TRIAL = ROOT / "tests" / "fixtures" / "reference-packs" / "clinical-trial-prescreener"
# Test inputs in US Core, which codes procedures and referrals in SNOMED CT. None is in a package.
FIXTURE_ALLOWANCES = {
    "tests/fixtures/us-core/referral.json": "the referral router's input: the referral's code "
    "(US Core binds ServiceRequest.code to procedure codes)",
    "tests/fixtures/us-core/invalid/referral.json": "the same referral, made invalid on purpose",
    "tests/fixtures/us-core/prior-auth.json": "the physical therapy the patient had, a US Core "
    "Procedure the prior-auth pack never reads the system of",
    "tests/fixtures/us-core/invalid/records.json": "a Procedure and a ServiceRequest made invalid "
    "on purpose, as US Core codes them",
}


def tracked(trees: tuple[str, ...] = TREES) -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", *trees], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [ROOT / path for path in listed.split("\0") if path]


def check_snomed() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_snomed", ROOT / "scripts/check_snomed.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert found  # the US Core fixtures still code in SNOMED CT
    for path, systems in found.items():
        if path.startswith("tests/fixtures/us-core/"):
            assert path in FIXTURE_ALLOWANCES, f"{path} names {systems}: allow it here with why"
            assert systems == {SNOMED}, path
            continue
        # A reference or community pack may carry one it declares (CONTRIBUTING.md); a dataset
        # or a fixture outside a pack may not.
        assert path.startswith(("packs/", "community-packs/")), f"{path} names {systems}"
        manifest = load_pack(ROOT / "/".join(path.split("/")[:2])).manifest
        declared = {use.url: use.licence_note for use in manifest.code_systems}
        for system in systems:
            assert declared.get(system), f"{path}: {system} without a licence_note"
    assert set(FIXTURE_ALLOWANCES) <= set(found), "an allowance no file needs: drop it"


@pytest.mark.parametrize(
    "pack",
    sorted(
        p.parent.name for p in (ROOT / "tests" / "fixtures" / "reference-packs").glob("*/pack.yaml")
    ),
)
def test_every_reference_pack_says_its_licence_and_loads(pack: str) -> None:
    manifest = load_pack(ROOT / "tests" / "fixtures" / "reference-packs" / pack).manifest
    assert manifest.license == "Apache-2.0"
    for use in manifest.code_systems:
        assert (use.licence_note is not None) == bool(licensed_systems(use.url)), use


def test_the_prior_auth_codes_are_fictional_and_complete() -> None:
    """Every procedure code the pack, its data and its tests use is in the fictional system, and
    the system is declared, named as fictional, and published to the validator."""
    pack = load_pack(ROOT / "tests" / "fixtures" / "reference-packs" / "prior-auth-preflight")
    (fictional,) = [use for use in pack.manifest.code_systems if use.url == FICTIONAL_PROCEDURES]
    assert "fictional" in fictional.name and "not CPT" in fictional.name
    text = "\n".join(
        (
            ROOT / "tests" / "fixtures" / "reference-packs" / "prior-auth-preflight" / name
        ).read_text()
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
    pack = ROOT / "tests" / "fixtures" / "reference-packs" / "prior-auth-preflight"
    found: set[str] = set()
    for path in [
        *sorted((pack / "data").glob("*.jsonl")),
        ROOT / "tests" / "fixtures" / "datasets" / "prior-auth-local-site-example.jsonl",
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
    # The manifest's words (criteria, Jev instructions) name no five-digit procedure code, as
    # 0.1.3's "MRI of the lumbar spine (CPT 72148, ...)" did, and neither it nor the input schema
    # calls a code CPT.
    manifest = (pack / "pack.yaml").read_text()
    assert re.findall(r"(?<![\d.])\d{5}(?![\d.])", manifest) == []
    for name in ("pack.yaml", "schemas/input.schema.json"):
        text = (pack / name).read_text()
        assert len(re.findall(r"\bCPT\b", text)) == len(re.findall(r"\bnot CPT\b", text)), name


def test_nothing_a_package_is_built_from_carries_snomed_ct() -> None:
    """No SNOMED CT concept id (one the repository has used, or, but in the vendored swagger-ui,
    any other number written as one), concept URI, url or OID in what the wheel, the sdist and
    the images carry (`SHIPPED`), but the url or OID where the code must name the system. CI runs
    the same check on the built wheel and sdist (scripts/check_snomed.py)."""
    check = check_snomed()
    assert all(tracked((tree,)) for tree in SHIPPED), "a path in SHIPPED has no tracked file"
    found = []
    for path in tracked(SHIPPED):
        if path.is_file():
            name = path.relative_to(ROOT).as_posix()
            found += check.findings(name, path.read_text(encoding="utf-8", errors="replace"))
    assert found == []
    for module in check.NAMES_THE_SYSTEM:
        text = (ROOT / "src" / module).read_text()
        assert SNOMED in licensed_systems(text), f"{module} names no SNOMED CT: drop it"


def test_the_package_scan_finds_snomed_ct_however_it_is_written(tmp_path: Path) -> None:
    check = check_snomed()
    wheel = tmp_path / "mendari-0.1.0-py3-none-any.whl"
    shaped = "is written as a SNOMED CT concept id (a concept's partition and check digit); if it "
    shaped += "is not one, add it to NOT_CONCEPT_IDS in scripts/check_snomed.py with why"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("mendari/_datasets/a.jsonl", '{"code": "44054006"}\n')
        archive.writestr("mendari/_packs/b/fhir.json", '{"system": "http:\\/\\/snomed.info\\/sct"}')
        archive.writestr("mendari/c.py", 'OID = "2.16.840.1.113883.6.96"\n')
        archive.writestr("mendari/d.md", "see http://snomed.info/id/3457005 for the concept\n")
        archive.writestr("mendari/core/spec.py", '"http://snomed.info/sct"\n')  # allowed
        archive.writestr("mendari/e.json", '{"p": 0.3457005, "sha": "a44054006f", "n": 134570051}')
        # Any id, not only the ones the repository has used: diabetes mellitus and hypertension.
        archive.writestr("mendari/_packs/f/rules.yaml", 'codes: ["E11*", "73211009", "38341003"]')
        archive.writestr("mendari/g.md", "Coded 3457005. Or 73211008, whose check digit is wrong.")
        # The vendored code's numbers are not codes, but a known id is found there too.
        archive.writestr("mendari/dashboard/vendor/h.js", "x=106217008;y=808000;z=22298006")
        archive.writestr("mendari-0.1.0.dist-info/RECORD", "mendari/i.py,sha256=x,106217008\n")
    found = [line for name, text in check.members(wheel) for line in check.findings(name, text)]
    assert found == [
        "mendari/_datasets/a.jsonl: SNOMED CT concept id 44054006",
        "mendari/_packs/b/fhir.json: names SNOMED CT (its url, OID or a concept URI)",
        "mendari/c.py: names SNOMED CT (its url, OID or a concept URI)",
        "mendari/d.md: SNOMED CT concept id 3457005",
        "mendari/d.md: a SNOMED CT concept URI",
        "mendari/d.md: names SNOMED CT (its url, OID or a concept URI)",
        f"mendari/_packs/f/rules.yaml: 38341003 {shaped}",
        f"mendari/_packs/f/rules.yaml: 73211009 {shaped}",
        "mendari/g.md: SNOMED CT concept id 3457005",
        "mendari/dashboard/vendor/h.js: SNOMED CT concept id 22298006",
    ]
    # An sdist's members sit under its root folder and src/; the allowance follows the module.
    allowed = "mendari-0.1.0/src/mendari/core/spec.py"
    assert check.package_path(allowed) == "mendari/core/spec.py"
    assert check.findings(allowed, '"http://snomed.info/sct"') == []
    assert check.findings("mendari-0.1.0/agents/x/spec.py", '"http://snomed.info/sct"') != []
    # The sdist's and the tree's bundled agents are Mendari's own files as well.
    for name in ("mendari-0.1.0/agents/x/a.yaml", "agents/x/a.yaml"):
        assert check.findings(name, "73211009") == [f"{name}: 73211009 {shaped}"]


def test_a_concept_id_is_told_by_its_partition_and_check_digit() -> None:
    check = check_snomed()
    assert all(check.is_concept_id(code) for code in check.CONCEPT_IDS)
    assert check.is_concept_id("73211009") and check.is_concept_id("38341003")
    # RxNorm's metformin, a 32-bit limit, a wrong check digit; with a valid check digit, a
    # description's partition (01), a leading zero, 5 digits and 19.
    for number in (
        "860975",
        "2147483647",
        "73211008",
        "12345013",
        "04405409",
        "12006",
        "1111111111111111008",
    ):
        assert not check.is_concept_id(number), number
    for number, why in check.NOT_CONCEPT_IDS.items():
        assert check.is_concept_id(number) and why, f"{number}: not needed, drop it"


def test_the_condition_and_referral_codes_are_fictional_and_complete() -> None:
    """Every condition code the trial pack's rules, CQL, generator and local-site example use
    that is not ICD-10-CM is a fictional condition code, and the referral router's FHIR focus is
    the fictional referral code; both systems are declared, named as fictional and not SNOMED
    CT, and published to the validator."""
    declared: dict[str, str] = {}
    for pack in (TRIAL, ROOT / "tests" / "fixtures" / "reference-packs" / "referral-router"):
        declared |= {use.url: use.name for use in load_pack(pack).manifest.code_systems}
    for system in (CONDITION_SYSTEM, REFERRAL_SYSTEM):
        assert "fictional" in declared[system] and "not SNOMED CT" in declared[system]
        assert system not in LICENSED_CODE_SYSTEMS
    assert all(re.fullmatch(r"FC\d{3}", code) for code in CONDITIONS)
    assert all(re.fullmatch(r"FR\d{3}", code) for code in REFERRALS)
    rules = yaml.safe_load((TRIAL / "logic" / "criteria.yaml").read_text())
    listed = {
        code
        for question in rules["questions"].values()
        for check in question["checks"]
        for code in check.get("condition", {}).get("codes", [])
        if not code.endswith("*")
    }
    cql = set(re.findall(r"'(FC\d{3})'", (TRIAL / "logic" / "criteria.cql").read_text()))
    assert listed == cql == set(CONDITIONS)
    used = {
        condition["code"]
        for line in (ROOT / "tests" / "fixtures" / "datasets" / "trial-local-site-example.jsonl")
        .read_text()
        .splitlines()
        for condition in json.loads(line)["input"].get("conditions", [])
    }
    generated = {code for codes in synthetic_trial.LOCAL.values() for code in codes}
    assert {code for code in used if not _ICD10_CODE.match(code)} == generated
    assert generated <= set(CONDITIONS)
    mapping = yaml.safe_load(
        (
            ROOT
            / "tests"
            / "fixtures"
            / "reference-packs"
            / "referral-router"
            / "fhir"
            / "mapping.yaml"
        ).read_text()
    )
    # The referral router's FHIR focus is its code set `referral_category` (spec 0.3, LP-23k).
    assert mapping["focus_where"]["code_set"] == "referral_category"
    category = mapping["code_sets"]["referral_category"]
    assert category == {"system": REFERRAL_SYSTEM, "codes": [REFERRAL["code"]]}
    assert REFERRAL["code"] in REFERRALS
    published = {r["url"]: {c["code"] for c in r["concept"]} for r in sandbox_resources()}
    assert published[CONDITION_SYSTEM] == set(CONDITIONS)
    assert published[REFERRAL_SYSTEM] == set(REFERRALS)


def test_each_local_condition_code_says_what_its_record_says() -> None:
    """A record's condition text becomes its FHIR coding's display (record_fhir), so each
    fictional code the local site writes has, in the published CodeSystem, the display its text
    says: the generator's codes, and every coding of the local-site example as FHIR."""
    for event, codes in synthetic_trial.LOCAL.items():
        for code in codes:
            assert CONDITIONS[code] == synthetic_trial.TEXT[event], (event, code)
    (published,) = [r for r in sandbox_resources() if r["url"] == CONDITION_SYSTEM]
    display = {concept["code"]: concept["display"] for concept in published["concept"]}
    codings = [
        coding
        for line in (ROOT / "tests" / "fixtures" / "datasets" / "trial-local-site-example.jsonl")
        .read_text()
        .splitlines()
        for entry in record_fhir.bundle(json.loads(line)["input"], synthetic_trial.AS_OF)["entry"]
        if entry["resource"]["resourceType"] == "Condition"
        for coding in entry["resource"].get("code", {}).get("coding", [])
        if coding["system"] == CONDITION_SYSTEM
    ]
    assert len(codings) >= 150
    assert [c for c in codings if c.get("display") != display[c["code"]]] == []


def test_a_code_with_a_trailing_line_break_is_not_read_as_icd10() -> None:
    """`$` matches before a final newline: `E11.9\\n` was coded as ICD-10-CM, a code no system
    holds, in a Condition the record builds."""
    assert record_fhir.condition_system("E11.9") == record_fhir.ICD10
    assert record_fhir.condition_system("E11.9\n") != record_fhir.ICD10
