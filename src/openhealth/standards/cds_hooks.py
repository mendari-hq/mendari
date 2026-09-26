"""CDS Hooks 2.0: expose each pack that declares `cds_hooks` as a CDS service.

v0.1 requires the EHR to send the data via `prefetch`; the service never calls back into the
caller's FHIR server. That keeps the service free of outbound requests to caller-supplied URLs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from openhealth.core.decision import Decision, DecisionResult
from openhealth.core.pack import Pack
from openhealth.core.spec import Outcome, Priority
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
    return {"cards": [card(decision.pack, result, task)]}


def card(pack: Pack, result: DecisionResult, task: Mapping[str, Any]) -> dict[str, Any]:
    if result.priority is Priority.STAT:
        indicator = "critical"
    elif result.outcome is Outcome.AUTO:
        indicator = "info"
    else:
        indicator = "warning"
    target = result.recommendation or "manual triage"
    verb = {
        Outcome.AUTO: "Routed",
        Outcome.HUMAN_REVIEW: "Review: route",
        Outcome.ABSTAIN: "No recommendation;",
        Outcome.BLOCK: "Blocked;",
    }[result.outcome]
    summary = f"{verb} to {target}" + (
        f" ({result.priority.value})" if result.priority.value != "routine" else ""
    )
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
                "label": f"Create routing task for {target}",
                "uuid": str(uuid.uuid4()),
                "isRecommended": result.outcome is not Outcome.BLOCK,
                "actions": [
                    {"type": "create", "description": task["description"], "resource": task}
                ],
            }
        ],
        "extension": {"openhealth.decision_id": result.id},
    }
