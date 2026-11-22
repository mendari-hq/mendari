# SPDX-License-Identifier: Apache-2.0
"""Character features for the built-in classifier (`providers/naive_bayes.py`): a model file may
say that it also counts the runs of characters inside each word (`character_ngrams`, format
`mendari.naive_bayes/2`), which reads a misspelt or unseen word by the parts it shares with the
words it learned. Off unless a model file says so: a model without it is trained and scored as
before, byte for byte."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, load_pack
from mendari.cli import main
from mendari.errors import PackValidationError
from mendari.evals.harness import load_dataset
from mendari.evals.training import train_naive_bayes
from mendari.providers import build_provider
from mendari.providers.base import softmax, text_from
from mendari.providers.naive_bayes import FORMAT, FORMAT_CHARACTERS, log_posterior, tokenize, train
from tests.conftest import INBOX_DIR, ROOT, message

HARDER = (
    ROOT / "tests" / "fixtures" / "datasets" / "drafts" / "patient-inbox-harder" / "heldout.jsonl"
)
MODEL = "models/destination-nb.json"
TOY = [
    ("please refill my prescription", "medical_assistant"),
    ("refill the prescription for metformin", "medical_assistant"),
    ("my prescription needs a refill", "medical_assistant"),
    ("question about my billing statement", "billing"),
    ("the billing statement is wrong", "billing"),
    ("send me the statement for billing", "billing"),
] * 4


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """A copy of the Patient Inbox Router to train in."""
    root = tmp_path / "patient-inbox-router"
    shutil.copytree(INBOX_DIR, root)
    return root


def toy(**options: Any) -> dict[str, Any]:
    records = [{"input": message(text), "labels": {"q": label}} for text, label in TOY]
    return train(
        records,
        calibration=records[:6],
        question="q",
        labels=["medical_assistant", "billing"],
        text_fields=["message.body"],
        **options,
    )


def answer(model: dict[str, Any], body: str) -> dict[str, float]:
    features = model.get("character_ngrams")
    words = tokenize(body, tuple(features) if features else None)
    scores = log_posterior(model["labels"], model["log_prior"], model["tokens"], words)
    return softmax(scores, model["temperature"])


def test_words_alone_are_the_tokens_they_were() -> None:
    assert tokenize("My bill") == ["my", "bill", "my bill"]


def test_character_features_are_the_runs_inside_each_word() -> None:
    assert tokenize("My bill", (3, 4)) == [
        "my",
        "bill",
        "my bill",
        "#<my",  # each word between its edges, `<` and `>`
        "#my>",
        "#<my>",
        "#<bi",
        "#bil",
        "#ill",
        "#ll>",
        "#<bil",
        "#bill",
        "#ill>",
    ]


def test_a_word_too_long_to_be_one_has_no_runs_counted() -> None:
    """A link or an encoded attachment is one word: each of its characters would give up to
    seven runs."""
    word = "x" * 64  # between its edges, 66 characters: 64 runs of 3, 63 of 4, 62 of 5
    assert sum(token.startswith("#") for token in tokenize(word, (3, 5))) == 64 + 63 + 62
    assert tokenize(word + "x", (3, 5)) == [word + "x"]


def test_a_model_trained_without_them_is_the_format_it_was() -> None:
    model = toy()
    assert model["format"] == FORMAT == "mendari.naive_bayes/1"
    assert "character_ngrams" not in model
    assert list(model) == [
        "format", "question", "labels", "text_fields", "temperature", "log_prior", "training",
        "tokens",
    ]  # fmt: skip
    assert not any(token.startswith("#") for token in model["tokens"])


def test_a_model_trained_with_them_says_so_and_is_another_format() -> None:
    model = toy(character_ngrams=(3, 5))
    assert model["format"] == FORMAT_CHARACTERS == "mendari.naive_bayes/2"
    assert model["character_ngrams"] == [3, 5]
    assert "#<ref" in model["tokens"] and "refill" in model["tokens"]


def test_a_misspelt_word_is_read_by_its_parts() -> None:
    misspelt = "refil perscription"  # neither word was in the training text
    words, characters = toy(), toy(character_ngrams=(3, 5))
    assert max(answer(words, misspelt).values()) < 0.6  # nothing to go on: near the prior
    read = answer(characters, misspelt)
    assert read["medical_assistant"] > 0.9


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"character_ngrams": None}, "character_ngrams"),  # the format says it has them
        ({"character_ngrams": [3]}, "character_ngrams"),
        ({"character_ngrams": [5, 3]}, "character_ngrams"),
        ({"character_ngrams": [1, 3]}, "character_ngrams"),
        ({"character_ngrams": [3, 9]}, "character_ngrams"),
        ({"character_ngrams": [3.0, 5]}, "character_ngrams"),
        ({"character_ngrams": [True, 5]}, "character_ngrams"),
        ({"format": FORMAT}, "character_ngrams"),  # a /1 model never holds them
        ({"format": "mendari.naive_bayes/3"}, "expected format"),
    ],
    ids=lambda value: json.dumps(value) if isinstance(value, dict) else None,
)
def test_a_model_file_that_misstates_its_features_is_refused(
    inbox: Path, change: dict[str, Any], problem: str
) -> None:
    model = json.loads((inbox / MODEL).read_text())
    model |= {"format": FORMAT_CHARACTERS, "character_ngrams": [3, 5]}
    (inbox / MODEL).write_text(json.dumps(model))
    build_provider(load_pack(inbox), "router", "naive_bayes")  # as it should be: it loads
    for key, value in change.items():
        if value is None:
            del model[key]
        else:
            model[key] = value
    (inbox / MODEL).write_text(json.dumps(model))
    with pytest.raises(PackValidationError, match=problem):
        build_provider(load_pack(inbox), "router", "naive_bayes")


def test_a_pack_decides_with_a_model_that_has_them(inbox: Path) -> None:
    train_naive_bayes(load_pack(inbox, missing_models_ok=True), character_ngrams=(3, 5))
    decision = Decision.from_path(inbox)
    result = decision.decide(message("Hi, I need a refill on my lisinopril. Thanks"))
    assert result.answers["destination"].label == "medical_assistant"
    assert decision.provider_info["router"].model.startswith("naive_bayes@")  # type: ignore[union-attr]


# -- `mendari train` -----------------------------------------------------------------------------


def trained(root: Path) -> dict[str, Any]:
    model: dict[str, Any] = json.loads((root / MODEL).read_text())
    return model


def test_training_rebuilds_a_model_the_way_its_file_says_it_was_built(
    inbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = inbox
    shipped = (root / MODEL).read_bytes()
    assert main(["train", str(root)]) == 0
    assert (root / MODEL).read_bytes() == shipped  # the reference model: words alone

    assert main(["train", str(root), "--character-ngrams", "3-5"]) == 0
    with_characters = (root / MODEL).read_bytes()
    assert trained(root)["character_ngrams"] == [3, 5]
    assert "words and runs of 3 to 5 characters" in capsys.readouterr().out

    assert main(["train", str(root)]) == 0  # nothing said: as the file says
    assert (root / MODEL).read_bytes() == with_characters

    assert main(["train", str(root), "--character-ngrams", "none"]) == 0
    assert (root / MODEL).read_bytes() == shipped


@pytest.mark.parametrize(
    "damage",
    [
        lambda model: {**model, "character_ngrams": [5, 3]},
        lambda model: {key: value for key, value in model.items() if key != "character_ngrams"},
        lambda model: "not a model",
    ],
    ids=["a range that is not one", "no range", "not a model"],
)
def test_a_model_that_does_not_say_how_it_was_built_is_not_rebuilt_as_words_alone(
    inbox: Path, damage: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Training it again as its file says cannot be done, and guessing would quietly take its
    character features away: the command asks."""
    assert main(["train", str(inbox), "--character-ngrams", "3-5"]) == 0
    (inbox / MODEL).write_text(json.dumps(damage(trained(inbox))))
    damaged = (inbox / MODEL).read_bytes()
    capsys.readouterr()

    assert main(["train", str(inbox)]) == 1
    assert "cannot tell how this model was built" in capsys.readouterr().err
    assert (inbox / MODEL).read_bytes() == damaged

    assert main(["train", str(inbox), "--character-ngrams", "3-5"]) == 0  # said: rebuilt
    assert trained(inbox)["character_ngrams"] == [3, 5]


