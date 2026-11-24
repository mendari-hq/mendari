# SPDX-License-Identifier: Apache-2.0
"""One scope language for API keys and access tokens (ADR-0004).

- **FHIR reads** use SMART v2 resource scopes for what `/fhir` serves: `system/<Type>.<letters>`,
  `r` for read and vread, `s` for search, the letters in SMART's order (`cruds`). `system/*.rs`
  covers every served type. SMART v1 forms map as SMART says: `.read` is `.rs`, `.write` is
  `.cud`, `.*` is `.cruds`.
- **Mendari's own actions** use URI scopes on mendari.co: `DECIDE` for every decision entry point
  (`$decide` included) and `OUTCOME` for recording what happened downstream. `system/Task.c` and
  `.u` would tell a SMART client that `POST /fhir/Task` works; it does not (ADR-0003).
- **Deciding implies discovering** (ADR-0004 as amended, LP-12c): `DECIDE` also grants
  `DISCOVER`, the read-only lists of the agents a credential may decide with (`GET /api/agents`,
  `GET /cds-services`), so one decide-scoped credential finds what it decides with. No other
  read: an agent's detail, examples, datasets, `/openapi.json` and the audit stay closed to a
  scoped credential. No scope URI of its own.

A `Grant` only narrows: a principal's effective permissions are its role's intersected with its
grant's, and a principal with no grant keeps its role's. A scoped principal reaches only the
routes `needed()` names a scope for, and only with that scope; everything else, every other
`/api` route included, is a `403 insufficient_scope`.

Keys parse strictly (`parse`: a scope Mendari does not grant is a `ScopeError` naming it, a
`422`); tokens leniently (`parse_lenient`, then `Grant.narrow` to what the client registered:
writes, granular `?param=` scopes and anything not served are dropped, as SMART allows).

`resource_scope` reads the same grammar in every SMART context (patient/, user/, system/), for
the scopes an EHR publishes and a launch asks it for (`mendari smart check`); it grants nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from mendari.errors import ScopeError

DECIDE = "https://mendari.co/fhir/scope/decide"
OUTCOME = "https://mendari.co/fhir/scope/outcome"
# Mendari's URI scopes and the permission each grants.
ACTIONS: dict[str, str] = {DECIDE: "decide", OUTCOME: "outcome"}
READ = "read"  # the permission every FHIR read scope grants
DISCOVER = "discover"  # the permission DECIDE implies: the lists of packs to decide with
# The FHIR types `/fhir` serves to read (`r`, read and vread) and search (`s`). No NamingSystem
# search is served, so `system/NamingSystem.rs` is not granted.
SERVED: dict[str, str] = {
    "Task": "rs",
    "AuditEvent": "rs",
    "Provenance": "rs",
    "CodeSystem": "rs",
    "NamingSystem": "r",
}
_LETTERS = "cruds"  # SMART v2's order
_V1 = {"read": "rs", "write": "cud", "*": "cruds"}
_RESOURCE = re.compile(r"(?P<context>[a-z]+)/(?P<type>\*|[A-Z][A-Za-z]{0,63})\.(?P<letters>[^?]+)")
_V2_LETTERS = re.compile(r"c?r?u?d?s?")
_CONTEXTS = ("patient", "user", "system")  # SMART's, for an EHR's scopes (`resource_scope`)
_SHOWN = 100  # characters of a refused scope a message repeats


@dataclass(frozen=True)
class Grant:
    """What a credential's scopes allow: FHIR (type, letter) pairs and Mendari's actions.
    `scope` is the scope text as granted (space-separated; what is stored and shown)."""

    fhir: frozenset[tuple[str, str]]
    actions: frozenset[str]
    scope: str

    @property
    def permissions(self) -> frozenset[str]:
        """The Mendari permissions the scopes stand for (intersected with a role's); `DECIDE`
        brings `DISCOVER` with it."""
        read = {READ} if self.fhir else set()
        discover = {DISCOVER} if DECIDE in self.actions else set()
        return frozenset(read | discover | {ACTIONS[uri] for uri in self.actions})

    def covers(self, needed: str) -> bool:
        """Whether this grant includes the scope a route needs (from `needed()`)."""
        if needed in ACTIONS:
            return needed in self.actions
        context, _, rest = needed.partition("/")
        kind, _, letter = rest.partition(".")
        return context == "system" and (kind, letter) in self.fhir

    def narrow(self, registered: Grant) -> Grant:
        """What a token gets: what it asked for, within what its client registered."""
        return _grant(self.fhir & registered.fhir, self.actions & registered.actions)


def parse(scope: str, *, permissions: Iterable[str] | None = None) -> Grant:
    """A key's scopes, strictly: every scope must be one Mendari grants, in full, and (given the
    role's `permissions`) one the role can use; otherwise a `ScopeError` naming the first that is
    not. The grant keeps the scopes as given (deduplicated and sorted)."""
    names = scope.split()
    if not names:
        raise ScopeError("name at least one scope, or leave the scope out for the role's own")
    allowed = None if permissions is None else frozenset(permissions)
    fhir: set[tuple[str, str]] = set()
    actions: set[str] = set()
    for name in names:
        shown = name[:_SHOWN]
        if name in ACTIONS:
            permission = ACTIONS[name]
            actions.add(name)
        else:
            pairs, refused = _resource(name)
            if refused:
                raise ScopeError(f"scope {shown!r} is not granted here: {refused}")
            permission = READ
            fhir |= pairs
        if allowed is not None and permission not in allowed:
            reason = f"it needs the {permission!r} permission, which the role does not have"
            raise ScopeError(f"scope {shown!r} is not granted here: {reason}")
    return Grant(frozenset(fhir), frozenset(actions), " ".join(sorted(set(names))))


def parse_lenient(scope: str) -> Grant:
    """A token request's scopes, leniently: what Mendari can grant of each, nothing for the rest
    (writes, granular `?param=` and patient or user scopes, unknown names). The grant's text is
    the canonical form of what is left."""
    fhir: set[tuple[str, str]] = set()
    actions: set[str] = set()
    for name in scope.split():
        if name in ACTIONS:
            actions.add(name)
            continue
        pairs, _ = _resource(name, lenient=True)
        fhir |= pairs
    return _grant(frozenset(fhir), frozenset(actions))


@dataclass(frozen=True)
class ResourceScope:
    """A SMART resource scope in any context, v1 or v2, as permissions: what an EHR publishes
    or a launch asks it for (`mendari smart check`), not what Mendari grants."""

    context: str  # patient, user or system
    type: str  # a FHIR resource type, or * for every type
    letters: frozenset[str]  # some of c, r, u, d, s
    query: str  # a granular scope's ?param=value..., else ""


def resource_scope(name: str) -> ResourceScope | None:
    """`name` as a SMART resource scope (`patient/Observation.rs`, `user/*.read`,
    `patient/Observation.rs?category=laboratory`), with SMART v1's `.read` as `rs`, `.write` as
    `cud` and `.*` as all five letters; None for any other scope (`launch`, `openid`) and for a
    malformed one (v2 letters out of SMART's order `cruds`)."""
    base, mark, query = name.partition("?")
    found = _RESOURCE.fullmatch(base)
    if found is None or found["context"] not in _CONTEXTS or (mark and not query):
        return None
    letters = _V1.get(found["letters"], found["letters"])
    if not letters or not _V2_LETTERS.fullmatch(letters):
        return None
    return ResourceScope(found["context"], found["type"], frozenset(letters), query)


def _grant(fhir: frozenset[tuple[str, str]], actions: frozenset[str]) -> Grant:
    """A grant whose text is canonical: one SMART v2 scope per type, then the URIs."""
    by_type: dict[str, set[str]] = {}
    for kind, letter in fhir:
        by_type.setdefault(kind, set()).add(letter)
    names = [
        f"system/{kind}.{''.join(c for c in _LETTERS if c in letters)}"
        for kind, letters in sorted(by_type.items())
    ]
    return Grant(fhir, actions, " ".join([*names, *sorted(actions)]))


def _resource(name: str, *, lenient: bool = False) -> tuple[set[tuple[str, str]], str]:
    """The (type, letter) pairs a SMART resource scope grants, and why it is not granted in full
    (empty when it is). Leniently, the pairs Mendari serves of it, never a reason."""
    if "?" in name:
        return set(), "granular scopes (?param=) are not granted"
    found = _RESOURCE.fullmatch(name)
    if found is None:
        return set(), "not a scope Mendari knows"
    if found["context"] != "system":
        return set(), "only system/ scopes are granted (Mendari has no patient or user context)"
    letters = _V1.get(found["letters"], found["letters"])
    if not letters or not _V2_LETTERS.fullmatch(letters):
        return set(), "the letters must be some of c, r, u, d, s, in that order"
    kind = found["type"]
    if kind != "*" and kind not in SERVED:
        return set(), f"Mendari serves no {kind} resources"
    if kind == "*":
        pairs = {(t, c) for t, served in SERVED.items() for c in letters if c in served}
        unserved = "".join(c for c in letters if not any(c in s for s in SERVED.values()))
    else:
        pairs = {(kind, c) for c in letters if c in SERVED[kind]}
        unserved = "".join(c for c in letters if c not in SERVED[kind])
    if unserved and not lenient:
        served = SERVED.get(kind, "rs")
        return pairs, f"Mendari serves only {served!r} (r read, s search) of {kind} resources"
    return pairs, ""


# -- the scope each route needs ----------------------------------------------------------------
# (method, path pattern, scope) -> first match wins. A FHIR pattern names the type in a group
# and the scope is a letter; Mendari's own are URIs. Every other route is out of reach of a
# scoped principal (public ones aside: they need no credential at all).

_ROUTE_SCOPES: list[tuple[str, re.Pattern[str], str]] = [
    (method, re.compile(pattern), scope)
    for method, pattern, scope in [
        # Read and vread: an id never starts with `_` or `$` (`_search`, `$decide`).
        ("GET", r"^/fhir/(?P<type>[A-Za-z]+)/[^/_$][^/]*(/_history/[^/]+)?$", "r"),
        ("GET", r"^/fhir/(?P<type>[A-Za-z]+)$", "s"),
        ("POST", r"^/fhir/(?P<type>[A-Za-z]+)/_search$", "s"),
        ("POST", r"^/api/agents/[^/]+/[^/]+/(decide|guard|fhir/decide|pas|actions)$", DECIDE),
        ("POST", r"^/cds-services/[^/]+$", DECIDE),
        ("POST", r"^/cds-services/[^/]+/feedback$", DECIDE),  # a card's feedback (LP-23f)
        ("POST", r"^/fhir/\$decide$", DECIDE),
        ("POST", r"^/fhir/Questionnaire/\$questionnaire-package$", DECIDE),  # Da Vinci DTR
        ("POST", r"^/api/audit/[^/]+/outcome$", OUTCOME),
        # The lists of agents a credential may decide with, which deciding needs (`DISCOVER`).
        ("GET", r"^/api/agents$", DECIDE),
        ("GET", r"^/cds-services$", DECIDE),
    ]
]


def needed(method: str, path: str) -> str | None:
    """The scope a request needs, or None: no scope reaches it."""
    if method == "HEAD":
        method = "GET"
    for rule_method, pattern, scope in _ROUTE_SCOPES:
        found = pattern.match(path) if rule_method == method else None
        if found is None:
            continue
        if scope in ACTIONS:
            return scope
        kind = found["type"]
        return f"system/{kind}.{scope}" if scope in SERVED.get(kind, "") else None
    return None


def supported() -> list[str]:
    """Every scope Mendari grants, in SMART v2 form (`scopes_supported` in discovery)."""
    return [
        *(f"system/{kind}.{letters}" for kind, letters in SERVED.items()),
        "system/*.rs",
        *ACTIONS,
    ]
