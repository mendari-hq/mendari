# SPDX-License-Identifier: Apache-2.0
"""`mendari` command-line interface."""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import shutil
import sys
import textwrap
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, NoReturn
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from mendari import __version__
from mendari.core import clock, tenancy
from mendari.core.agent import Agent
from mendari.core.audit import SYSTEM_ID, AuditStore, break_glass_reason
from mendari.core.decision import Decision
from mendari.core.registry import discover, resolve_agent, version_key
from mendari.errors import AgentValidationError, HubError, MendariError
from mendari.sandbox import DEMO_AGENT, example_datasets_dir
from mendari.settings import (
    applies_to,
    daily_limits,
    fhir_validation,
    parse_providers,
    providers_for,
)

EXIT_DEGRADED = 2
# `mendari probe`: what `--fail-on` exits with, and how many cases it probes unless told
# (`evals/probe_report.py` EXIT_FOUND and `evals/probe.py` DEFAULT_LIMIT, which a test holds to
# these: the parser is built without importing either).
EXIT_FOUND = 2
PROBE_CASES = 50


def _decision(args: argparse.Namespace, **kwargs: Any) -> Decision:
    agent = resolve_agent(args.agent)
    providers = _providers(agent, parse_providers(args.provider), typed=bool(args.provider))
    return Decision(agent, providers=providers, fhir_validation=fhir_validation(), **kwargs)


def _providers(agent: Agent, settings: dict[str, str], *, typed: bool = True) -> dict[str, str]:
    """The settings that apply to this agent. A setting that names it but cannot apply (a slot's
    unknown variant) is an error here, where the server only reports it. So is a typed setting
    (`--provider`, `--run`) that applies to nothing in the agent: an unknown slot, or another
    agent's setting. From $MENDARI_PROVIDERS, a
    deployment-wide list naming other agents' slots too, such a setting is only a warning."""
    slots = {slot: set(spec.variants) for slot, spec in agent.manifest.providers.items()}
    chosen, skipped = providers_for(agent.id, slots, settings, named_only=agent.named_settings_only)
    if skipped:
        raise MendariError(f"provider settings that do not apply to {agent.id}: {skipped}")
    unused = [
        _unused(agent, key, variant, slots)
        for key, variant in settings.items()
        if not applies_to(key, agent.id, slots, named_only=agent.named_settings_only)
    ]
    if unused and typed:
        raise AgentValidationError(agent.id, unused)
    for problem in unused:
        print(f"warning: MENDARI_PROVIDERS: {problem}", file=sys.stderr)
    return chosen


def _unused(agent: Agent, key: str, variant: str, slots: dict[str, set[str]]) -> str:
    target, sep, slot = key.rpartition(":")
    if sep and target != agent.id:
        return f"provider setting {key}={variant} names {target}, not this agent"
    return (
        f"provider setting {key}={variant}: the agent has no slot {slot!r} "
        f"(its slots: {', '.join(sorted(slots))})"
    )


def _read_json(path: str | None, flag: str, *, shape: type | None = None) -> Any:
    """A JSON file a flag names (`-` or nothing: standard input), as one line naming the flag and
    the file when it cannot be read: missing, not UTF-8, not JSON (where, never its text), or not
    the `shape` (an object) the command reads."""
    where = "standard input" if path in (None, "-") else f"{flag} {_path_shown(path)}"
    try:
        if path in (None, "-"):
            loaded = json.load(sys.stdin)
        else:
            with open(path, encoding="utf-8") as source:
                loaded = json.load(source)
    except json.JSONDecodeError as error:
        raise MendariError(
            f"{where}: not valid JSON (line {error.lineno}, column {error.colno}: {error.msg})"
        ) from None
    except UnicodeDecodeError:
        raise MendariError(f"{where}: not UTF-8 text; save it as UTF-8") from None
    if shape is not None and not isinstance(loaded, shape):
        raise MendariError(f"{where}: expected a JSON {'object' if shape is dict else 'list'}")
    return loaded


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


# -- commands --------------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    found = discover()
    for agent in sorted(found.agents, key=lambda p: p.id):
        m = agent.manifest
        print(f"{m.id:<40} {m.version:<8} risk {int(m.risk.level)}  {m.name}")
    for root, problem in found.problems.items():
        print(f"invalid agent at {root}:\n{problem}", file=sys.stderr)
    # An owned agent whose provider configs `mendari validate` refuses still loads: said here.
    for agent in sorted(found.agents, key=lambda p: (p.id, p.root)):
        if agent.warnings:
            listed = "\n".join(f"  - {warning}" for warning in agent.warnings)
            print(f"warnings for {agent.ref} at {agent.root}:\n{listed}", file=sys.stderr)
    if not found.agents:
        print("No agents found. Set MENDARI_AGENTS_PATH or run from a checkout.", file=sys.stderr)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from mendari.evals.validation import validate

    agent = resolve_agent(args.agent)
    validate(agent)
    print(f"ok  {agent.ref}  {agent.digest}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    from mendari.evals import run_behaviour_tests

    decision = _decision(args, origin_cap=False)  # the agent's own policy, as its tests say
    outcomes = run_behaviour_tests(decision)
    if not outcomes:  # nothing ran: a pass would say nothing (LP-15)
        where = decision.agent.manifest.tests or "tests/cases.yaml (and `tests:` in agent.yaml)"
        raise MendariError(
            f"{decision.agent.ref}: no behaviour tests to run: add cases to {where} (spec §10)"
        )
    for outcome in outcomes:
        print(f"{'PASS' if outcome.passed else 'FAIL'}  {outcome.name}")
        for failure in outcome.failures:
            print(f"        {failure}")
    failed = sum(not o.passed for o in outcomes)
    print(f"\n{len(outcomes) - failed} passed, {failed} failed")
    return 1 if failed else 0


def cmd_probe(args: argparse.Namespace) -> int:
    """`mendari probe`: change the text of an agent's own cases in ways that keep its meaning,
    decide each variant as the original is decided, and report what the change made worse
    (`evals/probe.py`): ids, names and counts, never a case's text. `--write` keeps each
    failure as a behaviour test in the agent (`evals/probe_tests.py`), and `--confirm` keeps
    one that waited for a person. The exit code is 0 unless `--fail-on` names what was found."""
    from mendari.core.registry import is_within
    from mendari.evals import probe, probe_report, probe_tests
    from mendari.evals.probe_model import Writer
    from mendari.evals.probe_variants import by_a_model, translation

    decision = _decision(args, origin_cap=False)  # the agent's own policy, as its tests say
    agent = decision.agent
    said = sys.stderr if args.json else sys.stdout  # with --json the report alone is on stdout
    if args.confirm:
        for name in probe_tests.confirm(agent, args.confirm):
            where = probe_tests.PROBE_TESTS
            print(f"Confirmed {name}: `mendari test` runs it now ({where}).", file=said)
        return 0
    if args.write and (problem := probe_tests.not_writable(agent)) is not None:
        raise MendariError(f"--write: {problem}")  # before the run, not after it
    if args.variants_out and is_within(args.variants_out, agent.root):
        raise MendariError(
            "--variants-out: that file would be in the agent's own folder, where every file is "
            "part of the agent: keep the variants outside it"
        )
    given = probe.load_variants(args.variants_from, agent.manifest) if args.variants_from else []
    names = [name for listed in args.transform or [] for name in listed.split(",") if name]
    names += [translation(language) for language in args.language or []]
    if not names and not given:
        names = list(probe.Options().transformations)  # every offline one
    by_model = any(by_a_model(name) is not None for name in names)
    if args.model and not by_model:
        raise MendariError(
            "--model writes paraphrase, negation and translate variants: name one with "
            "--transform or --language"
        )
    options = probe.Options(
        transformations=tuple(dict.fromkeys(names)),
        seed=args.seed,
        limit=args.limit,
        variants=args.variants,
        fields=tuple(args.field) if args.field else None,
        workers=args.workers,
    )
    cases = probe.source(agent, args.dataset, tests=args.from_tests)
    writer = Writer(args.model) if by_model else None
    planned = probe.plan(decision, cases, options, given=given, writer=writer)
    if args.dry_run:
        summary = planned.summary()
        print(json.dumps(summary, indent=2) if args.json else probe_report.planned(summary))
        return 0
    found = probe.run(planned)
    report = json.dumps(found.report, indent=2)
    for path, content in (
        (args.out, report + "\n"),
        (args.variants_out, probe.dump_variants(found.variants)),
    ):
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content, encoding="utf-8")
    print(report if args.json else probe_report.text(found.report))
    if args.write:
        kept = probe.keep(agent, found.findings)
        for line in probe_report.kept_lines(
            kept.added, kept.waiting, kept.already, kept.left_out, args.agent
        ):
            print(line, file=said)
    return probe_report.exit_code(found.report, args.fail_on)


def cmd_decide(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy

    # Recorded under the PHI mode set here (checked first). A database a protected mode kept
    # refuses an input in plaintext, so a run without the deployment's settings cannot add one.
    store = AuditStore(args.db, phi=phi_policy()) if args.db else None
    held = None
    if store is not None:  # recorded in a deployment's trail: its holds apply here too

        def held(agent: Agent) -> str | None:
            hold = store.hold_for(agent.id, agent.digest, tenancy.DEFAULT_ORG)
            return None if hold is None else f"hold {hold['id']}"

    approved = None
    if store is not None:  # an agent from a registry, approved in that deployment's trail

        def approved(agent: Agent) -> bool:
            return (
                store.registry_approval(agent.id, agent.digest, in_org=tenancy.DEFAULT_ORG)
                is not None
            )

    decision = _decision(args, store=store, hold_source=held, origin_approval=approved)
    if args.text is not None:
        payload: Any = {"message": {"body": args.text}}
    else:
        payload = _read_json(args.input, "--input")
    if args.fhir:
        result, task = decision.decide_fhir(payload)
        _print_json({"decision": result.to_dict(), "task": task})
    else:
        decided = decision.decide(payload)
        # What the agent wrote (spec §15), shown to whoever ran the command; never recorded.
        written = {"outputs": dict(decided.outputs)} if decided.outputs else {}
        _print_json({**decided.to_dict(), **written})
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from mendari.core.card import benchmark
    from mendari.evals import compare, confidence, gate, operating_point, resolve_dataset
    from mendari.evals.compare import FROM_REGISTRY, not_compared
    from mendari.evals.harness import evaluation
    from mendari.evals.render import comparison_text, report_markdown, report_text

    if args.json and args.markdown:
        raise MendariError("choose --json or --markdown, not both")
    stated = gate.Limits(
        min_accuracy=args.min_accuracy,
        max_false_automation=args.max_false_automation,
        min_critical_detection=args.min_critical_detection,
        max_critical_misses=args.max_critical_misses,
    )
    held = _held_to(args)
    sharing = bool(args.share or args.share_out)
    issued: dict[str, Any] | None = None
    publisher_key = _publishing(args, sharing)
    targets = _targets(args)
    if sharing:
        # A site's run (LP-22f): the ticket first, bound to the whole dataset file and the
        # configuration (the deterministic arm unless --provider names one), then the run.
        if args.limit:
            raise MendariError(
                "a shared run covers the whole dataset file (its ticket's commitment is over "
                "the file): drop --limit"
            )
        agent = resolve_agent(args.agent)
        configuration = _site_configuration(agent, args.provider, consent=args.consent_remote)
        decision = Decision(
            agent, providers=configuration, fhir_validation=fhir_validation(), origin_cap=False
        )
        dataset = resolve_dataset(agent, args.dataset)
        issued = _ticket_for(args, agent, configuration, dataset)
    else:
        decision = _decision(args, origin_cap=False)  # measures the agent's own policy
        agent = decision.agent
        dataset = resolve_dataset(agent, args.dataset)
        if args.limit:
            dataset = dataset.head(args.limit)
    workers = args.workers or (8 if decision.is_remote else 1)
    run = evaluation(decision, dataset, workers=workers, targets=targets)
    report = run.report
    checked = None
    if stated.stated or held is not None:
        tolerance = agent.manifest.evaluation.degradation_tolerance
        checked = gate.check(report, stated, held, tolerance)

    comparison = None
    if args.baseline != "none":
        if args.baseline == "auto":
            is_benchmark_run = dataset.name == agent.manifest.evaluation.benchmark_dataset
            baseline = None if is_benchmark_run else benchmark(agent)
        else:
            baseline = _read_json(args.baseline, "--baseline", shape=dict)
        if baseline is not None:
            tolerance = agent.manifest.evaluation.degradation_tolerance
            if args.baseline == "auto" and agent.origin == "hub":
                # An agent from a registry carries its publisher's benchmark, never a reference
                # (ADR-0012); a report given with --baseline is the caller's own.
                comparison = not_compared(FROM_REGISTRY)
            else:
                comparison = compare(report, baseline, tolerance)
            report["comparison"] = comparison

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists() or out.is_file():  # a file: its ledger goes beside it, first
            _write_ledger(out, run.ledger.dumps(), run.ledger.file_name)
        else:  # a device such as /dev/stdout
            print("note: --out is not a file, so no ledger is written beside it", file=sys.stderr)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.db:
        AuditStore(args.db).save_report(report, ledger=run.ledger.dumps(), usage=run.usage)
    if publisher_key is not None:
        # Counts only (mendari.publisher-evaluation/1), signed with a namespace key, sent with
        # its publisher token: the registry re-runs a local configuration and refuses counts
        # that are not its own (ADR-0020).
        with _registry_client(args) as registry:
            published = _publish_run(registry, agent, run, publisher_key, args.labelled_by)
            print(
                f"published evaluation {published['id']} of {published['agent']}@"
                f"{published['version']} ({_configuration_words(published['configuration'])}, "
                f"dataset {published['dataset']}): {published['rerun_words']}",
                file=sys.stderr,
            )
            for line in _where_it_shows(registry, agent.id):
                print(line, file=sys.stderr)
    if issued is not None:
        from mendari.evals.share import shareable
        from mendari.hub import site_signing

        # Counts only (mendari.site-report/1), from the run's ledger, small cells suppressed,
        # signed with the site's key as the ticket's answer.
        counts = shareable(run.ledger, remote_consent=bool(args.consent_remote))
        submission = site_signing.Site().sign(
            site_signing.SUBMISSION, {"ticket": issued["ticket"]["id"], "report": counts}
        )
        if args.share_out:
            Path(args.share_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.share_out).write_text(
                json.dumps(submission, indent=2) + "\n", encoding="utf-8"
            )
            print(_submission_summary(submission), file=sys.stderr)
            print(
                f"wrote the signed submission to {args.share_out} (counts only; nothing sent): "
                "read it, then `mendari site submit` it",
                file=sys.stderr,
            )
        else:
            with _registry_client(args) as registry:
                shared = registry.submit(submission, issued["submit_secret"])
            print(
                f"shared report {shared['id']} for ticket {shared['ticket']} (counts only, signed)",
                file=sys.stderr,
            )
    if args.json:
        _print_json(report)
        if checked is not None:  # beside the JSON, never in it
            print(checked.text(), file=sys.stderr)
    elif args.markdown:
        from mendari.evals.ledger import case_hash
        from mendari.evals.result import critical_cases

        # Cases by their ids, which only this machine can tell from the ledger's hashes.
        ids = {case_hash(dataset.case_key, r["id"]): r["id"] for r in dataset.records}
        print(report_markdown(report, critical_cases(run.ledger), ids))
        # What the automation threshold lets through, and the threshold the cases support when
        # the run was given targets: the engine's sentences, as the terminal prints them.
        print(operating_point.markdown(report["run"]["operating_point"]))
        if said := confidence.markdown(report["run"]):
            print(said)
        if comparison:  # as the terminal shows it, in a block that renders nothing
            print("### Against the agent's benchmark\n")
            print(textwrap.indent(comparison_text(comparison), "    ") + "\n")
        if checked is not None:
            print(checked.markdown())
    else:
        print(report_text(report))
        print()
        print(operating_point.text(report["run"]["operating_point"], *_no_target(targets)))
        if said := confidence.text(report["run"]):
            # What a calibration did on these cases, and what a second signal did.
            print()
            print(said)
        if comparison:
            print()
            print(comparison_text(comparison))
        if checked is not None:
            print()
            print(checked.text())
        if args.out:
            print(f"\nReport written to {args.out}")
    degraded = comparison is not None and comparison["status"] == "degraded"
    failed = checked is not None and not checked.passed
    return EXIT_DEGRADED if failed or (degraded and args.fail_on_degradation) else 0


def _held_to(args: argparse.Namespace) -> dict[str, Any] | None:
    """`--no-worse-than`: the earlier result the run is held to, or None without the option. It
    is read and checked before the run, and before a shared run asks for its ticket: a file that
    is no result of this agent stops here, and `--out` may then write the new result over it."""
    if not args.no_worse_than:
        return None
    from mendari.evals.gate import baseline_problem

    held: dict[str, Any] = _read_json(args.no_worse_than, "--no-worse-than", shape=dict)
    agent = resolve_agent(args.agent)
    problem = baseline_problem(held, agent.id, agent.manifest.policy.primary)
    if problem is not None:
        raise MendariError(f"--no-worse-than {_path_shown(args.no_worse_than)}: {problem}")
    return held


def _targets(args: argparse.Namespace) -> Any:
    """What a recommended automation threshold must keep to (`--target-critical-miss`,
    `--target-false-automation`, stated with `--target-confidence`). Each was read as a share
    by the command line (`_target`, `_confidence`), before any work."""
    from mendari.evals.operating_point import CONFIDENCE, OF_CRITICAL, Targets

    of = args.target_critical_miss_of
    if of is not None and args.target_critical_miss is None:
        raise MendariError(
            "--target-critical-miss-of says what --target-critical-miss is a share of: give "
            "that target too"
        )
    confidence = args.target_confidence
    return Targets(
        critical_miss=args.target_critical_miss,
        critical_miss_of=OF_CRITICAL if of is None else of,
        false_automation=args.target_false_automation,
        confidence=CONFIDENCE if confidence is None else confidence,
    )


def _no_target(targets: Any) -> list[str]:
    """What the operating point adds when the run was given no target: how to give one."""
    if targets.given:
        return []
    return [
        "No target was given. Add --target-critical-miss or --target-false-automation (for "
        "example --target-critical-miss 1%) for the lowest automation threshold these cases "
        "support."
    ]


def _publishing(args: argparse.Namespace, sharing: bool) -> Any:
    """`--publish`: the publisher key to sign with, once what a published evaluation cannot be
    is refused, before the run (ADR-0020): it is the publisher's, never a site's share; it
    covers the whole dataset; and the dataset is one the agent's manifest declares (a public set
    in its signed archive, never a file). None without `--publish`."""
    if not args.publish:
        if args.key:
            raise MendariError("--key signs a published evaluation: add --publish")
        if args.labelled_by:
            raise MendariError("--labelled-by is said of a published evaluation: add --publish")
        return None
    from mendari.hub import archive

    if sharing:
        raise MendariError("--publish is a publisher's evaluation and --share a site's: choose one")
    if args.limit:
        raise MendariError("a published evaluation covers the whole dataset: drop --limit")
    if not args.key:
        raise MendariError("--publish signs with a namespace's publisher key: --key FILE")
    _published_dataset(resolve_agent(args.agent), args.dataset, "--publish")
    _labelled_by(args.labelled_by)
    return archive.load_private_key(Path(args.key).read_bytes())


def _published_dataset(agent: Agent, dataset: str, flag: str) -> None:
    """A published evaluation's dataset is one the agent's manifest declares: a public set in
    its signed archive, which the registry holds too, never a file; and one large enough to
    publish. Refused here, before anything is run or sent."""
    from mendari.evals import resolve_dataset
    from mendari.evals.share import MIN_DATASET

    declared = agent.manifest.evaluation.datasets
    if dataset not in declared:
        named = ", ".join(declared) or "none"
        raise MendariError(
            f"{flag}: the dataset is one the agent's manifest declares, in its signed archive "
            f"({named}), never a file"
        )
    cases = len(resolve_dataset(agent, dataset).records)
    if cases < MIN_DATASET:
        raise MendariError(
            f"{flag}: a published evaluation is of at least {MIN_DATASET} cases, and {dataset} "
            f"has {cases}"
        )


def _labelled_by(said: str | None) -> None:
    """`--labelled-by`: who labelled the dataset, in the publisher's words. It is signed and
    shown as written, so it is one line a page can print."""
    from mendari.hub.evaluations import MAX_LABELLED_BY
    from mendari.hub.yank import printable_line

    if said is None:
        return
    if not said.strip() or printable_line(said, MAX_LABELLED_BY):
        raise MendariError(
            f"--labelled-by is one line of printable text, at most {MAX_LABELLED_BY} characters"
        )


def _configuration_words(configuration: Mapping[str, str]) -> str:
    return ", ".join(f"{slot}={variant}" for slot, variant in sorted(configuration.items()))


def _publish_run(
    registry: Any, agent: Agent, run: Any, key: Any, labelled_by: str | None
) -> dict[str, Any]:
    """Sign one run as the publisher's evaluation and send it; the registry's answer. A run in
    which a provider failed is published with how many cases that was (where an answer is
    missing the engine sends the case to a person, so those cases say nothing of the
    configuration), and this says so here too."""
    from mendari.hub import evaluations

    record = evaluations.build(agent, run, key=key, labelled_by=labelled_by)
    failed = record["claimed"]["failed_cases"]
    if failed:
        print(
            f"note: a provider failed on {failed} of {record['dataset']['n']} cases of this "
            "run; the evaluation says so. Run it again for one with none",
            file=sys.stderr,
        )
    return dict(registry.publish_evaluation(record))


def _linkable(url: Any, ends: str) -> str | None:
    """An address a registry gave, when it is one to print and to paste into a README: https
    (http on this machine), ending as this agent's page does, and holding nothing that would
    end a Markdown link or move a terminal's cursor. Anything else is no address."""
    from mendari.core.https_only import is_https_or_local

    if not isinstance(url, str) or len(url) > 500 or not url.endswith(ends):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._~:/%+-]+", url) or not is_https_or_local(url):
        return None
    return url


def _where_it_shows(registry: Any, agent_id: str) -> list[str]:
    """After a publish: where the Agent Hub shows the agent's evaluation, and the Markdown
    line that puts its badge in a README, linked to that page. The badge is the registry's own
    answer, so its address is the registry's; the page's is whatever Hub the registry names
    (`MENDARI_REGISTRY_HUB_URL`). For a registry that serves no evaluation page, it says so."""
    namespace, name = agent_id.split("/")
    try:
        found = registry.evaluation(namespace, name)
    except HubError:
        return ["this registry serves no evaluation page or badge"]
    answer = registry.evaluation_url(namespace, name)
    links = found.get("links") if isinstance(found.get("links"), Mapping) else {}
    page = _linkable(links.get("page"), f"/agents/{namespace}/{name}/evaluation")
    lines = []
    if page is None:
        lines.append(
            "page:  this registry names no Agent Hub (its operator sets "
            f"MENDARI_REGISTRY_HUB_URL); its own answer is {answer}"
        )
    else:
        lines.append(f"page:  {page}")
    badge = registry.badge_url(namespace, name)
    lines.append(f"badge: [![Mendari evaluation]({badge})]({page or answer})")
    said = found.get("badge") if isinstance(found.get("badge"), Mapping) else {}
    if isinstance(said.get("message"), str) and re.fullmatch(
        r"[\x20-\x7e]{1,200}", said["message"]
    ):
        lines.append(f"       it reads: Mendari | {said['message']}")
    return lines


def _calibration_set_note(agent: Agent, name: str) -> None:
    """Say, for an `llm` variant and an `agent` variant alike, that a calibration is being
    fitted on one of the agent's own evaluation sets: `mendari eval` on it then reports on the
    cases the fit saw. The agent's fitting sets (`train`, `calibration`) are what a fit is for."""
    if name in agent.manifest.evaluation.datasets and name not in ("train", "calibration"):
        print(
            f"note: {name} is one of this agent's evaluation sets. A calibration fitted on the "
            "cases an evaluation reports on makes that evaluation look better calibrated than "
            "the agent is: fit it on a set of its own (`calibration` in evaluation.datasets).",
            file=sys.stderr,
        )


