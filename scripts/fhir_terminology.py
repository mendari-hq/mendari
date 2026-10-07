# SPDX-License-Identifier: Apache-2.0
"""Write Mendari's terminology (CodeSystems and NamingSystems) for the HL7 validator (CI job
`fhir-conformance`), one file per resource, as the server serves them for the packs installed
here (the latest version of each), plus the sandbox's experimental systems, which are never
served. The validator loads the folder (`--ig`) and checks it (`--valid`).

    uv run python scripts/fhir_terminology.py var/fhir-terminology
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mendari.core.pack import Pack
from mendari.core.registry import discover, version_key
from mendari.standards import terminology


def main(out: Path) -> None:
    latest: dict[str, Pack] = {}
    for pack in discover().packs:
        current = latest.get(pack.id)
        if current is None or version_key(pack.version) > version_key(current.version):
            latest[pack.id] = pack
    found = [*terminology.resources(latest.values()), *terminology.sandbox_resources()]
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.json"):
        stale.unlink()
    for resource in found:
        path = out / f"{resource['resourceType']}-{resource['id']}.json"
        path.write_text(json.dumps(resource, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(found)} terminology resources written to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "var/fhir-terminology"))
