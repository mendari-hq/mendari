"""Generate `tests/fixtures/fhir/r4-search-parameters.json` from the R4 core package.

The standard R4 SearchParameters of the types Mendari searches (Task, AuditEvent, Provenance,
CodeSystem) and of Resource and DomainResource, which every one of them inherits: canonical url,
code, type and the bases among those. Tests check `mendari.standards.search` against it: every
supported parameter is the R4 SearchParameter it claims, and the standard parameters Mendari does
not support (a `400` under its hybrid strictness) are exactly R4's for the type. The fixture is
checked in, so the tests run without the package; when the package is cached in `var/`, a test
regenerates it and compares. Experimental examples (`SearchParameter/example`) are left out.

Usage (the package is downloaded once and verified by digest):

    uv run python scripts/r4_search_parameters.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from us_core_subset import PACKAGES, package

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests/fixtures/fhir/r4-search-parameters.json"
BASES = ("Task", "AuditEvent", "Provenance", "CodeSystem", "Resource", "DomainResource")


def search_parameters(files: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for resource in files.values():
        if not isinstance(resource, dict) or resource.get("resourceType") != "SearchParameter":
            continue
        bases = [base for base in resource.get("base", []) if base in BASES]
        if bases and not resource.get("experimental"):
            found.append(
                {
                    "url": resource["url"],
                    "code": resource["code"],
                    "type": resource["type"],
                    "base": sorted(bases),
                }
            )
    return sorted(found, key=lambda p: (p["url"], p["code"]))


def main() -> None:
    parameters = search_parameters(package(*PACKAGES["r4"]))
    fixture = {"source": "hl7.fhir.r4.core#4.0.1", "bases": list(BASES), "parameters": parameters}
    OUT.write_text(json.dumps(fixture, indent=1) + "\n", encoding="utf-8")
    print(f"{len(parameters)} SearchParameters written to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
