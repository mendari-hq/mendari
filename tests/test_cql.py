"""The CQL provider (providers/cql.py), the record-to-FHIR converter it sends (standards/
record_fhir.py) and variant agreement (evals/agreement.py). A fake CQL engine answers here; the CI
job `cql` runs the trial pack's library on HAPI FHIR's Clinical Reasoning module."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from openhealth.cli import main
from openhealth.core.pack import Pack
from openhealth.core.registry import resolve_pack
from openhealth.errors import PackValidationError, ProviderError
from openhealth.evals.agreement import agreement, pack_cases
from openhealth.providers import build_provider
from openhealth.providers import cql as cql_module
from openhealth.standards import record_fhir
from openhealth.standards.fhir_client import GuardedHttp

TRIAL = "openhealth/clinical-trial-prescreener"
ENGINE = "https://cql.example.org/fhir"


@pytest.fixture
def trial() -> Pack:
    return resolve_pack(TRIAL)


class FakeEngine:
    """Answers `$evaluate` like HAPI: each asked-for definition as a value, computed here by the
    pack's `criteria` provider, so a faithful mapping agrees on every case."""

    def __init__(self, pack: Pack, flip: str | None = None, error: bool = False):
        self.criteria = build_provider(pack, "structured", "criteria")
        self.names = pack.manifest.providers["structured"].variants["cql"].model_extra or {}
        self.flip, self.error = flip, error
        self.uploads = 0
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            self.uploads += 1
            return httpx.Response(200, json=json.loads(request.content))
        body = json.loads(request.content)
        self.requests.append(body)
        if self.error:
            outcome = {"resourceType": "OperationOutcome", "issue": [{"diagnostics": "boom"}]}
            error = {"name": "evaluation error", "resource": outcome}
            return httpx.Response(200, json={"resourceType": "Parameters", "parameter": [error]})
        params = {p["name"]: p for p in body["parameter"]}
        as_of = params["parameters"]["resource"]["parameter"][0]["valueDate"]
        record = _record_from(params["data"]["resource"], as_of)
        answers = self.criteria.answer(record).answers
        out = []
        for question, expression in self.names["expressions"].items():
            label = answers[question].label
            if question == self.flip:
                label = "no" if label != "no" else "yes"
            out.append({"name": expression, "valueString": label})
        return httpx.Response(200, json={"resourceType": "Parameters", "parameter": out})


def _record_from(bundle: dict[str, Any], as_of: str) -> dict[str, Any]:
    """Back from FHIR to the record, the way the fake engine reads it."""
    record: dict[str, Any] = {"as_of": as_of}
    record |= {"conditions": [], "observations": [], "medications": []}
    for entry in bundle["entry"]:
        r = entry["resource"]
        if r["resourceType"] == "Patient" and "birthDate" in r:
            born = date.fromisoformat(r["birthDate"])
            today = date.fromisoformat(as_of)
            record["patient"] = {
                "age": today.year - born.year - ((today.month, today.day) < (born.month, born.day))
            }
        elif r["resourceType"] == "Condition":
            item = {"code": r["code"]["coding"][0]["code"]}
            if "onsetDateTime" in r:
                item["onset"] = r["onsetDateTime"]
            record["conditions"].append(item)
        elif r["resourceType"] == "Observation":
            item = {"code": r["code"]["coding"][0]["code"], "date": r.get("effectiveDateTime")}
            if "valueQuantity" in r:
                item["value"] = r["valueQuantity"]["value"]
                if "unit" in r["valueQuantity"]:
                    item["unit"] = r["valueQuantity"]["unit"]
            record["observations"].append(item)
        elif r["resourceType"] == "MedicationStatement":
            period = r.get("effectivePeriod", {})
            record["medications"].append(
                {
                    "name": r["medicationCodeableConcept"].get("text", ""),
                    "status": r["status"],
                    **{k: v for k, v in period.items()},
                }
            )
    return record


