# SPDX-License-Identifier: Apache-2.0
"""What code systems the repository's data carries (LP-14). CPT, whose codes the American Medical
Association licenses, appears in no tracked agent, dataset or fixture. Nothing a package or an
image is built from (src/, agents/, NOTICE, licenses/image) carries SNOMED CT, and SNOMED CT's url
or OID is named only where Mendari's code must name the system (scripts/check_snomed.py, which CI
also runs on the built wheel and sdist). A licensed system (`LICENSED_CODE_SYSTEMS`) named by its
url or OID appears only in an agent that declares it with its licence terms (`code_systems`,
checked at load), or, for SNOMED CT alone, in a US Core test input (US Core binds some codes to
it). Whether the repository's tests may keep SNOMED CT is recorded for counsel (tasks/todo.md,
LP-14)."""

from __future__ import annotations

import importlib.util
import subprocess
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from mendari.core.pack import load_pack
from mendari.core.spec import licensed_systems
from tests.conftest import FIXTURE_AGENTS

ROOT = Path(__file__).resolve().parents[1]
TREES = ("agents", "community-agents", "tests/fixtures")
# What the wheel, the sdist and the images are built from: the wheel from src/ and agents/; the
# sdist adds README.md, LICENSE, NOTICE and pyproject.toml; the images copy the same trees and
# carry /licenses (#180): LICENSE, NOTICE and what the build writes from licenses/image (its
# SOURCE.md, the components' and crates' licence texts).
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
# Test inputs in US Core, which codes procedures and referrals in SNOMED CT. None is in a package.
US_CORE_INPUTS = "tests/fixtures/us-core/"


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


def agent_of(relative: str) -> Path | None:
    """The folder of the agent a tracked file is part of (the nearest folder above it holding an
    `agent.yaml`, within the trees scanned), or None."""
    path = (ROOT / relative).parent
    while path != ROOT and path.is_relative_to(ROOT):
        if (path / "agent.yaml").is_file():
            return path
        path = path.parent
    return None


def test_cpt_is_in_no_pack_dataset_or_fixture() -> None:
    assert [path for path, systems in named().items() if CPT in systems] == []


def test_a_licensed_system_is_only_where_it_is_declared_or_allowed() -> None:
    for path, systems in named().items():
        if path.startswith(US_CORE_INPUTS):
            assert systems == {SNOMED}, path  # a US Core input may code in SNOMED CT, only
            continue
        # An agent may carry one it declares (CONTRIBUTING.md); a dataset or a fixture outside an
        # agent may not.
        root = agent_of(path)
        assert root is not None, f"{path} names {systems}"
        manifest = load_pack(root).manifest
        declared = {use.url: use.licence_note for use in manifest.code_systems}
        for system in systems:
            assert declared.get(system), f"{path}: {system} without a licence_note"


@pytest.mark.parametrize("pack", sorted(p.parent.name for p in FIXTURE_AGENTS.glob("*/agent.yaml")))
def test_every_reference_pack_says_its_licence_and_loads(pack: str) -> None:
    manifest = load_pack(FIXTURE_AGENTS / pack).manifest
    assert manifest.license == "Apache-2.0"
    for use in manifest.code_systems:
        assert (use.licence_note is not None) == bool(licensed_systems(use.url)), use


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
