# SPDX-License-Identifier: Apache-2.0
"""Do two variants of a provider slot give the same answers? For logic ported from one form to
another (a trial-eligibility agent's criteria, written once in YAML and once in CQL),
agreement on every case is the test that the port is faithful.

The report names cases by id and questions by key, never case text, like every report.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from mendari.core.pack import Pack
from mendari.errors import MendariError
from mendari.evals.harness import resolve_dataset
from mendari.evals.pack_tests import load_cases
from mendari.providers import build_provider

Case = tuple[str, Mapping[str, Any]]


def pack_cases(pack: Pack, datasets: Iterable[str] | None = None) -> list[Case]:
    """The pack's behaviour tests and datasets (all of its evaluation datasets by default)."""
    cases: list[Case] = [
        (f"test: {c.get('name', 'unnamed case')}", c["input"]) for c in load_cases(pack)
    ]
    for ref in datasets if datasets is not None else pack.manifest.evaluation.datasets:
        dataset = resolve_dataset(pack, ref)
        cases += [(f"{dataset.name}: {r['id']}", r["input"]) for r in dataset.records]
    return cases


def agreement(
    pack: Pack, slot: str, left: str, right: str, cases: Iterable[Case]
) -> dict[str, Any]:
    if left == right:
        raise MendariError("compare two different variants")
    first, second = build_provider(pack, slot, left), build_provider(pack, slot, right)
    # Every provider that would be asked, a composite's members included, can be: a member
    # that cannot (an `llm` with no model, say) would leave the other to answer alone, and the
    # report would call two variants equal when one never ran.
    for leaf in (*first.leaves(), *second.leaves()):
        leaf.preflight()
    differences: list[dict[str, Any]] = []
    counted = 0
    for case_id, inputs in cases:
        counted += 1
        ran = {left: first.answer(inputs), right: second.answer(inputs)}
        asked = {q for output in ran.values() for q in (*output.answers, *output.failures)}
        for question in sorted(asked):
            labels = {
                name: output.answers[question].label if question in output.answers else None
                for name, output in ran.items()
            }
            # A question one variant left unanswered, or answered with a member down (a
            # composite's partial failure), is not agreement: a port is shown faithful only
            # where both variants ran whole and gave the same label.
            failed = [name for name, output in ran.items() if question in output.failures]
            if labels[left] != labels[right] or failed:
                difference: dict[str, Any] = {"case": case_id, "question": question, **labels}
                if failed:
                    difference["failed"] = failed
                differences.append(difference)
    return {
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "slot": slot,
        "variants": [left, right],
        "cases": counted,
        "questions": sorted(first.questions),
        # No case compared is no evidence: agreement is never claimed on none.
        "agree": counted > 0 and not differences,
        "differences": differences,
    }
