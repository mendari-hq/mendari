# SPDX-License-Identifier: Apache-2.0
"""FHIR R4 adapter: map resources onto an agent's input, and a decision onto a FHIR Task.

Mappings are declarative (``fhir/mapping.yaml`` in the agent) so that clinical informaticists
can review them without reading code. Paths use a small FHIRPath subset: dotted element names,
where a list-valued element yields all of its items and ``[n]`` selects the n-th item.

`fields` map single values (the first match). `lists` map every resource of a type to an object,
for patient records with many Conditions, Observations or MedicationStatements. Input can be a
Bundle, a single resource, or a CDS Hooks prefetch object whose values may themselves be search
Bundles; all of them are flattened, and resources are de-duplicated by type and id.

The mapping (docs/spec/agent-manifest.md section 9, LP-23g) also has the ``[system=<uri>]``
predicate, which keeps the items of one code system (a coding, an identifier); `paths`, tried in
order until one gives a value; `follow`, one reference hop resolved inside the record and never
fetched (a path of the field's own type read in place, `own_type`, LP-23k); the
`attachment_text` transform; `code_sets` and the `code_set` selector, for a field and for
`focus_where` (LP-23k); and list `resources`, several resource types into one list, with the
`medication_period` derivation. `FhirAdapter.looked_for` says what each field and list looks
for.

The mapping never fetches. A CDS hook and the SMART launch's decide may first read what a
mapping reaches by a link the record does not hold (`standards/linked.py`, LP-23h): each
resource read is added to the record (`Indexed.link`) and found by the reference that named it,
and each link not read keeps its reason (`Indexed.unread`), which the data-coverage record
counts.
"""

from __future__ import annotations

import base64
import html
import math
import re
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mendari.core import clock
from mendari.core.agent import Agent
from mendari.core.spec import AgentManifest, Outcome, Priority
from mendari.core.templates import (
    UNKNOWN,
    expansion_count,
    expansions,
    fill,
    template_problems,
    unknown_placeholders,
)
from mendari.errors import AgentValidationError, FhirError, FhirStructureError, ReadReason

if TYPE_CHECKING:
    from mendari.core.decision import DecisionResult

OUTCOME_SYSTEM = "https://mendari.co/fhir/CodeSystem/decision-outcome"
DECISION_ID_SYSTEM = "https://mendari.co/fhir/NamingSystem/decision-id"
# An agent version (`namespace/name@version`) is a code in `meta.tag` (the CodeSystem), and an
# identifier when it acts as the engine, an AuditEvent `agent` (the NamingSystem).
AGENT_TAG_SYSTEM = "https://mendari.co/fhir/CodeSystem/agent-version"
AGENT_ID_SYSTEM = "https://mendari.co/fhir/NamingSystem/agent-version"
MANUAL_TRIAGE = "manual-triage"  # the performer when there is no recommendation
DISPLAY_LIMIT = 250
# A path segment: an element name, then at most one of `[n]`, `[*]` and (spec §9.2)
# `[system=<uri>]`, a uri as `code_systems` declares one.
_SEGMENT = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9_]*)"
    r"(?:\[(?:(?P<index>\*|\d+)|system=(?P<system>(?:https?://|urn:)[^\s\[\]]+))\])?\Z"
)
_RESOURCE_TYPE = re.compile(r"^[A-Z][A-Za-z]{0,63}\Z")
# A relative reference inside the record: `Type/id`, optionally of one version.
_RELATIVE = re.compile(
    r"^(?P<type>[A-Z][A-Za-z]{0,63})/(?P<id>[A-Za-z0-9.-]{1,64})(?:/_history/[A-Za-z0-9.-]{1,64})?\Z"
)
_CODE_SET = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")
# A reference a runtime may read from a FHIR server (LP-23h, `link_target`): `Type/id`,
# optionally of one version, relative or ending an absolute http(s) URL with no query. A URL's
# scheme matches in any case, as RFC 3986 has it (and as `_ABSOLUTE` matches it).
_LINK = re.compile(
    r"^(?:(?i:https?)://[^\s?#]+/)?(?P<type>[A-Z][A-Za-z]{0,63})/(?P<id>[A-Za-z0-9.-]{1,64})"
    r"(?:/_history/[A-Za-z0-9.-]{1,64})?\Z"
)
_ABSOLUTE = re.compile(r"^https?://", re.IGNORECASE)

# The mapping's bounds for an agent the deployment does not own (`Agent.bounded`), and for every
# agent
# in `mendari validate`: paths a field tries, and codes in a code set. A `follow` is one hop for
# every agent: what it reaches is never followed further.
MAX_PATHS = 4
MAX_CODE_SET_CODES = 200
# The codes a `task.performer` template may expand to (`FhirAdapter.performer_codes`), for every
# agent: each is a concept of the performer CodeSystem built in memory (`terminology.py`), so a
# template such as "{a}{b}{c}{d}" over questions of 255 labels (4 billion codes) is refused at
# load rather than exhausting the server when the terminology is built.
MAX_PERFORMER_CODES = 10_000

# `medication_period` (spec §9.8, LP-23 conflict 39, with the changes the owner, a
# physician, chose on 2026-11-07 in answer to notes/clinician-review-medication-period.md):
# when a MedicationRequest's drug is taken. It starts at its first dosage instruction's bounds,
# else when it was written, and stops at those bounds' end, else at its start plus the bounds'
# duration, else is unknown: never inferred from its status. The dispensing window
# (`dispenseRequest.validityPeriod`) is not when the patient takes the drug, so it is not read.
MEDICATION_START = ("dosageInstruction[0].timing.repeat.boundsPeriod.start", "authoredOn")
# The stop's paths, in order: the bounds' end, then a duration added to the start (`stop_after`).
MEDICATION_END = (
    "dosageInstruction[0].timing.repeat.boundsPeriod.end",
    "dosageInstruction[0].timing.repeat.boundsDuration",
)
# A MedicationRequest left out of its list (`listed`): entered in error, one that is not an order
# to give the drug (its intent a proposal, a plan or an option), or one not to give it.
ENTERED_IN_ERROR = "entered-in-error"
NOT_ORDERS = frozenset({"proposal", "plan", "option"})
UCUM = "http://unitsofmeasure.org"
# The UCUM units a `boundsDuration` may be in, in days as UCUM defines them: an hour is 1/24 of a
# day, a week 7 days, a month (`mo`, the mean Julian month) 30.4375 days and a year (`a`, the
# Julian year) 365.25 days. A duration in any other unit leaves the stop unknown.
DURATION_DAYS: Mapping[str, Fraction] = {
    "h": Fraction(1, 24),
    "d": Fraction(1),
    "wk": Fraction(7),
    "mo": Fraction(487, 16),
    "a": Fraction(1461, 4),
}
_FULL_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T|$)")  # a date or dateTime with its day

# Why an attachment's text was not read (`read_attachment`): reasons of the data-coverage
# record's closed set (`READ_REASONS`, LP-23d).
# Not text/plain or text/html, or no content type; or a url to another type than a Binary, or to
# a Binary with no content.
UNSUPPORTED_TYPE = "unsupported_type"
UNDECODABLE = "undecodable"  # data that is not base64, or not UTF-8 once decoded
NOT_FETCHED = "not_fetched"  # only a `url`, to a Binary that is not in the record
# Notes whose joined text is longer than the agent's input schema lets the field be (`overlong`).
TOO_LARGE = "too_large"
TEXT_TYPES = ("text/plain", "text/html")
# The elements whose repetitions R4 defines as one document in other formats (`renditions`): a
# DocumentReference's `content` ("each with a different format") and a DiagnosticReport's
# `presentedForm` ("multiple formats are allowed but they SHALL be semantically equivalent").
RENDITIONS = frozenset({("DocumentReference", "content"), ("DiagnosticReport", "presentedForm")})
# The whitespace R4's base64Binary allows between its characters (`\s` of its regex, as XML
# Schema has it: space, tab, line feed, carriage return), dropped before decoding.
_BASE64_SPACE = str.maketrans("", "", " \t\n\r")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceRule(_Model):
    required: bool = False


