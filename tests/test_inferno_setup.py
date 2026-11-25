# SPDX-License-Identifier: Apache-2.0
"""Inferno's conformance runs against Mendari (sandbox/inferno/README.md, LP-23l): the scripted
suites' compose project is standalone and pinned, and each expected result that is not a pass
says why and which requirement it leaves unmet. No Inferno and no Docker here: the files are
read as they are committed."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INFERNO = ROOT / "sandbox" / "inferno"
SCRIPTED = INFERNO / "scripted"
COMPOSE = SCRIPTED / "docker-compose.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "conformance.yml"
GOLDENS = sorted(INFERNO.glob("*_expected.json"))
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
# What a test that is not a pass must say at least: a reason a reviewer can act on.
SHORTEST_REASON = 30


def _script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _script("inferno_check")


def _compose() -> dict[str, Any]:
    loaded = yaml.safe_load(COMPOSE.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _golden(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_scripted_project_and_its_dockerfile_are_pinned() -> None:
    """A compose project of its own, not an overlay (the root file's and this one's `engine`
    would merge): no service extends another file's, nothing is included, and every image is
    pinned by digest or built here (the engine from the repository's Dockerfile, the kits from
    sandbox/inferno/scripted/Dockerfile). The image takes the CRD kit, its base, by a full
    commit and at the version its tag names, on a Ruby pinned by digest, and the SMART and
    Bulk kits by the versions their expected results name, each with its sha256 in the
    lock."""
    compose = _compose()
    assert compose["name"] == "mendari-inferno-scripted"
    assert "include" not in compose
    services = compose["services"]
    built = {s["image"] for s in services.values() if "build" in s}
    assert built == {"mendari-engine:inferno", "mendari-inferno-scripted:0.15.0"}
    for name, service in services.items():
        assert "extends" not in service, name
        if service["image"] in built:
            assert "build" in service or service.get("pull_policy") == "never", name
        else:
            assert DIGEST.search(service["image"]), (name, service["image"])
    # The web app and its worker: one database, one host for the URLs the kits make.
    inferno, worker = services["inferno"], services["worker"]
    assert inferno["volumes"] == worker["volumes"] == ["inferno-data:/opt/inferno/data"]
    assert "inferno-data" in compose["volumes"]
    assert inferno["environment"] is worker["environment"]  # one anchor
    assert "inferno" in worker["depends_on"]  # the web app fills the new volume first
    dockerfile = (SCRIPTED / "Dockerfile").read_text()
    base = next(line for line in dockerfile.splitlines() if line.startswith("FROM "))
    assert re.fullmatch(r"FROM ruby:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", base), base
    assert "ARG KIT_REPOSITORY=https://github.com/inferno-framework/davinci-crd-test-kit.git\n" in (
        dockerfile
    )
    lock = (SCRIPTED / "Gemfile.lock").read_text()
    assert re.search(r"^ARG KIT_COMMIT=[0-9a-f]{40}$", dockerfile, re.M)
    tagged = re.escape(services["inferno"]["image"].rsplit(":", 1)[1])
    assert re.search(rf"^    davinci_crd_test_kit \({tagged}\)$", lock, re.M)
    by_suite = {golden["suite"]: golden for golden in map(_golden, GOLDENS)}
    for suite, gem in (
        ("smart_client_stu2_2", "smart_app_launch_test_kit"),
        ("bulk_data_v200_client", "bulk_data_test_kit"),
    ):
        version = re.escape(by_suite[suite]["version"])
        assert re.search(rf"^  {gem} \({version}\) sha256=[0-9a-f]{{64}}$", lock, re.M), suite
    # Each JVM's heap is capped, so one suite's services fit in 8 GB.
    for name in ("hl7_validator_service", "fhirpath", "fhir"):
        assert re.fullmatch(r"-Xmx\d+[mg]", services[name]["environment"]["JAVA_TOOL_OPTIONS"])


@pytest.mark.parametrize("path", GOLDENS, ids=lambda p: p.name)
def test_every_expected_failure_or_skip_has_a_reason(path: Path) -> None:
    """Every expected results file: each test listed once with a result Inferno gives (never
    `error`: a run that ends in one is fixed, not recorded), each result but a pass with a
    reason that says why, and each fail or skip with the requirement ids it leaves unmet
    (scripts/inferno_check.py refuses the file otherwise)."""
    golden = _golden(path)
    entries = check.expected_results(golden)
    assert path.name == f"{golden['suite']}_expected.json"
    for test, entry in entries.items():
        assert entry["result"] in ("pass", "fail", "skip", "omit"), test
        if entry["result"] == "pass":
            assert "reason" not in entry, test  # a pass has nothing to explain
            continue
        reason = entry["reason"].strip()
        assert len(reason) >= SHORTEST_REASON, (test, reason)
        assert not re.search(r"\b(TODO|TBD|FIXME)\b", reason), test
        if entry["result"] in ("fail", "skip"):
            assert entry["requirements"], test
    shorts = [entry["short_id"] for entry in entries.values()]
    assert len(shorts) == len(set(shorts)), "a short id listed twice"


def test_each_expected_results_file_is_one_conformance_jobs_verdict() -> None:
    """No file of expected results that no job checks, and no job without one."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    checked = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "inferno_check.py" in step.get("run", "")
    ]
    named = sorted(
        re.search(r"--expected (sandbox/inferno/\S+_expected\.json)", run)[1]  # type: ignore[index]
        for run in checked
    )
    assert named == sorted(str(p.relative_to(ROOT)) for p in GOLDENS)
