"""`mendari` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from mendari.core.audit import AuditStore
from mendari.core.decision import Decision
from mendari.core.pack import Pack
from mendari.core.registry import discover, resolve_pack
from mendari.errors import MendariError, PackValidationError
from mendari.sandbox import example_datasets_dir
from mendari.settings import (
    daily_limits,
    fhir_validation,
    parse_providers,
    providers_for,
    server_providers,
)

EXIT_DEGRADED = 2


def _decision(args: argparse.Namespace, **kwargs: Any) -> Decision:
    pack = resolve_pack(args.pack)
    providers = _providers(pack, parse_providers(args.provider))
    return Decision(pack, providers=providers, fhir_validation=fhir_validation(), **kwargs)


def _providers(pack: Pack, settings: dict[str, str]) -> dict[str, str]:
    """The settings that apply to this pack; a setting that names it but cannot apply is an
    error here (it was typed on purpose), where the server only reports it."""
    slots = {slot: set(spec.variants) for slot, spec in pack.manifest.providers.items()}
    chosen, skipped = providers_for(pack.id, slots, settings)
    if skipped:
        raise MendariError(f"provider settings that do not apply to {pack.id}: {skipped}")
    return chosen


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


# -- commands --------------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    found = discover()
    for pack in sorted(found.packs, key=lambda p: p.id):
        m = pack.manifest
        print(f"{m.id:<40} {m.version:<8} risk {int(m.risk.level)}  {m.name}")
    for root, problem in found.problems.items():
        print(f"invalid pack at {root}:\n{problem}", file=sys.stderr)
    if not found.packs:
        print("No packs found. Set MENDARI_PACKS_PATH or run from a checkout.", file=sys.stderr)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    pack = resolve_pack(args.pack)
    from mendari.providers import build_provider

    for slot, spec in pack.manifest.providers.items():
        for variant in spec.variants:
            build_provider(pack, slot, variant)
    if pack.manifest.inputs.fhir:
        from mendari.standards.fhir import FhirAdapter

        FhirAdapter.for_pack(pack)  # parse and check the mapping, not only that it exists
    problems = _test_input_problems(pack)
    if problems:
        raise PackValidationError(pack.id, problems)
    print(f"ok  {pack.ref}  {pack.digest}")
    return 0


def _test_input_problems(pack: Pack) -> list[str]:
    """Behaviour-test inputs must match the input schema, unless the test expects a BLOCK;
    otherwise the test decides a BLOCK and fails confusingly (e.g. unquoted YAML flow-mapping
    text split into extra keys)."""
    from mendari.core import input_schema
    from mendari.evals.pack_tests import load_cases

    validator = input_schema.validator(pack.input_schema, formats=False)
    problems = []
    for number, case in enumerate(load_cases(pack), start=1):
        if (case.get("expect") or {}).get("outcome") == "BLOCK":
            continue  # a test of the input contract itself
        for error in validator.iter_errors(case.get("input")):
            problems.append(
                f"{pack.manifest.tests} case {number} ({case.get('name', 'unnamed')}): "
                f"input {error.message}"
            )
    return problems


def cmd_test(args: argparse.Namespace) -> int:
    from mendari.evals import run_pack_tests

    decision = _decision(args)
    outcomes = run_pack_tests(decision)
    for outcome in outcomes:
        print(f"{'PASS' if outcome.passed else 'FAIL'}  {outcome.name}")
        for failure in outcome.failures:
            print(f"        {failure}")
    failed = sum(not o.passed for o in outcomes)
    print(f"\n{len(outcomes) - failed} passed, {failed} failed")
    return 1 if failed else 0


def cmd_decide(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy

    # Recorded under the deployment's PHI mode, as the server records it (checked first).
    store = AuditStore(args.db, phi=phi_policy()) if args.db else None
    decision = _decision(args, store=store)
    if args.text is not None:
        payload: Any = {"message": {"body": args.text}}
    else:
        source = sys.stdin if args.input in (None, "-") else open(args.input, encoding="utf-8")  # noqa: SIM115
        with source:
            payload = json.load(source)
    if args.fhir:
        result, task = decision.decide_fhir(payload)
        _print_json({"decision": result.to_dict(), "task": task})
    else:
        _print_json(decision.decide(payload).to_dict())
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from mendari.core.card import benchmark
    from mendari.evals import compare, evaluate, resolve_dataset
    from mendari.evals.render import comparison_text, report_text

    decision = _decision(args)
    pack = decision.pack
    dataset = resolve_dataset(pack, args.dataset)
    if args.limit:
        dataset = dataset.head(args.limit)
    workers = args.workers or (8 if decision.is_remote else 1)
    report = evaluate(decision, dataset, workers=workers)

    comparison = None
    if args.baseline != "none":
        if args.baseline == "auto":
            is_benchmark_run = dataset.name == pack.manifest.evaluation.benchmark_dataset
            baseline = None if is_benchmark_run else benchmark(pack)
        else:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        if baseline is not None:
            tolerance = pack.manifest.evaluation.degradation_tolerance
            comparison = compare(report, baseline, tolerance)
            report["comparison"] = comparison

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.db:
        AuditStore(args.db).save_report(report)
    if args.share:
        with _registry_client(args) as registry:
            shared = registry.share_report(report, args.share)
        print(f"shared with the registry as {shared['site']} (metrics only)", file=sys.stderr)
    if args.json:
        _print_json(report)
    else:
        print(report_text(report))
        if comparison:
            print()
            print(comparison_text(comparison))
        if args.out:
            print(f"\nReport written to {args.out}")
    degraded = comparison is not None and comparison["status"] == "degraded"
    return EXIT_DEGRADED if degraded and args.fail_on_degradation else 0


def cmd_train(args: argparse.Namespace) -> int:
    from mendari.evals import resolve_dataset
    from mendari.providers import naive_bayes

    pack = resolve_pack(args.pack, missing_models_ok=True)
    trained = 0
    for slot, spec in pack.manifest.providers.items():
        if args.slot and slot != args.slot:
            continue
        for name, variant in spec.variants.items():
            config = variant.model_extra or {}
            if variant.type != "naive_bayes":
                continue
            (question,) = spec.answers
            model = naive_bayes.train(
                resolve_dataset(pack, args.train).records,
                calibration=resolve_dataset(pack, args.calibration).records,
                question=question,
                labels=pack.manifest.labels(question),
                text_fields=config.get("text_fields", ["message.subject", "message.body"]),
            )
            target = pack.path(config["model_file"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(model, separators=(",", ":")) + "\n", encoding="utf-8")
            calibration = model["training"]["calibration"]
            print(
                f"{slot}/{name}: {model['training']['examples']} examples, "
                f"vocabulary {model['training']['vocabulary']}, temperature {model['temperature']} "
                f"(calibration NLL {calibration['nll_before']} -> {calibration['nll_after']}) "
                f"-> {target.relative_to(pack.root)}"
            )
            trained += 1
    if not trained:
        raise MendariError("no naive_bayes variants to train")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from mendari.evals import resolve_dataset
    from mendari.providers import build_provider
    from mendari.providers.llm import LlmProvider, fit_calibration

    pack = resolve_pack(args.pack)
    provider = build_provider(pack, args.slot, args.variant)
    if not isinstance(provider, LlmProvider):
        raise MendariError(f"{args.slot}/{args.variant} is not an llm variant")
    dataset = resolve_dataset(pack, args.dataset)
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
    target = pack.path(relative)
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


def cmd_agreement(args: argparse.Namespace) -> int:
    from mendari.evals.agreement import agreement, pack_cases

    pack = resolve_pack(args.pack)
    left, right = args.variants
    cases = pack_cases(pack, args.dataset or None)
    report = agreement(pack, args.slot, left, right, cases)
    if args.json:
        _print_json(report)
    else:
        verdict = "agree" if report["agree"] else f"differ {len(report['differences'])} time(s)"
        print(f"{pack.id} {args.slot}: {left} and {right} {verdict} on {report['cases']} cases")
        for item in report["differences"][:20]:
            print(f"  {item['case']}  {item['question']}: {item[left]} vs {item[right]}")
    return 0 if report["agree"] else 1


def cmd_leaderboard(args: argparse.Namespace) -> int:
    from mendari.evals import evaluate, leaderboard, resolve_dataset

    pack = resolve_pack(args.pack)
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.report]
    datasets = []
    for name in args.dataset:
        dataset = resolve_dataset(pack, name)
        datasets.append(dataset.head(args.limit) if args.limit else dataset)
    for run in args.run:
        decision = Decision(pack, providers=_providers(pack, parse_providers(run.split(","))))
        workers = args.workers or (8 if decision.is_remote else 1)
        for dataset in datasets:
            report = evaluate(decision, dataset, workers=workers)
            reports.append(report)
            errors = report.get("errors") or {}
            status = f"{sum(errors.values())} errors {errors}" if errors else "done"
            print(f"{leaderboard.config_label(report['providers'])} on {dataset.name}: {status}")
    if not reports:
        raise MendariError("nothing to add: pass --run and --dataset, or --report")
    target = Path(args.out) if args.out else pack.attestation(leaderboard.PATH)
    existing = None
    if target.is_file() and not args.fresh:
        existing = json.loads(target.read_text(encoding="utf-8"))
    board = leaderboard.build(pack, reports, existing)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
    print("\n".join(leaderboard.markdown(board)))
    print(f"-> {target}")
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    from mendari.core.card import render_card

    pack = resolve_pack(args.pack)
    card = render_card(pack)
    if args.write:
        target = pack.root / "DECISION_CARD.md"
        if target.is_symlink():  # the write would land wherever the link points
            raise MendariError(f"{target} is a symbolic link; not writing through it")
        target.write_text(card, encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(card)
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    pack = resolve_pack(args.pack)
    _print_json(pack.output_schema if args.output else pack.input_schema)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        raise MendariError("the server needs extras: pip install 'mendari[server]'") from None
    from mendari.server.app import create_app

    # Behind a proxy, the client's address (rate limits per address, the log) comes from
    # X-Forwarded-For, but only from the proxies trusted here. The environment carries the
    # choice to /api/health (`client_ip_source`), as it would for uvicorn run directly.
    trusted = args.forwarded_allow_ips or os.environ.get("FORWARDED_ALLOW_IPS") or "127.0.0.1"
    os.environ["FORWARDED_ALLOW_IPS"] = trusted if args.proxy_headers else ""
    app = create_app(
        db_path=args.db,
        providers=server_providers(args.provider),
        budget_limits=daily_limits(),
        datasets_dir=Path(args.datasets_dir) if args.datasets_dir else example_datasets_dir(),
    )
    # No uvicorn access log: its line carries the query string (a sign-in callback's code and
    # state, a search's values). Mendari's own access line (server/security_log.py) replaces it.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=trusted,
    )
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy

    # Notes sealed under encrypt are checked with this deployment's key ($MENDARI_PHI_KEY).
    store = AuditStore(args.db, phi=phi_policy()) if args.notes else AuditStore(args.db)
    status = store.verify(notes=args.notes)
    _print_json(status)
    return 0 if status["ok"] else 1


def cmd_audit_export(args: argparse.Namespace) -> int:
    from mendari.standards import audit_fhir

    events = AuditStore(args.db).iter_events(pack_id=args.pack, since=args.since, until=args.until)
    lines = (json.dumps(r, separators=(",", ":")) + "\n" for r in audit_fhir.resources(events))
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


def cmd_feedback_export(args: argparse.Namespace) -> int:
    from mendari.evals.feedback import feedback_dataset

    pack = resolve_pack(args.pack)
    with AuditStore(args.db) as store:
        records, skipped = feedback_dataset(store, pack_id=pack.id, rating=args.rating)
    _write_jsonl(Path(args.out), records)
    print(
        f"{len(records)} rated decisions -> {args.out}"
        + (f" ({skipped} skipped: input stored as hash only)" if skipped else "")
    )
    labelled = sum(bool(record["labels"]) for record in records)
    if labelled:
        print(f"{labelled} already carry the right answer from feedback.")
    if labelled < len(records):
        print(f"Fill in `labels` for the {'other ' if labelled else ''}{len(records) - labelled}.")
    if records:
        print("Then use the file with `mendari eval --dataset`.")
    return 0


SANDBOX_KINDS = {
    # kind: (default pack directory, default local-site output)
    "inbox": ("packs/patient-inbox-router", "datasets/local-site-example.jsonl"),
    "trial": ("packs/clinical-trial-prescreener", "datasets/trial-local-site-example.jsonl"),
    "referral": ("packs/referral-router", "datasets/referral-local-site-example.jsonl"),
    "prior-auth": ("packs/prior-auth-preflight", "datasets/prior-auth-local-site-example.jsonl"),
    "guard": ("packs/ai-guard", "datasets/guard-local-site-example.jsonl"),
}


def cmd_sandbox_generate(args: argparse.Namespace) -> int:
    from mendari.sandbox import (
        synthetic_guard,
        synthetic_inbox,
        synthetic_prior_auth,
        synthetic_referral,
        synthetic_trial,
    )

    generators = {
        "inbox": synthetic_inbox,
        "trial": synthetic_trial,
        "referral": synthetic_referral,
        "prior-auth": synthetic_prior_auth,
        "guard": synthetic_guard,
    }
    generator = generators[args.kind]
    pack_dir, local_out = SANDBOX_KINDS[args.kind]
    # Takes a directory rather than a pack reference: the pack cannot load before its data exists.
    data_dir = Path(args.pack_dir or pack_dir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for split, records in generator.public_splits().items():
        _write_jsonl(data_dir / f"{split}.jsonl", records)
        print(f"{len(records):>5} -> {data_dir / f'{split}.jsonl'}")
    local = generator.local_site()
    local_path = Path(args.local_out or local_out)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(local_path, local)
    print(f"{len(local):>5} -> {local_path}")
    return 0


def cmd_sandbox_seed(args: argparse.Namespace) -> int:
    from mendari.sandbox import fhir_workflow

    created = fhir_workflow.seed(
        args.fhir, count=args.count, wait_seconds=args.wait, pack_id=args.pack
    )
    print(f"seeded {created} synthetic {args.pack} cases into {args.fhir}")
    return 0


def cmd_sandbox_worker(args: argparse.Namespace) -> int:
    from mendari.sandbox import fhir_workflow

    fhir_workflow.run_worker(
        args.fhir, args.engine, pack_id=args.pack, interval=args.interval, once=args.once
    )
    return 0


def cmd_sandbox_smart_launch(args: argparse.Namespace) -> int:
    from mendari.sandbox import smart_launch

    result = smart_launch.run(
        engine=args.engine,
        launcher=args.launcher,
        fhir=args.fhir,
        pack_id=args.pack,
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
        print(f"{finding.status:<4}  {finding.check:<16}  {finding.detail}")
    return 1 if any(finding.status == "fail" for finding in findings) else 0


def cmd_sandbox_pilot_check(args: argparse.Namespace) -> int:
    from mendari.sandbox import pilot_check

    try:
        result = pilot_check.run(engine=args.engine, messages=args.messages)
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
    return 0


def cmd_bulk_dataset(args: argparse.Namespace) -> int:
    from mendari.standards import bulk
    from mendari.standards.fhir import FhirAdapter

    adapter = FhirAdapter.for_pack(resolve_pack(args.pack))
    resources = list(bulk.read_ndjson(Path(args.ndjson)))
    labels = bulk.load_labels(Path(args.labels))
    records, skipped = bulk.dataset(adapter, resources, labels, key_system=args.key_system)
    if not records:
        raise MendariError(f"no labelled cases for {args.pack} in {args.ndjson} ({skipped})")
    _write_jsonl(Path(args.out), records)
    left = ", ".join(f"{n} {why.replace('_', ' ')}" for why, n in skipped.items() if n)
    print(f"{len(records)} cases written to {args.out}" + (f" (left out: {left})" if left else ""))
    return 0


def _omop_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    from mendari.standards import omop
    from mendari.standards.bulk import load_labels

    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    db = omop.open_database(args.db, schema=args.schema)
    try:
        people = list(omop.read(db, schema=args.schema, as_of=as_of, limit=args.limit))
    finally:
        db.close()
    labels = load_labels(Path(args.labels)) if getattr(args, "labels", None) else {}
    records = []
    for person, record in people:
        item: dict[str, Any] = {"id": person, "input": record}
        if person in labels:
            item["labels"] = labels[person]
        records.append(item)
    if not records:
        raise MendariError(f"no people in {args.db}")
    return records


def cmd_omop_dataset(args: argparse.Namespace) -> int:
    pack = resolve_pack(args.pack)
    records = _omop_records(args)
    decision = Decision(pack)
    invalid = [r["id"] for r in records if decision.validate(r["input"])]
    if invalid:
        raise MendariError(
            f"{len(invalid)} record(s) don't fit {pack.id}'s input, e.g. {invalid[0]}: "
            "OMOP gives record packs (conditions, labs, medications) their input"
        )
    _write_jsonl(Path(args.out), records)
    labelled = sum("labels" in r for r in records)
    print(f"{len(records)} people written to {args.out} ({labelled} labelled)")
    return 0


def cmd_omop_run(args: argparse.Namespace) -> int:
    from mendari.standards import omop

    pack = resolve_pack(args.pack)
    records = _omop_records(args)
    decision = Decision(pack, providers=_providers(pack, parse_providers(args.provider)))
    decided = [decision.decide(r["input"]).to_dict() for r in records]
    report = {
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        **omop.population(decided, list(pack.manifest.questions), min_cell=args.min_cell),
    }
    if args.json:
        _print_json(report)
        return 0
    print(f"{pack.id}: {report['people']} people (cells under {args.min_cell} suppressed)")
    for outcome, count in report["outcomes"].items():
        print(f"  {outcome}: {'<' + str(args.min_cell) if count is None else count}")
    primary = pack.manifest.policy.primary
    for label, count in report["answers"][primary].items():
        print(f"  {primary} = {label}: {'<' + str(args.min_cell) if count is None else count}")
    return 0


def cmd_sandbox_omop(args: argparse.Namespace) -> int:
    from mendari.evals.harness import load_dataset
    from mendari.standards import omop

    source = load_dataset(args.dataset)
    db = omop.open_database(args.db, schema=args.schema, create=True)
    try:
        written = omop.write(
            db, [(r["id"], r["input"]) for r in source.records], schema=args.schema
        )
    finally:
        db.close()
    print(f"{written} synthetic people written to {args.db}")
    return 0


def cmd_sandbox_omop_check(args: argparse.Namespace) -> int:
    from mendari.sandbox import omop_check

    result = omop_check.run(Path(args.dataset), args.pack)
    _print_json(result)
    if not result["identical"]:
        differences = ", ".join(result["differences"])
        print(
            f"omop-check: the OMOP round trip evaluates differently ({differences})",
            file=sys.stderr,
        )
    return 0 if result["identical"] else 1


def cmd_sandbox_payer(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        raise MendariError("the payer needs extras: pip install 'mendari[server]'") from None
    from mendari.sandbox.payer import create_payer_app

    uvicorn.run(create_payer_app(), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_pas_build(args: argparse.Namespace) -> int:
    from mendari.standards import pas

    record = AuditStore(args.db).get(args.decision)
    if record is None:
        raise MendariError(f"no decision {args.decision} in {args.db}")

    def resource(path: str) -> dict[str, Any]:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or "resourceType" not in loaded:
            raise MendariError(f"{path} is not a FHIR resource")
        return loaded

    bundle = pas.request_bundle(
        decision={"id": record["id"], "recommendation": record["recommendation"]},
        order=resource(args.order),
        patient=resource(args.patient),
        coverage=resource(args.coverage),
        insurer=resource(args.insurer),
        requester=resource(args.requester),
        service_type=args.service_type,
        place_of_service=args.place_of_service,
        diagnoses=args.diagnosis,
    )
    Path(args.out).write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(f"PAS request for decision {record['id']} written to {args.out}")
    return 0


def cmd_sandbox_bulk_check(args: argparse.Namespace) -> int:
    from mendari.sandbox import bulk_check

    result = bulk_check.run(args.fhir, args.pack)
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


def cmd_pack_build(args: argparse.Namespace) -> int:
    import hashlib

    from mendari.hub import archive

    pack = resolve_pack(args.pack)
    key = archive.load_private_key(Path(args.key).read_bytes())
    data = archive.build(pack, key)
    out = Path(args.out) / archive.archive_name(pack)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"{out}  sha256:{hashlib.sha256(data).hexdigest()}  ({len(data)} bytes)")
    return 0


def cmd_pack_verify(args: argparse.Namespace) -> int:
    import tempfile

    from mendari.hub import archive

    public = archive.load_public_key(Path(args.public_key).read_bytes())
    verified = archive.verify(Path(args.archive).read_bytes(), {archive.key_id(public): public})
    with tempfile.TemporaryDirectory() as scratch:
        pack = archive.extract(verified, Path(scratch))
        print(f"OK {pack.id}@{pack.version} {pack.digest} signed by {verified.key_id}")
    return 0


def cmd_registry_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        raise MendariError("the registry needs extras: pip install 'mendari[server]'") from None
    from mendari.hub.registry import create_registry_app

    uvicorn.run(create_registry_app(args.db), host=args.host, port=args.port, log_level="info")
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
    print(f"logged in to {args.registry} ({health['packs']} packs{access})")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    from mendari.hub import archive

    pack = resolve_pack(args.pack)
    key = archive.load_private_key(Path(args.key).read_bytes())
    with _registry_client(args) as registry:
        published = registry.push(pack, key)
    print(f"published {published['id']}@{published['version']} {published['digest']}")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    from mendari.hub.client import pull

    with _registry_client(args) as registry:
        pack = pull(args.ref, registry, accept_new_key=args.accept_new_key)
    print(f"{pack.id}@{pack.version} {pack.digest}\n  verified and cached in {pack.root}")
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
        accuracy = (item.get("benchmark") or {}).get("accuracy")
        shown = "" if accuracy is None else f"  benchmark {accuracy:.1%}"
        risk = f"risk {item['risk']['level']}"
        print(f"{item['id']}@{item['version']}  {risk}  {item['publisher_type']}{shown}")
        print(f"    {item['summary'][:110]}")
    if not found:
        print("no packs match")
    return 0


def cmd_keys_register(args: argparse.Namespace) -> int:
    with _registry_client(args) as registry:
        key_id = registry.add_key(args.namespace, Path(args.public_key).read_bytes())
    print(f"{key_id} registered for {args.namespace}")
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """For a scheduler: every pack's alerts (for one organization with --org; otherwise all of
    them together), posted to a webhook when one is set; exit 1 on any."""
    import os

    import httpx

    from mendari.core import tenancy
    from mendari.evals.monitoring import check_webhook, monitor

    webhook = args.webhook or os.environ.get("MENDARI_ALERT_WEBHOOK", "").strip()
    if webhook:
        check_webhook(webhook)  # before any work, so a typo fails at once
    store = AuditStore(args.db)
    packs = [resolve_pack(args.pack)] if args.pack else discover().packs
    with tenancy.acting_for(args.org):
        reports = [monitor(store, pack, days=args.days) for pack in packs]
    alerting = [r for r in reports if r["alerts"]]
    for report in reports:
        status = f"{len(report['alerts'])} alert(s)" if report["alerts"] else "ok"
        print(f"{report['pack']['id']}: {status}")
        for alert in report["alerts"]:
            print(f"  [{alert['severity']}] {alert['kind']}: {alert['message']}")
    if webhook and alerting:
        body = {
            "source": "mendari",
            "organization": args.org,
            "alerts": [{"pack": r["pack"], "alerts": r["alerts"]} for r in alerting],
        }
        try:
            httpx.post(webhook, json=body, timeout=10, follow_redirects=False).raise_for_status()
        except httpx.HTTPError as error:
            raise MendariError(f"the alert webhook failed ({type(error).__name__})") from None
    return 1 if alerting else 0


def cmd_phi_purge(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy

    days = args.days or int(os.environ.get("MENDARI_RETENTION_DAYS", "0") or 0)
    if days < 1:
        raise MendariError("give --days or set MENDARI_RETENTION_DAYS")
    store = AuditStore(args.db, phi=phi_policy())
    purged, erased = store.purge_inputs(days), store.purge_notes(days)
    print(
        f"{purged} stored input(s) deleted and {erased} note(s) erased, older than {days} days "
        "(decisions and chain kept)"
    )
    return 0


def cmd_notes_erase(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy

    erased = AuditStore(args.db, phi=phi_policy()).erase_note(
        args.id, erased_by=args.by, reason=args.reason
    )
    print(
        f"note {erased['note_id']} of decision {erased['decision_id']} erased; "
        "backups, the database's history and earlier exports still hold it"
    )
    return 0


TEMPLATE = Path(__file__).resolve().parent / "templates" / "new_pack"
# A URL, not a path: `mendari new` runs anywhere, mostly outside a checkout of the repository.
AUTHORING_GUIDE = (
    "https://github.com/Junebugg1214/mendari/blob/main/docs/guides/authoring-a-pack.md"
)


def cmd_new(args: argparse.Namespace) -> int:
    """A new pack from the template: YAML only, and it validates, tests and evaluates as it is."""
    from mendari.core.spec import _PACK_ID

    target = Path(args.dir)
    if target.exists() and any(target.iterdir()):
        raise MendariError(f"{target} exists and is not empty")
    if not _PACK_ID.match(args.id):
        raise MendariError("--id must look like 'namespace/name' (lowercase, digits, dashes)")
    name = args.name or args.id.split("/", 1)[1].replace("-", " ").capitalize()
    values = {"{{id}}": args.id, "{{name}}": name, "{{publisher}}": args.publisher}
    for source in sorted(TEMPLATE.rglob("*")):
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        for placeholder, value in values.items():
            text = text.replace(placeholder, value)
        destination = target / source.relative_to(TEMPLATE)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    print(f"{args.id} created in {target}. Next:")
    for step in (
        f"mendari validate {target}",
        f"mendari test {target}",
        f"mendari eval {target} --dataset examples",
        f"mendari card {target} --write",
    ):
        print(f"  {step}")
    print(f"The guide: {AUTHORING_GUIDE}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from mendari.sandbox.demo import run_demo

    run_demo(datasets_dir=example_datasets_dir())
    return 0


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# -- parser ----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mendari", description="Package, evaluate and deploy healthcare Decision Packs."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def pack_command(name: str, handler: Any, help_text: str, providers: bool = False) -> Any:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("pack", help="pack id (namespace/name[@version]) or directory")
        if providers:
            command.add_argument(
                "--provider",
                action="append",
                metavar="SLOT=VARIANT",
                help="use a non-default provider variant (repeatable)",
            )
        command.set_defaults(handler=handler)
        return command

    sub.add_parser("list", help="list packs on the search path").set_defaults(handler=cmd_list)
    pack_command("validate", cmd_validate, "validate a pack and build every provider variant")
    pack_command("test", cmd_test, "run the pack's behaviour tests", providers=True)

    decide = pack_command("decide", cmd_decide, "run one decision", providers=True)
    decide.add_argument("--input", help="JSON input file ('-' for stdin)")
    decide.add_argument("--text", help="shortcut: message body text")
    decide.add_argument("--fhir", action="store_true", help="input is a FHIR Bundle or resource")
    decide.add_argument("--db", help="record the decision in this audit database")

    evaluate = pack_command("eval", cmd_eval, "evaluate on a labelled dataset", providers=True)
    evaluate.add_argument("--dataset", required=True, help="pack dataset name or JSONL path")
    evaluate.add_argument(
        "--baseline",
        default="auto",
        help="'auto' (pack benchmark), 'none', or a report JSON path",
    )
    evaluate.add_argument("--limit", type=int, help="only evaluate the first N cases")
    evaluate.add_argument(
        "--workers", type=int, help="parallel decisions (default: 8 for remote providers, else 1)"
    )
    evaluate.add_argument("--out", help="write the full JSON report here")
    evaluate.add_argument("--db", help="also store the report in this database (dashboard)")
    evaluate.add_argument("--json", action="store_true", help="print the JSON report")
    evaluate.add_argument(
        "--fail-on-degradation",
        action="store_true",
        help=f"exit {EXIT_DEGRADED} when local validation detects degradation",
    )
    evaluate.add_argument(
        "--share", metavar="SITE", help="share the metrics (never cases) with the registry as SITE"
    )
    evaluate.add_argument("--registry", help="registry URL for --share (default: logged into)")

    train = pack_command("train", cmd_train, "train the pack's naive_bayes variants")
    train.add_argument("--slot", help="only this provider slot")
    train.add_argument("--train", default="train", help="training dataset (default: train)")
    train.add_argument("--calibration", default="calibration", help="calibration dataset")

    calibrate = pack_command(
        "calibrate", cmd_calibrate, "fit per-question temperatures for an llm variant (live calls)"
    )
    calibrate.add_argument("--slot", required=True, help="provider slot")
    calibrate.add_argument("--variant", required=True, help="llm variant of the slot")
    calibrate.add_argument("--dataset", default="calibration", help="labelled dataset")
    calibrate.add_argument("--limit", type=int, default=200, help="records to use (default 200)")
    calibrate.add_argument("--workers", type=int, default=8, help="parallel calls (default 8)")

    agree = pack_command(
        "agreement", cmd_agreement, "check that two variants of a slot give the same answers"
    )
    agree.add_argument("--slot", required=True, help="provider slot")
    agree.add_argument("--variants", nargs=2, required=True, metavar="VARIANT")
    agree.add_argument(
        "--dataset",
        action="append",
        default=[],
        help="dataset name or file (repeat; default: the pack's tests and every dataset)",
    )
    agree.add_argument("--json", action="store_true", help="the full report")

    board = pack_command(
        "leaderboard", cmd_leaderboard, "compare provider configurations on the same datasets"
    )
    board.add_argument(
        "--run",
        action="append",
        default=[],
        help="a configuration to evaluate, e.g. router=llm,safety_screen=rules_and_llm (repeat)",
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
    board.add_argument("--out", help="leaderboard file (default: <pack>/evals/leaderboard.json)")
    board.add_argument("--fresh", action="store_true", help="replace instead of merging")

    card = pack_command("card", cmd_card, "render the Decision Card")
    card.add_argument("--write", action="store_true", help="write DECISION_CARD.md into the pack")

    schema = pack_command("schema", cmd_schema, "print the input (or output) JSON Schema")
    schema.add_argument("--output", action="store_true", help="print the output schema")

    serve = sub.add_parser("serve", help="run the API, CDS Hooks services and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--db", default="var/mendari.db", help="audit database: SQLite path or postgresql:// URL"
    )
    serve.add_argument("--datasets-dir", help="extra evaluation datasets for the dashboard")
    serve.add_argument("--provider", action="append", metavar="SLOT=VARIANT")
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
    verify.add_argument(
        "--notes",
        action="store_true",
        help="also check each kept note against its chained commitment (with $MENDARI_PHI_KEY)",
    )
    verify.set_defaults(handler=cmd_audit_verify)
    audit_export = audit.add_parser(
        "export", help="export the audit trail as FHIR NDJSON (AuditEvent, Provenance)"
    )
    audit_export.add_argument(
        "--db", default="var/mendari.db", help="SQLite path or postgresql:// URL"
    )
    audit_export.add_argument("--pack", help="only this pack's decisions (namespace/name)")
    audit_export.add_argument("--since", help="events at or after this time (ISO 8601, UTC)")
    audit_export.add_argument("--until", help="events before this time (ISO 8601, UTC)")
    audit_export.add_argument("--out", help="write to this file (default: standard output)")
    audit_export.set_defaults(handler=cmd_audit_export)

    feedback = sub.add_parser("feedback", help="thumbs up / down feedback").add_subparsers(
        dest="feedback_command", required=True
    )
    export = feedback.add_parser("export", help="export rated decisions as a dataset to label")
    export.add_argument("pack", help="pack id or directory")
    export.add_argument("--db", default="var/mendari.db", help="SQLite path or postgresql:// URL")
    export.add_argument("--rating", choices=["down", "up"], default="down")
    export.add_argument("--out", required=True, help="JSONL file to write")
    export.set_defaults(handler=cmd_feedback_export)

    sandbox = sub.add_parser("sandbox", help="synthetic sandbox tools").add_subparsers(
        dest="sandbox_command", required=True
    )
    generate = sandbox.add_parser("generate", help="regenerate a reference pack's synthetic data")
    generate.add_argument("--kind", choices=sorted(SANDBOX_KINDS), default="inbox")
    generate.add_argument("--pack-dir", help="default: the kind's reference pack")
    generate.add_argument("--local-out", help="default: datasets/<kind's local-site example>")
    generate.set_defaults(handler=cmd_sandbox_generate)
    seed = sandbox.add_parser("seed", help="load synthetic cases into FHIR as US Core resources")
    seed.add_argument("--fhir", required=True, help="FHIR R4 base URL")
    seed.add_argument(
        "--pack",
        default="mendari/patient-inbox-router",
        choices=["mendari/patient-inbox-router", "mendari/referral-router"],
        help="whose local-site cases (default: the inbox's messages)",
    )
    seed.add_argument("--count", type=int, default=40)
    seed.add_argument("--wait", type=int, default=180, help="seconds to wait for the server")
    seed.set_defaults(handler=cmd_sandbox_seed)
    worker = sandbox.add_parser("worker", help="route new FHIR Communications through the engine")
    worker.add_argument("--fhir", required=True)
    worker.add_argument("--engine", required=True, help="engine base URL")
    worker.add_argument("--pack", default="mendari/patient-inbox-router")
    worker.add_argument("--interval", type=float, default=5.0)
    worker.add_argument("--once", action="store_true")
    worker.set_defaults(handler=cmd_sandbox_worker)
    smart = sandbox.add_parser(
        "smart-launch", help="run a SMART EHR launch against the sandbox and decide the work items"
    )
    smart.add_argument("--engine", required=True, help="engine URL (its MENDARI_PUBLIC_URL)")
    smart.add_argument("--launcher", required=True, help="smart-launcher-v2 base URL")
    smart.add_argument("--fhir", required=True, help="FHIR R4 base URL, to find a patient")
    smart.add_argument(
        "--pack",
        default="mendari/referral-router",
        choices=["mendari/patient-inbox-router", "mendari/referral-router"],
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
    pilot.add_argument("--messages", type=int, default=12, help="inbox messages to decide")
    pilot.set_defaults(handler=cmd_sandbox_pilot_check)
    payer = sandbox.add_parser("payer", help="a mock payer answering Da Vinci PAS requests")
    payer.add_argument("--host", default="127.0.0.1")
    payer.add_argument("--port", type=int, default=8200)
    payer.set_defaults(handler=cmd_sandbox_payer)
    omop_write = sandbox.add_parser(
        "omop", help="write a dataset's records into an OMOP CDM database (synthetic data)"
    )
    omop_write.add_argument("--dataset", required=True, help="a JSONL dataset of record inputs")
    omop_write.add_argument("--db", required=True, help="SQLite path or postgresql:// URL")
    omop_write.add_argument("--schema", default="", help="database schema (e.g. cdm)")
    omop_write.set_defaults(handler=cmd_sandbox_omop)
    omop_check = sandbox.add_parser(
        "omop-check", help="round-trip a dataset through OMOP and compare the evaluations"
    )
    omop_check.add_argument("--dataset", required=True, help="a labelled JSONL dataset")
    omop_check.add_argument("--pack", default="mendari/clinical-trial-prescreener")
    omop_check.set_defaults(handler=cmd_sandbox_omop_check)
    bulk_check = sandbox.add_parser(
        "bulk-check", help="Bulk export the sandbox, evaluate it, and compare with the dataset file"
    )
    bulk_check.add_argument("--fhir", required=True, help="the sandbox's FHIR R4 base URL")
    bulk_check.add_argument("--pack", default="mendari/referral-router")
    bulk_check.set_defaults(handler=cmd_sandbox_bulk_check)

    pas_cmd = sub.add_parser(
        "pas", help="Da Vinci PAS prior-authorization requests"
    ).add_subparsers(dest="pas_command", required=True)
    pas_build = pas_cmd.add_parser("build", help="the PAS request Bundle for a complete request")
    pas_build.add_argument("--db", default="var/mendari.db", help="the audit database")
    pas_build.add_argument("--decision", required=True, help="a submit_ready decision id")
    for name in ("patient", "coverage", "insurer", "requester", "order"):
        pas_build.add_argument(f"--{name}", required=True, help=f"the {name} (FHIR JSON file)")
    pas_build.add_argument("--service-type", help="X12 service type (1365), e.g. 62")
    pas_build.add_argument("--place-of-service", help="CMS place of service, e.g. 22")
    pas_build.add_argument("--diagnosis", action="append", default=[], help="ICD-10-CM (repeat)")
    pas_build.add_argument("--out", required=True, help="where to write the Bundle")
    pas_build.set_defaults(handler=cmd_pas_build)

    omop_cmd = sub.add_parser(
        "omop", help="OMOP CDM databases as pack inputs (population-level evaluation)"
    ).add_subparsers(dest="omop_command", required=True)
    for name, handler, text in (
        ("dataset", cmd_omop_dataset, "write each person's record as a local dataset"),
        ("run", cmd_omop_run, "decide every person and report the population (metrics only)"),
    ):
        command = omop_cmd.add_parser(name, help=text)
        command.add_argument("--db", required=True, help="SQLite path or postgresql:// URL")
        command.add_argument("--schema", default="", help="the CDM's schema (e.g. cdm)")
        command.add_argument("--pack", required=True, help="a record pack")
        command.add_argument("--as-of", help="screening date (default: each person's last day)")
        command.add_argument("--limit", type=int, help="only the first N people")
        command.set_defaults(handler=handler)
        if name == "dataset":
            command.add_argument("--out", required=True, help="JSONL file to write")
            command.add_argument("--labels", help="JSONL of {id, labels} by person_source_value")
        else:
            command.add_argument("--provider", action="append", metavar="SLOT=VARIANT")
            command.add_argument("--min-cell", type=int, default=10, help="smallest cell shown")
            command.add_argument("--json", action="store_true")

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
    dataset_bulk = bulk.add_parser("dataset", help="turn an export into a pack's local dataset")
    dataset_bulk.add_argument("--pack", required=True, help="namespace/name")
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
    push_cmd = sub.add_parser("push", help="sign a pack and publish it to the registry")
    push_cmd.add_argument("pack", help="namespace/name[@version] or a directory")
    push_cmd.add_argument("--key", required=True, help="publisher private key (PEM)")
    push_cmd.add_argument("--registry", help="registry URL (default: the one logged into)")
    push_cmd.set_defaults(handler=cmd_push)
    pull_cmd = sub.add_parser("pull", help="download, verify and cache a pack from the registry")
    pull_cmd.add_argument("ref", help="namespace/name[@version]")
    pull_cmd.add_argument("--registry", help="registry URL (default: the one logged into)")
    pull_cmd.add_argument(
        "--accept-new-key", action="store_true", help="trust a key the namespace rotated to"
    )
    pull_cmd.set_defaults(handler=cmd_pull)
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
    pack_cmd = sub.add_parser("pack", help="signed pack archives").add_subparsers(
        dest="pack_command", required=True
    )
    build_pack = pack_cmd.add_parser("build", help="build a signed .ohpack archive")
    build_pack.add_argument("pack", help="namespace/name[@version] or a directory")
    build_pack.add_argument("--key", required=True, help="publisher private key (PEM)")
    build_pack.add_argument("--out", default="dist", help="directory for the archive")
    build_pack.set_defaults(handler=cmd_pack_build)
    verify_pack = pack_cmd.add_parser("verify", help="verify an archive's files and signature")
    verify_pack.add_argument("archive")
    verify_pack.add_argument("--public-key", required=True, help="publisher public key (PEM)")
    verify_pack.set_defaults(handler=cmd_pack_verify)

    registry_cmd = sub.add_parser("registry", help="the pack registry service").add_subparsers(
        dest="registry_command", required=True
    )
    serve_registry = registry_cmd.add_parser(
        "serve", help="run the registry (admin token: $MENDARI_REGISTRY_ADMIN_TOKEN)"
    )
    serve_registry.add_argument(
        "--db", default="var/registry.db", help="SQLite path or postgresql:// URL"
    )
    serve_registry.add_argument("--host", default="127.0.0.1")
    serve_registry.add_argument("--port", type=int, default=8100)
    serve_registry.set_defaults(handler=cmd_registry_serve)

    monitor_cmd = sub.add_parser("monitor", help="check deployed packs for drift and degradation")
    monitor_cmd.add_argument(
        "--db", default="var/mendari.db", help="SQLite path or postgresql:// URL"
    )
    monitor_cmd.add_argument("--pack", help="one pack (default: every pack)")
    monitor_cmd.add_argument("--days", type=int, default=30)
    monitor_cmd.add_argument(
        "--org", help="one organization (default: every organization together)"
    )
    monitor_cmd.add_argument("--webhook", help="post alerts here (default: $MENDARI_ALERT_WEBHOOK)")
    monitor_cmd.set_defaults(handler=cmd_monitor)

    smart_cmd = sub.add_parser("smart", help="SMART on FHIR EHR launch tools").add_subparsers(
        dest="smart_command", required=True
    )
    smart_check = smart_cmd.add_parser(
        "check",
        help="before go-live: an EHR's registration, allow-list, discovery, PKCE and keys, "
        "with this deployment's settings",
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
    purge.add_argument("--days", type=int, help="default: $MENDARI_RETENTION_DAYS")
    purge.set_defaults(handler=cmd_phi_purge)

    notes_cmd = sub.add_parser("notes", help="review, feedback and payer notes").add_subparsers(
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

    new = sub.add_parser("new", help="start a new pack from a template (YAML only)")
    new.add_argument("dir", help="an empty or new directory")
    new.add_argument("--id", required=True, help="namespace/name, e.g. my-clinic/refill-router")
    new.add_argument("--name", help="a display name (default: from the id)")
    new.add_argument("--publisher", default="Your organization", help="who publishes it")
    new.set_defaults(handler=cmd_new)

    sub.add_parser("demo", help="offline end-to-end demo on synthetic data").set_defaults(
        handler=cmd_demo
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except MendariError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
