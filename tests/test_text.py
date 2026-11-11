# SPDX-License-Identifier: Apache-2.0
"""The plain form of a text (`core/text.py`): what the providers that decide on this machine
read. Text that differs only in typography decides the same: on hand-written emergencies a
phone's curly apostrophe alone took the stat priority off 3 of the 55 the keyword rules flagged
("it won't stop bleeding"). Every character a reader could not tell from its keyboard twin, or
could not see at all, is written here by its Unicode name."""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from mendari import Decision, Outcome, Pack, Priority, load_pack
from mendari.core.decision import DecisionResult
from mendari.core.text import _KEYBOARD as KEYBOARD_TABLE
from mendari.core.text import plain
from mendari.evals.harness import resolve_dataset
from mendari.providers import build_provider
from mendari.providers.base import text_from
from mendari.providers.naive_bayes import tokenize
from tests.conftest import ROOT, message

PACKS = sorted(path for path in (ROOT / "packs").iterdir() if (path / "pack.yaml").is_file())

CURLY = "\N{RIGHT SINGLE QUOTATION MARK}"  # a phone's apostrophe
NO_BREAK = "\N{NO-BREAK SPACE}"  # a web form's space
NARROW = "\N{NARROW NO-BREAK SPACE}"
ZERO_WIDTH = "\N{ZERO WIDTH SPACE}"  # carried in pasted text
BYTE_ORDER_MARK = "\N{ZERO WIDTH NO-BREAK SPACE}"
SOFT_HYPHEN = "\N{SOFT HYPHEN}"
ACUTE = "\N{COMBINING ACUTE ACCENT}"
EN_DASH = "\N{EN DASH}"  # a range, as a word processor or a language model writes one
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"  # a phone's three full stops


@pytest.mark.parametrize(
    "apostrophe",
    [
        "\N{RIGHT SINGLE QUOTATION MARK}",
        "\N{LEFT SINGLE QUOTATION MARK}",
        "\N{SINGLE LOW-9 QUOTATION MARK}",
        "\N{SINGLE HIGH-REVERSED-9 QUOTATION MARK}",
        "\N{PRIME}",
        "\N{MODIFIER LETTER APOSTROPHE}",
        "\N{ACUTE ACCENT}",  # the accent keys beside the apostrophe
        "\N{GRAVE ACCENT}",
    ],
    ids=unicodedata.name,
)
def test_an_apostrophe_is_the_keyboards(apostrophe: str) -> None:
    assert plain(f"it won{apostrophe}t stop bleeding") == "it won't stop bleeding"


@pytest.mark.parametrize(
    "mark",
    [
        "\N{LEFT DOUBLE QUOTATION MARK}",
        "\N{RIGHT DOUBLE QUOTATION MARK}",
        "\N{DOUBLE LOW-9 QUOTATION MARK}",
        "\N{DOUBLE HIGH-REVERSED-9 QUOTATION MARK}",
        "\N{DOUBLE PRIME}",
    ],
    ids=unicodedata.name,
)
def test_a_quotation_mark_is_the_keyboards(mark: str) -> None:
    assert plain(f"{mark}do not take{mark}") == '"do not take"'


@pytest.mark.parametrize(
    "space",
    [
        "  ",
        "\t",
        NO_BREAK,
        NARROW,
        "\N{THIN SPACE}",
        "\N{IDEOGRAPHIC SPACE}",
        f" {NO_BREAK} \t",
    ],
    ids=["two", "tab", "no-break", "narrow no-break", "thin", "ideographic", "mixed"],
)
def test_every_run_of_spaces_is_one_space(space: str) -> None:
    assert plain(f"chest{space}pain") == "chest pain"


@pytest.mark.parametrize(
    "invisible",
    [
        ZERO_WIDTH,
        "\N{ZERO WIDTH NON-JOINER}",
        "\N{ZERO WIDTH JOINER}",
        BYTE_ORDER_MARK,
        SOFT_HYPHEN,
        "\N{LEFT-TO-RIGHT MARK}",
        "\N{WORD JOINER}",
    ],
    ids=unicodedata.name,
)
def test_invisible_format_characters_are_left_out(invisible: str) -> None:
    assert plain(f"che{invisible}st pain") == "chest pain"


