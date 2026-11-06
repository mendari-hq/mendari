# SPDX-License-Identifier: Apache-2.0
"""Inferno's conformance runs against Mendari (sandbox/inferno/README.md, LP-23l): the scripted
suites' compose project is standalone and pinned, each expected result that is not a pass
says why and which requirement it leaves unmet, and the CRD job registers Inferno's issuer as
Inferno signs its tokens, with no secret anywhere. No Inferno and no Docker here: the files
are read as they are committed."""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari.server.cds_clients import cds_clients

ROOT = Path(__file__).resolve().parents[1]
INFERNO = ROOT / "sandbox" / "inferno"
SCRIPTED = INFERNO / "scripted"
COMPOSE = SCRIPTED / "docker-compose.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "conformance.yml"
GOLDENS = sorted(INFERNO.glob("*_expected.json"))
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
CRD_SUITE = "crd_server_v221"
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
inputs = _script("inferno_scripted_inputs")


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
    sandbox/inferno/scripted/Dockerfile). The image takes the CRD kit by the commit its expected
    results name, on the pinned Ruby the DTR image uses, and the SMART and Bulk kits by the
    versions theirs name, each with its sha256 in the lock."""
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
    assert base in (INFERNO / "Dockerfile").read_text().splitlines()  # DTR's pinned Ruby
    assert "ARG KIT_REPOSITORY=https://github.com/inferno-framework/davinci-crd-test-kit.git\n" in (
        dockerfile
    )
    lock = (SCRIPTED / "Gemfile.lock").read_text()
    by_suite = {golden["suite"]: golden for golden in map(_golden, GOLDENS)}
    assert f"ARG KIT_COMMIT={by_suite[CRD_SUITE]['commit']}\n" in dockerfile
    assert re.search(
        rf"^    davinci_crd_test_kit \({by_suite[CRD_SUITE]['version']}\)$", lock, re.M
    )
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
    """Every expected results file, DTR's included: each test listed once with a result
    Inferno gives (never `error`: a run that ends in one is fixed, not recorded), each result
    but a pass with a reason that says why, and each fail or skip with the requirement ids it
    leaves unmet (scripts/inferno_check.py refuses the file otherwise)."""
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


def test_the_crd_run_starts_the_groups_its_expected_results_list() -> None:
    golden = _golden(INFERNO / f"{CRD_SUITE}_expected.json")
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["crd-server"]["steps"]
    (run,) = [s["run"] for s in steps if "inferno_session.py" in s.get("run", "")]
    words = shlex.split(run)
    started = words[words.index("--groups") + 1 :]
    assert started[: started.index("--out")] == golden["groups"]
    for entry in golden["tests"]:  # every test listed is under a group the run starts
        assert any(entry["short_id"].startswith(f"{group}.") for group in golden["groups"])
    # Group 3.7 is not started: its must-support test names no requirement (README).
    assert "3.7" not in golden["groups"]
    assert not any(entry["short_id"].startswith("3.7.") for entry in golden["tests"])


def _signin() -> dict[str, str]:
    environment = _compose()["services"]["engine-signin"]["environment"]
    return {key: str(value) for key, value in environment.items()}


def test_the_job_registers_infernos_issuer_and_holds_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CRD kit signs each hook call with `iss` its suite's URL on INFERNO_HOST, `jku` that
    plus /jwks.json, and sends that plus /fhir as the hook's fhirServer; its `aud` is the
    `base_url` the inputs give it plus the path. So the signed-in engine registers exactly
    that issuer, keys URL and FHIR base, its FHIR allow-list is Inferno's host alone, and its
    MENDARI_PUBLIC_URL is the inputs' `base_url`. The entry passes Mendari's own startup check
    of MENDARI_CDS_CLIENTS. The PHI keys come from the shell (the job makes them), and the
    workflow names no secret."""
    host = _compose()["x-inferno-environment"]["INFERNO_HOST"]
    environment = _signin()
    (client,) = json.loads(environment["MENDARI_CDS_CLIENTS"])
    iss = f"{host}/custom/{CRD_SUITE}"
    assert client["iss"] == iss
    assert client["jwks_uri"] == f"{iss}/jwks.json"
    assert client["fhir_servers"] == [f"{iss}/fhir"] == [inputs.CRD_FHIR]
    assert "jwks" not in client  # Inferno always sends jku: keys by value would refuse it
    assert not {"sub", "tenant"} & set(client)  # its tokens carry neither
    assert client["org"] == "pilot"
    assert environment["MENDARI_FHIR_ALLOWED_SERVERS"] == host == "http://nginx"
    base_url = next(item["value"] for item in inputs.crd()["inputs"] if item["name"] == "base_url")
    assert environment["MENDARI_PUBLIC_URL"] == base_url == inputs.SIGNIN_ENGINE
    for key in ("MENDARI_PHI_KEY", "MENDARI_INPUT_HASH_KEY"):
        assert environment[key] == f"${{{key}:-}}"
    assert "MENDARI_CDS_SANDBOX" not in environment
    assert not re.search(r"\bsecrets\.", WORKFLOW.read_text())
    # As the engine reads it at startup, with its own allow-list and public URL.
    for key in ("MENDARI_CDS_CLIENTS", "MENDARI_PUBLIC_URL", "MENDARI_FHIR_ALLOWED_SERVERS"):
        monkeypatch.setenv(key, environment[key])
    registry = cds_clients(signed_in=True, ehrs={}, trusted=frozenset())
    assert registry is not None
