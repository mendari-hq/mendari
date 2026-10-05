"""Train a pack's `naive_bayes` variants on its own datasets (`mendari train`)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mendari.core.pack import Pack
from mendari.evals.harness import resolve_dataset
from mendari.providers import naive_bayes


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


def train_naive_bayes(
    pack: Pack, *, train: str = "train", calibration: str = "calibration", slot: str | None = None
) -> list[Trained]:
    """Train every `naive_bayes` variant and composite member (of `slot`, if given) on the
    datasets `train` and `calibration`, and write each model to its `model_file`,
    deterministically: the same pack and datasets give the same bytes. A model file several
    variants name is trained once, for the first."""
    trained = []
    written: set[str] = set()
    for slot_name, name, config in naive_bayes_configs(pack):
        if slot and slot_name != slot:
            continue
        model_file = config["model_file"]
        if model_file in written:
            continue
        written.add(model_file)
        (question,) = pack.manifest.providers[slot_name].answers
        model = naive_bayes.train(
            resolve_dataset(pack, train).records,
            calibration=resolve_dataset(pack, calibration).records,
            question=question,
            labels=pack.manifest.labels(question),
            text_fields=config.get("text_fields", ["message.subject", "message.body"]),
        )
        target = pack.path(model_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(model, separators=(",", ":")) + "\n", encoding="utf-8")
        trained.append(Trained(slot_name, name, target, model))
    return trained
