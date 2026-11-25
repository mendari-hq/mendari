# SPDX-License-Identifier: Apache-2.0
"""What the tests of record reading use the chart-summary fixture with.

Synthetic test fixture. Not clinically reviewed. Not for patient care.

`tests/fixtures/reference-agents/chart-summary` (`mendari-test/chart-summary`) is a
`patient-view` service. Its records and datasets are `tests/support/fixture_records.py`'s. This
module gives the tests:

- `chart_copy`: a copy of the agent under its own id (so a copy served beside the bundled
  agent, or loaded as one from a registry, never stands in its place), its FHIR mapping changed
  as a test needs: what `mini()` of `tests/test_spec_03.py` did for the prescreen fixture. Spec
  versions under 0.3 start from `MAPPING_0_2`, a mapping with no spec 0.3 key.
- `order_sign`: the agent's `order-sign` twin. A manifest declares one CDS hook, so the order
  hook is a copy: its focus a ServiceRequest whose code is in its own code set `checked_orders`
  (ICD-10-PCS, which a site's order codes may be translated into), the order's code, text and
  priority in its input beside the chart, and a card for every order it decides.
- `prefetch` and `hook`: a case's record as the patient-view hook's prefetch (each search key a
  searchset, each request's Medication included as `_include` asks), and the hook's request.
  `order_hook` is the twin's, the case's order a draft order.
"""

from __future__ import annotations

import copy
import json
import shutil
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from tests.support.fixture_records import (
    AGENT,
    AGENT_ID,
    BASE,
    ICD10PCS,
    ORDER_CODE_SET,
    ORDERS,
    PRESCRIBER,
    TODAY,
    Case,
    Site,
)

CHART_DIR = AGENT
CHART_ID = AGENT_ID
SERVICE = "/cds-services/mendari-test.chart-summary"
COPY_ID = "mendari-test/chart-copy"  # a changed copy's id: never the bundled agent's
ORDERS_ID = "mendari-test/chart-orders"  # the order-sign twin's
ORDERS_SERVICE = "/cds-services/mendari-test.chart-orders"
PRACTITIONER = f"Practitioner/{PRESCRIBER}"
# The prefetch keys of the patient-view hook, each with the resource types its search returns.
KEYS = {
    "patient": ("Patient",),
    "conditions": ("Condition",),
    "medications": ("MedicationStatement",),
    "medication_requests": ("MedicationRequest", "Medication"),
    "notes": ("DocumentReference",),
}

# The agent's mapping written with no spec 0.3 key (`newer_keys` finds none): what a copy at spec
# 0.1 or 0.2 starts from. Notes are 0.2's `base64_text` over the attachments' data, codings the
# first of each concept, and medications MedicationStatements alone.
MAPPING_0_2: dict[str, Any] = {
    "fhir_version": "R4",
    "focus": "Patient",
    "resources": {"Patient": {"required": True}},
    "fields": {
        "patient.age": {"path": "Patient.birthDate", "transform": "age_years"},
        "patient.sex": {
            "path": "Patient.gender",
            "map": {"female": "female", "male": "male", "other": "other", "unknown": "unknown"},
        },
        "notes": {"path": "DocumentReference.content.attachment.data", "transform": "base64_text"},
    },
    "lists": {
        "conditions": {
            "resource": "Condition",
            "fields": {
                "code": {"path": "code.coding[0].code"},
                "text": {"path": "code.text"},
                "status": {"path": "clinicalStatus.coding[0].code"},
                "onset": {"path": "onsetDateTime"},
            },
        },
        "medications": {
            "resource": "MedicationStatement",
            "fields": {
                "code": {"path": "medicationCodeableConcept.coding[0].code"},
                "name": {"path": "medicationCodeableConcept.text"},
                "status": {"path": "status"},
                "start": {"path": "effectivePeriod.start"},
                "end": {"path": "effectivePeriod.end"},
            },
        },
    },
}

Edit = Callable[[dict[str, Any]], None]


