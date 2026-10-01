"""Shareable evaluation reports (evals/share.py): metrics leave the site, cases never do."""

from __future__ import annotations

import json

from mendari.core.registry import resolve_pack
from mendari.evals.share import MIN_GROUP, shareable, summary

BENCHMARK = json.loads(
    (resolve_pack("mendari/referral-router").root / "evals" / "benchmark.json").read_text()
)


def test_case_ids_and_dataset_identity_stay_home() -> None:
    shared = shareable(BENCHMARK)
    text = json.dumps(shared)
    assert "misclassified" not in shared
    assert all(case["id"] not in text for case in BENCHMARK["misclassified"])
    assert shared["dataset"] == {"n": BENCHMARK["dataset"]["n"]}  # no name, no digest
    assert "id" not in shared  # the report's own id links it to the site's store


def test_small_groups_and_bins_are_left_out() -> None:
    report = {
        **BENCHMARK,
        "subgroups": {
            "age": {"90+": {"n": 2, "accuracy": 0.5}, "40-64": {"n": 50, "accuracy": 0.9}}
        },
    }
    shared = shareable(report)
    assert shared["subgroups"] == {"age": {"40-64": {"n": 50, "accuracy": 0.9}}}
    bins = shared["questions"]["next_step"]["calibration"]["bins"]
    assert bins and all(b["n"] >= MIN_GROUP for b in bins)
    assert (
        shared["questions"]["next_step"]["calibration"]["ece"]
        == BENCHMARK["questions"]["next_step"]["calibration"]["ece"]
    )


def test_the_summary_names_the_numbers_a_comparison_shows() -> None:
    numbers = summary(shareable(BENCHMARK))
    assert numbers["primary"] == "next_step" and numbers["n"] == BENCHMARK["dataset"]["n"]
    assert numbers["accuracy"] == BENCHMARK["questions"]["next_step"]["accuracy"]
    assert numbers["automation_rate"] == 0.0
    assert numbers["ece"] is not None
