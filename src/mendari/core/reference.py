# SPDX-License-Identifier: Apache-2.0
"""The reference packs of the installed release: the packs Mendari ships, and their digests.

A pack pulled from a registry runs on the engine for packs the deployment does not own (RE2,
the caps, the input limits and a CPU budget per decision: `core/origin.py` `bounded`), unless
its digest, the one `load_pack` hashed from its files, is the digest of one of these. Equal
digests mean equal behaviour files (`pack.yaml`, `logic/`, `data/`, `models/`, `schemas/`,
`tests/`): what a pack decides with is read only from the files its digest covers
(`Pack.read_bytes`). So a copy of a reference pack keeps the maintained engine wherever it was
pulled from, and one changed byte puts it on the other (ADR-0009).
"""

from __future__ import annotations

import functools
from pathlib import Path

from mendari.core.pack import MANIFEST, pack_digest
from mendari.errors import PackValidationError

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
# Where the reference packs are: bundled in the wheel, or the repository's `packs/` in a checkout.
BUNDLED_DIRS = (_PACKAGE_DIR / "_packs", _PACKAGE_DIR.parents[1] / "packs")


def bundled_dir() -> Path | None:
    """The directory of the packs this release bundles: the first of `BUNDLED_DIRS` there."""
    return next((path for path in BUNDLED_DIRS if path.is_dir()), None)


@functools.cache
def reference_digests() -> frozenset[str]:
    """The digests of the packs of the installed release (each directory of `bundled_dir()`
    holding a `pack.yaml`, one level deep); computed once per process (about 0.04 s for five).
    A directory that cannot be hashed is left out, never raised: it is no reference."""
    root = bundled_dir()
    if root is None:
        return frozenset()
    digests = set()
    for directory in sorted(root.iterdir()):
        if not (directory / MANIFEST).is_file():
            continue
        try:
            digests.add(pack_digest(directory))
        except PackValidationError:
            continue
    return frozenset(digests)
