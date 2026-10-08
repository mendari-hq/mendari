# SPDX-License-Identifier: Apache-2.0
"""What code systems the repository's data carries (LP-14). CPT, whose codes the American Medical
Association licenses, appears in no tracked pack, dataset or fixture: the prior-authorization
pack's procedure codes, everywhere it or its data uses one, are fictional. Nothing a package is
built from (src/, packs/, datasets/) carries SNOMED CT: the trial prescreener's condition codes,
its local-site example's and the referral router's referral code are fictional too, and SNOMED
CT's url or OID is named only where Mendari's code must name the system (scripts/check_snomed.py,
which CI also runs on the built wheel and sdist). A licensed system (`LICENSED_CODE_SYSTEMS`)
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
from mendari.sandbox import synthetic_prior_auth, synthetic_trial
from mendari.sandbox.fhir_records import REFERRAL, REFERRAL_SYSTEM, REFERRALS
from mendari.sandbox.synthetic_prior_auth import PROCEDURE_SYSTEM as FICTIONAL_PROCEDURES
from mendari.sandbox.synthetic_prior_auth import PROCEDURES
from mendari.sandbox.synthetic_trial import CONDITION_SYSTEM, CONDITIONS
from mendari.standards.record_fhir import _ICD10_CODE
from mendari.standards.terminology import sandbox_resources

ROOT = Path(__file__).resolve().parents[1]
TREES = ("packs", "community-packs", "datasets", "tests/fixtures")
SHIPPED = ("src", "packs", "datasets")  # what the wheel, the sdist and the images are built from
CPT = "http://www.ama-assn.org/go/cpt"
SNOMED = "http://snomed.info/sct"
TRIAL = ROOT / "packs" / "clinical-trial-prescreener"
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
    # The manifest's words (criteria, Jev instructions) name no five-digit procedure code, as
    # 0.1.3's "MRI of the lumbar spine (CPT 72148, ...)" did, and neither it nor the input schema
    # calls a code CPT.
    manifest = (pack / "pack.yaml").read_text()
    assert re.findall(r"(?<![\d.])\d{5}(?![\d.])", manifest) == []
    for name in ("pack.yaml", "schemas/input.schema.json"):
        text = (pack / name).read_text()
        assert len(re.findall(r"\bCPT\b", text)) == len(re.findall(r"\bnot CPT\b", text)), name


def test_nothing_a_package_is_built_from_carries_snomed_ct() -> None:
    """No SNOMED CT concept id, concept URI, url or OID in src/, packs/ or datasets/ (what the
    wheel, the sdist and the images carry), but the url or OID where the code must name the
    system. CI runs the same check on the built wheel and sdist (scripts/check_snomed.py)."""
    check = check_snomed()
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
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("mendari/_datasets/a.jsonl", '{"code": "44054006"}\n')
        archive.writestr("mendari/_packs/b/fhir.json", '{"system": "http:\\/\\/snomed.info\\/sct"}')
        archive.writestr("mendari/c.py", 'OID = "2.16.840.1.113883.6.96"\n')
        archive.writestr("mendari/d.md", "see http://snomed.info/id/3457005 for the concept\n")
        archive.writestr("mendari/core/spec.py", '"http://snomed.info/sct"\n')  # allowed
        archive.writestr("mendari/e.json", '{"p": 0.3457005, "sha": "a44054006f", "n": 13457005}')
    found = [line for name, text in check.members(wheel) for line in check.findings(name, text)]
    assert found == [
        "mendari/_datasets/a.jsonl: SNOMED CT concept id 44054006",
        "mendari/_packs/b/fhir.json: names SNOMED CT (its url, OID or a concept URI)",
        "mendari/c.py: names SNOMED CT (its url, OID or a concept URI)",
        "mendari/d.md: SNOMED CT concept id 3457005",
        "mendari/d.md: a SNOMED CT concept URI",
        "mendari/d.md: names SNOMED CT (its url, OID or a concept URI)",
    ]
    # An sdist's members sit under its root folder and src/; the allowance follows the module.
    allowed = "mendari-0.1.0/src/mendari/core/spec.py"
    assert check.package_path(allowed) == "mendari/core/spec.py"
    assert check.findings(allowed, '"http://snomed.info/sct"') == []
    assert check.findings("mendari-0.1.0/packs/x/spec.py", '"http://snomed.info/sct"') != []


def test_the_condition_and_referral_codes_are_fictional_and_complete() -> None:
    """Every condition code the trial pack's rules, CQL, generator and local-site example use
    that is not ICD-10-CM is a fictional condition code, and the referral router's FHIR focus is
    the fictional referral code; both systems are declared, named as fictional and not SNOMED
    CT, and published to the validator."""
    declared: dict[str, str] = {}
    for pack in (TRIAL, ROOT / "packs" / "referral-router"):
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
        for line in (ROOT / "datasets" / "trial-local-site-example.jsonl").read_text().splitlines()
        for condition in json.loads(line)["input"].get("conditions", [])
    }
    generated = {code for codes in synthetic_trial.LOCAL.values() for code in codes}
    assert {code for code in used if not _ICD10_CODE.match(code)} == generated
    assert generated <= set(CONDITIONS)
    mapping = yaml.safe_load(
        (ROOT / "packs" / "referral-router" / "fhir" / "mapping.yaml").read_text()
    )
    assert mapping["focus_where"]["in"] == [REFERRAL["code"]] and REFERRAL["code"] in REFERRALS
    published = {r["url"]: {c["code"] for c in r["concept"]} for r in sandbox_resources()}
    assert published[CONDITION_SYSTEM] == set(CONDITIONS)
    assert published[REFERRAL_SYSTEM] == set(REFERRALS)
