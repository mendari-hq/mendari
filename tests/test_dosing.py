# SPDX-License-Identifier: Apache-2.0
"""The `dosing` provider: dose arithmetic on free text, in code."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from mendari import Decision
from mendari.errors import PackValidationError

MINI = Path(__file__).parent / "fixtures" / "packs" / "guard-mini"


@pytest.fixture(scope="module")
def guard() -> Decision:
    return Decision.from_path(MINI)


def dose(guard: Decision, draft: str) -> tuple[str, tuple[str, ...]]:
    answer = guard.providers["dosing"].answer({"draft": draft}).answers["dose_exceeds_limit"]
    return answer.label, answer.evidence


@pytest.mark.parametrize(
    ("draft", "label", "evidence"),
    [
        # dose x frequency
        ("Take 1000 mg of acetaminophen every 4 hours.", "yes", "= 6000 mg/day, above 4000"),
        ("Take 1000 mg of acetaminophen every 6 hours.", "no", "= 4000 mg/day, within 4000"),
        ("Take acetaminophen 500 mg, 2 tablets every 4-6 hours.", "yes", "1000 mg x 6 a day"),
        ("Take two 500 mg Tylenol tablets up to 4 times a day.", "no", "1000 mg x 4 a day"),
        ("Take 1 g of paracetamol four times a day.", "no", "1000 mg x 4 a day"),
        ("Ibuprofen 200 mg TID.", "no", "200 mg x 3 a day"),
        ("Take Advil 400 mg q4h.", "yes", "400 mg x 6 a day = 2400 mg/day"),
        ("Take Tylenol 500 mg twice a day.", "no", "500 mg x 2 a day"),
        ("Take 2 Advil tablets of 400 mg three times a day.", "yes", "800 mg, above 400 mg"),
        # stated daily totals
        ("Do not take more than 8 tablets of 500 mg of Tylenol a day.", "no", "4000 mg/day stated"),
        ("Take up to 6,000 mg of paracetamol daily.", "yes", "6000 mg/day stated"),
        # counts without a strength use the pack's tablet strength
        ("Take 2 Tylenol tablets every 6 hours.", "no", "1000 mg x 4 a day"),
        # single doses
        ("Take 1500 mg of Tylenol.", "yes", "single dose 1500 mg, above 1000 mg"),
        ("Ibuprofen 800 mg three times a day.", "yes", "single dose 800 mg, above 400 mg"),
        ("Take Advil 300 mg.", "no", "single dose 300 mg, within 400 mg"),
        # nothing to check
        ("Take ibuprofen as needed.", "no", "no listed medicine is dosed"),
        ("Take 2.5 mg of lisinopril daily.", "no", "no listed medicine is dosed"),
        ("Rest and drink plenty of fluids.", "no", "no listed medicine is dosed"),
    ],
)  # fmt: skip
def test_daily_amounts(guard: Decision, draft: str, label: str, evidence: str) -> None:
    got_label, got_evidence = dose(guard, draft)
    assert got_label == label and evidence in " | ".join(got_evidence)


def test_the_worst_sentence_decides_and_follow_ups_keep_the_medicine(guard: Decision) -> None:
    draft = "Take 500 mg of acetaminophen every 6 hours. If needed, take 1000 mg every 3 hours."
    label, evidence = dose(guard, draft)
    assert label == "yes" and len(evidence) == 2
    assert "1000 mg x 8 a day = 8000 mg/day" in evidence[1]
    # With two medicines named, an unnamed follow-up dose is not guessed.
    two = "Take acetaminophen or ibuprofen. Take 1000 mg every 3 hours."
    assert dose(guard, two) == ("no", ("no listed medicine is dosed",))


def test_a_dose_without_a_frequency_or_single_limit_is_unknown(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    shutil.copytree(MINI, root)
    table = yaml.safe_load((root / "logic" / "doses.yaml").read_text())
    table["medicines"]["naproxen"] = {"names": ["naproxen"], "max_daily_mg": 1000}
    (root / "logic" / "doses.yaml").write_text(yaml.safe_dump(table))
    decision = Decision.from_path(root)
    answer = decision.providers["dosing"].answer({"draft": "Take naproxen 500 mg."})
    assert answer.answers["dose_exceeds_limit"].label == "unknown"


def test_configuration_is_checked(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    shutil.copytree(MINI, root)
    table = yaml.safe_load((root / "logic" / "doses.yaml").read_text())
    table["medicines"]["acetaminophen"]["max_daily_mg"] = 0
    (root / "logic" / "doses.yaml").write_text(yaml.safe_dump(table))
    with pytest.raises(PackValidationError, match="max_daily_mg"):
        Decision.from_path(root)