class FieldMapping(_Model):
    """One mapped value: `path`, or `paths` tried in order, the first that gives a value
    winning (spec §9.3). With `follow`, a path to a Reference is followed first, inside the
    record, and the paths are read in the resource it reaches, but a path of the field's own
    type (`own_type`), read where the field is. `code_set` picks a coding of one of the
    mapping's code sets; `map` turns what was found into the agent's value."""

    path: str | None = None
    paths: list[str] | None = Field(default=None, min_length=1)
    follow: str | None = None
    transform: Literal["join", "age_band", "age_years", "base64_text", "attachment_text"] | None = (
        None
    )
    code_set: str | None = None
    map: dict[str, str] | None = None

    @model_validator(mode="after")
    def _one_way(self) -> FieldMapping:
        if (self.path is None) == (self.paths is None):
            raise ValueError("give a field either `path` or `paths`, not both")
        if self.code_set is not None and self.transform is not None:
            raise ValueError("`code_set` picks a code itself, so it takes no `transform`")
        return self

    @property
    def tried(self) -> list[str]:
        """The paths, in the order they are tried."""
        return self.paths if self.paths is not None else [self.path or ""]


class MedicationPeriod(_Model):
    """The item keys a MedicationRequest's derived start and stop go to (spec §9.8)."""

    start: str = Field(min_length=1)
    end: str = Field(min_length=1)


class ListSource(_Model):
    """One resource type of a list (spec §9.7 `resources`): its fields, relative to the
    resource, and for a MedicationRequest optionally `medication_period`."""

    fields: dict[str, FieldMapping] = {}
    medication_period: MedicationPeriod | None = None

    @model_validator(mode="after")
    def _keys_apart(self) -> ListSource:
        period = self.medication_period
        if period is not None:
            if period.start == period.end:
                raise ValueError("medication_period's start and end must be two keys")
            taken = sorted({period.start, period.end} & set(self.fields))
            if taken:
                raise ValueError(f"medication_period writes {taken}, which fields also map")
        return self


class ListMapping(_Model):
    """A list target: `resource` and its `fields`, or (spec §9.7) `resources`, several types
    into one list, each with its own fields, items in the order the types are given."""

    resource: str | None = None
    fields: dict[str, FieldMapping] | None = None  # paths relative to the resource
    resources: dict[str, ListSource] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_form(self) -> ListMapping:
        if self.resources is None and (self.resource is None or self.fields is None):
            raise ValueError("give a list `resource` and `fields`, or `resources`")
        if self.resources is not None and (self.resource is not None or self.fields is not None):
            raise ValueError("give a list `resources`, or `resource` and `fields`, not both")
        return self

    def sources(self) -> dict[str, ListSource]:
        """Each resource type the list reads, with its fields, in order."""
        if self.resources is not None:
            return self.resources
        return {self.resource or "": ListSource(fields=self.fields or {})}


class CodeSet(_Model):
    """A set of the agent's own codes in one code system (spec §9.6 `code_sets`)."""

    system: str = Field(min_length=1, max_length=500, pattern=r"^(?:https?://|urn:)\S+$")
    codes: list[str] = Field(min_length=1)

    @field_validator("codes")
    @classmethod
    def _codes(cls, codes: list[str]) -> list[str]:
        if any(not code or code != code.strip() for code in codes):
            raise ValueError("a code is not empty and has no space at either end")
        if len(set(codes)) != len(codes):
            raise ValueError("codes lists a code more than once")
        return codes


class TaskTemplate(_Model):
    code_text: str
    performer_system: str
    # Templates (core/templates.py): `{recommendation}`, `{priority}`, `{<question>}`.
    # The default description is a message-routing wording.
    description: str = "Route to {recommendation}"
    performer: str | None = None  # a performer code (template); default: the recommendation


class FocusFilter(_Model):
    """Which resources of the focus type count, e.g. referrals among all ServiceRequests: those
    whose value at `path` is one of `in`, or (spec §9.6, LP-23k) whose codings at `path` give a
    code of the mapping's code set `code_set`, a site's translations into it included
    (`FhirAdapter.is_focus`)."""

    path: str  # relative to the resource, e.g. category[0].coding[0].code
    values: list[str] | None = Field(default=None, alias="in", min_length=1)
    code_set: str | None = None

    @model_validator(mode="after")
    def _one_way(self) -> FocusFilter:
        if (self.values is None) == (self.code_set is None):
            raise ValueError("give focus_where either `in` or `code_set`, not both")
        return self


class FhirMapping(_Model):
    fhir_version: Literal["R4"]
    focus: str
    focus_where: FocusFilter | None = None
    resources: dict[str, ResourceRule]
    fields: dict[str, FieldMapping] = {}
    lists: dict[str, ListMapping] = {}
    code_sets: dict[str, CodeSet] = {}
    task: TaskTemplate

    @field_validator("code_sets")
    @classmethod
    def _code_set_names(cls, value: dict[str, CodeSet]) -> dict[str, CodeSet]:
        bad = sorted(name for name in value if not _CODE_SET.match(name))
        if bad:
            raise ValueError(f"code set names {bad} must match {_CODE_SET.pattern}")
        return value

    @model_validator(mode="after")
    def _known_code_sets(self) -> FhirMapping:
        unknown = sorted(
            f"{where} names {mapped.code_set!r}"
            for where, _, mapped in located_fields(self)
            if mapped.code_set is not None and mapped.code_set not in self.code_sets
        )
        named = None if self.focus_where is None else self.focus_where.code_set
        if named is not None and named not in self.code_sets:
            unknown.append(f"focus_where names {named!r}")
        if unknown:
            raise ValueError(f"unknown code sets: {'; '.join(unknown)}")
        return self


class LookedFor(TypedDict):
    """What one mapped field looks for (`FhirAdapter.looked_for`), from the mapping alone."""

    follow: str | None  # the Reference it follows first, as a full path; None without `follow`
    paths: list[str]  # full paths, in the order tried (after a `follow`, in what it reaches)
    systems: list[str]  # the code systems it selects on: its predicates' and its code set's
    codes: list[str]  # the codes it looks for: its code set's, and its `map`'s keys


