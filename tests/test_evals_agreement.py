# SPDX-License-Identifier: Apache-2.0
"""Variant agreement (evals/agreement.py) fails closed: two variants agree only where both ran
whole on every question either asked, on at least one case."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from mendari.core.pack import Pack
from mendari.evals import agreement as agreement_module
from mendari.evals.agreement import agreement
from mendari.providers.base import Answer, ProviderInfo, ProviderOutput

INFO = ProviderInfo(slot="screen", variant="fake", type="fake")


def _answer(question: str, label: str) -> Answer:
    return Answer(question, label, 1.0, {label: 1.0}, INFO)


class Fake:
    """A provider that gives fixed answers and failures, whatever it is asked."""

    def __init__(self, answers: Mapping[str, str], failures: Mapping[str, str] | None = None):
        self.output = ProviderOutput(
            answers={q: _answer(q, label) for q, label in answers.items()},
            failures=dict(failures or {}),
        )
        self.questions = sorted(answers)

    def leaves(self) -> Iterator[Fake]:
        yield self

    def preflight(self) -> None:
        pass

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        return self.output


def _run(
    pack: Pack, monkeypatch: pytest.MonkeyPatch, left: Fake, right: Fake, cases: int = 1
) -> dict[str, Any]:
    fakes = {"a": left, "b": right}
    monkeypatch.setattr(
        agreement_module, "build_provider", lambda pack, slot, variant: fakes[variant]
    )
    return agreement(pack, "screen", "a", "b", [(f"case {i}", {}) for i in range(cases)])


def test_the_same_answers_agree(inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run(inbox_pack, monkeypatch, Fake({"q": "yes"}), Fake({"q": "yes"}))
    assert report["agree"] is True and report["differences"] == []


def test_a_question_only_the_second_variant_answers_is_a_difference(
    inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before, only the first variant's questions were compared: one the second answered alone
    was never looked at, and a question it did not answer raised a KeyError."""
    report = _run(inbox_pack, monkeypatch, Fake({"q": "yes"}), Fake({"q": "yes", "r": "no"}))
    assert report["agree"] is False
    assert report["differences"] == [{"case": "case 0", "question": "r", "a": None, "b": "no"}]
    report = _run(inbox_pack, monkeypatch, Fake({"q": "yes", "r": "no"}), Fake({"q": "yes"}))
    assert report["differences"] == [{"case": "case 0", "question": "r", "a": "no", "b": None}]


def test_a_partial_failure_is_never_agreement(
    inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A composite with a member down still answers, with the question in its failures: the
    same label then is not a faithful port, only the survivor answering alone."""
    down = Fake({"q": "yes"}, failures={"q": "member: timeout"})
    report = _run(inbox_pack, monkeypatch, Fake({"q": "yes"}), down)
    assert report["agree"] is False
    assert report["differences"] == [
        {"case": "case 0", "question": "q", "a": "yes", "b": "yes", "failed": ["b"]}
    ]


def test_no_case_is_no_agreement(inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _run(inbox_pack, monkeypatch, Fake({"q": "yes"}), Fake({"q": "yes"}), cases=0)
    assert report["cases"] == 0 and report["agree"] is False
