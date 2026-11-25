# SPDX-License-Identifier: Apache-2.0
"""The test fixtures' agents (`tests/support/fixture_agents.py` writes their generated files):
each committed file is what the generator writes, byte for byte, and each case's labels are the
facts it was made from; each fixture says what it is (synthetic, not clinically reviewed),
holds nothing that looks like a person's details, validates, passes its behaviour tests,
evaluates, and loads as an agent the deployment does not own would (a registry takes it).

The bundled demo agent's data is held to its own script the same way
(`scripts/demo_agent_data.py --check`, in CI's `demo-agent` job)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Decision, Outcome, Pack, load_pack
from mendari.cli import main as mendari
from mendari.core.pack import authored_patterns
from mendari.core.phi_detect import detect_files
from mendari.core.policy import GovernanceOverride
from mendari.core.registry import resolve_pack
from mendari.core.status import SYNTHETIC_EXAMPLE
from mendari.core.text import plain
from mendari.evals.harness import resolve_dataset
from mendari.providers import config_problems
from tests.conftest import (
    CLINICAL_DIR,
    FIXTURE_AGENTS,
    FIXTURE_NAMESPACE,
    ROOT,
    TRIAGE_DIR,
    Registry,
    shared_benchmark,
)
from tests.support import fixture_agents

FIXTURES = {fixture.name: fixture for fixture in fixture_agents.FIXTURES}
SYNTHETIC = "Synthetic test fixture."
NOT_REVIEWED = "Not clinically reviewed. Not for patient care."
APPROVED = GovernanceOverride(approved_by="the fixtures' tests", reference="no ticket")


def ids(folder: Path) -> str:
    return folder.name


# -- the generator --------------------------------------------------------------------------------


def test_the_committed_files_are_what_the_generator_writes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert fixture_agents.main(["--check"]) == 0
    said = capsys.readouterr().out.splitlines()
    written = sorted(path.relative_to(ROOT).as_posix() for path in fixture_agents.files())
    assert sorted(line.removeprefix("ok  ") for line in said) == written
    assert len(written) == 8  # six datasets and two cards


def test_the_command_writes_where_it_is_told_the_same_on_every_run(tmp_path: Path) -> None:
    """`python -m tests.support.fixture_agents`, the command a maintainer runs, in processes of
    their own with other hash seeds: the same bytes as the committed files, and `--check`
    finds a file that is not."""
    command = [sys.executable, "-m", "tests.support.fixture_agents"]
    for seed in ("0", "1"):
        out = tmp_path / f"seed-{seed}"
        ran = subprocess.run(
            [*command, "--root", str(out)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert ran.returncode == 0, ran.stderr
        for path in fixture_agents.files():
            relative = path.relative_to(ROOT)
            assert (out / relative).read_bytes() == path.read_bytes(), relative
    changed = tmp_path / "seed-0" / TRIAGE_DIR.relative_to(ROOT) / "data" / "test.jsonl"
    changed.write_text(changed.read_text(encoding="utf-8")[:-2] + "\n", encoding="utf-8")
    for out, failed in ((tmp_path / "seed-1", 0), (tmp_path / "seed-0", 1)):
        checked = subprocess.run(
            [*command, "--check", "--root", str(out)], cwd=ROOT, capture_output=True, text=True
        )
        assert checked.returncode == failed, checked.stderr
    assert "triage/data/test.jsonl is not what this module writes" in checked.stderr


def test_every_fixture_with_generated_files_is_one_the_tests_read() -> None:
    """Each generated fixture is a folder the engine reads in the tests, and the generator makes
    exactly the datasets its manifest declares, each where the manifest says."""
    assert {fixture.folder for fixture in fixture_agents.FIXTURES} == {TRIAGE_DIR, CLINICAL_DIR}
    for fixture in fixture_agents.FIXTURES:
        assert fixture.folder.parent == FIXTURE_AGENTS
        declared = load_pack(fixture.folder).manifest.evaluation.datasets
        assert declared == {name: f"data/{name}.jsonl" for name in fixture.counts}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_each_case_is_labelled_with_the_facts_it_was_made_from(name: str) -> None:
    """Labels come from the generator's own facts, never from a provider: each case's `kind`
    says which facts, and its labels are what that kind means. Each dataset holds the counts the
    generator names, no id twice and no input twice across the fixture's datasets."""
    fixture = FIXTURES[name]
    pack = load_pack(fixture.folder)
    seen: set[str] = set()
    for dataset, counts in fixture.counts.items():
        records = resolve_dataset(pack, dataset).records
        kinds = [record["attributes"]["kind"] for record in records]
        assert {kind: kinds.count(kind) for kind in counts} == dict(counts), dataset
        assert len(kinds) == sum(counts.values())
        assert len({record["id"] for record in records}) == len(records)
        for record in records:
            assert record["attributes"]["source"] == "synthetic"
            assert record["labels"] == expected_labels(name, record), record["id"]
            text = json.dumps(record["input"], sort_keys=True)
            assert text not in seen, record["id"]
            seen.add(text)


