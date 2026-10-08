# SPDX-License-Identifier: Apache-2.0
"""`mendari` command-line interface."""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from mendari import __version__
from mendari.core import clock, tenancy
from mendari.core.audit import SYSTEM_ID, AuditStore
from mendari.core.decision import Decision
from mendari.core.pack import Pack
from mendari.core.registry import discover, resolve_pack
from mendari.errors import MendariError, PackValidationError
from mendari.sandbox import example_datasets_dir
from mendari.settings import (
    applies_to,
    daily_limits,
    default_providers,
    fhir_validation,
    parse_providers,
    providers_for,
)

EXIT_DEGRADED = 2


def _decision(args: argparse.Namespace, **kwargs: Any) -> Decision:
    pack = resolve_pack(args.pack)
    providers = _providers(pack, parse_providers(args.provider), typed=bool(args.provider))
    return Decision(pack, providers=providers, fhir_validation=fhir_validation(), **kwargs)


def _providers(pack: Pack, settings: dict[str, str], *, typed: bool = True) -> dict[str, str]:
    """The settings that apply to this pack. A setting that names it but cannot apply (a slot's
    unknown variant) is an error here, where the server only reports it. So is a typed setting
    (`--provider`, `--run`) that applies to nothing in the pack: an unknown slot, another pack's
    setting, or an unscoped one for a community pack. From $MENDARI_PROVIDERS, a deployment-wide
    list naming other packs' slots too, such a setting is only a warning."""
    slots = {slot: set(spec.variants) for slot, spec in pack.manifest.providers.items()}
    chosen, skipped = providers_for(pack.id, slots, settings, community=pack.community)
    if skipped:
        raise MendariError(f"provider settings that do not apply to {pack.id}: {skipped}")
    unused = [
        _unused(pack, key, variant, slots)
        for key, variant in settings.items()
        if not applies_to(key, pack.id, slots, community=pack.community)
    ]
    if unused and typed:
        raise PackValidationError(pack.id, unused)
    for problem in unused:
        print(f"warning: MENDARI_PROVIDERS: {problem}", file=sys.stderr)
    return chosen


