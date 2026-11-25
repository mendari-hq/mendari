# SPDX-License-Identifier: Apache-2.0
"""Write the FHIR resources Mendari produces from FHIR input, for the HL7 validator (CI job
`fhir-conformance`), which checks them against Mendari's terminology (`--ig`):

- `task-message.json`: the Task the bundled demo agent makes from the US Core portal-message
  fixture (`tests/fixtures/us-core/triage.json`), read by its keyword rules (`sample_agent`);
- what the FHIR routes serve, from a local server: `capability.json` (`/fhir/metadata`),
  `operation-definition-decide.json`, and `decide-message.json` (`$decide` on that fixture);
- `served-*.json`: what the read and search routes answer for synthetic decisions made through
  the API (`served_reads_and_searches`): a Task read and vread (a decision reviewed, rated and
  given an outcome: versions 1-3), Task searchsets (a page with a `next` link, a lenient search
  with its warning entry, a `_summary=count` Bundle), an AuditEvent and a Provenance read and
  searchset, and a CodeSystem searchset. `tests/test_fhir_routes.py` runs the same requests
  through the in-process structural gate.

The product bundles one agent, which reads a portal message. An agent of another kind is given
with `--fixture NAME=AGENT`, any number of times: `task-NAME.json` and `decide-NAME.json`, as
above, for the US Core fixture of that name (`tests/fixtures/us-core/NAME.json`) decided by
AGENT (an id or a directory) with its own default providers; and, where an invalid fixture of
that name exists (`invalid/NAME.json`), `decide-NAME-refused.json`: that input under `enforce`,
a refused input with its findings.

    uv run python scripts/fhir_output_sample.py var/fhir-samples
    uv run python scripts/fhir_output_sample.py var/fhir-samples \\
      --fixture chart=path/to/an-agent-that-reads-a-patients-record
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sample_agent, beside this one

from sample_agent import (  # noqa: E402
    DEMO_AGENT_DIR,
    EMERGENCY,
    LOCAL,
    REFILL,
    TWO_REQUESTS,
    demo_decision,
)

from mendari import AuditStore, Decision  # noqa: E402
from mendari.core.agent import Agent  # noqa: E402
from mendari.core.registry import resolve_agent  # noqa: E402
from mendari.errors import MendariError  # noqa: E402
from mendari.sandbox import DEMO_AGENT  # noqa: E402
from mendari.server.app import create_app  # noqa: E402
from mendari.standards.fhir import DECISION_ID_SYSTEM  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "us-core"
MESSAGE = "message"  # the demo agent's samples, from the portal-message fixture
MESSAGE_FIXTURE = FIXTURES / "triage.json"
SERVER = "https://mendari.example"  # a reserved name: the samples' links point nowhere real
FORM = {"content-type": "application/x-www-form-urlencoded"}
# The CapabilityStatement, beside the other samples: the US Core run validates it.
CAPABILITY = Path("capability.json")


def main(out: Path, fixtures: Mapping[str, Agent] | None = None) -> None:
    """`fixtures`: the agent that decides each US Core fixture, by the fixture's name."""
    given = dict(fixtures or {})
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        store = AuditStore(Path(scratch) / "audit.db")
        decisions = {MESSAGE: (demo_decision(store), MESSAGE_FIXTURE)}
        for name, agent in given.items():
            decisions[name] = (Decision(agent, store=store), fixture(name))
        for name, (decision, source) in decisions.items():
            result, task = decision.decide_fhir(json.loads(source.read_text(encoding="utf-8")))
            written = out / f"task-{name}.json"
            write(written, task)
            print(f"{decision.agent.id}: {result.outcome.value}, Task written to {written}")
        store.close()
    served(out, given)


def fixture(name: str) -> Path:
    return FIXTURES / f"{name}.json"


def engine(db_path: str | Path, agents: Iterable[Agent] = (), **settings: Any) -> FastAPI:
    """A local engine that serves the demo agent, read by its rules, and `agents`."""
    return create_app(
        db_path=db_path,
        agents_paths=[DEMO_AGENT_DIR, *(agent.root for agent in agents)],
        default_providers={DEMO_AGENT: LOCAL},
        **settings,
    )


def served(out: Path, fixtures: Mapping[str, Agent]) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        for mode in ("warn", "enforce"):
            app = engine(Path(scratch) / f"{mode}.db", fixtures.values(), fhir_validation=mode)
            client = TestClient(app, raise_server_exceptions=True)
            if mode == "enforce":
                for name, agent in fixtures.items():
                    invalid = FIXTURES / "invalid" / f"{name}.json"
                    if invalid.is_file():
                        refused = decide(client, agent.id, invalid)
                        write(out / f"decide-{name}-refused.json", refused)
                continue
            write(out / CAPABILITY, client.get("/fhir/metadata").json())
            definition = client.get("/fhir/OperationDefinition/decide").json()
            write(out / "operation-definition-decide.json", definition)
            write(out / f"decide-{MESSAGE}.json", decide(client, DEMO_AGENT, MESSAGE_FIXTURE))
            for name, agent in fixtures.items():
                write(out / f"decide-{name}.json", decide(client, agent.id, fixture(name)))
    print(f"CapabilityStatement, OperationDefinition and $decide Bundles written to {out}")
    with tempfile.TemporaryDirectory() as scratch:
        client = TestClient(
            engine(Path(scratch) / "search.db"), base_url=SERVER, raise_server_exceptions=True
        )
        answers = served_reads_and_searches(client)
    for name, resource in answers.items():
        write(out / f"{name}.json", resource)
    print(f"{len(answers)} read and search answers written to {out}/served-*.json")


