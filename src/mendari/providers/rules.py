# SPDX-License-Identifier: Apache-2.0
"""Deterministic, auditable pattern rules.

Each label accumulates the weights of the patterns that match; a softmax over the scores gives
a probability distribution, so a message that matches nothing yields a *low-confidence* default
rather than a confident guess, and the policy can abstain honestly.

Optional typo tolerance normalises a word that is one edit (insertion, deletion, substitution or
adjacent transposition) away from a keyword used in the patterns, provided both start with the
same letter. Corrections are reported as evidence, so the audit trail shows "chset -> chest".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.core.origin import MAX_COMMUNITY_TEXT_CHARS
from mendari.core.pack import Pack
from mendari.core.patterns import RULES_FLAGS, Matcher, OutOfTime, check_time
from mendari.core.patterns import compile as compile_pattern
from mendari.core.spec import Question
from mendari.errors import PackValidationError, ProviderError
from mendari.providers.base import (
    FileConfig,
    Provider,
    ProviderOutput,
    closed_problems,
    fingerprint,
    read_problems,
    softmax,
    text_from,
)

_SCOPE_BREAK = re.compile(r"[.!?;\n]|\b(?:but|however|pero)\b", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_]+")
_REGEX_ESCAPE = re.compile(r"\\[A-Za-z]")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Pattern(_Model):
    name: str
    pattern: str
    weight: float = Field(gt=0)
    negatable: bool = False


class QuestionRules(_Model):
    default: str
    default_score: float = 1.0
    labels: dict[str, list[Pattern]]
    # Which input fields this question reads, if not the provider's `text_fields` (e.g. the
    # patient's question for one check and the model's draft for the others).
    text_fields: list[str] | None = Field(default=None, min_length=1)


class TypoTolerance(_Model):
    min_length: int = Field(default=5, ge=4)


class RulesConfig(_Model):
    text_fields: list[str] = Field(min_length=1)
    temperature: float = Field(default=1.0, gt=0)
    negation_cues: list[str] = []
    negation_window: int = Field(default=40, ge=0)
    typo_tolerance: TypoTolerance | None = None
    questions: dict[str, QuestionRules]


class RulesProvider(Provider):
    type = "rules"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        rules, problems = self._read(self.pack, self.info.slot, self.config)
        if rules is None:
            raise PackValidationError(self.pack.id, problems)
        self.rules = rules
        relative = self.config["config"]
        self.info = replace(self.info, model=f"rules@{fingerprint(self.pack, relative)}")
        problems = self._rules_problems(relative, rules, self.questions, bounded=self.pack.bounded)
        if problems:
            raise PackValidationError(self.pack.id, problems)

        # The pack's own patterns, on its engine (`core/patterns.py`); the negation pattern
        # below is Mendari's, built from escaped cues, and stays on `re`.
        bounded = self.pack.bounded
        self._compiled = {
            question: {
                label: [
                    (p, compile_pattern(p.pattern, RULES_FLAGS, community=bounded))
                    for p in patterns
                ]
                for label, patterns in self.rules.questions[question].labels.items()
            }
            for question in self.questions
        }
        cues = "|".join(re.escape(cue) for cue in self.rules.negation_cues)
        self._negation = re.compile(rf"\b(?:{cues})\b", re.IGNORECASE) if cues else None
        tolerance = self.rules.typo_tolerance
        self._typo_min = tolerance.min_length if tolerance else 0
        self._keywords = self._keyword_index(self._typo_min) if tolerance else {}

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
        closed = closed_problems(FileConfig, where, config)
        if closed:
            return closed
        rules, problems = cls._read(pack, where, config)
        if rules is None:
            return problems
        return cls._rules_problems(config["config"], rules, questions, bounded=bounded)

    @staticmethod
    def _read(
        pack: Pack, where: str, config: Mapping[str, Any]
    ) -> tuple[RulesConfig | None, list[str]]:
        """The rules file `config` names, read and checked against `RulesConfig`, or None and
        why not."""
        relative = config.get("config")
        if not isinstance(relative, str):
            return None, [f"{where}: rules need `config`"]
        data, problems = read_problems(lambda: pack.read_yaml(relative))
        if problems:
            return None, problems
        try:
            return RulesConfig.model_validate(data), []
        except ValidationError as error:
            return None, [f"{relative}: {error}"]

    @staticmethod
    def _rules_problems(
        relative: str, rules: RulesConfig, questions: Mapping[str, Question], *, bounded: bool
    ) -> list[str]:
        """The caps of a pack on the community engine (`bounded`; `core/caps.py`, imported only
        for one; `load_pack` checks them too, as written), and a rule for each question of the
        slot, with its labels."""
        problems: list[str] = []
        if bounded:
            from mendari.core.caps import rules_problems

            problems = rules_problems(relative, rules.model_dump())
        for question in questions:
            spec = rules.questions.get(question)
            if spec is None:
                problems.append(f"{relative}: no rules for question {question!r}")
                continue
            labels = set(questions[question].criteria)
            unknown = ({spec.default} | set(spec.labels)) - labels
            if unknown:
                problems.append(f"{relative}: unknown labels {sorted(unknown)} for {question!r}")
        return problems

    def _keyword_index(self, minimum: int) -> dict[str, tuple[str, ...]]:
        """Literal words used in this provider's patterns, indexed by first letter."""
        words: set[str] = set()
        for question in self.questions:
            for patterns in self.rules.questions[question].labels.values():
                for pattern in patterns:
                    literal = _REGEX_ESCAPE.sub(" ", pattern.pattern.lower())
                    words.update(w for w in _WORD.findall(literal) if len(w) >= minimum)
        index: dict[str, list[str]] = {}
        for word in sorted(words):
            index.setdefault(word[0], []).append(word)
        return {letter: tuple(group) for letter, group in index.items()}

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        try:
            return self._answer(inputs)
        except OutOfTime as error:  # a bounded pack's patterns used this decision's budget
            raise ProviderError("time_budget", str(error)) from None

    def _answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        texts: dict[tuple[str, ...], tuple[str, list[str]]] = {}
        answers = {}
        for question, compiled in self._compiled.items():
            spec = self.rules.questions[question]
            fields = tuple(spec.text_fields or self.rules.text_fields)
            if fields not in texts:
                texts[fields] = self._normalize(text_from(inputs, list(fields)))
            text, corrections = texts[fields]
            scores = dict.fromkeys(self.questions[question].criteria, 0.0)
            scores[spec.default] += spec.default_score
            evidence = []
            for label, patterns in compiled.items():
                for pattern, regex in patterns:
                    if self._matches(regex, text, pattern.negatable):
                        scores[label] += pattern.weight
                        evidence.append(f"{label}:{pattern.name}")
            probabilities = softmax(scores, self.rules.temperature)
            answers[question] = self.make_answer(
                question, probabilities, tuple(evidence + corrections)
            )
        return ProviderOutput(answers=answers)

    def _normalize(self, text: str) -> tuple[str, list[str]]:
        if not self._keywords:
            return text, []
        corrections: list[str] = []
        # The patterns of a pack on the community engine (`Pack.bounded`) run on texts of at
        # most this length (`core/origin.py`; the caps on their work are measured on it): a
        # correction that would make the text longer is not made.
        bounded = self.pack.bounded
        room = [MAX_COMMUNITY_TEXT_CHARS - len(text)] if bounded else None
        timed = bounded  # each word is compared with many keywords

        def fix(match: re.Match[str]) -> str:
            if timed:
                check_time()
            word = match.group(0)
            lower = word.lower()
            candidates = self._keywords.get(lower[0], ())
            if len(lower) < self._typo_min or lower in candidates:
                return word
            for keyword in candidates:
                if within_one_edit(lower, keyword):
                    if room is not None:
                        if len(keyword) - len(word) > room[0]:
                            return word
                        room[0] -= len(keyword) - len(word)
                    corrections.append(f"typo:{lower}->{keyword}")
                    return keyword
            return word

        return _WORD.sub(fix, text), corrections

    def _matches(self, regex: Matcher, text: str, negatable: bool) -> bool:
        for match in regex.finditer(text):
            if not (negatable and self._negated(text, match.start())):
                return True
        return False

    def _negated(self, text: str, start: int) -> bool:
        if self._negation is None:
            return False
        window = text[max(0, start - self.rules.negation_window) : start]
        # A sentence boundary or contrast ends a cue's scope ("No fever, but chest pain now").
        window = _SCOPE_BREAK.split(window)[-1]
        return bool(self._negation.search(window))


def within_one_edit(a: str, b: str) -> bool:
    """Optimal-string-alignment distance <= 1 (one insert, delete, substitute or swap)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
        if len(diffs) == 1:
            return True
        i = diffs[0]
        return len(diffs) == 2 and diffs[1] == i + 1 and a[i] == b[i + 1] and a[i + 1] == b[i]
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(shorter) and shorter[i] == longer[i]:
        i += 1
    return shorter[i:] == longer[i + 1 :]
