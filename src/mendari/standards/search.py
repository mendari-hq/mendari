# SPDX-License-Identifier: Apache-2.0
"""FHIR R4 search, the parts that know nothing of storage or HTTP: what each type can be searched
by, the parser, date and token semantics, paging cursors and the `searchset` Bundle.

One table, `SPECS`, says which parameters each searchable type supports, with the R4
SearchParameter each one is (a test checks every canonical against the R4 core package). It drives
the parser, the store mapping (`fhir_search.py`), the CapabilityStatement and the tests, so what
Mendari says it supports and what it does cannot drift apart.

**Unknown and unsupported parameters (hybrid strictness).** R4 says a server SHOULD ignore
parameters it does not support. Ignoring a filter the client meant, though, silently returns more
than it asked for: on an audit trail, every organization member's decisions instead of one
patient's. So:

- a parameter R4 does not define for the type is ignored, and an `OperationOutcome` entry
  (`search.mode = outcome`, severity warning) names it, never its value;
- a standard R4 parameter of the type that Mendari does not support (`patient`, `subject`,
  `focus`, `owner`, `agent`, `_sort`, `_include` …) is a `400`, unless `Prefer: handling=lenient`,
  which ignores it with the same warning entry;
- an unsupported modifier or chain is always a `400` (R4: SHALL reject);
- `Prefer: handling=strict` makes an unknown parameter a `400` too.

A value that does not parse is always a `400`. An empty value is ignored (it filters nothing).
Errors and warnings name parameters, never values: values reach logs.

**Dates are UTC.** Stored instants carry an offset, and a remote client cannot know the
deployment's zone, so a date without a time is the UTC day, and a time needs a zone (`Z` or
`±hh:mm`): without one it is a `400`. The value's precision makes a half-open range
`[low, high)`; a stored instant is a point in it or not. Prefixes `eq ne gt lt ge le sa eb`;
`ap` is a `400`.

**Tokens** match exactly and case-sensitively: `code`, `system|code`, `|code` (no system: Mendari
always sets one, so it matches nothing) and `system|` (every code of the system). A comma
separates values that are ORed; a repeated parameter is ANDed; `\\` escapes `,`, `|`, `$`, `\\`.

**Paging** is keyset paging, stateless (serverless instances share no memory): the `next` link
repeats the criteria with `cursor`, the opaque key of the last entry. A cursor is re-validated on
every page and only narrows a query its holder could run anyway; tenancy is applied again on every
page, so another organization replaying it sees only its own rows. `_count` defaults to 50 and is
clamped to 500. `Bundle.total` is given only for `_total=accurate`, `_summary=count` or
`_count=0` (the last two return the total and no entries).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode

from mendari.errors import FhirRequestError
from mendari.standards.audit_fhir import EVENT_SYSTEM
from mendari.standards.fhir import (
    DECISION_ID_SYSTEM,
    OUTCOME_SYSTEM,
    PACK_TAG_SYSTEM,
    narrative,
)

ParamType = Literal["token", "date", "reference", "uri"]
Handling = Literal["strict", "lenient"]
_SPEC = "http://hl7.org/fhir/SearchParameter"
TASK_STATUS_SYSTEM = "http://hl7.org/fhir/task-status"
PRIORITY_SYSTEM = "http://hl7.org/fhir/request-priority"

DEFAULT_COUNT = 50
MAX_COUNT = 500
# Bounds on what one search asks the database to do: values ORed in one parameter, and repeats
# of one parameter (ANDed). Beyond them a search is a 400 (too-costly), not a database error.
MAX_VALUES = 100
MAX_REPEATS = 20
CURSOR = "cursor"  # Mendari's paging parameter: reserved, not an R4 name
# Presentation, not criteria: every FHIR interaction takes them, so they are never "ignored".
_PRESENTATION = frozenset({"_format", "_pretty"})
# R4 result and special parameters Mendari does not support (search.html 3.1.1.5, 3.1.1.6).
_UNSUPPORTED_RESULT = frozenset(
    {"_sort", "_include", "_revinclude", "_elements", "_contained", "_containedType", "_has"}
    | {"_list", "_type"}
)
# Every R4 SearchParameter whose base is Resource or DomainResource: they apply to every type
# here (tests/fixtures/fhir/r4-search-parameters.json, from the R4 core package).
R4_COMMON = frozenset(
    {"_content", "_filter", "_id", "_lastUpdated", "_profile", "_query", "_security"}
    | {"_source", "_tag", "_text"}
)
# The R4 SearchParameters defined on each type itself (the same fixture).
R4_PARAMETERS: dict[str, frozenset[str]] = {
    "Task": frozenset(
        {"authored-on", "based-on", "business-status", "code", "encounter", "focus"}
        | {"group-identifier", "identifier", "intent", "modified", "owner", "part-of"}
        | {"patient", "performer", "period", "priority", "requester", "status", "subject"}
    ),
    "AuditEvent": frozenset(
        {"action", "address", "agent", "agent-name", "agent-role", "altid", "date", "entity"}
        | {"entity-name", "entity-role", "entity-type", "outcome", "patient", "policy", "site"}
        | {"source", "subtype", "type"}
    ),
    "Provenance": frozenset(
        {"agent", "agent-role", "agent-type", "entity", "location", "patient", "recorded"}
        | {"signature-type", "target", "when"}
    ),
    "CodeSystem": frozenset(
        {"code", "content-mode", "context", "context-quantity", "context-type"}
        | {"context-type-quantity", "context-type-value", "date", "description", "identifier"}
        | {"jurisdiction", "language", "name", "publisher", "status", "supplements", "system"}
        | {"title", "url", "version"}
    ),
}

_DATES = (
    "UTC: a date without a time is that UTC day, and a time needs a zone (Z or ±hh:mm). "
    "Prefixes eq, ne, gt, lt, ge, le, sa and eb; ap is not supported."
)


@dataclass(frozen=True)
class Param:
    """A supported search parameter: its name, type, the R4 SearchParameter it is, what it
    searches here, and the modifiers it takes (none unless listed)."""

    name: str
    type: ParamType
    definition: str
    documentation: str
    modifiers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ResourceSearch:
    """How one type is searched: its parameters, and the fields of its paging key (`id`: a FHIR
    id; `instant`: a stored timestamp; `seq`: a chain position)."""

    resource: str
    params: tuple[Param, ...]
    key: tuple[tuple[str, Literal["id", "instant", "seq"]], ...]

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)

    @property
    def unsupported(self) -> frozenset[str]:
        """The standard R4 parameters of this type that Mendari does not support."""
        names = {p.name for p in self.params}
        return (R4_PARAMETERS[self.resource] | R4_COMMON | _UNSUPPORTED_RESULT) - names


def _p(name: str, kind: ParamType, definition: str, documentation: str, *modifiers: str) -> Param:
    return Param(name, kind, f"{_SPEC}/{definition}", documentation, frozenset(modifiers))


_ENTITY = (
    "The decision the event is about: Task/{id}, {id} or the Task's absolute URL; with "
    f":identifier, {DECISION_ID_SYSTEM}|{{id}}."
)
SPECS: dict[str, ResourceSearch] = {
    spec.resource: spec
    for spec in (
        ResourceSearch(
            "Task",
            (
                _p("_id", "token", "Resource-id", "The decision id."),
                _p("identifier", "token", "Task-identifier", f"{DECISION_ID_SYSTEM}|{{id}}."),
                _p(
                    "status",
                    "token",
                    "Task-status",
                    "requested (waiting for a reviewer in Mendari), completed or failed "
                    "(the input was refused).",
                ),
                _p("business-status", "token", "Task-business-status", f"{OUTCOME_SYSTEM}."),
                _p("priority", "token", "Task-priority", "routine, urgent, asap or stat."),
                _p("authored-on", "date", "Task-authored-on", f"When it was decided. {_DATES}"),
                _p(
                    "_lastUpdated",
                    "date",
                    "Resource-lastUpdated",
                    f"When its latest version was made. {_DATES}",
                ),
                _p("_tag", "token", "Resource-tag", f"{PACK_TAG_SYSTEM}|{{pack}}@{{version}}."),
            ),
            (("t", "instant"), ("id", "id")),
        ),
        ResourceSearch(
            "AuditEvent",
            (
                _p("_id", "token", "Resource-id", "{decision id}-{chain position}."),
                _p("date", "date", "AuditEvent-date", f"When it was recorded. {_DATES}"),
                _p("subtype", "token", "AuditEvent-subtype", f"{EVENT_SYSTEM}."),
                _p("entity", "reference", "AuditEvent-entity", _ENTITY, "identifier"),
                _p("_lastUpdated", "date", "Resource-lastUpdated", f"As date. {_DATES}"),
            ),
            (("seq", "seq"),),
        ),
        ResourceSearch(
            "Provenance",
            (
                _p("_id", "token", "Resource-id", "{decision id}-p{chain position}."),
                _p("recorded", "date", "Provenance-recorded", f"When it was recorded. {_DATES}"),
                _p("target", "reference", "Provenance-target", _ENTITY, "identifier"),
                _p("_lastUpdated", "date", "Resource-lastUpdated", f"As recorded. {_DATES}"),
            ),
            (("seq", "seq"),),
        ),
        ResourceSearch(
            "CodeSystem",
            (
                _p("_id", "token", "Resource-id", "The CodeSystem's id."),
                _p("url", "uri", "conformance-url", "The canonical url; url|version also."),
                _p("version", "token", "conformance-version", "The business version."),
            ),
            (("id", "id"),),
        ),
    )
}


# -- parsed values ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    """`system` None: any system; "" no system (`|code`). `code` None: any code (`system|`)."""

    system: str | None
    code: str | None


@dataclass(frozen=True)
class DateRange:
    """A date value at its precision: the half-open UTC range `[low, high)`, with its prefix."""

    prefix: str
    low: datetime
    high: datetime


@dataclass(frozen=True)
class Reference:
    """`Task/{id}`, a bare `{id}` (`type` None), or an absolute URL (`base` set)."""

    base: str | None
    type: str | None
    id: str


@dataclass(frozen=True)
class Uri:
    url: str
    version: str | None


Value = Token | DateRange | Reference | Uri


@dataclass(frozen=True)
class Clause:
    """One parameter: its values are ORed. The clauses of a search are ANDed."""

    name: str
    modifier: str | None
    values: tuple[Value, ...]


@dataclass
class Search:
    resource: str
    clauses: list[Clause] = field(default_factory=list)
    count: int = DEFAULT_COUNT
    summary_count: bool = False  # the total and no entries
    total: bool = False  # Bundle.total asked for (_total=accurate)
    cursor: dict[str, Any] | None = None  # the paging key to continue after
    used: list[tuple[str, str]] = field(default_factory=list)  # for the links, as sent
    ignored: list[str] = field(default_factory=list)  # parameter names only, never values

    @property
    def counted(self) -> bool:
        """Whether the Bundle carries a total."""
        return self.total or self.summary_count


class SearchError(FhirRequestError):
    """A search that cannot run: a `400` OperationOutcome of `issues` ({code, diagnostics}),
    which name parameters, never their values."""

    def __init__(self, issues: list[dict[str, str]]):
        self.issues = issues
        Exception.__init__(self, "; ".join(issue["diagnostics"] for issue in issues))


# -- the parser ------------------------------------------------------------------------------


def handling(prefer: str | None) -> Handling | None:
    """The `handling` preference of a `Prefer` header (RFC 7240), if it is strict or lenient."""
    for part in re.split(r"[,;]", prefer or ""):
        name, _, value = part.partition("=")
        setting = value.strip().strip('"').lower()
        if name.strip().lower() == "handling" and setting in ("strict", "lenient"):
            return "strict" if setting == "strict" else "lenient"
    return None


def parse(
    spec: ResourceSearch, items: Iterable[tuple[str, str]], prefer: Handling | None = None
) -> Search:
    """A search from its parameters, in order (for POST `_search`, the URL's then the body's),
    under the client's `Prefer: handling`. Raises `SearchError` with every problem found."""
    search = Search(spec.resource)
    problems: list[dict[str, str]] = []
    results: set[str] = set()
    for raw_name, raw_value in items:
        name, _, modifier = raw_name.partition(":")
        if raw_value == "" or name in _PRESENTATION:
            continue
        if name in _RESULTS:
            if name in results:
                problems.append(_issue("invalid", f"{name} may appear only once"))
            elif modifier:
                problems.append(_issue("not-supported", f"{name} takes no modifier"))
            else:
                results.add(name)
                problems += _RESULTS[name](spec, search, raw_value, prefer)
            continue
        problems += _criterion(spec, search, raw_name, raw_value, prefer)
    if problems:
        raise SearchError(problems)
    return search


