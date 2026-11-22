# SPDX-License-Identifier: Apache-2.0
"""The reference packs of the installed release: the packs Mendari ships, and their digests.

A pack pulled from a registry runs on the engine for packs the deployment does not own (RE2,
the caps, the input limits and a CPU budget per decision: `core/origin.py` `bounded`), unless
its digest, the one `load_pack` hashed from its files, is the digest of one of these. The digest
covers every file in the pack except those under its `evals/`, any `DECISION_CARD.md`, and
hidden entries and `__pycache__` at any depth (`pack_digest`), and what a pack decides with is
read only from the files its digest covers (`Pack.read_bytes`): equal digests mean equal
behaviour. So a copy of a reference pack keeps the maintained engine wherever it was pulled
from, and one changed byte puts it on the other (ADR-0009).

A pack from a registry never takes a reference pack's place: one with a reference pack's id and
a version that ties its (`version_key`) but other files is refused at load (`core/pack.py`
`_shadow_problem`), so at pull and at publish too; `reference_packs` is the index it reads.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from mendari.core.pack import MANIFEST, pack_digest, parse_yaml
from mendari.core.spec import is_pack_id, is_version
from mendari.errors import PackValidationError

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
# Where the bundled agents are: in the wheel, or the repository's `agents/` in a checkout.
BUNDLED_DIRS: tuple[Path, ...] = (_PACKAGE_DIR / "_agents", _PACKAGE_DIR.parents[1] / "agents")


def bundled_dir() -> Path | None:
    """The directory of the packs this release bundles: the first of `BUNDLED_DIRS` there."""
    return next((path for path in BUNDLED_DIRS if path.is_dir()), None)


class ReferencePack(NamedTuple):
    """A pack this release bundles: its manifest's version, and the digest of its files."""

    version: str
    digest: str


@functools.cache
def reference_packs() -> Mapping[str, tuple[ReferencePack, ...]]:
    """The packs of the installed release by id (each directory of `bundled_dir()` holding a
    `pack.yaml`, one level deep), with their versions and digests; computed once per process.
    The id and version are read from the manifest alone, not by loading the pack. A directory
    that cannot be hashed, or whose manifest names no id or version, is left out, never raised:
    it is no reference."""
    root = bundled_dir()
    if root is None:
        return MappingProxyType({})
    found: dict[str, list[ReferencePack]] = {}
    for directory in sorted(root.iterdir()):
        manifest = directory / MANIFEST
        if not manifest.is_file():
            continue
        try:
            digest = pack_digest(directory)
            raw = parse_yaml(manifest.read_bytes(), MANIFEST, str(directory))
        except (OSError, PackValidationError):
            continue
        pack_id = raw.get("id") if isinstance(raw, dict) else None
        version = raw.get("version") if isinstance(raw, dict) else None
        if is_pack_id(pack_id) and is_version(version):
            found.setdefault(pack_id, []).append(ReferencePack(version, digest))
    return MappingProxyType({pack_id: tuple(packs) for pack_id, packs in found.items()})


@functools.cache
def reference_digests() -> frozenset[str]:
    """The digests of the packs of the installed release (`reference_packs`), computed once per
    process (about 0.04 s for five)."""
    return frozenset(pack.digest for packs in reference_packs().values() for pack in packs)
