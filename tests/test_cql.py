# SPDX-License-Identifier: Apache-2.0
"""The CQL provider (providers/cql.py), the record-to-FHIR converter it sends (standards/
record_fhir.py) and variant agreement (evals/agreement.py). A fake CQL engine answers here; the CI
job `cql` runs the trial pack's library on HAPI FHIR's Clinical Reasoning module."""

from __future__ import annotations

import base64
import json
import re
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from mendari.cli import main
from mendari.core.pack import Pack, load_pack
from mendari.core.registry import resolve_pack
from mendari.errors import PackValidationError, ProviderError
from mendari.evals.agreement import agreement, pack_cases
from mendari.providers import build_provider
from mendari.providers import cql as cql_module
from mendari.sandbox.synthetic_trial import CONDITION_SYSTEM
from mendari.standards import record_fhir
from mendari.standards.fhir_client import GuardedHttp

TRIAL = "mendari/clinical-trial-prescreener"
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
    monkeypatch.setenv("MENDARI_CQL_URL", ENGINE)
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
    monkeypatch.delenv("MENDARI_CQL_URL", raising=False)
    with pytest.raises(ProviderError, match="not_configured"):
        provider.preflight()
    monkeypatch.setenv("MENDARI_CQL_URL", ENGINE)
    monkeypatch.delenv("MENDARI_FHIR_ALLOWED_SERVERS", raising=False)
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
            {"code": "FC201", "text": "Myocardial infarction"},
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
    # A code neither ICD-10-CM nor fictional is read as a site's SNOMED CT; the local-site
    # example's fictional codes keep their own system.
    assert by_id["condition-1"]["code"]["coding"][0]["system"] == record_fhir.SNOMED
    assert by_id["condition-2"]["code"]["coding"] == [
        {"system": CONDITION_SYSTEM, "code": "FC201", "display": "Myocardial infarction"}
    ]
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


class SharedEngine:
    """A CQL engine shared by packs, which behaves like HAPI's: a PUT by id replaces the stored
    Library, and `$evaluate` runs the first stored Library with the requested one's declared
    name and version. A library whose text says EVIL answers 'yes' to everything."""

    def __init__(self, honest: FakeEngine):
        self.honest = honest
        self.stored: dict[str, dict[str, Any]] = {}
        self.ids: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        resource_id = request.url.path.split("/Library/")[1].split("/")[0]
        if request.method == "PUT":
            resource = json.loads(request.content)
            assert resource["id"] == resource_id
            self.stored[resource_id] = resource
            self.ids.append(resource_id)
            return httpx.Response(200, json=resource)
        wanted = self.stored[resource_id]
        ran = next(
            r
            for r in self.stored.values()
            if (r["name"], r["version"]) == (wanted["name"], wanted["version"])
        )
        text = base64.b64decode(ran["content"][0]["data"]).decode()
        declared = cql_module.library_declaration(text)
        assert declared is not None and declared.group(1) == ran["name"]  # as HAPI checks
        if "EVIL" not in text:
            return self.honest(request)
        names = self.honest.names["expressions"].values()
        out = [{"name": name, "valueString": "yes"} for name in names]
        return httpx.Response(200, json={"resourceType": "Parameters", "parameter": out})


