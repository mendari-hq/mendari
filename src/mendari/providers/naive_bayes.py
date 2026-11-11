# SPDX-License-Identifier: Apache-2.0
"""A small, dependency-free multinomial naive Bayes text classifier with temperature scaling.

It exists so that every pack can ship a local, inspectable, reproducible statistical baseline:
no network, no credentials, no PHI leaving the machine. Naive Bayes is famously overconfident,
so training always fits a temperature on a held-out calibration split; the fitted value and the
negative log-likelihood before/after calibration are stored in the model file as provenance.

Its features are a text's words and pairs of neighbouring words. A model file may add the runs
of characters inside each word (`character_ngrams`, the shortest and longest run): a misspelt or
unseen word is then read by the parts it shares with the words the model learned. Such a file
says so and has a format of its own (`FORMAT_CHARACTERS`), so an engine that does not know the
features refuses it instead of scoring it without them. Measured on the inbox router, trained on
the pack's own data and read on the harder draft's held-out messages: 322 of 436 right with
words alone, 353 with runs of 3 to 5 characters, and 6 confident mistakes (at 0.90 or more) for
12. On the pack's own test it is the same within one case, and a little less sure. A model
without the key is trained and scored as it always was, byte for byte.
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

from pydantic import BaseModel, ConfigDict

from mendari.core.pack import Pack
from mendari.core.spec import Question
from mendari.errors import PackValidationError
from mendari.providers.base import (
    Provider,
    ProviderOutput,
    closed_problems,
    fingerprint,
    once,
    read_problems,
    softmax,
    text_from,
)
from mendari.providers.calibration import fit_temperature

FORMAT = "mendari.naive_bayes/1"
FORMAT_CHARACTERS = "mendari.naive_bayes/2"  # a model with `character_ngrams`
# The shortest and longest run of characters a model may count.
MIN_NGRAM = 2
MAX_NGRAM = 8
# A longer "word" (a link, an encoded attachment) is counted as a word and has no runs counted:
# each character of a word gives up to seven of them.
MAX_NGRAM_WORD = 64
CharacterNgrams = tuple[int, int]  # the shortest and the longest run, both counted
_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)?")


class NaiveBayesConfig(BaseModel):
    """A `naive_bayes` variant: the trained model file, and the text fields it reads, which must
    be those it was trained on (by default, the model's own)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    model_file: str
    text_fields: list[str] | None = None


def tokenize(text: str, character_ngrams: CharacterNgrams | None = None) -> list[str]:
    """A text's features: its words, each pair of neighbouring words and, with
    `character_ngrams`, every run of that many characters inside each word of at most
    `MAX_NGRAM_WORD` characters, the word held between its edges (`<` and `>`) and each run
    marked `#`, which no word holds."""
    words = _TOKEN.findall(text.lower())
    tokens = words + [f"{a} {b}" for a, b in itertools.pairwise(words)]
    if character_ngrams is not None:
        shortest, longest = character_ngrams
        for word in words:
            if len(word) > MAX_NGRAM_WORD:
                continue
            edged = f"<{word}>"
            tokens += [
                f"#{edged[start : start + size]}"
                for size in range(shortest, longest + 1)
                for start in range(len(edged) - size + 1)
            ]
    return tokens


def ngrams_problem(value: Any) -> bool:
    """Whether `value` is not a `character_ngrams`: two whole numbers, the shortest run and the
    longest, from `MIN_NGRAM` to `MAX_NGRAM`."""
    return not (
        isinstance(value, list | tuple)
        and len(value) == 2
        and all(isinstance(size, int) and not isinstance(size, bool) for size in value)
        and MIN_NGRAM <= value[0] <= value[1] <= MAX_NGRAM
    )


class NaiveBayesProvider(Provider):
    type = "naive_bayes"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        model, problems = self._checked(
            self.pack, self.info.slot, self.config, self.questions, trained=True
        )
        if model is None:
            raise PackValidationError(self.pack.id, problems)
        (self.question,) = self.questions
        relative = self.config["model_file"]
        self.info = replace(self.info, model=f"naive_bayes@{fingerprint(self.pack, relative)}")
        self.model = model
        self.labels: list[str] = model["labels"]
        self.text_fields: list[str] = model["text_fields"]
        self.temperature: float = model["temperature"]
        features = model.get("character_ngrams")
        self._character_ngrams: CharacterNgrams | None = (
            None if features is None else (features[0], features[1])
        )
        self._log_prior: list[float] = model["log_prior"]
        self._tokens: dict[str, list[float]] = model["tokens"]

    @classmethod
    def config_problems(
        cls,
        pack: Pack,
        where: str,
        config: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        bounded: bool,
        trained: bool,
    ) -> list[str]:
        return (
            closed_problems(NaiveBayesConfig, where, config)
            or cls._checked(pack, where, config, questions, trained=trained)[1]
        )

    @staticmethod
    def _checked(
        pack: Pack,
        where: str,
        config: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        trained: bool,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """The model `model_file` names, read and checked against the slot's one question, or
        None and why not (unless `trained`, it is not read: None and no problem)."""
        relative = config.get("model_file")
        if not isinstance(relative, str) or len(questions) != 1:
            return None, [f"{where}: naive_bayes needs `model_file` and exactly one question"]
        if not trained:
            return None, []
        # Read and checked for its shape once per `providers.config_problems` call (`once`).
        model, unreadable = once((NaiveBayesProvider.type, relative), lambda: _read(pack, relative))
        if unreadable:
            return None, list(unreadable)
        (question,) = questions
        problems = []
        if model["question"] != question:
            problems.append(f"{relative}: trained for {model['question']!r}, not {question!r}")
        elif set(model["labels"]) != set(questions[question].criteria):
            problems.append(f"{relative}: labels do not match the question's labels")
        elif config.get("text_fields", model["text_fields"]) != model["text_fields"]:
            problems.append(f"{relative}: trained on different text_fields; retrain the model")
        return (None, problems) if problems else (model, [])

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        words = tokenize(text_from(inputs, self.text_fields), self._character_ngrams)
        logits = log_posterior(self.labels, self._log_prior, self._tokens, words)
        probabilities = softmax(logits, self.temperature)
        return ProviderOutput(
            answers={self.question: self.make_answer(self.question, probabilities)}
        )


def _read(pack: Pack, relative: str) -> tuple[Any, list[str]]:
    """The naive_bayes model at `relative`, and nothing wrong with it; or why it is not one (it
    cannot be read, is not JSON, or is not of the format and shape `answer` reads)."""
    model, problems = read_problems(lambda: pack.read_json(relative))
    if problems:
        return None, problems
    if not isinstance(model, dict):
        return None, [f"{relative}: not a naive_bayes model (expected a mapping)"]
    if model.get("format") not in (FORMAT, FORMAT_CHARACTERS):
        return None, [f"{relative}: expected format {FORMAT}"]
    if (shape := _shape_problem(model)) is not None:
        return None, [f"{relative}: not a naive_bayes model ({shape})"]
    return model, []


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
        # A model that counts runs of characters says so in its format, and no other does.
        "character_ngrams": (
            not ngrams_problem(model.get("character_ngrams"))
            if model["format"] == FORMAT_CHARACTERS
            else "character_ngrams" not in model
        ),
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
    character_ngrams: CharacterNgrams | None = None,
) -> dict[str, Any]:
    """Fit on `records` and calibrate on the disjoint `calibration` split.

    Each record looks like {"input": {...}, "labels": {question: label, ...}}. With
    `character_ngrams` the model also counts those runs of characters (`tokenize`), says so, and
    is of `FORMAT_CHARACTERS`; without, it is the model this always trained.
    """
    if not records or not calibration:
        raise ValueError("training and calibration splits must both be non-empty")
    if character_ngrams is not None and ngrams_problem(character_ngrams):
        raise ValueError(
            f"character_ngrams is the shortest and longest run, {MIN_NGRAM} to {MAX_NGRAM}"
        )
    index = {label: i for i, label in enumerate(labels)}
    doc_counts = [0] * len(labels)
    token_counts: dict[str, list[int]] = {}
    for record in records:
        label_index = index[record["labels"][question]]
        doc_counts[label_index] += 1
        for token in tokenize(text_from(record["input"], list(text_fields)), character_ngrams):
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
                labels,
                log_prior,
                tokens,
                tokenize(text_from(r["input"], list(text_fields)), character_ngrams),
            ),
            r["labels"][question],
        )
        for r in calibration
    ]
    temperature, nll_before, nll_after = fit_temperature(logits)
    features: dict[str, Any] = (
        {} if character_ngrams is None else {"character_ngrams": list(character_ngrams)}
    )
    return {
        "format": FORMAT_CHARACTERS if features else FORMAT,
        "question": question,
        "labels": list(labels),
        "text_fields": list(text_fields),
        **features,
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