def test_a_decomposed_accent_is_composed() -> None:
    decomposed = f"no puedo respirar, vo{ACUTE}mito con sangre"
    assert plain(decomposed) == "no puedo respirar, vómito con sangre"


def test_a_soft_hyphen_between_a_letter_and_its_accent_does_not_keep_them_apart() -> None:
    assert plain(f"vo{SOFT_HYPHEN}{ACUTE}mito") == "vómito"


def test_a_character_with_one_canonical_form_takes_it() -> None:
    assert plain("\N{OHM SIGN}") == "\N{GREEK CAPITAL LETTER OMEGA}"


@pytest.mark.parametrize(
    "end",
    ["\r\n", "\r", "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}", "\x85", "\x0b", "\x0c"],
    ids=["CR LF", "CR", "line separator", "paragraph separator", "next line", "vertical tab",
         "form feed"],
)  # fmt: skip
def test_every_line_end_is_a_newline(end: str) -> None:
    assert plain(f"No fever{end}Chest pain now") == "No fever\nChest pain now"


def test_a_newline_stays_a_newline() -> None:
    # A pack's patterns never match across one, and a negation stops at one (`providers/rules.py`).
    assert plain("No fever\n\nChest  pain now \n") == "No fever\n\nChest pain now \n"


@pytest.mark.parametrize(
    "dash",
    [
        "\N{HYPHEN}",
        "\N{NON-BREAKING HYPHEN}",
        "\N{FIGURE DASH}",
        EN_DASH,
        "\N{EM DASH}",
        "\N{HORIZONTAL BAR}",
        "\N{MINUS SIGN}",
    ],
    ids=unicodedata.name,
)
def test_a_dash_is_a_hyphen(dash: str) -> None:
    assert plain(f"rock{dash}hard") == "rock-hard"


def test_an_ellipsis_is_a_full_stop() -> None:
    # A phone writes three full stops as one character: a sentence ends there all the same.
    assert plain(f"no fever{ELLIPSIS} chest pain") == "no fever. chest pain"


@pytest.mark.parametrize(
    "text",
    [
        "\N{LATIN SMALL LIGATURE FI}rst",
        "5 \N{MICRO SIGN}g",
        "\N{FULLWIDTH LATIN SMALL LETTER C}\N{FULLWIDTH LATIN SMALL LETTER H}est",
        "\N{FULLWIDTH APOSTROPHE}",
        "\N{CIRCLED DIGIT ONE}",
        "x\N{SUPERSCRIPT TWO}",
        "\N{VULGAR FRACTION ONE HALF} tablet",
    ],
    ids=ascii,
)
def test_compatibility_characters_are_left_as_they_are(text: str) -> None:
    # No compatibility folding: a ligature, the micro sign and full-width letters stay.
    assert plain(text) == text


def test_text_that_composing_would_lengthen_stays_as_it_is() -> None:
    # This letter composes to two characters: the plain form is never longer than the text.
    qa, ka_nukta = "\N{DEVANAGARI LETTER QA}", "\N{DEVANAGARI LETTER KA}\N{DEVANAGARI SIGN NUKTA}"
    assert unicodedata.normalize("NFC", qa) == ka_nukta
    assert plain(f"{qa} cafe") == f"{qa} cafe"
    assert plain(f"{qa} cafe{ACUTE}") == f"{ka_nukta} café"  # no longer in all: composed


def test_the_spec_lists_every_character_the_plain_form_writes_another_way() -> None:
    """Spec §5 names each by its code point (a pack author writes patterns by that list): the
    list is the table's, but the ASCII line ends it names in words."""
    spec = (ROOT / "docs" / "spec" / "decision-pack-0.2.md").read_text(encoding="utf-8")
    section = spec[spec.index("read it in its **plain form**") : spec.index("A newline stays a")]
    listed: set[int] = set()
    for first, last in re.findall(r"U\+([0-9A-F]{4})(?:\s+to\s+U\+([0-9A-F]{4}))?", section):
        listed |= set(range(int(first, 16), int(last or first, 16) + 1))
    assert listed == {code for code in KEYBOARD_TABLE if code not in map(ord, "\r\x0b\x0c")}


