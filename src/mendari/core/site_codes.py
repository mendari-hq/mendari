# SPDX-License-Identifier: Apache-2.0
"""A site's own codes, translated per organization (LP-23i, `MENDARI_SITE_CODES`).

An agent may name sets of its own codes (`code_sets`) and read a field through one (the
`code_set` selector, `standards/fhir.py`). A site's EHR often codes the same things its own way:
a local code system, or another code of a standard one. A site code map says how its codes
translate into the agent's, one organization at a time:

    {"<org>": {"version": "<label>",
               "sets": {"<code set>": [{"system": "<uri>", "code": "<site code>",
                                        "as": "<a code the set declares>"}]}}}

- **Off unless a deployment sets `MENDARI_SITE_CODES`**: then no code is translated, and every
  decision is made as before. Set, it is inline JSON (a value that starts with `{`) or the path
  of a JSON file, read and checked at startup (`site_codes`): its shape (`parse`), and each
  translation against the code sets the agents found here declare (`check`), so a translation
  lands only on a code its set declares, and a set no agent declares stops the server. A refusal
  names the setting, the organization, the code set and the translation's position, never a
  system or a code. A JSON object that gives one key twice is refused, never read as one of its
  meanings.
- **Per organization.** A decision reads with the map of the organization it acts for
  (`tenancy.current()`; the default one in open mode), never another's. With sign-in off every
  organization is `default`. Only the server reads the setting: the command line (`mendari
  decide --fhir`, `mendari bulk dataset`) reads every record without a map.
- **The agent's own codes first.** A translation only fills in for the agent's own codes: a field
  reads all its paths for one of its set's own codes before a translated one counts
  (`standards.fhir.read_field`), so a record whose own codes give the field a value reads the
  same with a map as without.
- **Versioned and hashed.** A map's canonical form (`mendari.site-codes/1`, `canonical`) is the
  format, the organization, the version and every set's translations sorted by system and code,
  as sorted-key JSON with ASCII escapes and no spaces; its SHA-256 is taken over those bytes. The
  same map written in another order has the same digest, and a changed map, or one organization's
  map given to another, has another. Each FHIR decision of an organization with a map records
  `site_codes: {version, sha256}` beside its data-coverage record; it is null otherwise.
- **Never automated.** A decision whose input took a value through a translation goes from AUTO
  to HUMAN_REVIEW, and so does its rule's BLOCK, final at once (the `site_code` cap,
  `core/policy.py`), so configuration never makes AUTO that the agent alone could not
  (Definition of shipped, section 7), and a wrong translation reaches a person. Lifting the cap
  is the owner's call, after the G5-6 `site-validation` prerequisite.

Translations are configuration, not part of an agent: no agent file, logic file or digest changes.
A site's own SNOMED CT codes may sit in its map under its own licence; Mendari ships none, and
its examples are fictional.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from mendari.core.tenancy import DEFAULT_ORG
from mendari.errors import MendariError

if TYPE_CHECKING:
    from mendari.core.agent import Agent

ENV = "MENDARI_SITE_CODES"
FORMAT = "mendari.site-codes/1"
MAX_ORGANIZATIONS = 100
MAX_TRANSLATIONS = 10_000  # in one organization's map, its sets together
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SYSTEM = 500  # characters, as an agent's code set system (`standards.fhir.CodeSet`)
MAX_CODE = 256  # characters, a site's code and the agent's code it translates into
TRANSLATION_KEYS = ("system", "code", "as")
MAP_KEYS = ("version", "sets")
_ORG = re.compile(r"[\x21-\x7e]{1,100}")  # visible ASCII, as a CDS client's `org`
_VERSION = re.compile(r"[\x21-\x7e]{1,64}")
_SET = re.compile(r"[a-z][a-z0-9_]{0,63}")  # as an agent names a code set (`standards.fhir`)
_SYSTEM = re.compile(r"(?:https?://|urn:)\S+")

# One translation, read-only: {"system": ..., "code": ..., "as": ...}.
Translation = Mapping[str, str]


@dataclass(frozen=True)
class SiteCodeMap:
    """One organization's site code map, checked: its version, its translations by code set
    (each `{system, code, as}`, in the order given) and the SHA-256 of its canonical form."""

    org: str
    version: str
    sets: Mapping[str, tuple[Translation, ...]]
    sha256: str

    def stamp(self) -> dict[str, str]:
        """What a decision records of the map it read with: its version and digest."""
        return {"version": self.version, "sha256": self.sha256}


@dataclass(frozen=True)
class SiteCodeMaps:
    """Every organization's site code map (`MENDARI_SITE_CODES`, checked), by organization."""

    maps: Mapping[str, SiteCodeMap]

    def for_org(self, org: str) -> SiteCodeMap | None:
        """The map of `org`, or None: another organization's is never given."""
        return self.maps.get(org)


