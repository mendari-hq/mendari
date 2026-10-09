# SPDX-License-Identifier: Apache-2.0
"""One canonical form of a case's input (LP-11, `evals/canonical.py`): the strict form and the
`norm/v1` form are pinned byte for byte, since the coverage report, the hidden-case index and the
pack store's upload check (LP-20) must agree on what the same case is. A change here changes what
an index built before matches. Every character outside ASCII is made with `chr`, so the source
shows which one it is."""

from __future__ import annotations

import json
import sys

import pytest

from mendari.errors import MendariError
from mendari.evals.canonical import NORM_VERSION, canonical_input, fold, normalised_input

E_ACUTE = chr(0xE9)  # e with an acute accent, composed (NFC)
COMBINING_ACUTE = chr(0x301)  # after an "e": the same letter, decomposed (NFD)
GRINNING = chr(0x1F600)  # outside the Basic Multilingual Plane: a surrogate pair when escaped
IDEOGRAPHIC_SPACE, NO_BREAK_SPACE = chr(0x3000), chr(0xA0)
EN_DASH, EM_DASH, ELLIPSIS, FI_LIGATURE = chr(0x2013), chr(0x2014), chr(0x2026), chr(0xFB01)


def wide(text: str) -> str:
    """`text` in full-width letters and digits, as some keyboards and forms type them."""
    return "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalnum() else c for c in text)


def test_the_strict_form_sorts_keys_drops_spaces_and_escapes_to_ascii() -> None:
    assert canonical_input({"b": 1, "a": [2, {"d": None, "c": True}]}) == (
        '{"a":[2,{"c":true,"d":null}],"b":1}'
    )
    text = E_ACUTE + '\n\t"' + GRINNING
    assert canonical_input({"t": text}) == '{"t":"\\u00e9\\n\\t\\"\\ud83d\\ude00"}'


def test_the_strict_form_tells_unicode_forms_and_numbers_apart() -> None:
    composed, decomposed = "caf" + E_ACUTE, "cafe" + COMBINING_ACUTE
    assert canonical_input({"t": composed}) != canonical_input({"t": decomposed})
    assert canonical_input({"dose": 5}) != canonical_input({"dose": 5.5})


def test_norm_v1_is_pinned() -> None:
    assert NORM_VERSION == "norm/v1"
    body = wide("Chest") + IDEOGRAPHIC_SPACE + wide("PAIN")
    body += f" {EN_DASH} since {FI_LIGATURE}ve days!"
    value = {
        "message": {"body": body},
        "age": 54,
        "flags": [True, None, wide("Y") + "es"],
        "Body_Text": "  a_b   C  ",
    }
    assert normalised_input(value) == (
        '{"Body_Text":"a_b c","age":54,"flags":[true,null,"yes"],'
        '"message":{"body":"chest pain since five days"}}'
    )


def test_norm_v1_meets_case_spacing_punctuation_and_unicode_forms() -> None:
    same = [
        {"t": "Chest pain, since Monday!"},
        {"t": "chest   PAIN since monday"},
        {"t": f"{wide('Chest pain')} {EM_DASH} since Monday{ELLIPSIS}"},
        {"t": f"chest{NO_BREAK_SPACE}pain\tsince\nmonday"},
    ]
    assert len({normalised_input(value) for value in same}) == 1
    assert len({canonical_input(value) for value in same}) == len(same)
    composed, decomposed = "caf" + E_ACUTE, "cafe" + COMBINING_ACUTE
    assert normalised_input({"t": composed}) == normalised_input({"t": decomposed})


def test_norm_v1_keeps_keys_numbers_and_shape() -> None:
    assert normalised_input({"t": "take 5 mg"}) != normalised_input({"t": "take 50 mg"})
    assert normalised_input({"dose": 5}) != normalised_input({"dose": "5"})
    assert normalised_input({"Text": "a"}) != normalised_input({"text": "a"})
    assert normalised_input({"t": ["a", "b"]}) != normalised_input({"t": "a b"})
    assert normalised_input({"t": "a_b"}) != normalised_input({"t": "a b"})  # `_` is a word
    assert fold("") == "" and fold("!!!") == ""


def test_a_value_json_cannot_hold_is_refused_without_its_text() -> None:
    lone_surrogate = chr(0xD800)
    for value, what in (
        ({"t": float("nan")}, "a number that is not finite"),
        ({"t": {1, 2}}, "a value of type set"),
        ({"t": "secret" + lone_surrogate}, "a lone surrogate"),
    ):
        for form in (canonical_input, normalised_input):
            with pytest.raises(MendariError, match=what) as refused:
                form(value)
            assert "secret" not in str(refused.value)


def test_norm_v1_keeps_vowel_signs_and_accents_with_their_letters() -> None:
    heart = chr(0x926) + chr(0x93F) + chr(0x932)  # Devanagari: one vowel sign apart
    lentil = chr(0x926) + chr(0x93E) + chr(0x932)
    assert fold(heart) != fold(lentil) and " " not in fold(heart)
    no = chr(0xE44) + chr(0xE21) + chr(0xE48)  # Thai: one tone mark apart
    wood = chr(0xE44) + chr(0xE21) + chr(0xE49)
    assert fold(no) != fold(wood)
    assert fold("I" + chr(0x307) + "stanbul") == fold(chr(0x130) + "stanbul")  # one word


def test_norm_v1_drops_invisible_characters() -> None:
    invisible = [chr(0x200B), chr(0xAD), chr(0x2060), chr(0x200D), chr(0x200E), chr(0xFEFF)]
    for character in invisible:
        assert fold("Ibu" + character + "profen") == "ibuprofen", hex(ord(character))
    assert normalised_input({"t": chr(0xFEFF) + "take"}) == normalised_input({"t": "take"})


def test_norm_v1_writes_whole_numbers_as_integers() -> None:
    assert normalised_input({"age": 57.0}) == normalised_input({"age": 57}) == '{"age":57}'
    assert normalised_input({"x": -0.0}) == normalised_input({"x": 0})
    assert normalised_input({"x": 1e2}) == normalised_input({"x": 100})
    assert normalised_input({"x": 57.5}) != normalised_input({"x": 57})
    assert normalised_input({"flag": True}) != normalised_input({"flag": 1})
    assert canonical_input({"age": 57.0}) != canonical_input({"age": 57})  # the strict form


def test_norm_v1_drops_signs_and_comparison_symbols() -> None:
    """Documented, not a slip: "ER+" and "ER-" are one case under norm/v1 (and "< 30" and
    "> 30"), as every symbol and punctuation mark is a space."""
    assert fold("ER+ breast") == fold("ER- breast") == "er breast"
    assert fold("eGFR < 30") == fold("eGFR > 30") == "egfr 30"


def test_fold_is_idempotent_on_every_character_of_the_basic_plane() -> None:
    for code in range(0x10000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        once = fold("a" + chr(code) + "b")
        assert fold(once) == once, hex(code)


def test_a_deep_value_folds_without_recursion() -> None:
    depth = sys.getrecursionlimit() + 100
    value: object = "Deep"
    for _ in range(depth):
        value = [value]
    try:
        json.dumps({"t": value})
    except RecursionError:
        pytest.skip("this Python's JSON encoder recurses: such a value is refused as too deep")
    expected = '{"t":' + "[" * depth + '"deep"' + "]" * depth + "}"
    assert normalised_input({"t": value}) == expected