class Indexed(dict[str, list[dict[str, Any]]]):
    """A record by resource type (`FhirAdapter.index`). `urls` holds each Bundle entry's
    resource by its `fullUrl`, `ids` each resource that has an id by its type and id, and
    `contained` a resource's contained resources by id, so a reference (`#id`, `urn:uuid:…`,
    `Type/id`) resolves inside the record with one lookup (`resolve`), however many resources
    the record or the resource holding the reference has.

    Linked reads (LP-23h, `standards/linked.py`) add to it: `linked` holds each resource read
    for a link the record did not hold, by the reference as written (`link`), which `resolve`
    then finds; `unread` each link that was not read, with why. A resource read so is reached by
    its link only: it is not one of the record's resources of its type."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: dict[str, dict[str, Any]] = {}
        self.ids: dict[tuple[str, str], dict[str, Any]] = {}
        self.linked: dict[str, Mapping[str, Any]] = {}
        self.unread: dict[str, ReadReason] = {}
        # By the container's identity, which the container held beside its table keeps.
        self._contained: dict[int, tuple[Any, dict[str, Mapping[str, Any]]]] = {}

    def link(self, reference: str, resource: Mapping[str, Any]) -> None:
        """`resource` is what `reference` (as written) names: `resolve` finds it from now on."""
        self.linked[reference] = resource
        self.unread.pop(reference, None)

    def contained(self, container: Any) -> Mapping[str, Mapping[str, Any]]:
        """The resources `container` (the resource holding a reference) contains, by id, the
        first of an id: read the first time a `#id` in it is resolved, then looked up."""
        held = self._contained.get(id(container))
        if held is not None and held[0] is container:
            return held[1]
        table: dict[str, Mapping[str, Any]] = {}
        for item in _as_list(_object(container).get("contained")):
            if (
                isinstance(item, Mapping)
                and "resourceType" in item
                and isinstance(item.get("id"), str)
            ):
                table.setdefault(item["id"], item)
        self._contained[id(container)] = (container, table)
        return table

    @classmethod
    def of(cls, record: Mapping[str, Any]) -> Indexed:
        """`record` itself when it is an Indexed; otherwise (a mapping of a resource type to one
        resource or a list of them) its resources by type, with their ids indexed, the first of
        a type and id kept."""
        if isinstance(record, Indexed):
            return record
        indexed = cls()
        for kind, resources in record.items():
            items = _as_list(resources)
            indexed[kind] = items
            for item in items:
                held = item.get("id") if isinstance(item, Mapping) else None
                if held is not None:
                    indexed.ids.setdefault((kind, str(held)), item)
        return indexed


# A site's translations into the mapping's code sets: {code set: [{system, code, as}]}, the
# `sets` of one organization's site code map (LP-23i, `core/site_codes.py`), read by `code_set`
# selectors only.
SiteCodes = Mapping[str, Sequence[Mapping[str, Any]]]


@dataclass(frozen=True)
class Reading:
    """What a mapping reads a record with: the record indexed (for references), the mapping's
    code sets and a site's translations into them. `FhirAdapter.to_input` reads with one, and
    the data-coverage record (`data_coverage.measure`) reads the same way, with the same
    translations. `translated` collects, as the record is read, each code set a translation
    gave a field its value through (`read_field`)."""

    record: Indexed
    code_sets: Mapping[str, CodeSet]
    site_codes: SiteCodes
    translated: set[str] = field(default_factory=set, compare=False)
    # Each code set's translations as a lookup table (`translations`), built once per reading.
    tables: dict[str, Mapping[tuple[str, str], str]] = field(
        default_factory=dict, compare=False, repr=False
    )

    @classmethod
    def of(
        cls, mapping: FhirMapping, record: Mapping[str, Any], site_codes: SiteCodes | None = None
    ) -> Reading:
        """`record` read with `mapping`: references resolve in the record indexed by id once
        (`Indexed.of`), not by a scan for each."""
        return cls(Indexed.of(record), mapping.code_sets, site_codes or {})

    def translations(self, name: str) -> Mapping[tuple[str, str], str]:
        """The site's translations into code set `name` as a table (`translation_table`): built
        the first time a field reads through the set, then shared by every field, path and
        resource of the record, so a map of any size is read once per reading."""
        table = self.tables.get(name)
        if table is None:
            given = self.site_codes.get(name, ())
            table = self.tables[name] = translation_table(given, self.code_sets[name])
        return table


@dataclass(frozen=True)
class MappedInput:
    """A record read with a mapping (`FhirAdapter.read`): the agent's input, and the code sets a
    site's translation gave a field its value through (LP-23i), sorted; empty without
    translations, or when every value came from the agent's own codes."""

    value: dict[str, Any]
    translated: tuple[str, ...]


def _performer_choices(manifest: AgentManifest) -> dict[str, list[str]]:
    """What each placeholder of a `task.performer` template can read (`performer_codes`)."""
    return {
        "recommendation": [*manifest.labels(manifest.policy.primary), MANUAL_TRIAGE],
        "priority": [priority.value for priority in Priority],
        **{question: [*manifest.labels(question), UNKNOWN] for question in manifest.questions},
    }


class FhirAdapter:
    def __init__(self, agent: Agent, mapping: FhirMapping):
        self.agent = agent
        self.mapping = mapping

    @classmethod
    def for_agent(cls, agent: Agent, *, bare: bool | None = None) -> FhirAdapter:
        """The agent's adapter: its mapping, checked, and its Task text's placeholders, which
        must name built-ins or questions. With `bare` (by default for an agent the deployment
        does not own, `Agent.bounded`, which `load_agent` checks so; `mendari validate` asks it
        of every agent) they must also be bare names (`template_problems`). Otherwise an owned
        agent's Task text renders as it always did. The mapping's rules are checked for every agent
        (`mapping_problems`), and its bounds under `bare` too."""
        mapping = read_mapping(agent)
        relative = agent.manifest.inputs.fhir
        templates = {"description": mapping.task.description, "performer": mapping.task.performer}
        questions = agent.manifest.questions
        held = agent.bounded if bare is None else bare
        problems = []
        for name, text in templates.items():
            if not text:
                continue
            if held:
                found = template_problems(text, questions)
                problems += [f"{relative}: task.{name} {problem}" for problem in found]
            elif unknown := unknown_placeholders(text, questions):
                problems.append(f"{relative}: task.{name} uses unknown placeholders {unknown}")
        problems += [
            f"{relative}: {problem}"
            for problem in mapping_problems(mapping, agent.manifest, held=held)
        ]
        performer = mapping.task.performer or "{recommendation}"
        if not unknown_placeholders(performer, questions):
            count = expansion_count(performer, _performer_choices(agent.manifest))
            if count > MAX_PERFORMER_CODES:
                problems.append(
                    f"{relative}: task.performer can give {count:,} codes (every combination of "
                    f"its placeholders' labels); a performer template gives at most "
                    f"{MAX_PERFORMER_CODES:,}: use fewer placeholders"
                )
        if problems:
            raise AgentValidationError(agent.id, problems)
        return cls(agent, mapping)

    def index(
        self,
        resources: Mapping[str, Any],
        *,
        focus: Mapping[str, Any] | None = None,
        excused: Collection[str] = (),
        site_codes: SiteCodes | None = None,
    ) -> Indexed:
        """Accept a Bundle, a single resource, or a mapping of resources (e.g. CDS prefetch).

        With `focus` (e.g. one draft order), the record is indexed around that resource and any
        other resource of the focus type in `resources` is ignored. A required resource type in
        `excused` may be missing: its read failed, and the decision abstains (LP-23d). Which
        resources of the focus type count is `is_focus`'s, with `site_codes` (LP-23k)."""
        indexed = Indexed()
        kept = indexed.ids  # by type and id: a resource seen again is the first one
        table = self._focus_table(site_codes)
        for url, resource in iter_entries(resources if focus is None else [resources, focus]):
            kind = resource["resourceType"]
            if kind == self.mapping.focus and not self._focused(resource, table)[0]:
                continue  # e.g. a lab order next to the referral
            if focus is not None and kind == self.mapping.focus and resource is not focus:
                continue
            key = None if resource.get("id") is None else (kind, str(resource["id"]))
            if key is not None and key in kept:
                if url is not None:
                    indexed.urls.setdefault(url, kept[key])
                continue
            if kind == self.mapping.focus and kind in indexed:
                raise FhirError(f"expected exactly one {kind}, got several")
            copy = dict(resource)
            if key is not None:
                kept[key] = copy
            indexed.setdefault(kind, []).append(copy)
            if url is not None:
                indexed.urls.setdefault(url, copy)
        missing = [
            k
            for k, rule in self.mapping.resources.items()
            if rule.required and k not in indexed and k not in excused
        ]
        if missing:
            raise FhirError(f"missing required FHIR resources: {', '.join(missing)}")
        return indexed

    def is_focus(self, resource: Mapping[str, Any], *, site_codes: SiteCodes | None = None) -> bool:
        """Whether a resource is one the agent decides about: of the focus type and, with
        `focus_where`, one of its values at its path; or (spec §9.6) a code of its code set among
        the codings at its path, read as a `code_set` field reads them, a site's translations
        into the set (`site_codes`, LP-23i) included. A decision about a focus that counts only
        through a translation is never AUTO (`read`'s `translated`, LP-23k)."""
        return self._focused(resource, self._focus_table(site_codes))[0]

    def _focus_table(self, site_codes: SiteCodes | None) -> Mapping[tuple[str, str], str]:
        """The site's translations into `focus_where`'s code set, as a lookup table, built once
        for a call that reads many resources (`index`, `focus_resources`)."""
        rule = self.mapping.focus_where
        if rule is None or rule.code_set is None or not site_codes:
            return {}
        given = site_codes.get(rule.code_set, ())
        return translation_table(given, self.mapping.code_sets[rule.code_set])

    def _focused(
        self, resource: Mapping[str, Any], table: Mapping[tuple[str, str], str]
    ) -> tuple[bool, bool]:
        """`is_focus` with a translation table, and whether only a translation made it count."""
        focus, rule = self.mapping.focus, self.mapping.focus_where
        if resource.get("resourceType") != focus:
            return False, False
        if rule is None:
            return True, False
        found = select({focus: resource}, f"{focus}.{rule.path}")
        if rule.code_set is None:
            return any(str(v) in (rule.values or ()) for v in found), False
        code, translated = picked(found, self.mapping.code_sets[rule.code_set], table)
        return code is not None, translated

    def focus_resources(
        self, resources: Any, *, site_codes: SiteCodes | None = None
    ) -> list[dict[str, Any]]:
        """The focus resources in a Bundle, resource or mapping (e.g. CDS `draftOrders`), by
        `is_focus` with `site_codes`."""
        table = self._focus_table(site_codes)
        found: dict[str, dict[str, Any]] = {}
        for resource in iter_resources(resources):
            if self._focused(resource, table)[0]:
                found.setdefault(str(resource.get("id", len(found))), dict(resource))
        return list(found.values())

    def to_input(
        self, indexed: Mapping[str, Any], *, site_codes: SiteCodes | None = None
    ) -> dict[str, Any]:
        """The agent's input from an indexed record: `read`'s value."""
        return self.read(indexed, site_codes=site_codes).value

    def read(
        self, indexed: Mapping[str, Any], *, site_codes: SiteCodes | None = None
    ) -> MappedInput:
        """The agent's input from an indexed record, and the code sets a translation gave it a
        value through. `site_codes` (LP-23i) are a site's translations into the mapping's code
        sets, read by `code_set` selectors only (a field's, and `focus_where`'s, LP-23k); without
        them a code set matches its own codes only. Nothing else reads them. A focus that counts
        only through a translation adds its code set too, so its decision is never AUTO."""
        reading = Reading.of(self.mapping, indexed, site_codes)
        rule = self.mapping.focus_where
        if rule is not None and rule.code_set is not None:
            table = reading.translations(rule.code_set)
            if any(self._focused(r, table)[1] for r in _as_list(indexed.get(self.mapping.focus))):
                reading.translated.add(rule.code_set)
        result: dict[str, Any] = {}
        for target, mapped in self.mapping.fields.items():
            value = read_field(mapped, indexed, None, reading).value
            if value is not None:
                _put(result, target, value)
        for target, spec in self.mapping.lists.items():
            items = []
            for kind, source in spec.sources().items():
                for resource in _as_list(indexed.get(kind)):
                    item = list_item(kind, source, resource, reading)
                    if item:
                        items.append(item)
            _put(result, target, items)
        return MappedInput(result, tuple(sorted(reading.translated)))

    def looked_for(self) -> dict[str, Any]:
        """What each mapped field and list looks for, from the mapping alone, never the record:
        `looked_for` of the adapter's mapping (LP-23g), which the data-coverage record LP-23d
        builds (`data_coverage.measure`) counts against."""
        return looked_for(self.mapping)

    @property
    def performer_template(self) -> str:
        """The performer code's template; without one, the Task goes to the recommendation."""
        return self.mapping.task.performer or "{recommendation}"

    def performer_codes(self) -> dict[str, dict[str, str]]:
        """Every code `to_task` can put in `performerType`, each with the placeholder values that
        give it (the first found), for the agent's performer CodeSystem (`terminology.py`)."""
        choices = _performer_choices(self.agent.manifest)
        codes: dict[str, dict[str, str]] = {}
        for code, values in expansions(self.performer_template, choices):
            codes.setdefault(code, values)
        return codes

    def to_task(self, result: DecisionResult, indexed: Mapping[str, Any]) -> dict[str, Any]:
        """The decision as a Task. Its `meta.versionId` is left to the server that stores it;
        `meta.lastUpdated` is when the decision was made."""
        focus: Mapping[str, Any] = next(iter(_as_list(indexed.get(self.mapping.focus))), {})
        patient: Mapping[str, Any] = next(iter(_as_list(indexed.get("Patient"))), {})
        reference = _object(focus.get("subject")).get("reference")
        subject = (reference if isinstance(reference, str) else None) or (
            f"Patient/{patient['id']}" if isinstance(patient.get("id"), str) else None
        )
        handled_by = result.recommendation or MANUAL_TRIAGE
        template = self.mapping.task
        agent = f"{result.agent_id}@{result.agent_version}"
        performer = fill(self.performer_template, result, recommendation=handled_by)
        # Codes, ids and times only: reasons and descriptions can quote the case.
        summary = (
            f"Decision {result.id} by {agent}: {result.outcome.value}, priority "
            f"{result.priority.value}, performer {performer}. Made {result.created_at}."
        )
        task: dict[str, Any] = {
            "resourceType": "Task",
            "meta": {
                "lastUpdated": result.created_at,
                "tag": [{"system": AGENT_TAG_SYSTEM, "code": agent}],
            },
            "text": narrative(summary),
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
                {"coding": [{"system": template.performer_system, "code": performer}]}
            ],
            "note": [{"text": reason} for reason in result.reasons],
        }
        if isinstance(focus.get("id"), str):
            task["focus"] = {"reference": f"{self.mapping.focus}/{focus['id']}"}
        if subject:
            task["for"] = {"reference": subject}
        if result.confidence is not None:
            task["input"] = [
                {"type": {"text": "confidence"}, "valueDecimal": round(result.confidence, 4)}
            ]
        return task


