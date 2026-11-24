# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from mendari.cli import main
from mendari.core import clock
from mendari.core.decision import Decision
from mendari.core.pack import load_pack
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals.harness import load_dataset
from mendari.sandbox import DEMO_AGENT, example_datasets_dir, fhir_workflow
from mendari.sandbox.code_systems import CASE_SYSTEM
from mendari.sandbox.fhir_records import message_entries
from mendari.sandbox.fhir_workflow import _process_batch
from mendari.standards import us_core
from mendari.standards.fhir import FhirAdapter
from tests.conftest import DEMO_AGENT_DIR
from tests.support import synthetic_inbox
from tests.support.fhir_records import referral_entries
from tests.support.sandbox import (
    EHR_PROFILE,
    INBOX,
    PRIOR_AUTH,
    REFERRAL,
    TRIAL,
    seed,
    seed_bundle,
)

DATASETS = example_datasets_dir() or Path("datasets")

FHIR = "http://fhir.test/fhir"
DECIDE = "http://engine.test/api/agents/mendari/patient-inbox-router/fhir/decide"


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
            decision = {
                "outcome": "AUTO",
                "recommendation": "medical_assistant",
                "priority": "routine",
            }
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
    record = synthetic_inbox.local_site(n=1)[0]
    entries = message_entries(record)
    patient, communication = (entry["resource"] for entry in entries)
    assert patient["meta"]["tag"][0]["code"] == "synthetic"
    assert communication["subject"]["reference"] == entries[0]["fullUrl"]
    assert communication["payload"][0]["contentString"] == record["input"]["message"]["body"]


def test_synthetic_generation_is_deterministic() -> None:
    assert synthetic_inbox.local_site(n=25) == synthetic_inbox.local_site(n=25)
    splits = synthetic_inbox.public_splits()
    assert [len(v) for v in splits.values()] == [1500, 300, 600]
    ids = [r["id"] for split in splits.values() for r in split]
    assert len(ids) == len(set(ids))


# -- US Core resources for the sandbox (fhir_records) -------------------------------------------

REFERRAL_CASES = load_dataset(DATASETS / "referral-local-site-example.jsonl").records
TODAY = date(2026, 10, 15)


def referral_case(record: dict[str, Any]) -> dict[str, Any]:
    """The case's resources as a collection Bundle (one referral and its record)."""
    entries = referral_entries(record, today=TODAY)
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def test_every_seeded_resource_conforms_to_us_core() -> None:
    """The HL7 validator checks a sample in CI (scripts/sandbox_fhir_sample.py)."""
    inbox = synthetic_inbox.local_site(n=20)
    assert us_core.validate([e["resource"] for r in inbox for e in message_entries(r)]) == []
    for record in REFERRAL_CASES:
        assert us_core.validate(referral_case(record)) == [], record["id"]


def test_referral_cases_round_trip_through_the_pack_mapping() -> None:
    """FHIR -> the referral pack's mapping gives back the dataset's input, moved to today: the
    same patient, referral and record, with every date shifted by the same gap. A document or
    report code the checklist does not look for (the local site's history and physical, LOINC
    34117-2) is the one thing not read back: the mapping reads codes through the code sets the
    checklist's codes are declared in (spec 0.3, LP-23k), so such an item keeps its name and
    date, not its code."""
    adapter = FhirAdapter.for_pack(resolve_pack(REFERRAL))
    sets = adapter.mapping.code_sets
    looked_for = {
        "documents": set(sets["checklist_documents"].codes),
        "reports": set(sets["checklist_reports"].codes),
    }
    dropped = 0
    for record in REFERRAL_CASES:
        got = adapter.to_input(adapter.index(referral_case(record)))
        want = moved_input(record["input"])
        for key, codes in looked_for.items():
            for item in want.get(key, []):
                if item.get("code") not in codes:
                    del item["code"]
                    dropped += 1
        assert got == want, record["id"]
    assert dropped


def test_referral_cases_decide_the_same_from_fhir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    decision = Decision.from_hub(REFERRAL)
    for record in REFERRAL_CASES[:60]:
        from_fhir, _ = decision.decide_fhir(referral_case(record))
        direct = decision.decide(moved_input(record["input"]))
        assert (from_fhir.outcome, from_fhir.recommendation) == (
            direct.outcome,
            direct.recommendation,
        )
        assert {q: a.label for q, a in from_fhir.answers.items()} == {
            q: a.label for q, a in direct.answers.items()
        }


def test_seed_bundles_hold_the_requested_cases() -> None:
    inbox = seed_bundle(INBOX, 10)
    assert sum(e["resource"]["resourceType"] == "Communication" for e in inbox["entry"]) == 10
    referral = seed_bundle(REFERRAL, 5)
    identifiers = [
        e["resource"]["identifier"][0]["value"]
        for e in referral["entry"]
        if e["resource"]["resourceType"] == "Patient"
    ]
    assert identifiers == [r["id"] for r in REFERRAL_CASES[:5]]  # labels join back by case id
    with pytest.raises(MendariError, match="can seed"):
        seed_bundle("mendari/ai-guard", 1)


def moved_input(data: dict[str, Any]) -> dict[str, Any]:
    """What the mapping should read: no as-of date (FHIR decides as of today), dates shifted so
    the case sits at TODAY, and the document dates as FHIR instants."""
    shift = TODAY - date.fromisoformat(data["as_of"])
    move = lambda day: (date.fromisoformat(day) + shift).isoformat()  # noqa: E731
    expected = {k: v for k, v in data.items() if k != "as_of"}
    expected["documents"] = [
        {**d, "date": f"{move(d['date'])}T12:00:00Z"} for d in data.get("documents", [])
    ]
    for key in ("reports", "labs"):
        expected[key] = [{**item, "date": move(item["date"])} for item in data.get(key, [])]
    return expected


