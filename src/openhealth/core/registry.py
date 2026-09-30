"""Local hub: find Decision Packs on the search path.

Search order: `OPENHEALTH_PACKS_PATH` (os.pathsep-separated), then packs pulled from a registry
(the verified cache, `openhealth pull`), then the packs bundled with the installed distribution
(or the repository's `packs/` directory in a source checkout).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from openhealth.core.pack import MANIFEST, Pack, load_pack
from openhealth.errors import OpenHealthError, PackNotFoundError

_PACKAGE_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Discovery:
    packs: list[Pack]
    problems: dict[str, str]


def search_paths() -> list[Path]:
    paths = [Path(p) for p in os.environ.get("OPENHEALTH_PACKS_PATH", "").split(os.pathsep) if p]
    from openhealth.hub.client import packs_cache  # packs pulled from a registry, verified

    if packs_cache().is_dir():
        paths.append(packs_cache())
    for bundled in (_PACKAGE_DIR / "_packs", _PACKAGE_DIR.parents[1] / "packs"):
        if bundled.is_dir():
            paths.append(bundled)
            break
    return paths


def discover(paths: list[Path] | None = None) -> Discovery:
    packs: list[Pack] = []
    problems: dict[str, str] = {}
    seen: set[Path] = set()
    for base in paths if paths is not None else search_paths():
        for manifest in sorted(Path(base).glob(f"**/{MANIFEST}")):
            root = manifest.parent.resolve()
            if root in seen or len(manifest.relative_to(base).parts) > 4:
                continue
            seen.add(root)
            try:
                packs.append(load_pack(root))
            except OpenHealthError as error:
                problems[str(root)] = str(error)
    return Discovery(packs=packs, problems=problems)


def resolve_pack(
    ref: str, paths: list[Path] | None = None, *, missing_models_ok: bool = False
) -> Pack:
    """Resolve a directory, `namespace/name` (latest version) or `namespace/name@version`.
    `missing_models_ok` applies to a directory: a pack about to be trained."""
    as_path = Path(ref)
    if (as_path / MANIFEST).is_file():
        return load_pack(as_path, missing_models_ok=missing_models_ok)
    pack_id, _, version = ref.partition("@")
    matches = [
        pack
        for pack in discover(paths).packs
        if pack.id == pack_id and (not version or pack.version == version)
    ]
    if not matches:
        raise PackNotFoundError(
            f"no pack {ref!r} on the search path "
            f"({', '.join(map(str, paths if paths is not None else search_paths())) or 'empty'})"
        )
    return max(matches, key=lambda pack: version_key(pack.version))


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("-")[0]))
