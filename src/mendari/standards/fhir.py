"""FHIR R4 adapter: map resources onto a pack's input, and a decision onto a FHIR Task.

Mappings are declarative (``fhir/mapping.yaml`` in the pack) so that clinical informaticists
can review them without reading code. Paths use a small FHIRPath subset: dotted element names,
where a list-valued element yields all of its items and ``[n]`` selects the n-th item.

`fields` map single values (the first match). `lists` map every resource of a type to an object,
for patient records with many Conditions, Observations or MedicationStatements. Input can be a
Bundle, a single resource, or a CDS Hooks prefetch object whose values may themselves be search
Bundles; all of them are flattened, and resources are de-duplicated by type and id.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterator, Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.core import clock
from mendari.core.pack import Pack
from mendari.core.spec import Outcome
from mendari.core.templates import fill, unknown_placeholders
from mendari.errors import FhirError, PackValidationError

if TYPE_CHECKING:
    from mendari.core.decision import DecisionResult

OUTCOME_SYSTEM = "https://mendari.co/fhir/CodeSystem/decision-outcome"
DECISION_ID_SYSTEM = "https://mendari.co/fhir/NamingSystem/decision-id"
PACK_TAG_SYSTEM = "https://mendari.co/fhir/CodeSystem/decision-pack"
_SEGMENT = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_]*)(?:\[(?P<index>\*|\d+)\])?$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceRule(_Model):
    required: bool = False


class FieldMapping(_Model):
    path: str
    transform: Literal["join", "age_band", "age_years", "base64_text"] | None = None
    map: dict[str, str] | None = None


class ListMapping(_Model):
    resource: str
    fields: dict[str, FieldMapping]  # paths relative to the resource


class TaskTemplate(_Model):
    code_text: str
    performer_system: str
    # Templates (core/templates.py): `{recommendation}`, `{priority}`, `{<question>}`.
    # The default description is the inbox's routing wording.
    description: str = "Route to {recommendation}"
    performer: str | None = None  # a performer code (template); default: the recommendation


class FocusFilter(_Model):
    """Which resources of the focus type count, e.g. referrals among all ServiceRequests."""

    path: str  # relative to the resource, e.g. category[0].coding[0].code
    values: list[str] = Field(alias="in", min_length=1)


class FhirMapping(_Model):
    fhir_version: Literal["R4"]
    focus: str
    focus_where: FocusFilter | None = None
    resources: dict[str, ResourceRule]
    fields: dict[str, FieldMapping] = {}
    lists: dict[str, ListMapping] = {}
    task: TaskTemplate


Indexed = dict[str, list[dict[str, Any]]]


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
        templates = {"description": mapping.task.description, "performer": mapping.task.performer}
        problems = [
            f"{relative}: task.{name} uses unknown placeholders {unknown}"
            for name, text in templates.items()
            if text and (unknown := unknown_placeholders(text, pack.manifest.questions))
        ]
        if problems:
            raise PackValidationError(pack.id, problems)
        return cls(pack, mapping)

    def index(
        self, resources: Mapping[str, Any], *, focus: Mapping[str, Any] | None = None
    ) -> Indexed:
        """Accept a Bundle, a single resource, or a mapping of resources (e.g. CDS prefetch).

        With `focus` (e.g. one draft order), the record is indexed around that resource and any
        other resource of the focus type in `resources` is ignored."""
        indexed: Indexed = {}
        seen: set[tuple[str, str]] = set()
        for resource in iter_resources(resources if focus is None else [resources, focus]):
            kind = resource["resourceType"]
            if kind == self.mapping.focus and not self.is_focus(resource):
                continue  # e.g. a lab order next to the referral
            if focus is not None and kind == self.mapping.focus and resource is not focus:
                continue
            if resource.get("id") is not None:
                key = (kind, str(resource["id"]))
                if key in seen:
                    continue
                seen.add(key)
            if kind == self.mapping.focus and kind in indexed:
                raise FhirError(f"expected exactly one {kind}, got several")
            indexed.setdefault(kind, []).append(dict(resource))
        missing = [
            k for k, rule in self.mapping.resources.items() if rule.required and k not in indexed
        ]
        if missing:
            raise FhirError(f"missing required FHIR resources: {', '.join(missing)}")
        return indexed

    def is_focus(self, resource: Mapping[str, Any]) -> bool:
        focus, rule = self.mapping.focus, self.mapping.focus_where
        if resource.get("resourceType") != focus:
            return False
        if rule is None:
            return True
        return any(str(v) in rule.values for v in select({focus: resource}, f"{focus}.{rule.path}"))

    def focus_resources(self, resources: Any) -> list[dict[str, Any]]:
        """The focus resources in a Bundle, resource or mapping (e.g. CDS `draftOrders`)."""
        found: dict[str, dict[str, Any]] = {}
        for resource in iter_resources(resources):
            if self.is_focus(resource):
                found.setdefault(str(resource.get("id", len(found))), dict(resource))
        return list(found.values())

    def to_input(self, indexed: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for target, field in self.mapping.fields.items():
            value = _apply(field, select(indexed, field.path))
            if value is not None:
                _put(result, target, value)
        for target, spec in self.mapping.lists.items():
            items = []
            for resource in _as_list(indexed.get(spec.resource)):
                item = {}
                for name, field in spec.fields.items():
                    values = select({spec.resource: resource}, f"{spec.resource}.{field.path}")
                    value = _apply(field, values)
                    if value is not None:
                        item[name] = value
                if item:
                    items.append(item)
            _put(result, target, items)
        return result

    def to_task(self, result: DecisionResult, indexed: Mapping[str, Any]) -> dict[str, Any]:
        focus: Mapping[str, Any] = next(iter(_as_list(indexed.get(self.mapping.focus))), {})
        patient: Mapping[str, Any] = next(iter(_as_list(indexed.get("Patient"))), {})
        subject = (focus.get("subject") or {}).get("reference") or (
            f"Patient/{patient['id']}" if patient.get("id") else None
        )
        handled_by = result.recommendation or "manual-triage"
        template = self.mapping.task
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
            "description": result.fallback
            or fill(template.description, result, recommendation=handled_by),
            "authoredOn": result.created_at,
            "performerType": [
                {
                    "coding": [
                        {
                            "system": template.performer_system,
                            "code": fill(template.performer, result, recommendation=handled_by)
                            if template.performer
                            else handled_by,
                        }
                    ]
                }
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


def select(indexed: Mapping[str, Any], path: str) -> list[Any]:
    """Values at `path`; `indexed` maps a resource type to one resource or a list of them."""
    resource_type, *segments = path.split(".")
    current: list[Any] = _as_list(indexed.get(resource_type))
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
    if field.transform == "base64_text":  # attachments (e.g. DocumentReference notes), joined
        texts = [t for t in (_decode(v) for v in values) if t]
        return "\n\n".join(texts) or None
    value = values[0] if values else None
    if value is None:
        return None
    if field.transform == "age_band":
        return age_band(str(value))
    if field.transform == "age_years":
        return age_years(str(value))

    if field.map is not None:
        return field.map.get(str(value))
    return value


def age_years(birth_date: str, today: date | None = None) -> int | None:
    """Age in whole years from a FHIR date (YYYY, YYYY-MM or YYYY-MM-DD)."""
    parts = [int(p) for p in birth_date.split("-")[:3] if p.isdigit()]
    if not parts:
        return None
    year, month, day = ([*parts, 7, 1])[:3] if len(parts) == 1 else ([*parts, 15])[:3]
    today = today or clock.today()
    try:
        born = date(year, month, day)
    except ValueError:
        return None
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return age if age >= 0 else None


def age_band(birth_date: str, today: date | None = None) -> str | None:
    """Coarse age band from a FHIR date (YYYY, YYYY-MM or YYYY-MM-DD)."""
    age = age_years(birth_date, today)
    if age is None:
        return None
    if age < 18:
        return "0-17"
    if age < 40:
        return "18-39"
    if age < 65:
        return "40-64"
    return "65+"


def iter_resources(value: Any) -> Iterator[Mapping[str, Any]]:
    """Every resource in a Bundle, a resource, or a mapping/list of either (flattened)."""
    if isinstance(value, list):
        for item in value:
            yield from iter_resources(item)
    elif isinstance(value, Mapping):
        if value.get("resourceType") == "Bundle":
            for entry in value.get("entry") or []:
                if isinstance(entry, Mapping):
                    yield from iter_resources(entry.get("resource"))
        elif "resourceType" in value:
            yield value
        else:
            for item in value.values():
                yield from iter_resources(item)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _put(result: dict[str, Any], target: str, value: Any) -> None:
    *parents, leaf = target.split(".")
    node = result
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value


def _decode(value: Any) -> str | None:
    try:
        return base64.b64decode(str(value), validate=True).decode("utf-8").strip() or None
    except (binascii.Error, UnicodeDecodeError):
        return None