def expected_labels(name: str, record: dict[str, Any]) -> dict[str, str]:
    kind = record["attributes"]["kind"]
    if name == "clinical-review":
        _, _, _, severity, disposition = fixture_agents.NOTE_KINDS[kind]
        return {"disposition": disposition, "severity": severity}
    urgent = kind in fixture_agents.URGENT_KINDS
    if kind in fixture_agents.TEAMS:
        destination = kind
    elif urgent:
        destination = "nurse"
    else:  # a denied symptom or an insult: the request's team, asked in the message
        teams = [team for team in fixture_agents.TEAMS if asks(team, record["input"])]
        assert len(teams) == 1, record["id"]
        destination = teams[0]
    return {
        "destination": destination,
        "urgency": "yes" if urgent else "no",
        "abusive": "yes" if kind in ("abusive", "abusive_urgent") else "no",
    }


def asks(team: str, given: dict[str, Any]) -> bool:
    """Whether the message holds one of `team`'s request sentences, whatever fills it."""
    body = given["message"]["body"]
    for template in fixture_agents.REQUESTS[team]:
        head = template.split("{")[0]
        tail = template.split("}")[-1]
        if (head and head in body) and (not tail or tail in body):
            return True
    return False


# -- what each fixture is --------------------------------------------------------------------------


