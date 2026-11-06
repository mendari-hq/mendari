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

A spec 0.3 mapping (LP-23g) is counted against what the adapter says each field looks for
(`fhir.looked_for`, `FhirAdapter.looked_for`): a field's `paths` are the ones it tries, in order,
and it counts what the paths it read found (each in order until one gave the field its value,
as the mapping reads them). Its codes are its code set's too, and its systems the ones it
selects on (its `[system=…]` predicates', its code set's), declared ones all; a coded path that
selects on none has the declared systems, as a 0.2 path does. A field that follows a reference
says how many references it found and how many resources they reached inside the record
(`follow`), and counts the resources of each type it reads. A list of several resource types
(`resources`) holds a record for each type under `sources`, shaped as a one-type list's, with a
`medication_period`'s two keys among its fields. An `attachment_text` note that could not be
read counts by `fhir.read_attachment`'s reason (`unsupported_type`, `undecodable`,
`not_fetched`); an attachment with neither data nor a link has nothing to read and counts for
nothing, as for `base64_text`. A 0.1 or 0.2 mapping's record is as it was.

It holds counts and the pack's own words only: never a value, an id, text, a patient's code or a
system the pack does not declare (a site's own system can name the site). The output is
canonical, keys and lists sorted, so one record gives the same bytes on every run.
"""

from __future__ import annotations

import base64
from collections import Counter
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, get_args

from mendari.errors import ReadReason
from mendari.standards.fhir import (
    MEDICATION_END,
    MEDICATION_START,
    FhirMapping,
    FieldMapping,
    Indexed,
    ListSource,
    LookedFor,
    Reading,
    codings_of,
    list_item,
    looked_for,
    read_attachment,
    read_field,
    select,
    split_path,
)

FORMAT = "mendari.data-coverage/1"
READ_REASONS: frozenset[str] = frozenset(get_args(ReadReason))
OTHER = "other"  # every coding whose system the pack does not declare, as one count
_ATTACHMENT_DATA = "data"
_NOTES = ("base64_text", "attachment_text")  # the transforms that read attachments


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


@dataclass
class _Found:
    """What one field found, across its scopes (the record, or each resource of a list)."""

    values: int = 0
    codings: Counter[str] = field(default_factory=Counter)
    notes: Counter[str] = field(default_factory=Counter)  # unreadable attachments, by reason
    references: int = 0
    reached: int = 0


def unreadable_notes(mapping: FhirMapping, indexed: Indexed) -> dict[str, dict[str, int]]:
    """Each `base64_text` or (spec 0.3) `attachment_text` field (a list's as `<list>.<field>`)
    whose attachments could not be read, with a count by reason. For `base64_text`: data present
    that does not decode (`undecodable`), or only a `url` and no data (`not_fetched`); for
    `attachment_text`, `fhir.read_attachment`'s reason. Fields whose notes all read are left
    out."""
    reading = Reading.of(mapping, indexed)
    found: dict[str, Counter[str]] = {}
    for target, mapped in mapping.fields.items():
        if mapped.transform in _NOTES:
            found[target] = _read(mapped, None, [indexed], reading, None).notes
    for name, spec in mapping.lists.items():
        for kind, source in spec.sources().items():
            scopes = [{kind: resource} for resource in _as_list(indexed.get(kind))]
            for key, mapped in source.fields.items():
                if mapped.transform in _NOTES:
                    counts = _read(mapped, kind, scopes, reading, None).notes
                    found.setdefault(f"{name}.{key}", Counter()).update(counts)
    return {key: dict(sorted(counts.items())) for key, counts in sorted(found.items()) if counts}


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
    reading = Reading.of(mapping, indexed)
    looked = looked_for(mapping)
    total = Counter(unreadable.values())

    where = mapping.focus_where
    focus = {
        "resource": mapping.focus,
        "paths": [f"{mapping.focus}.{where.path}"] if where is not None else [],
        "codes": sorted(where.values) if where is not None else [],
        "resources": len(_as_list(indexed.get(mapping.focus))),
        "unreadable": dict(failed.get(mapping.focus, Counter())),
    }
    fields: dict[str, Any] = {}
    for target, mapped in mapping.fields.items():
        found = _read(mapped, None, [indexed], reading, declared)
        read_in = _types(mapped, None)
        record = _field(mapped, None, looked["fields"][target], found, declared)
        record["resources"] = {kind: len(_as_list(indexed.get(kind))) for kind in read_in}
        record["unreadable"] = _merged(*(failed.get(kind) for kind in read_in), found.notes)
        total.update(found.notes)
        fields[target] = record
    lists: dict[str, Any] = {}
    for name, spec in mapping.lists.items():
        records: dict[str, Any] = {}
        kinds: set[str] = set()
        notes: Counter[str] = Counter()
        for kind, source in spec.sources().items():
            looked_in = looked["lists"][name][kind]
            records[kind], own, counts = _source(
                kind, source, looked_in, indexed, reading, declared, failed
            )
            kinds |= own
            notes.update(counts)
        total.update(notes)
        if spec.resources is None:  # one type: its record, as a 0.2 list has it
            lists[name] = next(iter(records.values()))
        else:
            lists[name] = {
                "items": sum(record["items"] for record in records.values()),
                "sources": records,
                "unreadable": _merged(*(failed.get(kind) for kind in sorted(kinds)), notes),
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


def _source(
    kind: str,
    source: ListSource,
    looked: Mapping[str, LookedFor],
    indexed: Indexed,
    reading: Reading,
    declared: list[str],
    failed: Mapping[str, Counter[str]],
) -> tuple[dict[str, Any], set[str], Counter[str]]:
    """One resource type of a list: its record (the resources of the type, its items, each item
    key's field record, what could not be read), the resource types it reads (its own, and the
    types its follows reach) and its unreadable notes by reason."""
    resources = _as_list(indexed.get(kind))
    scopes = [{kind: resource} for resource in resources]
    fields: dict[str, Any] = {}
    kinds = {kind}
    notes: Counter[str] = Counter()
    for key, mapped in _item_keys(source):
        found = _read(mapped, kind, scopes, reading, declared)
        fields[key] = _field(mapped, kind, looked[key], found, declared)
        kinds.update(_types(mapped, kind))
        notes.update(found.notes)
    record = {
        "resource": kind,
        "resources": len(resources),
        "items": sum(1 for resource in resources if list_item(kind, source, resource, reading)),
        "fields": fields,
        "unreadable": _merged(*(failed.get(other) for other in sorted(kinds)), notes),
    }
    return record, kinds, notes


def _item_keys(source: ListSource) -> Iterator[tuple[str, FieldMapping]]:
    """Each key a list source writes, with the mapping it is read by: its fields, then a
    `medication_period`'s two keys as the paths its derivation reads (`fhir.medication_period`)."""
    yield from source.fields.items()
    period = source.medication_period
    if period is not None:
        yield period.start, FieldMapping(paths=list(MEDICATION_START))
        yield period.end, FieldMapping(paths=list(MEDICATION_END))


def _read(
    mapped: FieldMapping,
    kind: str | None,
    scopes: Sequence[Mapping[str, Any]],
    reading: Reading,
    declared: Sequence[str] | None,
) -> _Found:
    """What one field found across `scopes`, read as the mapping reads it (`fhir.read_field`):
    the values at the paths it read, the codings of the concepts they read (each coding once, by
    declared system or `other`), its unreadable attachments by reason and, with `follow`, the
    references it found and what they reached. `declared` None counts no codings. A null is not
    a value: R4 JSON pads a primitive array with nulls to line it up with its `_x` sibling, so a
    null counts for nothing and is never an undecodable note, as LP-23d counted."""
    found = _Found()
    for scope in scopes:
        read = read_field(mapped, scope, kind, reading)
        found.references += read.references
        found.reached += read.reached
        counted: set[int] = set()  # the codings an earlier path of the field counted
        for path, where, read_pairs in read.reads:
            pairs = [(container, value) for container, value in read_pairs if value is not None]
            values = [value for _, value in pairs]
            found.values += len(values)
            if declared is not None:
                codings = _codings(mapped, path, where, values)
                for coding in codings:
                    if isinstance(coding, Mapping) and id(coding) in counted:
                        continue
                    system = coding.get("system") if isinstance(coding, Mapping) else None
                    counted_as = system if isinstance(system, str) and system in declared else OTHER
                    found.codings[counted_as] += 1
                counted.update(id(coding) for coding in codings if isinstance(coding, Mapping))
            found.notes.update(_notes(mapped, path, where, pairs, reading))
    return found


def _field(
    mapped: FieldMapping, kind: str | None, looked: LookedFor, found: _Found, declared: list[str]
) -> dict[str, Any]:
    """What one field looked for and found: its paths as the mapping says them, in the order
    tried (a list field's relative to its resource), the pack's codes and the systems it looks
    in, the values found and, for a field that reads codings, its concepts' codings by declared
    system; with `follow`, its references and what they reached."""
    coded = _coded(mapped, kind)
    chosen = [system for system in looked["systems"] if system in declared]
    record: dict[str, Any] = {
        "paths": list(mapped.tried),
        "codes": list(looked["codes"]),
        "systems": chosen if looked["systems"] else (declared if coded else []),
        "values": found.values,
        "codings": {OTHER: 0, **found.codings} if coded else {},
    }
    if mapped.follow is not None:
        record["follow"] = {
            "path": mapped.follow,
            "references": found.references,
            "reached": found.reached,
        }
    return record


def _coded(mapped: FieldMapping, kind: str | None) -> bool:
    """Whether a field reads codings: a `code_set` selector, or a path into a concept's (a list
    field's read from its resource type, unless it follows a reference)."""
    if mapped.code_set is not None:
        return True
    start = f"{kind}." if kind is not None and mapped.follow is None else ""
    return any(_codings_path(f"{start}{path}") is not None for path in mapped.tried)


def _codings(
    mapped: FieldMapping, path: str, where: Mapping[str, Any], values: list[Any]
) -> list[Any]:
    """The codings a path read: for a `code_set` selector the ones it chooses among
    (`fhir.codings_of`), else every coding of the concept the path reads."""
    if mapped.code_set is not None:
        return codings_of(values)
    concept = _codings_path(path)
    return [] if concept is None else select(where, concept)


def _types(mapped: FieldMapping, kind: str | None) -> list[str]:
    """The resource types a field reads, in order, each once: a top-level field's paths' (and,
    with `follow`, the type it follows from), a list field's own and, with `follow`, the types
    its paths read in what it reached."""
    found = [kind] if kind is not None else []
    if mapped.follow is not None and kind is None:
        found.append(split_path(mapped.follow)[0])
    if kind is None or mapped.follow is not None:
        found += [split_path(path)[0] for path in mapped.tried]
    return list(dict.fromkeys(found))


def _codings_path(path: str) -> str | None:
    """The path of every coding of the concept a coded path reads (`code.coding[0].code` reads
    one of `code.coding`), or None for a path that reads no coding."""
    segments = split_path(path)
    for at in range(len(segments) - 1, 0, -1):
        if segments[at].split("[", 1)[0] == "coding":
            return ".".join([*segments[:at], "coding"])
    return None


def _notes(
    mapped: FieldMapping,
    path: str,
    where: Mapping[str, Any],
    pairs: list[tuple[Any, Any]],
    reading: Reading,
) -> Counter[str]:
    """The attachments a note field's path read that could not be read, by reason. `pairs` are
    the path's values with their resources, nulls dropped (`_read`)."""
    counts: Counter[str] = Counter()
    if mapped.transform == "attachment_text":
        for container, attachment in pairs:
            if (
                isinstance(attachment, Mapping)
                and attachment.get(_ATTACHMENT_DATA) is None
                and attachment.get("url") is None
            ):
                continue  # nothing there to read
            reason = read_attachment(attachment, container=container, record=reading.record)[1]
            if reason is not None:
                counts[reason] += 1
    elif mapped.transform == "base64_text":
        for _, value in pairs:
            if not decodes(value):
                counts["undecodable"] += 1
        *head, last = split_path(path)
        if last == _ATTACHMENT_DATA and head:
            for attachment in select(where, ".".join(head)):
                if (
                    isinstance(attachment, Mapping)
                    and attachment.get(_ATTACHMENT_DATA) is None
                    and attachment.get("url") is not None
                ):
                    counts["not_fetched"] += 1
    return counts


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
