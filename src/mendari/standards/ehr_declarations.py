# SPDX-License-Identifier: Apache-2.0
"""What a site's EHR does not offer, declared per FHIR server (`MENDARI_EHR_DECLARATIONS`).

A pack may read one list from several resource types (spec 0.3 list `resources`: the trial
pack's medications from MedicationStatement and MedicationRequest). Some EHRs do not offer one
of them at all: Epic has no R4 MedicationStatement API, so its read is refused, and today any
prefetch key that cannot be read makes the decision ABSTAIN. A site may declare what its EHR
does not offer, one FHIR base at a time:

    {"<FHIR base>": {"org": "<organization>", "version": "<label>",
                     "attested_by": "<who checked it>", "reference": "<where it is recorded>",
                     "not_offered": ["MedicationStatement"]}}

- **Off unless a deployment sets it.** Then every source is read, and one that cannot be read
  abstains, as before. Set, it is inline JSON (a value that starts with `{`) or the path of a
  JSON file, read and checked at startup (`ehr_declarations`): its shape (`parse`), each base
  (normalized, on `MENDARI_FHIR_ALLOWED_SERVERS`, registered in `MENDARI_SMART_EHRS` or in a CDS
  client's `fhir_servers`, and for the organization those registrations name: `default` with
  sign-in off), and each declared type, which must be a resource type of R4 that a pack found
  here reads as one of several sources of a list (`excusable`). Anything wrong stops the server,
  and the refusal names the setting, the base and the field. A JSON object that gives one key
  twice is refused.
- **A claim, attested.** Declaring a type says more than that the EHR lacks it: that what the
  pack should see is in the list's other sources (for a trial-eligibility agent, every
  medication in the EHR's MedicationRequests). So each entry names who attested it
  (`attested_by`) and where that is recorded (`reference`), as a governance override does.
- **Only one of several sources.** For a pack, a declared type is left out of what is read only
  when it is one source of a multi-source list, another source of that list is not declared and
  is read by a prefetch key that ran, and the pack reads the type nowhere else: not as its focus,
  a `resources` rule, a top-level field, a single-source list or what a `follow` reaches
  (`excusable`, `EhrDeclaration.skipped`). A declaration can never leave a list or a field with
  no source at all. For any other pack, or any other base or organization, it does nothing.
- **Recorded, never automated.** A decision that left a declared source out records the
  declaration (`ehr_declaration: {version, sha256, attested_by, reference}`), marks the source
  and the list `not_offered` in its data-coverage record, so `resources: 0` is never read as
  "read and empty", and never automates: AUTO, and a rule's BLOCK, become HUMAN_REVIEW, with the
  limitation as a reason whatever the outcome (`core/policy.py`). Configuration never makes AUTO
  what the pack alone could not.
- **Versioned and hashed.** An entry's canonical form (`mendari.ehr-declarations/1`) is the
  format, the base, the organization, the version, who attested it, its reference and the sorted
  types, as sorted-key JSON with ASCII escapes and no spaces; its SHA-256 is taken over those
  bytes, so the same entry written in another order has the same digest and a changed one
  another.

Only the server reads the setting, for a SMART launch's decide and a CDS hook (by the request's
`fhirServer`); `fhir/decide`, `$decide`, the command line and `mendari bulk` read every source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from mendari.core.tenancy import DEFAULT_ORG
from mendari.errors import MendariError
from mendari.standards.data_coverage import source_type

if TYPE_CHECKING:
    from mendari.core.pack import Pack
    from mendari.server.cds_clients import CdsRegistry
    from mendari.standards.fhir import FhirMapping
    from mendari.standards.smart import Registration

ENV = "MENDARI_EHR_DECLARATIONS"
FORMAT = "mendari.ehr-declarations/1"
MAX_BASES = 50
MAX_FILE_BYTES = 1024 * 1024
MAX_ATTESTED = 100  # characters: who attested it
MAX_REFERENCE = 200  # characters: where it is recorded
ENTRY_KEYS = ("org", "version", "attested_by", "reference", "not_offered")
_ORG = re.compile(r"[\x21-\x7e]{1,100}")  # visible ASCII, as a CDS client's `org`
_VERSION = re.compile(r"[\x21-\x7e]{1,64}")


@dataclass(frozen=True)
class EhrDeclaration:
    """What one FHIR server does not offer, checked: its base (normalized), the organization
    it is declared for, its version, who attested it and where, the resource types it does not
    offer, and the SHA-256 of its canonical form."""

    base: str
    org: str
    version: str
    attested_by: str
    reference: str
    not_offered: frozenset[str]
    sha256: str

    def stamp(self) -> dict[str, str]:
        """What a decision that left a declared source out records of the declaration."""
        return {
            "version": self.version,
            "sha256": self.sha256,
            "attested_by": self.attested_by,
            "reference": self.reference,
        }

    def shown(self) -> dict[str, Any]:
        """What `/api/health` shows of it."""
        return {**self.stamp(), "not_offered": sorted(self.not_offered)}

    def skipped(
        self, mapping: FhirMapping, templates: Mapping[str, str], queried: Collection[str]
    ) -> dict[str, str]:
        """The prefetch keys of `templates` (a pack's, read with `mapping`) to leave out, each
        with the resource type it reads: a key whose type this server does not offer and the pack
        may read its list without (`excusable`), when each list it is a source of has another
        source, not declared, that a key of `queried` reads (a key whose template was filled for
        this launch, or every key of a hook, sent or fetched). Empty for any other pack."""
        kinds = excusable(mapping, templates, self.not_offered)
        if not kinds:
            return {}
        read = {source_type(templates[key]) for key in queried if key in templates}
        found: dict[str, str] = {}
        for key, template in templates.items():
            kind = source_type(template)
            if kind not in kinds:
                continue
            lists = [
                spec.resources
                for spec in mapping.lists.values()
                if spec.resources is not None and kind in spec.resources
            ]
            if all(
                any(other in read and other not in self.not_offered for other in sources)
                for sources in lists
            ):
                found[key] = kind
        return found


@dataclass(frozen=True)
class EhrDeclarations:
    """Every declaration (`MENDARI_EHR_DECLARATIONS`, checked), by normalized base."""

    entries: Mapping[str, EhrDeclaration]

    def for_base(self, url: object, org: str) -> EhrDeclaration | None:
        """The declaration for the FHIR server `url` (a launch's `iss`, a hook's `fhirServer`),
        normalized, when it is declared for `org`; None otherwise: another organization's is
        never given."""
        from mendari.standards.smart import normalize_base

        if not isinstance(url, str):
            return None
        try:
            base = normalize_base(url)
        except ValueError:  # a URL that does not even split
            return None
        found = None if base is None else self.entries.get(base)
        return found if found is not None and found.org == org else None

    def for_org(self, org: str | None) -> dict[str, dict[str, Any]]:
        """What `/api/health` shows: the declarations of `org` (every one for None), by base."""
        return {
            base: entry.shown()
            for base, entry in sorted(self.entries.items())
            if org is None or entry.org == org
        }


def canonical(entry: EhrDeclaration) -> bytes:
    """An entry's canonical form (`mendari.ehr-declarations/1`), whose SHA-256 is its digest."""
    return _canonical(
        entry.base,
        entry.org,
        entry.version,
        entry.attested_by,
        entry.reference,
        entry.not_offered,
    )


def _canonical(
    base: str,
    org: str,
    version: str,
    attested_by: str,
    reference: str,
    not_offered: Iterable[str],
) -> bytes:
    body = {
        "format": FORMAT,
        "base": base,
        "org": org,
        "version": version,
        "attested_by": attested_by,
        "reference": reference,
        "not_offered": sorted(not_offered),
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("ascii")


def excusable(
    mapping: FhirMapping, templates: Mapping[str, str], declared: Collection[str]
) -> frozenset[str]:
    """The types of `declared` a pack (its `mapping` and prefetch `templates`) may read its
    lists without: each one that is a source of one or more multi-source lists, each of which
    has another source that is not declared and that a prefetch key reads, and that the pack
    reads nowhere else (its focus, a `resources` rule, a top-level field's paths or `follow`, a
    single-source list, or a type a `follow` reaches). Defined against the whole declared set,
    so declaring every source of a list excuses none of them."""
    from mendari.standards.fhir import followed_types, located_fields, split_path

    prefetched = {source_type(template) for template in templates.values()}
    elsewhere = {mapping.focus, *mapping.resources}
    for _, kind, mapped in located_fields(mapping):
        if kind is None:
            elsewhere.update(split_path(path)[0] for path in mapped.tried)
            if mapped.follow is not None:
                elsewhere.add(split_path(mapped.follow)[0])
        if mapped.follow is not None:
            elsewhere.update(followed_types(mapped, kind))
    for spec in mapping.lists.values():
        if spec.resources is None and spec.resource is not None:
            elsewhere.add(spec.resource)
    found = set()
    for kind in set(declared) - elsewhere:
        lists = [
            spec.resources
            for spec in mapping.lists.values()
            if spec.resources is not None and kind in spec.resources
        ]
        if lists and all(
            any(other in prefetched and other not in declared for other in sources)
            for sources in lists
        ):
            found.add(kind)
    return frozenset(found)


def pack_excusable(pack: Pack, declared: Collection[str]) -> frozenset[str]:
    """`excusable` for a pack, from its mapping and its CDS prefetch templates; none for a pack
    without a FHIR mapping, or whose mapping cannot be read (it is refused where it is read)."""
    from mendari.standards.fhir import read_mapping

    if pack.manifest.inputs.fhir is None:
        return frozenset()
    try:
        mapping = read_mapping(pack)
    except MendariError:
        return frozenset()
    hooks = pack.manifest.cds_hooks
    return excusable(mapping, hooks.prefetch if hooks else {}, declared)


def ehr_declarations(
    packs: Iterable[Pack],
    *,
    signed_in: bool,
    ehrs: Mapping[str, Registration],
    cds: CdsRegistry | None,
    allowed: Collection[str] | None = None,
) -> EhrDeclarations | None:
    """`MENDARI_EHR_DECLARATIONS`, read and checked: None when it is unset, and then every
    source is read. Inline JSON, or the path of a JSON file. Anything wrong in it (`parse`), a
    base not on the allow-list or not registered for its organization, or a type no pack found
    here may read its list without (`check`), stops the server here, at startup. `allowed` is
    the FHIR allow-list, read from the environment when not given."""
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return None
    declarations = parse(raw if raw.startswith("{") else _read_file(raw), signed_in=signed_in)
    check(declarations, packs, signed_in=signed_in, ehrs=ehrs, cds=cds, allowed=allowed)
    return declarations


def parse(raw: str, *, signed_in: bool) -> EhrDeclarations:
    """The declarations `raw` (the setting's JSON) holds, checked for their shape: an object of 1
    to `MAX_BASES` FHIR bases, each unique once normalized (`smart.normalize_base`), each an
    object of exactly `org` (visible ASCII; `default` with sign-in off), `version` (1 to 64
    visible ASCII characters), `attested_by` and `reference` (printable text without spaces at
    either end) and `not_offered`, a list of one or more distinct R4 resource type names."""
    from mendari.standards import fhir_structure
    from mendari.standards.smart import normalize_base

    try:
        given = json.loads(raw, object_pairs_hook=_once)
    except _KeyTwice:
        raise MendariError(f"{ENV} gives a key twice in one object") from None
    except (ValueError, RecursionError):
        raise MendariError(f"{ENV} is not JSON: an object of FHIR bases") from None
    if not isinstance(given, dict) or not 1 <= len(given) <= MAX_BASES:
        raise MendariError(
            f"{ENV} must be a JSON object of 1 to {MAX_BASES} FHIR bases, each with what it does "
            "not offer"
        )
    types = fhir_structure.rules().resource_types
    entries: dict[str, EhrDeclaration] = {}
    for number, (url, entry) in enumerate(given.items(), start=1):
        try:
            base = normalize_base(url)
        except ValueError:
            base = None
        if base is None:
            raise MendariError(f"{ENV} base {number} is not a FHIR base (http or https, no query)")
        where = f"{ENV} base {base}"
        if base in entries:
            raise MendariError(f"{where} is given twice")
        if not isinstance(entry, dict) or sorted(entry) != sorted(ENTRY_KEYS):
            raise MendariError(f"{where} must be an object of {', '.join(ENTRY_KEYS)}")
        org = entry["org"]
        if not isinstance(org, str) or not _ORG.fullmatch(org):
            raise MendariError(f"{where}: org must be 1 to 100 visible ASCII characters")
        if not signed_in and org != DEFAULT_ORG:
            raise MendariError(f"{where}: with sign-in off, the one organization is {DEFAULT_ORG}")
        version = entry["version"]
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise MendariError(f"{where}: version must be 1 to 64 visible ASCII characters")
        attested_by = _text(entry, "attested_by", MAX_ATTESTED, where)
        reference = _text(entry, "reference", MAX_REFERENCE, where)
        listed = entry["not_offered"]
        if not isinstance(listed, list) or not listed:
            raise MendariError(f"{where}: not_offered must be a list of one or more resource types")
        for position, kind in enumerate(listed, start=1):
            if not isinstance(kind, str) or kind not in types:
                raise MendariError(f"{where}: not_offered {position} is not an R4 resource type")
        if len(set(listed)) != len(listed):
            raise MendariError(f"{where}: not_offered names a type twice")
        kinds = frozenset(listed)
        digest = hashlib.sha256(
            _canonical(base, org, version, attested_by, reference, kinds)
        ).hexdigest()
        entries[base] = EhrDeclaration(
            base=base,
            org=org,
            version=version,
            attested_by=attested_by,
            reference=reference,
            not_offered=kinds,
            sha256=digest,
        )
    return EhrDeclarations(MappingProxyType(entries))


def check(
    declarations: EhrDeclarations,
    packs: Iterable[Pack],
    *,
    signed_in: bool,
    ehrs: Mapping[str, Registration],
    cds: CdsRegistry | None,
    allowed: Collection[str] | None = None,
) -> None:
    """Each declaration against the deployment: its base's origin is on the FHIR allow-list;
    the base is registered, for launches (`MENDARI_SMART_EHRS`) or in a CDS client's
    `fhir_servers`, and with sign-in on for the declaration's organization in each registration
    that names it; and each type it declares is one a pack of `packs` may read its list without
    (`excusable`)."""
    from mendari.standards.fhir_client import allowed_servers, origin_of

    servers = allowed_servers() if allowed is None else frozenset(allowed)
    found = list(packs)
    for base, entry in declarations.entries.items():
        where = f"{ENV} base {base}"
        if origin_of(base) not in servers:
            raise MendariError(f"{where} is not on MENDARI_FHIR_ALLOWED_SERVERS")
        launch = ehrs.get(base)
        clients = [c for c in (cds.clients if cds is not None else ()) if base in c.fhir_servers]
        if launch is None and not clients:
            raise MendariError(
                f"{where} is registered neither in MENDARI_SMART_EHRS nor in a CDS client's "
                "fhir_servers"
            )
        if signed_in:
            if launch is not None and launch.org != entry.org:
                raise MendariError(
                    f"{where}: org is not the organization MENDARI_SMART_EHRS registers it to"
                )
            if any(client.org != entry.org for client in clients):
                raise MendariError(
                    f"{where}: org is not the organization of every CDS client that lists it"
                )
        for kind in sorted(entry.not_offered):
            if not any(kind in pack_excusable(pack, entry.not_offered) for pack in found):
                raise MendariError(
                    f"{where}: not_offered {kind}: no pack found here reads it as one of "
                    "several sources of a list, beside a source not declared"
                )


class _KeyTwice(ValueError):
    pass


def _once(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object whose keys are each given once (`json.loads`' `object_pairs_hook`)."""
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise _KeyTwice(key)
        found[key] = value
    return found


def _text(entry: Mapping[str, Any], key: str, most: int, where: str) -> str:
    value = entry[key]
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > most
        or not value.isprintable()
    ):
        raise MendariError(
            f"{where}: {key} must be 1 to {most} printable characters, without spaces at either end"
        )
    return value


def _read_file(name: str) -> str:
    try:
        with Path(name).open("rb") as file:
            data = file.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise MendariError(
            f"{ENV} is neither inline JSON (starting with {{) nor a JSON file that can be read"
        ) from None
    if len(data) > MAX_FILE_BYTES:
        raise MendariError(f"{ENV} names a file larger than {MAX_FILE_BYTES:,} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise MendariError(f"{ENV} names a file that is not UTF-8") from None
