"""`openhealth demo`: the v0.1 story end to end, offline, on synthetic data, in a few seconds."""

from __future__ import annotations

from pathlib import Path

from openhealth.core.audit import AuditStore
from openhealth.core.card import benchmark
from openhealth.core.decision import Decision
from openhealth.evals import compare, evaluate, load_dataset, resolve_dataset
from openhealth.evals.render import comparison_text, pct

PACK_ID = "openhealth/patient-inbox-router"
SAMPLE_IDS = ("pub-01800", "pub-01801", "pub-01802", "pub-01803", "pub-01805", "pub-01806")


def _section(title: str) -> None:
    print(f"\n== {title} {'=' * max(0, 72 - len(title))}")


def run_demo(datasets_dir: Path | None) -> None:
    store = AuditStore(":memory:")
    router = Decision.from_hub(PACK_ID, store=store)
    pack = router.pack
    m = pack.manifest
    providers = ", ".join(f"{slot}={info.variant}" for slot, info in router.provider_info.items())
    print(f"{m.name} {m.version}  ·  risk level {int(m.risk.level)} ({m.risk.category})")
    print(f"providers: {providers}")
    print(f"digest:    {pack.digest}")

    _section("1. Route synthetic patient messages")
    test = resolve_dataset(pack, "test")
    examples = [r for r in test.records if r["id"] in SAMPLE_IDS]
    examples += [r for r in test.records if r["labels"]["urgency"] == "urgent"][:2]
    examples.append({"id": "adhoc", "input": {"message": {"body": "hello?"}}, "labels": {}})
    for record in examples:
        result = router(record["input"])
        body = " ".join(record["input"]["message"]["body"].split())
        confidence = "  -  " if result.confidence is None else f"{result.confidence:.2f}"
        target = result.recommendation or "(manual triage)"
        print(
            f"{result.outcome.value:<12} {target:<17} {result.priority.value:<7} {confidence}  "
            f"{body[:62]}{'…' if len(body) > 62 else ''}"
        )

    _section("2. Human review queue")
    truth = {}
    for record in test.records[:300]:
        truth[router(record["input"]).id] = record["labels"]["destination"]
    pending = store.pending_reviews(limit=1000)
    print(f"{len(pending)} decisions waiting for review (stat first). Simulating reviewers…")
    for item in pending:
        if item["id"] in truth:
            store.resolve_review(item["id"], final_label=truth[item["id"]], reviewer="demo-nurse")
    stats = store.stats()
    outcomes = "  ".join(f"{k} {v}" for k, v in stats["outcomes"].items())
    print(f"decisions: {stats['decisions']}   {outcomes}")
    print(
        f"human override rate: {pct(stats['human_override_rate'])} "
        f"of {stats['reviews_resolved']} reviews"
    )
    chain = store.verify()
    print(f"audit chain: {'verified' if chain['ok'] else 'BROKEN'} ({chain['events']} events)")

    _section("3. Local validation before deployment")
    baseline = benchmark(pack)
    local_path = (datasets_dir / "local-site-example.jsonl") if datasets_dir else None
    if baseline is None or local_path is None or not local_path.is_file():
        print("skipped: needs the pack benchmark and datasets/local-site-example.jsonl")
        return
    local = evaluate(Decision(pack), load_dataset(local_path))
    comparison = compare(local, baseline, m.evaluation.degradation_tolerance)
    print(comparison_text(comparison))
    critical = local["decisions"]["critical"][0]
    print(
        f"\nEmergency screen at the local site: {critical['detected']}/{critical['n']} detected, "
        f"{critical['auto_missed']} automated."
    )
    print("Next: `openhealth serve` and open http://127.0.0.1:8000 for the dashboard.")