def served_reads_and_searches(client: TestClient) -> dict[str, Any]:
    """What the `/fhir` read and search routes answer, by sample name, for synthetic decisions
    the demo agent made through the API of an `engine`: one reviewed (overridden), rated and
    given an outcome (Task versions 1-3), a routine message (automated) and an abstention
    still waiting for review."""

    def decided(message: Mapping[str, Any]) -> str:
        return str(call(client.post(f"/api/agents/{DEMO_AGENT}/decide", json=message))["id"])

    reviewed = decided(EMERGENCY)
    call(
        client.post(
            f"/api/review/{reviewed}", json={"final_label": "appointment", "reviewer": "rn-1"}
        )
    )
    call(client.post(f"/api/audit/{reviewed}/feedback", json={"rating": "up"}))
    outcome = {"final_label": "other", "source": "staff-reroute"}
    call(client.post(f"/api/audit/{reviewed}/outcome", json=outcome))
    decided(REFILL)
    decided(TWO_REQUESTS)
    seq = {e["event"]: e["seq"] for e in call(client.get(f"/api/audit/{reviewed}"))["events"]}
    lenient = {"prefer": "handling=lenient"}
    target = f"target:identifier={DECISION_ID_SYSTEM}|{reviewed}&_count=2"
    return {
        "served-task": call(client.get(f"/fhir/Task/{reviewed}")),
        "served-task-v2": call(client.get(f"/fhir/Task/{reviewed}/_history/2")),
        "served-searchset-task": call(client.get("/fhir/Task?_count=1&_total=accurate")),
        "served-searchset-lenient": call(
            client.get("/fhir/Task?status=requested&patient=Patient/p1&stauts=x", headers=lenient)
        ),
        "served-searchset-count": call(
            client.post("/fhir/Task/_search", content="_summary=count", headers=FORM)
        ),
        "served-auditevent": call(
            client.get(f"/fhir/AuditEvent/{reviewed}-{seq['review.resolved']}")
        ),
        "served-searchset-auditevent": call(client.get(f"/fhir/AuditEvent?entity=Task/{reviewed}")),
        "served-provenance": call(
            client.get(f"/fhir/Provenance/{reviewed}-p{seq['outcome.recorded']}")
        ),
        "served-searchset-provenance": call(
            client.post("/fhir/Provenance/_search", content=target, headers=FORM)
        ),
        "served-searchset-codesystem": call(client.get("/fhir/CodeSystem?_count=3")),
    }


def call(response: Any) -> Any:
    response.raise_for_status()
    return response.json()


def decide(client: TestClient, agent_id: str, source: Path) -> Any:
    data = json.loads(source.read_text(encoding="utf-8"))
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "agent", "valueString": agent_id},
            {"name": "data", "resource": data},
        ],
    }
    response = client.post("/fhir/$decide", json=parameters)
    response.raise_for_status()
    return response.json()


def write(path: Path, resource: dict[str, Any]) -> None:
    path.write_text(json.dumps(resource, indent=2) + "\n", encoding="utf-8")


def _fixture_agent(value: str) -> tuple[str, Agent]:
    """`NAME=AGENT` as (the US Core fixture's name, the agent that decides it)."""
    name, separator, agent = value.partition("=")
    if not separator or not name or not agent:
        raise argparse.ArgumentTypeError(f"{value!r}: write NAME=AGENT")
    if name == MESSAGE or not fixture(name).is_file():
        known = ", ".join(sorted(path.stem for path in FIXTURES.glob("*.json")))
        raise argparse.ArgumentTypeError(f"{name!r} is no US Core fixture here ({known})")
    try:
        return name, resolve_agent(agent)
    except MendariError as error:
        raise argparse.ArgumentTypeError(f"{agent}: {error}") from None


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("out", nargs="?", type=Path, default=Path("var/fhir-samples"))
    parser.add_argument(
        "--fixture",
        action="append",
        default=[],
        type=_fixture_agent,
        metavar="NAME=AGENT",
        help="also decide the US Core fixture NAME with AGENT, an id or a directory: an agent "
        "of a kind the product does not bundle (a patient's record, say)",
    )
    args = parser.parse_args(argv)
    main(args.out, dict(args.fixture))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
