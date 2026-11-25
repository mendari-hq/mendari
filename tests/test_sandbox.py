# SPDX-License-Identifier: Apache-2.0
"""The product's sandbox (`mendari sandbox seed` and `worker`, sandbox/fhir_workflow.py): a
message agent's synthetic cases as US Core resources on a FHIR server, and the worker that sends
each message to the engine once. It runs on the bundled demo agent unless another message agent
is named (here the triage test fixture)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from mendari.cli import main
from mendari.core.agent import load_agent
from mendari.errors import MendariError
from mendari.sandbox import DEMO_AGENT, fhir_workflow
from mendari.sandbox.code_systems import CASE_SYSTEM
from mendari.sandbox.fhir_records import message_entries
from mendari.sandbox.fhir_workflow import _process_batch
from mendari.standards import us_core
from mendari.standards.fhir import FhirAdapter
from tests.conftest import CHART_DIR, DEMO_AGENT_DIR, TRIAGE_DIR

ROOT = Path(__file__).resolve().parents[1]
FHIR = "http://fhir.test/fhir"
DECIDE = f"http://engine.test/api/agents/{DEMO_AGENT}/fhir/decide"


class StaleFhir:
    """A FHIR server whose searches always return the original (cached) results, like HAPI's
    search cache, but whose updates honour If-Match versioning."""

    def __init__(self) -> None:
        self.communication: dict[str, Any] = {
            "resourceType": "Communication",
            "id": "1",
            "meta": {"versionId": "1"},
            "status": "in-progress",
            "payload": [{"contentString": "I need a refill on my metformin"}],
        }
        self.cached_search = json.loads(json.dumps(self.communication))
        self.tasks: list[dict[str, Any]] = []
        self.decisions = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "engine.test":
            self.decisions += 1
            decision = {"outcome": "AUTO", "recommendation": "refill", "priority": "routine"}
            return httpx.Response(
                200, json={"decision": decision, "task": {"resourceType": "Task"}}
            )
        if request.method == "GET":
            return httpx.Response(200, json={"entry": [{"resource": self.cached_search}]})
        if request.method == "PUT":
            if request.headers.get("if-match") != f'W/"{self.communication["meta"]["versionId"]}"':
                return httpx.Response(412)
            body = json.loads(request.content)
            version = str(int(self.communication["meta"]["versionId"]) + 1)
            self.communication = {**body, "meta": {"versionId": version}}
            return httpx.Response(200, json=self.communication)
        self.tasks.append(json.loads(request.content))
        return httpx.Response(201, json={})


def test_worker_processes_each_message_once_despite_stale_searches() -> None:
    fhir = StaleFhir()
    with httpx.Client(transport=httpx.MockTransport(fhir)) as client:
        assert _process_batch(client, FHIR, DECIDE) == 1
        assert _process_batch(client, FHIR, DECIDE) == 0  # stale result is skipped, not redone
    assert fhir.decisions == 1
    assert len(fhir.tasks) == 1
    assert fhir.communication["status"] == "completed"
    assert fhir.tasks[0]["meta"]["tag"][0]["code"] == "synthetic"


def test_seed_resources_are_tagged_and_linked() -> None:
    record = fhir_workflow.labelled_cases(load_agent(DEMO_AGENT_DIR)).records[0]
    entries = message_entries(record)
    patient, communication = (entry["resource"] for entry in entries)
    assert patient["meta"]["tag"][0]["code"] == "synthetic"
    assert communication["subject"]["reference"] == entries[0]["fullUrl"]
    assert communication["payload"][0]["contentString"] == record["input"]["message"]["body"]


def _demo_agent_data() -> ModuleType:
    """scripts/demo_agent_data.py, which writes the demo agent's synthetic cases."""
    script = ROOT / "scripts" / "demo_agent_data.py"
    spec = importlib.util.spec_from_file_location("demo_agent_data", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_synthetic_generation_is_deterministic() -> None:
    """What the sandbox seeds by default is the demo agent's benchmark set, which
    scripts/demo_agent_data.py writes the same on every run: a sandbox seeded twice, or on two
    machines, holds the same messages, each under an id of its own."""
    script = _demo_agent_data()
    assert script.build(25) == script.build(25)
    made = script.build()
    assert len(made) == script.CASES
    ids = [record["id"] for record in made]
    assert len(ids) == len(set(ids))
    benchmark = load_agent(DEMO_AGENT_DIR).manifest.evaluation.benchmark_dataset
    assert benchmark == "test"
    committed = (DEMO_AGENT_DIR / "data" / "test.jsonl").read_text(encoding="utf-8")
    assert script.render(made) == committed


def test_every_seeded_resource_conforms_to_us_core() -> None:
    """Every case of the demo agent's benchmark set, and of another message agent's (the triage
    fixture's), as the sandbox seeds it. The HL7 validator checks a sample in CI
    (scripts/sandbox_fhir_sample.py)."""
    for folder in (DEMO_AGENT_DIR, TRIAGE_DIR):
        records = fhir_workflow.labelled_cases(load_agent(folder)).records
        resources = [entry["resource"] for record in records for entry in message_entries(record)]
        assert records and us_core.validate(resources) == [], folder.name


def test_seeding_twice_is_a_no_op_even_with_hapis_search_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HAPI caches search results, counts included: the check for an earlier seed (the server
    holds the first case's Patient, by its case id) must ask for fresh ones, or a cached "0"
    seeds twice. A seed of no case sends nothing."""
    posted: list[dict[str, Any]] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/metadata"):
            return httpx.Response(200, json={})
        assert url.endswith("/Patient")
        fresh = kwargs["headers"].get("cache-control") == "no-cache"
        wanted = kwargs["params"]["identifier"]
        held = sum(
            f"{identifier['system']}|{identifier['value']}" == wanted
            for bundle in posted
            for entry in bundle["entry"]
            if entry["resource"]["resourceType"] == "Patient"
            for identifier in entry["resource"]["identifier"]
        )
        return httpx.Response(200, json={"total": held if fresh else 0})

    def post(url: str, **kwargs: Any) -> httpx.Response:
        posted.append(kwargs["json"])
        return httpx.Response(200, json={})

    monkeypatch.setattr(fhir_workflow.httpx, "get", get)
    monkeypatch.setattr(fhir_workflow.httpx, "post", post)
    agent = str(DEMO_AGENT_DIR)
    assert fhir_workflow.seed(FHIR, count=3, agent=agent) == 3
    assert fhir_workflow.seed(FHIR, count=3, agent=agent) == 0
    assert len(posted) == 1
    assert fhir_workflow.seed(FHIR, count=0, agent=agent) == 0
    assert len(posted) == 1


# -- the product's sandbox: the bundled demo agent ----------------------------------------------


def _case_ids(bundle: dict[str, Any]) -> list[str]:
    """The case id each Patient of a seed bundle carries, in order."""
    return [
        next(i["value"] for i in e["resource"]["identifier"] if i["system"] == CASE_SYSTEM)
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Patient"
    ]


def _bodies(bundle: dict[str, Any]) -> list[str]:
    return [
        e["resource"]["payload"][0]["contentString"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Communication"
    ]


def test_the_sandbox_seeds_the_demo_agents_messages() -> None:
    """`mendari sandbox seed` loads the bundled demo agent's cases unless told otherwise: its
    benchmark set, a reproducible mix that always holds urgent messages, each a Patient carrying
    the case id and a Communication that reads back, through the agent's own mapping, as the
    case's input. Every resource conforms to US Core."""
    demo = load_agent(DEMO_AGENT_DIR)
    assert demo.id == DEMO_AGENT
    cases = {r["id"]: r for r in fhir_workflow.labelled_cases(demo).records}
    assert len(cases) == 600  # its benchmark dataset, `test`
    bundle = fhir_workflow.seed_bundle(demo, 20)
    assert us_core.validate([e["resource"] for e in bundle["entry"]]) == []
    seeded = _case_ids(bundle)
    assert len(seeded) == len(_bodies(bundle)) == 20 and len(bundle["entry"]) == 40
    assert seeded == _case_ids(fhir_workflow.seed_bundle(demo, 20))  # the same mix every time
    urgent = [case for case in seeded if cases[case]["labels"]["urgent_symptoms"] == "yes"]
    assert len(urgent) == 2  # one in ten of the count, and never none
    adapter = FhirAdapter.for_agent(demo)
    for position, case in enumerate(seeded):
        pair = bundle["entry"][2 * position : 2 * position + 2]
        read = adapter.to_input(adapter.index({"resourceType": "Bundle", "entry": pair}))
        assert read == cases[case]["input"], case
    assert fhir_workflow.seed_bundle(demo, 0)["entry"] == []
    one = fhir_workflow.seed_bundle(demo, 1)
    assert cases[_case_ids(one)[0]]["labels"]["urgent_symptoms"] == "yes"


def test_seed_takes_another_message_agent_and_dataset_and_refuses_other_cases() -> None:
    """Another message agent is named (`--agent`, here the triage test fixture), and its cases
    (`--dataset`: one of its own, or a file). Cases that are not messages (the chart-summary
    fixture's patient charts) are refused, saying what a message is."""
    triage = load_agent(TRIAGE_DIR)
    by_file = str(TRIAGE_DIR / "data" / "calibration.jsonl")
    calibration = _case_ids(fhir_workflow.seed_bundle(triage, 10, by_file))
    assert calibration == _case_ids(fhir_workflow.seed_bundle(triage, 10, "calibration"))
    assert calibration != _case_ids(fhir_workflow.seed_bundle(triage, 10))  # its `test` set
    own = fhir_workflow.labelled_cases(triage, "examples").records
    named = fhir_workflow.seed_bundle(triage, 4, "examples")
    assert len(_bodies(named)) == 4
    assert set(_bodies(named)) <= {record["input"]["message"]["body"] for record in own}
    chart = load_agent(CHART_DIR)
    with pytest.raises(MendariError, match=f"seeds portal messages, and {chart.id}'s"):
        fhir_workflow.seed_bundle(chart, 3)
    with pytest.raises(MendariError, match="neither a file nor an agent dataset"):
        fhir_workflow.seed_bundle(triage, 3, "no-such-dataset")


def test_the_cli_seeds_and_works_the_demo_agent_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari sandbox seed` and `worker` name no agent by default: the bundled demo agent. An
    agent given as a folder is asked for under its own id."""
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    posted: list[dict[str, Any]] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"total": 0})

    def post(url: str, **kwargs: Any) -> httpx.Response:
        posted.append(kwargs["json"])
        return httpx.Response(200, json={})

    monkeypatch.setattr(fhir_workflow.httpx, "get", get)
    monkeypatch.setattr(fhir_workflow.httpx, "post", post)
    assert main(["sandbox", "seed", "--fhir", FHIR, "--count", "5"]) == 0
    assert capsys.readouterr().out == f"seeded 5 synthetic messages of {DEMO_AGENT} into {FHIR}\n"
    assert len(_bodies(posted[0])) == 5
    worked: list[tuple[str, str]] = []

    def batch(client: httpx.Client, fhir: str, decide_url: str) -> int:
        worked.append((fhir, decide_url))
        return 0

    monkeypatch.setattr(fhir_workflow, "_process_batch", batch)
    worker = ["sandbox", "worker", "--fhir", FHIR, "--once", "--engine"]
    assert main([*worker, "http://engine.test"]) == 0
    assert main([*worker, "http://e.test", "--agent", str(TRIAGE_DIR)]) == 0
    assert main([*worker, "http://e.test", "--agent", "my-clinic/not-on-this-machine"]) == 0
    triage = load_agent(TRIAGE_DIR).id
    assert worked == [
        (FHIR, f"http://engine.test/api/agents/{DEMO_AGENT}/fhir/decide"),
        (FHIR, f"http://e.test/api/agents/{triage}/fhir/decide"),
        (FHIR, "http://e.test/api/agents/my-clinic/not-on-this-machine/fhir/decide"),
    ]
