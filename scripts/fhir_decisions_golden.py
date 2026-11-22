# SPDX-License-Identifier: Apache-2.0
"""Write tests/fixtures/fhir/decisions-golden.json: how each pack with a FHIR mapping decides
each of its synthetic FHIR cases, on the day the golden pins (LP-23a).

The cases are each pack's local-site example (`datasets/`), turned into FHIR by the sandbox's
builders (`mendari.sandbox.fhir_records`) for that day, and decided through the pack's FHIR
mapping (`Decision.decide_fhir`) with the pack's own local providers and no store. For each case
the golden holds the outcome, the recommendation, the priority, each answer's label and the
input hash; a case the FHIR path refuses holds its refusal instead. With no store the hash is
the unkeyed `decision.input_hash` of what the mapping read, so an equal hash proves the mapping
read the same input, byte for byte. LP-23 changes the FHIR path in steps (failure behaviour,
mapping syntax 0.3, linked reads, a site code map) that are each to leave these decisions as
they are, until a pack adopts the new syntax (23k), whose pull request explains each change.

    uv run python scripts/fhir_decisions_golden.py          # write the golden
    uv run python scripts/fhir_decisions_golden.py --check  # the file is what the packs decide
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from mendari.core import clock
from mendari.core.decision import Decision
from mendari.errors import FhirError
from mendari.evals.harness import load_dataset
from mendari.sandbox.fhir_records import (
    inbox_entries,
    prior_auth_entries,
    referral_entries,
    trial_entries,
)

ROOT = Path(__file__).resolve().parent.parent
PACKS = ROOT / "tests" / "fixtures" / "reference-packs"  # the fixture packs the golden pins
GOLDEN = ROOT / "tests" / "fixtures" / "fhir" / "decisions-golden.json"
FORMAT = "mendari.fhir-decisions-golden/1"
# The day the cases sit at, and the day the packs decide on (`clock.today`).
TODAY = date(2026, 10, 17)
NOON = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)  # when the inbox's messages were sent

Build = Callable[[dict[str, Any]], list[dict[str, Any]]]
# Each pack with a FHIR mapping: its directory, its local-site example and its builder.
CASES: dict[str, tuple[str, str, Build]] = {
    "mendari/patient-inbox-router": (
        "patient-inbox-router",
        "local-site-example.jsonl",
        lambda record: inbox_entries(record, now=NOON),
    ),
    "mendari/referral-router": (
        "referral-router",
        "referral-local-site-example.jsonl",
        lambda record: referral_entries(record, today=TODAY),
    ),
    "mendari/clinical-trial-prescreener": (
        "clinical-trial-prescreener",
        "trial-local-site-example.jsonl",
        lambda record: trial_entries(record, today=TODAY),
    ),
    "mendari/prior-auth-preflight": (
        "prior-auth-preflight",
        "prior-auth-local-site-example.jsonl",
        lambda record: prior_auth_entries(record, today=TODAY),
    ),
}


@contextmanager
def on_the_day() -> Iterator[None]:
    """Decide as of `TODAY` (ages and date windows read `clock.today`)."""
    with mock.patch.object(clock, "today", lambda: TODAY):
        yield


def decided(decision: Decision, record: dict[str, Any], build: Build) -> dict[str, Any]:
    """One case as the golden holds it."""
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": build(record)}
    try:
        result, _ = decision.decide_fhir(bundle)
    except FhirError as error:  # e.g. an order the pack has no requirements for
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
    """Every case of every pack, decided as of `TODAY`."""
    packs: dict[str, Any] = {}
    with on_the_day():
        for pack_id, (directory, dataset, build) in CASES.items():
            decision = Decision.from_path(PACKS / directory)  # no store: unkeyed hashes
            if decision.pack.id != pack_id or decision.is_remote:
                raise SystemExit(f"{directory}: not {pack_id} on local providers")
            records = load_dataset(ROOT / "tests" / "fixtures" / "datasets" / dataset).records
            packs[pack_id] = {
                "dataset": f"datasets/{dataset}",
                "cases": [decided(decision, record, build) for record in records],
            }
    return {"format": FORMAT, "today": TODAY.isoformat(), "packs": packs}


def render(data: dict[str, Any]) -> str:
    """The golden as committed: one case a line, its id first, one trailing newline."""
    lines = ["{", f'  "format": {json.dumps(data["format"])},', f'  "today": "{data["today"]}",']
    lines.append('  "packs": {')
    for position, (pack_id, pack) in enumerate(data["packs"].items()):
        lines += [f"    {json.dumps(pack_id)}: {{", f'      "dataset": "{pack["dataset"]}",']
        lines.append('      "cases": [')
        lines.append(",\n".join(f"        {json.dumps(case)}" for case in pack["cases"]))
        lines += ["      ]", "    }" + ("," if position < len(data["packs"]) - 1 else "")]
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        if not GOLDEN.is_file() or GOLDEN.read_text(encoding="utf-8") != render(golden()):
            print(
                f"{GOLDEN.relative_to(ROOT)} is not what the packs decide: if the change is "
                "meant, run scripts/fhir_decisions_golden.py and explain each changed case",
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