def test_seeding_twice_is_a_no_op_even_with_hapis_search_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HAPI caches search results, including counts: the check must ask for fresh ones."""
    posted: list[dict[str, Any]] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/metadata"):
            return httpx.Response(200, json={})
        fresh = kwargs["headers"].get("cache-control") == "no-cache"
        total = sum(
            1
            for b in posted
            for e in b["entry"]
            if e["resource"]["resourceType"] == "ServiceRequest"
        )
        return httpx.Response(200, json={"total": total if fresh else 0})

    def post(url: str, **kwargs: Any) -> httpx.Response:
        posted.append(kwargs["json"])
        return httpx.Response(200, json={})

    monkeypatch.setattr(fhir_workflow.httpx, "get", get)
    monkeypatch.setattr(fhir_workflow.httpx, "post", post)
    assert seed("http://fhir.test/fhir", count=3, pack_id=REFERRAL) == 3
    assert seed("http://fhir.test/fhir", count=3, pack_id=REFERRAL) == 0
    assert len(posted) == 1


def test_the_trial_and_prior_auth_packs_and_the_ehr_profile_seed_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixtures' `seed` (tests/support/sandbox.py) takes the trial and prior-authorization
    packs' cases, and the vendor-shaped EHR profile (LP-23e). A pack already seeded is found by
    its first case's Patient (the product's `post_once`), so the prior-authorization pack's
    tagged ServiceRequests never pass for the referral router's; the profile's PUTs are sent
    every time, and replace what an earlier seed of it made."""
    posted: list[dict[str, Any]] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/metadata"):
            return httpx.Response(200, json={})
        assert url.endswith("/Patient") and kwargs["headers"].get("cache-control") == "no-cache"
        wanted = kwargs["params"]["identifier"]
        held = [
            identifier
            for bundle in posted
            for entry in bundle["entry"]
            if entry["resource"]["resourceType"] == "Patient"
            for identifier in entry["resource"]["identifier"]
            if f"{identifier['system']}|{identifier['value']}" == wanted
        ]
        return httpx.Response(200, json={"total": len(held)})

    def post(url: str, **kwargs: Any) -> httpx.Response:
        posted.append(kwargs["json"])
        return httpx.Response(200, json={})

    monkeypatch.setattr(fhir_workflow.httpx, "get", get)
    monkeypatch.setattr(fhir_workflow.httpx, "post", post)
    fhir = "http://fhir.test/fhir"
    assert seed(fhir, count=3, pack_id=PRIOR_AUTH) == 3  # ServiceRequests
    assert seed(fhir, count=3, pack_id=REFERRAL) == 3  # not taken for the above
    assert seed(fhir, count=2, pack_id=TRIAL) == 2  # Patients
    for pack_id in (PRIOR_AUTH, REFERRAL, TRIAL):
        assert seed(fhir, count=3, pack_id=pack_id) == 0, pack_id
    assert len(posted) == 3
    for _ in range(2):
        assert seed(fhir, count=0, pack_id=EHR_PROFILE) == 15  # its Patients
    assert posted[-1] == posted[-2] and len(posted) == 5
    assert {e["request"]["method"] for e in posted[-1]["entry"]} == {"PUT"}
    assert seed_bundle(TRIAL, 0)["entry"] == []


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
    demo = load_pack(DEMO_AGENT_DIR)
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
    adapter = FhirAdapter.for_pack(demo)
    for position, case in enumerate(seeded):
        pair = bundle["entry"][2 * position : 2 * position + 2]
        read = adapter.to_input(adapter.index({"resourceType": "Bundle", "entry": pair}))
        assert read == cases[case]["input"], case
    assert fhir_workflow.seed_bundle(demo, 0)["entry"] == []
    one = fhir_workflow.seed_bundle(demo, 1)
    assert cases[_case_ids(one)[0]]["labels"]["urgent_symptoms"] == "yes"


def test_seed_takes_another_message_agent_and_dataset_and_refuses_other_cases() -> None:
    """Another message agent is named (`--agent`), and its cases (`--dataset`: one of its own,
    or a file). Cases that are not messages are refused, saying what a message is."""
    inbox = resolve_pack(INBOX)
    local = str(DATASETS / "local-site-example.jsonl")
    assert _case_ids(fhir_workflow.seed_bundle(inbox, 10, local)) == _case_ids(
        seed_bundle(INBOX, 10)
    )
    own = fhir_workflow.labelled_cases(inbox, "red_flags").records
    named = fhir_workflow.seed_bundle(inbox, 4, "red_flags")
    assert len(_bodies(named)) == 4
    assert set(_bodies(named)) <= {record["input"]["message"]["body"] for record in own}
    with pytest.raises(MendariError, match="seeds portal messages, and mendari/referral-router's"):
        fhir_workflow.seed_bundle(resolve_pack(REFERRAL), 3)
    with pytest.raises(MendariError, match="neither a file nor an agent dataset"):
        fhir_workflow.seed_bundle(inbox, 3, "no-such-dataset")


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
    assert main([*worker, "http://e.test", "--agent", str(resolve_pack(INBOX).root)]) == 0
    assert main([*worker, "http://e.test", "--agent", "my-clinic/not-on-this-machine"]) == 0
    assert worked == [
        (FHIR, f"http://engine.test/api/agents/{DEMO_AGENT}/fhir/decide"),
        (FHIR, f"http://e.test/api/agents/{INBOX}/fhir/decide"),
        (FHIR, "http://e.test/api/agents/my-clinic/not-on-this-machine/fhir/decide"),
    ]
