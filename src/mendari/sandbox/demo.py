# SPDX-License-Identifier: Apache-2.0
"""`mendari demo`: the bundled demo agent end to end, offline, on synthetic data, in seconds.

One story, in six numbered sections and an ending:

1. It routes a few patient portal messages through the agent under its policy.
2. It shows what the agent asked to do with them (spec §13): the demo agent declares two
   actions and its canned answers propose them. A write is released with an AUTO decision, a
   message to the patient is held until a reviewer confirms the decision, and an action the
   agent never declared is blocked. Mendari authorizes each and runs none.
3. It fills a review queue, has simulated reviewers work it, and verifies the audit chain. A
   decision that waits holds the acknowledgement the agent wrote, and its reviewer says what
   becomes of it (ADR-0027): released with a label they confirm, withheld with one they correct.
4. It evaluates the agent on its 600-case set the way a site would before going live. The demo
   agent answers from canned responses (its `mock_file`) and does not detect urgent messages
   that are worded as a patient might say them: the evaluation is there to catch exactly that.
5. It probes the agent (`evals/probe.py`): some of the same cases, each rewritten in ways that
   keep its meaning, to find the failures no one wrote a case for. The probe's own report says
   whose answers it read: the fixture's, on this machine, and not an agent's.
6. It grades what the agent wrote (`evals/written.py`): the demo agent also writes the
   acknowledgement its `send_acknowledgement` action would send. A canned judge grades all 300
   acknowledgements of its `written` set against the agent's rubric, simulated graders graded
   160 of them, and the report says per criterion whether the judge agrees with them enough to
   be believed about the rest. Where it does not, the judge's figure is withheld. The section
   says plainly that the graders are simulated and the grades canned, and that its three states
   are staged (`STAGED`): both are rules the maintainers wrote, keyed on the same phrases.

It ends with what next: the evaluation's verdict; its operating point
(`evals/operating_point.py`), which says whether these cases support any automation threshold
that keeps the urgent messages no person reads to `TARGETS`; the commands to type next; and
where a model of your own goes, with the two things to do once it answers: check its confidence
and gate what it does.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

from mendari.core.agent import Agent
from mendari.core.audit import AuditStore
from mendari.core.decision import Decision, DecisionResult
from mendari.core.reference import bundled_dir
from mendari.core.registry import SearchPath, resolve_agent
from mendari.evals import evaluate, probe, probe_report, resolve_dataset, written
from mendari.evals.operating_point import NONE_CERTIFIED, RECOMMENDED, SUPPORTED, Targets
from mendari.evals.render import shown
from mendari.evals.result import counted

AGENT_ID = "mendari/portal-message-agent"
RUBRIC = "acknowledgement_quality"  # the demo agent's rubric for what it writes
# What section 6 says of itself, in one sentence: its three states are arranged.
STAGED = (
    "The simulated graders and the canned judge are both rules the maintainers wrote, keyed on "
    "the same phrases, so the states below are staged: they show how the gate works, and say "
    "nothing about how good any real judge is."
)
WRITTEN_SET = "written"  # the set it writes for, and whose simulated grades it carries
PROBED = 40  # cases the probe rewrites, each in six ways
FINDINGS_SHOWN = 3
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
# person (critical misses), shown with 95% confidence.
TARGETS = Targets(critical_miss=0.01)
# The demo's last words: the commands to type next, each as the command line reads it, and what
# each is for. They are what an installed Mendari can do.
NEXT = (
    ("mendari new my-agent --id my-clinic/my-agent", "an agent of your own, from a template"),
    ("mendari probe my-agent", "its failures, found and kept as tests (--write)"),
    ("mendari serve", "the console, at http://127.0.0.1:8000"),
)
# A URL, not a folder: an installed Mendari has no `examples/` beside it. The folder of every
# starter, since product code names no model's vendor (tests/test_shipped.py).
EXAMPLES = "https://github.com/mendari-hq/mendari/tree/main/examples"
# An action the demo agent's manifest does not declare, asked for one of its decisions.
UNDECLARED = ("renew_prescription", {"days_supply": 90})
MODEL = f"A model answers behind MENDARI_AGENT_URL: the starters are at {EXAMPLES}"
# What a live agent can then have that canned answers cannot: its confidence calibrated and a
# second signal, and its actions gated as section 2 showed. The guides' folder, as a URL.
GUIDES = "https://github.com/mendari-hq/mendari/tree/main/docs/guides"
THEN = f"Then check its confidence (mendari calibrate) and gate what it does: {GUIDES}"
_WIDTH = 100


def _section(title: str) -> None:
    print(f"\n== {title} {'=' * max(0, 72 - len(title))}")


def bundled() -> Agent:
    """The demo agent this release bundles, and never a copy of it pulled from a registry: a
    pulled copy comes first on the search path, and it automates nothing until an organization
    approves it, so the demo would show every message waiting for a person."""
    root = bundled_dir()
    return resolve_agent(AGENT_ID, [] if root is None else [SearchPath(root, "maintained")])


def run_demo() -> None:
    with AuditStore(":memory:") as store:
        _run(store)


def _run(store: AuditStore) -> None:
    decision = Decision(bundled(), store=store)
    agent = decision.agent
    m = agent.manifest
    providers = ", ".join(f"{slot}={info.variant}" for slot, info in decision.provider_info.items())
    print(f"{m.name} {m.version}  ·  risk level {int(m.risk.level)} ({m.risk.category})")
    print(f"providers: {providers}  (canned answers offline; set MENDARI_AGENT_URL for yours)")

    _section("1. Patient messages, through the agent and its policy")
    decided = []
    for body in SAMPLES:
        result = decision({"message": {"body": body}})
        decided.append(result)
        confidence = "  -  " if result.confidence is None else f"{result.confidence:.2f}"
        target = result.recommendation or "(front desk)"
        print(
            f"{result.outcome.value:<12} {target:<12} {result.priority.value:<7} {confidence}  "
            f"{body[:56]}{'…' if len(body) > 56 else ''}"
        )
    print("The last-but-one message describes chest pain in other words; see section 4.")

    _section("2. What the agent asks to do")
    _actions(decision, store, decided)

    _section("3. The review queue and the audit trail")
    test = resolve_dataset(agent, "test")
    truth = {}
    for record in test.records[:REVIEWED]:
        truth[decision(record["input"]).id] = record["labels"][m.policy.primary]
    pending = store.pending_reviews(limit=1000)
    print(f"{len(pending)} decisions wait for a person (urgent first). Simulating reviewers…")
    for item in pending:
        if item["id"] in truth:
            label = truth[item["id"]]
            store.resolve_review(
                item["id"], final_label=label, reviewer="demo-nurse", texts=_verdicts(item, label)
            )
    stats = store.stats()
    outcomes = "  ".join(f"{k} {v}" for k, v in stats["outcomes"].items())
    print(f"decisions: {stats['decisions']}   {outcomes}")
    overridden = counted(stats["human_override_rate"], stats["reviews_resolved"])
    print(f"overridden by a reviewer: {shown(overridden)}")
    chain = store.verify()
    print(f"audit chain: {'verified' if chain['ok'] else 'BROKEN'} ({chain['events']} events)")

    _section("4. Before going live: evaluate the agent")
    report = evaluate(Decision(agent), test, targets=TARGETS)
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

    _section("5. Find the failures nobody wrote down")
    print(
        "Each case is rewritten six ways that keep its meaning (a typing error, a line break, an\n"
        "instruction slipped in): an answer that then changes is a failure."
    )
    planned = probe.plan(
        Decision(agent), probe.source(agent, test.name), probe.Options(limit=PROBED)
    )
    found = probe.run(planned)
    # The probe's own report, from its second line: the agent is named above.
    print("\n".join(probe_report.text(found.report, shown=FINDINGS_SHOWN).splitlines()[1:]))

    _section("6. What the agent wrote")
    _written(agent)

    _section("What next")
    point = report["run"]["operating_point"]
    for line in (
        f"Verdict: {verdict(point, decisions['critical'])}",
        f"Operating point: {advice(point)}",
    ):
        print(textwrap.fill(line, _WIDTH, subsequent_indent="  ", break_on_hyphens=False))
    print("Next:")
    column = max(len(command) for command, _ in NEXT) + 3
    for command, what in NEXT:
        print(f"  {command:<{column}}{what}")
    print(f"  {MODEL}")
    print(f"  {THEN}")


def _actions(agent: Decision, store: AuditStore, decided: Sequence[DecisionResult]) -> None:
    """What may be done with the actions the agent proposed with the messages above, in four
    lines: one released with an automated decision, one held for a decision's reviewer, the same
    one once the reviewer confirms, and one the agent never declared, asked for afterwards."""
    print("Mendari authorizes each action and runs none: your caller runs what is released.")
    automated = next(result for result in decided if result.outcome.value == "AUTO")
    reviewed = next(
        result
        for result in decided
        if any(action.disposition == "held" for action in result.actions or ())
        and len(result.actions or ()) > 1
    )
    write = next(a for a in automated.actions or () if a.disposition == "released")
    contact = next(a for a in reviewed.actions or () if a.effect == "contact")
    _line(write.disposition, write.name, write.effect, "with the AUTO decision on message 1")
    position = decided.index(reviewed) + 1
    waits = f"message {position} waits for a reviewer"
    _line(contact.disposition, contact.name, contact.effect, waits)
    confirmed = store.resolve_review(
        reviewed.id,
        final_label=str(reviewed.recommendation),
        reviewer="demo-nurse",
        # The acknowledgement the agent wrote waits with the decision: the reviewer read it.
        texts=dict.fromkeys(reviewed.outputs, "release"),
    )
    after = next(a for a in confirmed["actions"] if a["id"] == contact.id)
    _line(after["disposition"], after["name"], after["effect"], "the reviewer confirmed it")
    asked = agent.authorize_action(automated.id, *UNDECLARED)
    _line(asked["disposition"], "an action the agent never declared", None, "asked for message 1")


def _verdicts(item: Mapping[str, Any], label: str) -> dict[str, str]:
    """A simulated reviewer's verdict on each text a decision in the queue holds (ADR-0027):
    released with a label they confirm, withheld with one they correct."""
    verdict = "release" if item["recommendation"] == label else "withhold"
    return {text["name"]: verdict for text in item.get("outputs", ())}


def _written(agent: Any) -> None:
    """The agent's written output, graded: per criterion, how far the canned judge agrees with
    the simulated graders, the gate's state, and the judge's share for the ungraded outputs
    only where that state is validated."""
    from mendari.providers.judge import Judge

    manifest = agent.manifest
    rubric = manifest.rubrics[RUBRIC]
    dataset = resolve_dataset(agent, WRITTEN_SET)
    judge = Judge(agent, RUBRIC, mode="canned")  # offline, whatever model this machine has set
    run = written.run(Decision(agent, origin_cap=False), dataset, RUBRIC, judge=judge)
    declared = manifest.evaluation.grades[RUBRIC]
    scales = {key: [level for level, _ in c.scale()] for key, c in rubric.criteria.items()}
    _, grades = written.load_grades(
        agent.read_bytes(declared.file), where=declared.file, scales=scales
    )
    found = written.report(run.ledger, grades)
    cases = found["cases"]
    for line in (
        f"The agent also writes the {rubric.output} its send_acknowledgement action would send. "
        f"A judge grades each one against the agent's rubric ({RUBRIC}), in the words a "
        "clinician reads, and is believed only as far as it agrees with clinicians.",
        f"Here {len(found['graders'])} SIMULATED graders (made by a script, no clinician) graded "
        f"{cases['graded']} of the {cases['with_output']} synthetic texts, {cases['graded_twice']} "
        "of them twice, and the judge's grades are canned. A demonstration: conclude nothing "
        "from these figures.",
        STAGED,
    ):
        print(textwrap.fill(line, _WIDTH, break_on_hyphens=False))
    for key, measured in found["criteria"].items():
        state = measured["validation"]["state"]
        kappa = measured["judge_agreement"]["kappa"]
        interval = kappa.get("interval") or {}
        if kappa["estimate"] is None:
            agreed = "kappa not estimable"
        elif interval.get("status") != "ok":
            agreed = f"kappa {kappa['estimate']:.2f}, its interval not estimable"
        else:
            agreed = (
                f"kappa {kappa['estimate']:.2f} ({interval['low']:.2f} to {interval['high']:.2f})"
            )
        if measured["judged"] is not None:
            rest = f"judged, of the ungraded: {shown(measured['judged']['passing'])} pass"
        else:
            rest = "judged share withheld"
        if measured["judge_agreement"]["symmetry"]["leaning"]:
            rest += f"; the judge leans {measured['judge_agreement']['symmetry']['leaning']}"
        name = key + (" (must not fail)" if measured["must_pass"] else "")
        print(f"{name:<32} judge {state:<22} {agreed}; {rest}")
    print(
        "Below its floors a judge's figure is withheld, never shown with a caveat. The full report:"
    )
    print(f"  mendari written eval {AGENT_ID} --rubric {RUBRIC} --dataset {WRITTEN_SET}")


def _line(disposition: str, name: str, effect: Any, why: str) -> None:
    what = name if effect is None else f"{name} ({getattr(effect, 'value', effect)})"
    print(f"{disposition:<9} {what:<36} {why}")


def verdict(point: Mapping[str, Any], critical: Sequence[Mapping[str, Any]]) -> str:
    """The evaluation's verdict: whether these cases support the agent's automation threshold
    (the operating point's status), then what the evaluation saw of the critical cases, the
    urgent messages. A critical case that is automated is a critical miss; one that was not
    detected and that a person reads is not."""
    positives = sum(item["n"] for item in critical)
    undetected = sum(item["n"] - item["detected"] for item in critical)
    misses = sum(item["auto_missed"] for item in critical)
    if misses:
        seen = (
            f"On the test set the agent did not detect {undetected} of {positives} urgent "
            f"messages, and {misses} of them would have been automated: critical misses, which "
            "no person reads."
        )
    elif undetected:
        seen = (
            f"On the test set the agent did not detect {undetected} of {positives} urgent "
            "messages, and a person reads each of them."
        )
    else:
        seen = f"On the test set the agent detected each of the {positives} urgent messages."
    status = point["status"]
    if status == SUPPORTED or (status == RECOMMENDED and point["current_supported"]):
        return f"its automation threshold holds on this set. {seen}"
    if status == RECOMMENDED:
        return f"not ready at its automation threshold {point['change']['from']:.2f}. {seen}"
    if status == NONE_CERTIFIED:
        return f"not ready. {seen}"
    return f"review. {seen}"


def advice(point: Mapping[str, Any]) -> str:
    """The operating point in a line or two: the block's own summary, then what to do about it.
    `mendari eval` prints the counts behind it."""
    status = point["status"]
    then = ""
    if status == SUPPORTED:
        then = "Nothing to change: evaluate again when the agent or its cases change."
    elif status == RECOMMENDED:
        then = (
            f"To adopt it, change one line of the agent's manifest (`{point['change']['line']}`), "
            "then evaluate again."
        )
    elif status == NONE_CERTIFIED:
        then = (
            "No threshold fixes this: fix the agent, or add a rule or a second signal, and "
            "evaluate again before it decides anything alone."
            if point["cannot_fix"]
            else "Fix the agent or add cases, and evaluate again before it decides anything alone."
        )
    return f"{point['summary']} {then}".rstrip()