def _criterion(
    spec: ResourceSearch, search: Search, raw_name: str, raw_value: str, prefer: Handling | None
) -> list[dict[str, str]]:
    name, _, modifier = raw_name.partition(":")
    base_name = name.split(".", 1)[0]
    param = spec.param(base_name)
    if param is None:
        standard = base_name in spec.unsupported
        if prefer == "strict" or (standard and prefer != "lenient"):
            why = (
                "is not supported on {} (Prefer: handling=lenient ignores it)"
                if standard
                else "is not a search parameter of {}"
            )
            return [_issue("not-supported", f"{_shown(base_name)} {why.format(spec.resource)}")]
        search.ignored.append(_shown(raw_name))
        return []
    if "." in name:  # search.html 3.1.1.4.4: an unsupported modifier (or chain) SHALL be refused
        return [_issue("not-supported", f"{param.name}: chained parameters are not supported")]
    if modifier and modifier not in param.modifiers:
        return [_issue("not-supported", f"{param.name}: :{_shown(modifier)} is not supported")]
    try:
        values = tuple(_values(param, modifier or None, raw_value))
    except ValueError as error:
        return [_issue("value", f"{param.name}: {error}")]
    if len(values) > MAX_VALUES:
        return [_issue("too-costly", f"{param.name}: at most {MAX_VALUES} values in one list")]
    if sum(clause.name == param.name for clause in search.clauses) >= MAX_REPEATS:
        return [_issue("too-costly", f"{param.name}: at most {MAX_REPEATS} repeats")]
    search.clauses.append(Clause(param.name, modifier or None, values))
    search.used.append((raw_name, raw_value))
    return []


