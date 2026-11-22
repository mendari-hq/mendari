# SPDX-License-Identifier: Apache-2.0
"""Synthetic sandbox: data generation, FHIR workflow and demo. Nothing here touches real PHI."""

import os
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parents[1]


# Where example datasets are, when a distribution bundles any (read when called).
DATASET_DIRS: tuple[Path, ...] = (_PACKAGE_DIR / "_datasets", _PACKAGE_DIR.parents[1] / "datasets")


def example_datasets_dir() -> Path | None:
    """The example datasets: `$MENDARI_DATASETS_PATH` (the sandbox mounts its own there), else
    those a distribution bundles (`DATASET_DIRS`), else None."""
    named = os.environ.get("MENDARI_DATASETS_PATH", "").strip()
    if named:
        return Path(named) if Path(named).is_dir() else None
    return next((candidate for candidate in DATASET_DIRS if candidate.is_dir()), None)
