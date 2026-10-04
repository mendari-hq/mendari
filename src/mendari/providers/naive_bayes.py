"""A small, dependency-free multinomial naive Bayes text classifier with temperature scaling.

It exists so that every pack can ship a local, inspectable, reproducible statistical baseline:
no network, no credentials, no PHI leaving the machine. Naive Bayes is famously overconfident,
so training always fits a temperature on a held-out calibration split; the fitted value and the
negative log-likelihood before/after calibration are stored in the model file as provenance.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from mendari.errors import PackValidationError
from mendari.providers.base import Provider, ProviderOutput, fingerprint, softmax, text_from
from mendari.providers.calibration import fit_temperature

FORMAT = "mendari.naive_bayes/1"
_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)?")


def tokenize(text: str) -> list[str]:
    words = _TOKEN.findall(text.lower())
    return words + [f"{a} {b}" for a, b in itertools.pairwise(words)]


class NaiveBayesProvider(Provider):
    type = "naive_bayes"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        relative = self.config.get("model_file")
        if not isinstance(relative, str) or len(self.questions) != 1:
            raise PackValidationError(
                self.pack.id,
                [f"{self.info.slot}: naive_bayes needs `model_file` and exactly one question"],
            )

        model = self.pack.read_json(relative)
        (self.question,) = self.questions
        problems = []
        if not isinstance(model, dict):
            problems.append(f"{relative}: not a naive_bayes model (expected a mapping)")
        elif model.get("format") != FORMAT:
            problems.append(f"{relative}: expected format {FORMAT}")
        elif (shape := _shape_problem(model)) is not None:
            problems.append(f"{relative}: not a naive_bayes model ({shape})")
        elif model["question"] != self.question:
            problems.append(f"{relative}: trained for {model['question']!r}, not {self.question!r}")
        elif set(model["labels"]) != set(self.questions[self.question].criteria):
            problems.append(f"{relative}: labels do not match the question's labels")
        elif self.config.get("text_fields", model["text_fields"]) != model["text_fields"]:
            problems.append(f"{relative}: trained on different text_fields; retrain the model")
        if problems:
            raise PackValidationError(self.pack.id, problems)

        self.info = replace(self.info, model=f"naive_bayes@{fingerprint(self.pack, relative)}")
        self.model = model
        self.labels: list[str] = model["labels"]
        self.text_fields: list[str] = model["text_fields"]
        self.temperature: float = model["temperature"]
        self._log_prior: list[float] = model["log_prior"]
        self._tokens: dict[str, list[float]] = model["tokens"]

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        text = text_from(inputs, self.text_fields)
        logits = log_posterior(self.labels, self._log_prior, self._tokens, tokenize(text))
        probabilities = softmax(logits, self.temperature)
        return ProviderOutput(
            answers={self.question: self.make_answer(self.question, probabilities)}
        )


def _shape_problem(model: dict[str, Any]) -> str | None:
    """The first field of a model file that is not what `answer` reads, or None."""
    labels = model.get("labels")

    def numbers(value: Any) -> bool:
        return (
            isinstance(value, list)
            and isinstance(labels, list)
            and len(value) == len(labels)
            and all(isinstance(x, int | float) and not isinstance(x, bool) for x in value)
        )

    temperature = model.get("temperature")
    tokens = model.get("tokens")
    checks = {
        "question": isinstance(model.get("question"), str),
        "labels": isinstance(labels, list) and all(isinstance(x, str) for x in labels),
        "text_fields": isinstance(model.get("text_fields"), list)
        and all(isinstance(x, str) for x in model["text_fields"]),
        "temperature": isinstance(temperature, int | float)
        and not isinstance(temperature, bool)
        and temperature > 0,
        "log_prior": numbers(model.get("log_prior")),
        "tokens": isinstance(tokens, dict) and all(numbers(v) for v in tokens.values()),
    }
    return next((name for name, ok in checks.items() if not ok), None)


def log_posterior(
    labels: Sequence[str],
    log_prior: Sequence[float],
    tokens: Mapping[str, Sequence[float]],
    words: Iterable[str],
) -> dict[str, float]:
    scores = list(log_prior)
    for word in words:
        likelihoods = tokens.get(word)
        if likelihoods is not None:
            for index, value in enumerate(likelihoods):
                scores[index] += value
    return dict(zip(labels, scores, strict=True))


def train(
    records: Sequence[Mapping[str, Any]],
    *,
    question: str,
    labels: Sequence[str],
    text_fields: Sequence[str],
    calibration: Sequence[Mapping[str, Any]],
    alpha: float = 1.0,
    min_count: int = 2,
) -> dict[str, Any]:
    """Fit on `records` and calibrate on the disjoint `calibration` split.

    Each record looks like {"input": {...}, "labels": {question: label, ...}}.
    """
    if not records or not calibration:
        raise ValueError("training and calibration splits must both be non-empty")
    index = {label: i for i, label in enumerate(labels)}
    doc_counts = [0] * len(labels)
    token_counts: dict[str, list[int]] = {}
    for record in records:
        label_index = index[record["labels"][question]]
        doc_counts[label_index] += 1
        for token in tokenize(text_from(record["input"], list(text_fields))):
            token_counts.setdefault(token, [0] * len(labels))[label_index] += 1

    vocabulary = {t: c for t, c in token_counts.items() if sum(c) >= min_count}
    totals = [sum(counts[i] for counts in vocabulary.values()) for i in range(len(labels))]
    size = len(vocabulary)
    log_prior = [
        round(math.log((doc_counts[i] + alpha) / (len(records) + alpha * len(labels))), 6)
        for i in range(len(labels))
    ]
    tokens = {
        token: [
            round(math.log((counts[i] + alpha) / (totals[i] + alpha * size)), 5)
            for i in range(len(labels))
        ]
        for token, counts in sorted(vocabulary.items())
    }

    logits = [
        (
            log_posterior(
                labels, log_prior, tokens, tokenize(text_from(r["input"], list(text_fields)))
            ),
            r["labels"][question],
        )
        for r in calibration
    ]
    temperature, nll_before, nll_after = fit_temperature(logits)
    return {
        "format": FORMAT,
        "question": question,
        "labels": list(labels),
        "text_fields": list(text_fields),
        "temperature": round(temperature, 4),
        "log_prior": log_prior,
        "training": {
            "examples": len(records),
            "dataset_sha256": _records_digest(records),
            "alpha": alpha,
            "min_count": min_count,
            "vocabulary": size,
            "calibration": {
                "examples": len(calibration),
                "dataset_sha256": _records_digest(calibration),
                "nll_before": round(nll_before, 4),
                "nll_after": round(nll_after, 4),
            },
        },
        "tokens": tokens,
    }


def _records_digest(records: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(json.dumps(record, sort_keys=True).encode())
    return digest.hexdigest()
