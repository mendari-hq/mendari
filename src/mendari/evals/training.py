# SPDX-License-Identifier: Apache-2.0
"""Train a pack's `naive_bayes` variants on its own datasets (`mendari train`)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from mendari.core.pack import Pack
from mendari.errors import PackValidationError
from mendari.evals.harness import resolve_dataset
from mendari.providers import naive_bayes
from mendari.providers.naive_bayes import CharacterNgrams

# Train a model with the features its file says it was built with (`built_with`).
AS_BUILT: Final = "as built"
Features = CharacterNgrams | None | Literal["as built"]


@dataclass(frozen=True)
class Trained:
    slot: str
    variant: str  # the variant, or `variant.member` for a member of a composite
    path: Path  # where the model was written, in the pack
    model: dict[str, Any]


def naive_bayes_configs(pack: Pack) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(slot, variant, config) for every `naive_bayes` variant, and every `naive_bayes` member
    of a composite variant (`variant.member`), in manifest order."""
    for slot_name, spec in pack.manifest.providers.items():
        for name, variant in spec.variants.items():
            config = {"type": variant.type, **(variant.model_extra or {})}
            if variant.type == "naive_bayes":
                yield slot_name, name, config
            members = config.get("members")
            if isinstance(members, dict):
                for member_name, member in members.items():
                    if isinstance(member, dict) and member.get("type") == "naive_bayes":
                        yield slot_name, f"{name}.{member_name}", member


def model_files(pack: Pack, slot: str | None = None) -> list[tuple[str, str, dict[str, Any]]]:
    """(slot, variant, config) for each model file the pack's `naive_bayes` variants and
    composite members name (of `slot`, if given), once each: a file several name is the first's."""
    found = []
    seen: set[str] = set()
    for slot_name, name, config in naive_bayes_configs(pack):
        if (slot and slot_name != slot) or config["model_file"] in seen:
            continue
        seen.add(config["model_file"])
        found.append((slot_name, name, config))
    return found


def built_with(pack: Pack, model_file: str) -> CharacterNgrams | None:
    """The character features the model at `model_file` says it was built with
    (`character_ngrams`), or None: a model of words alone, or no model to read there yet (one
    trained for the first time)."""
    try:
        model = pack.read_json(model_file)
    except PackValidationError:
        return None
    features = model.get("character_ngrams") if isinstance(model, dict) else None
    if not isinstance(features, list) or naive_bayes.ngrams_problem(features):
        return None
    return (features[0], features[1])


def fit(
    pack: Pack,
    slot: str,
    config: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    calibration: Sequence[Mapping[str, Any]],
    character_ngrams: CharacterNgrams | None,
) -> dict[str, Any]:
    """The model of one `naive_bayes` variant of `slot`, fitted on `records` and calibrated on
    `calibration`, for the slot's question, its labels and the variant's text fields."""
    (question,) = pack.manifest.providers[slot].answers
    return naive_bayes.train(
        records,
        calibration=calibration,
        question=question,
        labels=pack.manifest.labels(question),
        text_fields=config.get("text_fields", ["message.subject", "message.body"]),
        character_ngrams=character_ngrams,
    )


def written(model: Mapping[str, Any]) -> str:
    """A model as its file holds it: the same model gives the same bytes."""
    return json.dumps(model, separators=(",", ":")) + "\n"


def train_naive_bayes(
    pack: Pack,
    *,
    train: str = "train",
    calibration: str = "calibration",
    slot: str | None = None,
    character_ngrams: Features = AS_BUILT,
) -> list[Trained]:
    """Train every `naive_bayes` variant and composite member (of `slot`, if given) on the
    datasets `train` and `calibration`, and write each model to its `model_file`,
    deterministically: the same pack and datasets give the same bytes. A model file several
    variants name is trained once, for the first.

    Each model is rebuilt the way its file says it was built (`built_with`: with its character
    features, or words alone; a first training is words alone), unless `character_ngrams` says
    otherwise: a range, or None for words alone."""
    trained = []
    for slot_name, name, config in model_files(pack, slot):
        model_file = config["model_file"]
        features = (
            built_with(pack, model_file) if character_ngrams == AS_BUILT else character_ngrams
        )
        model = fit(
            pack,
            slot_name,
            config,
            resolve_dataset(pack, train).records,
            resolve_dataset(pack, calibration).records,
            features,
        )
        target = pack.path(model_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(written(model), encoding="utf-8")
        trained.append(Trained(slot_name, name, target, model))
    return trained
