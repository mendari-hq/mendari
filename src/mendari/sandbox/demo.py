# SPDX-License-Identifier: Apache-2.0
"""`mendari demo`: the bundled demo agent end to end, offline, on synthetic data, in seconds.

It routes a few patient portal messages through the agent under its policy, fills a review queue
and has simulated reviewers work it, verifies the audit chain, then evaluates the agent on its
600-case set the way a site would before going live. The demo agent answers from canned
responses (its `mock_file`) and misses urgent messages that are worded as a patient might say
them: the evaluation is there to catch exactly that, and the demo says what it found. Its
verdict is the evaluation's operating point (`evals/operating_point.py`): whether these cases
support any automation threshold that keeps the urgent messages no person reads to `TARGETS`.
"""

from __future__ import annotations

from typing import Any

from mendari.core.audit import AuditStore
from mendari.core.decision import Decision
from mendari.evals import evaluate, operating_point, resolve_dataset
from mendari.evals.operating_point import NONE_CERTIFIED, RECOMMENDED, SUPPORTED, Targets
from mendari.evals.render import shown
from mendari.evals.result import counted

PACK_ID = "mendari/portal-message-agent"
SAMPLES = (
    "Can you send a refill of my metformin to the pharmacy?",
    "I need to reschedule my appointment to Friday.",
    "Did my lab results come back yet?",
    "Please refill my inhaler. Also I have had chest pain since last night.",
    "It feels like an elephant is sitting on my chest.",
    "No chest pain or anything like that, only a question about my bill.",
)
REVIEWED = 200  # the first cases of the test set, decided and then reviewed
# What the demo holds the agent to: of the urgent messages, at most 1% automated, so read by no
# person, shown with 95% confidence.
TARGETS = Targets(critical_miss=0.01)


def _section(title: str) -> None:
    print(f"\n== {title} {'=' * max(0, 72 - len(title))}")


def run_demo() -> None:
    with AuditStore(":memory:") as store:
        _run(store)


def _run(store: AuditStore) -> None:
    agent = Decision.from_hub(PACK_ID, store=store)
    pack = agent.pack
    m = pack.manifest
    providers = ", ".join(f"{slot}={info.variant}" for slot, info in agent.provider_info.items())
    print(f"{m.name} {m.version}  ·  risk level {int(m.risk.level)} ({m.risk.category})")
    print(f"providers: {providers}  (canned answers offline; set MENDARI_AGENT_URL for yours)")

    _section("1. Patient messages, through the agent and its policy")
    for body in SAMPLES:
        result = agent({"message": {"body": body}})
        confidence = "  -  " if result.confidence is None else f"{result.confidence:.2f}"
        target = result.recommendation or "(front desk)"
        print(
            f"{result.outcome.value:<12} {target:<12} {result.priority.value:<7} {confidence}  "
            f"{body[:56]}{'…' if len(body) > 56 else ''}"
        )
    print("The last-but-one message describes chest pain in other words; see section 3.")

    _section("2. The review queue and the audit trail")
    test = resolve_dataset(pack, "test")
    truth = {}
    for record in test.records[:REVIEWED]:
        truth[agent(record["input"]).id] = record["labels"][m.policy.primary]
    pending = store.pending_reviews(limit=1000)
    print(f"{len(pending)} decisions wait for a person (urgent first). Simulating reviewers…")
    for item in pending:
        if item["id"] in truth:
            store.resolve_review(item["id"], final_label=truth[item["id"]], reviewer="demo-nurse")
    stats = store.stats()
    outcomes = "  ".join(f"{k} {v}" for k, v in stats["outcomes"].items())
    print(f"decisions: {stats['decisions']}   {outcomes}")
    overridden = counted(stats["human_override_rate"], stats["reviews_resolved"])
    print(f"overridden by a reviewer: {shown(overridden)}")
    chain = store.verify()
    print(f"audit chain: {'verified' if chain['ok'] else 'BROKEN'} ({chain['events']} events)")

    _section("3. Before going live: evaluate the agent")
    report = evaluate(Decision(pack), test, targets=TARGETS)
    decisions = report["decisions"]
    request = report["questions"][m.policy.primary]
    print(f"{test.name} set, {len(test.records)} synthetic messages with labels")
    print(f"{m.policy.primary} correct:   {shown(counted(request['accuracy'], request['n']))}")
    automation = decisions["automation"]
    print(f"automated:         {automation['n']} of {len(test.records)}")
    unsafe = counted(automation["false_automation_share"], len(test.records))
    print(f"automated, wrong:  {shown(unsafe)} of all cases")
    for critical in decisions["critical"]:
        name = f"{critical['question']}={critical['label']}"
        print(
            f"critical {name}: {critical['detected']} of {critical['n']} detected, "
            f"{critical['auto_missed']} would have been automated"
        )
    # Whether any automation threshold holds the agent to TARGETS on this evidence: what it
    # found and what to do, without the counts printed just above (`mendari eval` prints all).
    point = report["run"]["operating_point"]
    print()
    print(operating_point.text(point, kinds=operating_point.WHAT_NEXT))
    missed = sum(c["n"] - c["detected"] for c in decisions["critical"])
    automated = sum(c["auto_missed"] for c in decisions["critical"])
    print(f"\nVerdict: {_verdict(point, missed, automated)}")
    print(
        "\nNext: put a model behind it (the model-backed example in examples/, then "
        "MENDARI_AGENT_URL), wrap your "
        "own agent with `mendari new my-agent --id my-clinic/my-agent`, and run "
        "`mendari serve` for the dashboard at http://127.0.0.1:8000."
    )


def _verdict(point: dict[str, Any], missed: int, automated: int) -> str:
    """What the evaluation says of going live, from its operating point: whether these cases
    support the agent's automation threshold, another one, or none."""
    if automated:
        seen = (
            f"The agent missed {missed} urgent messages, and {automated} of them would have gone "
            "out with no person reading them."
        )
    elif missed:
        seen = f"The agent missed {missed} urgent messages, and a person sees each of them."
    else:
        seen = "Every urgent message in this set was caught."
    status = point["status"]
    if status == SUPPORTED:
        return f"its automation threshold holds on this set. {seen} {point['guarantee']}"
    if status == RECOMMENDED:
        now, to = (f"{point['change'][key]:.2f}" for key in ("from", "to"))
        if point["current_supported"]:
            return (
                f"its automation threshold holds on this set, and these cases support {to} too, "
                f"which automates more. {seen} {point['guarantee']}"
            )
        return (
            f"not ready at its automation threshold {now}. {seen} These cases support {to}: "
            "change that one line of its manifest, and evaluate again before it decides "
            "anything alone."
        )
    if status == NONE_CERTIFIED:
        why = (
            "No automation threshold fixes that: fix the agent, or add a rule or a second signal,"
            if point["cannot_fix"]
            else "No automation threshold can be certified on this set: fix the agent or add cases,"
        )
        return f"not ready. {seen} {why} and evaluate again before it decides anything alone."
    return f"review. {seen} {point['summary']}"