def _count(spec: ResourceSearch, search: Search, value: str, _: Handling | None) -> list[Any]:
    if not re.fullmatch(r"\d{1,9}", value):
        return [_issue("value", "_count must be a whole number, 0 or more")]
    search.count = min(int(value), MAX_COUNT)  # SHALL NOT return more; may return fewer
    search.summary_count = search.summary_count or search.count == 0
    return []


def _total(spec: ResourceSearch, search: Search, value: str, _: Handling | None) -> list[Any]:
    if value not in ("none", "estimate", "accurate"):
        return [_issue("value", "_total must be none, estimate or accurate")]
    search.total = value == "accurate"  # an estimate is allowed to be none at all
    search.used.append(("_total", value))
    return []


def _summary(
    spec: ResourceSearch, search: Search, value: str, prefer: Handling | None
) -> list[Any]:
    if value in ("count", "false"):
        search.summary_count = search.summary_count or value == "count"
        search.used.append(("_summary", value))
        return []
    if prefer == "lenient":
        search.ignored.append("_summary")
        return []
    return [_issue("not-supported", "_summary: only count and false are supported")]


def _cursor(spec: ResourceSearch, search: Search, value: str, _: Handling | None) -> list[Any]:
    try:
        search.cursor = decode_cursor(spec, value)
    except ValueError:
        return [_issue("value", f"{CURSOR}: not a valid paging link; start the search again")]
    return []


