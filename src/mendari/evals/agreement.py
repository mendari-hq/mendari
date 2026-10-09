# SPDX-License-Identifier: Apache-2.0
"""Do two variants of a provider slot give the same answers? For logic ported from one form to
another (the trial pack's criteria, written once in YAML and once in CQL), agreement on every
case is the test that the port is faithful.

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
    differences: list[dict[str, str]] = []
    counted = 0
    for case_id, inputs in cases:
        counted += 1
        a, b = first.answer(inputs).answers, second.answer(inputs).answers
        for question in sorted(a):
            if a[question].label != b[question].label:
                differences.append(
                    {
                        "case": case_id,
                        "question": question,
                        left: a[question].label,
                        right: b[question].label,
                    }
                )
    return {
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "slot": slot,
        "variants": [left, right],
        "cases": counted,
        "questions": sorted(first.questions),
        "agree": not differences,
        "differences": differences,
    }