def display(text: str) -> str:
    """A coding's display from agent text (e.g. a label's criteria): whitespace collapsed and, past
    250 characters, cut on a word boundary. Mendari's CodeSystems and the codings it emits share
    this, so a display always matches its CodeSystem."""
    text = " ".join(text.split())
    if len(text) <= DISPLAY_LIMIT:
        return text
    head = text[:DISPLAY_LIMIT]
    head = head.rsplit(" ", 1)[0] if " " in head else head[:-1]
    return head.rstrip(" ,;:") + "…"


def narrative(summary: str, rows: Sequence[tuple[str, str]] = ()) -> dict[str, str]:
    """A generated narrative (`text`, dom-6): a sentence and an optional code/display table.
    Callers pass codes, ids, times and agent-authored text, never case text."""
    body = f"<p>{html.escape(summary)}</p>"
    if rows:
        cells = "".join(
            f"<tr><td>{html.escape(code)}</td><td>{html.escape(shown)}</td></tr>"
            for code, shown in rows
        )
        body += f"<table><tr><th>Code</th><th>Display</th></tr>{cells}</table>"
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{body}</div>'}


def select(indexed: Mapping[str, Any], path: str) -> list[Any]:
    """Values at `path`; `indexed` maps a resource type to one resource or a list of them."""
    return [value for _, value in _walk(indexed, path)]


def _walk(indexed: Mapping[str, Any], path: str) -> list[tuple[Any, Any]]:
    """`select`, each value with the resource it was read from (whose contained resources a
    `#id` reference names). `[n]` indexes everything the path found so far, across resources."""
    resource_type, *segments = split_path(path)
    current: list[tuple[Any, Any]] = [(r, r) for r in _as_list(indexed.get(resource_type))]
    for segment in segments:
        match = _SEGMENT.match(segment)
        if match is None:
            raise FhirError(f"unsupported path segment {segment!r} in {path!r}")
        collected: list[tuple[Any, Any]] = []
        for root, node in current:
            value = node.get(match["name"]) if isinstance(node, Mapping) else None
            if isinstance(value, list):
                collected.extend((root, item) for item in value)
            elif value is not None:
                collected.append((root, value))
        system = match["system"]
        if system is not None:
            collected = [
                (root, item)
                for root, item in collected
                if isinstance(item, Mapping) and item.get("system") == system
            ]
        index = match["index"]
        if index is not None and index != "*":
            collected = collected[int(index) : int(index) + 1]
        current = collected
    return current


def split_path(path: str) -> list[str]:
    """A path's segments: split at each dot outside `[...]`, since a system uri holds dots. A
    path with no `[system=…]` splits as `path.split(".")` does."""
    segments, depth, start = [], 0, 0
    for position, char in enumerate(path):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(depth - 1, 0)
        elif char == "." and depth == 0:
            segments.append(path[start:position])
            start = position + 1
    segments.append(path[start:])
    return segments