def _calibrate_agent(args: argparse.Namespace, agent: Agent, provider: Any) -> int:
    """`mendari calibrate` for an `agent` variant (ADR-0024): the live agent is asked about each
    labelled case once, and one temperature per question is fitted on its own confidence, for
    the version its service reported (`providers/agent_calibration.py`)."""
    from mendari.evals import resolve_dataset
    from mendari.evals.harness import record_problems
    from mendari.providers import agent_calibration

    where = f"{args.slot}/{args.variant}"
    provider.preflight()
    dataset = resolve_dataset(agent, args.dataset)
    if args.limit:
        dataset = dataset.head(args.limit)
    problems = record_problems(agent.manifest, dataset)
    if problems:
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise MendariError(f"{problems[0]}{more}")
    _calibration_set_note(agent, dataset.name)
    records = dataset.records
    print(f"asking {provider.info.model or where} about {len(records)} {dataset.name} cases...")
    fitted = agent_calibration.fit(
        provider,
        records,
        dataset=dataset.name,
        sha256=dataset.sha256,
        date=clock.today().isoformat(),
        workers=args.workers,
    )
    for question, why in fitted["not_fitted"].items():
        print(f"  {question}: not fitted ({why})")
    if not fitted["temperatures"]:
        raise MendariError(
            f"{where}: nothing was fitted, so nothing was written: a question needs "
            f"{agent_calibration.MIN_CASES} labelled cases the agent answered"
        )
    relative = (
        provider.settings.calibration or f"models/{args.slot}-{args.variant}-calibration.json"
    )
    target = agent.path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fitted, indent=2) + "\n", encoding="utf-8")
    for question, found in fitted["questions"].items():
        print(
            f"  {question}: T={fitted['temperatures'][question]} on {found['cases']} cases "
            f"(NLL {found['nll_before']} -> {found['nll_after']}, "
            f"ECE {found['ece_before']} -> {found['ece_after']} on the same cases)"
        )
    if fitted["failures"]:
        print(f"  failed calls: {fitted['failures']}")
    print(f"cost: ${fitted['cost_usd']}" if fitted["cost_usd"] is not None else "cost: unknown")
    service = fitted["service"]
    print(
        f"fitted for {service['id'] or 'the agent'} version {service['version']}: when its service "
        "reports another version, nothing is rescaled and no decision is automated until it is "
        "calibrated again"
    )
    print(f"-> {relative}")
    if provider.settings.calibration is None:
        print(f"add `calibration: {relative}` to the {where} variant to use it")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from mendari.evals import resolve_dataset
    from mendari.providers import build_provider
    from mendari.providers.agent import AgentProvider
    from mendari.providers.llm import LlmProvider, fit_calibration

    agent = resolve_agent(args.agent)
    provider = build_provider(agent, args.slot, args.variant)
    if isinstance(provider, AgentProvider):
        return _calibrate_agent(args, agent, provider)
    if not isinstance(provider, LlmProvider):
        raise MendariError(f"{args.slot}/{args.variant} is not an llm or an agent variant")
    provider.preflight()  # e.g. no model (there is no default): one line, before anything else
    dataset = resolve_dataset(agent, args.dataset)
    _calibration_set_note(agent, dataset.name)
    records = dataset.records[: args.limit] if args.limit else dataset.records
    print(f"asking {provider.model} about {len(records)} {dataset.name} records...")
    fitted = fit_calibration(provider, records, workers=args.workers)
    fitted["fitted_on"] = {
        "dataset": dataset.name,
        "sha256": dataset.sha256,
        "records": len(records),
    }
    relative = (
        provider.settings.calibration or f"models/{args.slot}-{args.variant}-calibration.json"
    )
    target = agent.path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fitted, indent=2) + "\n", encoding="utf-8")
    for question, stats in fitted["questions"].items():
        print(
            f"  {question}: T={fitted['temperatures'][question]} on {stats['examples']} examples "
            f"(NLL {stats['nll_before']} -> {stats['nll_after']})"
        )
    if fitted["failures"]:
        print(f"  failed calls: {fitted['failures']}")
    print(f"cost: ${fitted['cost_usd']}" if fitted["cost_usd"] is not None else "cost: unknown")
    print(f"-> {relative}")
    if provider.settings.calibration is None:
        print(f"add `calibration: {relative}` to the {args.slot}/{args.variant} variant to use it")
    return 0