@pytest.mark.parametrize("folder", [TRIAGE_DIR, CLINICAL_DIR], ids=ids)
def test_each_fixture_says_it_is_synthetic_and_not_clinically_reviewed(folder: Path) -> None:
    pack = load_pack(folder)
    manifest = pack.manifest
    assert manifest.id.startswith(f"{FIXTURE_NAMESPACE}/") and "(test fixture)" in manifest.name
    assert manifest.summary.startswith(SYNTHETIC) and manifest.summary.endswith(NOT_REVIEWED)
    assert manifest.card.provenance == "synthetic"
    assert SYNTHETIC.lower().rstrip(".") in manifest.card.intended_use.lower()
    assert pack.status_label == SYNTHETIC_EXAMPLE
    assert SYNTHETIC_EXAMPLE == "Synthetic example. Not clinically reviewed. Not for patient care."
    card = (folder / "DECISION_CARD.md").read_text(encoding="utf-8").splitlines()
    assert card[2] == f"**{SYNTHETIC_EXAMPLE}**"
    assert SYNTHETIC in (folder / "CHANGELOG.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("folder", [TRIAGE_DIR, CLINICAL_DIR], ids=ids)
def test_no_file_of_a_fixture_looks_like_a_persons_details(folder: Path) -> None:
    """No finding of any kind, names included (`core/phi_detect.py`), in any of its files; and
    every text of its data and every pattern of its rules is in plain form already, so what is
    typed differently decides the same (`core/text.py`)."""
    files = {
        str(path.relative_to(ROOT)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }
    report = detect_files(files)
    assert report.findings == () and len(files) >= 9
    pack = load_pack(folder)
    for name in pack.manifest.evaluation.datasets:
        for record in resolve_dataset(pack, name).records:
            for text in texts(record["input"]):
                assert plain(text) == text and "  " not in text, record["id"]
    for where, pattern, _ in authored_patterns(pack):
        assert plain(pattern) == pattern, where


def texts(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [text for item in value.values() for text in texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in texts(item)]
    return [value] if isinstance(value, str) else []


@pytest.mark.parametrize("folder", [TRIAGE_DIR, CLINICAL_DIR], ids=ids)
def test_each_fixture_is_a_bundled_agent_and_would_load_from_a_registry(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by its id as the tests' bundled agents are (maintained), with no warning; and
    loaded as an agent the deployment does not own (its patterns on RE2, its schema's keywords
    and its caps), with no problem in any variant."""
    pack = load_pack(folder)
    found = resolve_pack(pack.id)
    assert (found.root, found.origin, found.digest) == (folder, "maintained", pack.digest)
    assert pack.warnings == () and not pack.bounded
    monkeypatch.setattr(Pack, "bounded", property(lambda self: True))
    bounded = load_pack(folder)
    assert bounded.bounded and config_problems(bounded) == []


@pytest.mark.parametrize("folder", [TRIAGE_DIR, CLINICAL_DIR], ids=ids)
def test_each_fixture_validates_passes_its_tests_and_evaluates(
    folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari validate`, `mendari test` and `mendari eval --dataset test`, as CI runs them on
    every fixture. Its keyword rules miss what they miss on purpose, and an evaluation says so;
    nothing is automated by default."""
    assert mendari(["validate", str(folder)]) == 0
    assert mendari(["test", str(folder)]) == 0
    said = capsys.readouterr().out
    assert " failed" in said and "0 failed" in said
    assert mendari(["eval", str(folder), "--dataset", "test", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    decisions = report["decisions"]
    (critical,) = decisions["critical"]
    if folder == TRIAGE_DIR:
        assert report["dataset"]["n"] == 300
        assert report["providers"]["router"]["variant"] == "rules"
        assert (critical["n"], critical["detected"], critical["auto_missed"]) == (66, 36, 0)
        assert decisions["outcome_counts"]["BLOCK"] == 24  # the insults without a symptom
    else:
        assert report["dataset"]["n"] == 120
        assert (critical["n"], critical["detected"], critical["auto_missed"]) == (36, 24, 0)
    assert decisions["outcome_counts"]["AUTO"] == 0


# -- the clinical review fixture: risk level 2 -----------------------------------------------------


def test_the_clinical_review_fixture_automates_only_under_an_approval(
    agent_copy: Any,
) -> None:
    """At risk level 2 nothing is automated without a governance approval. With one, a
    confident suggestion is: care at home, for no symptom or a mild one of a few days; a
    moderate or severe symptom goes to a person at priority urgent, and no case to be seen today
    is automated. Level 3 is as level 2; level 4 is never automated, approval or not."""
    pack = load_pack(CLINICAL_DIR)
    records = resolve_dataset(pack, "test").records
    default, approved = Decision(pack), Decision(pack, governance=APPROVED)
    assert not [r["id"] for r in records if default(r["input"]).outcome is Outcome.AUTO]
    automated = [r for r in records if approved(r["input"]).outcome is Outcome.AUTO]
    assert {r["attributes"]["kind"] for r in automated} == {"none", "mild_short"}
    assert all(r["labels"]["disposition"] == "self_care" for r in automated)
    for record in records:
        kind = record["attributes"]["kind"]
        if kind in ("moderate", "severe"):
            decided = approved(record["input"])
            assert (decided.outcome, decided.priority.value) == (Outcome.HUMAN_REVIEW, "urgent")
            assert decided.rule == "moderate-or-worse"
    mild = {"note": {"text": "Patient reports mild throat pain for two days."}}
    for level, outcome in ((3, Outcome.AUTO), (4, Outcome.HUMAN_REVIEW)):

        def at(manifest: dict[str, Any], level: int = level) -> None:
            manifest["risk"]["level"] = level

        copy = load_pack(agent_copy(manifest=at, agent=CLINICAL_DIR))
        assert Decision(copy, governance=APPROVED)(mild).outcome is outcome, level
        assert Decision(copy)(mild).outcome is Outcome.HUMAN_REVIEW


def test_the_severity_rule_reads_the_expected_score() -> None:
    """`min_score: 1.5`: a confident `moderate` (an expected score just under 2) matches it, a
    confident `mild` (about 1) does not."""
    triage = Decision(load_pack(CLINICAL_DIR))
    moderate = triage({"note": {"text": "Patient reports moderate back pain since yesterday."}})
    assert moderate.answers["severity"].label == "moderate"
    assert 1.5 <= (moderate.answers["severity"].value or 0) < 2
    assert moderate.rule == "moderate-or-worse"
    mild = triage({"note": {"text": "Patient reports slight knee pain since yesterday."}})
    assert (mild.answers["severity"].label, mild.rule) == ("mild", None)
    assert (mild.answers["severity"].value or 0) < 1.5


# -- the registry and a site's report --------------------------------------------------------------


@pytest.mark.parametrize("folder", [TRIAGE_DIR, CLINICAL_DIR], ids=ids)
def test_a_registry_takes_each_fixture(registry: Registry, folder: Path) -> None:
    """Published as it is, under its own namespace (`Registry.fixture_publisher`)."""
    pack = load_pack(folder)
    published = registry.publish(pack)
    assert published.status_code == 201, published.text
    namespace, name = pack.id.split("/")
    assert namespace == FIXTURE_NAMESPACE
    listed = registry.client.get(f"/v1/agents/{namespace}/{name}").json()
    assert listed["versions"] == [pack.version] and listed["latest"]["digest"] == pack.digest


def test_the_triage_fixtures_shared_benchmark_is_evaluated_once() -> None:
    """It commits no benchmark: what a site would share of a run of its `test` set, run once a
    session, and a copy each time."""
    pack = load_pack(TRIAGE_DIR)
    assert not (TRIAGE_DIR / "evals").exists()
    shared = shared_benchmark(pack.id)
    assert shared["format"] == "mendari.site-report/2"
    assert shared["pack"] == {"id": pack.id, "version": pack.version, "digest": pack.digest}
    assert shared["dataset"]["n"] == 300
    again = shared_benchmark(pack.id)
    assert again == shared and again is not shared


def test_the_fixtures_manifests_are_as_written() -> None:
    """Their providers' and policies' keys, which the tests that decide with them rely on."""
    triage = yaml.safe_load((TRIAGE_DIR / "agent.yaml").read_text(encoding="utf-8"))
    assert triage["policy"]["automation_requires"] == {"screen": ["agent", "checked"]}
    assert triage["policy"]["labels"] == {
        "billing": {"allow_auto": False},
        "front_desk": {"auto_threshold": 0.95},
    }
    screen = triage["providers"]["screen"]["variants"]
    assert screen["agent"]["agent_id"] == screen["repeated"]["agent_id"] == "triage-screen"
    assert screen["repeated"]["repeat"] == 3 and screen["checked"]["type"] == "check"
    for mock in ("router.json", "screen.json"):
        canned = json.loads((TRIAGE_DIR / "fixtures" / mock).read_text(encoding="utf-8"))
        assert canned["agent_version"] == "2.0.0", mock
    clinical = yaml.safe_load((CLINICAL_DIR / "agent.yaml").read_text(encoding="utf-8"))
    assert clinical["risk"]["level"] == 2
    assert clinical["questions"]["severity"]["type"] == "score"
