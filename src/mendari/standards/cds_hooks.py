# SPDX-License-Identifier: Apache-2.0
"""CDS Hooks 2.0: expose each pack that declares `cds_hooks` as a CDS service.

The EHR sends the data via `prefetch` (resources or search Bundles), and for order hooks the
orders in `context.draftOrders`. A prefetch key the EHR left out is fetched from the request's
`fhirServer` with its `fhirAuthorization` token, using the pack's own prefetch template, but only
when that server is on the deployment's allow-list (`MENDARI_FHIR_ALLOWED_SERVERS`, empty by
default; see `fhir_client`). Card wording comes from the pack's `cds_hooks.card`; without it, the
card describes a routing decision. A pack that declares `coverage_information` also returns, for
each decided order, a Da Vinci CRD system action (`standards/davinci.py`).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import quote

from mendari import notices
from mendari.core import clock
from mendari.core.decision import Decision, DecisionResult
from mendari.core.origin import COMMUNITY_LABEL
from mendari.core.pack import Pack
from mendari.core.spec import Outcome, Priority
from mendari.core.templates import fill
from mendari.errors import CdsHooksError, FhirError
from mendari.standards import davinci
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import FhirClient

# Hooks whose context carries the orders being written (CDS Hooks `draftOrders`).
ORDER_HOOKS = ("order-select", "order-sign")
# The most draft orders one order hook request may decide: each is a decision (provider calls,
# a chained record), so an unbounded list would multiply one request's cost.
MAX_ORDERS = 50
_TOKEN = re.compile(r"\{\{\s*context\.(\w+)\s*\}\}")
_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")  # a FHIR id


def service_id(pack: Pack) -> str:
    return pack.id.replace("/", ".")


def discovery(packs: Iterable[Pack]) -> dict[str, Any]:
    services = []
    for pack in packs:
        hooks = pack.manifest.cds_hooks
        if hooks is None:
            continue
        usage = (
            f"{pack.status_label} {notices.NOTICE} "
            f"Risk level {int(pack.manifest.risk.level)} ({pack.manifest.risk.category}). "
            f"Validate locally first: `mendari eval {pack.id} --dataset <local data>`."
        )
        description = hooks.description
        if pack.community:  # the EHR's integrator reads this before turning the service on
            description = f"{description} ({COMMUNITY_LABEL})"
            usage = (
                f"{COMMUNITY_LABEL}: it recommends, and a person confirms every decision. {usage}"
            )
        services.append(
            {
                "hook": hooks.hook,
                "id": service_id(pack),
                "title": hooks.title,
                "description": description,
                "prefetch": hooks.prefetch,
                "usageRequirements": usage,
            }
        )
    return {"services": services}


def invoke(
    decision: Decision,
    request: Mapping[str, Any],
    *,
    most: int = MAX_ORDERS,
    charge: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """The cards (and Da Vinci system actions) for one hook request. An order hook decides each
    draft order the pack is about, at most `most` of them; `charge` is given the number of
    decisions about to be made, before anything is fetched or decided (the rate limit), and
    refuses by raising."""
    hooks = decision.pack.manifest.cds_hooks
    if hooks is None:
        raise FhirError(f"pack {decision.pack.id} is not a CDS service")
    if request.get("hook") != hooks.hook:
        raise FhirError(f"service expects hook {hooks.hook!r}, got {request.get('hook')!r}")
    orders: list[dict[str, Any]] = []
    if hooks.hook in ORDER_HOOKS:
        # One decision per draft order the pack is about (e.g. each referral, not the lab
        # orders signed with it), each with the prefetched record.
        context = request.get("context") or {}
        orders = FhirAdapter.for_pack(decision.pack).focus_resources(context.get("draftOrders"))
        if len(orders) > most:
            raise CdsHooksError(
                f"{len(orders)} draft orders to decide; at most {most} in one request"
            )
    if charge is not None:
        charge(len(orders) if hooks.hook in ORDER_HOOKS else 1)
    prefetch = dict(request.get("prefetch") or {})
    missing = [key for key in hooks.prefetch if not prefetch.get(key)]
    if missing:
        prefetch.update(fetch(hooks.prefetch, missing, request))
    if hooks.hook in ORDER_HOOKS:
        decided = [decision.decide_fhir(prefetch, focus=order) for order in orders]
    else:
        decided = [decision.decide_fhir(prefetch)]
    template = hooks.card
    cards = []
    for result, task in decided:
        shown = result.recommendation or "none"
        if template is not None and template.show is not None and shown not in template.show:
            continue  # decided and audited, but nothing worth interrupting the user for
        cards.append(card(decision.pack, result, task))
    response: dict[str, Any] = {"cards": cards}
    coverage = davinci.coverage_reference(prefetch) if hooks.coverage_information else None
    if coverage is not None:
        actions = []
        for order, (result, _) in zip(orders, decided, strict=True):
            info = hooks.coverage_information.get(result.recommendation or "")
            if info is not None:
                form = None
                dtr = hooks.questionnaire
                if dtr is not None and result.recommendation == dtr.missing:
                    labels = {key: answer.label for key, answer in result.answers.items()}
                    context = davinci.context_of(decision.pack, labels)
                    url = davinci.questionnaire_url(decision.pack, context)
                    form = f"{url}|{decision.pack.version}"
                extension = davinci.coverage_information(
                    info, result, coverage, clock.today(), questionnaire=form
                )
                actions.append(davinci.system_action(order, extension))
        if actions:
            response["systemActions"] = actions
    return response


def fetch(
    templates: Mapping[str, str], keys: list[str], request: Mapping[str, Any]
) -> dict[str, Any]:
    """The prefetch the EHR left out, read from its `fhirServer` (allow-listed servers only)."""
    server = request.get("fhirServer")
    if not server:
        raise FhirError(
            f"prefetch required for: {', '.join(keys)} (and no fhirServer to fetch from)"
        )
    authorization = request.get("fhirAuthorization") or {}
    token = authorization.get("access_token")
    if token and str(authorization.get("token_type", "Bearer")).lower() != "bearer":
        raise FhirError("fhirAuthorization must be a Bearer token")
    context = request.get("context") or {}
    queries = {key: fill_template(templates[key], context) for key in keys}
    with FhirClient(str(server), token=token) as client:
        return {key: client.read(query) for key, query in queries.items()}


def fill_template(template: str, context: Mapping[str, Any]) -> str:
    """A prefetch template with its `{{context.x}}` tokens filled; each value must be a FHIR id,
    so nothing but an id can reach the query."""

    def value(match: re.Match[str]) -> str:
        filled = context.get(match.group(1))
        if not isinstance(filled, str) or not _ID.match(filled):
            raise FhirError(f"context.{match.group(1)} is needed to fetch {template.split('?')[0]}")
        return quote(filled, safe="")

    query = _TOKEN.sub(value, template)
    if "{{" in query:
        raise FhirError(f"cannot fill the prefetch template {template!r}")
    return query


def source_label(pack: Pack) -> str:
    """Who a card comes from, as the clinician sees it under the card: a community pack says it
    is not clinically reviewed."""
    label = f"Mendari · {pack.manifest.name} {pack.version}"
    return f"{label} · {COMMUNITY_LABEL}" if pack.community else label


def card(pack: Pack, result: DecisionResult, task: Mapping[str, Any]) -> dict[str, Any]:
    template = pack.manifest.cds_hooks.card if pack.manifest.cds_hooks else None
    target = result.recommendation or "manual triage"
    key = result.recommendation or "none"
    if result.priority is Priority.STAT:
        indicator = "critical"
    elif template is not None and key in template.indicators:
        indicator = template.indicators[key]
    elif result.outcome is Outcome.AUTO:
        indicator = "info"
    else:
        indicator = "warning"
    if template is not None and key in template.summaries:
        summary = fill(template.summaries[key], result, recommendation=target)
    else:
        verb = {
            Outcome.AUTO: "Routed",
            Outcome.HUMAN_REVIEW: "Review: route",
            Outcome.ABSTAIN: "No recommendation;",
            Outcome.BLOCK: "Blocked;",
        }[result.outcome]
        summary = f"{verb} to {target}" + (
            f" ({result.priority.value})" if result.priority.value != "routine" else ""
        )
    label = template.suggestion if template is not None else f"Create routing task for {target}"
    confidence = "n/a" if result.confidence is None else f"{result.confidence:.0%}"
    detail = "\n".join(
        [f"*{pack.status_label}* *{notices.NOTICE}*", ""]
        + [f"**Outcome:** {result.outcome.value} · **confidence:** {confidence}", ""]
        + [f"- {reason}" for reason in result.reasons]
        + ([f"\nFallback: {result.fallback}"] if result.fallback else [])
    )
    return {
        "uuid": str(uuid.uuid4()),
        "summary": summary[:140],
        "indicator": indicator,
        "detail": detail,
        "source": {"label": source_label(pack)},
        "suggestions": [
            {
                "label": label,
                "uuid": str(uuid.uuid4()),
                "isRecommended": result.outcome is not Outcome.BLOCK,
                "actions": [
                    {"type": "create", "description": task["description"], "resource": task}
                ],
            }
        ],
        "extension": {"mendari.decision_id": result.id},
    }