_RESULTS = {"_count": _count, "_total": _total, "_summary": _summary, CURSOR: _cursor}


def _issue(code: str, diagnostics: str) -> dict[str, str]:
    return {"code": code, "diagnostics": diagnostics}


def _shown(name: str) -> str:
    """A parameter name as diagnostics show it: the client's text, so kept short."""
    return name if len(name) <= 64 else name[:61] + "..."


# -- values ----------------------------------------------------------------------------------

_ID = re.compile(r"[A-Za-z0-9\-.]{1,64}")
_TYPE = re.compile(r"[A-Z][A-Za-z]{1,63}")


def _values(param: Param, modifier: str | None, raw: str) -> Iterator[Value]:
    for part in _split(raw, ","):
        if part == "":
            raise ValueError("an empty value in a list")
        if param.type == "token" or modifier == "identifier":
            yield token(part)
        elif param.type == "date":
            yield parse_date(_unescape(part))
        elif param.type == "uri":
            url, *version = _split(part, "|", once=True)
            yield Uri(_unescape(url), _unescape(version[0]) if version and version[0] else None)
        else:
            yield reference(_unescape(part))


def _split(text: str, separator: str, *, once: bool = False) -> list[str]:
    """`text` cut at each `separator` not escaped by a backslash (`\\,` is a comma in a value),
    or at the first only. The pieces keep their escapes, for the next cut and `_unescape`."""
    parts: list[str] = []
    current, escaped = "", False
    for char in text:
        if escaped:
            current, escaped = current + char, False
        elif char == "\\":
            current, escaped = current + char, True
        elif char == separator and not (once and parts):
            parts.append(current)
            current = ""
        else:
            current += char
    return [*parts, current]


