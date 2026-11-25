# SPDX-License-Identifier: Apache-2.0
"""The triage fixture's synthetic messages as FHIR: its US Core fixture, and a message as a
collection Bundle.

Synthetic test fixture data. Not clinically reviewed. Not for patient care.

`mendari-test/triage` (`tests/fixtures/reference-agents/triage`, fixture F1 of wave 6) decides a
patient's message to a clinic: a Communication, from a Patient its mapping does not read. A
message is built by the sandbox's own builder (`mendari.sandbox.fhir_records.message_entries`,
which `mendari sandbox seed` loads a message with) on a fixed day, each resource then given a
fixed id and a fullUrl under `BASE` in place of its fresh `urn:uuid`, the Communication naming its
sender as `Patient/<id>` (`collection`): so a test, and the file below, get the same Bundle on
every run.

The file is `tests/fixtures/us-core/triage.json` (`CASE`, one message; the HL7 validator checks it
in CI, `scripts/fhir_validate.py --valid`, as the in-process checks do):

    uv run python -m tests.support.triage_records          # write it
    uv run python -m tests.support.triage_records --check  # exit 1 if the committed one differs
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mendari.sandbox.code_systems import SANDBOX_TAG
from mendari.sandbox.fhir_records import message_entries

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "us-core" / "triage.json"
BASE = "http://example.org/fhir"  # where the Bundle says its resources are
SENT = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)  # when the message was sent
PATIENT_ID, COMMUNICATION_ID = "tr-p1", "tr-c1"
MESSAGE = "Can I get a refill of my blood pressure medicine?"  # the fixture's case text
# The message the file holds: a refill, which the fixture's rules send to the pharmacy.
CASE: dict[str, Any] = {
    "id": "triage-us-core-1",
    "input": {"message": {"subject": "Refill", "body": MESSAGE, "channel": "portal"}},
}


def _with_id(resource: dict[str, Any], resource_id: str) -> dict[str, Any]:
    """`resource` with its id, after its type."""
    rest = {key: value for key, value in resource.items() if key not in ("resourceType", "id")}
    return {"resourceType": resource["resourceType"], "id": resource_id, **rest}


def collection(
    record: dict[str, Any],
    *,
    patient_id: str = PATIENT_ID,
    communication_id: str = COMMUNICATION_ID,
    base: str = BASE,
    sent: datetime = SENT,
    bundle_id: str | None = None,
) -> dict[str, Any]:
    """A message case (`{id, input: {message}}`) as a collection Bundle: its sender and its
    Communication, as the sandbox builds them, under the ids given and fullUrls under `base`."""
    sender, message = (entry["resource"] for entry in message_entries(record, now=sent))
    patient = _with_id(sender, patient_id)
    communication = _with_id(message, communication_id)
    for name in ("subject", "sender"):
        communication[name] = {"reference": f"Patient/{patient_id}"}
    found: dict[str, Any] = {"resourceType": "Bundle"}
    if bundle_id is not None:
        found["id"] = bundle_id
    found |= {
        "meta": {"tag": [SANDBOX_TAG]},
        "type": "collection",
        "entry": [
            {"fullUrl": f"{base}/Patient/{patient_id}", "resource": patient},
            {"fullUrl": f"{base}/Communication/{communication_id}", "resource": communication},
        ],
    }
    return found


def us_core_triage() -> dict[str, Any]:
    """`tests/fixtures/us-core/triage.json`: `CASE` as a collection Bundle, valid US Core 6.1.0."""
    return collection(CASE, bundle_id="triage")


def written() -> dict[Path, str]:
    """Every file this module writes, by path, with its content."""
    return {FIXTURE: json.dumps(us_core_triage(), indent=2, ensure_ascii=True) + "\n"}


def differing() -> list[Path]:
    """The files whose committed content is not what this module writes."""
    return [
        path
        for path, text in written().items()
        if not path.is_file() or path.read_text(encoding="utf-8") != text
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if a committed file is not what it writes"
    )
    arguments = parser.parse_args(argv)
    if arguments.check:
        stale = differing()
        for path in stale:
            print(
                f"{path.relative_to(ROOT)} is not what tests/support/triage_records.py writes: "
                "run `uv run python -m tests.support.triage_records`",
                file=sys.stderr,
            )
        return 1 if stale else 0
    for path, text in written().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