def _unused(pack: Pack, key: str, variant: str, slots: dict[str, set[str]]) -> str:
    target, sep, slot = key.rpartition(":")
    if sep and target != pack.id:
        return f"provider setting {key}={variant} names {target}, not this pack"
    if not sep and pack.community and slot in slots:
        return (
            f"provider setting {key}={variant}: a community pack takes only settings that name "
            f"it ({pack.id}:{slot}={variant})"
        )
    return (
        f"provider setting {key}={variant}: the pack has no slot {slot!r} "
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
    for pack in sorted(found.packs, key=lambda p: p.id):
        m = pack.manifest
        print(f"{m.id:<40} {m.version:<8} risk {int(m.risk.level)}  {m.name}")
    for root, problem in found.problems.items():
        print(f"invalid pack at {root}:\n{problem}", file=sys.stderr)
    # An owned pack whose provider configs `mendari validate` refuses still loads: said here.
    for pack in sorted(found.packs, key=lambda p: (p.id, p.root)):
        if pack.warnings:
            listed = "\n".join(f"  - {warning}" for warning in pack.warnings)
            print(f"warnings for {pack.ref} at {pack.root}:\n{listed}", file=sys.stderr)
    if not found.packs:
        print("No packs found. Set MENDARI_PACKS_PATH or run from a checkout.", file=sys.stderr)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from mendari.evals.pack_tests import validate_pack

    pack = resolve_pack(args.pack)
    validate_pack(pack)
    print(f"ok  {pack.ref}  {pack.digest}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    from mendari.evals import run_pack_tests

    decision = _decision(args)
    outcomes = run_pack_tests(decision)
    if not outcomes:  # nothing ran: a pass would say nothing (LP-15)
        where = decision.pack.manifest.tests or "tests/cases.yaml (and `tests:` in pack.yaml)"
        raise MendariError(
            f"{decision.pack.ref}: no behaviour tests to run: add cases to {where} (spec §10)"
        )
    for outcome in outcomes:
        print(f"{'PASS' if outcome.passed else 'FAIL'}  {outcome.name}")
        for failure in outcome.failures:
            print(f"        {failure}")
    failed = sum(not o.passed for o in outcomes)
    print(f"\n{len(outcomes) - failed} passed, {failed} failed")
    return 1 if failed else 0


def cmd_decide(args: argparse.Namespace) -> int:
    from mendari.core.phi import phi_policy

    # Recorded under the PHI mode set here (checked first). A database a protected mode kept
    # refuses an input in plaintext, so a run without the deployment's settings cannot add one.
    store = AuditStore(args.db, phi=phi_policy()) if args.db else None
    held = None
    if store is not None:  # recorded in a deployment's trail: its holds apply here too

        def held(pack: Pack) -> str | None:
            hold = store.hold_for(pack.id, pack.digest, tenancy.DEFAULT_ORG)
            return None if hold is None else f"hold {hold['id']}"

    decision = _decision(args, store=store, hold_source=held)
    if args.text is not None:
        payload: Any = {"message": {"body": args.text}}
    else:
        payload = _read_json(args.input, "--input")
    if args.fhir:
        result, task = decision.decide_fhir(payload)
        _print_json({"decision": result.to_dict(), "task": task})
    else:
        _print_json(decision.decide(payload).to_dict())
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from mendari.core.card import benchmark
    from mendari.evals import compare, resolve_dataset
    from mendari.evals.harness import evaluation
    from mendari.evals.render import comparison_text, report_text

    decision = _decision(args)
    pack = decision.pack
    dataset = resolve_dataset(pack, args.dataset)
    if args.limit:
        dataset = dataset.head(args.limit)
    workers = args.workers or (8 if decision.is_remote else 1)
    run = evaluation(decision, dataset, workers=workers)
    report = run.report

    comparison = None
    if args.baseline != "none":
        if args.baseline == "auto":
            is_benchmark_run = dataset.name == pack.manifest.evaluation.benchmark_dataset
            baseline = None if is_benchmark_run else benchmark(pack)
        else:
            baseline = _read_json(args.baseline, "--baseline", shape=dict)
        if baseline is not None:
            tolerance = pack.manifest.evaluation.degradation_tolerance
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
        AuditStore(args.db).save_report(report, ledger=run.ledger.dumps())
    if args.share_out:
        from mendari.evals.share import shareable

        Path(args.share_out).parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(shareable(report), indent=2) + "\n"
        Path(args.share_out).write_text(payload, encoding="utf-8")
        print(f"wrote the report --share sends to {args.share_out} (metrics only)", file=sys.stderr)
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
    from mendari.evals.training import train_naive_bayes

    pack = resolve_pack(args.pack, missing_models_ok=True)
    trained = train_naive_bayes(
        pack, train=args.train, calibration=args.calibration, slot=args.slot
    )
    for item in trained:
        training = item.model["training"]
        calibration = training["calibration"]
        print(
            f"{item.slot}/{item.variant}: {training['examples']} examples, "
            f"vocabulary {training['vocabulary']}, temperature {item.model['temperature']} "
            f"(calibration NLL {calibration['nll_before']} -> {calibration['nll_after']}) "
            f"-> {item.path.relative_to(pack.root)}"
        )
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


def cmd_compare(args: argparse.Namespace) -> int:
    from mendari.core.registry import resolve_pack
    from mendari.evals import paired
    from mendari.evals.harness import load_dataset, resolve_dataset
    from mendari.evals.ledger import case_hash

    if args.seed < 0 or args.resamples < 1 or not 0 < args.alpha < 1:
        raise MendariError("--seed is 0 or more, --resamples 1 or more, --alpha between 0 and 1")
    a = paired.load(args.a, flag="A")
    b = paired.load(args.b, flag="B")
    if Path(args.dataset).is_file():
        dataset = load_dataset(args.dataset)
    else:
        dataset = resolve_dataset(resolve_pack(a.header["pack"]["id"]), args.dataset)
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

    pack = resolve_pack(args.pack)
    reports = [_read_json(path, "--report", shape=dict) for path in args.report]
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
        existing = _read_json(str(target), "--out", shape=dict)
    board = leaderboard.build(pack, reports, existing)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
    print("\n".join(leaderboard.markdown(board, pack.digest, pack.version)))
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
        import uvicorn  # noqa: F401 - there before any work (_run_server runs it)
    except ImportError:
        raise MendariError(
            "the server needs the server extra: pip install 'mendari[server]' (in a clone: "
            "pip install '.[server]', or uv sync)"
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
        default_providers=default_providers(),
        budget_limits=daily_limits(),
        datasets_dir=Path(args.datasets_dir) if args.datasets_dir else example_datasets_dir(),
    )
    # A typed --provider that no served pack takes (an unknown slot, a variant no pack with the
    # slot has) is a mistake; MENDARI_PROVIDERS, shared by a deployment's instances, is only
    # reported (/api/health, the startup log).
    not_taken = app.state.provider_settings_not_taken
    if args.provider and not_taken:
        raise MendariError(f"--provider settings no served pack takes: {', '.join(not_taken)}")
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
    with _read_store(args.db, phi=phi_policy() if args.notes else None) as store:
        status = store.verify(notes=args.notes)
    _print_json(status)
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


def _shown(target: str) -> str:
    """A database target as it is printed: a URL without its password, anywhere in it."""
    if "://" not in target:
        return target
    parts = urlsplit(target)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
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
# The tables every release of Mendari has had: a database from before a release that added one
# (`automation_holds`, LP-06) is still Mendari's, and its server migrates it.
_EVERY_RELEASE = ("audit_events", "decisions")


def _probe(target: str, tables: tuple[str, ...] = ()) -> tuple[bool, bool]:
    """(the database has Mendari's `tables`, they hold anything: a decision, a deployment or a
    hold), read without writing: nothing is created or migrated in a database that is not
    Mendari's."""
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
            rows = sum(
                (connection.execute(f"SELECT count(*) FROM {table}").fetchone() or (0,))[0]
                for table in tables
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
        for table in tables:
            counted = lite.execute(f"SELECT count(*) FROM {table}").fetchone()
            rows += counted[0] if counted else 0
        return True, rows > 0
    finally:
        lite.close()


def _hold_store(target: str | None, *, serving: bool) -> tuple[AuditStore, str]:
    """The deployment's database for the break-glass, refused when it cannot be the one a server
    runs on: not given, a SQLite file that does not exist (it would be created, and hold
    nothing), a database without Mendari's tables (nothing is created in it), and, to set a hold
    (`serving`), one that holds no decision, deployment or hold. Returns it and its name as
    shown (no password)."""
    target = target or os.environ.get("DATABASE_URL", "").strip() or None
    if target is None:
        raise MendariError("give --db (the server's database) or set DATABASE_URL")
    shown = _servers_database(target, used=serving)
    return AuditStore(target), shown


def _servers_database(target: str, *, used: bool, tables: tuple[str, ...] | None = None) -> str:
    """`target` as shown (no password) when it can be a server's database: it exists, has
    Mendari's tables (`tables`; read before anything is created in it) and, with `used`, holds
    a decision, a deployment or a hold. A `MendariError` saying why when it cannot."""
    shown = _shown(target)
    if not target.startswith(("postgres://", "postgresql://")) and not Path(target).is_file():
        raise MendariError(f"no database at {shown}: give the server's --db")
    try:
        mendari, held = _probe(target, tables or _MENDARI_TABLES)
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
        raise MendariError("--digest is a pack digest (sha256:<64 hex>)")
    store, shown = _hold_store(args.db, serving=True)
    if args.pack is not None:
        served = {d["pack_digest"] for d in store.deployments(args.pack, limit=1000)}
        if not served:
            raise MendariError(f"{shown} has never served {args.pack}")
        if args.digest is not None and args.digest not in served:
            raise MendariError(f"{shown} has never served {args.pack} at {args.digest}")
    org = None if args.all_orgs else args.org
    hold = store.hold_automation(
        org=org,
        pack_id=args.pack,
        pack_digest=args.digest,
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


def cmd_audit_export(args: argparse.Namespace) -> int:
    from mendari.standards import audit_fhir

    if args.administrative and args.pack:
        print("--pack is for decisions' events, not --administrative ones", file=sys.stderr)
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
            resources = audit_fhir.resources(store.iter_events(pack_id=args.pack, **bounds))
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
    """The deployment's totals, read-only: today's metered calls (Jev, LLM) against this
    environment's daily limits, the last purge with its counts, and the latest deployment
    records. In memberships mode the server shows them to no organization (ADR-0008)."""
    limits = daily_limits()
    with _read_store(args.db) as store:
        _print_json(
            {
                "day": clock.today().isoformat(),
                **{
                    provider: {
                        "calls_today": store.usage(provider),
                        "daily_limit": limits[provider],
                    }
                    for provider in ("jev", "llm")
                },
                "last_purge": store.last_purge(),
                "deployments": store.deployments(None, limit=args.deployments),
            }
        )
    return 0


def cmd_feedback_export(args: argparse.Namespace) -> int:
    from mendari.evals.feedback import feedback_dataset

    pack = resolve_pack(args.pack)
    with _read_store(args.db) as store:
        store.require_current("feedback export")
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
    providers = _providers(pack, parse_providers(args.provider), typed=bool(args.provider))
    decision = Decision(pack, providers=providers)
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
        import uvicorn  # noqa: F401 - there before any work (_run_server runs it)
    except ImportError:
        raise MendariError(
            "the payer needs the server extra: pip install 'mendari[server]' (in a clone: "
            "pip install '.[server]', or uv sync)"
        ) from None
    from mendari.sandbox.payer import create_payer_app

    _run_server(create_payer_app(), args.host, args.port)
    return 0


def cmd_pas_build(args: argparse.Namespace) -> int:
    from mendari.standards import pas

    with _read_store(args.db) as store:
        record = store.get(args.decision)
    if record is None:
        raise MendariError(f"no decision {args.decision} in {_shown(args.db)}")

    def resource(path: str) -> dict[str, Any]:
        loaded = _read_json(path, "a FHIR resource", shape=dict)
        if "resourceType" not in loaded:
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
        pack = archive.extract(verified, Path(scratch), origin="hub")  # as a puller loads it
        print(f"OK {pack.id}@{pack.version} {pack.digest} signed by {verified.key_id}")
    return 0


def cmd_registry_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn  # noqa: F401 - there before any work (_run_server runs it)
    except ImportError:
        raise MendariError(
            "the registry needs the server extra: pip install 'mendari[server]' (in a clone: "
            "pip install '.[server]', or uv sync)"
        ) from None
    from mendari.hub.registry import create_registry_app

    _run_server(create_registry_app(args.db), args.host, args.port)
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
    if pack.bounded:
        print("  runs on the engine for packs from a registry (RE2, a 1 s CPU budget per decision)")
    else:
        print("  runs on the maintained engine: a copy of the reference pack in this release")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from mendari.evals import figures, render

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
        benchmark = item.get("benchmark") or {}
        accuracy = benchmark.get("accuracy")
        # A registry entry is someone else's data: a figure that is not a whole k of a whole n
        # is shown as n/a (figures.counted), never a reason to stop the listing.
        shown = (
            ""
            if accuracy is None
            else f"  benchmark {render.shown(figures.counted(accuracy, benchmark.get('n')))}"
        )
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
    them together), posted to a webhook when one is set (`mendari.alerts`: in its format, signed
    with its secret, tried three times); exit 1 on any alert, 3 when they could not be posted.
    Nothing is recorded: the database is opened read-only."""
    from mendari.alerts import alert_settings, check_webhook, deliver
    from mendari.core import tenancy
    from mendari.evals.monitoring import monitor

    alerts = alert_settings(args.webhook)  # before any work, so a typo fails at once
    if alerts is not None:
        check_webhook(alerts.webhook)
    packs = [resolve_pack(args.pack)] if args.pack else discover().packs
    with _read_store(args.db) as store, tenancy.acting_for(args.org):
        store.require_current("monitor")
        reports = [monitor(store, pack, days=args.days) for pack in packs]
    alerting = [r for r in reports if r["alerts"]]
    for report in reports:
        status = f"{len(report['alerts'])} alert(s)" if report["alerts"] else "ok"
        print(f"{report['pack']['id']}: {status}")
        for alert in report["alerts"]:
            print(f"  [{alert['severity']}] {alert['kind']}: {alert['message']}")
    if alerts is not None and alerting:
        lines = [
            f"{report['pack']['id']}: [{alert['severity']}] {alert['kind']}: {alert['message']}"
            for report in alerting
            for alert in report["alerts"]
        ]
        posted = deliver(
            alerts,
            event="monitor.alerts",
            org=args.org,
            alerts=[{"pack": r["pack"], "alerts": r["alerts"]} for r in alerting],
            lines=lines,
            attempts=3,
            timeout=10,
        )
        if not posted:
            print("error: the alert webhook failed after 3 tries", file=sys.stderr)
            return 3
    return 1 if alerting else 0


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


TEMPLATE = Path(__file__).resolve().parent / "templates" / "new_pack"
# Laid over TEMPLATE by `mendari new --community`: the community defaults.
COMMUNITY_TEMPLATE = Path(__file__).resolve().parent / "templates" / "community_pack"
# A URL, not a path: `mendari new` runs anywhere, mostly outside a checkout of the repository.
AUTHORING_GUIDE = (
    "https://github.com/Junebugg1214/mendari/blob/main/docs/guides/authoring-a-pack.md"
)
COMMUNITY_GUIDE = (
    "https://github.com/Junebugg1214/mendari/blob/main/docs/guides/contributing-a-pack.md"
)


def cmd_new(args: argparse.Namespace) -> int:
    """A new pack from the template: YAML only, and it validates, tests and evaluates as it is.
    With `--community`, a community pack (`community-packs/<name>`, id `community/<name>`) that
    also passes `mendari community-check`, its benchmark and card written."""
    from mendari.core.origin import COMMUNITY_NAME, COMMUNITY_NAMESPACE
    from mendari.core.spec import _PACK_ID

    target = Path(args.dir)
    if target.exists() and any(target.iterdir()):
        raise MendariError(f"{target} exists and is not empty")
    pack_id = args.id
    if args.community:
        folder = target.resolve().name
        if not COMMUNITY_NAME.match(folder):
            raise MendariError(
                f"a community pack's folder name is lowercase letters, digits and dashes, "
                f"not {folder!r}"
            )
        expected = f"{COMMUNITY_NAMESPACE}/{folder}"
        if pack_id not in (None, expected):
            raise MendariError(f"a community pack in {folder}/ has the id {expected}")
        pack_id = expected
    if pack_id is None:
        raise MendariError("--id is required: namespace/name, e.g. my-clinic/refill-router")
    if not _PACK_ID.match(pack_id):
        raise MendariError("--id must look like 'namespace/name' (lowercase, digits, dashes)")
    name = args.name or pack_id.split("/", 1)[1].replace("-", " ").capitalize()
    values = {"{{id}}": pack_id, "{{name}}": name, "{{publisher}}": args.publisher}
    templates = [TEMPLATE, COMMUNITY_TEMPLATE] if args.community else [TEMPLATE]
    for template in templates:
        for source in sorted(template.rglob("*")):
            if not source.is_file():
                continue
            text = source.read_text(encoding="utf-8")
            for placeholder, value in values.items():
                text = text.replace(placeholder, value)
            destination = target / source.relative_to(template)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
    if args.community:
        _publish_benchmark(target)
    print(f"{pack_id} created in {target}. Next:")
    steps = [f"mendari validate {target}", f"mendari test {target}"]
    if args.community:
        benchmark = f"{target}/evals/benchmark.json"
        steps += [
            f"mendari eval {target} --dataset examples --baseline none --out {benchmark}",
            f"mendari card {target} --write",
            f"add {{id: {pack_id}, maintainers: [your-github-name]}} to "
            "community-packs/MAINTAINERS.yaml",
            f"mendari community-check {target} --base origin/main",
        ]
    else:
        steps += [
            f"mendari eval {target} --dataset examples",
            f"mendari card {target} --write",
        ]
    for step in steps:
        print(f"  {step}")
    print(f"The guide: {AUTHORING_GUIDE}")
    if args.community:
        print(f"Contributing it: {COMMUNITY_GUIDE}")
    return 0


def _publish_benchmark(target: Path) -> None:
    """Write a new pack's benchmark (`evaluation.benchmark`) and Decision Card, as `mendari
    eval --baseline none --out` and `mendari card --write` would."""
    from mendari.core.card import render_card
    from mendari.core.pack import load_pack
    from mendari.evals import resolve_dataset
    from mendari.evals.harness import evaluation as evaluate_with_ledger

    pack = load_pack(target, origin="community")
    evaluation = pack.manifest.evaluation
    assert evaluation.benchmark and evaluation.benchmark_dataset
    run = evaluate_with_ledger(Decision(pack), resolve_dataset(pack, evaluation.benchmark_dataset))
    destination = target / evaluation.benchmark
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_ledger(destination, run.ledger.dumps(), run.ledger.file_name)
    destination.write_text(json.dumps(run.report, indent=2) + "\n", encoding="utf-8")
    pack = load_pack(target, origin="community")
    (target / "DECISION_CARD.md").write_text(render_card(pack), encoding="utf-8")


def cmd_community_check(args: argparse.Namespace) -> int:
    from mendari.community.check import check

    report = check(args.dir, base=args.base)
    print(report.markdown() if args.markdown else report.text())
    return 0 if report.passed else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from mendari.sandbox.demo import run_demo

    run_demo(datasets_dir=example_datasets_dir())
    return 0


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# -- parser ----------------------------------------------------------------------------------


def _port(text: str) -> int:
    """A TCP port, checked before a server starts (argparse names the flag)."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port: {text!r}") from None
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError(f"not a port: {port} (1 to 65535)")
    return port


def _iso_date(text: str) -> str:
    """A date as YYYY-MM-DD, checked before any work starts (argparse names the flag)."""
    try:
        date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a date: give YYYY-MM-DD") from None
    return text


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


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="mendari", description="Package, evaluate and deploy healthcare Decision Packs."
    )
    parser.add_argument("--version", action="version", version=f"mendari {__version__}")
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
    evaluate.add_argument(
        "--share-out",
        metavar="FILE",
        help="write the report --share sends to FILE, to read first (sends nothing)",
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

    paired_runs = sub.add_parser(
        "compare", help="compare two evaluation runs case by case (from their ledgers)"
    )
    paired_runs.add_argument("a", help="run A: a ledger (.jsonl) or a report with its ledger")
    paired_runs.add_argument("b", help="run B, on the same dataset")
    paired_runs.add_argument("--dataset", required=True, help="pack dataset name or JSONL path")
    paired_runs.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    paired_runs.add_argument("--resamples", type=int, default=2000, help="bootstrap resamples")
    paired_runs.add_argument("--alpha", type=float, default=0.05, help="level (default 0.05)")
    paired_runs.add_argument("--json", action="store_true", help="JSON, cases by hash only")
    paired_runs.add_argument("--out", help="also write the comparison as JSON")
    paired_runs.set_defaults(handler=cmd_compare)

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
    serve.add_argument("--port", type=_port, default=8000)
    serve.add_argument(
        "--db", default="var/mendari.db", help="audit database: SQLite path or postgresql:// URL"
    )
    serve.add_argument("--datasets-dir", help="extra evaluation datasets for the dashboard")
    serve.add_argument(
        "--provider",
        action="append",
        metavar="SLOT=VARIANT",
        help="instead of $MENDARI_PROVIDERS; with Jev set up, the reference packs run Mendari + "
        "Jev in every slot these choose nothing for",
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
        help="the deployment's totals: today's metered calls, the last purge, its deployments",
    )
    usage.add_argument("--db", default="var/mendari.db", help="SQLite path or postgresql:// URL")
    usage.add_argument(
        "--deployments", type=int, default=20, help="how many deployment records (newest first)"
    )
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
    hold.add_argument("--pack", help="one pack (namespace/name); default: every pack")
    hold.add_argument("--digest", help="one version of --pack (sha256:...)")
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
    payer.add_argument("--port", type=_port, default=8200)
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
        command.add_argument(
            "--as-of", type=_iso_date, help="screening date (default: each person's last day)"
        )
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
    serve_registry.add_argument("--port", type=_port, default=8100)
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
    new.add_argument("--id", help="namespace/name, e.g. my-clinic/refill-router")
    new.add_argument("--name", help="a display name (default: from the id)")
    new.add_argument("--publisher", default="Your organization", help="who publishes it")
    new.add_argument(
        "--community",
        action="store_true",
        help="a community pack for community-packs/<name> (id community/<name>), with the "
        "defaults community-check asks for",
    )
    new.set_defaults(handler=cmd_new)

    community = sub.add_parser(
        "community-check",
        help="the checks CI runs on a community pack (community-packs/<name>), with what to fix",
    )
    community.add_argument("dir", help="the pack's folder, community-packs/<name>")
    community.add_argument(
        "--base",
        help="a git commit to compare with (e.g. origin/main): also checks the change's scope, "
        "the version bump and regressions",
    )
    community.add_argument("--markdown", action="store_true", help="print Markdown (CI summary)")
    community.set_defaults(handler=cmd_community_check)

    sub.add_parser("demo", help="offline end-to-end demo on synthetic data").set_defaults(
        handler=cmd_demo
    )
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
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
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
    if isinstance(error, PackValidationError):
        return [_one_line(f"{error.pack}: {problem}") for problem in error.problems]
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
