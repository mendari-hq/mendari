# SPDX-License-Identifier: Apache-2.0
"""The grading method's pinned constants (LP-17a): `mendari.grading/1.0`, a draft not in effect.

The method is a document, `docs/grading/method-1.0.md`; this module pins its version, the SHA-256
of its bytes and its parameters, so the code that grades (from LP-17d on) cites these and no
number of its own. Nothing imports this module yet.

The document is not in the wheel or the sdist (pyproject.toml ships `src/`, `packs/` and
`datasets/`; the documentation site is built from the repository), so an installed package cannot
read it. Its hash is therefore pinned here, never computed at run time, and
tests/test_grading_method.py checks it against the repository's file, and checks every parameter
here against the document's parameter block. Any edit to the document re-pins the hash; a result
made under another hash is stale (the method's section 12).

`EFFECTIVE` is the owner's to set, with the document's effective date: until then the method is a
draft and `in_effect` is false whatever the date. No function here reads a clock.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Mapping
from types import MappingProxyType

from mendari.evals import stats

METHOD = "mendari.grading/1.0"
METHOD_PATH = "docs/grading/method-1.0.md"  # from the repository root; not in the wheel
METHOD_SHA256 = "59d28c87024baad14e395b1b3329fec6a4d7685404789b05e93f688b3c3be40a"
EFFECTIVE: datetime.date | None = None  # the owner's: None is a draft, not in effect

# The parameters (the document's "Parameters" block, each the owner's to confirm).
LEVEL = 0.95  # every interval shown
ALPHA = 0.05  # the simultaneous level (1 - ALPHA / L) and Holm
RESAMPLES = 2000  # every bootstrap
SEED = 0  # every bootstrap
MIN_POSITIVES = 100  # distinct positives per critical label, at least
RECALL_FLOOR = 0.95  # on each critical label's simultaneous lower bound
FALSE_AUTOMATION_CEILING = 0.02  # on false automation's 95% upper bound
RECALL_BANDS = (0.99, 0.98, 0.97, 0.95, 0.90)  # higher is better
FALSE_AUTOMATION_BANDS = (0.005, 0.01, 0.02, 0.05)  # lower is better
ACCURACY_BAND_STEP = 0.05  # on accuracy's 95% lower bound
BRIER_BAND_STEP = 0.05  # on Brier's 95% upper bound
COST_BANDS_USD_PER_1000 = (0, 1, 10, 100)  # lower is better
STALE_AFTER_DAYS = 45
MIN_REPLICATES = 3  # a remote configuration (the method's Terms)
MAX_REPLICATES = 5  # a remote configuration
CANARY_SIZE = 30  # cases drawn from a pack's `test` set
SPLIT_FRACTION = 0.5  # the selection part's share of a frozen set (the developer flow)
NOTICE_DAYS = 30  # before a change takes effect (B30)
CALIBRATION_BINS = 10  # equal-mass bins (ECE and Brier reliability)
CALIBRATION_MIN_BIN = 30  # cases per bin, at least
SUBGROUP_MIN = 10  # cases per subgroup cell, at least
SUBMISSION_KEY_MAX_DAYS = 365  # a submission key's lifetime (the developer flow), at most

# The same values under the names the document's block uses.
PARAMETERS: Mapping[str, object] = MappingProxyType(
    {
        "level": LEVEL,
        "alpha": ALPHA,
        "resamples": RESAMPLES,
        "seed": SEED,
        "min_positives": MIN_POSITIVES,
        "recall_floor": RECALL_FLOOR,
        "false_automation_ceiling": FALSE_AUTOMATION_CEILING,
        "recall_bands": RECALL_BANDS,
        "false_automation_bands": FALSE_AUTOMATION_BANDS,
        "accuracy_band_step": ACCURACY_BAND_STEP,
        "brier_band_step": BRIER_BAND_STEP,
        "cost_bands_usd_per_1000": COST_BANDS_USD_PER_1000,
        "stale_after_days": STALE_AFTER_DAYS,
        "min_replicates": MIN_REPLICATES,
        "max_replicates": MAX_REPLICATES,
        "canary_size": CANARY_SIZE,
        "split_fraction": SPLIT_FRACTION,
        "notice_days": NOTICE_DAYS,
        "calibration_bins": CALIBRATION_BINS,
        "calibration_min_bin": CALIBRATION_MIN_BIN,
        "subgroup_min": SUBGROUP_MIN,
        "submission_key_max_days": SUBMISSION_KEY_MAX_DAYS,
    }
)


def _labels(labels: int) -> int:
    if isinstance(labels, bool) or not isinstance(labels, int):
        raise TypeError("labels must be an integer")
    if labels < 1:
        raise ValueError("an arm has at least one critical label")
    return labels


def simultaneous_level(labels: int) -> float:
    """The level of each of an arm's critical-label intervals for the grade's bound: 1 - ALPHA/L,
    so that its L intervals hold together with probability at least 1 - ALPHA (Bonferroni). The
    intervals are two-sided, as the method writes them: each lower bound is one-sided at
    1 - ALPHA/(2L)."""
    return 1.0 - ALPHA / _labels(labels)


def min_positives(floor: float, labels: int) -> int:
    """n_min(floor, L): the distinct positives a critical label needs, for an arm with L critical
    labels, before an attestation of its recall floor can be met. The smallest n whose n-of-n
    lower bound at `simultaneous_level(L)` (`stats.clopper_pearson`) is at or above `floor`, and
    never fewer than MIN_POSITIVES: on fewer, even a perfect result is insufficient evidence."""
    if isinstance(floor, bool) or not isinstance(floor, int | float):
        raise TypeError("floor must be a number")
    if not 0.0 < floor < 1.0:
        raise ValueError("floor must be in (0, 1)")
    level = simultaneous_level(labels)
    # The n-of-n lower bound is ((1 - level) / 2) ** (1 / n), so n is about log(tail) / log(floor).
    # Start from there and step, so the answer is the bound `stats` computes, not this estimate.
    n = max(1, math.floor(math.log((1.0 - level) / 2.0) / math.log(floor)))
    while stats.clopper_pearson(n, n, level)[0] < floor:
        n += 1
    while n > 1 and stats.clopper_pearson(n - 1, n - 1, level)[0] >= floor:
        n -= 1
    return max(MIN_POSITIVES, n)


def in_effect(today: datetime.date) -> bool:
    """Whether the method is in effect on `today` (the caller's date: nothing here reads a
    clock). Never before the owner sets EFFECTIVE."""
    if isinstance(today, datetime.datetime) or not isinstance(today, datetime.date):
        raise TypeError("today must be a date")
    return EFFECTIVE is not None and today >= EFFECTIVE
