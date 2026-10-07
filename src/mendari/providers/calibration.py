# SPDX-License-Identifier: Apache-2.0
"""Temperature scaling: one scalar that makes a model's probabilities honest.

A temperature T rescales scores before the softmax (p ∝ exp(score / T)): T > 1 softens an
over-confident model and T < 1 sharpens an under-confident one, without changing which label
wins. T is fitted on a held-out calibration split by minimising the negative log-likelihood
over a log-spaced grid, which needs no optimiser and cannot diverge.

Naive Bayes calibrates its log-posteriors this way; providers that report probabilities
(such as LLMs) calibrate their log-probabilities with `rescale`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from mendari.providers.base import softmax

Scored = Sequence[tuple[Mapping[str, float], str]]  # (scores per label, true label)

FLOOR = 1e-12


def temperature_grid(low: float = 0.25, high: float = 200.0, steps: int = 400) -> list[float]:
    return [
        math.exp(math.log(low) + (math.log(high) - math.log(low)) * i / (steps - 1))
        for i in range(steps)
    ]


def nll(scored: Scored, temperature: float) -> float:
    """Mean negative log-likelihood of the true labels at this temperature."""
    total = 0.0
    for scores, truth in scored:
        total -= math.log(max(softmax(scores, temperature)[truth], FLOOR))
    return total / len(scored)


def fit_temperature(scored: Scored) -> tuple[float, float, float]:
    """(temperature, nll at T=1, nll at the fitted T) on a calibration split."""
    if not scored:
        raise ValueError("calibration needs at least one labelled example")
    temperature = min(temperature_grid(), key=lambda t: nll(scored, t))
    return temperature, nll(scored, 1.0), nll(scored, temperature)


def log_probabilities(probabilities: Mapping[str, float]) -> dict[str, float]:
    return {label: math.log(max(p, FLOOR)) for label, p in probabilities.items()}


def rescale(probabilities: Mapping[str, float], temperature: float) -> dict[str, float]:
    """Apply a fitted temperature to a probability distribution."""
    return softmax(log_probabilities(probabilities), temperature)
