# SPDX-License-Identifier: Apache-2.0
"""Key presets: one per documented integration path, in one table that drives the API
(`POST /api/auth/keys` with `preset`, `GET /api/auth/keys/presets`), the docs (the deployment
guide's table, which is the only documentation of the Da Vinci and outcomes paths' keys) and the
doc-snippet checks (LP-12i, ADR-0008).

A preset is a label and a recipe: the role and scope a key for that path needs, and its default
expiry. What a key may do is its role and scope as created, read on every request; the label is
never read when a key is used, so a later edit of this table never widens or narrows a key that
exists (a rotated key keeps its label and its stored scope). `calls` are the entry points the
path's documentation describes; what a preset's key reaches (`reaches`, computed from the same
rules the guard applies, server/auth.py) may be more, and the docs show both. Several presets
reach the same routes (every decision entry point needs the one decide scope, ADR-0004): their
names say what the key is for, not a boundary.

No preset for Backend Services clients here: the dashboard's Access view (LP-12h) offers the
scoped ones to fill in a client's role and scope, and a client stores no preset label; a preset
with no scope is for keys only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from starlette.routing import Route as Served

from mendari.core.audit import KEY_DAYS, KEY_MAX_DAYS
from mendari.server.scopes import DECIDE, OUTCOME

Route = tuple[str, str]  # (method, path template as mounted)
ID = re.compile(r"[a-z][a-z0-9-]{1,39}")
# The FHIR types a decision's record and trail are read as (docs/guides/api.md): named, never
# `system/*`, which would widen on its own as Mendari serves more types.
_TRAIL_READS = "system/AuditEvent.rs system/Provenance.rs system/Task.rs"


@dataclass(frozen=True)
class Preset:
    id: str  # stable: stored on the key and chained in `key.created`
    title: str
    purpose: str  # one sentence: what the integration does with the key
    role: str  # one of MACHINE_ROLES
    scope: str | None  # canonical text (`scopes.parse(...).scope`); None: the role's own
    calls: tuple[Route, ...]  # the entry points its documentation describes
    # The documentation page's source path under docs/ (a .md file, which the site serves at
    # the same path without `.md`, as `guides/api/`) and its heading's anchor.
    doc: str
    examples: tuple[str, ...] = field(default=())  # snippet steps ("file/step") CI runs with it
    days: int = KEY_DAYS

    def as_dict(self, reaches: Iterable[Route]) -> dict[str, Any]:
        """What `GET /api/auth/keys/presets` answers for it (`examples` are CI's, not shown)."""
        return {
            "id": self.id,
            "title": self.title,
            "purpose": self.purpose,
            "role": self.role,
            "scope": self.scope,
            "days": self.days,
            "min_days": 1,
            "max_days": KEY_MAX_DAYS,
            "calls": [list(call) for call in self.calls],
            "reaches": [list(route) for route in reaches],
            "doc": self.doc,
        }


_ALL = [
    Preset(
        id="rest",
        title="REST API",
        purpose="List the packs, decide on a case and read its audit record: the quickstart's "
        "three calls. The whole `service` role: no scope reaches Mendari's own audit reads.",
        role="service",
        scope=None,
        calls=(
            ("GET", "/api/packs"),
            ("POST", "/api/packs/{namespace}/{name}/decide"),
            ("GET", "/api/audit/{decision_id}"),
        ),
        doc="guides/api.md#quickstart-in-three-calls",
        examples=("quickstart/packs", "quickstart/decide", "quickstart/record"),
    ),
    Preset(
        id="fhir",
        title="FHIR in, FHIR out",
        purpose="Decide with FHIR `$decide`, then read the decision as a Task and its trail by "
        "the AuditEvent and Provenance searches (the trail as one Bundle, `trail` in the "
        "guide's script, reads Mendari's own API: a `rest` key).",
        role="service",
        scope=f"{DECIDE} {_TRAIL_READS}",
        calls=(
            ("POST", "/fhir/$decide"),
            ("GET", "/fhir/Task"),
            ("GET", "/fhir/Task/{resource_id}"),
            ("GET", "/fhir/Task/{resource_id}/_history/{version_id}"),
            ("GET", "/fhir/AuditEvent"),
            ("GET", "/fhir/Provenance"),
        ),
        doc="guides/api.md#fhir-in-fhir-out",
        examples=("fhir/metadata", "fhir/decide", "fhir/search"),
    ),
    Preset(
        id="ai-guard",
        title="AI Guard",
        purpose="Check a model's draft reply before a patient reads it.",
        role="service",
        scope=DECIDE,
        calls=(("POST", "/api/packs/{namespace}/{name}/guard"),),
        doc="guides/api.md#guarding-a-models-replies",
        examples=("guard/check",),
    ),
    Preset(
        id="cds-hooks",
        title="CDS Hooks",
        purpose="An EHR that lists the CDS services and calls them with a hook's context and "
        "prefetch.",
        role="service",
        scope=DECIDE,
        calls=(("GET", "/cds-services"), ("POST", "/cds-services/{service_id}")),
        doc="guides/api.md#fhir-in-fhir-out",
        examples=("cds/discover", "cds/hook"),
    ),
    Preset(
        id="da-vinci",
        title="Da Vinci CRD, DTR and PAS",
        purpose="A payer-facing EHR integration: coverage requirements by CDS Hooks, the DTR "
        "questionnaire package and the PAS request built from a decision.",
        role="service",
        scope=DECIDE,
        calls=(
            ("POST", "/cds-services/{service_id}"),
            ("POST", "/fhir/Questionnaire/$questionnaire-package"),
            ("POST", "/api/packs/{namespace}/{name}/pas"),
        ),
        doc="deploying-with-phi.md#2-sign-in-and-roles",
    ),
    Preset(
        id="outcomes",
        title="Outcomes",
        purpose="A downstream system that records what happened after a decision, for monitoring.",
        role="service",
        scope=OUTCOME,
        calls=(("POST", "/api/audit/{decision_id}/outcome"),),
        doc="deploying-with-phi.md#2-sign-in-and-roles",
    ),
    Preset(
        id="fhir-reader",
        title="FHIR reader",
        purpose="Read decisions as Tasks and their trail as AuditEvent and Provenance, and "
        "nothing else.",
        role="viewer",
        scope=_TRAIL_READS,
        calls=(
            ("GET", "/fhir/Task"),
            ("GET", "/fhir/Task/{resource_id}"),
            ("GET", "/fhir/AuditEvent"),
            ("GET", "/fhir/Provenance"),
        ),
        doc="deploying-with-phi.md#2-sign-in-and-roles",
    ),
]
# In the order a reader meets them.
PRESETS: dict[str, Preset] = {preset.id: preset for preset in _ALL}


def served(routes: Iterable[Any]) -> list[Route]:
    """Every route an app serves, by method (HEAD aside): what a preset's reach is computed
    over, the same for the API and the tests."""
    return sorted(
        {
            (method, route.path)
            for route in routes
            if isinstance(route, Served)
            for method in route.methods or ()
            if method != "HEAD"
        }
    )


def template(route: str) -> re.Pattern[str]:
    """A path template (`/api/audit/{decision_id}`) as the paths it stands for."""
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(route)) + "$")


def markdown() -> str:
    """The deployment guide's table, from the table above (a test keeps the guide's copy equal)."""
    rows = [
        "| Preset | For | Role | Scope | Calls |",
        "|---|---|---|---|---|",
    ]
    for preset in _ALL:
        scope = f"`{preset.scope}`" if preset.scope else "none: the role's own"
        calls = "<br>".join(f"`{method} {path}`" for method, path in preset.calls)
        rows.append(f"| `{preset.id}` | {preset.title} | `{preset.role}` | {scope} | {calls} |")
    return "\n".join(rows)
