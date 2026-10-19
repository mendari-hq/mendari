# SPDX-License-Identifier: Apache-2.0
"""What a FHIR decision looked for in the record and what it found (`mendari.data-coverage/1`,
LP-23d).

Every decision made from FHIR carries this record (`DecisionResult.data_coverage`), so a site
can see how a pack read its EHR's data: for the focus, each mapped field and each mapped list,

- the paths looked for, the code systems the pack declares (`code_systems`) and the codes the
  pack itself looks for (its `focus_where` values and a field's `map` keys);
- the resources of the path's type, counted, and the number of values found;
- codings counted by declared system, every other system as one count (`other`);
- what could not be read, as reason codes (`READ_REASONS`) with their counts: a prefetch read
  that failed, counted against the resource type its template reads, and a note whose data does
  not decode (`undecodable`) or that is only a link (`not_fetched`).

It holds counts and the pack's own words only: never a value, an id, text, a patient's code or a
system the pack does not declare (a site's own system can name the site). The output is
canonical, keys and lists sorted, so one record gives the same bytes on every run.
"""

from __future__ import annotations

import base64
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from typing import Any, get_args

from mendari.errors import ReadReason
from mendari.standards.fhir import FhirMapping, FieldMapping, Indexed, list_item, select

FORMAT = "mendari.data-coverage/1"
READ_REASONS: frozenset[str] = frozenset(get_args(ReadReason))
OTHER = "other"  # every coding whose system the pack does not declare, as one count
_ATTACHMENT_DATA = "data"


def source_type(template: str) -> str:
    """The resource type a prefetch template reads: `Condition?patient=...` is Condition."""
    return template.partition("?")[0].partition("/")[0]


def decodes(value: Any) -> bool:
    """Whether an attachment's data is base64 of UTF-8 text (blank text included), as
    `fhir._decode` reads it."""
    try:
        base64.b64decode(str(value), validate=True).decode("utf-8")
    except ValueError:  # binascii.Error, UnicodeDecodeError, or non-ASCII text
        return False
    return True


def unreadable_notes(mapping: FhirMapping, indexed: Indexed) -> dict[str, dict[str, int]]:
    """Each `base64_text` field (a list's as `<list>.<field>`) whose attachments could not be
    read, with a count by reason: data present that does not decode (`undecodable`), or only a
    `url` and no data (`not_fetched`). Fields whose notes all read are left out."""
    found: dict[str, dict[str, int]] = {}
    for target, field in mapping.fields.items():
        if field.transform == "base64_text" and (counts := _attachments([indexed], field.path)):
            found[target] = counts
    for name, spec in mapping.lists.items():
        resources = [{spec.resource: r} for r in _as_list(indexed.get(spec.resource))]
        for key, field in spec.fields.items():
            path = f"{spec.resource}.{field.path}"
            if field.transform == "base64_text" and (counts := _attachments(resources, path)):
                found[f"{name}.{key}"] = counts
    return dict(sorted(found.items()))