def canonical(org: str, version: str, sets: Mapping[str, Iterable[Translation]]) -> bytes:
    """A map's canonical form (`mendari.site-codes/1`), whose SHA-256 is its digest: the format,
    the organization, the version and each set's translations sorted by system and code, as JSON
    with sorted keys, no spaces and ASCII escapes."""
    body = {
        "format": FORMAT,
        "org": org,
        "version": version,
        "sets": {
            name: sorted(
                ({key: entry[key] for key in TRANSLATION_KEYS} for entry in translations),
                key=lambda entry: (entry["system"], entry["code"]),
            )
            for name, translations in sets.items()
        },
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("ascii")


def site_codes(agents: Iterable[Agent], *, signed_in: bool) -> SiteCodeMaps | None:
    """`MENDARI_SITE_CODES`, read and checked: None when it is unset, and then no code is
    translated. Inline JSON, or the path of a JSON file (relative to the working directory).
    Anything wrong in it (`parse`), or a translation the code sets of `agents` do not take
    (`check`), stops the server here, at startup."""
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return None
    maps = parse(raw if raw.startswith("{") else _read_file(raw), signed_in=signed_in)
    check(maps, agents)
    return maps


def parse(raw: str, *, signed_in: bool) -> SiteCodeMaps:
    """The maps `raw` (the setting's JSON) holds, checked for their shape: an object of 1 to
    `MAX_ORGANIZATIONS` organizations (`default` alone with sign-in off), each an object of
    `version` (1 to 64 visible ASCII characters) and `sets`, an object of code set names (as an
    agent names them), each a list of one or more translations `{system, code, as}`: `system` a
    uri (`http`, `https` or `urn:`), `code` and `as` text without spaces at either end, and no
    site code (system and code) twice in one set; at most `MAX_TRANSLATIONS` in one map."""
    try:
        given = json.loads(raw, object_pairs_hook=_once)
    except _KeyTwice:
        raise MendariError(f"{ENV} gives a key twice in one object") from None
    except (ValueError, RecursionError):
        raise MendariError(f"{ENV} is not JSON: an object of organizations") from None
    if not isinstance(given, dict) or not 1 <= len(given) <= MAX_ORGANIZATIONS:
        raise MendariError(
            f"{ENV} must be a JSON object of 1 to {MAX_ORGANIZATIONS} organizations, each with "
            "its map"
        )
    maps = {org: _map(org, entry, signed_in=signed_in) for org, entry in given.items()}
    return SiteCodeMaps(MappingProxyType(maps))


def check(maps: SiteCodeMaps, agents: Iterable[Agent]) -> None:
    """Each map against the code sets `agents` declare (spec §9.6 `code_sets`): every set a map
    names is declared by one of them, and every translation's `as` is a code that set declares
    in one of them. A translation then applies in an agent whose set of that name lists its `as`,
    and in no other (`standards.fhir.pick_code`). An agent whose mapping cannot be read declares no
    set here; it is refused where it is read."""
    from mendari.standards.fhir import read_mapping  # core reads a mapping only here

    declared: dict[str, set[str]] = {}
    for agent in agents:
        if agent.manifest.inputs.fhir is None:
            continue
        try:
            mapping = read_mapping(agent)
        except MendariError:
            continue
        for name, code_set in mapping.code_sets.items():
            declared.setdefault(name, set()).update(code_set.codes)
    for site in maps.maps.values():
        for name, translations in site.sets.items():
            codes = declared.get(name)
            where = f"{ENV} organization {site.org!r}, code set {name!r}"
            if codes is None:
                raise MendariError(f"{where}: no agent found here declares it")
            for position, entry in enumerate(translations, start=1):
                if entry["as"] not in codes:
                    raise MendariError(
                        f"{where}, translation {position}: it translates into a code the set "
                        "does not declare"
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


def _map(org: str, entry: object, *, signed_in: bool) -> SiteCodeMap:
    if not _ORG.fullmatch(org):
        raise MendariError(
            f"{ENV}: an organization is 1 to 100 visible ASCII characters, without spaces"
        )
    where = f"{ENV} organization {org!r}"
    if not signed_in and org != DEFAULT_ORG:
        raise MendariError(f"{where}: with sign-in off, the one organization is {DEFAULT_ORG}")
    if not isinstance(entry, dict) or sorted(entry) != sorted(MAP_KEYS):
        raise MendariError(f"{where} must be an object of version and sets")
    version = entry["version"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise MendariError(f"{where}: version must be 1 to 64 visible ASCII characters")
    sets = entry["sets"]
    if not isinstance(sets, dict) or not sets:
        raise MendariError(f"{where}: sets must be an object of one or more code sets")
    checked: dict[str, tuple[Translation, ...]] = {}
    count = 0
    for number, (name, translations) in enumerate(sets.items(), start=1):
        if not _SET.fullmatch(name):
            raise MendariError(f"{where}: code set {number}'s name must match {_SET.pattern}")
        checked[name] = _translations(f"{where}, code set {name!r}", translations)
        count += len(checked[name])
        if count > MAX_TRANSLATIONS:
            raise MendariError(f"{where} has more than {MAX_TRANSLATIONS:,} translations")
    digest = hashlib.sha256(canonical(org, version, checked)).hexdigest()
    return SiteCodeMap(org=org, version=version, sets=MappingProxyType(checked), sha256=digest)


def _translations(where: str, given: object) -> tuple[Translation, ...]:
    if not isinstance(given, list) or not given:
        raise MendariError(f"{where} must be a list of one or more translations")
    seen: set[tuple[str, str]] = set()
    kept: list[Translation] = []
    for position, entry in enumerate(given, start=1):
        at = f"{where}, translation {position}"
        if not isinstance(entry, dict) or sorted(entry) != sorted(TRANSLATION_KEYS):
            raise MendariError(f"{at} must be an object of system, code and as")
        system = entry["system"]
        if not isinstance(system, str) or len(system) > MAX_SYSTEM or not _SYSTEM.fullmatch(system):
            raise MendariError(
                f"{at}: system must be a uri (http, https or urn:) of at most {MAX_SYSTEM} "
                "characters"
            )
        for key in ("code", "as"):
            value = entry[key]
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > MAX_CODE
                or not value.isprintable()
            ):
                raise MendariError(
                    f"{at}: {key} must be 1 to {MAX_CODE} printable characters, without spaces "
                    "at either end"
                )
        pair = (system, entry["code"])
        if pair in seen:
            raise MendariError(f"{at} translates a site code the set translates already")
        seen.add(pair)
        kept.append(MappingProxyType({key: entry[key] for key in TRANSLATION_KEYS}))
    return tuple(kept)
