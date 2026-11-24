# SPDX-License-Identifier: Apache-2.0
"""The grading method 1.0 (LP-17a, docs/grading/method-1.0.md) and its pinned constants
(`mendari.evals.method`): the code pins the document's hash and every number in its parameter
block; the document has its version, an effective-date line and a changelog; the evidence rule's
minimums reach the floor with a perfect result and no fewer do (against scipy and statsmodels),
and its table is what the code gives; nothing is in effect without an effective date."""

from __future__ import annotations

import datetime
import hashlib
import inspect
import itertools
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from scipy import stats as st
from statsmodels.stats.proportion import proportion_confint

from mendari.core import audit
from mendari.evals import method, result, share, stats

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / method.METHOD_PATH
TEXT = DOCUMENT.read_text(encoding="utf-8")
LINES = TEXT.splitlines()
FLOORS = (0.90, 0.95, 0.97, 0.98, 0.99)
# What the module holds besides its parameters.
NOT_PARAMETERS = {"METHOD", "METHOD_PATH", "METHOD_SHA256", "EFFECTIVE", "PARAMETERS"}
# The critical labels of each reference pack (L), as the method's Terms name them.
CRITICAL_LABELS = {
    "mendari/patient-inbox-router": 1,
    "mendari/prior-auth-preflight": 1,
    "mendari/referral-router": 2,
    "mendari/ai-guard": 4,
    "mendari/clinical-trial-prescreener": 6,
}


def section(heading: str) -> str:
    """The text under one `## ` heading of the method, up to the next."""
    _, found, rest = TEXT.partition(f"\n## {heading}\n")
    assert found, f"no section {heading!r}"
    return rest.split("\n## ", 1)[0]


def parameter_block() -> tuple[str, dict[str, Any]]:
    blocks = re.findall(r"^```yaml\n(.*?)^```$", TEXT, flags=re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, "the method has one machine-readable parameter block"
    assert blocks[0] in section("Parameters")
    loaded = yaml.safe_load(blocks[0])
    assert isinstance(loaded, dict)
    return blocks[0], loaded


def typed(value: Any) -> Any:
    """A value with its type, lists as tuples: 2000 is not 2000.0, nor 0.5 "0.5"."""
    if isinstance(value, list | tuple):
        return tuple(typed(v) for v in value)
    return (type(value).__name__, value)


def scipy_lower(n: int, labels: int) -> float:
    """The lower bound of n of n, two-sided at 1 - 0.05/L: Beta(n, 1)'s quantile at alpha/2."""
    return float(st.beta.ppf(0.05 / labels / 2, n, 1))


def statsmodels_lower(n: int, labels: int) -> float:
    return float(proportion_confint(n, n, alpha=0.05 / labels, method="beta")[0])


def lower_bounds(n: int, labels: int) -> tuple[float, float, float]:
    """Mendari's, scipy's and statsmodels' lower bound of a perfect result on n positives."""
    level = method.simultaneous_level(labels)
    return (
        stats.clopper_pearson(n, n, level)[0],
        scipy_lower(n, labels),
        statsmodels_lower(n, labels),
    )


def test_the_pinned_hash_is_the_method_documents() -> None:
    """The hash in the code is of the document's bytes. It is pinned, not computed at run time,
    because the document is in neither the wheel nor the sdist: an installed package cannot read
    it (pyproject.toml, checked here so that a change to that is a change to this choice)."""
    assert method.METHOD_PATH == "docs/grading/method-1.0.md"
    assert re.fullmatch(r"[0-9a-f]{64}", method.METHOD_SHA256)
    assert hashlib.sha256(DOCUMENT.read_bytes()).hexdigest() == method.METHOD_SHA256
    targets = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]
    sdist, wheel = targets["targets"]["sdist"], targets["targets"]["wheel"]
    assert not [path for path in sdist["include"] if path.lstrip("/").startswith("docs")]
    assert wheel["packages"] == ["src/mendari"]
    assert not [path for path in wheel.get("force-include", {}) if path.startswith("docs")]