def _unescape(text: str) -> str:
    return re.sub(r"\\([\\,|$])", r"\1", text)


def token(text: str) -> Token:
    """`code`, `system|code`, `|code` or `system|` (as cut from a list, escapes kept)."""
    parts = _split(text, "|", once=True)
    if len(parts) == 1:
        return Token(None, _unescape(text))
    system, code = (_unescape(p) for p in parts)
    return Token(system, code or None)


def reference(text: str) -> Reference:
    """`[type]/[id]`, a bare `[id]`, or an absolute `[base]/[type]/[id]`. A version-specific
    reference (`.../_history/[vid]`) is not supported."""
    if "/_history/" in text:
        raise ValueError("version-specific references are not supported")
    absolute = re.match(r"https?://[^/]+", text)
    base: str | None = None
    if absolute:
        head, _, ident = text.rpartition("/")
        base, _, kind = head.rpartition("/")
        if len(base) < absolute.end() or not kind:
            raise ValueError("an absolute reference is [base]/[type]/[id]")
    else:
        kind, slash, ident = text.rpartition("/")
        if slash and not kind:
            raise ValueError("a reference is [type]/[id], [id] or an absolute URL")
    if not _ID.fullmatch(ident) or (kind and not _TYPE.fullmatch(kind)):
        raise ValueError("a reference is [type]/[id], [id] or an absolute URL")
    return Reference(base, kind or None, ident)


_DATE = re.compile(
    r"(?P<prefix>eq|ne|gt|lt|ge|le|sa|eb|ap)?"
    r"(?P<y>\d{4})(?:-(?P<m>\d{2})(?:-(?P<d>\d{2})"
    r"(?:T(?P<H>\d{2}):(?P<M>\d{2})(?::(?P<S>\d{2})(?:\.(?P<f>\d{1,6}))?)?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})?)?)?)?"
)