def resolve(reference: Any, container: Any, record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The resource a FHIR Reference points to, found inside the record and never fetched: `#id`
    among the contained resources of `container` (the resource holding the reference), a
    Bundle entry by its exact `fullUrl` (`urn:uuid:…`, or an absolute URL an entry has), a
    resource a linked read added for that reference as written (`Indexed.link`), or `Type/id`
    among the record's resources. None for anything else: an absolute URL no entry has, a
    reference by identifier only, a malformed one. In an `Indexed` record, a lookup; a plain
    mapping is indexed for the call."""
    target = reference.get("reference") if isinstance(reference, Mapping) else None
    if not isinstance(target, str) or not target:
        return None
    indexed = Indexed.of(record)
    if target.startswith("#"):
        return indexed.contained(container).get(target[1:])
    if target in indexed.urls:
        return indexed.urls[target]
    if target in indexed.linked:
        return indexed.linked[target]
    match = _RELATIVE.match(target)
    if match is None:
        return None
    return indexed.ids.get((match["type"], match["id"]))


def link_target(reference: str) -> tuple[str, str] | None:
    """The resource type and id a reference names when it may be read from a FHIR server
    (LP-23h): `Type/id`, optionally `/_history/<version>`, relative or at the end of an absolute
    http(s) URL with no query. None for anything else: `#id`, `urn:…`, a search, an operation."""
    match = _LINK.match(reference)
    return None if match is None else (match["type"], match["id"])


def is_absolute(reference: str) -> bool:
    """Whether a reference or url is an absolute http(s) URL."""
    return _ABSOLUTE.match(reference) is not None


def waiting_link(attachment: Any, container: Any, record: Mapping[str, Any]) -> str | None:
    """The `url` an attachment's text waits on (LP-23h): a text/plain or text/html attachment
    with no data, whose url names nothing inside the record (`resolve`) and is a link a runtime
    may read, a relative `Type/id` or any absolute http(s) URL; the runtime reads only a Binary,
    and gives any other type `UNSUPPORTED_TYPE`. None for anything else."""
    if not isinstance(attachment, Mapping) or attachment.get("data") is not None:
        return None
    url = attachment.get("url")
    if not isinstance(url, str) or _media_type(attachment.get("contentType")) not in TEXT_TYPES:
        return None
    if resolve({"reference": url}, container, record) is not None:
        return None
    return url if is_absolute(url) or link_target(url) is not None else None


def renditions(path: str) -> bool:
    """Whether a path reads renditions of one document (`RENDITIONS`): the attachments of a
    DocumentReference's `content` or a DiagnosticReport's `presentedForm`."""
    segments = split_path(path)
    return len(segments) > 1 and (segments[0], segments[1].split("[", 1)[0]) in RENDITIONS


def unread_notes(
    path: str, pairs: Sequence[tuple[Any, Any]], record: Mapping[str, Any]
) -> list[tuple[str, str | None]]:
    """Each note at `path` that could not be read, in order, as its reason (`read_attachment`)
    and, for `UNSUPPORTED_TYPE`, the label of its format (`format_label`), else None; `pairs`
    are the path's values, each with the resource holding it. An attachment with neither data
    nor a url has nothing to read and counts for nothing.

    Where the path reads renditions (`renditions`), the attachments of one resource are one
    document in other formats (the HTML and the RTF of one note), so they count as one: nothing
    when one of them was read as text, else once, by the reason of its first text/plain or
    text/html rendition that has one (`cap`, a failed read, `unsupported_type` when its Binary
    holds another type), else by its first reason (`unsupported_type`: a note with no text
    rendition, RTF, PDF or CDA only). A text rendition read and empty beside an RTF one is no
    text read: the note counts, by the RTF's reason. Elsewhere (a Communication's payloads) each
    attachment that cannot be read counts."""
    results: list[tuple[Any, Any, str | None, str | None]] = []
    for container, attachment in pairs:
        if (
            isinstance(attachment, Mapping)
            and attachment.get("data") is None
            and attachment.get("url") is None
        ):
            continue  # nothing there to read
        text, reason = read_attachment(attachment, container=container, record=record)
        results.append((container, attachment, text, reason))
    if not renditions(path):
        return [
            (reason, _unread_format(reason, attachment, container, record))
            for container, attachment, _, reason in results
            if reason is not None
        ]
    groups: dict[int, list[tuple[Any, Any, str | None, str | None]]] = {}
    for item in results:
        groups.setdefault(id(item[0]), []).append(item)
    found: list[tuple[str, str | None]] = []
    for items in groups.values():
        if any(text for _, _, text, _ in items):
            continue  # one rendition reads: the others are the same document
        unread = [(c, a, reason) for c, a, _, reason in items if reason is not None]
        if not unread:
            continue  # every rendition read, and empty
        texts = [u for u in unread if _media_type(_object(u[1]).get("contentType")) in TEXT_TYPES]
        container, attachment, reason = (texts or unread)[0]
        found.append((reason, _unread_format(reason, attachment, container, record)))
    return found


def format_label(media_type: str | None) -> str:
    """A content type a note could not be read in, as one label of a closed set, never the
    type as written (its parameters can carry a file name): `rtf`, `pdf`, `xml` (CDA and other
    XML), `image`, `none` (no content type) or `other`."""
    kind = _media_type(media_type)
    if kind is None:
        return "none"
    if kind in ("application/rtf", "text/rtf", "text/richtext"):
        return "rtf"
    if kind == "application/pdf":
        return "pdf"
    if kind.endswith(("/xml", "+xml")):
        return "xml"
    if kind.startswith("image/"):
        return "image"
    return "other"


def _unread_format(
    reason: str, attachment: Any, container: Any, record: Mapping[str, Any]
) -> str | None:
    """The format label of a note that could not be read as `UNSUPPORTED_TYPE`, else None: the
    Binary's own type when the record holds the Binary its url names (as `read_attachment` reads
    it), else the attachment's."""
    if reason != UNSUPPORTED_TYPE:
        return None
    given = _object(attachment)
    url = given.get("url")
    if given.get("data") is None and isinstance(url, str):
        binary = resolve({"reference": url}, container, record)
        held = _object(binary)
        if held.get("resourceType") == "Binary" and held.get("contentType") is not None:
            return format_label(held.get("contentType"))
    return format_label(given.get("contentType"))


def read_attachment(
    attachment: Any, *, container: Any = None, record: Mapping[str, Any] | None = None
) -> tuple[str | None, str | None]:
    """An Attachment's text and, when it could not be read, why (spec §9.5 `attachment_text`).

    Its content is its inline `data` or, with only a `url`, a Binary that url names inside the
    record (`resolve`: contained in `container`, a Bundle entry, `Binary/<id>`, or one a linked
    read added; never fetched here). text/plain is read as it is, text/html as text through
    `html.parser` with scripts and styles dropped, both as UTF-8. Returns (text, None) when
    read; (None, reason) when the content could not be: `UNSUPPORTED_TYPE` for any other
    content type (a missing one included), and for a url to another type than a Binary or to a
    Binary with no content; `UNDECODABLE` for data that is not base64 (line breaks and spaces in
    it aside) or not UTF-8; and for a url to a resource the record does not hold, why a linked
    read did not read it (`Indexed.unread`: `off_server`, `cap`, `unsupported_type`, a failed
    read), else `NOT_FETCHED`; (None, None) for an attachment with no content, or empty text."""
    if not isinstance(attachment, Mapping):
        return None, UNSUPPORTED_TYPE
    kind = _media_type(attachment.get("contentType"))
    if kind not in TEXT_TYPES:
        return None, UNSUPPORTED_TYPE
    data = attachment.get("data")
    url = attachment.get("url")
    if data is None and url is not None:
        indexed = Indexed.of(record or {})
        binary = resolve({"reference": url}, container, indexed)
        if binary is None:
            unread = indexed.unread.get(url) if isinstance(url, str) else None
            return None, unread or NOT_FETCHED
        if binary.get("resourceType") != "Binary" or binary.get("data") is None:
            return None, UNSUPPORTED_TYPE  # another type, or a Binary that holds no content
        kind = _media_type(binary.get("contentType")) or kind  # what the Binary says it holds
        if kind not in TEXT_TYPES:
            return None, UNSUPPORTED_TYPE
        data = binary["data"]
    if data is None:
        return None, None
    try:
        # R4's base64Binary may hold whitespace (data wrapped every 76 characters, as MIME
        # does), which the strict decode refuses: it is dropped first.
        text = base64.b64decode(str(data).translate(_BASE64_SPACE), validate=True).decode("utf-8")
    except ValueError:  # binascii.Error, UnicodeDecodeError, or non-ASCII text
        return None, UNDECODABLE
    text = html_text(text) if kind == "text/html" else text.strip()
    return text or None, None


