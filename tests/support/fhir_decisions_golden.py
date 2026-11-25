# SPDX-License-Identifier: Apache-2.0
"""Write tests/fixtures/fhir/decisions-golden.json: how each agent with a FHIR mapping decides
each of its synthetic FHIR cases, on the day the golden pins (LP-23a).

The agents (`CASES`) are the test fixtures' two with a mapping, `mendari-test/triage` (a message:
a Communication in) and `mendari-test/chart-summary` (a patient's record in), and the bundled demo
agent, `mendari/portal-message-agent` (a message too). Each case is a line of one of the agent's
own datasets, turned into FHIR for that day: a message by the sandbox's builder
(`mendari.sandbox.fhir_records.message_entries`, as `mendari sandbox seed` loads one), a chart by
its fixture's (`tests/support/fixture_records.py`, whose records read back as the dataset's
inputs). It is decided through the agent's FHIR mapping (`Decision.decide_fhir`) with the agent's
own providers, offline (rules, and an agent variant's canned answers: no service is called), and
no store. For each case the golden holds the outcome, the recommendation, the priority, each
answer's label and the input hash; a case the FHIR path refuses holds its refusal instead. With
no store the hash is the unkeyed `decision.input_hash` of what the mapping read, so an equal hash
proves the mapping read the same input, byte for byte. A change to the FHIR path that is to leave
these decisions as they are shows here that it does; one that changes a decision on purpose
reruns this module, and its pull request explains each changed case.

    uv run python -m tests.support.fhir_decisions_golden          # write the golden
    uv run python -m tests.support.fhir_decisions_golden --check  # it is what the agents decide
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple
from unittest import mock

from mendari.core import clock
from mendari.core.decision import Decision
from mendari.core.pack import load_pack
from mendari.errors import FhirError
from mendari.evals.harness import resolve_dataset
from mendari.providers.agent import ENDPOINT_ENV, TOKEN_ENV
from mendari.sandbox.fhir_records import message_entries
from tests.conftest import CHART_DIR, DEMO_AGENT_DIR, ROOT, TRIAGE_DIR
from tests.support import fixture_records

GOLDEN = ROOT / "tests" / "fixtures" / "fhir" / "decisions-golden.json"
FORMAT = "mendari.fhir-decisions-golden/1"
# The day the cases sit at, and the day the agents decide on (`clock.today`): the day the chart
# fixture's records are built for, so an age read from a birth date is the case's age.
TODAY = fixture_records.TODAY
NOON = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)  # when the messages were sent

Build = Callable[[dict[str, Any]], list[dict[str, Any]]]


class Golden(NamedTuple):
    """An agent the golden pins: its folder, the dataset of its own whose cases it decides, and
    how a case of that dataset becomes FHIR (the entries of a collection Bundle)."""

    directory: Path
    dataset: str
    build: Build


def messages(record: dict[str, Any]) -> list[dict[str, Any]]:
    """A message case as the sandbox seeds it: its sender and the Communication, sent at noon."""
    return message_entries(record, now=NOON)


@cache
def _charts() -> dict[str, fixture_records.Case]:
    return {
        case.id: case for split in fixture_records.PLANS for case in fixture_records.cases(split)
    }


def chart(record: dict[str, Any]) -> list[dict[str, Any]]:
    """A chart case's record, as its fixture builds it for `TODAY`."""
    return _charts()[record["id"]].entries(TODAY)


CASES: dict[str, Golden] = {
    "mendari-test/triage": Golden(TRIAGE_DIR, "test", messages),
    "mendari-test/chart-summary": Golden(CHART_DIR, "test", chart),
    # Its hand-written examples, not its generated benchmark: the golden pins how the demo agent
    # reads FHIR, and moves only when that does, not when `scripts/demo_agent_data.py` does.
    "mendari/portal-message-agent": Golden(DEMO_AGENT_DIR, "examples", messages),
}


def records(agent_id: str) -> list[dict[str, Any]]:
    """The cases the golden pins for `agent_id`: its dataset's lines, in the file's order."""
    golden = CASES[agent_id]
    return resolve_dataset(load_pack(golden.directory), golden.dataset).records


@contextmanager
def on_the_day() -> Iterator[None]:
    """Decide as of `TODAY` (ages and date windows read `clock.today`), and offline: an agent
    variant answers from its canned answers, whatever service the shell names."""
    with mock.patch.object(clock, "today", lambda: TODAY), mock.patch.dict(os.environ):
        for name in (ENDPOINT_ENV, TOKEN_ENV):
            os.environ.pop(name, None)
        yield


def offline(decision: Decision) -> bool:
    """Whether every provider answers here: a local one, or an agent from its canned answers."""
    return all(
        not provider.remote or getattr(provider, "canned", None) is not None
        for provider in decision.providers.values()
    )


def decided(decision: Decision, record: dict[str, Any], build: Build) -> dict[str, Any]:
    """One case as the golden holds it."""
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": build(record)}
    try:
        result, _ = decision.decide_fhir(bundle)
    except FhirError as error:  # e.g. a record without what the mapping needs to decide
        return {"id": record["id"], "refused": str(error)}
    return {
        "id": record["id"],
        "outcome": result.outcome.value,
        "recommendation": result.recommendation,
        "priority": result.priority.value,
        "answers": {key: result.answers[key].label for key in sorted(result.answers)},
        "input_hash": result.input_hash,
    }


def golden() -> dict[str, Any]:
    """Every case of every agent, decided as of `TODAY`."""
    agents: dict[str, Any] = {}
    with on_the_day():
        for agent_id, (directory, dataset, build) in CASES.items():
            decision = Decision.from_path(directory)  # no store: unkeyed hashes
            if decision.pack.id != agent_id or not offline(decision):
                raise SystemExit(f"{directory}: not {agent_id} answering offline")
            agents[agent_id] = {
                "dataset": dataset,
                "cases": [decided(decision, record, build) for record in records(agent_id)],
            }
    return {"format": FORMAT, "today": TODAY.isoformat(), "packs": agents}


def render(data: dict[str, Any]) -> str:
    """The golden as committed: one case a line, its id first, one trailing newline."""
    lines = ["{", f'  "format": {json.dumps(data["format"])},', f'  "today": "{data["today"]}",']
    lines.append('  "packs": {')
    for position, (agent_id, agent) in enumerate(data["packs"].items()):
        lines += [f"    {json.dumps(agent_id)}: {{", f'      "dataset": "{agent["dataset"]}",']
        lines.append('      "cases": [')
        lines.append(",\n".join(f"        {json.dumps(case)}" for case in agent["cases"]))
        lines += ["      ]", "    }" + ("," if position < len(data["packs"]) - 1 else "")]
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        if not GOLDEN.is_file() or GOLDEN.read_text(encoding="utf-8") != render(golden()):
            print(
                f"{GOLDEN.relative_to(ROOT)} is not what the agents decide: if the change is "
                "meant, run `python -m tests.support.fhir_decisions_golden` and explain each "
                "changed case",
                file=sys.stderr,
            )
            return 1
        return 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(render(golden()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
