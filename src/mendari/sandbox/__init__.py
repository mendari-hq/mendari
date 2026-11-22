# SPDX-License-Identifier: Apache-2.0
"""Synthetic sandbox: data generation, FHIR workflow and demo. Nothing here touches real PHI."""

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parents[1]


# Where example datasets are, when a distribution bundles any (read when called).
DATASET_DIRS: tuple[Path, ...] = (_PACKAGE_DIR / "_datasets", _PACKAGE_DIR.parents[1] / "datasets")


def example_datasets_dir() -> Path | None:
    """Example datasets bundled with the distribution (`DATASET_DIRS`), or None: none is."""
    return next((candidate for candidate in DATASET_DIRS if candidate.is_dir()), None)
