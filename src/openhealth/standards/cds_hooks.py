"""CDS Hooks 2.0: expose each pack that declares `cds_hooks` as a CDS service.

The EHR must send the data via `prefetch` (resources or search Bundles); the service never calls
back into the caller's FHIR server, which keeps it free of outbound requests to caller-supplied
URLs. Card wording comes from the pack's `cds_hooks.card`; without it, the card describes a
routing decision.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from openhealth.core.decision import Decision, DecisionResult
from openhealth.core.pack import Pack
from openhealth.core.spec import Outcome, Priority
from openhealth.core.templates import fill
from openhealth.errors import FhirError


def service_id(pack: Pack) -> str:
    return pack.id.replace("/", ".")


def discovery(packs: Iterable[Pack]) -> dict[str, Any]:
    services = []
    for pack in packs:
        hooks = pack.manifest.cds_hooks
        if hooks is None:
            continue
        services.append(
            {
                "hook": hooks.hook,
                "id": service_id(pack),
                "title": hooks.title,
                "description": hooks.description,
                "prefetch": hooks.prefetch,
                "usageRequirements": (
                    f"Risk level {int(pack.manifest.risk.level)} ({pack.manifest.risk.category}). "
                    f"Validate locally first: `openhealth eval {pack.id} --dataset <local data>`."
                ),
            }
        )
    return {"services": services}


def invoke(decision: Decision, request: Mapping[str, Any]) -> dict[str, Any]:
    hooks = decision.pack.manifest.cds_hooks
    if hooks is None:
        raise FhirError(f"pack {decision.pack.id} is not a CDS service")
    if request.get("hook") != hooks.hook:
        raise FhirError(f"service expects hook {hooks.hook!r}, got {request.get('hook')!r}")
    prefetch = request.get("prefetch") or {}
    missing = [key for key in hooks.prefetch if not prefetch.get(key)]
    if missing:
        raise FhirError(f"prefetch required for: {', '.join(missing)}")
    result, task = decision.decide_fhir(prefetch)
    template = hooks.card
    shown = result.recommendation or "none"
    if template is not None and template.show is not None and shown not in template.show:
        return {"cards": []}  # decided and audited, but nothing worth interrupting the user for
    return {"cards": [card(decision.pack, result, task)]}


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
        [f"**Outcome:** {result.outcome.value} · **confidence:** {confidence}", ""]
        + [f"- {reason}" for reason in result.reasons]
        + ([f"\nFallback: {result.fallback}"] if result.fallback else [])
    )
    return {
        "uuid": str(uuid.uuid4()),
        "summary": summary[:140],
        "indicator": indicator,
        "detail": detail,
        "source": {"label": f"OpenHealth · {pack.manifest.name} {pack.version}"},
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
        "extension": {"openhealth.decision_id": result.id},
    }
