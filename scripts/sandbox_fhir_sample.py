# SPDX-License-Identifier: Apache-2.0
"""Write what the sandbox posts to its FHIR server, for the HL7 validator (CI job
`fhir-conformance`), each held to US Core 6.1.0: the bundled demo agent's portal messages, as
`mendari sandbox seed` loads them, and what the tests' own sandbox loads for the fixture agents
(`tests/support/sandbox.py`): another agent's portal messages, referral cases, and the
vendor-shaped EHR profile (LP-23e).

uv run python scripts/sandbox_fhir_sample.py var/fhir-samples
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # tests.support: the fixture agents' sandbox

from tests.support.sandbox import EHR_PROFILE, INBOX, REFERRAL, seed_bundle  # noqa: E402

from mendari.core.pack import load_pack  # noqa: E402
from mendari.sandbox import fhir_workflow  # noqa: E402

DEMO_AGENT_DIR = ROOT / "agents" / "portal-message-agent"


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    samples = {
        "sandbox-messages": fhir_workflow.seed_bundle(load_pack(DEMO_AGENT_DIR), 20),
        "sandbox-inbox": seed_bundle(INBOX, 20),
        "sandbox-referral": seed_bundle(REFERRAL, 40),
        "sandbox-ehr-profile": seed_bundle(EHR_PROFILE, 0),  # the whole profile: no count applies
    }
    for name, bundle in samples.items():
        (out / f"{name}.json").write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        print(f"{len(bundle['entry'])} resources written to {out / name}.json")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-samples"))