def parse_date(text: str) -> DateRange:
    """A date search value as a UTC range at its precision: a year, month or day is that UTC
    period; a time needs a zone and is precise to its last digit."""
    match = _DATE.fullmatch(text)
    if match is None:
        raise ValueError("not a FHIR date (yyyy[-mm[-dd[Thh:mm[:ss[.ffffff]]{Z|±hh:mm}]]])")
    g = match.groupdict()
    prefix = g["prefix"] or "eq"
    if prefix == "ap":
        raise ValueError("the ap prefix is not supported")
    if g["H"] is not None and g["tz"] is None:
        raise ValueError("a time needs a zone (Z or ±hh:mm): dates here are UTC")
    try:
        low = datetime(
            int(g["y"]),
            int(g["m"] or 1),
            int(g["d"] or 1),
            int(g["H"] or 0),
            int(g["M"] or 0),
            int(g["S"] or 0),
            int((g["f"] or "").ljust(6, "0") or 0),
            tzinfo=_zone(g["tz"]),
        )
        high = _after(low, g)
        return DateRange(prefix, low.astimezone(UTC), high.astimezone(UTC))
    except (ValueError, OverflowError):
        raise ValueError("not a valid date") from None


def _zone(text: str | None) -> timezone:
    if text in (None, "Z"):
        return UTC
    hours, minutes = int(text[1:3]), int(text[4:6])
    if hours > 14 or minutes > 59:
        raise ValueError("not a valid zone")
    offset = timedelta(hours=hours, minutes=minutes)
    return timezone(offset if text[0] == "+" else -offset)


def _after(low: datetime, g: Mapping[str, str | None]) -> datetime:
    """The end of the value's precision: the next year, month, day, minute, second or fraction."""
    if g["f"]:
        return low + timedelta(microseconds=10 ** (6 - len(g["f"])))
    if g["S"]:
        return low + timedelta(seconds=1)
    if g["H"]:
        return low + timedelta(minutes=1)
    if g["d"]:
        return low + timedelta(days=1)
    if g["m"]:
        return low.replace(year=low.year + (low.month == 12), month=low.month % 12 + 1)
    return low.replace(year=low.year + 1)


def date_matches(instant: datetime, value: DateRange) -> bool:
    """Whether a stored instant (a point) matches a date value: the semantics the SQL bounds
    (`bounds`) implement, and what the tests check them against."""
    return {
        "eq": value.low <= instant < value.high,
        "ne": not value.low <= instant < value.high,
        "gt": instant >= value.high,
        "sa": instant >= value.high,
        "ge": instant >= value.low,
        "lt": instant < value.low,
        "eb": instant < value.low,
        "le": instant < value.high,
    }[value.prefix]


def bounds(value: DateRange) -> tuple[str | None, str | None, bool]:
    """A date value as text bounds on a stored timestamp: `(low, high, outside)`, meaning
    `low <= t < high` (a None side is open), or its complement when `outside`.

    Stored timestamps are UTC ISO-8601 to the millisecond (`2026-10-01T14:00:00.004+00:00`), so
    comparing text compares instants. A bound finer than a millisecond rounds up to the next
    one, which keeps `>=` and `<` exact for millisecond values."""
    low, high = stored(value.low), stored(value.high)
    return {
        "eq": (low, high, False),
        "ne": (low, high, True),
        "gt": (high, None, False),
        "sa": (high, None, False),
        "ge": (low, None, False),
        "lt": (None, low, False),
        "eb": (None, low, False),
        "le": (None, high, False),
    }[value.prefix]


def stored(instant: datetime) -> str:
    """An instant in the stored format, rounded up to the millisecond."""
    utc = instant.astimezone(UTC)
    extra = utc.microsecond % 1000
    if extra:
        utc += timedelta(microseconds=1000 - extra)
    return utc.isoformat(timespec="milliseconds")


# -- paging ----------------------------------------------------------------------------------

# A stored timestamp (written to the millisecond, UTC; looser, so older rows still page).
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})")