def overlong(mapping: FhirMapping, value: dict[str, Any], schema: Any) -> tuple[str, ...]:
    """Each `attachment_text` field of the mapping's `fields` whose text in `value` (the input
    `FhirAdapter.read` gave) is longer than the `maxLength` the agent's input `schema` gives that
    field (found by `properties`, one part of its dotted target at a time), sorted, each taken
    out of `value` (spec §9.5). The texts of all of a patient's notes, joined, can be longer
    than the schema allows one field, which would refuse the whole input (a BLOCK, which shows no
    card unless the agent's card template names `refused`); without the field, the decision
    abstains instead (`could not read <field>: too_large`), for a person, as for a note it could
    not read. The text is never cut: a cut could lose the very sentence a criterion reads."""
    found = []
    for target, most in text_limits(mapping, schema).items():
        *parents, leaf = target.split(".")
        holder: Any = value
        for part in parents:
            holder = holder.get(part) if isinstance(holder, dict) else None
        text = holder.get(leaf) if isinstance(holder, dict) else None
        if isinstance(text, str) and len(text) > most:
            del holder[leaf]
            found.append(target)
    return tuple(sorted(found))


def text_limits(mapping: FhirMapping, schema: Any) -> dict[str, int]:
    """The `maxLength` the agent's input `schema` gives each `attachment_text` field of the
    mapping's `fields` (found by `properties`, one part of its dotted target at a time), for the
    fields it gives one: what `overlong` measures the joined text against, and past which a
    linked read stops reading the field's notes (`standards/linked.py`)."""
    limits = {}
    for target, mapped in mapping.fields.items():
        if mapped.transform != "attachment_text":
            continue
        node: Any = schema
        for part in target.split("."):
            properties = node.get("properties") if isinstance(node, Mapping) else None
            node = properties.get(part) if isinstance(properties, Mapping) else None
        most = node.get("maxLength") if isinstance(node, Mapping) else None
        if isinstance(most, int) and not isinstance(most, bool):
            limits[target] = most
    return limits


def _media_type(value: Any) -> str | None:
    """A content type's media type, lower case, without its parameters (`; charset=…`)."""
    if not isinstance(value, str):
        return None
    return value.split(";")[0].strip().lower() or None


