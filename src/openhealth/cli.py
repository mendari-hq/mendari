"""`openhealth` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from openhealth.core.audit import AuditStore
from openhealth.core.decision import Decision
from openhealth.core.registry import discover, resolve_pack
from openhealth.errors import OpenHealthError

EXIT_DEGRADED = 2
_PACKAGE_DIR = Path(__file__).resolve().parent


def example_datasets_dir() -> Path | None:
    """Datasets bundled with the distribution, or the repository's `datasets/` in a checkout."""
    for candidate in (_PACKAGE_DIR / "_datasets", _PACKAGE_DIR.parents[1] / "datasets"):
        if candidate.is_dir():
            return candidate
    return None


def parse_providers(values: Sequence[str] | None) -> dict[str, str]:
    """`slot=variant` pairs from flags, falling back to $OPENHEALTH_PROVIDERS (comma-separated)."""
    items = list(values or [])
    if not items and os.environ.get("OPENHEALTH_PROVIDERS"):
        items = [v for v in os.environ["OPENHEALTH_PROVIDERS"].split(",") if v.strip()]
    overrides = {}
    for item in items:
        slot, sep, variant = item.partition("=")
        if not sep or not slot.strip() or not variant.strip():
            raise OpenHealthError(f"provider override {item!r} must look like slot=variant")
        overrides[slot.strip()] = variant.strip()
    return overrides


def _decision(args: argparse.Namespace, **kwargs: Any) -> Decision:
    return Decision(resolve_pack(args.pack), providers=parse_providers(args.provider), **kwargs)


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
    print(f"ok  {pack.ref}  {pack.digest}")
    return 0


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
    report = evaluate(decision, dataset)

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

    pack = resolve_pack(args.pack)
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
        datasets_dir=Path(args.datasets_dir) if args.datasets_dir else example_datasets_dir(),
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    status = AuditStore(args.db).verify()
    _print_json(status)
    return 0 if status["ok"] else 1


def cmd_sandbox_generate(args: argparse.Namespace) -> int:
    from openhealth.sandbox import synthetic_inbox

    # Takes a directory rather than a pack reference: the pack cannot load before its data exists.
    data_dir = Path(args.pack_dir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for split, records in synthetic_inbox.public_splits().items():
        _write_jsonl(data_dir / f"{split}.jsonl", records)
        print(f"{len(records):>5} -> {data_dir / f'{split}.jsonl'}")
    local = synthetic_inbox.local_site()
    Path(args.local_out).parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(Path(args.local_out), local)
    print(f"{len(local):>5} -> {args.local_out}")
    return 0


def cmd_sandbox_seed(args: argparse.Namespace) -> int:
    from openhealth.sandbox import fhir_workflow

    created = fhir_workflow.seed(args.fhir, count=args.count, wait_seconds=args.wait)
    print(f"seeded {created} synthetic messages into {args.fhir}")
    return 0


def cmd_sandbox_worker(args: argparse.Namespace) -> int:
    from openhealth.sandbox import fhir_workflow

    fhir_workflow.run_worker(
        args.fhir, args.engine, pack_id=args.pack, interval=args.interval, once=args.once
    )
    return 0


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

    card = pack_command("card", cmd_card, "render the Decision Card")
    card.add_argument("--write", action="store_true", help="write DECISION_CARD.md into the pack")

    schema = pack_command("schema", cmd_schema, "print the input (or output) JSON Schema")
    schema.add_argument("--output", action="store_true", help="print the output schema")

    serve = sub.add_parser("serve", help="run the API, CDS Hooks services and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", default="var/openhealth.db", help="audit database path")
    serve.add_argument("--datasets-dir", help="extra evaluation datasets for the dashboard")
    serve.add_argument("--provider", action="append", metavar="SLOT=VARIANT")
    serve.set_defaults(handler=cmd_serve)

    audit = sub.add_parser("audit", help="audit trail tools").add_subparsers(
        dest="audit_command", required=True
    )
    verify = audit.add_parser("verify", help="verify the audit hash chain")
    verify.add_argument("--db", default="var/openhealth.db")
    verify.set_defaults(handler=cmd_audit_verify)

    sandbox = sub.add_parser("sandbox", help="synthetic sandbox tools").add_subparsers(
        dest="sandbox_command", required=True
    )
    generate = sandbox.add_parser("generate", help="regenerate the synthetic inbox datasets")
    generate.add_argument("--pack-dir", default="packs/patient-inbox-router")
    generate.add_argument("--local-out", default="datasets/local-site-example.jsonl")
    generate.set_defaults(handler=cmd_sandbox_generate)
    seed = sandbox.add_parser("seed", help="load synthetic Patients + Communications into FHIR")
    seed.add_argument("--fhir", required=True, help="FHIR R4 base URL")
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