def encode_cursor(key: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(key), separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(spec: ResourceSearch, text: str) -> dict[str, Any]:
    """A cursor back into the paging key of `spec`'s type, or ValueError: its fields, and
    only those, each in its expected form (a cursor is client input like any other)."""
    try:
        key = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ValueError("not a cursor") from None
    if not isinstance(key, dict) or set(key) != {name for name, _ in spec.key}:
        raise ValueError("not a cursor for this type")
    for name, kind in spec.key:
        value = key[name]
        valid = {
            "id": lambda v: isinstance(v, str) and _ID.fullmatch(v) is not None,
            "instant": lambda v: isinstance(v, str) and _INSTANT.fullmatch(v) is not None,
            "seq": lambda v: type(v) is int and 0 <= v < 2**63,
        }[kind](value)
        if not valid:
            raise ValueError("not a cursor for this type")
    return key


# -- the Bundle ------------------------------------------------------------------------------


def searchset(
    *,
    base: str,
    search: Search,
    resources: Sequence[Mapping[str, Any]],
    total: int | None,
    next_key: Mapping[str, Any] | None,
    timestamp: str,
) -> dict[str, Any]:
    """The `searchset` Bundle for one page: absolute full URLs and links under the FHIR `base`;
    `self` with the parameters used (as a GET, also for POST `_search`), `_count` as applied and
    the cursor; `next` when there is more. Ignored parameters get one warning outcome entry."""
    url = f"{base.rstrip('/')}/{search.resource}"
    criteria = [*search.used, ("_count", str(search.count))]
    here = [] if search.cursor is None else [(CURSOR, encode_cursor(search.cursor))]
    links = [{"relation": "self", "url": _link(url, criteria + here)}]
    if next_key is not None and not search.summary_count:
        following = [*criteria, (CURSOR, encode_cursor(next_key))]
        links.append({"relation": "next", "url": _link(url, following)})
    bundle: dict[str, Any] = {"resourceType": "Bundle", "type": "searchset", "timestamp": timestamp}
    if search.counted and total is not None:
        bundle["total"] = total
    bundle["link"] = links
    entries = [
        {
            "fullUrl": f"{base.rstrip('/')}/{r['resourceType']}/{r['id']}",
            "resource": r,
            "search": {"mode": "match"},
        }
        for r in ([] if search.summary_count else resources)
    ]
    if search.ignored:
        entries.append(_ignored(search))
    if entries:
        bundle["entry"] = entries
    return bundle


def _ignored(search: Search) -> dict[str, Any]:
    """The warning entry for ignored parameters (R4: search.mode outcome, never an error)."""
    names = ", ".join(sorted(set(search.ignored)))
    diagnostics = (
        f"ignored, not supported on {search.resource}: {names}. "
        "Send Prefer: handling=strict to make this an error."
    )
    outcome = {
        "resourceType": "OperationOutcome",
        "text": narrative(diagnostics),
        "issue": [{"severity": "warning", "code": "not-supported", "diagnostics": diagnostics}],
    }
    digest = hashlib.sha256(f"{search.resource}|{names}".encode()).hexdigest()[:32]
    return {
        "fullUrl": f"urn:uuid:{uuid.UUID(digest)}",
        "resource": outcome,
        "search": {"mode": "outcome"},
    }


def _link(url: str, items: Sequence[tuple[str, str]]) -> str:
    """`,` `:` and `/` are legal in a query (RFC 3986) and keep the link readable; `|`, `\\`,
    spaces and the rest are percent-encoded."""
    return f"{url}?{urlencode(list(items), safe=',:/')}" if items else url


def search_params(spec: ResourceSearch) -> list[dict[str, str]]:
    """The type's `searchParam` entries for the CapabilityStatement."""
    return [
        {
            "name": p.name,
            "definition": p.definition,
            "type": p.type,
            "documentation": p.documentation,
        }
        for p in spec.params
    ]