def _outside_trial(root: Path, header: str) -> Pack:
    """An outside copy of the trial pack whose library declares `header` and answers 'yes'."""
    pack = root / "outsider"
    shutil.copytree(resolve_pack(TRIAL).root, pack)
    manifest = yaml.safe_load((pack / "pack.yaml").read_text())
    manifest["id"] = "outsider/evil-trial"
    (pack / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    library = pack / "logic" / "criteria.cql"
    text = library.read_text().replace("library GlycemiaTwoCriteria version '0.2.0'", header)
    library.write_text(text + "\n// EVIL\n")
    return load_pack(pack)


@pytest.mark.parametrize("first", ["shipped", "outsider"])
@pytest.mark.parametrize("version", ["0.2.0", "6.6.6"])
def test_a_pack_cannot_replace_or_shadow_another_packs_library_on_the_engine(
    trial: Pack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str, version: str
) -> None:
    """R3: the Library's id was the name the library declares, so an outside pack declaring
    the shipped library's name replaced it on the shared engine (a PUT by id), and the shipped
    pack's later decisions ran the outsider's CQL; with the same version too, the engine (which
    finds a library by name and version) could run either. Checked on HAPI 8.12 as well."""
    engine = SharedEngine(FakeEngine(trial))
    use(engine, monkeypatch)
    outsider = _outside_trial(tmp_path, f"library GlycemiaTwoCriteria version '{version}'")
    shipped_cql = build_provider(trial, "structured", "cql")
    outsider_cql = build_provider(outsider, "structured", "cql")
    assert isinstance(shipped_cql, cql_module.CqlProvider)
    order = [shipped_cql, outsider_cql] if first == "shipped" else [outsider_cql, shipped_cql]
    record = {**pack_cases(trial, [])[0][1], "patient": {"age": 90}}
    for provider in [*order, *order]:
        age = provider.answer(record).answers["age_18_75"].label
        assert age == ("no" if provider is shipped_cql else "yes")
    assert len(set(engine.ids)) == 2 and len({r["name"] for r in engine.stored.values()}) == 2
    resource = shipped_cql.library_resource()
    assert re.fullmatch(r"[A-Za-z0-9.-]{1,64}", resource["id"])
    assert resource["name"].startswith("GlycemiaTwoCriteria_")
    assert resource["url"].endswith(f"/Library/{resource['name']}")
    evidence = shipped_cql.answer(record).answers["age_18_75"].evidence
    assert evidence == ("CQL GlycemiaTwoCriteria 0.2.0: Age 18 To 75 = no",)  # as declared


@pytest.mark.parametrize(
    ("prefix", "allowed"),
    [
        ("", True),
        ("// a comment\n\n", True),
        ("/* a\nblock */ \r\n", True),
        ("/*\nlibrary Decoy version '1'\n*/\n", False),
        ("/* library Decoy version '1' */\n", True),
        ("// x\r  library Decoy version '1'\n", True),  # read as a new line, in Python and CQL
        ("using FHIR version '4.0.1'\n", False),
        ("/* never closed\n", False),
    ],
)
def test_the_declaration_the_engine_reads_is_the_one_renamed(
    trial: Pack, tmp_path: Path, prefix: str, allowed: bool
) -> None:
    """The name is rewritten where CQL declares it, its first statement. A header-like line
    in a comment before it (which `library_header`, main's search, finds first) would leave
    the real declaration's name as written on the engine, so such a library is refused."""
    root = tmp_path / "pack"
    shutil.copytree(trial.root, root)
    library = root / "logic" / "criteria.cql"
    text = library.read_text()
    start = text.index("library GlycemiaTwoCriteria")
    library.write_text(prefix + text[start:])
    pack = load_pack(root)
    if not allowed:
        with pytest.raises(PackValidationError, match="must be the library's first statement"):
            build_provider(pack, "structured", "cql")
        return
    provider = build_provider(pack, "structured", "cql")
    assert isinstance(provider, cql_module.CqlProvider)
    uploaded = base64.b64decode(provider.library_resource()["content"][0]["data"]).decode()
    declared = cql_module.library_declaration(uploaded)
    assert declared is not None and declared.groups() == (provider.engine_name, provider.version)
    assert provider.engine_name.startswith(provider.name + "_")
    read = (prefix + text[start:]).replace("\r\n", "\n").replace("\r", "\n")  # as read_text
    assert uploaded.replace(provider.engine_name, provider.name, 1) == read


def test_finding_the_declaration_is_linear() -> None:
    for source in ["/*" * 50_000, "//\n" * 50_000 + "x", " " * 100_000 + "library X"]:
        started = time.perf_counter()
        assert cql_module.library_declaration(source) is None
        assert time.perf_counter() - started < 0.05


# Libraries of blank lines: `^\s*library` (MULTILINE) ran from each line's start to the end of the
# text, so the search was quadratic (160,000 blank lines took 21 s). Load runs it on every pack
# now (LP-16e), a registry's at publish and pull too.
BLANK_LIBRARIES = ["\n" * 100_000, "library\n" + "\n" * 100_000]


def test_finding_the_header_is_linear() -> None:
    for source in BLANK_LIBRARIES:
        started = time.perf_counter()
        assert cql_module._HEADER.search(source) is None
        assert time.perf_counter() - started < 0.05


def test_a_library_of_blank_lines_is_checked_at_load_in_linear_time(tmp_path: Path) -> None:
    """The load-time check (`CqlProvider.config_problems`, as for a pack from a registry) on a
    library of blank lines: refused for having no declaration, at once."""
    for number, source in enumerate(BLANK_LIBRARIES):
        root = tmp_path / str(number)
        shutil.copytree(resolve_pack(TRIAL).root, root)
        (root / "logic" / "criteria.cql").write_text(source)
        pack = load_pack(root)  # owned: loads, and warns
        assert any("needs a `library Name version" in w for w in pack.warnings)
        slot = pack.manifest.providers["structured"]
        config = {"type": "cql", **(slot.variants["cql"].model_extra or {})}
        questions = {key: pack.manifest.questions[key] for key in slot.answers}
        started = time.perf_counter()
        problems = cql_module.CqlProvider.config_problems(
            pack, "structured", config, questions, bounded=True, trained=True
        )
        assert time.perf_counter() - started < 0.05
        assert problems == ["logic/criteria.cql: needs a `library Name version '...'` declaration"]


def test_an_expression_the_engine_did_not_evaluate_is_not_unknown(
    trial: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A definition missing from the engine's Parameters (a wrong library bound, a renamed
    declaration) was read as null, a confident `unknown`; it is an invalid response."""
    case = pack_cases(trial, [])[0][1]
    fake = FakeEngine(trial)

    def dropping(request: httpx.Request) -> httpx.Response:
        response = fake(request)
        if request.method != "PUT":
            body = response.json()
            body["parameter"] = body["parameter"][1:]  # the first definition never comes back
            return httpx.Response(200, json=body)
        return response

    use(dropping, monkeypatch)
    with pytest.raises(ProviderError, match="was not evaluated") as raised:
        build_provider(trial, "structured", "cql").answer(case)
    assert raised.value.code == "invalid_response"