@pytest.fixture
def engine(trial: Pack, monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    fake = FakeEngine(trial)
    use(fake, monkeypatch)
    return fake


def use(fake: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHEALTH_CQL_URL", ENGINE)
    transport = httpx.MockTransport(fake)
    monkeypatch.setattr(
        cql_module,
        "GuardedHttp",
        lambda **kw: GuardedHttp(transport=transport, allowed={"https://cql.example.org"}),
    )


def test_the_cql_variant_agrees_with_criteria_through_the_engine(
    trial: Pack, engine: FakeEngine
) -> None:
    cases = pack_cases(trial, ["exclusions"])
    report = agreement(trial, "structured", "criteria", "cql", cases)
    assert report["agree"] is True and report["cases"] == len(cases)
    assert engine.uploads == 1  # the library goes up once, then every case is evaluated
    first = engine.requests[0]["parameter"]
    assert {p["name"] for p in first} >= {"subject", "useServerData", "data", "parameters"}
    assert next(p for p in first if p["name"] == "useServerData")["valueBoolean"] is False


def test_a_disagreement_is_reported_by_case_id_and_question(
    trial: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(FakeEngine(trial, flip="t2d_diagnosis"), monkeypatch)
    report = agreement(trial, "structured", "criteria", "cql", pack_cases(trial, ["exclusions"]))
    assert report["agree"] is False
    first = report["differences"][0]
    assert set(first) == {"case", "question", "criteria", "cql"}
    assert first["question"] == "t2d_diagnosis"
    assert first["case"].startswith("test: ")  # the pack's tests first, by name; then datasets
    assert any(d["case"].startswith("exclusions: ") for d in report["differences"])


def test_engine_failures_are_provider_errors(trial: Pack, monkeypatch: pytest.MonkeyPatch) -> None:
    case = pack_cases(trial, [])[0][1]
    use(FakeEngine(trial, error=True), monkeypatch)
    with pytest.raises(ProviderError, match="evaluation error") as raised:
        build_provider(trial, "structured", "cql").answer(case)
    assert "boom" not in str(raised.value)  # the engine's own text stays with the engine

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    use(down, monkeypatch)
    with pytest.raises(ProviderError, match="unavailable"):
        build_provider(trial, "structured", "cql").answer(case)


def test_results_become_labels() -> None:
    assert cql_module._label("yes") == "yes"
    assert cql_module._label(True) == "yes" and cql_module._label(False) == "no"
    assert cql_module._label(None) == "unknown"  # no result, or data absent
    assert cql_module._label(3) is None  # not a label: the provider refuses it
    absent = {"name": "HbA1c", "_valueBoolean": {"extension": []}}
    assert cql_module._results({"resourceType": "Parameters", "parameter": [absent]}) == {}


def test_the_engine_must_be_configured_and_allow_listed(
    trial: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = build_provider(trial, "structured", "cql")
    monkeypatch.delenv("OPENHEALTH_CQL_URL", raising=False)
    with pytest.raises(ProviderError, match="not_configured"):
        provider.preflight()
    monkeypatch.setenv("OPENHEALTH_CQL_URL", ENGINE)
    monkeypatch.delenv("OPENHEALTH_FHIR_ALLOWED_SERVERS", raising=False)
    with pytest.raises(ProviderError, match="allow-list"):
        provider.answer(pack_cases(trial, [])[0][1])


def test_a_bad_cql_variant_fails_when_the_pack_loads(trial: Pack, tmp_path: Path) -> None:
    provider = build_provider(trial, "structured", "cql")
    assert provider.info.model is not None and provider.info.model.startswith(
        "cql:GlycemiaTwoCriteria@"
    )
    base = {"type": "cql", "library": "logic/criteria.cql", "expressions": {"x": "X"}}
    info, questions = provider.info, provider.questions
    with pytest.raises(PackValidationError, match="exactly the slot's questions"):
        cql_module.CqlProvider(trial, info, questions, base)
    with pytest.raises(PackValidationError, match="no file"):
        cql_module.CqlProvider(trial, info, questions, {**base, "library": "logic/none.cql"})


def test_the_record_becomes_fhir_without_inventing_anything() -> None:
    record = {
        "patient": {"age": 57, "sex": "male"},
        "conditions": [
            {"code": "E11.9", "text": "Type 2 diabetes", "status": "active", "onset": "2019"},
            {"code": "22298006", "status": "historical"},
        ],
        "observations": [
            {"code": "4548-4", "value": 8.1, "unit": "%", "date": "2026-09-01"},
            {"code": "33914-3", "date": "not a date"},
        ],
        "medications": [{"name": "metformin", "status": "on it", "start": "2025-09"}],
    }
    bundle = record_fhir.bundle(record, date(2026, 9, 30))
    by_id = {e["resource"]["id"]: e["resource"] for e in bundle["entry"]}
    assert by_id["record-patient"] == {
        "resourceType": "Patient",
        "id": "record-patient",
        "birthDate": "1969-09-30",
        "gender": "male",
    }
    assert by_id["condition-0"]["code"]["coding"][0]["system"] == record_fhir.ICD10
    assert by_id["condition-0"]["onsetDateTime"] == "2019-07-01"  # mid-year, as `criteria` reads it
    assert by_id["condition-1"]["code"]["coding"][0]["system"] == record_fhir.SNOMED
    assert "clinicalStatus" not in by_id["condition-1"]  # not a FHIR clinical status
    assert by_id["observation-0"]["valueQuantity"] == {"value": 8.1, "unit": "%"}
    assert "effectiveDateTime" not in by_id["observation-1"]
    assert "valueQuantity" not in by_id["observation-1"]
    medication = by_id["medication-0"]
    assert medication["status"] == "unknown" and medication["effectivePeriod"] == {
        "start": "2025-09-15"
    }
    assert record_fhir.born(40, date(2024, 2, 29)) == date(1984, 2, 29)
    assert record_fhir.born(41, date(2024, 2, 29)) == date(1983, 2, 28)


def test_the_cli_exits_1_when_variants_differ(
    trial: Pack, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(FakeEngine(trial, flip="no_insulin"), monkeypatch)
    args = ["agreement", TRIAL, "--slot", "structured", "--variants", "criteria", "cql"]
    assert main([*args, "--dataset", "exclusions"]) == 1
    out = capsys.readouterr().out
    assert "criteria and cql differ" in out and "no_insulin" in out
    use(FakeEngine(trial), monkeypatch)
    assert main([*args, "--dataset", "exclusions"]) == 0
