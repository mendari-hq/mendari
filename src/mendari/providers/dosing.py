# SPDX-License-Identifier: Apache-2.0
"""Dose arithmetic on free text: does a stated dose go above a medicine's daily maximum?

"Take 1000 mg of acetaminophen every 4 hours" means 6000 mg a day. Working that out is
arithmetic, which models (Jev included) are unreliable at, so this provider does it in code.
It reads text fields (e.g. an LLM's draft reply) sentence by sentence and, for each medicine
the pack lists, parses amounts and frequencies:

    medicines:
      acetaminophen:
        names: [acetaminophen, paracetamol, tylenol]
        max_daily_mg: 4000
        max_single_mg: 1000

- an amount: "500 mg", "1 g", "2 tablets of 500 mg", "two 500 mg tablets";
- a frequency: "every 4 hours" ("every 4-6 hours" counts the shorter interval), "3 times a
  day", "twice daily";
- or a daily total: "up to 3000 mg a day", "no more than 8 tablets of 500 mg a day".

The answer is `yes` when the largest daily amount a sentence allows is above the maximum (or a
single dose is above `max_single_mg`), `no` when every stated dose is within the limits or no
listed medicine is dosed, and `unknown` when a listed medicine has a dose whose daily amount
cannot be worked out. It answers exactly one yes/no/unknown question. Only what the text says
is checked: not the patient's weight, age, kidney function or other medicines.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.core.pack import Pack
from mendari.core.spec import Question
from mendari.errors import PackValidationError
from mendari.providers.base import (
    FileConfig,
    Provider,
    ProviderOutput,
    closed_problems,
    fingerprint,
    read_problems,
    text_from,
)

DECIDED = 0.98  # probability of a determined answer; the rest is shared evenly
_SENTENCE = re.compile(r"(?<!\d)[.!?;\n]+(?!\d)")  # not inside "2.5 mg"
_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twelve": 12,
    "once": 1,
    "twice": 2,
}
_N = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|twelve)"
_UNIT_MG = {
    "mg": 1.0,
    "milligram": 1.0,
    "milligrams": 1.0,
    "g": 1000.0,
    "gram": 1000.0,
    "grams": 1000.0,
    "mcg": 0.001,
    "microgram": 0.001,
    "micrograms": 0.001,
}
_FORMS = r"(?:tablets?|pills?|capsules?|caplets?|tabs?)"
_AMOUNT = re.compile(rf"\b{_N}\s*(mg|milligrams?|g|grams?|mcg|micrograms?)\b", re.IGNORECASE)
# A count just before an amount: "2 tablets of 500 mg", "2 Advil tablets of 200 mg",
# "2 x 500 mg", "two 500 mg tablets".
_COUNT_BEFORE = re.compile(
    rf"\b{_N}\s*(?:(?:[a-z]+\s+)?{_FORMS}\s*(?:of\s*)?|x\s*)$", re.IGNORECASE
)
_BARE_BEFORE = re.compile(rf"\b{_N}\s+$", re.IGNORECASE)
_FORM_AFTER = re.compile(
    rf"^\s*(?:[a-z]+\s+)?{_FORMS}\b", re.IGNORECASE
)  # "500 mg (Tylenol) tablets"
# A count just after an amount: "500 mg, 2 tablets every 6 hours".
_COUNT_AFTER = re.compile(rf"^\s*,?\s*(?:take\s+)?{_N}\s*{_FORMS}\b", re.IGNORECASE)
# A daily total just after an amount: "3000 mg a day", "500 mg tablets daily", "in 24 hours".
_DAILY = r"(?:(?:a|per|each)\s+day|daily|/\s*day|in\s+(?:a\s+day|24\s*hours))\b"
# Up to four words may sit between an amount and "a day" ("500 mg of Tylenol a day"), but not
# a frequency: "500 mg twice a day" is 500 mg twice, not 500 mg a day.
_DAILY_AFTER = re.compile(rf"^((?:\s*[\w,-]+){{0,4}}?)\s*{_DAILY}", re.IGNORECASE)
_FREQUENCY_WORD = re.compile(
    r"\b(?:every|times?|twice|once|hours?|hourly|q\d+h|bid|tid|qid)\b", re.IGNORECASE
)
# Counts without a strength: "2 tablets every 6 hours", "8 tablets a day" (needs tablet_mg).
_COUNT_ONLY = re.compile(rf"\b{_N}\s+(?:[a-z]+\s+)?{_FORMS}\b(?!\s*of\s*\d)", re.IGNORECASE)
_EVERY = re.compile(
    rf"\bevery\s+{_N}(?:\s*(?:-|to|or)\s*{_N})?\s*(?:hours?|hrs?|h)\b|\bq\s*(\d+)\s*h\b",
    re.IGNORECASE,
)
_EVERY_HOUR = re.compile(r"\bevery\s+hour\b|\bhourly\b", re.IGNORECASE)
_TIMES = re.compile(rf"\b{_N}\s*times\s*(?:(?:a|per|each)\s*day|daily)\b", re.IGNORECASE)
_SIMPLE = re.compile(r"\b(once|twice)\s*(?:a\s*day|daily)\b", re.IGNORECASE)
_ABBREVIATIONS = {"qd": 1, "bid": 2, "tid": 3, "qid": 4}
_ABBREVIATION = re.compile(r"\b(qd|bid|tid|qid)\b", re.IGNORECASE)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Medicine(_Model):
    names: list[str] = Field(min_length=1)  # generic and brand names, case-insensitive
    max_daily_mg: float = Field(gt=0)
    max_single_mg: float | None = Field(default=None, gt=0)
    tablet_mg: float | None = Field(default=None, gt=0)  # strength when only "N tablets" is said


class DosingConfig(_Model):
    text_fields: list[str] = Field(min_length=1)
    medicines: dict[str, Medicine] = Field(min_length=1)


@dataclass(frozen=True)
class Finding:
    label: str  # yes | no | unknown
    evidence: str


class DosingProvider(Provider):
    type = "dosing"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        table, problems = self._checked(
            self.pack, self.info.slot, self.config, self.questions, bounded=self.pack.bounded
        )
        if table is None:
            raise PackValidationError(self.pack.id, problems)
        self.table = table
        relative = self.config["config"]
        self._patterns = {
            name: re.compile(
                r"\b(?:" + "|".join(re.escape(a) for a in medicine.names) + r")\b", re.IGNORECASE
            )
            for name, medicine in self.table.medicines.items()
        }
        self.info = replace(self.info, model=f"dosing@{fingerprint(self.pack, relative)}")

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
            closed_problems(FileConfig, where, config)
            or cls._checked(pack, where, config, questions, bounded=bounded)[1]
        )

    @staticmethod
    def _checked(
        pack: Pack,
        where: str,
        config: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        bounded: bool,
    ) -> tuple[DosingConfig | None, list[str]]:
        """The dosing table `config` names, read and checked (a pack on the community engine's
        caps, `core/caps.py`, which `load_pack` checks too; one yes/no/unknown question), or
        None and why not."""
        relative = config.get("config")
        if not isinstance(relative, str):
            return None, [f"{where}: `config` is required"]
        data, problems = read_problems(lambda: pack.read_yaml(relative))
        if problems:
            return None, problems
        try:
            table = DosingConfig.model_validate(data)
        except ValidationError as error:
            return None, [f"{relative}: {error}"]
        if bounded:
            from mendari.core.caps import dosing_problems

            problems = dosing_problems(relative, table.model_dump())
            if problems:
                return None, problems
        if len(questions) != 1:
            return None, [f"{relative}: answers exactly one question"]
        ((key, question),) = questions.items()
        if not {"yes", "no", "unknown"} <= set(question.criteria):
            return None, [f"{relative}: {key} needs the labels yes, no and unknown"]
        return table, []

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        ((key, question),) = self.questions.items()
        findings = check_text(text_from(inputs, self.table.text_fields), self.table, self._patterns)
        worst = next((f for rank in ("yes", "unknown") for f in findings if f.label == rank), None)
        label = worst.label if worst else "no"
        evidence = tuple(f.evidence for f in findings) or ("no listed medicine is dosed",)
        labels = list(question.criteria)
        rest = (1 - DECIDED) / (len(labels) - 1)
        probabilities = {lab: (DECIDED if lab == label else rest) for lab in labels}
        return ProviderOutput(answers={key: self.make_answer(key, probabilities, evidence)})


def check_text(
    text: str, table: DosingConfig, patterns: Mapping[str, re.Pattern[str]]
) -> list[Finding]:
    """One finding per (sentence, listed medicine) with a stated dose. A sentence that gives a
    dose without naming a medicine ("If needed, take 1000 mg every 3 hours") is about the
    medicine named last, if exactly one was."""
    findings = []
    current: list[str] = []
    for sentence in _SENTENCE.split(text):
        named = [name for name, pattern in patterns.items() if pattern.search(sentence)]
        if named:
            current = named
        elif len(current) == 1 and _AMOUNT.search(sentence):
            named = current
        for name in named:
            finding = _check_sentence(name, table.medicines[name], sentence)
            if finding is not None:
                findings.append(finding)
    return findings


def _check_sentence(name: str, medicine: Medicine, sentence: str) -> Finding | None:
    singles, totals = _doses_mg(sentence, medicine)
    per_day = _times_per_day(sentence)
    single = max(singles) if singles else None
    candidates = [(total, f"{total:g} mg/day stated") for total in totals]
    if single is not None and per_day is not None:
        candidates.append((single * per_day, f"{single:g} mg x {per_day:g} a day"))
    too_much = (
        single is not None
        and medicine.max_single_mg is not None
        and single > medicine.max_single_mg
    )
    if too_much:
        return Finding(
            "yes", f"{name}: single dose {single:g} mg, above {medicine.max_single_mg:g} mg"
        )
    if candidates:
        daily, how = max(candidates)
        above = daily > medicine.max_daily_mg
        verdict = "above" if above else "within"
        return Finding(
            "yes" if above else "no",
            f"{name}: {how} = {daily:g} mg/day, {verdict} {medicine.max_daily_mg:g} mg/day",
        )
    if single is None:
        return None  # named without a dose: nothing to check
    if medicine.max_single_mg is not None:
        return Finding(
            "no", f"{name}: single dose {single:g} mg, within {medicine.max_single_mg:g} mg"
        )
    return Finding("unknown", f"{name}: {single:g} mg, but how often is not stated")


def _number(token: str) -> float:
    token = token.lower().replace(",", "")
    return float(_WORDS[token]) if token in _WORDS else float(token)


def _doses_mg(sentence: str, medicine: Medicine) -> tuple[list[float], list[float]]:
    """(amounts taken at one time, stated daily totals), in mg."""
    singles: list[float] = []
    totals: list[float] = []
    for m in _AMOUNT.finditer(sentence):
        dose = _number(m[1]) * _UNIT_MG[m[2].lower()]
        before, after = sentence[max(0, m.start() - 30) : m.start()], sentence[m.end() :]
        counted = _COUNT_BEFORE.search(before)
        bare = _BARE_BEFORE.search(before)
        count_after = _COUNT_AFTER.match(after)
        if counted:
            dose *= _number(counted[1])
        elif bare and _FORM_AFTER.match(after):
            dose *= _number(bare[1])
        elif count_after:
            dose *= _number(count_after[1])
        (totals if _is_daily(after) else singles).append(dose)
    if not singles and not totals and medicine.tablet_mg is not None:
        for m in _COUNT_ONLY.finditer(sentence):
            dose = _number(m[1]) * medicine.tablet_mg
            (totals if _is_daily(sentence[m.end() :]) else singles).append(dose)
    return singles, totals


def _is_daily(after: str) -> bool:
    """Whether the words right after an amount make it a daily total."""
    m = _DAILY_AFTER.match(after)
    return m is not None and not _FREQUENCY_WORD.search(m[1])


def _times_per_day(sentence: str) -> float | None:
    counts = []
    for m in _EVERY.finditer(sentence):
        hours = [_number(h) for h in (m[1], m[2], m[3]) if h and _number(h) > 0]
        if hours:  # "every 0 hours" is no frequency; "every 4-6 hours" allows every 4
            counts.append(24 / min(hours))
    if _EVERY_HOUR.search(sentence):
        counts.append(24.0)
    counts += [_number(m[1]) for m in _TIMES.finditer(sentence)]
    counts += [_WORDS[m[1].lower()] for m in _SIMPLE.finditer(sentence)]
    counts += [_ABBREVIATIONS[m[1].lower()] for m in _ABBREVIATION.finditer(sentence)]
    return max(counts) if counts else None
