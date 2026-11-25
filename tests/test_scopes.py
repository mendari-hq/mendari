# SPDX-License-Identifier: Apache-2.0
"""The scope language for keys and tokens (server/scopes.py, ADR-0004)."""

from __future__ import annotations

import pytest

from mendari.errors import ScopeError
from mendari.server import scopes
from mendari.server.auth import PERMISSIONS
from mendari.server.scopes import DECIDE, OUTCOME, Grant, needed, parse, parse_lenient

EVERY_READ = {
    ("Task", "r"),
    ("Task", "s"),
    ("AuditEvent", "r"),
    ("AuditEvent", "s"),
    ("Provenance", "r"),
    ("Provenance", "s"),
    ("CodeSystem", "r"),
    ("CodeSystem", "s"),
    ("NamingSystem", "r"),
}


@pytest.mark.parametrize(
    ("scope", "fhir", "actions"),
    [
        ("system/Task.rs", {("Task", "r"), ("Task", "s")}, set()),
        ("system/Task.r", {("Task", "r")}, set()),
        ("system/AuditEvent.s", {("AuditEvent", "s")}, set()),
        ("system/NamingSystem.r", {("NamingSystem", "r")}, set()),
        ("system/*.rs", EVERY_READ, set()),
        ("system/*.r", {pair for pair in EVERY_READ if pair[1] == "r"}, set()),
        ("system/*.s", {pair for pair in EVERY_READ if pair[1] == "s"}, set()),
        # SMART v1: .read is .rs.
        ("system/Provenance.read", {("Provenance", "r"), ("Provenance", "s")}, set()),
        ("system/*.read", EVERY_READ, set()),
        (DECIDE, set(), {DECIDE}),
        (f"{OUTCOME}  {DECIDE} system/Task.r", {("Task", "r")}, {DECIDE, OUTCOME}),
    ],
)
def test_a_key_s_scopes_parse_to_what_they_grant(
    scope: str, fhir: set[tuple[str, str]], actions: set[str]
) -> None:
    grant = parse(scope)
    assert grant.fhir == fhir and grant.actions == actions


def test_a_key_keeps_its_scopes_as_given_deduplicated_and_sorted() -> None:
    grant = parse(f"system/Task.rs {DECIDE} system/Task.rs system/*.r")
    assert grant.scope == f"{DECIDE} system/*.r system/Task.rs"
    assert parse(grant.scope) == grant


@pytest.mark.parametrize(
    ("scope", "reason"),
    [
        ("openid", "not a scope Mendari knows"),
        ("https://mendari.co/fhir/scope/admin", "not a scope Mendari knows"),
        ("system/Task.c", "serves only 'rs'"),  # no REST create (ADR-0003)
        ("system/Task.cruds", "serves only 'rs'"),
        ("system/Task.u", "serves only 'rs'"),
        ("system/Task.write", "serves only 'rs'"),  # v1 .write is .cud
        ("system/Task.*", "serves only 'rs'"),  # v1 .* is .cruds
        ("system/*.write", "serves only 'rs'"),
        ("system/*.*", "serves only 'rs'"),
        ("system/NamingSystem.rs", "serves only 'r'"),  # no NamingSystem search
        ("system/NamingSystem.read", "serves only 'r'"),
        ("system/Patient.rs", "serves no Patient resources"),
        ("system/Questionnaire.r", "serves no Questionnaire resources"),
        ("patient/Task.rs", "only system/ scopes"),
        ("user/*.rs", "only system/ scopes"),
        ("system/Task.rs?status=completed", "granular"),
        ("system/Task.sr", "in that order"),
        ("system/Task.x", "in that order"),
        ("system/task.rs", "not a scope Mendari knows"),
        ("system/Task", "not a scope Mendari knows"),
    ],
)
def test_a_key_s_scope_mendari_does_not_grant_is_refused_by_name(scope: str, reason: str) -> None:
    with pytest.raises(ScopeError, match=reason) as refused:
        parse(f"system/Task.r {scope}")
    assert repr(scope[:100]) in str(refused.value)


def test_an_empty_scope_is_refused() -> None:
    for scope in ("", "   "):
        with pytest.raises(ScopeError, match="at least one scope"):
            parse(scope)


def test_a_refused_scope_is_repeated_only_in_part() -> None:
    long = "system/" + "A" * 400 + ".rs"
    with pytest.raises(ScopeError) as refused:
        parse(long)
    assert long not in str(refused.value) and long[:100] in str(refused.value)


@pytest.mark.parametrize(
    ("role", "scope", "refused"),
    [
        ("viewer", DECIDE, True),
        ("viewer", OUTCOME, True),
        ("viewer", "system/*.rs", False),
        ("service", f"{DECIDE} {OUTCOME} system/*.rs", False),
    ],
)
def test_a_key_s_scopes_must_be_usable_by_its_role(role: str, scope: str, refused: bool) -> None:
    if refused:
        with pytest.raises(ScopeError, match="which the role does not have"):
            parse(scope, permissions=PERMISSIONS[role])
    else:
        assert parse(scope, permissions=PERMISSIONS[role]).scope


def test_a_grant_stands_for_permissions() -> None:
    assert parse("system/Task.r").permissions == {"read"}
    assert parse(DECIDE).permissions == {"decide", "discover"}  # deciding finds the packs
    assert parse(f"{OUTCOME} system/CodeSystem.s").permissions == {"outcome", "read"}


