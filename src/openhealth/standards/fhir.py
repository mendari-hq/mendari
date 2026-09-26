"""FHIR R4 adapter: map resources onto a pack's input, and a decision onto a FHIR Task.

Mappings are declarative (``fhir/mapping.yaml`` in the pack) so that clinical informaticists
can review them without reading code. Paths use a small FHIRPath subset: dotted element names,
where a list-valued element yields all of its items and ``[n]`` selects the n-th item.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from openhealth.core.pack import Pack
from openhealth.core.spec import Outcome
from openhealth.errors import FhirError, PackValidationError

if TYPE_CHECKING:
    from openhealth.core.decision import DecisionResult

OUTCOME_SYSTEM = "https://openhealth.dev/fhir/CodeSystem/decision-outcome"
DECISION_ID_SYSTEM = "https://openhealth.dev/fhir/NamingSystem/decision-id"
PACK_TAG_SYSTEM = "https://openhealth.dev/fhir/CodeSystem/decision-pack"
_SEGMENT = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_]*)(?:\[(?P<index>\*|\d+)\])?$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceRule(_Model):
    required: bool = False


class FieldMapping(_Model):
    path: str
    transform: Literal["join", "age_band"] | None = None
    map: dict[str, str] | None = None


class TaskTemplate(_Model):
    code_text: str
    performer_system: str


class FhirMapping(_Model):
    fhir_version: Literal["R4"]
    focus: str
    resources: dict[str, ResourceRule]
    fields: dict[str, FieldMapping]
    task: TaskTemplate


class FhirAdapter:
    def __init__(self, pack: Pack, mapping: FhirMapping):
        self.pack = pack
        self.mapping = mapping

    @classmethod
    def for_pack(cls, pack: Pack) -> FhirAdapter:
        relative = pack.manifest.inputs.fhir
        if relative is None:
            raise FhirError(f"pack {pack.id} does not declare a FHIR mapping")
        try:
            mapping = FhirMapping.model_validate(pack.read_yaml(relative))
        except ValidationError as error:
            raise PackValidationError(pack.id, [f"{relative}: {error}"]) from None
        return cls(pack, mapping)

    def index(self, resources: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Accept a Bundle, a single resource, or a mapping of resources (e.g. CDS prefetch)."""
        if resources.get("resourceType") == "Bundle":
            found = [entry.get("resource") for entry in resources.get("entry", [])]
        elif "resourceType" in resources:
            found = [resources]
        else:
            found = list(resources.values())
        indexed: dict[str, dict[str, Any]] = {}
        for resource in found:
            if not isinstance(resource, Mapping) or "resourceType" not in resource:
                continue
            kind = resource["resourceType"]
            if kind in indexed and kind == self.mapping.focus:
                raise FhirError(f"expected exactly one {kind}, got several")
            indexed.setdefault(kind, dict(resource))
        missing = [
            k for k, rule in self.mapping.resources.items() if rule.required and k not in indexed
        ]
        if missing:
            raise FhirError(f"missing required FHIR resources: {', '.join(missing)}")
        return indexed

    def to_input(self, indexed: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for target, field in self.mapping.fields.items():
            values = select(indexed, field.path)
            value = _apply(field, values)
            if value is None:
                continue
            *parents, leaf = target.split(".")
            node = result
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = value
        return result

    def to_task(
        self, result: DecisionResult, indexed: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, Any]:
        focus = indexed.get(self.mapping.focus, {})
        patient = indexed.get("Patient", {})
        subject = (focus.get("subject") or {}).get("reference") or (
            f"Patient/{patient['id']}" if patient.get("id") else None
        )
        handled_by = result.recommendation or "manual-triage"
        task: dict[str, Any] = {
            "resourceType": "Task",
            "meta": {
                "tag": [
                    {"system": PACK_TAG_SYSTEM, "code": f"{result.pack_id}@{result.pack_version}"}
                ]
            },
            "identifier": [{"system": DECISION_ID_SYSTEM, "value": result.id}],
            "status": "ready" if result.outcome is Outcome.AUTO else "requested",
            "businessStatus": {
                "coding": [{"system": OUTCOME_SYSTEM, "code": result.outcome.value}],
                "text": result.outcome.value.replace("_", " ").lower(),
            },
            "intent": "order",
            "priority": result.priority.value,
            "code": {"text": self.mapping.task.code_text},
            "description": result.fallback or f"Route to {handled_by}",
            "authoredOn": result.created_at,
            "performerType": [
                {"coding": [{"system": self.mapping.task.performer_system, "code": handled_by}]}
            ],
            "note": [{"text": reason} for reason in result.reasons],
        }
        if focus.get("id"):
            task["focus"] = {"reference": f"{self.mapping.focus}/{focus['id']}"}
        if subject:
            task["for"] = {"reference": subject}
        if result.confidence is not None:
            task["input"] = [
                {"type": {"text": "confidence"}, "valueDecimal": round(result.confidence, 4)}
            ]
        return task


def select(indexed: Mapping[str, Mapping[str, Any]], path: str) -> list[Any]:
    resource_type, *segments = path.split(".")
    root = indexed.get(resource_type)
    current: list[Any] = [root] if root is not None else []
    for segment in segments:
        match = _SEGMENT.match(segment)
        if match is None:
            raise FhirError(f"unsupported path segment {segment!r} in {path!r}")
        collected: list[Any] = []
        for node in current:
            value = node.get(match["name"]) if isinstance(node, Mapping) else None
            if isinstance(value, list):
                collected.extend(value)
            elif value is not None:
                collected.append(value)
        index = match["index"]
        if index is not None and index != "*":
            collected = collected[int(index) : int(index) + 1]
        current = collected
    return current


def _apply(field: FieldMapping, values: list[Any]) -> Any:
    if field.transform == "join":
        text = "\n".join(v for v in values if isinstance(v, str) and v.strip())
        return text or None
    value = values[0] if values else None
    if value is None:
        return None
    if field.transform == "age_band":
        return age_band(str(value))
    if field.map is not None:
        return field.map.get(str(value))
    return value


def age_band(birth_date: str, today: date | None = None) -> str | None:
    """Coarse age band from a FHIR date (YYYY, YYYY-MM or YYYY-MM-DD)."""
    parts = [int(p) for p in birth_date.split("-")[:3] if p.isdigit()]
    if not parts:
        return None
    year, month, day = ([*parts, 7, 1])[:3] if len(parts) == 1 else ([*parts, 15])[:3]
    today = today or datetime.now(UTC).date()
    try:
        born = date(year, month, day)
    except ValueError:
        return None
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if age < 0:
        return None
    if age < 18:
        return "0-17"
    if age < 40:
        return "18-39"
    if age < 65:
        return "40-64"
    return "65+"