def measure(
    mapping: FhirMapping,
    indexed: Indexed,
    *,
    unreadable: Mapping[str, str],
    systems: Collection[str] = (),
    sources: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The data-coverage record of one decision. `indexed` is the record as the mapping read it
    (`FhirAdapter.index`); `unreadable` the prefetch keys that could not be read, each with its
    reason; `systems` the code systems the pack declares; `sources` the pack's prefetch
    templates, which say what resource type each key reads, so a field or a list whose resources
    could not be read says so, and a list read and empty does not."""
    unknown = sorted(set(unreadable.values()) - READ_REASONS)
    if unknown:
        raise ValueError(f"not a read reason: {unknown}")
    declared = sorted(set(systems))
    failed: dict[str, Counter[str]] = {}  # resource type -> its failed reads, by reason
    for key, reason in unreadable.items():
        template = (sources or {}).get(key)
        if template is not None:
            failed.setdefault(source_type(template), Counter())[reason] += 1
    notes = unreadable_notes(mapping, indexed)
    total = Counter(unreadable.values())
    for counts in notes.values():
        total.update(counts)

    where = mapping.focus_where
    focus = {
        "resource": mapping.focus,
        "paths": [f"{mapping.focus}.{where.path}"] if where is not None else [],
        "codes": sorted(where.values) if where is not None else [],
        "resources": len(_as_list(indexed.get(mapping.focus))),
        "unreadable": dict(failed.get(mapping.focus, Counter())),
    }
    fields: dict[str, Any] = {}
    for target, field in mapping.fields.items():
        kind = field.path.split(".", 1)[0]
        record = _field(field, field.path, [indexed], declared)
        record["resources"] = {kind: len(_as_list(indexed.get(kind)))}
        record["unreadable"] = _merged(failed.get(kind), notes.get(target))
        fields[target] = record
    lists: dict[str, Any] = {}
    for name, spec in mapping.lists.items():
        resources = _as_list(indexed.get(spec.resource))
        scopes = [{spec.resource: resource} for resource in resources]
        listed: dict[str, Any] = {}
        own: Counter[str] = Counter()
        for key, field in spec.fields.items():
            listed[key] = _field(field, f"{spec.resource}.{field.path}", scopes, declared)
            listed[key]["paths"] = [field.path]  # relative to the resource, as the mapping says
            own.update(notes.get(f"{name}.{key}", {}))
        lists[name] = {
            "resource": spec.resource,
            "resources": len(resources),
            "items": sum(1 for resource in resources if list_item(spec, resource)),
            "fields": listed,
            "unreadable": _merged(failed.get(spec.resource), own),
        }
    return _canonical(
        {
            "format": FORMAT,
            "focus": focus,
            "fields": fields,
            "lists": lists,
            "unreadable": dict(total),
        }
    )


def _field(
    field: FieldMapping, path: str, scopes: Sequence[Mapping[str, Any]], declared: list[str]
) -> dict[str, Any]:
    """What one field looked for and found, across `scopes` (the record, or each resource of a
    list): its path, the pack's codes and declared systems, the values found and, for a coded
    path, its concept's codings by declared system."""
    values = [value for scope in scopes for value in select(scope, path) if value is not None]
    concept = _codings_path(path)
    record: dict[str, Any] = {
        "paths": [path],
        "codes": sorted(field.map) if field.map is not None else [],
        "systems": declared if concept is not None else [],
        "values": len(values),
        "codings": {},
    }
    if concept is not None:
        counts: Counter[str] = Counter()
        for scope in scopes:
            for coding in select(scope, concept):
                system = coding.get("system") if isinstance(coding, Mapping) else None
                counts[system if isinstance(system, str) and system in declared else OTHER] += 1
        record["codings"] = {OTHER: 0, **counts}
    return record


def _codings_path(path: str) -> str | None:
    """The path of every coding of the concept a coded path reads (`code.coding[0].code` reads
    one of `code.coding`), or None for a path that reads no coding."""
    segments = path.split(".")
    for at in range(len(segments) - 1, 0, -1):
        if segments[at].split("[", 1)[0] == "coding":
            return ".".join([*segments[:at], "coding"])
    return None


def _attachments(scopes: Sequence[Mapping[str, Any]], path: str) -> dict[str, int]:
    """A `base64_text` path's attachments that could not be read, by reason."""
    counts: Counter[str] = Counter()
    head, _, last = path.rpartition(".")
    for scope in scopes:
        for value in select(scope, path):
            if value is not None and not decodes(value):
                counts["undecodable"] += 1
        if last != _ATTACHMENT_DATA or not head:
            continue
        for attachment in select(scope, head):
            if (
                isinstance(attachment, Mapping)
                and attachment.get(_ATTACHMENT_DATA) is None
                and attachment.get("url") is not None
            ):
                counts["not_fetched"] += 1
    return dict(sorted(counts.items()))


def _merged(*parts: Mapping[str, int] | None) -> dict[str, int]:
    total: Counter[str] = Counter()
    for part in parts:
        total.update(part or {})
    return dict(total)


def _canonical(value: Any) -> Any:
    """Every mapping with its keys sorted, so equal records are equal bytes."""
    if isinstance(value, Mapping):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]