def test_a_token_request_is_parsed_leniently() -> None:
    """Writes, granular and patient scopes, unknown names and unserved types are dropped; v1
    forms map as SMART says; what is left is written canonically."""
    asked = (
        "system/Task.cruds system/*.write system/NamingSystem.rs patient/Task.rs openid "
        f"system/Provenance.read system/Patient.rs system/AuditEvent.rs?date=gt2026 {DECIDE} "
        "system/CodeSystem.sr"
    )
    grant = parse_lenient(asked)
    assert grant.fhir == {
        ("Task", "r"),
        ("Task", "s"),
        ("NamingSystem", "r"),
        ("Provenance", "r"),
        ("Provenance", "s"),
    }
    assert grant.actions == {DECIDE}
    assert grant.scope == (f"system/NamingSystem.r system/Provenance.rs system/Task.rs {DECIDE}")
    assert parse_lenient("system/*.*").fhir == EVERY_READ
    assert parse_lenient("nothing here").scope == ""


def test_a_token_gets_what_it_asked_within_what_its_client_registered() -> None:
    registered = parse(f"system/Task.rs system/AuditEvent.r {DECIDE}")
    granted = parse_lenient(f"system/*.rs {OUTCOME} {DECIDE}").narrow(registered)
    assert granted.fhir == {("Task", "r"), ("Task", "s"), ("AuditEvent", "r")}
    assert granted.actions == {DECIDE}
    assert granted.scope == f"system/AuditEvent.r system/Task.rs {DECIDE}"
    assert parse_lenient("system/Provenance.rs").narrow(registered).scope == ""


@pytest.mark.parametrize(
    ("method", "path", "scope"),
    [
        ("GET", "/fhir/Task/t1", "system/Task.r"),
        ("GET", "/fhir/Task/t1/_history/2", "system/Task.r"),
        ("GET", "/fhir/Task", "system/Task.s"),
        ("POST", "/fhir/Task/_search", "system/Task.s"),
        ("GET", "/fhir/NamingSystem/n1", "system/NamingSystem.r"),
        ("GET", "/fhir/NamingSystem", None),  # no NamingSystem search
        ("GET", "/fhir/Patient/p1", None),
        ("GET", "/fhir/metadata", None),
        ("GET", "/fhir/OperationDefinition/decide", None),
        ("POST", "/fhir/Task", None),  # no create
        ("GET", "/fhir/Task/_search", None),
        ("POST", "/fhir/$decide", DECIDE),
        ("GET", "/fhir/$decide", None),
        ("POST", "/api/agents/a/b/decide", DECIDE),
        ("POST", "/api/agents/a/b/guard", DECIDE),
        ("POST", "/api/agents/a/b/fhir/decide", DECIDE),
        ("POST", "/api/agents/a/b/evals", None),
        ("POST", "/cds-services/triage", DECIDE),
        ("GET", "/cds-services", DECIDE),  # the lists to decide from (ADR-0004 as amended)
        ("GET", "/api/agents", DECIDE),
        ("GET", "/api/agents/a/b", None),  # a pack's detail: no scope reaches it
        ("POST", "/api/audit/d1/outcome", OUTCOME),
        ("POST", "/api/audit/d1/feedback", None),
        ("GET", "/api/audit", None),
        ("POST", "/api/smart/decide", None),
    ],
)
def test_each_route_needs_one_scope_or_none(method: str, path: str, scope: str | None) -> None:
    assert needed(method, path) == scope


def test_a_grant_covers_exactly_its_scopes() -> None:
    grant = parse(f"system/Task.r {OUTCOME}")
    assert grant.covers("system/Task.r") and grant.covers(OUTCOME)
    assert not grant.covers("system/Task.s")
    assert not grant.covers(DECIDE)
    assert not grant.covers("patient/Task.r")
    assert not Grant(frozenset(), frozenset(), "").covers("system/Task.r")


def test_every_supported_scope_is_granted_to_a_key() -> None:
    supported = scopes.supported()
    assert parse(" ".join(supported)).fhir == EVERY_READ
    assert {DECIDE, OUTCOME} <= set(supported)
    assert "system/NamingSystem.rs" not in supported


@pytest.mark.parametrize(
    ("scope", "read"),
    [
        ("patient/*.rs", ("patient", "*", "rs", "")),
        ("user/Observation.read", ("user", "Observation", "rs", "")),  # SMART v1
        ("system/Task.write", ("system", "Task", "cud", "")),
        ("patient/*.*", ("patient", "*", "cruds", "")),
        (
            "patient/Observation.rs?category=laboratory",
            ("patient", "Observation", "rs", "category=laboratory"),
        ),
        ("launch", None),
        ("launch/patient", None),
        ("openid", None),
        ("patient/*.sr", None),  # out of SMART's order
        ("patient/*.", None),
        ("patient/*.rs?", None),
        ("practitioner/*.rs", None),
        ("Patient/*.rs", None),
    ],
)
def test_an_ehrs_resource_scope_is_read_in_any_context(
    scope: str, read: tuple[str, str, str, str] | None
) -> None:
    """For `mendari smart check`: the scopes an EHR publishes and a launch asks for, in
    patient/, user/ and system/ alike. Reading one grants nothing: a key's `parse` still refuses
    every context but system/."""
    found = scopes.resource_scope(scope)
    if read is None:
        assert found is None
    else:
        context, kind, letters, query = read
        assert found == scopes.ResourceScope(context, kind, frozenset(letters), query)
    if found is not None and found.context != "system":
        with pytest.raises(ScopeError, match=r"granular scopes|only system/ scopes are granted"):
            parse(scope)