@given(st.text())
def test_the_plain_form_is_never_longer_and_is_its_own_plain_form(text: str) -> None:
    read = plain(text)
    assert len(read) <= len(text)
    assert plain(read) == read


KEYBOARD = st.characters(min_codepoint=0x21, max_codepoint=0x7E, exclude_characters="`")


@given(st.text(alphabet=KEYBOARD))
def test_keyboard_text_with_single_spaces_is_unchanged(word: str) -> None:
    text = f"{word} {word}\n{word}"
    assert plain(text) == text


# -- what the providers read -------------------------------------------------------------------


def test_each_field_is_read_in_its_plain_form() -> None:
    inputs = {
        "message": {
            "subject": f"Can{CURLY}t{NO_BREAK}breathe",
            "body": f"che{ZERO_WIDTH}st  pain\r\nnow",
        }
    }
    assert text_from(inputs, ["message.subject", "message.body"]) == (
        "Can't breathe\nchest pain\nnow"
    )


def test_a_field_with_nothing_to_read_is_left_out() -> None:
    inputs = {"message": {"subject": f"{ZERO_WIDTH}{NO_BREAK}", "body": "hello"}}
    assert text_from(inputs, ["message.subject", "message.body"]) == "hello"


TYPED = {
    # Each a true emergency, the first three of the pack's own data, as a phone, a web form or
    # pasted text writes it.
    "curly apostrophe: bleeding": (
        f"I cut my hand and it won{CURLY}t stop bleeding after 20 minutes of pressure."
    ),
    "curly apostrophe: throat": (
        f"Hello there, My throat feels like it{CURLY}s closing up after taking the new metformin."
    ),
    "curly apostrophe: stroke": f"My dad suddenly can{CURLY}t walk straight and is very confused",
    "no-break space": f"I have chest{NO_BREAK}pain and I{CURLY}m sweating",
    "two spaces": "I have chest  pain and I am sweating",
    "zero-width space": f"I have che{ZERO_WIDTH}st pain and I am sweating",
    "no-break space, Spanish": f"No{NO_BREAK}puedo respirar",
    "decomposed accent": f"Tengo vo{ACUTE}mito con sangre desde anoche",
}


@pytest.mark.parametrize("typed", TYPED)
def test_an_emergency_is_flagged_however_it_is_typed(router: Decision, typed: str) -> None:
    result = router.decide(message(TYPED[typed]))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "nurse")
    assert result.priority is Priority.STAT and result.rule == "possible-emergency"


def test_a_negation_ends_at_an_ellipsis_as_at_a_full_stop(router: Decision) -> None:
    for stop in ("...", ELLIPSIS):
        result = router.decide(message(f"No fever{stop} I have chest pain now"))
        assert result.priority is Priority.STAT, ascii(stop)


def test_the_classifier_reads_typographic_variants_as_the_same_words(inbox_pack: Pack) -> None:
    classifier = build_provider(inbox_pack, "router", "naive_bayes")
    typed = "I can't pay my bill, can you set up a payment plan?"
    fancy = f"I can{CURLY}t{NO_BREAK}pay my bill, can you set{ZERO_WIDTH} up a payment  plan?"
    assert tokenize(text_from(message(fancy), ["message.body"])) == tokenize(typed)
    assert (
        classifier.answer(message(fancy)).answers["destination"].probabilities
        == classifier.answer(message(typed)).answers["destination"].probabilities
    )


@pytest.mark.parametrize(
    "draft",
    [
        "Take acetaminophen 500 mg, 2 tablets every 4-6 hours.",
        f"Take acetaminophen 500 mg, 2 tablets every 4{EN_DASH}6 hours.",
        f"Take acetaminophen 500{NO_BREAK}mg, 2{NO_BREAK}tablets every 4{EN_DASH}6{NARROW}hours.",
    ],
    ids=["as a keyboard types it", "an en dash", "an en dash and no-break spaces"],
)
def test_a_dose_is_read_however_its_range_and_spaces_are_typed(draft: str) -> None:
    dosing = Decision.from_path(ROOT / "packs" / "ai-guard").providers["dosing"]
    answer = dosing.answer({"draft": draft}).answers["dose_exceeds_limit"]
    assert answer.label == "yes" and "1000 mg x 6 a day" in " ".join(answer.evidence)


