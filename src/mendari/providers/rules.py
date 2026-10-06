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

from mendari.core.caps import rules_problems
from mendari.core.origin import MAX_COMMUNITY_TEXT_CHARS
from mendari.core.patterns import RULES_FLAGS, Matcher
from mendari.core.patterns import compile as compile_pattern
from mendari.errors import PackValidationError
from mendari.providers.base import Provider, ProviderOutput, fingerprint, softmax, text_from

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
        relative = self.config.get("config")
        if not isinstance(relative, str):
            raise PackValidationError(self.pack.id, [f"{self.info.slot}: rules need `config`"])
        try:
            self.rules = RulesConfig.model_validate(self.pack.read_yaml(relative))
        except ValidationError as error:
            raise PackValidationError(self.pack.id, [f"{relative}: {error}"]) from None
        self.info = replace(self.info, model=f"rules@{fingerprint(self.pack, relative)}")

        # A community pack's caps (`core/caps.py`; `load_pack` checks them too, as written).
        problems = rules_problems(relative, self.rules.model_dump()) if self.pack.community else []
        for question in self.questions:
            spec = self.rules.questions.get(question)
            if spec is None:
                problems.append(f"{relative}: no rules for question {question!r}")
                continue
            labels = set(self.questions[question].criteria)
            unknown = ({spec.default} | set(spec.labels)) - labels
            if unknown:
                problems.append(f"{relative}: unknown labels {sorted(unknown)} for {question!r}")
        if problems:
            raise PackValidationError(self.pack.id, problems)

        # The pack's own patterns, on its engine (`core/patterns.py`); the negation pattern
        # below is Mendari's, built from escaped cues, and stays on `re`.
        community = self.pack.community
        self._compiled = {
            question: {
                label: [
                    (p, compile_pattern(p.pattern, RULES_FLAGS, community=community))
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
        # A community pack's patterns were timed on texts of at most this length
        # (`core/origin.py`): a correction that would make the text longer is not made.
        room = [MAX_COMMUNITY_TEXT_CHARS - len(text)] if self.pack.community else None

        def fix(match: re.Match[str]) -> str:
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