def test_a_first_training_is_words_alone(inbox: Path) -> None:
    shipped = (inbox / MODEL).read_bytes()
    (inbox / MODEL).unlink()
    assert main(["train", str(inbox)]) == 0
    assert (inbox / MODEL).read_bytes() == shipped


@pytest.mark.parametrize("given", ["3", "5-3", "1-3", "3-9", "a-b", "3-5-7"])
def test_a_range_that_is_not_one_is_refused(
    inbox: Path, given: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = inbox
    before = (root / MODEL).read_bytes()
    assert main(["train", str(root), "--character-ngrams", given]) == 1
    assert "--character-ngrams" in capsys.readouterr().err
    assert (root / MODEL).read_bytes() == before


def test_the_reference_models_are_words_alone() -> None:
    """So `mendari train` rebuilds them byte for byte, as CI checks."""
    for model in sorted(
        (ROOT / "tests" / "fixtures" / "reference-packs").glob("*/models/*-nb.json")
    ):
        assert json.loads(model.read_text())["format"] == FORMAT, model


# -- what they are for ---------------------------------------------------------------------------


def test_character_features_read_harder_messages_better() -> None:
    """The inbox router, trained on the pack's own data both ways, on the harder draft's
    held-out split (436 messages that share no family, paraphrase or sentence with it): 73.9%
    right with words alone, 81.0% with character features. A draft, not clinically reviewed:
    this measures the reading, and nothing about the pack."""
    pack = load_pack(INBOX_DIR)
    fields = ["message.subject", "message.body"]
    labels = pack.manifest.labels("destination")
    cases = load_dataset(HARDER).records

    def right(character_ngrams: tuple[int, int] | None) -> int:
        model = train(
            load_dataset(INBOX_DIR / "data" / "train.jsonl").records,
            calibration=load_dataset(INBOX_DIR / "data" / "calibration.jsonl").records,
            question="destination",
            labels=labels,
            text_fields=fields,
            character_ngrams=character_ngrams,
        )
        found = 0
        for case in cases:
            words = tokenize(text_from(case["input"], fields), character_ngrams)
            scores = log_posterior(labels, model["log_prior"], model["tokens"], words)
            found += max(scores, key=lambda label: scores[label]) == case["labels"]["destination"]
        return found

    words, characters = right(None), right((3, 5))
    assert (words, characters, len(cases)) == (322, 353, 436)