def _write_ledger(out: Path, ledger: bytes, name: str) -> None:
    """The run's ledger in `ledgers/` beside the report at `out` (mendari compare reads it), and
    the one the report it replaces named removed, so a benchmark keeps exactly its own."""
    folder = out.parent / "ledgers"
    previous = None
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        previous = json.loads(out.read_text(encoding="utf-8"))["ledger"]["id"]
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(ledger)
    if isinstance(previous, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", previous):
        stale = folder / (previous.removeprefix("sha256:") + ".jsonl")
        if stale.name != name:
            stale.unlink(missing_ok=True)


def cmd_written_eval(args: argparse.Namespace) -> int:
    """`mendari written eval`: the agent writes (or the input is read), the judge grades, and
    the report says how far the judge agrees with clinicians. Nothing is recorded."""
    from mendari.evals import resolve_dataset, written
    from mendari.evals.ledger import case_hash
    from mendari.evals.render import written_report_text
    from mendari.providers.judge import Judge

    _written_resamples(args.resamples)  # before the agent or a judge is asked anything
    decision = _decision(args, origin_cap=False)  # the agent's own answers, as `mendari eval`
    agent = decision.agent
    if args.rubric not in agent.manifest.rubrics:
        known = ", ".join(agent.manifest.rubrics) or "none"
        raise MendariError(f"{agent.id} has no rubric {args.rubric!r} (its rubrics: {known})")
    dataset = resolve_dataset(agent, args.dataset)
    whole = dataset
    if args.limit:
        dataset = dataset.head(args.limit)
    judge = Judge(agent, args.rubric, mode=args.judge)
    workers = args.workers or (8 if decision.is_remote or not judge.canned else 1)
    run = written.run(decision, dataset, args.rubric, judge=judge, workers=workers)
    grades = _written_grades(args, agent, whole)
    # The bootstrap's seed is the report's constant default: no option chooses its draws.
    found = written.report(run.ledger, grades, resamples=args.resamples)
    report = written.run_report(found, run.ledger)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists() or out.is_file():  # a file: its ledger goes beside it, first
            _write_ledger(out, run.ledger.dumps(), run.ledger.file_name)
        else:  # a device such as /dev/stdout
            print("note: --out is not a file, so no ledger is written beside it", file=sys.stderr)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.json:
        _print_json(report)  # case hashes only
    else:
        ids = {case_hash(dataset.case_key, r["id"]): r["id"] for r in dataset.records}
        print(written_report_text(report, ids))
    return 0


def _written_resamples(resamples: int) -> None:
    """Refuse, before anything is run or changed, resamples a judge's state may not be decided
    on (`evals/written.py` `resamples_problem`: fewer than 2,000 is a few draws, not a bound)."""
    from mendari.evals.written import resamples_problem

    problem = resamples_problem(resamples)
    if problem is not None:
        raise MendariError(f"--resamples: {problem}")


def _written_store(target: str | None, *, writes: bool) -> tuple[Any, str]:
    """The deployment's database for `mendari written round`: given, or $DATABASE_URL. To write,
    it must be a server's (`_servers_database`: a mistyped path is refused) and is opened under
    the deployment's PHI mode, which decides how a round's texts are kept. Otherwise opened
    read-only: nothing is created, migrated or marked. Returns the round store
    (`core/written.py`) and the database's name as shown (no password)."""
    from mendari.core.phi import phi_policy
    from mendari.core.written import WrittenStore

    if target is None:
        target = os.environ.get("DATABASE_URL", "").strip() or None
    if target is None:
        raise MendariError("give --db (the server's database) or set DATABASE_URL")
    if not writes:
        return WrittenStore(_read_store(target)), _shown(target)
    shown = _servers_database(target, used=True, also=("written_rounds",))
    return WrittenStore(AuditStore(target, phi=phi_policy())), shown


def cmd_written_round_create(args: argparse.Namespace) -> int:
    """A new grading round of an organization, from one run: the agent writes for every case
    (or the input is read), the judge grades each text, and a seeded sample of the texts is kept
    in the server's database for clinicians to grade in the console. Prints ids and counts."""
    from mendari.evals import resolve_dataset, written
    from mendari.providers.judge import Judge

    store, shown = _written_store(args.db, writes=True)
    with store.store:
        if store.store.phi.mode == "hash":  # before a model is asked for anything
            raise MendariError(
                "PHI mode hash keeps no patient text: a round keeps the texts to grade; switch "
                "to encrypt"
            )
        if store.store.organizations() and store.store.organization(args.org) is None:
            raise MendariError(f"no organization {args.org} in {shown}: create it first")
        decision = _decision(args, origin_cap=False)
        agent = decision.agent
        if args.rubric not in agent.manifest.rubrics:
            known = ", ".join(agent.manifest.rubrics) or "none"
            raise MendariError(f"{agent.id} has no rubric {args.rubric!r} (its rubrics: {known})")
        dataset = resolve_dataset(agent, args.dataset)
        if args.limit:
            dataset = dataset.head(args.limit)
        judge = Judge(agent, args.rubric, mode=args.judge)
        workers = args.workers or (8 if decision.is_remote or not judge.canned else 1)
        run = written.run(decision, dataset, args.rubric, judge=judge, workers=workers)
        made = store.create_round(
            args.org,
            agent,
            args.rubric,
            run,
            sample=args.sample,
            seed=args.seed,
            created_by=_command_line_actor(),
        )
    _print_json(made)
    print(
        f"created in {shown}: give its items to graders with "
        f"`mendari written round assign {made['round']} --grader USER --grader USER`",
        file=sys.stderr,
    )
    return 0


def cmd_written_round_assign(args: argparse.Namespace) -> int:
    """Give an open round's items to graders, by user id: one grader an item, a seeded share
    of them a second. Prints how many assignments each grader got, never which items are graded
    twice."""
    store, shown = _written_store(args.db, writes=True)
    with store.store:
        made = store.assign(args.round_id, args.grader, double=args.double)
    _print_json(made)
    print(
        f"{made['assigned']} assignment(s) made and {made['withdrawn']} withdrawn in {shown}",
        file=sys.stderr,
    )
    return 0


def cmd_written_round_progress(args: argparse.Namespace) -> int:
    """How a round's grading stands: its items, its assignments by state and each grader's,
    by user id. Counts only; read-only."""
    store, _ = _written_store(args.db, writes=False)
    with store.store:
        found = store.progress(args.round_id)
    if found["note"]:
        print(f"note: {found['note']}", file=sys.stderr)
    _print_json(found)
    return 0


def cmd_written_round_close(args: argparse.Namespace) -> int:
    """Close a round: its report is computed from the judge's grades and the graders', and
    every text the round kept is erased. Prints the report (hashes, never a text)."""
    from mendari.evals.render import written_report_text

    _written_resamples(args.resamples)  # before the database is opened
    store, shown = _written_store(args.db, writes=True)
    with store.store:
        closed = store.close(args.round_id, by=_command_line_actor(), resamples=args.resamples)
    if args.json:
        _print_json(closed["report"])
    else:
        print(written_report_text(closed["report"]))
    print(
        f"closed in {shown}: {closed['graded']} grade(s) kept, {closed['erased']} text(s) erased",
        file=sys.stderr,
    )
    return 0


def cmd_written_round_list(args: argparse.Namespace) -> int:
    """Every round (of `--org`, of `--agent`), with its state and counts: ids and counts,
    read-only. A closed round's report is in the console and in `mendari written round close`'s
    answer; here, its hash. `note` says what a state needs said: a round left closing, with how
    to finish it, and a stored report that is not the one the audit trail committed to."""
    store, _ = _written_store(args.db, writes=False)
    with store.store:
        rounds = store.rounds(args.org, args.agent)
    for found in rounds:
        report = found.pop("report")
        found["report_sha256"] = None if report is None else report.get("result_sha256")
        if found["note"]:
            print(f"note: round {found['id']}: {found['note']}", file=sys.stderr)
    _print_json(rounds)
    return 0


def _written_grades(args: argparse.Namespace, agent: Agent, dataset: Any) -> Any:
    """The clinicians' grades a written-output run is compared with: `--grades`, else the file
    the agent declares for the rubric when its dataset is the one run, else none. A file of
    another dataset than the one run is refused, as one of another rubric wording is."""
    from mendari.evals import written

    manifest = agent.manifest
    scales = {
        key: [level for level, _ in criterion.scale()]
        for key, criterion in manifest.rubrics[args.rubric].criteria.items()
    }
    declared = manifest.evaluation.grades.get(args.rubric)
    if args.grades:
        where = args.grades
        try:
            raw = Path(where).read_bytes()
        except OSError as error:
            raise MendariError(f"--grades: cannot read the file ({type(error).__name__})") from None
    elif declared is not None and declared.dataset == args.dataset:
        where, raw = declared.file, agent.read_bytes(declared.file)
    else:
        return None
    header, grades = written.load_grades(raw, where=where, scales=scales)
    if header["rubric"]["name"] != args.rubric:
        raise MendariError(f"{where}: its grades are of another rubric than {args.rubric}")
    if header["dataset"]["sha256"] != dataset.sha256:
        raise MendariError(f"{where}: its grades are of another dataset than the one run")
    return grades


# A bootstrap's resamples on the command line: enough for any interval, never a run for hours.
MAX_RESAMPLES = 100_000


def cmd_compare(args: argparse.Namespace) -> int:
    from mendari.core.registry import resolve_agent
    from mendari.evals import paired
    from mendari.evals.harness import load_dataset, resolve_dataset
    from mendari.evals.ledger import case_hash

    if args.seed < 0 or not 0 < args.alpha < 1:
        raise MendariError("--seed is 0 or more, --alpha between 0 and 1")
    if not 1 <= args.resamples <= MAX_RESAMPLES:
        raise MendariError(f"--resamples is 1 to {MAX_RESAMPLES}")
    a = paired.load(args.a, flag="A")
    b = paired.load(args.b, flag="B")
    if Path(args.dataset).is_file():
        dataset = load_dataset(args.dataset)
    else:
        dataset = resolve_dataset(resolve_agent(a.header["agent"]["id"]), args.dataset)
    result = paired.compare(
        a, b, dataset_sha256=dataset.sha256, seed=args.seed, resamples=args.resamples,
        alpha=args.alpha,
    )  # fmt: skip
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.json:
        _print_json(result)  # case hashes only
    else:
        ids = {case_hash(dataset.case_key, r["id"]): r["id"] for r in dataset.records}
        print(paired.text(result, ids))
    return 0


def cmd_leaderboard(args: argparse.Namespace) -> int:
    from mendari.evals import evaluate, leaderboard, resolve_dataset

    agent = resolve_agent(args.agent)
    reports = [_read_json(path, "--report", shape=dict) for path in args.report]
    datasets = []
    for name in args.dataset:
        dataset = resolve_dataset(agent, name)
        datasets.append(dataset.head(args.limit) if args.limit else dataset)
    for run in args.run:
        providers = _providers(agent, parse_providers(run.split(",")))
        decision = Decision(agent, providers=providers, origin_cap=False)  # the agent's policy
        workers = args.workers or (8 if decision.is_remote else 1)
        for dataset in datasets:
            report = evaluate(decision, dataset, workers=workers)
            reports.append(report)
            errors = report.get("errors") or {}
            status = f"{sum(errors.values())} errors {errors}" if errors else "done"
            print(f"{leaderboard.config_label(report['providers'])} on {dataset.name}: {status}")
    if not reports:
        raise MendariError("nothing to add: pass --run and --dataset, or --report")
    target = Path(args.out) if args.out else agent.attestation(leaderboard.PATH)
    existing = None
    if target.is_file() and not args.fresh:
        existing = _read_json(str(target), "--out", shape=dict)
    board = leaderboard.build(agent, reports, existing)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
    # The card's opening, then its tables: a regression check, not a ranking (LP-17g).
    print(leaderboard.preface(), end="\n\n")
    print("\n".join(leaderboard.markdown(board, agent.digest, agent.version)))
    print(f"-> {target}")
    return 0


def cmd_data_coverage(args: argparse.Namespace) -> int:
    """What a labelled set covers (`mendari.coverage/1`, LP-11): distinct cases and positives,
    duplicates that disagree, and the language, source and stratum mix. Read-only."""
    from mendari.evals import resolve_dataset
    from mendari.evals.coverage import coverage, coverage_text

    agent = resolve_agent(args.agent)
    dataset = resolve_dataset(agent, args.dataset)
    report = coverage(agent.manifest, dataset, agent=agent.ref, ids=args.ids)
    if args.json:
        _print_json(report)
    else:
        print(coverage_text(report))
    return 0


def cmd_data_check(args: argparse.Namespace) -> int:
    """A dataset checked against its agent (`mendari.dataset-check/1`, LP-20c): every dataset the
    agent declares (none is nothing to check), or one file. Exit 1 on a schema or label problem;
    duplicates, overlap with the training data and labels too thin to estimate are reported and
    do not fail. `--phi` also runs the PHI detector over each file (`mendari.phi-check/1`,
    LP-20d: kinds, files and lines, never a value), and a finding it rejects fails too."""
    from mendari.core import phi_detect
    from mendari.evals import dataset_checks
    from mendari.evals.harness import load_dataset, resolve_dataset

    agent = resolve_agent(args.agent)
    if args.file:
        path = Path(args.file)
        datasets = [(load_dataset(path), path.name, path.read_bytes())]
    else:
        declared = agent.manifest.evaluation.datasets
        datasets = [
            (resolve_dataset(agent, name), str(file), agent.read_bytes(file))
            for name, file in declared.items()
        ]
        if not datasets:
            print(f"{agent.ref} declares no dataset: nothing to check", file=sys.stderr)
            return 0
    reports = []
    failed = False
    for dataset, label, raw in datasets:
        report = dataset_checks.check(agent, dataset)
        failed |= not report.passed
        data = dict(report.data)
        text = dataset_checks.text(report)
        if args.phi:
            found = phi_detect.detect_files({label: raw})
            data["phi"] = found.to_json()
            failed |= phi_detect.rejects(found)
            text += "\n" + _phi_text(found)
        reports.append((data, text))
    if args.json:
        _print_json([data for data, _ in reports])
    else:
        print("\n\n".join(text for _, text in reports))
    return 1 if failed else 0


def _phi_text(report: Any) -> str:
    """A PHI report as the command line prints it: kinds, files and lines, never a value."""
    from mendari.core import phi_detect

    if not report.findings:
        return "phi: nothing that looks like a person's details"
    counts = ", ".join(f"{kind} {count}" for kind, count in report.counts.items())
    verdict = " (rejected)" if phi_detect.rejects(report) else ""
    lines = [f"phi: {counts}{verdict}"]
    lines += [f"  {f.file}:{f.line}: {f.kind}" for f in report.findings[:50]]
    return "\n".join(lines)


def cmd_card(args: argparse.Namespace) -> int:
    from mendari.core.card import card_of

    agent = resolve_agent(args.agent)
    card = card_of(agent)
    if args.write:
        target = agent.root / "DECISION_CARD.md"
        if target.is_symlink():  # the write would land wherever the link points
            raise MendariError(f"{target} is a symbolic link; not writing through it")
        target.write_text(card, encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(card)
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    agent = resolve_agent(args.agent)
    _print_json(agent.output_schema if args.output else agent.input_schema)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn  # noqa: F401 - there before any work (_run_server runs it)
    except ImportError:
        raise MendariError(
            "the server needs uvicorn, which installs with Mendari and is missing here: "
            "install Mendari again (pip install mendari; in a clone: uv sync)"
        ) from None
    from mendari.server.app import create_app

    # Behind a proxy, the client's address (rate limits per address, the log) comes from
    # X-Forwarded-For, but only from the proxies trusted here. The environment carries the
    # choice to /api/health (`client_ip_source`), as it would for uvicorn run directly.
    trusted = args.forwarded_allow_ips or os.environ.get("FORWARDED_ALLOW_IPS") or "127.0.0.1"
    os.environ["FORWARDED_ALLOW_IPS"] = trusted if args.proxy_headers else ""
    app = create_app(
        db_path=args.db,
        providers=parse_providers(args.provider),
        budget_limits=daily_limits(),
        datasets_dir=Path(args.datasets_dir) if args.datasets_dir else example_datasets_dir(),
    )
    # A typed --provider that no served agent takes (an unknown slot, a variant no agent with the
    # slot has) is a mistake; MENDARI_PROVIDERS, shared by a deployment's instances, is only
    # reported (/api/health, the startup log).
    not_taken = app.state.provider_settings_not_taken
    if args.provider and not_taken:
        raise MendariError(f"--provider settings no served agent takes: {', '.join(not_taken)}")
    # No uvicorn access log: its line carries the query string (a sign-in callback's code and
    # state, a search's values). Mendari's own access line (server/security_log.py) replaces it.
    _run_server(
        app,
        args.host,
        args.port,
        access_log=False,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=trusted,
    )
    return 0


def _run_server(app: Any, host: str, port: int, **options: Any) -> None:
    """uvicorn, whose failure to start (a port in use, a host it cannot bind) is one line and
    exit 1 like any other error, not its own exit code."""
    import uvicorn

    try:
        uvicorn.run(app, host=host, port=port, log_level="info", **options)
    except SystemExit as stop:
        if stop.code not in (None, 0):
            raise MendariError(
                f"could not listen on {host}:{port} (in use, or not allowed here): choose another "
                "--port or --host"
            ) from None
        raise


def cmd_audit_verify(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy

    # Notes sealed under encrypt are checked with this deployment's keys. Verifying opens the
    # database read-only: it creates, migrates and marks nothing (no key a stray setting holds is
    # recorded as known), and an older database is verified as it is.
    head = None
    if (args.against is None) != (args.public_key is None):
        raise MendariError("--against and --public-key go together: a statement and its key")
    if args.against is not None and args.since_checkpoint:
        raise MendariError("--against needs a full verification: leave out --since-checkpoint")
    if args.against is not None:  # a signed checkpoint (LP-21k), checked before the chain
        from mendari.core import checkpoint

        statement = _read_json(args.against, "--against", shape=dict)
        if isinstance(statement.get("statement"), dict):  # as `/api/audit/checkpoint` answers
            statement = statement["statement"]
        problem = checkpoint.problem(statement, checkpoint.load_public(args.public_key))
        if problem is not None:
            raise MendariError(f"--against: {problem}")
        head = statement["head"]
    with _read_store(args.db, phi=phi_policy() if args.notes else None) as store:
        status = store.verify(notes=args.notes, since_checkpoint=args.since_checkpoint, head=head)
    _print_json(status)
    return 0 if status["ok"] else 1


def cmd_audit_checkpoint(args: argparse.Namespace) -> int:
    """Verify from the last checkpoint and, when the chain holds, record its head as the next
    one (LP-21j), outside the chain; exit 1 when it is broken (nothing recorded). Opening the
    database migrates it, as every command that writes does; the checkpoint is the one row
    written: nothing is marked or chained."""
    from mendari.core.checkpoint import checkpoint_key

    key = checkpoint_key()  # a malformed key fails before anything is opened
    _servers_database(args.db, used=False, tables=_EVERY_RELEASE)  # mistyped: refused
    with AuditStore(args.db, marks=False) as store:
        status = store.record_checkpoint(signer=key)
    _print_json(status)
    if status.get("statement_refused") is not None:
        raise MendariError(
            f"the day's stored checkpoint statement is refused ({status['statement_refused']}): "
            "someone with the database changed it; follow the incident runbook"
        )
    return 0 if status["ok"] else 1


def _read_store(target: str, *, phi: Any = None) -> AuditStore:
    """The server's audit database opened read-only (LP-15): a missing file or a database that is
    not Mendari's is one line, and nothing is created or written, whatever the command does."""
    import sqlite3

    try:
        return AuditStore(target, phi=phi, read_only=True)
    except sqlite3.Error as error:
        raise MendariError(
            f"cannot read the database {_shown(target)} ({type(error).__name__}): is it the "
            "server's --db, and readable?"
        ) from None
    except Exception as error:
        if type(error).__module__.startswith("psycopg"):
            raise MendariError(
                f"cannot read the database {_shown(target)} ({type(error).__name__}): is the "
                "URL right and the server reachable?"
            ) from None
        raise


_LIBPQ_SECRET = re.compile(
    r"(?i)\b(password|pass|passwd|sslpassword)\s*=\s*(?:'(?:\\.|[^'])*'|\S+)"
)


def _shown(target: str) -> str:
    """A database target as it is printed: a URL or libpq DSN without its password."""
    if "://" not in target:
        return _LIBPQ_SECRET.sub(r"\1=***", target)
    parts = urlsplit(target)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        netloc = f"{netloc}:{port}"
    if parts.username:
        netloc = f"{parts.username}@{netloc}"
    query = "&".join(
        f"{key}=***"
        if key.lower() in ("password", "pass", "passwd", "sslpassword")
        else f"{key}={value}"
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


_MENDARI_TABLES = ("decisions", "deployments", "automation_holds")
# Two tables every Mendari database has: a database without them is not a server's.
_EVERY_RELEASE = ("audit_events", "decisions")


def _probe(
    target: str, tables: tuple[str, ...] = (), also: tuple[str, ...] = ()
) -> tuple[bool, bool]:
    """(the database has Mendari's `tables`, they hold anything: a decision, a deployment or a
    hold, or a row of `also`'s tables that it has), read without writing: nothing is created or
    migrated in a database that is not Mendari's."""
    tables = tables or _MENDARI_TABLES
    if target.startswith(("postgres://", "postgresql://")):
        import psycopg

        with psycopg.connect(target) as connection:
            found = connection.execute(
                "SELECT count(*) FROM unnest(%s::text[]) AS t WHERE to_regclass(t) IS NOT NULL",
                (list(tables),),
            ).fetchone()
            if not found or found[0] != len(tables):
                return False, False
            present = [
                table
                for table in also
                if (connection.execute("SELECT to_regclass(%s)", (table,)).fetchone() or (None,))[0]
            ]
            rows = sum(
                (connection.execute(f"SELECT count(*) FROM {table}").fetchone() or (0,))[0]
                for table in (*tables, *present)
            )
        return True, rows > 0
    import sqlite3

    # as_uri() escapes what a URI would read otherwise ("#", "?", "%", spaces).
    lite = sqlite3.connect(Path(target).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        names = {
            row[0] for row in lite.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not set(tables) <= names:
            return False, False
        rows = 0
        for table in (*tables, *(table for table in also if table in names)):
            counted = lite.execute(f"SELECT count(*) FROM {table}").fetchone()
            rows += counted[0] if counted else 0
        return True, rows > 0
    finally:
        lite.close()


def _hold_store(target: str | None, *, serving: bool) -> tuple[AuditStore, str]:
    """The deployment's database for the break-glass (holds, revocations, ending sessions),
    refused when it cannot be the one a server runs on: not given, a SQLite file that does not
    exist (it would be created, and hold nothing), a database without Mendari's tables (nothing
    is created in it), and, to set a hold (`serving`), one that holds no decision, deployment or
    hold. Returns it and its name as shown (no password)."""
    target = target or os.environ.get("DATABASE_URL", "").strip() or None
    if target is None:
        raise MendariError("give --db (the server's database) or set DATABASE_URL")
    shown = _servers_database(target, used=serving)
    return AuditStore(target), shown


def _servers_database(
    target: str,
    *,
    used: bool,
    tables: tuple[str, ...] | None = None,
    also: tuple[str, ...] = (),
) -> str:
    """`target` as shown (no password) when it can be a server's database: it exists, has
    Mendari's tables (`tables`; read before anything is created in it) and, with `used`, holds
    a decision, a deployment or a hold, or a row of `also`'s tables (a written round's or an
    agent store's commands: a database whose only rows are theirs is a server's too). A
    `MendariError` saying why when it cannot."""
    shown = _shown(target)
    if not target.startswith(("postgres://", "postgresql://")) and not Path(target).is_file():
        raise MendariError(f"no database at {shown}: give the server's --db")
    try:
        mendari, held = _probe(target, tables or _MENDARI_TABLES, also)
    except Exception as error:
        raise MendariError(f"cannot read {shown} ({type(error).__name__})") from None
    if not mendari:
        raise MendariError(f"{shown} is not a Mendari database")
    if used and not held:
        raise MendariError(f"{shown} has never been used: is it the server's?")
    return shown


def _command_line_actor() -> str:
    user = re.sub(r"[^A-Za-z0-9._-]", "", os.environ.get("USER", ""))[:64] or "unknown user"
    return f"command line ({user})"


def cmd_automation_hold(args: argparse.Namespace) -> int:
    """The break-glass: written straight to the database, so it works with the server down.
    Running servers see it within a few seconds."""
    if args.digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", args.digest):
        raise MendariError("--digest is an agent's digest (sha256:<64 hex>)")
    store, shown = _hold_store(args.db, serving=True)
    if args.agent is not None:
        served = {d["agent_digest"] for d in store.deployments(args.agent, limit=1000)}
        if not served:
            raise MendariError(f"{shown} has never served {args.agent}")
        if args.digest is not None and args.digest not in served:
            raise MendariError(f"{shown} has never served {args.agent} at {args.digest}")
    org = None if args.all_orgs else args.org
    hold = store.hold_automation(
        org=org,
        agent_id=args.agent,
        agent_digest=args.digest,
        reason=args.reason,
        set_by=_command_line_actor(),
    )
    _print_json(hold)
    print(f"held in {shown}", file=sys.stderr)
    return 0


def cmd_automation_lift(args: argparse.Namespace) -> int:
    store, shown = _hold_store(args.db, serving=False)
    if not store.lift_hold(args.hold_id, lifted_by=_command_line_actor(), org=None):
        raise MendariError(f"no hold {args.hold_id} in force in {shown}")
    print(f"lifted {args.hold_id} in {shown}", file=sys.stderr)
    return 0


def cmd_automation_list(args: argparse.Namespace) -> int:
    store, _ = _hold_store(args.db, serving=False)
    _print_json(store.holds(everyone=True))
    return 0


def _access_store(target: str | None, *, writes: bool = True) -> tuple[AuditStore, str]:
    """The deployment's database for `mendari access`: given, or $DATABASE_URL. Unless the
    command `writes`, opened read-only: nothing is created, migrated or marked, and an older
    database is read as it is. To write, it must have Mendari's tables (nothing is created in
    another database) and is opened under this deployment's PHI settings, read but never marked
    from here: a database a PHI mode protects takes an address's digest keyed with the
    deployment's input-hash key only (`MENDARI_INPUT_HASH_KEY`). Returns it and its name as
    shown (no password)."""
    from mendari.core.phi import phi_policy

    target = target or os.environ.get("DATABASE_URL", "").strip() or None
    if target is None:
        raise MendariError("give --db (the server's database) or set DATABASE_URL")
    if not writes:
        return _read_store(target), _shown(target)
    shown = _servers_database(target, used=False, tables=_EVERY_RELEASE)  # mistyped: refused
    policy = phi_policy()
    store = AuditStore(target, phi=policy, marks=False)
    if store.protected and not policy.hash_key:
        store.close()
        raise MendariError(
            f"{shown} is protected (PHI mode {store.protected}): run this with the deployment's "
            "MENDARI_INPUT_HASH_KEY, so the chain holds keyed digests of e-mail addresses"
        )
    return store, shown


def _access_call(make: Any) -> Any:
    """A store call's refusal (a bad id, role or address) as a one-line error."""
    try:
        return make()
    except ValueError as error:
        raise MendariError(str(error)) from None


def cmd_access_org_create(args: argparse.Namespace) -> int:
    store, shown = _access_store(args.db)
    created = _access_call(
        lambda: store.create_organization(
            org=args.org, name=args.name, created_by=_command_line_actor()
        )
    )
    _print_json(created)
    print(f"created in {shown}", file=sys.stderr)
    return 0


def cmd_access_org_list(args: argparse.Namespace) -> int:
    store, _ = _access_store(args.db, writes=False)
    _print_json(store.organizations())
    return 0


def cmd_access_bootstrap(args: argparse.Namespace) -> int:
    """The owner in their existing data (`default`): its record and an administrator's
    membership for the e-mail address they sign in with, verified. Run it again: it adds
    nothing twice and rewrites nothing."""
    store, shown = _access_store(args.db)
    done = _access_call(lambda: store.bootstrap(email=args.email, created_by=_command_line_actor()))
    _print_json(done)
    if done["organization"] is None and done["membership"] is None:
        print(f"nothing to do in {shown}: already bootstrapped", file=sys.stderr)
    else:
        print(f"bootstrapped in {shown}", file=sys.stderr)
    return 0


def cmd_access_invite(args: argparse.Namespace) -> int:
    """An invitation link to pass on: Mendari sends no e-mail. Shown once."""
    store, _ = _access_store(args.db)
    made = _access_call(
        lambda: store.create_invitation(
            org=args.org,
            email=args.email,
            roles=args.role,
            created_by=_command_line_actor(),
            days=args.days,
        )
    )
    token = made.pop("token")
    _print_json(made)
    print(f"the link (shown once): <the deployment's address>/invite#{token}", file=sys.stderr)
    return 0


def cmd_access_members(args: argparse.Namespace) -> int:
    store, _ = _access_store(args.db, writes=False)
    _print_json(store.memberships(org=args.org))
    return 0


def cmd_access_requests(args: argparse.Namespace) -> int:
    store, _ = _access_store(args.db, writes=False)
    _print_json(store.access_requests(args.org))
    return 0


def cmd_access_check(args: argparse.Namespace) -> int:
    """The preflight before memberships mode (read-only): each organization the database's keys,
    clients, approvals, holds, decisions and evaluation reports name, and the EHR registrations
    of the environment it runs in (`MENDARI_SMART_EHRS`), with counts, which have no record, and
    which cannot have one. Ids and counts only."""
    from mendari.core import accounts
    from mendari.standards import smart

    store, shown = _access_store(args.db, writes=False)  # a preflight writes nothing
    in_use = {entry["org"]: entry for entry in store.organizations_in_use()}
    recorded = {org["id"] for org in store.organizations()}
    for registration in smart.smart_ehrs().values():
        entry = in_use.setdefault(
            registration.org, {"org": registration.org, "recorded": registration.org in recorded}
        )
        entry["smart_ehrs"] = entry.get("smart_ehrs", 0) + 1
        entry.setdefault("recordable", accounts.is_org_id(registration.org))
    report = sorted(in_use.values(), key=lambda entry: entry["org"])
    _print_json(report)
    missing = [entry["org"] for entry in report if not entry["recorded"]]
    unrecordable = [entry["org"] for entry in report if not entry["recordable"]]
    if unrecordable:
        print(
            f"{shown}: {', '.join(unrecordable)} cannot have a record (an organization id is "
            "lower-case letters, digits and hyphens): its keys, clients and data stay under that "
            "id; do not switch to memberships mode until the owner decides what becomes of it",
            file=sys.stderr,
        )
    if missing:
        print(f"{shown}: no record for {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"{shown}: every organization in use has a record", file=sys.stderr)
    return 0


def cmd_access_keys(args: argparse.Namespace) -> int:
    """Every API key (of `--org`) with its status now (read-only): never a secret or its hash."""
    store, _ = _access_store(args.db, writes=False)
    _print_json(store.api_keys(args.org))
    return 0


def cmd_access_clients(args: argparse.Namespace) -> int:
    """Every Backend Services client (of `--org`): its keys' ids and thumbprints (read-only)."""
    store, _ = _access_store(args.db, writes=False)
    _print_json(store.clients(args.org))
    return 0


def cmd_access_revoke(args: argparse.Namespace) -> int:
    """The break-glass for credentials (LP-21l): written straight to the database, for any
    organization, so it works with the server down. A key or client stops working at once on
    every instance (each request reads it), and a client's access tokens go with it. Each is
    chained with who and why (`key.revoked`, `client.revoked`)."""
    reason = _access_call(lambda: break_glass_reason(args.reason))
    store, shown = _hold_store(args.db, serving=False)
    actor = _command_line_actor()
    revoked: dict[str, list[str]] = {"keys": [], "clients": []}
    if args.what == "key":
        if not store.revoke_api_key(args.id, revoked_by=actor, reason=reason):
            raise MendariError(f"no key {args.id} in force in {shown}")
        revoked["keys"].append(args.id)
    elif args.what == "client":
        if not store.revoke_client(args.id, revoked_by=actor, reason=reason):
            raise MendariError(f"no client {args.id} in force in {shown}")
        revoked["clients"].append(args.id)
    else:  # every key and client of an organization still in force
        for key in store.api_keys(args.org):
            if key["status"] in ("active", "replaced") and store.revoke_api_key(
                key["id"], args.org, revoked_by=actor, reason=reason
            ):
                revoked["keys"].append(key["id"])
        for client in store.clients(args.org):
            if client["revoked_at"] is None and store.revoke_client(
                client["client_id"], args.org, revoked_by=actor, reason=reason
            ):
                revoked["clients"].append(client["client_id"])
        if not revoked["keys"] and not revoked["clients"]:
            raise MendariError(f"no key or client of {args.org} in force in {shown}")
    _print_json(revoked)
    print(f"revoked in {shown}", file=sys.stderr)
    return 0


def cmd_access_end_sessions(args: argparse.Namespace) -> int:
    """The break-glass for sessions (LP-21l): signs out every person (`people`: sessions and
    sign-ins under way), every EHR launch (`launches`), or both (`all`), on every instance at
    once. Chained (`sessions.ended`) with the kinds, how many, who and why."""
    reason = _access_call(lambda: break_glass_reason(args.reason))
    # A server signs people in before its first decision: any Mendari database, named below.
    store, shown = _hold_store(args.db, serving=False)
    ended = store.end_sessions(args.kind, ended_by=_command_line_actor(), reason=reason)
    _print_json(ended)
    print(f"ended {ended['ended']} in {shown}", file=sys.stderr)
    return 0


def cmd_audit_export(args: argparse.Namespace) -> int:
    from mendari.standards import audit_fhir

    if args.administrative and args.agent:
        print("--agent is for decisions' events, not --administrative ones", file=sys.stderr)
        return 2
    bounds = {"since": args.since, "until": args.until}
    # An older database is exported as it is. With --org, a decision's events are read acting
    # for it (the export reads page by page, so for as long as it writes).
    with _read_store(args.db) as store, contextlib.ExitStack() as acting:
        if args.administrative:  # one organization's (--org), or every event on no decision
            events = store.iter_administrative_events(args.org, **bounds)
            resources = audit_fhir.administrative_resources(events)
        else:
            if args.org is not None:
                acting.enter_context(tenancy.acting_for(args.org))
            resources = audit_fhir.resources(store.iter_events(agent_id=args.agent, **bounds))
        lines = (json.dumps(r, separators=(",", ":")) + "\n" for r in resources)
        if not args.out:
            sys.stdout.writelines(lines)
            return 0
        count = 0
        with open(args.out, "w", encoding="utf-8") as out:
            for line in lines:
                out.write(line)
                count += 1
    print(f"{count} resources written to {args.out}", file=sys.stderr)
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """The deployment's totals, read-only: today's metered calls (LLM) against this
    environment's daily limits, the last purge with its counts, the latest deployment records,
    and the usage ledger (LP-21g) of every organization (or `--org`) over the last `--days`
    local days, with its totals by provider type and purpose (a cost only where every call was
    priced). In memberships mode the server shows the deployment's to no organization
    (ADR-0008); an organization's administrators see their own ledger (`GET /api/usage`)."""
    from datetime import timedelta

    from mendari.core.audit import usage_totals

    if not 1 <= args.days <= 366:
        raise MendariError("--days is 1 to 366")
    limits = daily_limits()
    until = clock.today()
    since = until - timedelta(days=args.days - 1)
    with _read_store(args.db) as store:
        rows = store.usage_ledger(args.org, since=since.isoformat(), until=until.isoformat())
        _print_json(
            {
                "day": clock.today().isoformat(),
                **{
                    provider: {
                        "calls_today": store.usage(provider),
                        "daily_limit": limits[provider],
                    }
                    for provider in ("llm",)
                },
                "last_purge": store.last_purge(),
                "deployments": store.deployments(None, limit=args.deployments),
                "ledger": {
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "rows": rows,
                    "totals": usage_totals(rows),
                },
            }
        )
    return 0


MAX_EXPORTED = 100_000  # the most decisions one `feedback export` reads
# Said with every export that wrote a record (`core/site_cases.py`).
EXPORTED_CASES = (
    "These are a site's cases, and each record says so (`attributes.exported_by`): keep the "
    "file outside the agent's folder, since `mendari archive build` and `mendari push` refuse "
    "an agent that holds one."
)


def cmd_feedback_export(args: argparse.Namespace) -> int:
    from mendari.evals.feedback import feedback_dataset, reviewed_dataset

    reviews = args.source == "reviews"
    if reviews and args.rating is not None:
        raise MendariError("--rating filters ratings: leave it out with --source reviews")
    if not 1 <= args.limit <= MAX_EXPORTED:
        raise MendariError(f"--limit is 1 to {MAX_EXPORTED:,}")
    agent = resolve_agent(args.agent)
    # With --org, one organization's decisions: what an agent learns from is one site's.
    with _read_store(args.db) as store, contextlib.ExitStack() as acting:
        if args.org is not None:
            acting.enter_context(tenancy.acting_for(args.org))
        if reviews:
            records, skipped = reviewed_dataset(store, agent_id=agent.id, limit=args.limit)
        else:
            records, skipped = feedback_dataset(
                store, agent_id=agent.id, rating=args.rating or "down", limit=args.limit
            )
    _write_jsonl(Path(args.out), records)
    counted = (
        f"{len(records)} reviewed decision{'' if len(records) == 1 else 's'}"
        if reviews
        else f"{len(records)} rated decisions"
    )
    print(
        f"{counted} -> {args.out}"
        + (f" ({skipped} skipped: no input to export, kept as a hash or sealed)" if skipped else "")
    )
    if reviews:
        if len(records) + skipped == args.limit:
            print(f"These are the newest {args.limit:,}: raise --limit for more.")
        if records:
            print(
                "Each carries the label a person chose. Use the file with `mendari eval --dataset`."
            )
            print(EXPORTED_CASES)
        return 0
    labelled = sum(bool(record["labels"]) for record in records)
    if labelled:
        print(f"{labelled} already carry the right answer from feedback.")
    if labelled < len(records):
        print(f"Fill in `labels` for the {'other ' if labelled else ''}{len(records) - labelled}.")
    if records:
        print("Then use the file with `mendari eval --dataset`.")
        print(EXPORTED_CASES)
    return 0


def _served_id(agent: str) -> str:
    """The id an engine serves an agent under, from what a sandbox command was given: a
    directory's own id, or the id as typed (the agent need not be on this machine)."""
    from mendari.core.agent import has_manifest

    return resolve_agent(agent).id if has_manifest(Path(agent)) else agent.partition("@")[0]


def cmd_sandbox_seed(args: argparse.Namespace) -> int:
    from mendari.sandbox import fhir_workflow

    created = fhir_workflow.seed(
        args.fhir,
        count=args.count,
        wait_seconds=args.wait,
        agent=args.agent,
        dataset=args.dataset,
    )
    print(f"seeded {created} synthetic messages of {_served_id(args.agent)} into {args.fhir}")
    return 0


def cmd_sandbox_worker(args: argparse.Namespace) -> int:
    from mendari.sandbox import fhir_workflow

    fhir_workflow.run_worker(
        args.fhir,
        args.engine,
        agent_id=_served_id(args.agent),
        interval=args.interval,
        once=args.once,
    )
    return 0


def cmd_sandbox_cds_client(args: argparse.Namespace) -> int:
    """The signed sandbox CDS client (LP-23e): its registry entry, then what each call got, and
    each problem on stderr (exit 1). A server it cannot reach is `main`'s one error line, which
    names its origin only."""
    from mendari.sandbox import cds_client

    if args.limit < 1:
        raise MendariError("--limit is at least 1: the client calls the hook for each case")
    agent = resolve_agent(args.agent)
    with cds_client.SandboxClient(listen=args.listen, iss=args.iss) as client:
        entry = client.entry(client_id=args.id, org=args.org, fhir_server=args.fhir)
        print(f"MENDARI_CDS_CLIENTS entry: {json.dumps(entry)}", flush=True)
        calls = cds_client.sandbox_calls(agent, args.fhir, limit=args.limit)
        result = client.run(engine=args.engine, fhir=args.fhir, calls=calls, audience=args.audience)
    _print_json(result)
    for problem in result["problems"]:
        print(f"cds-client: {problem}", file=sys.stderr)
    return 1 if result["problems"] else 0


def cmd_sandbox_smart_launch(args: argparse.Namespace) -> int:
    from mendari.sandbox import smart_launch

    if args.launch_url:
        launched = smart_launch.from_launch_url(args.launch_url, engine=args.engine)
        _print_json(launched)
        failed = [
            problem
            for problem, ok in (
                ("the patient in context was not read", launched["patient_read"]),
                ("the session did not end at logout", launched["session_ended"]),
            )
            if not ok
        ]
        for problem in failed:
            print(f"smart-launch: {problem}", file=sys.stderr)
        return 1 if failed else 0
    if not (args.launcher and args.fhir):
        raise MendariError("smart-launch needs --launcher and --fhir, or --launch-url")
    result = smart_launch.run(
        engine=args.engine,
        launcher=args.launcher,
        fhir=args.fhir,
        agent=resolve_agent(args.agent),
        patient=args.patient,
        limit=args.limit,
        expect_org=args.expect_org,
        db=args.db,
    )
    _print_json(result)
    problems = []
    if not result["decided"]:
        problems.append("no work item was decided")
    if not result["session_ended"]:
        problems.append("the session did not end at logout")
    if args.strict and any(item["input_issues"] for item in result["decided"]):
        problems.append("a decided input did not conform to US Core")
    if args.expect_org is not None:
        for item, actor in zip(result["decided"], result["requested_by"], strict=True):
            if actor is None:
                problems.append(f"{item['decision_id']} is not {args.expect_org}'s or names nobody")
            elif not smart_launch.names_launcher_user(actor, args.launcher):
                problems.append(f"{item['decision_id']} names {actor!r}, not <issuer>|<subject>")
    for problem in problems:
        print(f"smart-launch: {problem}", file=sys.stderr)
    return 1 if problems else 0


def cmd_smart_check(args: argparse.Namespace) -> int:
    """Whether a launch from an EHR's FHIR base would work with this deployment's settings."""
    from mendari.server import smart_check

    findings = smart_check.check(args.base)
    for finding in findings:
        print(f"{finding.status:<7}  {finding.check:<16}  {finding.detail}")
    return 1 if any(finding.status == "fail" for finding in findings) else 0


def cmd_sandbox_pilot_check(args: argparse.Namespace) -> int:
    from mendari.sandbox import pilot_check

    try:
        result = pilot_check.run(
            engine=args.engine, messages=args.messages, agent=args.agent, approve=args.approve
        )
    except MendariError as failure:
        print(f"pilot-check: {failure}", file=sys.stderr)
        return 1
    _print_json(result)
    return 0


def cmd_bulk_export(args: argparse.Namespace) -> int:
    import os

    from mendari.standards import bulk
    from mendari.standards.fhir_client import GuardedHttp

    with GuardedHttp() as http:
        token = None
        if args.client_id:
            key = Path(args.private_key).read_text() if args.private_key else None
            key = key or os.environ.get("MENDARI_BULK_PRIVATE_KEY", "")
            if not key:
                raise MendariError("backend services need --private-key (or its env var)")
            token = bulk.backend_token(
                args.fhir,
                client_id=args.client_id,
                private_key=key,
                key_id=args.key_id,
                scope=args.scope,
                http=http,
            )
        types = [t.strip() for t in (args.type or "").split(",") if t.strip()]
        manifest = bulk.export(
            args.fhir,
            Path(args.out),
            http=http,
            group=args.group,
            types=types,
            since=args.since,
            token=token,
        )
    for output in manifest.get("output", []):
        print(f"{output['count']:>7} {output['type']:<20} {Path(args.out) / output['file']}")
    if manifest["delete"] == "deleted":
        print("the server was asked to delete the export, and accepted")
    else:  # the files are here; the server drops its copy when the export expires
        print(f"the server did not delete the export ({manifest['delete']})", file=sys.stderr)
    return 0


def cmd_bulk_dataset(args: argparse.Namespace) -> int:
    from mendari.standards import bulk
    from mendari.standards.fhir import FhirAdapter

    adapter = FhirAdapter.for_agent(resolve_agent(args.agent))
    resources = list(bulk.read_ndjson(Path(args.ndjson)))
    labels = bulk.load_labels(Path(args.labels))
    records, skipped = bulk.dataset(adapter, resources, labels, key_system=args.key_system)
    if not records:
        raise MendariError(f"no labelled cases for {args.agent} in {args.ndjson} ({skipped})")
    _write_jsonl(Path(args.out), records)
    left = ", ".join(f"{n} {why.replace('_', ' ')}" for why, n in skipped.items() if n)
    print(f"{len(records)} cases written to {args.out}" + (f" (left out: {left})" if left else ""))
    return 0


def cmd_sandbox_bulk_check(args: argparse.Namespace) -> int:
    from mendari.sandbox import bulk_check

    result = bulk_check.run(args.fhir, resolve_agent(args.agent), args.dataset)
    _print_json(result)
    if not result["identical"]:
        differences = ", ".join(result["differences"])
        print(f"bulk-check: the export evaluates differently ({differences})", file=sys.stderr)
    return 0 if result["identical"] else 1


def cmd_keys_generate(args: argparse.Namespace) -> int:
    import os

    from mendari.hub import archive

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    private, public = archive.generate_key()
    secret = out / f"{args.name}.key"
    if secret.exists():
        raise MendariError(f"{secret} exists; not overwriting a key")
    descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(private)
    (out / f"{args.name}.pub").write_bytes(public)
    key_id = archive.key_id(archive.load_public_key(public))
    print(f"{key_id}\n  private: {secret} (keep it secret)\n  public:  {out / args.name}.pub")
    return 0


def cmd_agent_build(args: argparse.Namespace) -> int:
    import hashlib

    from mendari.hub import archive

    agent = resolve_agent(args.agent)
    key = archive.load_private_key(Path(args.key).read_bytes())
    data = archive.build(agent, key)
    out = Path(args.out) / archive.archive_name(agent)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"{out}  sha256:{hashlib.sha256(data).hexdigest()}  ({len(data)} bytes)")
    return 0


def cmd_agent_verify(args: argparse.Namespace) -> int:
    import tempfile

    from mendari.hub import archive

    public = archive.load_public_key(Path(args.public_key).read_bytes())
    verified = archive.verify(Path(args.archive).read_bytes(), {archive.key_id(public): public})
    with tempfile.TemporaryDirectory() as scratch:
        agent = archive.extract(verified, Path(scratch), origin="hub")  # as a puller loads it
        print(f"OK {agent.id}@{agent.version} {agent.digest} signed by {verified.key_id}")
    return 0


def cmd_registry_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn  # noqa: F401 - there before any work (_run_server runs it)
    except ImportError:
        raise MendariError(
            "the registry needs uvicorn, which installs with Mendari and is missing here: "
            "install Mendari again (pip install mendari; in a clone: uv sync)"
        ) from None
    from mendari.hub.registry import create_registry_app

    _run_server(create_registry_app(args.db), args.host, args.port)
    return 0


def _registry_checkpoint(
    path: str, flag: str, public_key: str | None = None, *, takes_public_key: bool = False
) -> dict[str, Any]:
    """A registry checkpoint read from the file `flag` names, its signature checked with the
    registry's key ($MENDARI_REGISTRY_SIGNING_KEY) or the public key given: refused, with why,
    before any database is opened. `takes_public_key`: the command has --public-key, so with
    neither key it is named as the other way."""
    from mendari.core.checkpoint import load_public
    from mendari.hub.registry import stated_head
    from mendari.hub.yank import KEY_ENV

    statement: dict[str, Any] = _read_json(path, flag, shape=dict)
    if takes_public_key and public_key is None and not os.environ.get(KEY_ENV, "").strip():
        raise MendariError(
            f"{flag}: a checkpoint is checked with the registry's key: set {KEY_ENV}, or give "
            "its public key with --public-key (as GET /v1/registry-key shows it)"
        )
    try:
        stated_head(statement, None if public_key is None else load_public(public_key))
    except MendariError as error:
        raise MendariError(f"{flag}: {error}") from None
    return statement


def cmd_registry_verify(args: argparse.Namespace) -> int:
    """Check the registry's chain, read-only: every event's hash and order, and every row it
    covers named by its event (exit 1 if not). With --checkpoint, a statement `mendari registry
    checkpoint` signed earlier and kept outside the database, the chain must also still hold
    the head it names: one rewritten from before it verifies alone and fails here."""
    from mendari.hub.registry import RegistryStore

    if args.public_key is not None and args.checkpoint is None:
        raise MendariError("--public-key goes with --checkpoint: the key that signed it")
    stated = (
        None
        if args.checkpoint is None
        else _registry_checkpoint(
            args.checkpoint, "--checkpoint", args.public_key, takes_public_key=True
        )
    )
    with RegistryStore(args.db, read_only=True) as store:
        result = store.verify(head=None if stated is None else stated["head"])
    if result["ok"]:
        print(f"registry chain verified: {result['events']} events, head {result['head']}")
        if stated is None:
            print(
                "checked without a checkpoint: a chain rewritten whole verifies too "
                "(--checkpoint FILE, from `mendari registry checkpoint`)"
            )
        else:
            print(
                f"it holds the checkpoint of {stated['date']}; {result['after_checkpoint']} "
                "events since, which the checkpoint says nothing about"
            )
        return 0
    where = f" at event {result['seq']}" if result["seq"] is not None else ""
    print(f"registry chain broken{where}: {result['problem']}", file=sys.stderr)
    return 1


def cmd_registry_checkpoint(args: argparse.Namespace) -> int:
    """Print the signed statement of the registry chain's head today (UTC), with the registry's
    own key ($MENDARI_REGISTRY_SIGNING_KEY): keep it where the registry cannot write, and give
    it to `registry verify --checkpoint` or `registry countersign --checkpoint` later. The
    database is opened read-only and nothing is stored or chained. It refuses a chain that does
    not verify and, with --previous (the last statement kept), one that no longer holds that
    statement's head."""
    from mendari.hub.registry import RegistryStore
    from mendari.hub.yank import registry_key

    registry_key()  # a malformed key fails before anything is opened
    previous = None if args.previous is None else _registry_checkpoint(args.previous, "--previous")
    with RegistryStore(args.db, read_only=True) as store:
        _print_json(store.checkpoint(previous=previous))
    return 0


def _existing_registry(db: str) -> str:
    """A registry's database that is already there: an operator's command never makes one (a
    token needs its namespace, which only a running registry creates)."""
    if "://" not in db and not Path(db).is_file():
        raise MendariError(
            f"no registry database at {db}: run `mendari registry serve --db {db}` once first"
        )
    return db


def cmd_registry_token(args: argparse.Namespace) -> int:
    """A publisher token minted by the operator, on the registry's database (LP-19b): for a
    registry nobody signs in to (a self-hosted `mendari registry serve`, CI, a seed script).
    Its owner is recorded as `operator`, with who ran this and why, chained (`token.issued`); the
    token is printed once, here, and kept nowhere."""
    from mendari.hub.registry import RegistryStore

    with RegistryStore(_existing_registry(args.db)) as store:
        minted = store.mint_token(
            args.namespace,
            org=args.org,
            owner="operator",
            name=args.name,
            days=args.days,
            by=_command_line_actor(),
            reason=args.reason,
        )
    _print_json(minted)
    print(
        f"made in {_shown(args.db)}: the token is shown once, above; give it only to its publisher",
        file=sys.stderr,
    )
    return 0


def cmd_registry_tokens(args: argparse.Namespace) -> int:
    """The owned publisher tokens in a registry's database, read-only: whose each is, when it
    ends, when it was revoked and by whom, the last day it was used. Never a secret."""
    from mendari.hub.registry import RegistryStore

    with RegistryStore(args.db, read_only=True) as store:
        _print_json(store.tokens(org=args.org, namespace=args.namespace))
    return 0


def cmd_registry_revoke_token(args: argparse.Namespace) -> int:
    """Revoke an owned publisher token on the registry's database, at once: the operator may
    revoke any organization's. Chained (`token.revoked`) with who ran this and why."""
    from mendari.hub.registry import RegistryStore

    with RegistryStore(_existing_registry(args.db)) as store:
        revoked = store.revoke_token(
            args.id, org=None, by=_command_line_actor(), reason=args.reason
        )
    _print_json(revoked)
    return 0


def cmd_registry_claim(args: argparse.Namespace) -> int:
    """Claim a namespace for an organization as the operator, on the registry's database
    (LP-19c): the only way to claim a reserved name (`mendari`, `openhealth`, and names that
    start with either and a dash). The claim is pending, and nothing of the namespace is served
    to anyone but its own token until it is approved. Chained with who ran this and why."""
    from mendari.hub.registry import RegistryStore

    with RegistryStore(_existing_registry(args.db)) as store:
        claimed = store.claim(
            args.namespace,
            org=args.org,
            by=_command_line_actor(),
            display_name=args.display_name,
            reserved=True,
            reason=args.reason,
        )
    _print_json(claimed)
    return 0


def cmd_registry_decide(args: argparse.Namespace) -> int:
    """Approve or refuse a pending claim as the operator, once (LP-19c). Approval needs a
    published version; from then the namespace is listed. A refused namespace stays taken and
    is never listed. Chained (`namespace.approved`, `namespace.refused`) with who and why."""
    from mendari.hub.registry import RegistryStore

    with RegistryStore(_existing_registry(args.db)) as store:
        decided = store.decide_claim(
            args.namespace,
            approve=args.registry_command == "approve",
            by=_command_line_actor(),
            note=args.reason,
        )
    _print_json(decided)
    return 0


def cmd_registry_site(args: argparse.Namespace) -> int:
    """Register a site for an organization as the operator, on the registry's database
    (LP-22d): for a registry nobody signs in to. The site is pending until it is verified
    (`mendari registry site-verify`). Chained (`site.registered`) with who ran this and why."""
    from mendari.hub.registry import RegistryStore

    reason = _registry_reason(args.reason, "--reason")
    with RegistryStore(_existing_registry(args.db)) as store:
        site = store.register_site(
            org=args.org, name=args.name, by=_command_line_actor(), reason=reason
        )
    _print_json(site)
    return 0


def cmd_registry_site_verify(args: argparse.Namespace) -> int:
    """Verify (or, with --refuse, refuse) a pending site as the operator, once, after checking
    outside Mendari that the organization is who it says (LP-22d). Chained (`site.verified`,
    `site.refused`) with who ran this and the reason, kept as the site's note."""
    from mendari.hub.registry import RegistryStore

    reason = _registry_reason(args.reason, "--reason")
    with RegistryStore(_existing_registry(args.db)) as store:
        site = store.decide_site(
            args.site, verify=not args.refuse, by=_command_line_actor(), note=reason, by_org=None
        )
    _print_json(site)
    return 0


def cmd_registry_site_key(args: argparse.Namespace) -> int:
    """Add a site's public key as the operator (LP-22d), from the file the site's
    `mendari site init` writes: its public key and the key's proof that the site holds it.
    Refused when the proof does not hold, or the key is a publisher's or any site's. Chained
    (`site.key_added`)."""
    from mendari.hub.registry import RegistryStore

    _registry_reason(args.reason, "--reason")
    sent = _read_json(args.key, "--key", shape=dict)
    public, proof = sent.get("public_pem"), sent.get("proof")
    if not isinstance(public, str) or not isinstance(proof, str):
        raise MendariError(
            f"--key {_path_shown(args.key)}: not a site key file (public_pem and proof, as "
            "`mendari site init` writes it)"
        )
    with RegistryStore(_existing_registry(args.db)) as store:
        store.add_site_key(args.site, public, proof, org=None, by=_command_line_actor())
        site = store.site_record(args.site)
    _print_json(site)
    return 0


def cmd_registry_site_revoke_key(args: argparse.Namespace) -> int:
    """Revoke a site key at once as the operator (LP-22d); --compromised takes every report it
    signed out of the pooled figures. Chained (`site.key_revoked`) with who and why."""
    from mendari.hub.registry import RegistryStore

    reason = _registry_reason(args.reason, "--reason")
    with RegistryStore(_existing_registry(args.db)) as store:
        site = store.revoke_site_key(
            args.key_id,
            org=None,
            by=_command_line_actor(),
            reason=reason,
            compromised=args.compromised,
        )
    _print_json(site)
    return 0


def cmd_registry_site_close(args: argparse.Namespace) -> int:
    """Close a site as the operator (LP-22d): no ticket and no report again; what it shared
    stays. Chained (`site.closed`) with who and why."""
    from mendari.hub.registry import RegistryStore

    reason = _registry_reason(args.reason, "--reason")
    with RegistryStore(_existing_registry(args.db)) as store:
        site = store.close_site(args.site, org=None, by=_command_line_actor(), reason=reason)
    _print_json(site)
    return 0


def cmd_registry_sites(args: argparse.Namespace) -> int:
    """The sites in a registry's database with their keys, read-only."""
    from mendari.hub.registry import RegistryStore

    with RegistryStore(args.db, read_only=True) as store:
        _print_json(store.sites(org=args.org, status=args.status))
    return 0


def _site_configuration(
    agent: Any, providers: list[str] | None, *, consent: bool
) -> dict[str, str]:
    """The configuration a site evaluates and shares (LP-22e): the variants `--provider` names
    (with the agent's defaults for the other slots), or with none named the agent's defaults
    when they are local, else its first wholly local configuration (each slot's first local
    variant). A configuration that calls anything off the site's machines needs
    `--consent-remote`."""
    # Only what --provider names: never $MENDARI_PROVIDERS, a deployment's setting.
    settings = parse_providers(providers) if providers else {}
    chosen = _providers(agent, settings, typed=bool(providers))
    decision = Decision(agent, providers=chosen)
    configuration = {slot: info.variant for slot, info in decision.provider_info.items()}
    slots = agent.manifest.providers
    local = all(_decides_here(slots[slot].variants[name]) for slot, name in configuration.items())
    if not providers and not local:
        local_variants = {
            slot: [name for name, variant in spec.variants.items() if _decides_here(variant)]
            for slot, spec in slots.items()
        }
        if not all(local_variants.values()):
            raise MendariError(
                f"{agent.id} has no configuration that runs wholly on this machine: name one "
                "with --provider, and --consent-remote"
            )
        return {slot: names[0] for slot, names in local_variants.items()}
    if not local and not consent:
        raise MendariError(
            "this configuration calls a provider off this machine: a site shares it only with "
            "--consent-remote (recorded in the ticket and the report)"
        )
    return configuration


def _decides_here(variant: Any) -> bool:
    """Whether a provider variant decides on this machine (`core.origin.is_local`): a local
    type, or a composite whose members all are."""
    from mendari.core.origin import is_local

    return is_local({"type": variant.type, **(variant.model_extra or {})})


def cmd_site_init(args: argparse.Namespace) -> int:
    """Make this machine a site's (LP-22e): its id (the registry gives it when the site is
    registered), a new private key and a commitment secret, written private (0600) in the
    configuration directory; none of them ever leaves the site. Writes the key file to give the
    registry (`--out`, default `site-key.json`): the public key and its proof."""
    from mendari.hub.site_signing import init_site

    made = init_site(args.site, new_key=args.command_name == "key")
    out = Path(args.out)
    out.write_text(json.dumps(made, indent=2) + "\n", encoding="utf-8")
    _print_json({key: made[key] for key in ("site", "key_id")})
    print(
        f"wrote {out}: give it to the registry (`mendari registry site-key --key {out}`, or the "
        "dashboard's Sites tab). The private key and the commitment secret stay here.",
        file=sys.stderr,
    )
    return 0


def cmd_site_key(args: argparse.Namespace) -> int:
    """A new key for this site (`--new`), the id and commitment secret kept: register it, then
    revoke the old one (`mendari registry site-revoke-key`, or the dashboard). The old private
    key is kept beside the new one (`site_key.<hex>.pem`), never deleted."""
    from mendari.hub.site_signing import Site, retired_key_path, site_dir

    if not args.new:
        raise MendariError("`mendari site key --new` makes a new key; nothing else is asked")
    site = Site()
    args.site = site.id
    args.command_name = "key"
    code = cmd_site_init(args)
    print(
        f"the old key ({site.key_id}) is kept in {retired_key_path(site_dir(), site.key_id)}: "
        "delete it once the new key is registered and the old one revoked.",
        file=sys.stderr,
    )
    return code


def _ticket_request(
    agent: Any, configuration: dict[str, str], dataset: Any, *, consent: bool, supersedes: Any
) -> dict[str, Any]:
    """A signed ticket request for a run of `agent` in `configuration` on `dataset`'s whole
    file (LP-22e): the commitment is over the file's bytes, `n` its cases."""
    from mendari.hub import site_signing

    site = site_signing.Site()
    raw = dataset.path.read_bytes() if dataset.path.is_file() else b""
    if not raw:
        raise MendariError(f"{dataset.name}: a site's dataset is a file it holds")
    fields: dict[str, Any] = {
        "agent": {"id": agent.id, "version": agent.version, "digest": agent.digest},
        "configuration": configuration,
        "remote_consent": bool(consent),
        "commitment": site.commitment(raw),
        "n": len(dataset.records),
    }
    if supersedes:
        fields["supersedes"] = supersedes
    return site.sign(site_signing.TICKET_REQUEST, fields)


def _keep_ticket(issued: Mapping[str, Any]) -> Path:
    """A ticket and its one-time secret, kept private in the configuration directory."""
    from mendari.hub import client as hub_client

    ticket_id = str((issued.get("ticket") or {}).get("id", ""))
    if not re.fullmatch(r"[0-9a-f]{16}", ticket_id):  # it names a file here
        raise MendariError("the registry answered with a ticket id that is not one")
    path = hub_client.config_dir() / "tickets" / f"{ticket_id}.json"
    hub_client._write_private(path, dict(issued))
    return path


def _ticket_for(
    args: argparse.Namespace, agent: Any, configuration: dict[str, str], dataset: Any
) -> dict[str, Any]:
    """The ticket a shared run answers: `--ticket FILE` (fetched on another machine), or one
    asked for now; either way checked against this run before it starts."""
    from mendari.hub import site_signing

    if args.ticket:
        issued = _read_json(args.ticket, "--ticket", shape=dict)
    else:
        request = _ticket_request(
            agent, configuration, dataset, consent=args.consent_remote, supersedes=args.supersedes
        )
        with _registry_client(args) as registry:
            issued = registry.request_ticket(request)
        _keep_ticket(issued)
    ticket = issued.get("ticket") if isinstance(issued.get("ticket"), dict) else {}
    site = site_signing.Site()
    expected = {
        "site": site.id,
        "digest": agent.digest,
        "configuration": configuration,
        "n": len(dataset.records),
        "commitment": site.commitment(dataset.path.read_bytes()),
        "remote_consent": bool(args.consent_remote),
    }
    if any(ticket.get(key) != value for key, value in expected.items()) or not isinstance(
        issued.get("submit_secret"), str
    ):
        raise MendariError(
            "the ticket is not this run's: its site, digest, configuration, dataset or consent "
            "differ (ask for a ticket for this run: `mendari site ticket`)"
        )
    return issued


def _submission_summary(submission: Mapping[str, Any]) -> str:
    """What a signed submission says, to read before it is sent: its counts, never more."""
    report = submission["report"]
    primary = report["questions"].get(report["primary"]) or {}
    lines = [
        f"{report['agent']['id']}@{report['agent']['version']} on {report['dataset']['n']} cases "
        f"(ticket {submission['ticket']}, site {submission['site']})",
        f"  {report['primary']}: {primary.get('right')} of {primary.get('n')} right",
        f"  automated with a wrong recommendation: {report['decisions']['false_automation']}",
    ]
    for item in report["decisions"]["critical"]:
        lines.append(
            f"  critical {item['question']}={item['label']}: {item['detected']} of {item['n']} "
            f"detected, {item['auto_missed']} automated"
        )
    return "\n".join(lines)


def cmd_site_ticket(args: argparse.Namespace) -> int:
    """Ask the registry for a ticket before evaluating (LP-22e): signed with this site's key,
    bound to the agent's digest, the configuration, the dataset's size and a keyed commitment to
    the dataset (an HMAC under the site's secret; the file's own hash never leaves). The ticket
    and its one-time submission secret are kept private here (`tickets/ID.json`). Offline:
    `--out FILE` writes the signed request and sends nothing; on a machine that reaches the
    registry, `--request FILE --out FILE` sends it and writes the ticket and its secret, for
    `mendari eval --ticket` (LP-22f)."""
    from mendari.evals import resolve_dataset
    from mendari.hub import client as hub_client

    if args.request:
        if not args.out:
            raise MendariError("--request sends a carried request: --out FILE keeps its ticket")
        request = _read_json(args.request, "--request", shape=dict)
    else:
        if not (args.agent and args.dataset):
            raise MendariError("name the agent and --dataset, or send a carried --request")
        agent = resolve_agent(args.agent)
        configuration = _site_configuration(agent, args.provider, consent=args.consent_remote)
        dataset = resolve_dataset(agent, args.dataset)
        request = _ticket_request(
            agent, configuration, dataset, consent=args.consent_remote, supersedes=args.supersedes
        )
        if args.out:
            Path(args.out).write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
            print(f"wrote the signed ticket request to {args.out} (nothing sent)", file=sys.stderr)
            return 0
    with _registry_client(args) as registry:
        issued = registry.request_ticket(request)
    kept = _keep_ticket(issued)
    if args.request:
        hub_client._write_private(Path(args.out), dict(issued))
        kept = Path(args.out)
    _print_json(issued["ticket"])
    print(
        f"ticket {issued['ticket']['id']} kept with its secret in {kept}; 30 days", file=sys.stderr
    )
    return 0


def cmd_site_submit(args: argparse.Namespace) -> int:
    """Send a signed submission written by `mendari eval --share-out` (LP-22f), from any
    machine: its ticket's secret from `--ticket FILE`, or from the tickets kept here."""
    from mendari.hub import client as hub_client

    submission = _read_json(args.submission, "the submission", shape=dict)
    ticket_id = str(submission.get("ticket", ""))
    if not re.fullmatch(r"[0-9a-f]{16}", ticket_id):
        raise MendariError("not a signed submission: it names no ticket")
    source = (
        Path(args.ticket)
        if args.ticket
        else hub_client.config_dir() / "tickets" / f"{ticket_id}.json"
    )
    issued = _read_json(str(source), "--ticket", shape=dict)
    secret = issued.get("submit_secret")
    if (issued.get("ticket") or {}).get("id") != ticket_id or not isinstance(secret, str):
        raise MendariError(f"{_path_shown(source)} is not this submission's ticket")
    with _registry_client(args) as registry:
        shared = registry.submit(submission, secret)
    print(
        f"shared report {shared['id']} for ticket {ticket_id} (counts only, signed)",
        file=sys.stderr,
    )
    return 0


def cmd_registry_accept_report(args: argparse.Namespace) -> int:
    """Take a signed submission carried by hand, as the operator, on the registry's database
    (LP-22f): every check a sent one passes but the ticket's secret (the operator holds the
    file, not the secret). Chained with who ran this."""
    from mendari.hub.registry import RegistryStore

    submission = _read_json(args.submission, "the submission", shape=dict)
    report = submission.get("report")
    found = report.get("agent") if isinstance(report, dict) else None
    agent: dict[str, Any] = found if isinstance(found, dict) else {}
    namespace, _, name = str(agent.get("id", "")).partition("/")
    with RegistryStore(_existing_registry(args.db)) as store:
        accepted = store.accept_submission(
            namespace,
            name,
            str(agent.get("version", "")),
            submission,
            ticket_id=None,
            by=_command_line_actor(),
        )
    _print_json(accepted)
    return 0


def _registry_version(spec: str) -> tuple[str, str, str]:
    """`namespace/name@version`, as every pull names a version."""
    match = re.fullmatch(r"([a-z0-9][a-z0-9-]*)/([a-z0-9][a-z0-9-]*)@([0-9A-Za-z.+-]+)", spec)
    if match is None:
        raise MendariError(f"{spec!r} is not a version: namespace/name@version")
    return match[1], match[2], match[3]


def _registry_reason(text: str, what: str) -> str:
    """A reason the registry chains: one printable line, as its routes ask."""
    from mendari.hub import yank

    why = yank.printable_line(text, yank.MAX_REASON)
    if why or not text.strip():
        raise MendariError(f"{what} is {why or 'empty: say why in one line'}")
    return text


def cmd_registry_publisher_type(args: argparse.Namespace) -> int:
    """Record who publishes in a namespace as the operator, after checking it (LP-19e): where
    nobody signs in, or there is no review organization. Chained
    (`namespace.publisher_type_set`) with who ran this; the reason is printed with the answer,
    for the operator's own record, and not kept (the event has no field for one)."""
    from mendari.hub.registry import RegistryStore

    reason = _registry_reason(args.reason, "--reason")
    with RegistryStore(_existing_registry(args.db)) as store:
        changed = store.set_publisher_type(
            args.namespace, args.publisher_type, by=_command_line_actor()
        )
    _print_json(
        {
            "namespace": args.namespace,
            "publisher_type": changed["publisher_type"],
            "reason": reason,
        }
    )
    return 0


def cmd_registry_review(args: argparse.Namespace) -> int:
    """Record a version's review as the operator (LP-19e): the risk level the review found and
    why, a short line about the agent, never case text. Chained (`agent.reviewed`) with who ran
    this."""
    from mendari.hub.registry import RegistryStore, ReviewRequest

    namespace, name, version = _registry_version(args.version)
    checked = ReviewRequest(
        risk_level=args.risk_level, note=_registry_reason(args.reason, "--reason")
    )
    with RegistryStore(_existing_registry(args.db)) as store:
        reviewed = store.review(
            namespace,
            name,
            version,
            risk_level=checked.risk_level,
            note=checked.note,
            reviewer=_command_line_actor(),
        )
    _print_json({"id": reviewed["id"], "version": version, "review": reviewed["checked"]["review"]})
    return 0


def cmd_registry_recall(args: argparse.Namespace) -> int:
    """Recall a version as the operator (LP-19e): the registry signs the record with its own
    key ($MENDARI_REGISTRY_SIGNING_KEY, which this command needs), and every server that pulled
    the version stops automating with it. Once. Chained (`agent.yanked`) with who ran this."""
    from mendari.hub.registry import RegistryStore

    namespace, name, version = _registry_version(args.version)
    with RegistryStore(_existing_registry(args.db)) as store:
        recalled = store.recall_as_registry(
            namespace,
            name,
            version,
            reason=_registry_reason(args.reason, "--reason"),
            severity=args.severity,
            by=_command_line_actor(),
        )
    _print_json({"id": recalled["id"], "version": version, "yank": recalled["yank"]})
    return 0


def cmd_registry_delete(args: argparse.Namespace) -> int:
    """Delete a version as the operator (LP-19e): its archive is no longer served, and its
    bytes go when no other version needs them; the row and a tombstone with the reason stay.
    Once, and for good. Chained (`agent.deleted`) with who ran this."""
    from mendari.hub.registry import RegistryStore

    namespace, name, version = _registry_version(args.version)
    with RegistryStore(_existing_registry(args.db)) as store:
        deleted = store.delete(
            namespace,
            name,
            version,
            reason=_registry_reason(args.reason, "--reason"),
            by=_command_line_actor(),
        )
    _print_json({"id": deleted["id"], "version": version, "deleted": deleted["deleted"]})
    return 0


def cmd_registry_revoke_key(args: argparse.Namespace) -> int:
    """Revoke one of a namespace's publisher keys as the operator (ADR-0020): it signs no
    archive, recall or evaluation after, and the evaluations it signed stay listed, left out of
    the comparison. Once. Chained (`key.revoked`) with who ran this."""
    from mendari.hub.registry import RegistryStore

    with RegistryStore(_existing_registry(args.db)) as store:
        revoked = store.revoke_key(
            args.namespace,
            args.key_id,
            reason=_registry_reason(args.reason, "--reason"),
            by=_command_line_actor(),
        )
    _print_json(revoked)
    return 0


def cmd_registry_claims(args: argparse.Namespace) -> int:
    """The namespace claims in a registry's database, read-only: each one's organization, who
    claimed it, its status, and who decided it, when and why."""
    from mendari.hub.registry import RegistryStore

    with RegistryStore(args.db, read_only=True) as store:
        _print_json(store.claims(org=args.org, status=args.status))
    return 0


def cmd_registry_countersign(args: argparse.Namespace) -> int:
    """Counter-sign every listed version that has no record, with the registry's own key
    ($MENDARI_REGISTRY_SIGNING_KEY), on the registry's database (LP-19i): the versions from
    before the registry counter-signed, or published while it had no key. Never a deleted
    version, nor one of a pending or refused namespace. Run again it signs nothing. Each record
    is chained (`agent.accepted`) with who ran this. It refuses to run when the chain does not
    verify, and skips, with why, a version whose stored archive is not one a key of its
    namespace signed (exit 1 when any is skipped). With --checkpoint (a statement `mendari
    registry checkpoint` signed earlier, kept outside the database) the chain must also still
    hold the head it names; without one it says so, and with one it says how many events the
    chain held after that head (`events_since_checkpoint`), which the checkpoint does not cover."""
    from mendari.hub.registry import RegistryStore

    stated = (
        None if args.checkpoint is None else _registry_checkpoint(args.checkpoint, "--checkpoint")
    )
    with RegistryStore(_existing_registry(args.db)) as store:
        done = store.countersign(by=_command_line_actor(), checkpoint=stated)
    _print_json({**done, "checkpoint": None if stated is None else stated["date"]})
    since = done["events_since_checkpoint"]
    if stated is not None and since:
        print(
            f"the checkpoint of {stated['date']} is {since} events behind the "
            "chain as it was checked: it says nothing about those events. Read them before you "
            "rely on this run, and take checkpoints on a schedule",
            file=sys.stderr,
        )
    if stated is None:
        print(
            "ran without a checkpoint: the chain verified, but a chain rewritten whole verifies "
            "too. Keep `mendari registry checkpoint` outside the database and pass it with "
            "--checkpoint",
            file=sys.stderr,
        )
    if done["skipped"]:
        print(
            f"{len(done['skipped'])} not counter-signed: not a version the registry could have "
            "accepted (each is named above with why); clients refuse them",
            file=sys.stderr,
        )
        return 1
    return 0


def _registry_client(args: argparse.Namespace) -> Any:
    from mendari.hub.client import RegistryClient, default_registry

    if args.registry:
        url, token = args.registry, None
        try:
            saved_url, saved_token = default_registry()
            token = saved_token if saved_url == args.registry.rstrip("/") else None
        except MendariError:
            pass
    else:
        url, token = default_registry()
    return RegistryClient(url, token)


def cmd_login(args: argparse.Namespace) -> int:
    from mendari.hub import client

    health = client.login(args.registry, args.token)
    access = "" if args.token else ", read-only"
    print(f"logged in to {args.registry} ({health['agents']} agents{access})")
    return 0


# The most configurations `mendari push --evaluate` runs without being told which: an agent's
# configurations multiply with its slots, and each is a run of the whole dataset.
MAX_EVALUATED = 8


def cmd_push(args: argparse.Namespace) -> int:
    """Sign an agent and publish it. With `--evaluate DATASET`, then evaluate each of its
    configurations on that dataset of its own and publish every one, signed with the same key
    (ADR-0020): the one step from a version to its evaluation page. A version the registry
    already holds, the same build, is not pushed again, so the command can be run again."""
    from mendari.hub import archive

    agent = resolve_agent(args.agent)
    key = archive.load_private_key(Path(args.key).read_bytes())
    if args.evaluate is None:
        for flag, given in (
            ("--configuration", args.configuration),
            ("--labelled-by", args.labelled_by),
            ("--workers", args.workers),
        ):
            if given:
                raise MendariError(f"{flag} is for --evaluate DATASET: add it")
        configurations: list[dict[str, str]] = []
    else:  # refused before anything is sent
        _published_dataset(agent, args.evaluate, "--evaluate")
        _labelled_by(args.labelled_by)
        configurations = _evaluated_configurations(agent, args.configuration)
    with _registry_client(args) as registry:
        try:
            published = registry.push(agent, key)
        except HubError as error:
            if error.status != 409 or args.evaluate is None:
                raise
            namespace, name = agent.id.split("/")
            held = registry.version(namespace, name, agent.version)
            if held.get("digest") != agent.digest:
                raise MendariError(
                    f"{agent.id}@{agent.version} is published as another build "
                    f"({held.get('digest')}): versions are immutable, so give this one a new "
                    "version and push again"
                ) from None
            print(f"already published: {agent.id}@{agent.version} {agent.digest}")
        else:
            print(f"published {published['id']}@{published['version']} {published['digest']}")
        if args.evaluate is None:
            return 0
        return _publish_evaluations(args, registry, agent, key, configurations)


def _evaluated_configurations(agent: Agent, named: Sequence[str]) -> list[dict[str, str]]:
    """The configurations `mendari push --evaluate` runs: those named (`--configuration`, each
    a slot's variant or several, as `mendari leaderboard --run` takes them; a slot left out
    keeps its default), or every configuration the agent declares (the product of its slots'
    variants, in the manifest's order), when they are few."""
    import itertools
    import math

    if named:
        chosen = [_providers(agent, parse_providers(given.split(","))) for given in named]
    else:
        slots = agent.manifest.providers
        count = math.prod(len(slot.variants) for slot in slots.values())
        if count > MAX_EVALUATED:
            raise MendariError(
                f"{agent.id} has {count} configurations: name the ones to evaluate, each with "
                "--configuration SLOT=VARIANT[,SLOT=VARIANT...]"
            )
        chosen = [
            dict(zip(slots, names, strict=True))
            for names in itertools.product(*(slot.variants for slot in slots.values()))
        ]
    distinct: list[dict[str, str]] = []
    for configuration in chosen:
        if configuration not in distinct:
            distinct.append(configuration)
    return distinct


def _publish_evaluations(
    args: argparse.Namespace,
    registry: Any,
    agent: Agent,
    key: Any,
    configurations: Sequence[Mapping[str, str]],
) -> int:
    """Evaluate each configuration on the agent's own dataset and publish it, one line each;
    then where the Agent Hub shows them and the README's badge line. Exit 1 when one was not
    published: the others are, and running the command again sends what is missing."""
    from mendari.evals import resolve_dataset
    from mendari.evals.harness import evaluation

    dataset = resolve_dataset(agent, args.evaluate)
    refused = 0
    for configuration in configurations:
        decision = Decision(
            agent,
            providers=dict(configuration),
            fhir_validation=fhir_validation(),
            origin_cap=False,
        )
        run = evaluation(
            decision, dataset, workers=args.workers or (8 if decision.is_remote else 1)
        )
        ran = {slot: str(p["variant"]) for slot, p in run.ledger.header["providers"].items()}
        try:
            published = _publish_run(registry, agent, run, key, args.labelled_by)
        except HubError as error:
            refused += 1
            print(f"  {_configuration_words(ran)}: not published: {error}", file=sys.stderr)
            continue
        print(f"  {_configuration_words(ran)}: {published['rerun_words']}")
    sent = len(configurations) - refused
    print(
        f"published {sent} of {len(configurations)} evaluations of {agent.id}@{agent.version} "
        f"on {dataset.name} ({len(dataset.records)} cases), counts only, signed"
    )
    if refused:
        print(
            "an evaluation the registry did not take is named above with why. A version in a "
            "namespace the registry has not listed yet takes none: run this again once it is",
            file=sys.stderr,
        )
    if sent:
        for line in _where_it_shows(registry, agent.id):
            print(line)
    return 1 if refused else 0


def cmd_pull(args: argparse.Namespace) -> int:
    from mendari.core.origin import pulled_record
    from mendari.hub.client import pull, recall_of

    with _registry_client(args) as registry:
        agent = pull(args.ref, registry, accept_new_key=args.accept_new_key)
        namespace, name = agent.id.split("/")
        recall = recall_of(registry.version(namespace, name, agent.version), registry, agent)
    print(f"{agent.id}@{agent.version} {agent.digest}\n  verified and cached in {agent.root}")
    if pulled_record(agent.root).get("accepted") is True:
        print("  counter-signed by the registry: its record names this version and verifies")
    else:
        print("  not counter-signed: this registry publishes no signing key")
    if recall is not None:
        by = "its publisher" if recall["signer"] == "publisher" else "the registry"
        signed = (
            "the record's signature holds"
            if recall["verified"]
            else f"the record does not verify: {recall['problem']}"
        )
        print(
            f"  RECALLED ({recall['severity']}) by {by} at {recall['at']}: {recall['reason']}\n"
            f"  {signed}; do not automate with this version",
            file=sys.stderr,
        )
    if agent.bounded:
        print(
            "  runs on the engine for agents from a registry (RE2, a 1 s CPU budget per decision)"
        )
    else:
        print("  runs on the maintained engine: a copy of an agent this release bundles")
    print(
        "  served, it never automates until your organization approves this digest "
        "(POST /api/registry-approvals); `mendari test` and `eval` check its own policy"
    )
    return 0


def cmd_yank(args: argparse.Namespace) -> int:
    """Recall a version your namespace published, signed with its key (`hub/yank.py`)."""
    from mendari.core.spec import is_agent_id, is_version
    from mendari.hub import archive

    agent_id, _, version = args.ref.partition("@")
    if not is_agent_id(agent_id) or not is_version(version):
        raise MendariError(f"{args.ref!r} is not namespace/name@version: name the exact version")
    key = archive.load_private_key(Path(args.key).read_bytes())
    namespace, name = agent_id.split("/")
    with _registry_client(args) as registry:
        recalled = registry.yank(
            namespace, name, version, reason=args.reason, severity=args.severity, key=key
        )
    print(f"recalled {recalled['id']}@{recalled['version']} ({args.severity})")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    with _registry_client(args) as registry:
        found = registry.search(
            q=" ".join(args.query),
            risk=args.risk,
            standard=args.standard,
            publisher_type=args.publisher_type,
        )
    if args.json:
        _print_json(found)
        return 0
    for item in found:
        # No figure: what a registry sends beside an entry may be the publisher's own benchmark,
        # and an uploaded number is never shown (ADR-0012). The risk level is the publisher's word.
        risk = f"risk {item['risk']['level']} (declared)"
        print(f"{item['id']}@{item['version']}  {risk}  {item['publisher_type']}")
        print(f"    {item['summary'][:110]}")
    if not found:
        print("no agents match")
    return 0


def cmd_keys_register(args: argparse.Namespace) -> int:
    with _registry_client(args) as registry:
        key_id = registry.add_key(args.namespace, Path(args.public_key).read_bytes())
    print(f"{key_id} registered for {args.namespace}")
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """For a scheduler: every agent's alerts (the served, newest, version of each), for one
    organization with --org, each organization on its own with --each-org (but the status
    organization, MENDARI_STATUS_ORG), otherwise all of them together; posted to a webhook when
    one is set (`mendari.alerts`: in its format, signed with its secret, tried up to three
    times), one alert per organization; exit 1 on any alert, 3 when any could not be posted.
    With --each-org (a scheduled job) an alert posted is the job done: exit 0 when every alert
    found was posted (or none was found); 1 when alerts were found with no webhook to post them,
    or one organization's monitoring failed (the others still run, its line says why); 3 when
    any could not be posted, whatever else happened.
    Nothing is recorded: the database is opened read-only. A benchmark counts only for the
    build and providers a server recorded running (`deployments`; a model a setting chose is
    not recorded, so a benchmark of one is not compared), under the organization's governance;
    otherwise the line says why, and only the checks that need no benchmark run (automated
    decisions rated wrong, overrides that doubled, provider errors, abstentions and latency)."""
    from mendari.alerts import alert_settings, check_webhook
    from mendari.core import tenancy
    from mendari.evals.monitoring import monitor, organizations, post_alerts, recorded_providers
    from mendari.settings import status_org

    alerts = alert_settings(args.webhook)  # before any work, so a typo fails at once
    if alerts is not None:
        check_webhook(alerts.webhook)
    skip = status_org() if args.each_org else None  # likewise
    if args.agent:
        agents = [resolve_agent(args.agent)]
    else:  # the version a server serves of each agent: the newest
        newest: dict[str, Agent] = {}
        for agent in discover().agents:
            if agent.id not in newest or version_key(agent.version) > version_key(
                newest[agent.id].version
            ):
                newest[agent.id] = agent
        agents = [newest[agent_id] for agent_id in sorted(newest)]
    found_alerts = undelivered = failed = False
    with _read_store(args.db) as store:
        orgs = organizations(store, days=args.days, skip=skip) if args.each_org else [args.org]
        for org in orgs:
            try:
                with tenancy.acting_for(org):
                    reports = [
                        monitor(
                            store, agent, days=args.days, providers=recorded_providers(store, agent)
                        )
                        for agent in agents
                    ]
            except Exception as error:
                debug = os.environ.get("MENDARI_DEBUG", "").strip().lower() in _DEBUG_ON
                if not args.each_org or getattr(args, "debug", False) or debug:
                    raise
                # One organization's failure never hides another's alerts, posted or not.
                for line in _problem_lines(error):
                    print(f"error: {org}: {line}", file=sys.stderr)
                failed = True
                continue
            where = f"{org}: " if args.each_org else ""
            for report in reports:
                status = f"{len(report['alerts'])} alert(s)" if report["alerts"] else "ok"
                reason = report["benchmark"]["reason"]
                compared = "" if reason is None else f" (no comparable benchmark: {reason})"
                print(f"{where}{report['agent']['id']}: {status}{compared}")
                for alert in report["alerts"]:
                    print(f"  [{alert['severity']}] {alert['kind']}: {alert['message']}")
            if not any(report["alerts"] for report in reports):
                continue
            found_alerts = True
            if alerts is not None and not post_alerts(alerts, org, reports, attempts=3):
                undelivered = True  # the warning above says why (its code), never the URL
    if undelivered:
        print("error: the alerts could not be posted to the webhook", file=sys.stderr)
        return 3
    if args.each_org:
        return 1 if failed or (found_alerts and alerts is None) else 0
    return 1 if found_alerts else 0


def cmd_packet(args: argparse.Namespace) -> int:
    """The committee packet of one agent (`core/packet.py`): one page to print, or the JSON it
    is made from (--json), written to --out or printed.

    **With --db**, from the server's database: for one organization with --org, otherwise every
    organization of the deployment together. Nothing is recorded: the database is opened
    read-only, and the command works with the server down. What only a running server knows
    (sign-in, sessions, the schedule, the model each part runs) is shown as not known; the rest
    is read from this environment's settings, which should be the server's, and the page says
    it was made from the command line.

    **Without --db**, from the agent's files alone (`packet.build_alone`): what a publisher
    hands a hospital before any deployment exists. No database is opened or made, and no
    setting of this machine is read; the sections on a deployment's data say that none was
    read, and why. --org and --days count a deployment's decisions, so they are refused."""
    from mendari.core import packet

    agent = resolve_agent(args.agent)
    if args.db is None:
        if args.org is not None or args.days is not None:
            raise MendariError(
                "--org and --days count a deployment's decisions: give --db, the server's "
                "database, with them. Without --db the packet is made from the agent's files "
                "alone"
            )
        built = packet.build_alone(agent)
    else:
        built = _deployment_packet(args, agent)
    text = json.dumps(built, indent=2) + "\n" if args.json else packet.render_html(built)
    # Said beside the document, never in it twice: the packet says the same on its cover.
    alone = (
        "no --db: made from the agent's files alone, so it holds no deployment's data, and "
        "says so in each section that would. A site adds --db with its server's database"
        if args.db is None
        else ""
    )
    if not args.out:
        sys.stdout.write(text)
        if alone:
            print(f"note: {alone}", file=sys.stderr)
        return 0
    target = Path(args.out)
    if target.is_symlink():  # the write would land wherever the link points
        raise MendariError(f"{target} is a symbolic link; not writing through it")
    target.write_text(text, encoding="utf-8")
    print(
        f"wrote {target}: {agent.id} {agent.version}, {len(built['sections'])} sections, "
        f"{len(built['not_stated'])} items not stated",
        file=sys.stderr,
    )
    if alone:
        print(f"note: {alone}", file=sys.stderr)
    return 0


def _deployment_packet(args: argparse.Namespace, agent: Agent) -> dict[str, Any]:
    """`mendari packet --db`: the packet of `agent` as the deployment whose database this is
    holds it (`packet.build`), read with this environment's settings."""
    from mendari.alerts import alert_settings
    from mendari.core import packet, tenancy
    from mendari.core.checkpoint import checkpoint_key, key_id
    from mendari.core.phi import phi_policy
    from mendari.evals.monitoring import recorded_providers
    from mendari.providers import provider_allow_list
    from mendari.providers.agent import ENDPOINT_ENV
    from mendari.settings import retention_days

    phi = phi_policy()  # before any work, so a bad setting fails at once
    alerts = alert_settings()
    signer = checkpoint_key()
    kept_days = retention_days()
    with _read_store(args.db) as store, tenancy.acting_for(args.org):
        deployment = packet.Deployment(
            phi_mode=phi.mode,
            input_hash=phi.hash_kind,
            retention_days=kept_days,
            checkpoint_key=None if signer is None else key_id(signer.public_key()),
            alert_destination=alerts is not None,
            alerts_signed=alerts is not None and bool(alerts.secret),
            provider_allowed_origins=provider_allow_list(),
            agent_endpoint=bool(os.environ.get(ENDPOINT_ENV, "").strip()),
            providers=recorded_providers(store, agent),
            seen_from="command line",
        )
        return packet.build(
            store,
            agent,
            deployment=deployment,
            days=packet.DEFAULT_DAYS if args.days is None else args.days,
            organization=args.org,
            holds=store.holds(args.org),
            records=store.deployments(agent.id, limit=20),
        )


def cmd_alerts_test(args: argparse.Namespace) -> int:
    """For the operator, after setting the webhook: one test alert (`alerts.test`) to
    $MENDARI_ALERT_WEBHOOK through `mendari.alerts`, in its format and signed with its secret,
    as every alert is; tried once, recorded nowhere. Refused in CI (`CI` or `GITHUB_ACTIONS`
    set), so no job posts to a real webhook, and without a webhook. What it prints names the
    outcome, never the URL or the secret. Exit 0 when delivered, 1 when not."""
    from mendari.alerts import TEST_EVENT, alert_settings, check_webhook, send_test

    for name in ("CI", "GITHUB_ACTIONS"):
        if os.environ.get(name, "").strip():
            raise MendariError(
                f"{name} is set: a test alert is never sent from CI; run it on your own machine"
            )
    settings = alert_settings()
    if settings is None:
        raise MendariError(
            "MENDARI_ALERT_WEBHOOK is not set: set it (and MENDARI_ALERT_SECRET) as the "
            "deployment has them, then run this again"
        )
    check_webhook(settings.webhook)
    sent = send_test(settings)
    if not sent.delivered:
        why = (
            f"the webhook answered {sent.status}"
            if sent.status is not None
            else f"no answer ({sent.error})"
        )
        raise MendariError(f"the test alert was not delivered: {why} (delivery {sent.id})")
    signed = "signed" if settings.secret else "not signed: MENDARI_ALERT_SECRET is not set"
    print(
        f"delivered: one {TEST_EVENT} in the {settings.format} format, {signed} "
        f"(the webhook answered {sent.status}, delivery {sent.id})"
    )
    return 0


def cmd_phi_purge(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy
    from mendari.settings import MAX_RETENTION_DAYS, retention_days

    days = args.days if args.days is not None else retention_days()
    if not 0 <= days <= MAX_RETENTION_DAYS:
        raise MendariError(f"--days is a number of days, 0 to {MAX_RETENTION_DAYS}")
    if days == 0 and not args.yes:
        raise MendariError("--days 0 deletes every kept input and erases every note: add --yes")
    _servers_database(args.db, used=False, tables=_EVERY_RELEASE)  # mistyped: refused
    summary = AuditStore(args.db, phi=phi_policy()).purge(days, source="command")
    print(
        f"{summary['inputs']} stored input(s) deleted and {summary['notes']} note(s) erased, "
        f"older than {days} days (decisions and chain kept)"
    )
    return 0


def cmd_phi_rotate(args: argparse.Namespace) -> int:
    """Re-seal under the current PHI key, or (`--check`) show, marking nothing, whether
    everything opens with it alone. JSON on stdout: key ids and counts, never a key or a value.
    A database from an older release is still migrated on opening, as by every command."""
    from mendari.core.phi import phi_policy

    policy = phi_policy()
    _servers_database(args.db, used=True)
    store = AuditStore(args.db, phi=policy, marks=False)  # --check marks nothing
    if args.check:
        checked = store.phi_check()
        print(json.dumps(checked, indent=2))
        if checked["ready"]:
            print("ready: everything opens with the current key alone", file=sys.stderr)
        return 0 if checked["ready"] else 1
    if not args.to:
        raise MendariError("give --to <key id>: phi_keys.current in /api/health")
    rotated = store.rotate_phi(to=args.to, batch=args.batch)
    print(json.dumps(rotated, indent=2))
    if rotated["unopenable"]:
        print(
            f"{len(rotated['unopenable'])} value(s) no key here opens were left as they are: add "
            "the key they name to MENDARI_PHI_READ_KEYS, or erase them (mendari phi erase), then "
            "run again",
            file=sys.stderr,
        )
        return 1
    print("done: run `mendari phi rotate --check` before dropping the read keys", file=sys.stderr)
    return 0


def cmd_phi_erase(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy

    if not SYSTEM_ID.fullmatch(args.by):
        raise MendariError("--by is an identifier, such as an email address")
    _servers_database(args.db, used=True, tables=_EVERY_RELEASE)  # mistyped: refused
    erased = AuditStore(args.db, phi=phi_policy()).erase_input(
        args.decision, erased_by=args.by, reason=args.reason
    )
    kept = ["its input"] if erased["input"] else []
    kept += [f"{len(erased['notes'])} note(s)"] if erased["notes"] else []
    print(
        f"decision {args.decision}: {' and '.join(kept)} erased; backups, the database's "
        "history and earlier exports still hold them"
    )
    if erased["still_in_chain"]:
        print(
            "its chained record or events still hold input, evidence or note text: only "
            "retiring the database removes them (docs/deploying-with-phi.md)"
        )
    return 0


def cmd_notes_erase(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy

    _servers_database(args.db, used=True, tables=_EVERY_RELEASE)  # mistyped: refused
    erased = AuditStore(args.db, phi=phi_policy()).erase_note(
        args.id, erased_by=args.by, reason=args.reason
    )
    print(
        f"note {erased['note_id']} of decision {erased['decision_id']} erased; "
        "backups, the database's history and earlier exports still hold it"
    )
    return 0


def _agent_store(target: str | None, *, writes: bool) -> tuple[Any, str]:
    """The deployment's database for `mendari store` (LP-20e): given, or $DATABASE_URL. To
    write, it must be a server's (`_servers_database`); otherwise opened read-only. Returns the
    agent store (`core/agent_store.py`) and the database's name as shown (no password)."""
    from mendari.core.agent_store import AgentStore

    if target is None:
        target = os.environ.get("DATABASE_URL", "").strip() or None
    if target is None:
        raise MendariError("give --db (the server's database) or set DATABASE_URL")
    if not writes:
        return AgentStore(_read_store(target)), _shown(target)
    shown = _servers_database(target, used=True, also=("store_versions",))
    return AgentStore(AuditStore(target, marks=False)), shown


def cmd_store_versions(args: argparse.Namespace) -> int:
    """An organization's agent store versions, newest first (LP-20e): ids, digests, states and
    files' hashes, never a file's content. Read-only."""
    store, _ = _agent_store(args.db, writes=False)
    with store.store:
        _print_json(store.versions(args.org, args.agent))
    return 0


def cmd_store_verify(args: argparse.Namespace) -> int:
    """Every kept file of every agent store hashes to its key, and every version whose files are
    kept to its digest (LP-20e). Read-only; exit 1 on a problem."""
    store, _ = _agent_store(args.db, writes=False)
    with store.store:
        found = store.verify()
    _print_json(found)
    return 0 if found["ok"] else 1


def cmd_store_withdraw(args: argparse.Namespace) -> int:
    """The operator withdraws a draft of an organization's store, with a reason on the chain
    (LP-20e)."""
    store, shown = _agent_store(args.db, writes=True)
    with store.store:
        moved = store.withdraw(args.org, args.digest, by="operator", reason=args.reason)
    _print_json(moved)
    print(f"withdrawn in {shown}", file=sys.stderr)
    return 0


def cmd_store_purge(args: argparse.Namespace) -> int:
    """Free the files that only versions withdrawn more than 30 days ago hold (LP-20e); the rows
    and their events stay."""
    store, shown = _agent_store(args.db, writes=True)
    with store.store:
        removed = store.purge()
    print(f"{removed} file(s) freed in {shown}", file=sys.stderr)
    return 0


# What `mendari new` writes: an AI agent on spec 0.4 with an offline mock fixture, and its
# examples, rules, input schema and tests.
TEMPLATE = Path(__file__).resolve().parent / "templates" / "base"
# A URL, not a path: `mendari new` runs anywhere, mostly outside a checkout of the repository.
AUTHORING_GUIDE = (
    "https://github.com/mendari-hq/mendari/blob/main/docs/guides/authoring-an-agent.md"
)
# The workflow `mendari new` writes into the agent's own `.github/workflows/`: Mendari's action
# (action.yml) at this release's tag, which checks the agent on every change. Its template is
# kept outside any `.github/` folder: a release holds no CI configuration of its own
# (scripts/check_excluded.py).
CI_TEMPLATE = Path(__file__).resolve().parent / "templates" / "ci" / "mendari.yml"
CI_WORKFLOW = Path(".github") / "workflows" / "mendari.yml"
CI_GUIDE = "https://github.com/mendari-hq/mendari/blob/main/docs/guides/evaluate-in-ci.md"


def cmd_new(args: argparse.Namespace) -> int:
    """A new agent from the template: YAML only, and it validates, tests and evaluates as it is,
    on an offline mock fixture that switches to a live agent endpoint when `MENDARI_AGENT_URL`
    is set, with the workflow that checks the agent on every change in its own repository
    (`CI_WORKFLOW`), which is no part of the agent: a hidden folder is in no digest or archive."""
    from mendari.core.spec import _AGENT_ID

    target = Path(args.dir)
    if target.exists() and any(target.iterdir()):
        raise MendariError(f"{target} exists and is not empty")
    agent_id = args.id
    if agent_id is None:
        raise MendariError("--id is required: namespace/name, e.g. my-clinic/refill-router")
    if not _AGENT_ID.match(agent_id):
        raise MendariError("--id must look like 'namespace/name' (lowercase, digits, dashes)")
    name = args.name or agent_id.split("/", 1)[1].replace("-", " ").capitalize()
    raw_values = {"{{id}}": agent_id, "{{name}}": name, "{{publisher}}": args.publisher}
    yaml_values = {
        "{{id}}": agent_id,
        "{{name}}": json.dumps(name, ensure_ascii=False),
        "{{publisher}}": json.dumps(args.publisher, ensure_ascii=False),
    }
    placeholder_re = re.compile("|".join(re.escape(k) for k in raw_values))

    def fill(text: str, values: dict[str, str]) -> str:
        return placeholder_re.sub(lambda match: values[match.group(0)], text)

    for source in sorted(TEMPLATE.rglob("*")):
        if not source.is_file():
            continue
        values = yaml_values if source.suffix in (".yaml", ".yml") else raw_values
        text = fill(source.read_text(encoding="utf-8"), values)
        destination = target / source.relative_to(TEMPLATE)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    # An agent with a repository of its own: checked there on every change.
    workflow = target / CI_WORKFLOW
    workflow.parent.mkdir(parents=True, exist_ok=True)
    text = CI_TEMPLATE.read_text(encoding="utf-8").replace("{{version}}", __version__)
    workflow.write_text(fill(text, yaml_values), encoding="utf-8")
    print(f"{agent_id} created in {target}. Next:")
    steps = [
        f"mendari validate {target}",
        f"mendari test {target}",
        f"mendari eval {target} --dataset examples",
        f"MENDARI_AGENT_URL=https://your-agent.example/decide mendari eval {target} "
        "--dataset examples",
        f"mendari eval {target} --dataset examples --provider reader=rules",
        f"mendari probe {target}",
    ]
    for step in steps:
        print(f"  {step}")
    print(f"The guide: {AUTHORING_GUIDE}")
    print(f"On every change: {target / CI_WORKFLOW} runs these checks on GitHub ({CI_GUIDE})")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from mendari.sandbox.demo import run_demo

    run_demo()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy
    from mendari.mcp import McpServer, serve

    # Decisions are recorded under the PHI mode set here, as `mendari decide --db` records them.
    with AuditStore(args.db or ":memory:", phi=phi_policy()) as store:
        serve(McpServer(store), sys.stdin, sys.stdout)
    return 0


def cmd_help(args: argparse.Namespace) -> int:
    """`mendari help`: the commands to start with, as `mendari --help` prints them; `mendari
    help all`: every command; `mendari help <command>` (and its subcommand): that command's own
    help, as its `--help` prints it."""
    parser = build_parser()
    if args.topic in ([], ["all"]):
        print(overview(parser, everything=bool(args.topic)))
        return 0
    command = _command(parser, args.topic)
    if command is None:
        raise MendariError(
            f"`mendari {' '.join(args.topic)}` is not a command: `mendari help all` lists "
            "every command"
        )
    command.print_help()
    return 0


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# -- parser ----------------------------------------------------------------------------------


def _listen(text: str) -> tuple[str, int]:
    """`HOST:PORT`, checked before a server starts; port 0 is any free port."""
    host, colon, port = text.rpartition(":")
    if not colon or not host or not port.isdigit() or not 0 <= int(port) < 65536:
        raise argparse.ArgumentTypeError(f"not HOST:PORT: {text!r}")
    return host.strip("[]"), int(port)


def _port(text: str) -> int:
    """A TCP port, checked before a server starts (argparse names the flag)."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port: {text!r}") from None
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError(f"not a port: {port} (1 to 65535)")
    return port


def _share(text: str) -> float:
    """A limit's share as people write one, `90%` or `0.9`, from 0 to 100%: checked before any
    work starts (argparse names the flag). A target's share is read the same way (`_target`)."""
    from mendari.evals.operating_point import share

    try:
        return share(text, whole=True)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{error}: {text!r}") from None


def _target(text: str) -> float:
    """A target's share, `1%` or `0.01`, above 0 and below 100%: what a recommended automation
    threshold must keep a risk to. Checked before any work starts (argparse names the flag)."""
    from mendari.evals.operating_point import Targets, share

    try:
        value = share(text)
        Targets(critical_miss=value)  # the range a target has
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{error}: {text!r}") from None
    return value


def _confidence(text: str) -> float:
    """The confidence a target is held with, `95%` or `0.95`, checked before any work starts
    (argparse names the flag)."""
    from mendari.evals.operating_point import Targets, share

    try:
        value = share(text)
        Targets(confidence=value)  # the range a confidence level has
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{error}: {text!r}") from None
    return value


def _whole(text: str) -> int:
    """A count, 0 or more, checked before any work starts (argparse names the flag)."""
    if not (text.isascii() and text.isdigit()):
        raise argparse.ArgumentTypeError(f"not a whole number, 0 or more: {text!r}")
    return int(text)


def _monitored_days(text: str) -> int:
    """The days `mendari monitor` covers: two whole weeks to 180 days (argparse names the flag),
    so the week before the last always exists."""
    from mendari.evals.monitoring import MAX_DAYS, MIN_DAYS

    try:
        days = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of days: {text!r}") from None
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise argparse.ArgumentTypeError(f"{days} days: give {MIN_DAYS} to {MAX_DAYS}")
    return days


def _case_limit(text: str) -> int | None:
    """`mendari probe --limit`: how many cases, 1 or more, or `all` (None)."""
    if text == "all":
        return None
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError("1 case or more, or `all`")
    return int(text)


class UsageError(MendariError):
    """A command line argparse refuses: one line, naming the command and where its help is."""


class _Parser(argparse.ArgumentParser):
    """Every parser and subparser of `mendari` (add_subparsers makes its subparsers of this
    class): a usage error is one line and exit 1, like any other error (LP-15), and every
    command takes `--debug`."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.add_argument(
            "--debug",
            action="store_true",
            default=argparse.SUPPRESS,
            help="show the full traceback of an error (also MENDARI_DEBUG=1)",
        )

    def error(self, message: str) -> NoReturn:
        raise UsageError(f"{self.prog}: {message} (see {self.prog} --help)")


def _agent_argument(
    command: argparse.ArgumentParser,
    about: str = "agent id (namespace/name[@version]) or directory",
    **kwargs: Any,
) -> None:
    """The agent a command takes (`args.agent`)."""
    command.add_argument("agent", metavar="agent", help=about, **kwargs)


def _agent_option(command: argparse.ArgumentParser, **kwargs: Any) -> None:
    """`--agent`, the agent a command is about (`args.agent`)."""
    if "choices" not in kwargs:  # with choices, the usage lists them
        kwargs.setdefault("metavar", "AGENT")
    command.add_argument("--agent", dest="agent", **kwargs)


class Group(NamedTuple):
    """Commands with one purpose, under the heading the help lists them by."""

    heading: str
    first: tuple[str, ...]  # what `mendari --help` shows: the commands to start with
    more: tuple[str, ...]  # the rest, which `mendari help all` shows after them


# Every command is in exactly one group (tests/test_cli_help.py), so `mendari help all`
# misses none. A new command goes in `more`, unless most people need it in their first hour.
GROUPS = (
    Group("Start", ("demo", "new"), ("help",)),
    Group(
        "Build and check",
        ("validate", "test", "eval", "probe", "compare"),
        ("card", "schema"),
    ),
    Group("Run", ("decide", "serve"), ("list", "mcp")),
    Group("Share", ("login", "search", "pull", "push"), ("keys", "archive", "yank", "site")),
    Group(
        "Evaluation",
        (),
        (
            *("data", "leaderboard", "calibrate"),
            *("feedback", "written"),
        ),
    ),
    Group(
        "Operations",
        (),
        (
            *("monitor", "alerts", "usage", "audit", "automation", "access"),
            *("phi", "notes", "store", "registry", "packet"),
        ),
    ),
    Group("Standards", (), ("smart", "bulk", "sandbox")),
)
EVERY_COMMAND = "`mendari help all` (also `mendari --help-all`) lists every command."
ONE_COMMAND = "`mendari help <command>` (also `mendari <command> --help`) shows its options."


def _subcommands(parser: argparse.ArgumentParser) -> Any:
    """The argument that holds a parser's commands (argparse keeps them in one), or None."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _command(parser: Any, words: Sequence[str]) -> argparse.ArgumentParser | None:
    """The parser of the command `words` spell from `parser` down (`audit verify`), or None when
    they spell none."""
    for word in words:
        commands = _subcommands(parser)
        if commands is None or word not in commands.choices:
            return None
        parser = commands.choices[word]
    return parser


def overview(parser: argparse.ArgumentParser, *, everything: bool = False) -> str:
    """What `mendari --help` prints: the commands to start with, under what each is for. With
    `everything` (`mendari help all`, `mendari --help-all`): every command, then the options."""
    about = {command.dest: command.help for command in _subcommands(parser)._get_subactions()}
    sections = []
    for group in GROUPS:
        names = group.first + (group.more if everything else ())
        if names:
            sections.append((group.heading, [(name, about.get(name)) for name in names]))
    if everything:
        options = [action for action in parser._actions if action.option_strings]
        sections.append(("Options", [(", ".join(o.option_strings), o.help) for o in options]))
    column = max(len(name) for _, rows in sections for name, _ in rows) + 4
    # As wide as argparse makes a command's own help, and never too narrow to say anything. A
    # line breaks between words only: never inside `--help-all` or a path.
    width = max(shutil.get_terminal_size().columns - 2, column + 20)
    lines = [parser.format_usage().rstrip(), "", parser.description or ""]
    for heading, rows in sections:
        lines += ["", heading]
        for name, text in rows:
            lines += textwrap.wrap(
                text or "",
                width,
                initial_indent=f"  {name:<{column - 2}}",
                subsequent_indent=" " * column,
                break_on_hyphens=False,
            ) or [f"  {name}"]
    footer = ONE_COMMAND if everything else EVERY_COMMAND
    lines += ["", *textwrap.wrap(footer, width, break_on_hyphens=False)]
    return "\n".join(lines)


class _Overview(argparse.Action):
    """`mendari --help`: print the commands to start with, and stop. With `const` set
    (`mendari --help-all`): every command."""

    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: Any):
        super().__init__(
            option_strings, argparse.SUPPRESS, nargs=0, default=argparse.SUPPRESS, **kwargs
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        print(overview(parser, everything=bool(self.const)))
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    # Its help is `overview`, not the list of every command argparse would print.
    parser = _Parser(
        prog="mendari",
        usage="%(prog)s <command> [options]",
        description="Evaluate, govern and deploy healthcare AI agents.",
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action=_Overview, help="show the commands to start with, and exit"
    )
    parser.add_argument(
        "--help-all", action=_Overview, const=True, help="show every command, and exit"
    )
    parser.add_argument("--version", action="version", version=f"mendari {__version__}")
    # `prog`: argparse would name each command after this parser's usage line.
    sub = parser.add_subparsers(dest="command", required=True, prog=parser.prog)

    def agent_command(name: str, handler: Any, help_text: str, providers: bool = False) -> Any:
        command = sub.add_parser(name, help=help_text)
        _agent_argument(command)
        if providers:
            command.add_argument(
                "--provider",
                action="append",
                metavar="SLOT=VARIANT",
                help="use a non-default provider variant (repeatable)",
            )
        command.set_defaults(handler=handler)
        return command

    sub.add_parser("list", help="list the agents this machine serves").set_defaults(
        handler=cmd_list
    )
    agent_command("validate", cmd_validate, "validate an agent and build every provider variant")
    agent_command("test", cmd_test, "run the agent's behaviour tests", providers=True)

    probing = agent_command(
        "probe",
        cmd_probe,
        "find failures by rewriting its cases; keep them as tests",
        providers=True,
    )
    probing.description = (
        "Changes the text of the agent's own cases in ways that keep their meaning, decides "
        "each variant as the original is decided, and reports the answers that changed: a "
        "critical answer lost, or a variant that would be automated while wrong or critical, "
        "first. The report holds ids and counts, never a case's text. Offline by default; a "
        "model writes paraphrases, translations and denials only when asked."
    )
    probing.add_argument(
        "--dataset",
        help="the labelled cases to change: one of the agent's datasets, by name, or a JSONL "
        "path (default: its benchmark dataset, else its first, else its behaviour tests)",
    )
    probing.add_argument(
        "--from-tests",
        action="store_true",
        help="change the agent's own behaviour tests instead of a dataset",
    )
    probing.add_argument(
        "--transform",
        action="append",
        metavar="NAME[,NAME]",
        help="what to change (repeatable). Offline, the default: typos, case, punctuation, "
        "whitespace, additions, injection. Written by a model: paraphrase, negation, "
        "translate:<language>",
    )
    probing.add_argument(
        "--language",
        action="append",
        metavar="LANGUAGE",
        help="also translate into this language with a model, as es or 'haitian creole' "
        "(repeatable): the same as --transform translate:<language>",
    )
    probing.add_argument(
        "--model",
        help="the model that writes paraphrase, negation and translate variants, asked as an "
        "llm variant is (default: $LLM_MODEL; it is sent the cases' text and nothing else)",
    )
    probing.add_argument(
        "--variants-from",
        metavar="FILE",
        help="also try the variants of this JSON Lines file: ones you wrote, or ones "
        "--variants-out kept (alone, when no --transform is named)",
    )
    probing.add_argument(
        "--variants-out",
        metavar="FILE",
        help="write every variant tried to FILE, outside the agent: to read, or to try again "
        "with --variants-from",
    )
    probing.add_argument(
        "--field",
        action="append",
        metavar="FIELD",
        help="a text field to change, as message.body (repeatable; default: the text the "
        "agent reads)",
    )
    probing.add_argument(
        "--limit",
        type=_case_limit,
        default=PROBE_CASES,
        metavar="N",
        help=f"probe at most N cases, or `all` (default {PROBE_CASES})",
    )
    probing.add_argument(
        "--variants",
        type=int,
        default=1,
        metavar="N",
        help="variants of each case by each transformation (default 1)",
    )
    probing.add_argument("--seed", type=int, default=0, help="the seed (default 0)")
    probing.add_argument(
        "--workers", type=int, help="parallel decisions (default: 8 for a remote agent, else 1)"
    )
    probing.add_argument(
        "--dry-run",
        action="store_true",
        help="say what would run and how many calls it would make; decide and call nothing",
    )
    probing.add_argument(
        "--write",
        action="store_true",
        help="keep each failure in the agent as a behaviour test `mendari test` runs "
        "(tests/probe.yaml), once",
    )
    probing.add_argument(
        "--confirm",
        action="append",
        metavar="ID",
        help="keep a test that waits in tests/probe-unconfirmed.yaml, once you have read what "
        "it expects (repeatable; probes nothing)",
    )
    probing.add_argument(
        "--fail-on",
        choices=("critical", "any"),
        help=f"exit {EXIT_FOUND} on a critical finding (a critical answer lost, or a variant "
        "automated while wrong or critical), or on any answer that changed",
    )
    probing.add_argument("--json", action="store_true", help="print the JSON report")
    probing.add_argument("--out", help="also write the JSON report here")

    decide = agent_command("decide", cmd_decide, "run one decision", providers=True)
    decide.add_argument("--input", help="JSON input file ('-' for stdin)")
    decide.add_argument("--text", help="shortcut: message body text")
    decide.add_argument("--fhir", action="store_true", help="input is a FHIR Bundle or resource")
    decide.add_argument("--db", help="record the decision in this audit database")

    evaluate = agent_command("eval", cmd_eval, "evaluate on a labelled dataset", providers=True)
    evaluate.description = (
        "Decides every case of a labelled dataset as a live decision is decided, and reports "
        "the agent's accuracy and calibration, the mix of outcomes (AUTO, HUMAN_REVIEW, ABSTAIN, "
        "BLOCK), each critical label's cases (how many were detected, how many automated), and "
        "the operating point: what the agent's automation threshold lets through, each risk "
        "with its exact upper bound. A share is k of n with its interval. The report holds "
        "counts, never a case's text."
    )
    evaluate.add_argument(
        "--dataset", required=True, help="one of the agent's datasets, by name, or a JSONL path"
    )
    evaluate.add_argument(
        "--baseline",
        default="auto",
        help="'auto' (the agent's benchmark, never one an agent from a registry carries), 'none', "
        "or a report JSON path",
    )
    evaluate.add_argument("--limit", type=int, help="only evaluate the first N cases")
    evaluate.add_argument(
        "--workers", type=int, help="parallel decisions (default: 8 for remote providers, else 1)"
    )
    evaluate.add_argument("--out", help="write the full JSON report here")
    evaluate.add_argument("--db", help="also store the report in this database (dashboard)")
    evaluate.add_argument("--json", action="store_true", help="print the JSON report")
    evaluate.add_argument(
        "--markdown",
        action="store_true",
        help="print the result as Markdown, for a CI job summary: counts with their intervals, "
        "and cases by id, never their text",
    )
    evaluate.add_argument(
        "--fail-on-degradation",
        action="store_true",
        help=f"exit {EXIT_DEGRADED} when local validation detects degradation",
    )
    # Two families of options that name the same figures, and differ in what they do. A limit
    # is on what this run measured, and a result that does not meet one fails the run (the gate
    # a CI job holds a change to, docs/guides/evaluate-in-ci.md). A target is what a recommended
    # automation threshold must keep to, with a stated confidence: advice, which fails nothing
    # (the operating point, docs/guides/choosing-a-threshold.md).
    # (A group's description is printed as written; an option's help doubles its `%`.)
    limits = evaluate.add_argument_group(
        "limits",
        f"What this run's result must be: one that does not meet a limit, or is worse than the "
        f"result --no-worse-than names, is exit {EXIT_DEGRADED}. A share is written 90% or 0.9.",
    )
    limits.add_argument(
        "--min-accuracy",
        type=_share,
        metavar="SHARE",
        help="the least accuracy on the agent's primary question",
    )
    limits.add_argument(
        "--min-critical-detection",
        type=_share,
        metavar="SHARE",
        help="the least share of each critical label's cases that must be detected",
    )
    limits.add_argument(
        "--max-critical-misses",
        type=_whole,
        metavar="N",
        help="the most critical misses: cases of each critical label that may be automated, so "
        "that no person reads them",
    )
    limits.add_argument(
        "--max-false-automation",
        type=_share,
        metavar="SHARE",
        help="the most false automation: cases automated with a wrong answer, as a share of the "
        "labelled cases",
    )
    limits.add_argument(
        "--no-worse-than",
        metavar="RESULT",
        help="an earlier result of this agent on the same dataset (as --out writes one) that "
        "this run must be no worse than: no critical label detected less often, no more "
        "critical misses, and accuracy and the share of automated cases that are wrong within "
        "the agent's tolerance",
    )
    targets = evaluate.add_argument_group(
        "targets",
        "What a recommended automation threshold must keep to: with a target, the report's "
        "operating point names the lowest threshold these cases support, with a stated "
        "confidence. Advice: a target never fails the run. A share is written 1% or 0.01.",
    )
    targets.add_argument(
        "--target-critical-miss",
        type=_target,
        metavar="SHARE",
        help="the most critical misses the threshold may leave: critical cases that are "
        "automated, so that no person reads them, as a share of each critical label's cases",
    )
    targets.add_argument(
        "--target-critical-miss-of",
        choices=("critical", "all"),
        help="what --target-critical-miss is a share of: each critical label's cases (critical, "
        "the default) or all cases (all)",
    )
    targets.add_argument(
        "--target-false-automation",
        type=_target,
        metavar="SHARE",
        help="the most false automation the threshold may leave: cases automated with a wrong "
        "answer, as a share of the labelled cases",
    )
    targets.add_argument(
        "--target-confidence",
        type=_confidence,
        metavar="LEVEL",
        help="the confidence the targets are held with, and each bound of the operating point "
        "is stated with (default 95%%)",
    )
    evaluate.add_argument(
        "--share",
        action="store_true",
        help="share the counts (never cases) with the registry as this site, signed, answering "
        "a ticket asked for before the run (`mendari site init` first)",
    )
    evaluate.add_argument(
        "--consent-remote",
        action="store_true",
        help="a shared run may call a provider off this machine (recorded)",
    )
    evaluate.add_argument(
        "--ticket", metavar="FILE", help="a ticket fetched on another machine (offline sharing)"
    )
    evaluate.add_argument(
        "--supersedes", metavar="REPORT", help="a shared run corrects this report of the site's"
    )
    evaluate.add_argument(
        "--registry", help="registry URL for --share or --publish (default: logged into)"
    )
    evaluate.add_argument(
        "--publish",
        action="store_true",
        help="publish this run as the publisher's signed evaluation (counts only) of the listed "
        "version, on one of the agent's own datasets (ADR-0020); `mendari push --evaluate` "
        "publishes one for every configuration",
    )
    evaluate.add_argument(
        "--key", metavar="FILE", help="the namespace's publisher key that signs --publish"
    )
    evaluate.add_argument(
        "--labelled-by",
        metavar="TEXT",
        help="with --publish: who labelled the dataset, in one line (it is signed and public)",
    )
    evaluate.add_argument(
        "--share-out",
        metavar="FILE",
        help="write the signed submission --share sends to FILE, to read first (sends nothing)",
    )

    data = sub.add_parser("data", help="labelled datasets").add_subparsers(
        dest="data_command", required=True
    )
    data_coverage = data.add_parser(
        "coverage", help="what a labelled set covers: distinct cases, positives and mix"
    )
    _agent_argument(data_coverage)
    data_coverage.add_argument(
        "--dataset", required=True, help="one of the agent's datasets, by name, or a JSONL path"
    )
    data_coverage.add_argument("--json", action="store_true", help="print the JSON report")
    data_coverage.add_argument(
        "--ids", action="store_true", help="also list the ids of duplicate cases"
    )
    data_coverage.set_defaults(handler=cmd_data_coverage)
    data_check = data.add_parser(
        "check", help="a dataset against its agent: schema, labels, duplicates, training overlap"
    )
    _agent_argument(data_check)
    data_check.add_argument(
        "file", nargs="?", help="a JSONL file (default: every dataset the agent declares)"
    )
    data_check.add_argument("--json", action="store_true", help="print the JSON reports")
    data_check.add_argument(
        "--phi",
        action="store_true",
        help="also look for what reads as a person's details (kinds and lines, never the text)",
    )
    data_check.set_defaults(handler=cmd_data_check)

    calibrate = agent_command(
        "calibrate",
        cmd_calibrate,
        "fit per-question temperatures for an llm or an agent variant (live calls)",
    )
    calibrate.add_argument("--slot", required=True, help="provider slot")
    calibrate.add_argument("--variant", required=True, help="llm or agent variant of the slot")
    calibrate.add_argument("--dataset", default="calibration", help="labelled dataset")
    calibrate.add_argument("--limit", type=int, default=200, help="records to use (default 200)")
    calibrate.add_argument("--workers", type=int, default=8, help="parallel calls (default 8)")

    paired_runs = sub.add_parser(
        "compare", help="compare two evaluation runs case by case (from their ledgers)"
    )
    paired_runs.add_argument("a", help="run A: a ledger (.jsonl) or a report with its ledger")
    paired_runs.add_argument("b", help="run B, on the same dataset")
    paired_runs.add_argument(
        "--dataset", required=True, help="one of the agent's datasets, by name, or a JSONL path"
    )
    paired_runs.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    paired_runs.add_argument("--resamples", type=int, default=2000, help="bootstrap resamples")
    paired_runs.add_argument("--alpha", type=float, default=0.05, help="level (default 0.05)")
    paired_runs.add_argument("--json", action="store_true", help="JSON, cases by hash only")
    paired_runs.add_argument("--out", help="also write the comparison as JSON")
    paired_runs.set_defaults(handler=cmd_compare)

    board = agent_command(
        "leaderboard", cmd_leaderboard, "compare provider configurations on the same datasets"
    )
    board.add_argument(
        "--run",
        action="append",
        default=[],
        help="a configuration to evaluate, e.g. reader=rules (repeat)",
    )
    board.add_argument(
        "--dataset", action="append", default=[], help="dataset for every --run (repeat)"
    )
    board.add_argument("--limit", type=int, help="only the first N cases of each dataset")
    board.add_argument("--workers", type=int, help="parallel decisions per run")
    board.add_argument(
        "--report",
        action="append",
        default=[],
        help="add an existing evaluation report, e.g. one from a deployment (repeat)",
    )
    board.add_argument("--out", help="leaderboard file (default: <agent>/evals/leaderboard.json)")
    board.add_argument("--fresh", action="store_true", help="replace instead of merging")

    card = agent_command("card", cmd_card, "render the Decision Card")
    card.add_argument(
        "--write", action="store_true", help="write DECISION_CARD.md into the agent's folder"
    )

    schema = agent_command("schema", cmd_schema, "print the input (or output) JSON Schema")
    schema.add_argument("--output", action="store_true", help="print the output schema")

    serve = sub.add_parser("serve", help="run the API, CDS Hooks services and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_port, default=8000)
    serve.add_argument(
        "--db", default="var/mendari.db", help="audit database: SQLite path or postgresql:// URL"
    )
    serve.add_argument("--datasets-dir", help="extra evaluation datasets for the dashboard")
    serve.add_argument(
        "--provider",
        action="append",
        metavar="SLOT=VARIANT",
        help="instead of $MENDARI_PROVIDERS",
    )
    serve.add_argument(
        "--proxy-headers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="take the client's address from X-Forwarded-For sent by a trusted proxy "
        "(default: on, as uvicorn)",
    )
    serve.add_argument(
        "--forwarded-allow-ips",
        help="the proxies trusted for X-Forwarded-For: addresses or networks, comma-separated "
        "(default: $FORWARDED_ALLOW_IPS, else 127.0.0.1); rate limits on sign-in count per "
        "client address, so set it to your proxy's",
    )
    serve.set_defaults(handler=cmd_serve)

    audit = sub.add_parser("audit", help="audit trail tools").add_subparsers(
        dest="audit_command", required=True
    )
    verify = audit.add_parser("verify", help="verify the audit hash chain")
    verify.add_argument("--db", default="var/mendari.db", help="SQLite path or postgresql:// URL")
    checked = verify.add_mutually_exclusive_group()
    checked.add_argument(
        "--notes",
        action="store_true",
        help="also check each kept note against its chained commitment (with $MENDARI_PHI_KEY)",
    )
    checked.add_argument(
        "--since-checkpoint",
        action="store_true",
        help="only the events after the last checkpoint (mendari audit checkpoint)",
    )
    verify.add_argument(
        "--against",
        help="a signed checkpoint, or /api/audit/checkpoint's answer (JSON file), whose head the "
        "chain must hold (with --public-key)",
    )
    verify.add_argument("--public-key", help="the checkpoint key's public half (base64)")
    verify.set_defaults(handler=cmd_audit_verify)
    checkpoint = audit.add_parser(
        "checkpoint", help="verify from the last checkpoint and record the head as the next"
    )
    checkpoint.add_argument("--db", required=True, help="the server's database")
    checkpoint.set_defaults(handler=cmd_audit_checkpoint)
    audit_export = audit.add_parser(
        "export", help="export the audit trail as FHIR NDJSON (AuditEvent, Provenance)"
    )
    audit_export.add_argument(
        "--db", default="var/mendari.db", help="SQLite path or postgresql:// URL"
    )
    _agent_option(audit_export, help="only this agent's decisions (namespace/name)")
    audit_export.add_argument(
        "--org", help="only this organization's events (default: every organization's)"
    )
    audit_export.add_argument(
        "--administrative",
        action="store_true",
        help="the events on no decision instead: keys, clients, people, approvals, holds, "
        "reports (with --org, that organization's; without, the deployment's own too)",
    )
    audit_export.add_argument("--since", help="events at or after this time (ISO 8601, UTC)")
    audit_export.add_argument("--until", help="events before this time (ISO 8601, UTC)")
    audit_export.add_argument("--out", help="write to this file (default: standard output)")
    audit_export.set_defaults(handler=cmd_audit_export)

    usage = sub.add_parser(
        "usage",
        help="the deployment's totals: today's calls, the usage ledger, the last purge and more",
    )
    usage.add_argument("--db", default="var/mendari.db", help="SQLite path or postgresql:// URL")
    usage.add_argument(
        "--deployments", type=int, default=20, help="how many deployment records (newest first)"
    )
    usage.add_argument("--days", type=int, default=30, help="the ledger's last days (1-366)")
    usage.add_argument("--org", help="one organization's ledger (default: every one)")
    usage.set_defaults(handler=cmd_usage)

    automation = sub.add_parser(
        "automation", help="hold or lift automation (works with the server down)"
    ).add_subparsers(dest="automation_command", required=True)
    hold = automation.add_parser(
        "hold", help="stop automation: every decision it covers goes to a person"
    )
    scope = hold.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--all-orgs", action="store_true", help="every organization (the deployment)"
    )
    scope.add_argument("--org", help="one organization")
    _agent_option(hold, help="one agent (namespace/name); default: every agent")
    hold.add_argument("--digest", help="one version of --agent (sha256:...)")
    hold.add_argument(
        "--reason", required=True, help="why, in a few words (at most 200 characters)"
    )
    hold.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    hold.set_defaults(handler=cmd_automation_hold)
    lift = automation.add_parser("lift", help="lift a hold (any organization's)")
    lift.add_argument("hold_id")
    lift.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    lift.set_defaults(handler=cmd_automation_lift)
    listed = automation.add_parser("list", help="every hold in force")
    listed.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    listed.set_defaults(handler=cmd_automation_list)

    access = sub.add_parser(
        "access",
        help="organizations, memberships and invitations (MENDARI_ACCOUNTS=memberships)",
    ).add_subparsers(dest="access_command", required=True)
    org_cmd = access.add_parser("org", help="organizations").add_subparsers(
        dest="org_command", required=True
    )
    org_create = org_cmd.add_parser("create", help="create an organization's record")
    org_create.add_argument("org", help="its id: lower-case letters, digits and hyphens")
    org_create.add_argument("--name", required=True, help="its name, as people read it")
    org_create.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    org_create.set_defaults(handler=cmd_access_org_create)
    org_list = org_cmd.add_parser("list", help="every organization's record")
    org_list.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    org_list.set_defaults(handler=cmd_access_org_list)
    bootstrap = access.add_parser(
        "bootstrap", help="the owner's administrator membership of `default` (idempotent)"
    )
    bootstrap.add_argument(
        "--email", required=True, help="the address the owner signs in with, verified"
    )
    bootstrap.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    bootstrap.set_defaults(handler=cmd_access_bootstrap)
    invite = access.add_parser("invite", help="an invitation link to pass on (no e-mail sent)")
    invite.add_argument("--org", required=True)
    invite.add_argument("--email", required=True, help="who may accept it, signed in, verified")
    invite.add_argument(
        "--role", action="append", required=True, help="a role it grants (repeat for several)"
    )
    invite.add_argument("--days", type=int, default=7, help="how long it stays valid (1-30)")
    invite.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    invite.set_defaults(handler=cmd_access_invite)
    members = access.add_parser("members", help="an organization's memberships in force")
    members.add_argument("--org", required=True)
    members.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    members.set_defaults(handler=cmd_access_members)
    requests = access.add_parser("requests", help="open requests for access")
    requests.add_argument("--org", help="to one organization")
    requests.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    requests.set_defaults(handler=cmd_access_requests)
    check = access.add_parser(
        "check", help="preflight: which organizations in use have no record (read-only)"
    )
    check.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    check.set_defaults(handler=cmd_access_check)
    for credentials, about, handler in (
        ("keys", "API keys, with their status (read-only)", cmd_access_keys),
        ("clients", "Backend Services clients (read-only)", cmd_access_clients),
    ):
        listing = access.add_parser(credentials, help=about)
        listing.add_argument("--org", help="of one organization")
        listing.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
        listing.set_defaults(handler=handler)
    revoke = access.add_parser(
        "revoke", help="break-glass: revoke a key, a client, or all of an organization's"
    ).add_subparsers(dest="revoke_command", required=True)
    for what, about in (("key", "an API key"), ("client", "a Backend Services client")):
        one = revoke.add_parser(what, help=f"{about}, of any organization")
        one.add_argument("id", help=f"its id (mendari access {what}s)")
        one.add_argument("--reason", required=True, help="why (chained; at most 200 characters)")
        one.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
        one.set_defaults(handler=cmd_access_revoke, what=what)
    every = revoke.add_parser("all", help="every key and client of an organization in force")
    every.add_argument("--org", required=True)
    every.add_argument("--reason", required=True, help="why (chained; at most 200 characters)")
    every.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    every.set_defaults(handler=cmd_access_revoke, what="all")
    end = access.add_parser(
        "end-sessions", help="break-glass: sign out every person, every EHR launch, or both"
    )
    end.add_argument("--kind", required=True, choices=["people", "launches", "all"])
    end.add_argument("--reason", required=True, help="why (chained; at most 200 characters)")
    end.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    end.set_defaults(handler=cmd_access_end_sessions)

    feedback = sub.add_parser("feedback", help="thumbs up / down feedback").add_subparsers(
        dest="feedback_command", required=True
    )
    export = feedback.add_parser(
        "export", help="export rated or reviewed decisions as a dataset of cases"
    )
    _agent_argument(export, "agent id or directory")
    export.add_argument("--db", default="var/mendari.db", help="SQLite path or postgresql:// URL")
    export.add_argument(
        "--source",
        choices=["ratings", "reviews"],
        default="ratings",
        help="ratings: thumbs feedback, to label (default); reviews: decisions a reviewer "
        "resolved or an outcome was recorded for, each with the label a person chose",
    )
    export.add_argument("--rating", choices=["down", "up"], help="with ratings (default: down)")
    export.add_argument(
        "--org", help="only this organization's decisions (default: every organization's)"
    )
    export.add_argument(
        "--limit", type=int, default=1000, help="the newest N decisions (default 1000)"
    )
    export.add_argument("--out", required=True, help="JSONL file to write")
    export.set_defaults(handler=cmd_feedback_export)

    sandbox = sub.add_parser("sandbox", help="synthetic sandbox tools").add_subparsers(
        dest="sandbox_command", required=True
    )
    demo = "the bundled demo agent"  # DEMO_AGENT: its id is long enough to wrap in a help text
    its_cases = "one of the agent's datasets or a JSONL file (default: its benchmark dataset)"
    seed = sandbox.add_parser(
        "seed", help="load an agent's synthetic messages into FHIR as US Core resources"
    )
    seed.add_argument("--fhir", required=True, help="FHIR R4 base URL")
    _agent_option(
        seed,
        default=DEMO_AGENT,
        help=f"the message agent whose cases to load, by id or directory (default: {demo})",
    )
    seed.add_argument("--dataset", help=f"the cases to load: {its_cases}")
    seed.add_argument("--count", type=int, default=40)
    seed.add_argument("--wait", type=int, default=180, help="seconds to wait for the server")
    seed.set_defaults(handler=cmd_sandbox_seed)
    worker = sandbox.add_parser("worker", help="route new FHIR Communications through the engine")
    worker.add_argument("--fhir", required=True)
    worker.add_argument("--engine", required=True, help="engine base URL")
    _agent_option(
        worker,
        default=DEMO_AGENT,
        help=f"the message agent that decides them, by id or directory (default: {demo})",
    )
    worker.add_argument("--interval", type=float, default=5.0)
    worker.add_argument("--once", action="store_true")
    worker.set_defaults(handler=cmd_sandbox_worker)
    cds_client = sandbox.add_parser(
        "cds-client",
        help="call an agent's CDS Hooks service over HTTP as an EHR does, with signed tokens "
        "(LP-23e)",
    )
    cds_client.add_argument("--engine", required=True, help="the engine's URL, to call")
    cds_client.add_argument(
        "--fhir",
        required=True,
        help="the FHIR server holding the sandbox's cases (mendari sandbox seed), sent as "
        "fhirServer",
    )
    _agent_option(
        cds_client,
        default=DEMO_AGENT,
        help=f"the agent whose service to call, by id or directory (default: {demo})",
    )
    cds_client.add_argument(
        "--limit", type=int, default=5, help="call it for at most this many cases (default 5)"
    )
    cds_client.add_argument(
        "--audience",
        help="the engine's MENDARI_PUBLIC_URL, which tokens are addressed to (default: --engine)",
    )
    cds_client.add_argument(
        "--listen",
        type=_listen,
        default=("127.0.0.1", 8300),
        metavar="HOST:PORT",
        help="where to serve the client's JWK Set (default 127.0.0.1:8300; port 0: any)",
    )
    cds_client.add_argument(
        "--iss",
        help="the client's URL as the engine reaches it: its iss, and its JWK Set's "
        "(default: http://HOST:PORT of --listen)",
    )
    cds_client.add_argument("--id", default="sandbox-client", help="its id in the registry")
    cds_client.add_argument("--org", default="default", help="its organization in the registry")
    cds_client.set_defaults(handler=cmd_sandbox_cds_client)
    smart = sandbox.add_parser(
        "smart-launch", help="run a SMART EHR launch against the sandbox and decide the work items"
    )
    smart.add_argument("--engine", required=True, help="engine URL (its MENDARI_PUBLIC_URL)")
    smart.add_argument("--launcher", help="smart-launcher-v2 base URL")
    smart.add_argument("--fhir", help="FHIR R4 base URL, to find a patient")
    smart.add_argument(
        "--launch-url",
        metavar="URL",
        help="instead of --launcher and --fhir: the launch URL an EHR made (the engine's "
        "/smart/launch with the EHR's iss and launch); read the patient in context and log out",
    )
    _agent_option(
        smart,
        default=DEMO_AGENT,
        help=f"the agent to launch, by id or directory: one with a FHIR mapping (default: {demo})",
    )
    smart.add_argument("--patient", help="patient id (default: one with a work item)")
    smart.add_argument("--limit", type=int, default=5, help="decide at most this many items")
    smart.add_argument("--strict", action="store_true", help="fail on any US Core issue")
    smart.add_argument(
        "--expect-org",
        metavar="ORG",
        help="with sign-in on: fail unless each decision is this organization's and its chain "
        "names the launcher's user as <issuer>|<subject> (reads --db)",
    )
    smart.add_argument(
        "--db", default="var/mendari.db", help="the engine's database, for --expect-org"
    )
    smart.set_defaults(handler=cmd_sandbox_smart_launch)
    pilot = sandbox.add_parser(
        "pilot-check",
        help="the v0.6 pilot: sign-in, API keys, PHI mode, review, governance, monitoring",
    )
    pilot.add_argument("--engine", required=True, help="engine URL (its MENDARI_PUBLIC_URL)")
    pilot.add_argument("--messages", type=int, default=12, help="portal messages to decide")
    _agent_option(
        pilot,
        default=DEMO_AGENT,
        help=f"the message agent the pilot runs, by id or directory (default: {demo})",
    )
    pilot.add_argument(
        "--approve",
        metavar="AGENT",
        help="a risk level 2 or 3 agent the engine serves, by id or directory: the committee "
        "approves its automation and its next decision must carry the approval. The demo agent "
        "is level 0 and needs none, so without this the approval is left out, and reported so",
    )
    pilot.set_defaults(handler=cmd_sandbox_pilot_check)
    bulk_check = sandbox.add_parser(
        "bulk-check", help="Bulk export the sandbox, evaluate it, and compare with the dataset file"
    )
    bulk_check.add_argument("--fhir", required=True, help="the sandbox's FHIR R4 base URL")
    _agent_option(
        bulk_check,
        default=DEMO_AGENT,
        help=f"the agent whose cases the sandbox was seeded with, by id or directory "
        f"(default: {demo})",
    )
    bulk_check.add_argument("--dataset", help=f"the labelled cases it was seeded from: {its_cases}")
    bulk_check.set_defaults(handler=cmd_sandbox_bulk_check)

    bulk = sub.add_parser("bulk", help="Bulk FHIR export into local datasets").add_subparsers(
        dest="bulk_command", required=True
    )
    export_bulk = bulk.add_parser("export", help="run a Bulk FHIR $export and download the files")
    export_bulk.add_argument("--fhir", required=True, help="FHIR R4 base URL (on the allow-list)")
    export_bulk.add_argument("--out", required=True, help="directory for the NDJSON files")
    export_bulk.add_argument("--group", help="export this Group (default: all patients)")
    export_bulk.add_argument("--type", help="resource types, comma-separated")
    export_bulk.add_argument("--since", help="only resources changed since (FHIR instant)")
    export_bulk.add_argument("--client-id", help="SMART Backend Services client id")
    export_bulk.add_argument("--private-key", help="PEM file (RS384 or ES384) for the assertion")
    export_bulk.add_argument("--key-id", default="mendari", help="the key's kid in your JWKS")
    export_bulk.add_argument("--scope", default="system/*.rs", help="backend services scope")
    export_bulk.set_defaults(handler=cmd_bulk_export)
    dataset_bulk = bulk.add_parser("dataset", help="turn an export into an agent's local dataset")
    _agent_option(dataset_bulk, required=True, help="namespace/name")
    dataset_bulk.add_argument("--ndjson", required=True, help="directory of NDJSON files")
    dataset_bulk.add_argument(
        "--labels", required=True, help='JSONL of {"id", "labels"} (a dataset file works)'
    )
    dataset_bulk.add_argument(
        "--key-system",
        default="https://mendari.co/fhir/synthetic-case",
        help="identifier system that holds the case id (default: the sandbox's)",
    )
    dataset_bulk.add_argument("--out", required=True, help="dataset file to write (JSONL)")
    dataset_bulk.set_defaults(handler=cmd_bulk_dataset)

    keys = sub.add_parser("keys", help="publisher keys").add_subparsers(
        dest="keys_command", required=True
    )
    generate_key = keys.add_parser("generate", help="create an Ed25519 publisher key pair")
    generate_key.add_argument("--out", default=".", help="directory for NAME.key and NAME.pub")
    generate_key.add_argument("--name", default="publisher")
    generate_key.set_defaults(handler=cmd_keys_generate)
    register_key = keys.add_parser("register", help="register a public key for a namespace")
    register_key.add_argument("--namespace", required=True)
    register_key.add_argument("--public-key", required=True, help="PEM file")
    register_key.add_argument("--registry", help="registry URL (default: the one logged into)")
    register_key.set_defaults(handler=cmd_keys_register)
    login_cmd = sub.add_parser("login", help="remember a registry (and a publisher token)")
    login_cmd.add_argument("--registry", required=True, help="registry URL")
    login_cmd.add_argument("--token", help="publisher token (omit for read-only)")
    login_cmd.set_defaults(handler=cmd_login)
    push_cmd = sub.add_parser(
        "push",
        help="sign and publish an agent (with --evaluate, its evaluations too)",
        description="Sign an agent and publish it to the registry. With --evaluate DATASET, "
        "then evaluate each of its configurations on that dataset of its own and publish "
        "every one, signed with the same key: what the agent's evaluation page on the Agent "
        "Hub and its README badge show.",
    )
    _agent_argument(push_cmd, "namespace/name[@version] or a directory")
    push_cmd.add_argument("--key", required=True, help="publisher private key (PEM)")
    push_cmd.add_argument("--registry", help="registry URL (default: the one logged into)")
    push_cmd.add_argument(
        "--evaluate",
        metavar="DATASET",
        help="then evaluate each configuration on this dataset of the agent's own and publish "
        "every one, signed with --key (counts only): what the agent's evaluation page and its "
        "badge show. A version already published, the same build, is not pushed again",
    )
    push_cmd.add_argument(
        "--configuration",
        action="append",
        default=[],
        metavar="SLOT=VARIANT",
        help="with --evaluate: a configuration to evaluate, e.g. reader=rules (repeat; a slot "
        f"left out keeps its default). Default: every configuration, up to {MAX_EVALUATED}",
    )
    push_cmd.add_argument(
        "--labelled-by",
        metavar="TEXT",
        help="with --evaluate: who labelled the dataset, in one line (it is signed and public)",
    )
    push_cmd.add_argument(
        "--workers",
        type=int,
        help="with --evaluate: parallel decisions (default: 8 for remote providers, else 1)",
    )
    push_cmd.set_defaults(handler=cmd_push)
    pull_cmd = sub.add_parser("pull", help="download, verify and cache an agent from the registry")
    pull_cmd.add_argument("ref", help="namespace/name[@version]")
    pull_cmd.add_argument("--registry", help="registry URL (default: the one logged into)")
    pull_cmd.add_argument(
        "--accept-new-key", action="store_true", help="trust a key the namespace rotated to"
    )
    pull_cmd.set_defaults(handler=cmd_pull)
    yank_cmd = sub.add_parser(
        "yank", help="recall a version your namespace published (signed with its key)"
    )
    yank_cmd.add_argument("ref", help="namespace/name@version")
    yank_cmd.add_argument("--reason", required=True, help="why, in one line (it is public)")
    yank_cmd.add_argument(
        "--severity", required=True, choices=("low", "medium", "high", "critical")
    )
    yank_cmd.add_argument("--key", required=True, help="publisher private key (PEM)")
    yank_cmd.add_argument("--registry", help="registry URL (default: the one logged into)")
    yank_cmd.set_defaults(handler=cmd_yank)
    search_cmd = sub.add_parser("search", help="search the registry")
    search_cmd.add_argument("query", nargs="*")
    search_cmd.add_argument("--risk", type=int, help="risk level 0-4")
    search_cmd.add_argument("--standard", default="", help='e.g. "FHIR" or "CDS Hooks order-sign"')
    search_cmd.add_argument(
        "--publisher-type", default="", choices=["", "community", "verified", "institutional"]
    )
    search_cmd.add_argument("--registry", help="registry URL (default: the one logged into)")
    search_cmd.add_argument("--json", action="store_true")
    search_cmd.set_defaults(handler=cmd_search)
    agent_cmd = sub.add_parser("archive", help="signed agent archives").add_subparsers(
        dest="archive_command", required=True
    )
    build_agent = agent_cmd.add_parser("build", help="build a signed .mendari archive")
    _agent_argument(build_agent, "namespace/name[@version] or a directory")
    build_agent.add_argument("--key", required=True, help="publisher private key (PEM)")
    build_agent.add_argument("--out", default="dist", help="directory for the archive")
    build_agent.set_defaults(handler=cmd_agent_build)
    verify_agent = agent_cmd.add_parser("verify", help="verify an archive's files and signature")
    verify_agent.add_argument("archive")
    verify_agent.add_argument("--public-key", required=True, help="publisher public key (PEM)")
    verify_agent.set_defaults(handler=cmd_agent_verify)

    site_cmd = sub.add_parser(
        "site", help="this machine as an evaluating site (LP-22): its key, tickets"
    ).add_subparsers(dest="site_command", required=True)
    site_init = site_cmd.add_parser(
        "init", help="make this machine a site's: a key and a commitment secret, kept here"
    )
    site_init.add_argument("--site", required=True, help="the site's id, from the registry")
    site_init.add_argument(
        "--out", default="site-key.json", help="the key file to give the registry"
    )
    site_init.set_defaults(handler=cmd_site_init, command_name="init")
    site_key_cmd = site_cmd.add_parser("key", help="a new key for this site (--new)")
    site_key_cmd.add_argument("--new", action="store_true", help="make a new key")
    site_key_cmd.add_argument(
        "--out", default="site-key.json", help="the key file to give the registry"
    )
    site_key_cmd.set_defaults(handler=cmd_site_key)
    site_ticket = site_cmd.add_parser(
        "ticket", help="ask the registry for a ticket before an evaluation (signed)"
    )
    _agent_argument(site_ticket, "agent id (namespace/name[@version])", nargs="?")
    site_ticket.add_argument("--dataset", help="the site's labelled cases (JSONL)")
    site_ticket.add_argument(
        "--request", metavar="FILE", help="send a signed request carried from the site (--out)"
    )
    site_ticket.add_argument(
        "--supersedes", metavar="REPORT", help="the ticket's report corrects this report"
    )
    site_ticket.add_argument(
        "--provider",
        action="append",
        metavar="SLOT=VARIANT",
        help="a variant for a slot (default: the deterministic arm)",
    )
    site_ticket.add_argument(
        "--consent-remote",
        action="store_true",
        help="the configuration may call a provider off this machine (recorded)",
    )
    site_ticket.add_argument("--registry", help="registry URL (default: logged into)")
    site_ticket.add_argument(
        "--out", metavar="FILE", help="write the signed request to FILE and send nothing"
    )
    site_ticket.set_defaults(handler=cmd_site_ticket)
    site_submit = site_cmd.add_parser(
        "submit", help="send a signed submission `mendari eval --share-out` wrote"
    )
    site_submit.add_argument("submission", help="the signed submission (JSON)")
    site_submit.add_argument("--ticket", metavar="FILE", help="its ticket file (default: kept)")
    site_submit.add_argument("--registry", help="registry URL (default: logged into)")
    site_submit.set_defaults(handler=cmd_site_submit)
    registry_cmd = sub.add_parser("registry", help="the agent registry service").add_subparsers(
        dest="registry_command", required=True
    )
    serve_registry = registry_cmd.add_parser(
        "serve",
        help="run the registry: public reads, publishes by owned token; every other write is "
        "the operator's commands below, on its database",
    )
    serve_registry.add_argument(
        "--db", default="var/registry.db", help="SQLite path or postgresql:// URL"
    )
    serve_registry.add_argument("--host", default="127.0.0.1")
    serve_registry.add_argument("--port", type=_port, default=8100)
    serve_registry.set_defaults(handler=cmd_registry_serve)
    verify_registry = registry_cmd.add_parser(
        "verify", help="check the registry's chain of events, read-only"
    )
    verify_registry.add_argument(
        "--db", default="var/registry.db", help="SQLite path or postgresql:// URL"
    )
    verify_registry.add_argument(
        "--checkpoint",
        metavar="FILE",
        help="a statement `mendari registry checkpoint` signed, kept outside the database: "
        "the chain must still hold its head",
    )
    verify_registry.add_argument(
        "--public-key",
        help="the registry's public key (base64, as GET /v1/registry-key shows it) to check "
        "--checkpoint with; otherwise $MENDARI_REGISTRY_SIGNING_KEY",
    )
    verify_registry.set_defaults(handler=cmd_registry_verify)
    checkpoint_registry = registry_cmd.add_parser(
        "checkpoint",
        help="print the signed statement of the chain's head today, to keep outside the "
        "database (key: $MENDARI_REGISTRY_SIGNING_KEY; read-only)",
    )
    checkpoint_registry.add_argument(
        "--db", default="var/registry.db", help="SQLite path or postgresql:// URL"
    )
    checkpoint_registry.add_argument(
        "--previous",
        metavar="FILE",
        help="the last statement kept: refuse to sign when the chain no longer holds its head",
    )
    checkpoint_registry.set_defaults(handler=cmd_registry_checkpoint)
    registry_db = "SQLite path or postgresql:// URL"
    mint_token = registry_cmd.add_parser(
        "token", help="mint a publisher token as the operator (shown once; chained with why)"
    )
    mint_token.add_argument("--db", default="var/registry.db", help=registry_db)
    mint_token.add_argument("--namespace", required=True, help="the namespace it publishes to")
    mint_token.add_argument("--org", required=True, help="the organization it belongs to")
    mint_token.add_argument("--name", default="operator", help="what it is for (a label)")
    mint_token.add_argument(
        "--days", type=int, default=90, help="how long it lives (1 to 365; default 90)"
    )
    mint_token.add_argument("--reason", required=True, help="why (chained; at most 200 characters)")
    mint_token.set_defaults(handler=cmd_registry_token)
    list_tokens = registry_cmd.add_parser(
        "tokens", help="list the owned publisher tokens, read-only (never a secret)"
    )
    list_tokens.add_argument("--db", default="var/registry.db", help=registry_db)
    list_tokens.add_argument("--org", help="only this organization's")
    list_tokens.add_argument("--namespace", help="only this namespace's")
    list_tokens.set_defaults(handler=cmd_registry_tokens)
    revoke_token = registry_cmd.add_parser(
        "revoke-token", help="revoke an owned publisher token, at once (chained with why)"
    )
    revoke_token.add_argument("id", help="the token's id (mendari registry tokens)")
    revoke_token.add_argument("--db", default="var/registry.db", help=registry_db)
    revoke_token.add_argument(
        "--reason", required=True, help="why (chained; at most 200 characters)"
    )
    revoke_token.set_defaults(handler=cmd_registry_revoke_token)
    claim = registry_cmd.add_parser(
        "claim", help="claim a namespace for an organization as the operator (pending; chained)"
    )
    claim.add_argument("--db", default="var/registry.db", help=registry_db)
    claim.add_argument("--namespace", required=True, help="the name (a reserved one too)")
    claim.add_argument("--org", required=True, help="the organization it is linked to")
    claim.add_argument("--display-name", default="", help="how it is shown (default: its name)")
    claim.add_argument("--reason", required=True, help="why (chained; at most 200 characters)")
    claim.set_defaults(handler=cmd_registry_claim)
    for decision, what in (
        ("approve", "approve a pending claim: the namespace is listed (needs a published version)"),
        ("refuse", "refuse a pending claim: the namespace stays taken and is never listed"),
    ):
        decide = registry_cmd.add_parser(decision, help=what)
        decide.add_argument("--db", default="var/registry.db", help=registry_db)
        decide.add_argument("--namespace", required=True, help="the claimed namespace")
        decide.add_argument(
            "--reason", required=True, help="why (chained and kept; at most 300 characters)"
        )
        decide.set_defaults(handler=cmd_registry_decide)
    list_claims = registry_cmd.add_parser("claims", help="list the namespace claims, read-only")
    list_claims.add_argument("--db", default="var/registry.db", help=registry_db)
    list_claims.add_argument("--org", help="only this organization's")
    list_claims.add_argument(
        "--status", choices=("pending", "approved", "refused"), help="only in this status"
    )
    list_claims.set_defaults(handler=cmd_registry_claims)
    site = registry_cmd.add_parser(
        "site", help="register a site for an organization as the operator (pending; chained)"
    )
    site.add_argument("--db", default="var/registry.db", help=registry_db)
    site.add_argument("--org", required=True, help="the organization it belongs to")
    site.add_argument("--name", required=True, help="the site's name, as it would be shown")
    site.add_argument("--reason", required=True, help="why (chained; at most 200 characters)")
    site.set_defaults(handler=cmd_registry_site)
    site_verify = registry_cmd.add_parser(
        "site-verify", help="verify a pending site after checking who it is (chained, once)"
    )
    site_verify.add_argument("--db", default="var/registry.db", help=registry_db)
    site_verify.add_argument("--site", required=True, help="the site's id")
    site_verify.add_argument("--refuse", action="store_true", help="refuse it instead")
    site_verify.add_argument(
        "--reason", required=True, help="how it was checked (chained and kept; one line)"
    )
    site_verify.set_defaults(handler=cmd_registry_site_verify)
    site_key = registry_cmd.add_parser(
        "site-key", help="add a site's public key (from `mendari site init`; chained)"
    )
    site_key.add_argument("--db", default="var/registry.db", help=registry_db)
    site_key.add_argument("--site", required=True, help="the site's id")
    site_key.add_argument(
        "--key",
        required=True,
        metavar="FILE",
        help="the site's key file (its public key and proof, from `mendari site init`)",
    )
    site_key.add_argument("--reason", required=True, help="why (one line)")
    site_key.set_defaults(handler=cmd_registry_site_key)
    site_revoke = registry_cmd.add_parser(
        "site-revoke-key", help="revoke a site key at once (chained with why)"
    )
    site_revoke.add_argument("key_id", help="the key's id (mendari registry sites)")
    site_revoke.add_argument("--db", default="var/registry.db", help=registry_db)
    site_revoke.add_argument(
        "--compromised",
        action="store_true",
        help="the key is thought compromised: every report it signed leaves the pooled figures",
    )
    site_revoke.add_argument("--reason", required=True, help="why (chained; one line)")
    site_revoke.set_defaults(handler=cmd_registry_site_revoke_key)
    site_close = registry_cmd.add_parser(
        "site-close", help="close a site: no ticket or report again (chained with why)"
    )
    site_close.add_argument("--db", default="var/registry.db", help=registry_db)
    site_close.add_argument("--site", required=True, help="the site's id")
    site_close.add_argument("--reason", required=True, help="why (chained; one line)")
    site_close.set_defaults(handler=cmd_registry_site_close)
    accept_report = registry_cmd.add_parser(
        "accept-report", help="take a signed site submission carried by hand (every check)"
    )
    accept_report.add_argument("submission", help="the signed submission (JSON)")
    accept_report.add_argument("--db", default="var/registry.db", help=registry_db)
    accept_report.set_defaults(handler=cmd_registry_accept_report)
    list_sites = registry_cmd.add_parser("sites", help="list the sites and their keys, read-only")
    list_sites.add_argument("--db", default="var/registry.db", help=registry_db)
    list_sites.add_argument("--org", help="only this organization's")
    list_sites.add_argument(
        "--status", choices=("pending", "verified", "refused", "closed"), help="only in this status"
    )
    list_sites.set_defaults(handler=cmd_registry_sites)
    set_type = registry_cmd.add_parser(
        "publisher-type", help="record who publishes in a namespace, after checking it (chained)"
    )
    set_type.add_argument("--db", default="var/registry.db", help=registry_db)
    set_type.add_argument("--namespace", required=True, help="the namespace")
    set_type.add_argument(
        "--type",
        dest="publisher_type",
        required=True,
        choices=("community", "verified", "institutional"),
    )
    set_type.add_argument("--reason", required=True, help="why (printed; one line)")
    set_type.set_defaults(handler=cmd_registry_publisher_type)
    review_version = registry_cmd.add_parser(
        "review", help="record the risk level a version's review found (chained)"
    )
    review_version.add_argument("version", help="namespace/name@version")
    review_version.add_argument("--db", default="var/registry.db", help=registry_db)
    review_version.add_argument(
        "--risk-level", type=int, required=True, choices=range(5), help="0 to 4"
    )
    review_version.add_argument(
        "--reason", required=True, help="a short note about the agent (chained and shown)"
    )
    review_version.set_defaults(handler=cmd_registry_review)
    recall_version = registry_cmd.add_parser(
        "recall",
        help="recall a version, signed by the registry "
        "(key: $MENDARI_REGISTRY_SIGNING_KEY; chained)",
    )
    recall_version.add_argument("version", help="namespace/name@version")
    recall_version.add_argument("--db", default="var/registry.db", help=registry_db)
    recall_version.add_argument(
        "--severity", required=True, choices=("low", "medium", "high", "critical")
    )
    recall_version.add_argument(
        "--reason", required=True, help="why (signed, chained and shown; one line)"
    )
    recall_version.set_defaults(handler=cmd_registry_recall)
    delete_version = registry_cmd.add_parser(
        "delete", help="delete a version's archive for good, keeping a tombstone (chained)"
    )
    delete_version.add_argument("version", help="namespace/name@version")
    delete_version.add_argument("--db", default="var/registry.db", help=registry_db)
    delete_version.add_argument("--reason", required=True, help="why (chained and kept; one line)")
    delete_version.set_defaults(handler=cmd_registry_delete)
    revoke_key = registry_cmd.add_parser(
        "revoke-key",
        help="revoke a namespace's publisher key: it signs nothing more, and its published "
        "evaluations leave the Hub's comparison (chained)",
    )
    revoke_key.add_argument("namespace")
    revoke_key.add_argument("key_id", help="ed25519:<16 hex>")
    revoke_key.add_argument("--db", default="var/registry.db", help=registry_db)
    revoke_key.add_argument("--reason", required=True, help="why (chained and kept; one line)")
    revoke_key.set_defaults(handler=cmd_registry_revoke_key)
    countersign = registry_cmd.add_parser(
        "countersign",
        help="counter-sign the listed versions that have no record "
        "(key: $MENDARI_REGISTRY_SIGNING_KEY; chained)",
    )
    countersign.add_argument("--db", default="var/registry.db", help=registry_db)
    countersign.add_argument(
        "--checkpoint",
        metavar="FILE",
        help="a statement `mendari registry checkpoint` signed, kept outside the database: "
        "sign nothing unless the chain still holds its head",
    )
    countersign.set_defaults(handler=cmd_registry_countersign)

    monitor_cmd = sub.add_parser("monitor", help="check deployed agents for drift and degradation")
    monitor_cmd.add_argument(
        "--db", default="var/mendari.db", help="SQLite path or postgresql:// URL"
    )
    _agent_option(monitor_cmd, help="one agent (default: every agent)")
    monitor_cmd.add_argument(
        "--days", type=_monitored_days, default=30, help="14 to 180, in whole weeks (default 30)"
    )
    orgs = monitor_cmd.add_mutually_exclusive_group()
    orgs.add_argument("--org", help="one organization (default: every organization together)")
    orgs.add_argument(
        "--each-org",
        action="store_true",
        help="each organization with decisions on its own, one alert each (but MENDARI_STATUS_ORG)",
    )
    monitor_cmd.add_argument("--webhook", help="post alerts here (default: $MENDARI_ALERT_WEBHOOK)")
    monitor_cmd.set_defaults(handler=cmd_monitor)

    packet_cmd = agent_command(
        "packet",
        cmd_packet,
        "the committee packet: what a governance committee reads about one agent, as one page "
        "to print",
    )
    packet_cmd.description = (
        "The committee packet of one agent: what it is and who made it, its risk level and "
        "what the runtime enforces, how a person stays in the loop, the publisher's evidence "
        "and a site's, monitoring, change control, data handling and approvals; then what "
        "nobody has stated, and a crosswalk to four published frameworks. With --db it is the "
        "version that deployment serves, read from the server's database, which is opened "
        "read-only and may be read with the server down. Without --db it is made from the "
        "agent's files alone, as a publisher makes it before any deployment exists: the "
        "sections on a deployment's data then say that none was read, and no database is "
        "opened or made. It holds counts, ids and dates, never a case's text, and it is a "
        "reading aid: not signed, and not a certification."
    )
    packet_cmd.add_argument(
        "--db",
        help="the server's database: a SQLite path or a postgresql:// URL (default: none, and "
        "the packet is made from the agent's files alone)",
    )
    packet_cmd.add_argument(
        "--org", help="with --db: one organization (default: every organization together)"
    )
    packet_cmd.add_argument(
        "--days",
        type=_monitored_days,
        help="with --db: the days its counts cover, 14 to 180 (default 90)",
    )
    packet_cmd.add_argument("--out", metavar="FILE", help="write it here (default: print it)")
    packet_cmd.add_argument("--json", action="store_true", help="the packet as JSON, not as a page")

    alerts_cmd = sub.add_parser(
        "alerts", help="the alert webhook ($MENDARI_ALERT_WEBHOOK)"
    ).add_subparsers(dest="alerts_command", required=True)
    alerts_cmd.add_parser(
        "test",
        help="post one signed test alert to $MENDARI_ALERT_WEBHOOK, to check it (never in CI)",
    ).set_defaults(handler=cmd_alerts_test)

    smart_cmd = sub.add_parser("smart", help="SMART on FHIR EHR launch tools").add_subparsers(
        dest="smart_command", required=True
    )
    smart_check = smart_cmd.add_parser(
        "check",
        help="before go-live: an EHR's registration, allow-list, discovery, scopes, PKCE and "
        "keys, with this deployment's settings (each ok, warn, unknown or fail; exit 1 on fail)",
    )
    smart_check.add_argument("base", help="the EHR's FHIR base, as it sends it in the launch's iss")
    smart_check.set_defaults(handler=cmd_smart_check)

    phi_cmd = sub.add_parser("phi", help="PHI mode tools").add_subparsers(
        dest="phi_command", required=True
    )
    purge = phi_cmd.add_parser(
        "purge", help="retention: delete kept inputs and erase notes older than N days"
    )
    purge.add_argument("--db", default="var/mendari.db", help="SQLite path or postgresql:// URL")
    purge.add_argument(
        "--days", type=int, help="default: $MENDARI_RETENTION_DAYS, else 30; 0: everything"
    )
    purge.add_argument("--yes", action="store_true", help="confirm --days 0")
    purge.set_defaults(handler=cmd_phi_purge)
    rotate = phi_cmd.add_parser(
        "rotate", help="re-seal kept inputs and notes under the current PHI key (key rotation)"
    )
    rotate.add_argument("--db", default="var/mendari.db", help="SQLite path or postgresql:// URL")
    rotate.add_argument("--to", help="the current key's id (phi_keys.current in /api/health)")
    rotate.add_argument("--batch", type=int, default=200, help="values per batch (default 200)")
    rotate.add_argument(
        "--check",
        action="store_true",
        help="check, marking nothing: does everything open with the current key alone?",
    )
    rotate.set_defaults(handler=cmd_phi_rotate)
    phi_erase = phi_cmd.add_parser(
        "erase", help="erase one decision's kept input, evidence and notes, on request"
    )
    phi_erase.add_argument(
        "--db", default="var/mendari.db", help="SQLite path or postgresql:// URL"
    )
    phi_erase.add_argument("--decision", required=True, help="the decision's id")
    phi_erase.add_argument("--reason", required=True, choices=["request", "error"])
    phi_erase.add_argument(
        "--by", required=True, help="who erases: an identifier, such as an email"
    )
    phi_erase.set_defaults(handler=cmd_phi_erase)

    notes_cmd = sub.add_parser("notes", help="review and feedback notes").add_subparsers(
        dest="notes_command", required=True
    )
    erase = notes_cmd.add_parser(
        "erase", help="erase a note (its chained commitment stays, and verifies)"
    )
    erase.add_argument("--db", default="var/mendari.db", help="SQLite path or postgresql:// URL")
    erase.add_argument("--id", required=True, help="the note's id (its event's note_ref.id)")
    erase.add_argument(
        "--reason",
        required=True,
        choices=["request", "error"],
        help="request: it should not have been written; error: it was wrong (chained)",
    )
    erase.add_argument(
        "--by", required=True, help="who erases it, as an identifier such as an email (chained)"
    )
    erase.set_defaults(handler=cmd_notes_erase)

    store = sub.add_parser(
        "store", help="organizations' agent stores: the operator's (LP-20)"
    ).add_subparsers(dest="store_command", required=True)
    store_versions = store.add_parser("versions", help="an organization's versions, newest first")
    store_versions.add_argument("--org", required=True, help="the organization")
    _agent_option(store_versions, help="only this agent (its id)")
    store_versions.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    store_versions.set_defaults(handler=cmd_store_versions)
    store_verify = store.add_parser(
        "verify", help="every kept file and version hashes to what it is kept under"
    )
    store_verify.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    store_verify.set_defaults(handler=cmd_store_verify)
    store_withdraw = store.add_parser("withdraw", help="withdraw a draft")
    store_withdraw.add_argument("digest", help="the version's digest (sha256:...)")
    store_withdraw.add_argument("--org", required=True, help="the organization")
    store_withdraw.add_argument("--reason", required=True, help="why, kept on the chain")
    store_withdraw.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    store_withdraw.set_defaults(handler=cmd_store_withdraw)
    store_purge = store.add_parser(
        "purge", help="free the files of versions withdrawn more than 30 days ago"
    )
    store_purge.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    store_purge.set_defaults(handler=cmd_store_purge)

    written = sub.add_parser(
        "written",
        help="what an agent wrote: clinician rubrics and a judge whose agreement is measured",
    ).add_subparsers(dest="written_command", required=True)
    written_eval = written.add_parser(
        "eval", help="grade an agent's written output with the judge, beside clinicians' grades"
    )
    written_eval.description = (
        "Runs every case of a dataset: the agent writes its output (or the input is read), and "
        "the rubric's judge grades each text. With clinicians' grades, reports per criterion how "
        "far the judge agrees with them (weighted kappa, with its interval) beside how far "
        "clinicians agree with each other, and gives the judge's grades for the outputs no "
        "clinician graded only where that agreement clears the floors; otherwise they are "
        "withheld. With no judge model set, grades are canned: a demonstration only. The report "
        "holds counts and hashes, never a text."
    )
    _agent_argument(written_eval)
    written_eval.add_argument("--rubric", required=True, help="one of the agent's rubrics, by name")
    written_eval.add_argument(
        "--dataset", required=True, help="one of the agent's datasets, by name, or a JSONL path"
    )
    written_eval.add_argument(
        "--provider",
        action="append",
        metavar="SLOT=VARIANT",
        help="use a non-default provider variant (repeatable)",
    )
    written_eval.add_argument("--limit", type=int, help="only run the first N cases")
    written_eval.add_argument(
        "--workers", type=int, help="parallel cases (default: 8 with a remote call, else 1)"
    )
    written_eval.add_argument(
        "--judge",
        choices=("auto", "canned", "live"),
        default="auto",
        help="auto (default): a model when one is set, else the rubric's canned grades; or force "
        "either",
    )
    written_eval.add_argument(
        "--grades",
        metavar="FILE",
        help="clinicians' grades (mendari.written-grades/1); default: the agent's own for this "
        "rubric and dataset, if it declares any",
    )
    written_eval.add_argument("--out", help="write the full JSON report here, its ledger beside it")
    written_eval.add_argument("--json", action="store_true", help="print the JSON report")
    written_eval.add_argument(
        "--resamples",
        type=int,
        default=2000,
        help="bootstrap resamples: 2000 (the default) or more, never fewer",
    )
    written_eval.set_defaults(handler=cmd_written_eval)
    written_round = written.add_parser(
        "round", help="grading rounds: clinicians grade a run's outputs in the console"
    ).add_subparsers(dest="written_round_command", required=True)
    round_create = written_round.add_parser(
        "create", help="a new round of an organization, from one run of an agent on a dataset"
    )
    round_create.description = (
        "Runs every case of a dataset as `mendari written eval` does, then keeps a seeded "
        "sample of the texts in the server's database for the organization's graders. The "
        "texts are sealed under the deployment's PHI key (PHI mode encrypt) and erased when "
        "the round closes. Prints ids and counts, never a text."
    )
    _agent_argument(round_create)
    round_create.add_argument("--rubric", required=True, help="one of the agent's rubrics, by name")
    round_create.add_argument(
        "--dataset", required=True, help="one of the agent's datasets, by name, or a JSONL path"
    )
    round_create.add_argument("--org", required=True, help="the organization whose graders grade")
    round_create.add_argument(
        "--sample", type=int, required=True, help="how many of the run's outputs to keep to grade"
    )
    round_create.add_argument(
        "--seed",
        type=int,
        default=0,
        help="the round's seed, fixed here and recorded: its sample, its assignments and its "
        "report's bootstrap all use it (default 0)",
    )
    round_create.add_argument(
        "--provider",
        action="append",
        metavar="SLOT=VARIANT",
        help="use a non-default provider variant (repeatable)",
    )
    round_create.add_argument("--limit", type=int, help="only run the first N cases")
    round_create.add_argument(
        "--workers", type=int, help="parallel cases (default: 8 with a remote call, else 1)"
    )
    round_create.add_argument(
        "--judge",
        choices=("auto", "canned", "live"),
        default="auto",
        help="auto (default): a model when one is set, else the rubric's canned grades",
    )
    round_create.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    round_create.set_defaults(handler=cmd_written_round_create)
    round_assign = written_round.add_parser(
        "assign", help="give a round's items to graders (user ids), a share of them to two"
    )
    round_assign.add_argument("round_id", metavar="ROUND", help="the round's id")
    round_assign.add_argument(
        "--grader",
        action="append",
        required=True,
        metavar="USER",
        help="a grader's user id, as the members list shows it (repeatable)",
    )
    round_assign.add_argument(
        "--double",
        type=float,
        default=0.25,
        help="the share of items a second grader also grades (default 0.25; 0 for none)",
    )
    round_assign.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    round_assign.set_defaults(handler=cmd_written_round_assign)
    round_progress = written_round.add_parser(
        "progress", help="how a round's grading stands (ids and counts)"
    )
    round_progress.add_argument("round_id", metavar="ROUND", help="the round's id")
    round_progress.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    round_progress.set_defaults(handler=cmd_written_round_progress)
    round_close = written_round.add_parser(
        "close", help="close a round: compute its report and erase the texts it kept"
    )
    round_close.add_argument("round_id", metavar="ROUND", help="the round's id")
    round_close.add_argument("--json", action="store_true", help="print the JSON report")
    round_close.add_argument(
        "--resamples",
        type=int,
        default=2000,
        help="bootstrap resamples: 2000 (the default) or more, never fewer",
    )
    round_close.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    round_close.set_defaults(handler=cmd_written_round_close)
    round_list = written_round.add_parser(
        "list", help="every round with its state and counts (ids and counts)"
    )
    round_list.add_argument("--org", help="only this organization's")
    round_list.add_argument(
        "--agent", dest="agent", metavar="AGENT", help="only this agent's (namespace/name)"
    )
    round_list.add_argument("--db", help="the server's database (default: $DATABASE_URL)")
    round_list.set_defaults(handler=cmd_written_round_list)

    new = sub.add_parser(
        "new",
        help="start a new agent from a template (YAML only)",
        description="A new agent in YAML that validates, tests and evaluates as written. It "
        "answers from an offline mock fixture until MENDARI_AGENT_URL names your agent.",
    )
    new.add_argument("dir", help="an empty or new directory")
    new.add_argument("--id", help="namespace/name, e.g. my-clinic/refill-router")
    new.add_argument("--name", help="a display name (default: from the id)")
    new.add_argument("--publisher", default="Your organization", help="who publishes it")
    new.set_defaults(handler=cmd_new)

    sub.add_parser("demo", help="offline end-to-end demo on synthetic data").set_defaults(
        handler=cmd_demo
    )
    mcp = sub.add_parser(
        "mcp",
        help="serve governed decisions to other AI agents as MCP tools (stdio)",
        description="A Model Context Protocol server on stdin/stdout: list_agents, decide, "
        "review_queue and verify_audit. Every decide call runs under the agent's policy and is "
        "written to the audit trail.",
    )
    mcp.add_argument("--db", help="audit database to record decisions in (default: in memory)")
    mcp.set_defaults(handler=cmd_mcp)

    helper = sub.add_parser(
        "help", help="list the commands (`help all`: every one), or show one command's help"
    )
    helper.add_argument(
        "topic",
        nargs="*",
        metavar="command",
        help="a command, with its subcommand if it has any; `all` lists every command",
    )
    helper.set_defaults(handler=cmd_help)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Runs one command. Any error is one line per problem on stderr and exit 1, never a
    traceback (LP-15); `--debug` (or MENDARI_DEBUG=1) lets the error through with its
    traceback. Ctrl-C is `interrupted` and exit 130. A line names what the user gave (a file,
    a flag, a URL's origin, a database without its password) and never an exception's own text
    unless Mendari wrote it: an error from a library can carry case text."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    switch = os.environ.get("MENDARI_DEBUG", "").strip().lower()
    if switch not in _DEBUG_ON + _DEBUG_OFF:
        print("error: MENDARI_DEBUG must be on or off (1 or 0)", file=sys.stderr)
        return 1
    debug = "--debug" in arguments or switch in _DEBUG_ON
    quiet = _quiet_libraries(debug)
    try:
        args = build_parser().parse_args(arguments)
        if tenancy.is_blank(getattr(args, "org", None)):
            raise MendariError("--org names an organization: it is never empty")
        code = int(args.handler(args))
        sys.stdout.flush()  # here, where a closed pipe is caught, not at interpreter exit
        return code
    except KeyboardInterrupt:
        if debug:
            raise
        print("interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # The reader of the output closed it (`mendari audit export | head`): not an error.
        # Later writes go nowhere, so the interpreter's own flush at exit stays silent.
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
        return 141
    except Exception as error:
        if debug:
            raise
        for line in _problem_lines(error):
            print(f"error: {line}", file=sys.stderr)
        return 1
    finally:
        quiet()


_DEBUG_ON = ("1", "on", "true", "yes")
_DEBUG_OFF = ("", "0", "off", "false", "no")
# Libraries that log their own error text to stderr beside the one line (a pool's failed
# connection: host, user, database): kept quiet on the command line unless --debug.
_NOISY_LIBRARIES = ("psycopg", "psycopg.pool")


def _quiet_libraries(debug: bool) -> Callable[[], None]:
    """Raises the noisy libraries' log level for this command; returns what restores it."""
    import logging

    if debug:
        return lambda: None
    loggers = [logging.getLogger(name) for name in _NOISY_LIBRARIES]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.CRITICAL)

    def restore() -> None:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)

    return restore


def _problem_lines(error: Exception) -> list[str]:
    """What an error says, one line per problem, with the fix where one is known."""
    if isinstance(error, AgentValidationError):
        return [_one_line(f"{error.agent}: {problem}") for problem in error.problems]
    if isinstance(error, MendariError):
        return [_one_line(str(error))]
    if isinstance(error, OSError) and error.filename is not None:
        fixes = {
            errno.ENOENT: "check the path",
            errno.EISDIR: "give a file, not a folder",
            errno.ENOTDIR: "give a folder, not a file",
            errno.EACCES: "check its permissions",
            errno.EEXIST: "choose a path that does not exist",
        }
        fix = fixes.get(error.errno or 0, "check the path")
        return [f"{_path_shown(error.filename)}: {error.strerror or type(error).__name__}; {fix}"]
    if isinstance(error, json.JSONDecodeError):
        return [f"not valid JSON (line {error.lineno}, column {error.colno}: {error.msg})"]
    if isinstance(error, UnicodeDecodeError):
        return [f"a file is not UTF-8 text ({error.reason}); save it as UTF-8"]
    kind = type(error).__name__
    module = type(error).__module__
    if module.startswith(("sqlite3", "psycopg")):
        return [f"the database could not be used ({kind}); check --db and that it is reachable"]
    if module.startswith("httpx"):
        try:  # the origin only: a full URL can carry a query or a token
            url = error.request.url  # type: ignore[attr-defined]
            where = f"{url.scheme}://{url.netloc.decode()}"
        except (AttributeError, RuntimeError):  # an error raised without its request
            where = "the server"
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status is not None:  # it answered: no body, which is the server's own text
            return [f"{where} answered {status}; check the URL, the credentials and its logs"]
        return [f"could not reach {where} ({kind}); is it running, and the URL right?"]
    return [f"unexpected {kind}; run again with --debug for the traceback"]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _path_shown(name: object) -> str:
    """A path as a line names it: what was given where a path belongs may be the content itself
    (a key's PEM, a decision's JSON), which is never printed."""
    text = str(name)
    content = "\n" in text or text.lstrip().startswith(("-----BEGIN", "{", "[")) or len(text) > 255
    if content or any(ord(c) < 32 for c in text):
        return "the path given (it looks like a file's content, not its path)"
    return text


if __name__ == "__main__":
    sys.exit(main())
