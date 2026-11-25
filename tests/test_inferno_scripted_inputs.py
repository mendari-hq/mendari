# SPDX-License-Identifier: Apache-2.0
"""The inputs of Inferno's scripted suites (scripts/inferno_scripted_inputs.py, LP-23l): what the
SMART App Launch and Bulk Data suites are given about the engines, on synthetic data only. The
engines' URLs and client ids are the compose project's, the launch's patient is the one patient
Inferno's FHIR server serves, and a Bulk export is a patient-level one. No Inferno here."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "inferno_scripted_inputs.py"
COMPOSE = ROOT / "sandbox" / "inferno" / "scripted" / "docker-compose.yml"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("inferno_scripted_inputs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inputs = _script()


def _values(preset: dict[str, Any]) -> dict[str, str]:
    assert all(isinstance(item["value"], str) for item in preset["inputs"])  # as Inferno reads
    return {item["name"]: item["value"] for item in preset["inputs"]}


def _environment(service: str) -> dict[str, str]:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    return {k: str(v) for k, v in services[service]["environment"].items()}


def test_smart_registers_the_open_engines_client_launch_url_and_redirect() -> None:
    values = _values(inputs.smart())
    engine = _environment("engine")
    public = engine["MENDARI_PUBLIC_URL"]
    assert values["client_id"] == engine["MENDARI_SMART_CLIENT_ID"]
    # Inferno adds iss and launch to the launch URL and nothing else: the engine serves one agent.
    assert values["smart_launch_urls"] == f"{public}/smart/launch"
    assert "," not in engine["MENDARI_AGENTS"]
    assert values["smart_redirect_uris"] == f"{public}/smart/callback"
    # The launch's patient is the one resource Inferno's FHIR server serves to its token.
    patient = json.loads(values["launch_context"])["patient"]
    served = json.loads(values["fhir_read_resources_bundle"])["entry"]
    assert [(e["resource"]["resourceType"], e["resource"]["id"]) for e in served] == [
        ("Patient", patient)
    ]
    assert served[0]["fullUrl"].endswith(f"/Patient/{patient}")
    # No `openid fhirUser`: the kit's SMART configuration has no jwks_uri for its id_token.
    assert set(engine["MENDARI_SMART_SCOPES"].split()) == {"launch", "patient/*.rs"}


def test_bulk_asks_for_a_patient_level_export_and_leaves_the_keys_to_the_driver() -> None:
    values = _values(inputs.bulk())
    assert values == {
        "client_id": inputs.BULK_CLIENT_ID,
        "export_type": "Patient",
        "group_id": "1a",  # required by the suite, read only for a Group export
    }
    assert "smart_jwk_set" not in values


def test_the_cli_writes_each_preset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for suite, suite_id in (("smart", "smart_client_stu2_2"), ("bulk", "bulk_data_v200_client")):
        out = tmp_path / f"{suite}.json"
        assert inputs.main([suite, "--out", str(out)]) == 0
        assert json.loads(out.read_text())["test_suite_id"] == suite_id
        assert f"{suite_id} inputs written" in capsys.readouterr().out