def test_every_parameter_in_the_code_is_in_the_documents_block() -> None:
    text, block = parameter_block()
    assert block.pop("method") == method.METHOD
    constants = {name for name in vars(method) if name.isupper()} - NOT_PARAMETERS
    assert constants == {key.upper() for key in method.PARAMETERS}
    for key, value in method.PARAMETERS.items():
        assert getattr(method, key.upper()) == value, key
    assert {key: typed(value) for key, value in block.items()} == {
        key: typed(value) for key, value in method.PARAMETERS.items()
    }
    # Every number in the block is the owner's to confirm (owner decision 5).
    for line in text.splitlines():
        if not line.startswith("method:"):
            assert "# owner to confirm" in line, line


def test_the_parameters_are_the_numbers_the_statistics_use() -> None:
    """What the method pins is what the code that computes already uses (LP-10's statistics,
    the report's evidence floor, a shared report's smallest group)."""
    assert (method.LEVEL, method.RESAMPLES, method.SEED) == (stats.LEVEL, stats.RESAMPLES, 0)
    assert (method.SEED, method.MIN_POSITIVES) == (result.SEED, result.MIN_POSITIVES)
    for name in ("bootstrap", "paired_bootstrap", "bootstrap_defined"):
        defaults = inspect.signature(getattr(stats, name)).parameters
        assert (defaults["resamples"].default, defaults["seed"].default) == (
            method.RESAMPLES,
            method.SEED,
        )
        assert defaults["level"].default == method.LEVEL
    ece = inspect.signature(stats.equal_mass_ece).parameters
    assert (ece["bins"].default, ece["min_bin_size"].default) == (
        method.CALIBRATION_BINS,
        method.CALIBRATION_MIN_BIN,
    )
    assert method.SUBGROUP_MIN == share.MIN_GROUP
    # The floor and the ceiling are band edges, and each band list runs from best to worst.
    assert method.RECALL_FLOOR in method.RECALL_BANDS
    assert method.FALSE_AUTOMATION_CEILING in method.FALSE_AUTOMATION_BANDS
    assert list(method.RECALL_BANDS) == sorted(method.RECALL_BANDS, reverse=True)
    assert list(method.FALSE_AUTOMATION_BANDS) == sorted(method.FALSE_AUTOMATION_BANDS)
    assert list(method.COST_BANDS_USD_PER_1000) == sorted(method.COST_BANDS_USD_PER_1000)
    assert 1 <= method.MIN_REPLICATES <= method.MAX_REPLICATES
    assert 0 < method.SPLIT_FRACTION < 1
    # A submission key never outlives what every API key may (the method only narrows it).
    assert 1 <= method.SUBMISSION_KEY_MAX_DAYS <= audit.KEY_MAX_DAYS


def numbers_in(text: str) -> set[float]:
    """The numbers a text of the method uses, with what is not a number of the method taken
    out: headings and list markers, names that carry digits (LP-17d, SHA-256, Ed25519, B30,
    `mendari.ledger/1`, norm/v1), references to a section, an attestation, a replicate, an owner
    decision or a stage, and the citations' years and equations. A percentage is its share."""
    kept = []
    for line in text.splitlines():
        if line.startswith("#") or re.match(r"^\s*\| (?:Floor|0\.\d+) \|", line):
            continue  # a heading, or the evidence table (its own test recomputes it)
        kept.append(re.sub(r"^\s*\d+\. ", " ", line))
    body = "\n".join(kept)
    for name in (
        r"[\w.-]+/v?\d+(?:\.\d+)?",  # a format or a version: mendari.ledger/1, norm/v1
        r"\b[A-Za-z][A-Za-z_]*-?\d[\w-]*",  # LP-17d, ADR-0011, SHA-256, Ed25519, B30, p95
        r"\b(?:sections?|attestation|replicate|stage) \d+(?: (?:to|and) \d+)?",
        r"\bowner decisions? \d+(?: and \d+)?",
        r"\(\d{4}\)|\b(?:19|20)\d\d\b|\beq\. \d+\.\d+|\bdefinition \d+",
    ):
        body = re.sub(name, " ", body)
    found = set()
    for number in re.findall(r"(?<![\w.,])\d+(?:,\d{3})*(?:\.\d+)?%?(?!\w)", body):
        value = float(number.rstrip("%").replace(",", ""))
        found.add(round(value / 100 if number.endswith("%") else value, 10))
    return found


