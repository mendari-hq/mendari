# SPDX-License-Identifier: Apache-2.0
"""A paired comparison of two evaluation runs on the same cases (`mendari compare A B --dataset X`,
LP-10), from their ledgers: two configurations, or two versions of an agent. Unlike
`mendari.evals.compare` (one run against the agent's published benchmark, different cases), every
figure here is on the cases both runs share, so a difference is the configurations', not the cases'.

Each figure is k of n in each run with its exact 95% interval; the cases where the two disagree
(b: only A, c: only B) give an exact McNemar p-value, adjusted with Holm over the family named in
the output, and `different` is Holm's decision at alpha. A figure with too few discordant cases
for the exact test ever to reach alpha (fewer than 6 at the default 0.05), or a critical label
with fewer than 100 positives, also says "insufficient evidence", beside that decision. The
primary question's Brier score is compared with a seeded paired bootstrap (a 95% interval),
outside the Holm family.
The cases that changed are listed by their ledger hash: critical detections first, then false
automation, correctness, and automation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mendari.errors import MendariError
from mendari.evals import metrics, stats
from mendari.evals.ledger import Ledger
from mendari.evals.render import shown
from mendari.evals.result import MIN_POSITIVES, PREDICTED, PROBABILITIES, TRUTH

FORMAT = "mendari.compare/1"
ALPHA = 0.05

Case = Mapping[str, Any]
Event = Callable[[Case], bool | None]  # None: the figure does not apply to this case


def load(path: str | Path, *, flag: str) -> Ledger:
    """A ledger: a `.jsonl` ledger, or a `mendari.eval/1` report whose ledger is beside it in
    `ledgers/` (as `mendari eval --out` writes it)."""
    path = Path(path)
    where = f"{flag} {path.name}"
    try:
        data = path.read_bytes()
    except OSError:
        raise MendariError(f"{where}: no such file") from None
    if path.suffix != ".json":
        return Ledger.loads(data, where=where)
    try:
        report = json.loads(data)
        ledger_id = report["ledger"]["id"]
    except (ValueError, KeyError, TypeError):
        raise MendariError(f"{where}: a report without a ledger; evaluate it again") from None
    beside = path.parent / "ledgers" / (str(ledger_id).removeprefix("sha256:") + ".jsonl")
    try:
        ledger = Ledger.loads(beside.read_bytes(), where=f"{where}'s ledger")
    except OSError:
        raise MendariError(
            f"{where}: its ledger is not beside it (ledgers/{beside.name})"
        ) from None
    if ledger.id != ledger_id:
        raise MendariError(f"{where}: the ledger beside it is not the one it names")
    return ledger


def _primary(ledger: Ledger) -> str:
    return str(ledger.header["primary"])


def _entry(case: Case, question: str) -> list[Any] | None:
    entry = case["q"].get(question)
    return None if entry is None else list(entry)


def _figures(ledger: Ledger) -> dict[str, tuple[Event, bool]]:
    """Each figure: what happens on a case, and whether more of it is better."""
    primary = _primary(ledger)

    def truth(case: Case) -> str | None:
        entry = _entry(case, primary)
        return None if entry is None else entry[TRUTH]

    def correct(case: Case) -> bool | None:
        entry = _entry(case, primary)
        if entry is None or entry[TRUTH] is None:
            return None
        return bool(entry[PREDICTED] == entry[TRUTH])

    def false_automation(case: Case) -> bool | None:
        expected = truth(case)
        return None if expected is None else case["o"] == "AUTO" and case["r"] != expected

    figures: dict[str, tuple[Event, bool]] = {}
    for index, (question, label) in enumerate(ledger.header["critical"]):

        def detected(
            case: Case, i: int = index, q: str = question, lab: str = label
        ) -> bool | None:
            if i not in case["k"]:
                return None
            entry = _entry(case, q)
            return entry is not None and entry[PREDICTED] == lab

        figures[f"critical {question}={label}"] = (detected, True)
    figures["false automation"] = (false_automation, False)
    figures["accuracy"] = (correct, True)
    figures["automation"] = (lambda case: case["o"] == "AUTO", True)
    return figures


def _brier(case: Case, question: str, labels: list[str]) -> float | None:
    entry = _entry(case, question)
    if entry is None or entry[TRUTH] is None or entry[PREDICTED] is None:
        return None
    return metrics.brier([dict(zip(labels, entry[PROBABILITIES], strict=True))], [entry[TRUTH]])


def compare(
    a: Ledger,
    b: Ledger,
    *,
    dataset_sha256: str,
    seed: int = 0,
    resamples: int = stats.RESAMPLES,
    alpha: float = ALPHA,
    lineage: bool = False,
) -> dict[str, Any]:
    """A against B on the cases both ran, from the dataset whose sha256 is given. `lineage`: A
    and B may be agents of other ids that ask the same questions, an agent store's draft and the
    agent it was copied from (LP-20g)."""
    for name, ledger in (("A", a), ("B", b)):
        if ledger.header["dataset"]["sha256"] != dataset_sha256:
            raise MendariError(f"{name} was not run on this dataset (its sha256 differs)")
    if not lineage and a.header["agent"]["id"] != b.header["agent"]["id"]:
        raise MendariError("A and B are evaluations of different agents")
    if (
        a.header["questions"] != b.header["questions"]
        or a.header["critical"] != b.header["critical"]
    ):
        raise MendariError("A and B ask different questions: compare runs of one agent's questions")
    in_b = {case["c"]: case for case in b.cases}
    pairs = [(case, in_b[case["c"]]) for case in a.cases if case["c"] in in_b]
    if not pairs:
        raise MendariError("A and B share no case")
    shared = {case["c"] for case, _ in pairs}

    figures: list[dict[str, Any]] = []
    p_values: dict[str, float] = {}
    changed: dict[str, list[str]] = {}
    for name, (event, better_up) in _figures(a).items():
        on = [(event(x), event(y), x["c"]) for x, y in pairs]
        on = [(ea, eb, c) for ea, eb, c in on if ea is not None and eb is not None]
        n = len(on)
        only_a = sum(1 for ea, eb, _ in on if ea and not eb)
        only_b = sum(1 for ea, eb, _ in on if eb and not ea)
        p = stats.mcnemar_exact(only_a, only_b)
        # The smallest p-value that many discordant cases can give, against alpha.
        unreachable = stats.mcnemar_exact(only_a + only_b, 0) > alpha
        few = unreachable or (name.startswith("critical") and n < MIN_POSITIVES)
        figures.append(
            {
                "figure": name,
                "higher_is_better": better_up,
                "a": stats.proportion(sum(1 for ea, _, _ in on if ea), n).to_dict(),
                "b": stats.proportion(sum(1 for _, eb, _ in on if eb), n).to_dict(),
                "only_a": only_a,
                "only_b": only_b,
                "p": p,
                "evidence": "insufficient" if few else "sufficient",
            }
        )
        p_values[name] = p
        for ea, eb, case in on:
            if ea != eb:
                changed.setdefault(case, []).append(name)
    family = stats.holm_family(p_values, alpha)
    for figure in figures:
        figure["p_adjusted"] = family[figure["figure"]]["adjusted"]
        figure["different"] = family[figure["figure"]]["reject"]

    primary = _primary(a)
    labels = list(a.header["questions"][primary])
    scored = [
        (sa, sb)
        for x, y in pairs
        if (sa := _brier(x, primary, labels)) is not None
        and (sb := _brier(y, primary, labels)) is not None
    ]
    brier = None
    if scored:
        estimate, low, high, p_boot = stats.paired_bootstrap(
            [sa for sa, _ in scored], [sb for _, sb in scored], resamples=resamples, seed=seed
        )
        brier = {
            "question": primary,
            "n": len(scored),
            "a_minus_b": round(estimate, stats.DIGITS),
            "low": round(low, stats.DIGITS),
            "high": round(high, stats.DIGITS),
            "p": p_boot,
            "note": "lower is better; outside the Holm family",
        }
    order = {name: i for i, name in enumerate(_figures(a))}
    return {
        "format": FORMAT,
        "agent": a.header["agent"]["id"],
        "a": {"ledger": a.id, "agent": a.header["agent"], "providers": a.header["providers"]},
        "b": {"ledger": b.id, "agent": b.header["agent"], "providers": b.header["providers"]},
        "dataset": {
            "sha256": dataset_sha256,
            "paired": len(pairs),
            "only_in_a": len(a.cases) - len(shared),
            "only_in_b": len(b.cases) - len(shared),
        },
        "alpha": alpha,
        "interval_level": stats.LEVEL,
        "seeds": {"bootstrap": seed, "resamples": resamples},
        "holm_family": list(p_values),
        "figures": figures,
        "brier": brier,
        "changed": [
            {"case": case, "changes": names}
            for case, names in sorted(changed.items(), key=lambda kv: (order[kv[1][0]], kv[0]))
        ],
    }


def text(result: Mapping[str, Any], ids: Mapping[str, Any] | None = None) -> str:
    """The comparison in words; `ids` (local only) names each changed case by its id."""
    dataset = result["dataset"]
    lines = [
        f"{result['agent']}: A against B on {dataset['paired']} shared cases "
        f"(only in A: {dataset['only_in_a']}, only in B: {dataset['only_in_b']})",
        f"Holm-adjusted over: {', '.join(result['holm_family'])} (alpha {result['alpha']}); "
        f"k of n [exact {result['interval_level']:.0%} interval, Clopper-Pearson]",
        "",
    ]
    for f in result["figures"]:
        verdict = "different" if f["different"] else "no difference shown"
        if f["evidence"] == "insufficient":
            verdict += "; insufficient evidence"
        lines.append(
            f"{f['figure']}: A {shown(f['a'])}, B {shown(f['b'])}; only A {f['only_a']}, "
            f"only B {f['only_b']}, p {f['p']:.4g} (adjusted {f['p_adjusted']:.4g}): {verdict}"
        )
    brier = result["brier"]
    if brier is not None:
        lines.append(
            f"Brier ({brier['question']}), A minus B: {brier['a_minus_b']:+.4f} "
            f"[{brier['low']:+.4f}, {brier['high']:+.4f}], p {brier['p']:.4g} on {brier['n']} "
            f"cases ({brier['note']}; seed {result['seeds']['bootstrap']})"
        )
    if result["changed"]:
        lines += ["", f"Changed cases ({len(result['changed'])}):"]
        for item in result["changed"][:50]:
            name = str((ids or {}).get(item["case"], item["case"]))
            lines.append(f"  {name}: {', '.join(item['changes'])}")
        if len(result["changed"]) > 50:
            lines.append(f"  and {len(result['changed']) - 50} more (--json lists them all)")
    return "\n".join(lines)
