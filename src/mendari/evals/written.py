# SPDX-License-Identifier: Apache-2.0
"""Written output (ADR-0025): what an agent wrote, graded by clinicians against a rubric and by a
model judge against the same rubric, and how far the judge is to be believed.

The floors below are the engine's. A manifest may raise `agreement` and `must_pass_recall`, never
lower them (`core/spec.py`), and every report repeats the four it was decided on."""

from __future__ import annotations

import math

from mendari.evals import stats

# The judge's weighted kappa with clinicians: its interval's lower bound is at least this.
AGREEMENT_FLOOR = 0.60
# A must-not-fail criterion: of the outputs a clinician failed, the share the judge failed too;
# its exact lower bound is at least this.
MUST_PASS_RECALL_FLOOR = 0.80
# Fewer outputs graded by both than this is insufficient evidence, whatever the agreement.
MIN_PAIRED = 50
# So is fewer clinician verdicts than this on either side of the pass line.
MIN_EACH_SIDE = 10


def min_failures(floor: float) -> int:
    """The clinician-found failures a must-not-fail criterion needs before its floor can be met:
    the smallest n whose n-of-n lower bound (`stats.clopper_pearson`, at `stats.LEVEL`) is at or
    above `floor`; 17 for 0.80. On fewer, a judge that found every one is insufficient
    evidence. Written as `method.min_positives` is."""
    if isinstance(floor, bool) or not isinstance(floor, int | float):
        raise TypeError("floor must be a number")
    if not 0.0 < floor < 1.0:
        raise ValueError("floor must be in (0, 1)")
    # The n-of-n lower bound is ((1 - level) / 2) ** (1 / n), so n is about log(tail) / log(floor).
    # Start from there and step, so the answer is the bound `stats` computes, not this estimate.
    n = max(1, math.floor(math.log((1.0 - stats.LEVEL) / 2.0) / math.log(floor)))
    while stats.clopper_pearson(n, n, stats.LEVEL)[0] < floor:
        n += 1
    while n > 1 and stats.clopper_pearson(n - 1, n - 1, stats.LEVEL)[0] >= floor:
        n -= 1
    return n