def test_the_text_uses_no_number_that_is_not_a_parameter_or_worked_out_from_one() -> None:
    """The parameter block holds every number the method sets, so the owner confirms each and the
    code pins it. Any other number in the text is worked out from those (section 8's minimums,
    the ceiling's 183, alpha/2), counts what the packs declare (L), or is the method's version or
    the unit of a figure. A rule with a number of its own fails here until it is a parameter."""
    _, block = parameter_block()
    block.pop("method")
    parameters = set()
    for value in block.values():
        for number in value if isinstance(value, list) else [value]:
            parameters.add(round(float(number), 10))
    ceiling = method.FALSE_AUTOMATION_CEILING
    least = next(n for n in itertools.count(1) if stats.clopper_pearson(0, n)[1] <= ceiling)
    worked_out = {
        *CRITICAL_LABELS.values(),  # L for each pack (Terms), also 1 - alpha/L and a rank
        *(method.min_positives(method.RECALL_FLOOR, count) for count in CRITICAL_LABELS.values()),
        least,  # the ceiling's minimum, 183, and the n below it, with their upper bounds
        least - 1,
        round(stats.clopper_pearson(0, least)[1], 5),
        round(stats.clopper_pearson(0, least - 1)[1], 5),
        method.ALPHA / 2,  # the n-of-n bound's tail
        float(method.METHOD.rsplit("/", 1)[1]),  # "method 1.0"
        1000,  # cost per 1,000 decisions: the unit of the cost bands
    }
    allowed = parameters | {round(float(number), 10) for number in worked_out}
    rules = TEXT.partition("\n## Parameters\n")[0]
    assert numbers_in(rules) - allowed == set()
    # The scan finds a number the block lacks: one rule with a number of its own fails.
    assert numbers_in("- a key lives for at most 400 days.") - allowed == {400}
    assert numbers_in("2. 95% of 1,000 (LP-17d, section 7), within 60 days.") - allowed == {60}


def test_the_method_has_a_version_an_effective_date_line_and_a_changelog() -> None:
    assert LINES[0] == "# Grading method 1.0"
    assert f"- **Version:** `{method.METHOD}`" in LINES
    date = "" if method.EFFECTIVE is None else f" {method.EFFECTIVE.isoformat()}"
    assert [line for line in LINES if line.startswith("- **Effective date:**")] == [
        f"- **Effective date:**{date}"
    ]
    status = "draft, not in effect" if method.EFFECTIVE is None else f"in effect from{date}"
    assert [line for line in LINES if line.startswith("- **Status:**")] == [
        f"- **Status:** {status}"
    ]
    assert re.search(r"^- \*\*1\.0 \(", section("Changelog"), flags=re.MULTILINE)
    # One section per requirement of the build prompt, 1 to 14, in order.
    numbered = re.findall(r"^## (\d+)\. ", TEXT, flags=re.MULTILINE)
    assert numbered == [str(number) for number in range(1, 15)]
    assert "30 days" in section("1. This document")
    assert '"engineering evaluation, not clinical validation"' in section("9. Scope statement")
    assert '!!! warning "Engineering evaluation, not clinical validation"' in LINES
    nav = yaml.safe_load((ROOT / "mkdocs.yml").read_text())["nav"]
    [grading] = [entry["Grading"] for entry in nav if "Grading" in entry]
    assert method.METHOD_PATH.removeprefix("docs/") in [p for e in grading for p in e.values()]


