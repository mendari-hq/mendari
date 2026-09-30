"""Da Vinci prior-authorization standards around a pack's decision on an order.

**CRD** (Coverage Requirements Discovery, STU 2.2): when a pack declares
`cds_hooks.coverage_information`, its order-hook response carries, for each decided order, a
system action that updates the order with CRD's `coverage-information` extension: covered or
not, whether prior authorization is needed, which documentation is needed and why, the patient's
Coverage, the date and an assertion id. The assertion id is the decision id, so the assertion
traces back to its audit record. Cards are unchanged, so an EHR that ignores system actions sees
what it saw before.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import date
from typing import Any

from openhealth.core.decision import DecisionResult
from openhealth.core.spec import CoverageInformation

CRD = "http://hl7.org/fhir/us/davinci-crd"
COVERAGE_INFORMATION = f"{CRD}/StructureDefinition/ext-coverage-information"


def coverage_reference(prefetch: Mapping[str, Any]) -> dict[str, str] | None:
    """The patient's Coverage from the `coverage` prefetch (a resource or a search Bundle)."""
    found = prefetch.get("coverage")
    if isinstance(found, Mapping) and found.get("resourceType") == "Bundle":
        entries = [e.get("resource") for e in found.get("entry") or [] if isinstance(e, Mapping)]
        found = next((r for r in entries if isinstance(r, Mapping)), None)
    if isinstance(found, Mapping) and found.get("resourceType") == "Coverage" and found.get("id"):
        return {"reference": f"Coverage/{found['id']}"}
    return None


def coverage_information(
    info: CoverageInformation, result: DecisionResult, coverage: Mapping[str, str], today: date
) -> dict[str, Any]:
    """The `coverage-information` extension for one decided order."""
    parts: list[dict[str, Any]] = [
        {"url": "coverage", "valueReference": dict(coverage)},
        {"url": "covered", "valueCode": info.covered},
    ]
    if info.pa_needed is not None:
        parts.append({"url": "pa-needed", "valueCode": info.pa_needed})
    parts += [{"url": "doc-needed", "valueCode": code} for code in info.doc_needed]
    parts += [{"url": "doc-purpose", "valueCode": code} for code in info.doc_purpose]
    parts += [{"url": "info-needed", "valueCode": code} for code in info.info_needed]
    if info.reason:
        parts.append({"url": "reason", "valueCodeableConcept": {"text": info.reason}})
    parts += [
        {"url": "date", "valueDate": today.isoformat()},
        {"url": "coverage-assertion-id", "valueString": result.id},
    ]
    return {"url": COVERAGE_INFORMATION, "extension": parts}


def system_action(order: Mapping[str, Any], extension: Mapping[str, Any]) -> dict[str, Any]:
    """A CDS Hooks `update` of the draft order, with the coverage information added."""
    updated = copy.deepcopy(dict(order))
    updated["extension"] = [*updated.get("extension", []), dict(extension)]
    return {
        "type": "update",
        "description": "Coverage information for this order (Da Vinci CRD)",
        "resource": updated,
    }