def decoded_size(data: Any) -> int:
    """The bytes base64 `data` decodes to, from its length (the whitespace R4 allows between its
    characters aside), without decoding it; 0 for anything that is not text."""
    if not isinstance(data, str):
        return 0
    compact = data.translate(_BASE64_SPACE)
    return max(len(compact) * 3 // 4 - compact[-2:].count("="), 0)


class _HtmlText(HTMLParser):
    """The text of an HTML document: scripts and styles dropped, a line break at each block."""

    DROPPED = frozenset({"script", "style"})
    BLOCKS = frozenset(
        {
            "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
            "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p",
            "pre", "section", "table", "td", "th", "tr", "ul",
        }
    )  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.dropped = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.DROPPED:
            self.dropped += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.DROPPED:
            self.dropped = max(self.dropped - 1, 0)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.dropped:
            self.parts.append(data)


def html_text(markup: str) -> str:
    """HTML as plain text (`read_attachment`): scripts and styles dropped, entities decoded,
    each block on its own line, spaces collapsed and empty lines left out."""
    parser = _HtmlText()
    parser.feed(markup)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def translation_table(translations: Iterable[Any], code_set: CodeSet) -> dict[tuple[str, str], str]:
    """A site's translations into `code_set` as a lookup table: each site code (system, code) to
    the set's own code it stands for. A translation whose `as` the set does not list, or whose
    system, code or `as` is not text, is left out."""
    own = set(code_set.codes)
    return {
        pair: entry["as"]
        for entry in translations
        if isinstance(entry, Mapping)
        and (pair := _system_code(entry)) is not None
        and isinstance(entry.get("as"), str)
        and entry["as"] in own
    }


def picked(
    values: Iterable[Any], code_set: CodeSet, table: Mapping[tuple[str, str], str]
) -> tuple[str | None, bool]:
    """The first of a code set's own codes among `values` (Codings, or CodeableConcepts whose
    codings are read), by the set's system and code: the `code_set` selector (spec §9.6). A coding
    of another system counts only through `table`, a site's translations (`{system, code, as}`,
    LP-23i) as `translation_table` makes them (a reading builds each set's once,
    `Reading.translations`), and then only as an `as` the set lists; a coding of the set's own
    system among `values` wins over a translated one. (A field with `paths` reads every path for
    the set's own codes before a translated one counts: `read_field`.) With it, whether a
    translation gave it (False for the set's own code); None and False when no coding matches."""
    codings = codings_of(values)
    own = set(code_set.codes)
    for coding in codings:
        code = coding.get("code")
        if coding.get("system") == code_set.system and isinstance(code, str) and code in own:
            return code, False
    if table:
        for coding in codings:
            key = _system_code(coding)
            if key is not None and key in table:
                return table[key], True
    return None, False


def codings_of(values: Iterable[Any]) -> list[Mapping[str, Any]]:
    """The codings among `values` as a `code_set` selector reads them (`picked`): a
    CodeableConcept's codings, a Coding as it is; anything that is not an object is left out."""
    codings: list[Mapping[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        if isinstance(value.get("coding"), list):
            codings += [coding for coding in value["coding"] if isinstance(coding, Mapping)]
        else:
            codings.append(value)
    return codings


def _system_code(coding: Mapping[str, Any]) -> tuple[str, str] | None:
    """A coding's (system, code), when both are text."""
    system, code = coding.get("system"), coding.get("code")
    return (system, code) if isinstance(system, str) and isinstance(code, str) else None


def medication_period(request: Mapping[str, Any]) -> tuple[Any, Any]:
    """A MedicationRequest's (start, stop) by `medication_period` (spec §9.8): the start is its
    first dosage instruction's `timing.repeat.boundsPeriod.start`, else `authoredOn`; the stop is
    that `boundsPeriod.end`, else the start plus that timing's `boundsDuration` (`stop_after`),
    else None (unknown), never inferred from its status."""
    scope = {"MedicationRequest": request}

    def first(paths: Sequence[str]) -> Any:
        for path in paths:
            found = select(scope, f"MedicationRequest.{path}")
            if found:
                return found[0]
        return None

    start = first(MEDICATION_START)
    end = first(MEDICATION_END[:1])
    if end is None:
        end = stop_after(start, first(MEDICATION_END[1:]))
    return start, end


def stop_after(start: Any, duration: Any) -> str | None:
    """The day a drug taken from `start` for `duration` (an R4 Duration) stops, as a FHIR date:
    the start's day plus the duration in whole days, rounded up (`duration_days`), so never
    earlier than the duration says. None, the stop unknown, when the start is not a date or
    dateTime with its day (a year or a month alone is not), or the duration has no days."""
    match = _FULL_DATE.match(start) if isinstance(start, str) else None
    days = duration_days(duration)
    if match is None or days is None:
        return None
    try:
        return (date(*map(int, match.groups())) + timedelta(days=days)).isoformat()
    except (ValueError, OverflowError):  # no such day, or past the last date there is
        return None


def duration_days(duration: Any) -> int | None:
    """An R4 Duration in whole days, rounded up: its `value`, a finite number of at least 0,
    times the days of its `code`, one of `DURATION_DAYS`' UCUM units. None when it is not such
    a Duration: no value or code, a code of another unit (its `unit`, the text for people, is not
    read), or a `system` other than UCUM's, which R4 requires a Duration's system to be."""
    if not isinstance(duration, Mapping):
        return None
    value, code, system = duration.get("value"), duration.get("code"), duration.get("system")
    if system is not None and system != UCUM:
        return None
    if not isinstance(code, str) or code not in DURATION_DAYS:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if (isinstance(value, float) and not math.isfinite(value)) or value < 0:
        return None
    # Exact: an integer as it is, a decimal as written (its shortest repr), never its binary value.
    amount = Fraction(value) if isinstance(value, int) else Fraction(repr(value))
    return math.ceil(amount * DURATION_DAYS[code])


def _absolute(kind: str | None, path: str) -> str:
    """A field's path as `select` reads it: a list field's starts from its resource type."""
    return path if kind is None else f"{kind}.{path}"


@dataclass(frozen=True)
class FieldRead:
    """How a field was read in one scope (`read_field`): its value (None when it got none) and
    each path it read, in order, as `select` reads it, with what it was read in and what it
    found there (each value with the resource it was read from). That is every path it tries
    when none gives a value, else those up to the one that did (a value through a site's
    translation reads on, for one of the set's own codes: `read_field`). With `follow`, how many
    references the field found and how many resources they reached inside the record, and
    `missing`, each reference as written that reached nothing and is a link (`link_target`) to a
    type the field's paths read: what a linked read reads (LP-23h), and what the data-coverage
    record counts by `Indexed.unread`'s reason, else `not_fetched`."""

    value: Any
    reads: tuple[tuple[str, Mapping[str, Any], list[tuple[Any, Any]]], ...]
    references: int = 0
    reached: int = 0
    missing: tuple[str, ...] = ()


def read_field(
    mapped: FieldMapping, scope: Mapping[str, Any], kind: str | None, reading: Reading
) -> FieldRead:
    """A field read in `scope` (the record, or one list resource as {its type: it}; `kind` is a
    list resource's type, which its relative paths start from): each of its paths in order, the
    first that gives a value winning, so a later path is not read at all. A value a site's
    translation gave (`code_set`, LP-23i) wins only when no path gives one of the set's own
    codes (spec §9.6): the paths after it are read for one, and when none gives one the
    first translated value is the field's, its code set added to `reading.translated`. A
    `follow` is resolved once for the field, whatever number of paths it tries, and the paths
    are read in the resources it reached, but a path that starts with the field's own type
    (`own_type`), which is read where the field is, as without `follow` (LP-23k)."""
    references, reached = 0, None
    missing: tuple[str, ...] = ()
    own = own_type(mapped, kind)
    if mapped.follow is not None:
        types = followed_types(mapped, kind)
        references, reached, missing = _followed(mapped.follow, scope, kind, reading.record, types)
    reads: list[tuple[str, Mapping[str, Any], list[tuple[Any, Any]]]] = []
    value = None
    through = None  # the first value a translation gave, while a later path may give an own code
    for path in mapped.tried:
        if reached is None:
            where, at = scope, _absolute(kind, path)
        elif split_path(path)[0] == own:  # the field's own resource: read in place
            where, at = scope, path
        else:
            wanted = split_path(path)[0]  # the type the path reads, among what was reached
            where, at = {wanted: [r for r in reached if r.get("resourceType") == wanted]}, path
        found = _walk(where, at)
        reads.append((at, where, found))
        given, translated = _value(mapped, found, reading)
        if given is not None and not translated:
            value = given
            break
        if through is None:
            through = given
    if value is None and through is not None and mapped.code_set is not None:
        value = through
        reading.translated.add(mapped.code_set)
    return FieldRead(value, tuple(reads), references, len(reached or ()), missing)


def own_type(mapped: FieldMapping, kind: str | None) -> str | None:
    """The type a followed field reads from itself (spec §9.4, LP-23k): a list field's
    resource type, or a top-level field's `follow` path's (`Coverage` for `Coverage.payor[0]`).
    A path of a followed field that starts with it is read where the field is, so a field may
    fall back from what its reference reaches to its own resource (a MedicationRequest's
    `medicationCodeableConcept`, a Coverage's own payor identifier). None without `follow`."""
    if mapped.follow is None:
        return None
    return kind if kind is not None else split_path(mapped.follow)[0]


def followed_types(mapped: FieldMapping, kind: str | None) -> set[str]:
    """The types a followed field's paths read in what its reference reaches: each path's type
    but the field's own (`own_type`). A reference that reaches nothing is a link only to one of
    these (`_followed`, `standards/linked.py`)."""
    own = own_type(mapped, kind)
    return {head for path in mapped.tried if (head := split_path(path)[0]) != own}


def _followed(
    follow: str,
    scope: Mapping[str, Any],
    kind: str | None,
    record: Indexed,
    types: Collection[str],
) -> tuple[int, list[Mapping[str, Any]], tuple[str, ...]]:
    """How many references `follow` finds, what they reach inside the record, in order, each
    once, and each reference that reached nothing and is a link to one of `types` (the types
    the field's paths read), as written, in order."""
    reached: list[Mapping[str, Any]] = []
    seen: set[int] = set()  # by identity: a resource reached twice is read once
    missing: list[str] = []
    references = _walk(scope, _absolute(kind, follow))
    for container, reference in references:
        found = resolve(reference, container, record)
        if found is None:
            target = _object(reference).get("reference")
            named = link_target(target) if isinstance(target, str) else None
            if named is not None and named[0] in types:
                missing.append(str(target))
        elif id(found) not in seen:
            seen.add(id(found))
            reached.append(found)
    return len(references), reached, tuple(missing)


def _value(
    mapped: FieldMapping, pairs: list[tuple[Any, Any]], reading: Reading
) -> tuple[Any, bool]:
    """What one path gives a field (None for nothing), and whether a site's translation gave
    it (a `code_set` field's only)."""
    if mapped.transform == "attachment_text":  # each attachment with the resource holding it
        texts = [read_attachment(v, container=c, record=reading.record)[0] for c, v in pairs]
        return "\n\n".join(text for text in texts if text) or None, False
    values = [value for _, value in pairs]
    if mapped.code_set is not None:
        table = reading.translations(mapped.code_set)
        code, translated = picked(values, reading.code_sets[mapped.code_set], table)
        value = code if code is None or mapped.map is None else mapped.map.get(code)
        return value, translated and value is not None
    return _apply(mapped, values), False


def listed(source: ListSource, resource: Any) -> bool:
    """Whether a resource of a list source's type can be an item of the list: all can but, when
    its source derives a MedicationRequest's period (spec §9.8), a request entered in error,
    one whose `intent` is `proposal`, `plan` or `option` (not an order to give the drug), and
    one whose `doNotPerform` is true (an order not to give it). What a resource left out so
    holds is not read: no linked read is made for it (LP-23h), and nothing it could not read is
    counted."""
    if source.medication_period is None:
        return True
    request = _object(resource)
    intent = request.get("intent")
    return (
        request.get("status") != ENTERED_IN_ERROR
        and not (isinstance(intent, str) and intent in NOT_ORDERS)
        and request.get("doNotPerform") is not True
    )


def list_item(kind: str, source: ListSource, resource: Any, reading: Reading) -> dict[str, Any]:
    """One resource of type `kind` as an item of a mapped list, by its source's fields and its
    `medication_period`: each key that has a value. An empty item is left out of the list, and
    a resource the list cannot hold (`listed`) is empty."""
    period = source.medication_period
    if not listed(source, resource):
        return {}
    item: dict[str, Any] = {}
    for name, mapped in source.fields.items():
        value = read_field(mapped, {kind: resource}, kind, reading).value
        if value is not None:
            item[name] = value
    if period is not None:
        start, end = medication_period(_object(resource))
        for key, value in ((period.start, start), (period.end, end)):
            if value is not None:
                item[key] = value
    return item


def located_fields(mapping: FhirMapping) -> Iterator[tuple[str, str | None, FieldMapping]]:
    """Every field of a mapping: where it is (`fields.<target>`, `lists.<target>.fields.<key>`,
    `lists.<target>.resources.<type>.fields.<key>`), its list's resource type (None for a top
    level field) and the field."""
    for target, mapped in mapping.fields.items():
        yield f"fields.{target}", None, mapped
    for target, spec in mapping.lists.items():
        if spec.resources is None:
            for key, mapped in (spec.fields or {}).items():
                yield f"lists.{target}.fields.{key}", spec.resource, mapped
            continue
        for kind, source in spec.resources.items():
            for key, mapped in source.fields.items():
                yield f"lists.{target}.resources.{kind}.fields.{key}", kind, mapped


def read_mapping(agent: Agent) -> FhirMapping:
    """An agent's FHIR mapping, of the mapping's shape: an `AgentValidationError` naming the file
    otherwise, and a `FhirError` for an agent that declares none."""
    relative = agent.manifest.inputs.fhir
    if relative is None:
        raise FhirError(f"agent {agent.id} does not declare a FHIR mapping")
    try:
        return FhirMapping.model_validate(agent.read_yaml(relative))
    except ValidationError as error:
        raise AgentValidationError(agent.id, [f"{relative}: {error}"]) from None


def mapping_problems(mapping: FhirMapping, manifest: AgentManifest, *, held: bool) -> list[str]:
    """What the spec holds a mapping to beyond its shape (`FhirAdapter.for_agent`): its paths
    are paths of the subset (a top-level field's start from a resource type, a list field's from
    its resource, a followed field's from the type it reaches); each `[system=…]` and each code
    set names a system its `code_systems` declares; `medication_period` derives a
    MedicationRequest's only. With `held` (an agent the deployment does not own, or any in
    `mendari validate`), a field tries at most `MAX_PATHS` paths and a code set has at most
    `MAX_CODE_SET_CODES` codes."""
    declared = {use.url for use in manifest.code_systems}
    problems: list[str] = []

    def check(where: str, path: str, *, typed: bool) -> None:
        segments = split_path(path)
        if any(_SEGMENT.match(segment) is None for segment in segments) or (
            typed and not _RESOURCE_TYPE.match(segments[0])
        ):
            start = "a resource type, then " if typed else ""
            problems.append(
                f"{where} {path!r} is not a path of the mapping's subset ({start}dotted element "
                "names, each with at most one [n], [*] or [system=<uri>])"
            )
            return
        for segment in segments:
            match = _SEGMENT.match(segment)
            system = match["system"] if match is not None else None
            if system is not None and system not in declared:
                problems.append(f"{where} {path!r} selects {system!r}, which code_systems lacks")

    if mapping.focus_where is not None:
        check("focus_where.path", mapping.focus_where.path, typed=False)
    for where, kind, mapped in located_fields(mapping):
        if mapped.follow is not None:
            check(f"{where}.follow", mapped.follow, typed=kind is None)
        for path in mapped.tried:
            check(
                f"{where}.{'paths' if mapped.paths else 'path'}",
                path,
                typed=kind is None or mapped.follow is not None,
            )
        if held and len(mapped.tried) > MAX_PATHS:
            problems.append(f"{where}.paths tries {len(mapped.tried)} paths, more than {MAX_PATHS}")
    for name, code_set in mapping.code_sets.items():
        if code_set.system not in declared:
            problems.append(f"code_sets.{name} is of {code_set.system!r}, which code_systems lacks")
        if held and len(code_set.codes) > MAX_CODE_SET_CODES:
            problems.append(
                f"code_sets.{name} has {len(code_set.codes)} codes, more than {MAX_CODE_SET_CODES}"
            )
    for target, spec in mapping.lists.items():
        for kind, source in spec.sources().items():
            where = f"lists.{target}.resources.{kind}" if spec.resources else f"lists.{target}"
            if not _RESOURCE_TYPE.match(kind):
                problems.append(f"{where}: {kind!r} is not a resource type")
            if source.medication_period is not None and kind != "MedicationRequest":
                problems.append(f"{where}.medication_period is a MedicationRequest's only")
    return problems


def looked_for(mapping: FhirMapping) -> dict[str, Any]:
    """What each mapped field and list looks for, from the mapping alone, never the record
    (LP-23g; `FhirAdapter.looked_for`, and the data-coverage record LP-23d builds,
    `data_coverage.measure`):

        {"fields": {<target>: LookedFor},
         "lists": {<target>: {<resource type>: {<item key>: LookedFor}}}}

    Each `LookedFor` holds the full paths in the order they are tried (a list's fields with
    their resource type in front), the Reference followed first if any, the code systems the
    field selects on (its `[system=…]` predicates' and its code set's) and the agent's own
    codes it looks for (its code set's, and its `map`'s keys), both sorted. A
    `medication_period` appears as its two keys, with the paths of its derivation. Targets,
    types and keys are in the mapping's order; nothing comes from a record, so it holds no
    value, id or text of a patient's."""
    lists: dict[str, dict[str, dict[str, LookedFor]]] = {}
    for target, spec in mapping.lists.items():
        lists[target] = {}
        for kind, source in spec.sources().items():
            keys = {
                name: _looked_for(mapped, kind, mapping.code_sets)
                for name, mapped in source.fields.items()
            }
            period = source.medication_period
            if period is not None:
                for key, paths in ((period.start, MEDICATION_START), (period.end, MEDICATION_END)):
                    derived = [f"{kind}.{path}" for path in paths]
                    keys[key] = LookedFor(follow=None, paths=derived, systems=[], codes=[])
            lists[target][kind] = keys
    return {
        "fields": {
            target: _looked_for(mapped, None, mapping.code_sets)
            for target, mapped in mapping.fields.items()
        },
        "lists": lists,
    }


def _looked_for(
    mapped: FieldMapping, kind: str | None, code_sets: Mapping[str, CodeSet]
) -> LookedFor:
    """What one field looks for (`FhirAdapter.looked_for`); `kind` is a list field's type."""
    follow = None if mapped.follow is None else _absolute(kind, mapped.follow)
    paths = [path if follow is not None else _absolute(kind, path) for path in mapped.tried]
    systems = set()
    for path in [*paths, *([follow] if follow else [])]:
        for segment in split_path(path):
            match = _SEGMENT.match(segment)
            if match is not None and match["system"] is not None:
                systems.add(match["system"])
    codes = set(mapped.map or {})
    if mapped.code_set is not None:
        systems.add(code_sets[mapped.code_set].system)
        codes.update(code_sets[mapped.code_set].codes)
    return LookedFor(follow=follow, paths=paths, systems=sorted(systems), codes=sorted(codes))


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
    parts = [int(p) for p in birth_date.split("-")[:3] if p.isascii() and p.isdigit()]
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
    """Every resource in a Bundle, a resource, or a mapping/list of either (flattened). A Bundle
    whose entries are not objects, or an entry whose resource is not one, is not FHIR JSON."""
    for _, resource in iter_entries(value):
        yield resource


def iter_entries(
    value: Any, full_url: str | None = None
) -> Iterator[tuple[str | None, Mapping[str, Any]]]:
    """`iter_resources`, each resource with the `fullUrl` of the Bundle entry that holds it
    (None outside a Bundle, or for an entry without one). Iterative so deeply nested JSON
    never hits Python's recursion limit."""
    stack: list[tuple[Any, str | None]] = [(value, full_url)]
    while stack:
        current, current_url = stack.pop()
        if isinstance(current, list):
            for item in reversed(current):
                stack.append((item, None))
        elif isinstance(current, Mapping):
            if current.get("resourceType") == "Bundle":
                entries = current.get("entry")
                if entries is not None and not isinstance(entries, list):
                    raise FhirStructureError([_wrong_type("Bundle.entry", "an array of objects")])
                children: list[tuple[Any, str | None]] = []
                for index, entry in enumerate(entries or []):
                    where = f"Bundle.entry[{index}]"
                    if not isinstance(entry, Mapping):
                        raise FhirStructureError([_wrong_type(where, "an object")])
                    resource = entry.get("resource")
                    if resource is not None and not isinstance(resource, Mapping):
                        raise FhirStructureError([_wrong_type(f"{where}.resource", "an object")])
                    url = entry.get("fullUrl")
                    children.append((resource, url if isinstance(url, str) else None))
                stack.extend(reversed(children))
            elif "resourceType" in current:
                yield current_url, current
            else:
                for item in reversed(list(current.values())):
                    stack.append((item, None))


def _wrong_type(expression: str, expected: str) -> dict[str, str]:
    element = expression.rsplit(".", 1)[-1].split("[")[0]
    return {
        "code": "structure",
        "expression": expression,
        "diagnostics": f"{element} must be {expected}",
    }


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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
    except ValueError:  # binascii.Error, UnicodeDecodeError, or non-ASCII text
        return None
