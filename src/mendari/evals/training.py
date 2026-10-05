"""Train a pack's `naive_bayes` variants on its own datasets (`mendari train`)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mendari.core.pack import Pack
from mendari.evals.harness import resolve_dataset
from mendari.providers import naive_bayes


@dataclass(frozen=True)
class Trained:
    slot: str
    variant: str
    path: Path  # where the model was written, in the pack
    model: dict[str, Any]


def train_naive_bayes(
    pack: Pack, *, train: str = "train", calibration: str = "calibration", slot: str | None = None
) -> list[Trained]:
    """Train every `naive_bayes` variant (of `slot`, if given) on the datasets `train` and
    `calibration`, and write each model to its `model_file`, deterministically: the same pack
    and datasets give the same bytes."""
    trained = []
    for slot_name, spec in pack.manifest.providers.items():
        if slot and slot_name != slot:
            continue
        for name, variant in spec.variants.items():
            config = variant.model_extra or {}
            if variant.type != "naive_bayes":
                continue
            (question,) = spec.answers
            model = naive_bayes.train(
                resolve_dataset(pack, train).records,
                calibration=resolve_dataset(pack, calibration).records,
                question=question,
                labels=pack.manifest.labels(question),
                text_fields=config.get("text_fields", ["message.subject", "message.body"]),
            )
            target = pack.path(config["model_file"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(model, separators=(",", ":")) + "\n", encoding="utf-8")
            trained.append(Trained(slot_name, name, target, model))
    return trained
