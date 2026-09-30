"""`openhealth` command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from openhealth.core.audit import AuditStore
from openhealth.core.decision import Decision
from openhealth.core.pack import Pack
from openhealth.core.registry import discover, resolve_pack
from openhealth.errors import OpenHealthError, PackValidationError
from openhealth.sandbox import example_datasets_dir
from openhealth.settings import daily_limits, fhir_validation, parse_providers, providers_for

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
        raise OpenHealthError(f"provider settings that do not apply to {pack.id}: {skipped}")
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
        print("No packs found. Set OPENHEALTH_PACKS_PATH or run from a checkout.", file=sys.stderr)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    pack = resolve_pack(args.pack)
    from openhealth.providers import build_provider

    for slot, spec in pack.manifest.providers.items():
        for variant in spec.variants:
            build_provider(pack, slot, variant)
    if pack.manifest.inputs.fhir:
        from openhealth.standards.fhir import FhirAdapter

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
    from jsonschema import Draft202012Validator

    from openhealth.evals.pack_tests import load_cases

    validator = Draft202012Validator(pack.input_schema)
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
    from openhealth.evals import run_pack_tests

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
    store = AuditStore(args.db) if args.db else None
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
    from openhealth.core.card import benchmark
    from openhealth.evals import compare, evaluate, resolve_dataset
    from openhealth.evals.render import comparison_text, report_text

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
    from openhealth.evals import resolve_dataset
    from openhealth.providers import naive_bayes

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
        raise OpenHealthError("no naive_bayes variants to train")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from openhealth.evals import resolve_dataset
    from openhealth.providers import build_provider
    from openhealth.providers.llm import LlmProvider, fit_calibration

    pack = resolve_pack(args.pack)
    provider = build_provider(pack, args.slot, args.variant)
    if not isinstance(provider, LlmProvider):
        raise OpenHealthError(f"{args.slot}/{args.variant} is not an llm variant")
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


def cmd_leaderboard(args: argparse.Namespace) -> int:
    from openhealth.evals import evaluate, leaderboard, resolve_dataset

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
        raise OpenHealthError("nothing to add: pass --run and --dataset, or --report")
    target = Path(args.out) if args.out else pack.path(leaderboard.PATH)
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
    from openhealth.core.card import render_card

    pack = resolve_pack(args.pack)
    card = render_card(pack)
    if args.write:
        target = pack.root / "DECISION_CARD.md"
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
        raise OpenHealthError("the server needs extras: pip install 'openhealth[server]'") from None
    from openhealth.server.app import create_app

    app = create_app(
        db_path=args.db,
        providers=parse_providers(args.provider),
        budget_limits=daily_limits(),
        datasets_dir=Path(args.datasets_dir) if args.datasets_dir else example_datasets_dir(),
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    status = AuditStore(args.db).verify()
    _print_json(status)
    return 0 if status["ok"] else 1


def cmd_audit_export(args: argparse.Namespace) -> int:
    from openhealth.standards import audit_fhir

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
    from openhealth.evals.feedback import feedback_dataset

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
        print("Then use the file with `openhealth eval --dataset`.")
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
    from openhealth.sandbox import (
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
    from openhealth.sandbox import fhir_workflow

    created = fhir_workflow.seed(
        args.fhir, count=args.count, wait_seconds=args.wait, pack_id=args.pack
    )
    print(f"seeded {created} synthetic {args.pack} cases into {args.fhir}")
    return 0


def cmd_sandbox_worker(args: argparse.Namespace) -> int:
    from openhealth.sandbox import fhir_workflow

    fhir_workflow.run_worker(
        args.fhir, args.engine, pack_id=args.pack, interval=args.interval, once=args.once
    )
    return 0


def cmd_sandbox_smart_launch(args: argparse.Namespace) -> int:
    from openhealth.sandbox import smart_launch

    result = smart_launch.run(
        engine=args.engine,
        launcher=args.launcher,
        fhir=args.fhir,
        pack_id=args.pack,
        patient=args.patient,
        limit=args.limit,
    )
    _print_json(result)
    problems = []
    if not result["decided"]:
        problems.append("no work item was decided")
    if not result["session_ended"]:
        problems.append("the session did not end at logout")
    if args.strict and any(item["input_issues"] for item in result["decided"]):
        problems.append("a decided input did not conform to US Core")
    for problem in problems:
        print(f"smart-launch: {problem}", file=sys.stderr)
    return 1 if problems else 0


def cmd_bulk_export(args: argparse.Namespace) -> int:
    import os

    from openhealth.standards import bulk
    from openhealth.standards.fhir_client import GuardedHttp

    with GuardedHttp() as http:
        token = None
        if args.client_id:
            key = Path(args.private_key).read_text() if args.private_key else None
            key = key or os.environ.get("OPENHEALTH_BULK_PRIVATE_KEY", "")
            if not key:
                raise OpenHealthError("backend services need --private-key (or its env var)")
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
    from openhealth.standards import bulk
    from openhealth.standards.fhir import FhirAdapter

    adapter = FhirAdapter.for_pack(resolve_pack(args.pack))
    resources = list(bulk.read_ndjson(Path(args.ndjson)))
    labels = bulk.load_labels(Path(args.labels))
    records, skipped = bulk.dataset(adapter, resources, labels, key_system=args.key_system)
    if not records:
        raise OpenHealthError(f"no labelled cases for {args.pack} in {args.ndjson} ({skipped})")
    _write_jsonl(Path(args.out), records)
    left = ", ".join(f"{n} {why.replace('_', ' ')}" for why, n in skipped.items() if n)
    print(f"{len(records)} cases written to {args.out}" + (f" (left out: {left})" if left else ""))
    return 0


def cmd_sandbox_bulk_check(args: argparse.Namespace) -> int:
    from openhealth.sandbox import bulk_check

    result = bulk_check.run(args.fhir, args.pack)
    _print_json(result)
    if not result["identical"]:
        differences = ", ".join(result["differences"])
        print(f"bulk-check: the export evaluates differently ({differences})", file=sys.stderr)
    return 0 if result["identical"] else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from openhealth.sandbox.demo import run_demo

    run_demo(datasets_dir=example_datasets_dir())
    return 0


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# -- parser ----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openhealth", description="Package, evaluate and deploy healthcare Decision Packs."
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
        "--db", default="var/openhealth.db", help="audit database: SQLite path or postgresql:// URL"
    )
    serve.add_argument("--datasets-dir", help="extra evaluation datasets for the dashboard")
    serve.add_argument("--provider", action="append", metavar="SLOT=VARIANT")
    serve.set_defaults(handler=cmd_serve)

    audit = sub.add_parser("audit", help="audit trail tools").add_subparsers(
        dest="audit_command", required=True
    )
    verify = audit.add_parser("verify", help="verify the audit hash chain")
    verify.add_argument(
        "--db", default="var/openhealth.db", help="SQLite path or postgresql:// URL"
    )
    verify.set_defaults(handler=cmd_audit_verify)
    audit_export = audit.add_parser(
        "export", help="export the audit trail as FHIR NDJSON (AuditEvent, Provenance)"
    )
    audit_export.add_argument(
        "--db", default="var/openhealth.db", help="SQLite path or postgresql:// URL"
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
    export.add_argument(
        "--db", default="var/openhealth.db", help="SQLite path or postgresql:// URL"
    )
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
        default="openhealth/patient-inbox-router",
        choices=["openhealth/patient-inbox-router", "openhealth/referral-router"],
        help="whose local-site cases (default: the inbox's messages)",
    )
    seed.add_argument("--count", type=int, default=40)
    seed.add_argument("--wait", type=int, default=180, help="seconds to wait for the server")
    seed.set_defaults(handler=cmd_sandbox_seed)
    worker = sandbox.add_parser("worker", help="route new FHIR Communications through the engine")
    worker.add_argument("--fhir", required=True)
    worker.add_argument("--engine", required=True, help="engine base URL")
    worker.add_argument("--pack", default="openhealth/patient-inbox-router")
    worker.add_argument("--interval", type=float, default=5.0)
    worker.add_argument("--once", action="store_true")
    worker.set_defaults(handler=cmd_sandbox_worker)
    smart = sandbox.add_parser(
        "smart-launch", help="run a SMART EHR launch against the sandbox and decide the work items"
    )
    smart.add_argument("--engine", required=True, help="engine URL (its OPENHEALTH_PUBLIC_URL)")
    smart.add_argument("--launcher", required=True, help="smart-launcher-v2 base URL")
    smart.add_argument("--fhir", required=True, help="FHIR R4 base URL, to find a patient")
    smart.add_argument(
        "--pack",
        default="openhealth/referral-router",
        choices=["openhealth/patient-inbox-router", "openhealth/referral-router"],
    )
    smart.add_argument("--patient", help="patient id (default: one with a work item)")
    smart.add_argument("--limit", type=int, default=5, help="decide at most this many items")
    smart.add_argument("--strict", action="store_true", help="fail on any US Core issue")
    smart.set_defaults(handler=cmd_sandbox_smart_launch)
    bulk_check = sandbox.add_parser(
        "bulk-check", help="Bulk export the sandbox, evaluate it, and compare with the dataset file"
    )
    bulk_check.add_argument("--fhir", required=True, help="the sandbox's FHIR R4 base URL")
    bulk_check.add_argument("--pack", default="openhealth/referral-router")
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
    export_bulk.add_argument("--key-id", default="openhealth", help="the key's kid in your JWKS")
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
        default="https://openhealth.dev/fhir/synthetic-case",
        help="identifier system that holds the case id (default: the sandbox's)",
    )
    dataset_bulk.add_argument("--out", required=True, help="dataset file to write (JSONL)")
    dataset_bulk.set_defaults(handler=cmd_bulk_dataset)

    sub.add_parser("demo", help="offline end-to-end demo on synthetic data").set_defaults(
        handler=cmd_demo
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except OpenHealthError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
