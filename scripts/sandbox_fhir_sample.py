# SPDX-License-Identifier: Apache-2.0
"""Write what `mendari sandbox seed` posts, for the HL7 validator (CI job `fhir-conformance`).

uv run python scripts/sandbox_fhir_sample.py var/fhir-samples
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mendari.sandbox.fhir_workflow import INBOX, REFERRAL, seed_bundle


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for pack_id, name, count in ((INBOX, "sandbox-inbox", 20), (REFERRAL, "sandbox-referral", 40)):
        bundle = seed_bundle(pack_id, count)
        (out / f"{name}.json").write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        print(f"{len(bundle['entry'])} resources written to {out / name}.json")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-samples"))