def _read(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _write(path: Path, data: Any) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")


def merged(mapping: dict[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """`mapping` with `changes`: a mapping value merged into a mapping key's (one level deep: a
    field or list given replaces the one of that name), any other value put in its place."""
    result = copy.deepcopy(mapping)
    for key, value in changes.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **copy.deepcopy(dict(value))}
        else:
            result[key] = copy.deepcopy(value)
    return result


def _older(spec: str) -> bool:
    return tuple(int(part) for part in spec.split(".")) < (0, 3)


def chart_copy(
    base: Path,
    changes: Mapping[str, Any] | None = None,
    *,
    spec: str | None = None,
    systems: Iterable[str] = (),
    use: str | None = None,
    agent_id: str = COPY_ID,
    manifest: Edit | None = None,
    schema: Edit | None = None,
    folder: str = "chart",
) -> Path:
    """A copy of the chart-summary agent in `base/<folder>` (one level deep, so `base` can be
    served as a search path), under `agent_id`; its directory.

    - `spec`: its `spec_version`. Under 0.3 the mapping starts from `MAPPING_0_2`, and the card
      loses its `refused` key (spec 0.3).
    - `systems`: code systems declared besides the agent's own (each named as fictional), for a
      test's `[system=...]` predicates and code sets.
    - `changes`: merged into the mapping (`merged`).
    - `use`: the variant the `reader` slot uses (`"rules"` reads the notes; `"agent"`, the
      default, answers from its canned answers).
    - `manifest` and `schema`: edits of the manifest and the input schema, made last."""
    root = base / folder
    shutil.copytree(CHART_DIR, root)
    data = _read(root / "agent.yaml")
    data["id"] = agent_id
    if spec is not None:
        data["spec_version"] = spec
    known = {use_["url"] for use_ in data.get("code_systems", [])}
    data["code_systems"] = [
        *data.get("code_systems", []),
        *({"url": url, "name": f"Fictional: {url}"} for url in systems if url not in known),
    ]
    if use is not None:
        data["providers"]["reader"]["use"] = use
    older = spec is not None and _older(spec)
    if older:
        card = data["cds_hooks"]["card"]
        for key in ("summaries", "indicators"):
            card[key].pop("refused", None)
        card["show"] = [label for label in card["show"] if label != "refused"]
    if manifest is not None:
        manifest(data)
    _write(root / "agent.yaml", data)
    mapping_path = root / "fhir" / "mapping.yaml"
    own = _read(mapping_path)
    start = merged(MAPPING_0_2, {"task": own["task"]}) if older else own
    _write(mapping_path, merged(start, changes or {}))
    if schema is not None:
        schema_path = root / "schemas" / "input.schema.json"
        document = json.loads(schema_path.read_text(encoding="utf-8"))
        schema(document)
        schema_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return root


def order_sign(
    base: Path,
    changes: Mapping[str, Any] | None = None,
    *,
    agent_id: str = ORDERS_ID,
    manifest: Edit | None = None,
    folder: str = "chart-orders",
) -> Path:
    """The agent's `order-sign` twin in `base/<folder>`, under `agent_id`: each draft order whose
    code is one of `checked_orders` (`fixture_records.ORDERS`, ICD-10-PCS, wherever it sits among
    the order's codings, a site's translation included) is decided with the patient's chart, the
    order's code, text and priority beside it, and gets a card whatever it decides. The Patient
    is not required, so the chart may come without one."""

    def twin(data: dict[str, Any]) -> None:
        hooks = data["cds_hooks"]
        hooks["hook"] = "order-sign"
        hooks["title"] = "Synthetic Chart Summary at order signing"
        hooks["description"] = (
            "Synthetic test fixture. When an order is signed, says when the patient should next "
            "be seen and whether an anticoagulant is listed. Not clinically reviewed."
        )
        card = hooks["card"]
        card.pop("show", None)
        card["suggestion"] = "Create a follow-up task for the order"
        if manifest is not None:
            manifest(data)

    def open_patient(document: dict[str, Any]) -> None:
        document["required"] = []

    order_changes = {
        "focus": "ServiceRequest",
        "focus_where": {"path": "code.coding", "code_set": ORDER_CODE_SET},
        "resources": {"ServiceRequest": {"required": True}, "Patient": {"required": False}},
        "code_sets": {ORDER_CODE_SET: {"system": ICD10PCS, "codes": sorted(ORDERS)}},
        "fields": {
            "order.code": {"path": "ServiceRequest.code.coding", "code_set": ORDER_CODE_SET},
            "order.text": {"path": "ServiceRequest.code.text"},
            "order.priority": {"path": "ServiceRequest.priority"},
        },
    }
    root = chart_copy(
        base,
        merged(order_changes, changes or {}),
        systems=(ICD10PCS,),
        agent_id=agent_id,
        manifest=twin,
        schema=open_patient,
        folder=folder,
    )
    return root


def _searchset(resources: Iterable[Mapping[str, Any]], matched: tuple[str, ...]) -> dict[str, Any]:
    """A searchset Bundle of `resources`: those of the `matched` type as matches, any other (a
    Medication `_include` brought) as an include."""
    found = [dict(resource) for resource in resources]
    modes = ["match" if resource["resourceType"] in matched else "include" for resource in found]
    entries = [
        {
            "fullUrl": f"{BASE}/{resource['resourceType']}/{resource['id']}",
            "resource": resource,
            "search": {"mode": mode},
        }
        for resource, mode in zip(found, modes, strict=True)
    ]
    total = modes.count("match")
    return {"resourceType": "Bundle", "type": "searchset", "total": total, "entry": entries}


def prefetch(case: Case, today: date = TODAY, *, site: Site = None) -> dict[str, Any]:
    """The case's record as the patient-view hook's prefetch: `patient` the Patient, and each
    search key a searchset of the case's resources of its type, the Medications its requests
    name included in `medication_requests` (`_include=MedicationRequest:medication`)."""
    resources = case.resources(today, site=site)
    found: dict[str, Any] = {}
    for key, types in KEYS.items():
        chosen = [resource for resource in resources if resource["resourceType"] in types]
        if key == "patient":
            found[key] = chosen[0]
            continue
        found[key] = _searchset(chosen, types[:1])
    return found


def hook(
    case: Case,
    today: date = TODAY,
    *,
    site: Site = None,
    fhir_server: str | None = None,
    token: str | None = None,
    prefetched: bool = True,
) -> dict[str, Any]:
    """The patient-view hook's request for the case's chart: with its record as prefetch, or
    without (`prefetched=False`) for the engine to read from `fhir_server` with `token`."""
    request: dict[str, Any] = {
        "hook": "patient-view",
        "hookInstance": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{BASE}/hook/{case.id}")),
        "context": {"userId": PRACTITIONER, "patientId": case.id},
    }
    if prefetched:
        request["prefetch"] = prefetch(case, today, site=site)
    if fhir_server is not None:
        request["fhirServer"] = fhir_server
    if token is not None:
        request["fhirAuthorization"] = {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "patient/*.read",
            "subject": "cds-service",
        }
    return request


def draft(case: Case, today: date = TODAY, *, site: Site = None) -> dict[str, Any]:
    """The case's order as a draft order, as the EHR sends it while it is signed."""
    order = next(
        resource
        for resource in case.resources(today, site=site)
        if resource["resourceType"] == "ServiceRequest"
    )
    return {**order, "status": "draft"}


def order_hook(
    case: Case,
    *orders: Mapping[str, Any],
    today: date = TODAY,
    site: Site = None,
    fhir_server: str | None = None,
    token: str | None = None,
    prefetched: bool = True,
) -> dict[str, Any]:
    """The order-sign twin's request: the case's chart as prefetch, and as `draftOrders` the
    given orders, or the case's own (`draft`) when none is given."""
    request = hook(
        case, today, site=site, fhir_server=fhir_server, token=token, prefetched=prefetched
    )
    request["hook"] = "order-sign"
    signed = list(orders) or [draft(case, today, site=site)]
    request["context"]["draftOrders"] = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": dict(order)} for order in signed],
    }
    return request
