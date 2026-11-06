# SPDX-License-Identifier: Apache-2.0
"""A grade (LP-17j): the scoped attestations of one arm on one set, under the grading method 1.0
(`docs/grading/method-1.0.md`, section 6). Each attestation is met, not met or insufficient
evidence. There is no letter, no score and no composite: a grade is its list of attestations,
never a summary of them.

1. **No critical case automated:** met when no positive of any critical label was decided AUTO
   and each critical label has at least `method.MIN_POSITIVES` distinct positives. A critical
   case decided AUTO makes it not met, whatever the evidence: no further case could undo it.
2. **The recall floor**, one per critical label: met when the label's simultaneous lower bound,
   over its distinct groups (`scoreboard`'s grade's bound), is at or above `method.RECALL_FLOOR`.
3. **The false-automation ceiling:** met when false automation's 95% upper bound is at or below
   `method.FALSE_AUTOMATION_CEILING`.

An attestation is insufficient evidence when even a perfect result on these cases could not
meet it: fewer distinct positives than `method.MIN_POSITIVES` (1) or than `method.min_positives`
at the pack's L (2), or fewer labelled cases than `method.min_labelled` (3: 183 for 0.02). Each
bound is worked out from the entry's k and n, never read from its stored figure (6 places), so a
bound just under a floor never rounds up to meet it. Each attestation carries the entry's figures
it was decided on, each k of n with its interval, and the minimum it needed. While the method
is not in effect on the day the entry was measured (`scope.method.in_effect`), every attestation
is marked draft.

A grade is given only on a frozen hidden set: on a public set `grade` is None and `attestations`
refuses. An entry made under another method is refused: its floors may not be these. Nothing
here reads a clock, a setting or the network, and nothing names a case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mendari.errors import MendariError
from mendari.evals import method, stats

PUBLIC = "public"  # the set kind of every shipped set, which nothing grades or ranks
PUBLIC_SET = "public set: regression check, not a ranking"
MET, NOT_MET, INSUFFICIENT = "met", "not met", "insufficient evidence"
STATES = (MET, NOT_MET, INSUFFICIENT)
NO_CRITICAL_AUTOMATED = "no_critical_automated"
RECALL_FLOOR = "recall_floor"
FALSE_AUTOMATION_CEILING = "false_automation_ceiling"
# The safety attestations: an arm with any of these not met is listed unranked (`bands`).
SAFETY = (NO_CRITICAL_AUTOMATED, RECALL_FLOOR)


def is_public(entry: Mapping[str, Any]) -> bool:
    """Whether an entry is of a public set: never graded and never ranked."""
    return bool(entry["scope"]["set"]["kind"] == PUBLIC)


def grade(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """The grade of one entry (`mendari.scoreboard/1`): None on a public set; else the method it
    is made under and its attestations."""
    if is_public(entry):
        return None
    found = attestations(entry)
    return {
        "method": {
            "version": method.METHOD,
            "sha256": method.METHOD_SHA256,
            "in_effect": bool(entry["scope"]["method"]["in_effect"]),
        },
        "attestations": found,
    }


def attestations(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The attestations of one entry, in the method's order: no critical case automated, then
    each critical label's recall floor in the pack's order, then the false-automation ceiling.
    Each holds its `attestation`, its `state` (`STATES`), the figures it was decided on as the
    entry holds them (each k of n with its interval), the minimum it `needed` and whether it is
    a `draft`.

    Refused, in a `MendariError`: an entry of a public set, of another method, with no critical
    label, or whose grade's bound is not at the method's simultaneous level."""
    if is_public(entry):
        raise MendariError(f"{PUBLIC_SET}: a public set is never graded")
    scope = entry["scope"]["method"]
    if (scope["version"], scope["sha256"]) != (method.METHOD, method.METHOD_SHA256):
        raise MendariError(
            f"the entry was made under another method than {method.METHOD} "
            f"(sha256 {method.METHOD_SHA256}): run it again"
        )
    critical = entry["figures"]["critical"]
    labels = len(critical)
    if not labels:
        raise MendariError("the entry has no critical label: there is nothing to attest")
    level = method.simultaneous_level(labels)
    for item in critical:
        bound = item["bound"]
        if (bound["level"], bound["labels"]) != (level, labels) or (bound["k"], bound["n"]) != (
            item["distinct"]["detected"],
            item["distinct"]["positives"],
        ):
            raise MendariError(
                f"the grade's bound of {item['question']}={item['label']} is not the method's "
                f"(at {level}, over the distinct groups)"
            )
    draft = not scope["in_effect"]
    return [
        _no_critical_automated(critical, draft),
        *(_recall_floor(item, labels, draft) for item in critical),
        _false_automation_ceiling(entry["figures"]["false_automation"], draft),
    ]


def _no_critical_automated(critical: Sequence[Mapping[str, Any]], draft: bool) -> dict[str, Any]:
    automated = sum(item["automated"]["k"] for item in critical)
    short = any(item["distinct"]["positives"] < method.MIN_POSITIVES for item in critical)
    state = NOT_MET if automated else INSUFFICIENT if short else MET
    return {
        "attestation": NO_CRITICAL_AUTOMATED,
        "state": state,
        "labels": [
            {
                "question": item["question"],
                "label": item["label"],
                "automated": dict(item["automated"]),  # k of n, with its 95% interval
                "distinct_positives": item["distinct"]["positives"],
            }
            for item in critical
        ],
        "needed": method.MIN_POSITIVES,
        "draft": draft,
    }


def _recall_floor(item: Mapping[str, Any], labels: int, draft: bool) -> dict[str, Any]:
    level = method.simultaneous_level(labels)
    needed = method.min_positives(method.RECALL_FLOOR, labels)
    k, n = item["distinct"]["detected"], item["distinct"]["positives"]
    if n < needed:
        state = INSUFFICIENT
    else:
        low = stats.clopper_pearson(k, n, level)[0]  # exact: a stored bound is rounded
        state = MET if low >= method.RECALL_FLOOR else NOT_MET
    return {
        "attestation": RECALL_FLOOR,
        "question": item["question"],
        "label": item["label"],
        "state": state,
        "floor": method.RECALL_FLOOR,
        "bound": dict(item["bound"]),  # detected groups of the positive groups, at its level
        "needed": needed,
        "draft": draft,
    }


def _false_automation_ceiling(found: Mapping[str, Any], draft: bool) -> dict[str, Any]:
    needed = method.min_labelled(method.FALSE_AUTOMATION_CEILING)
    k, n = found["k"], found["n"]
    if n < needed:
        state = INSUFFICIENT
    else:
        high = stats.clopper_pearson(k, n, method.LEVEL)[1]  # exact: a stored bound is rounded
        state = MET if high <= method.FALSE_AUTOMATION_CEILING else NOT_MET
    return {
        "attestation": FALSE_AUTOMATION_CEILING,
        "state": state,
        "ceiling": method.FALSE_AUTOMATION_CEILING,
        "false_automation": dict(found),  # k of n, with its 95% interval
        "level": method.LEVEL,
        "needed": needed,
        "draft": draft,
    }
