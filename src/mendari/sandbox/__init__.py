"""Synthetic sandbox: data generation, FHIR workflow and demo. Nothing here touches real PHI."""

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parents[1]


def example_datasets_dir() -> Path | None:
    """Datasets bundled with the distribution, or the repository's `datasets/` in a checkout."""
    for candidate in (_PACKAGE_DIR / "_datasets", _PACKAGE_DIR.parents[1] / "datasets"):
        if candidate.is_dir():
            return candidate
    return None