@pytest.mark.parametrize("labels", range(1, 9), ids=lambda labels: f"L={labels}")
def test_the_minimum_positives_reach_the_floor_with_a_perfect_result(labels: int) -> None:
    """At n_min a perfect result's simultaneous lower bound is at or above the floor; at
    n_min - 1 it is below it, or n_min is the 100 every critical label needs regardless.
    Mendari's bound, scipy's beta.ppf and statsmodels' proportion_confint(method="beta") agree,
    and n_min is what a search with scipy alone finds."""
    assert method.simultaneous_level(labels) == pytest.approx(1 - 0.05 / labels, abs=1e-15)
    for floor in FLOORS:
        n = method.min_positives(floor, labels)
        assert all(bound >= floor for bound in lower_bounds(n, labels)), (floor, n)
        if n > method.MIN_POSITIVES:
            assert all(bound < floor for bound in lower_bounds(n - 1, labels)), (floor, n)
        else:
            assert n == method.MIN_POSITIVES
        smallest = next(m for m in itertools.count(1) if scipy_lower(m, labels) >= floor)
        assert n == max(method.MIN_POSITIVES, smallest), (floor, labels)


def test_the_documents_evidence_table_is_what_the_code_gives() -> None:
    evidence = section("8. Evidence")
    header = re.search(r"^\s*\| Floor \|(.*)\|\s*$", evidence, flags=re.MULTILINE)
    assert header is not None
    labels = [int(cell.strip().removeprefix("L = ")) for cell in header.group(1).split("|")]
    assert labels == list(range(1, 9))
    rows = re.findall(r"^\s*\| (0\.\d+) \|(.*)\|\s*$", evidence, flags=re.MULTILINE)
    table = {float(floor): [int(cell) for cell in cells.split("|")] for floor, cells in rows}
    assert set(table) == {0.95, 0.97, 0.98, 0.99}
    for floor, row in table.items():
        assert row == [method.min_positives(floor, count) for count in labels], floor
    # The packs' critical labels, and what each needs at the floor of 0.95.
    for pack, count in CRITICAL_LABELS.items():
        manifest = yaml.safe_load(
            (
                ROOT / "tests" / "fixtures" / "reference-agents" / pack.split("/")[1] / "agent.yaml"
            ).read_text()
        )
        assert manifest["id"] == pack and len(manifest["evaluation"]["critical"]) == count
    assert [method.min_positives(0.95, count) for count in (1, 2, 4, 6)] == [100, 100, 100, 107]
    assert "Prescreener needs 107" in " ".join(evidence.split())
    # The false-automation ceiling's minimum: no wrong AUTO in 183 labelled cases, not 182.
    assert "n = 183" in evidence
    ceiling = method.FALSE_AUTOMATION_CEILING
    assert stats.clopper_pearson(0, 183, method.LEVEL)[1] <= ceiling
    assert stats.clopper_pearson(0, 182, method.LEVEL)[1] > ceiling
    upper = proportion_confint(0, 183, alpha=1 - method.LEVEL, method="beta")[1]
    assert upper == pytest.approx(stats.clopper_pearson(0, 183)[1], abs=1e-12)


def test_a_floor_or_a_label_count_out_of_range_is_refused() -> None:
    for floor in (0.0, 1.0, 1.5, -0.1):
        with pytest.raises(ValueError):
            method.min_positives(floor, 1)
    for labels in (0, -1):
        with pytest.raises(ValueError):
            method.simultaneous_level(labels)
    with pytest.raises(TypeError):
        method.simultaneous_level(True)
    with pytest.raises(TypeError):
        method.min_positives(0.95, 2.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        method.in_effect(datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC))


def test_nothing_is_in_effect_without_an_effective_date(monkeypatch: pytest.MonkeyPatch) -> None:
    assert method.EFFECTIVE is None, "the owner sets the effective date, never a build session"
    for day in (datetime.date(2026, 10, 17), datetime.date(2100, 1, 1), datetime.date.max):
        assert method.in_effect(day) is False
    assert "- **Status:** draft, not in effect" in LINES
    monkeypatch.setattr(method, "EFFECTIVE", datetime.date(2026, 12, 1))
    assert method.in_effect(datetime.date(2026, 11, 30)) is False
    assert method.in_effect(datetime.date(2026, 12, 1)) is True