# -- nothing that ships changes ------------------------------------------------------------------


def _texts(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return [value] if isinstance(value, str) else []


@pytest.mark.parametrize("path", PACKS, ids=lambda path: path.name)
def test_every_text_of_a_reference_packs_data_is_already_plain(path: Path) -> None:
    """So the packs' data, trained models, benchmarks and digests are what they were: the plain
    form changes no text they hold."""
    pack = load_pack(path)
    for name in pack.manifest.evaluation.datasets:
        for record in resolve_dataset(pack, name).records:
            changed = [text for text in _texts(record["input"]) if plain(text) != text]
            assert not changed, (name, record["id"])


@pytest.mark.parametrize("path", PACKS, ids=lambda path: path.name)
def test_every_pattern_of_a_reference_pack_is_written_in_plain_text(path: Path) -> None:
    """A pattern is matched against plain text, so a character the plain form never holds (a
    curly quote, a no-break space) could never match."""
    for config in sorted((path / "logic").glob("*.yaml")):
        for text in _texts(yaml.safe_load(config.read_text(encoding="utf-8"))):
            assert plain(text) == text, (config.name, text)


def _retyped(value: Any, how: Callable[[str], str]) -> Any:
    """`value` with each free text (a string with a space: codes and choices have none) retyped."""
    if isinstance(value, dict):
        return {key: _retyped(item, how) for key, item in value.items()}
    if isinstance(value, list):
        return [_retyped(item, how) for item in value]
    return how(value) if isinstance(value, str) and " " in value else value


def _spaced(text: str) -> str:
    """Every third space a no-break space, every third two spaces."""
    parts = text.split(" ")
    spaces = [" ", NO_BREAK, "  "]
    return "".join(
        part + (spaces[index % 3] if index < len(parts) - 1 else "")
        for index, part in enumerate(parts)
    )


def _pasted(text: str) -> str:
    """A zero-width space inside every fourth word, and a byte order mark first."""
    words = text.split(" ")
    return BYTE_ORDER_MARK + " ".join(
        word[: len(word) // 2] + ZERO_WIDTH + word[len(word) // 2 :] if index % 4 == 0 else word
        for index, word in enumerate(words)
    )


RETYPED: dict[str, Callable[[str], str]] = {
    "curly apostrophes": lambda text: text.replace("'", CURLY),
    "no-break and double spaces": _spaced,
    "zero-width spaces": _pasted,
    "decomposed accents": lambda text: unicodedata.normalize("NFD", text),
}


@pytest.mark.parametrize("path", PACKS, ids=lambda path: path.name)
@pytest.mark.parametrize("how", RETYPED)
def test_a_critical_case_decides_the_same_however_it_is_typed(path: Path, how: str) -> None:
    """Every true critical case of every reference pack's datasets, but its training data,
    retyped: the same outcome, recommendation, priority and answers as the text as shipped."""
    decision = Decision.from_path(path)
    manifest = decision.pack.manifest
    critical = [(item.question, item.label) for item in manifest.evaluation.critical]
    checked = 0
    for name in manifest.evaluation.datasets:
        if name in ("train", "calibration"):
            continue
        for record in resolve_dataset(decision.pack, name).records:
            labels = record.get("labels", {})
            if not any(labels.get(question) == label for question, label in critical):
                continue
            shipped = decision.decide(record["input"])
            retyped = decision.decide(_retyped(copy.deepcopy(record["input"]), RETYPED[how]))
            assert _decided(retyped) == _decided(shipped), (name, record["id"])
            checked += 1
    assert checked


def _decided(result: DecisionResult) -> tuple[Any, ...]:
    answers = {question: answer.label for question, answer in result.answers.items()}
    return (result.outcome, result.recommendation, result.priority, result.rule, answers)
