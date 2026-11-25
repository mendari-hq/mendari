# SPDX-License-Identifier: Apache-2.0
"""Local hub: find agents on the search path.

Search order: `MENDARI_AGENTS_PATH` (os.pathsep-separated), then packs pulled from a registry
(the verified cache, `mendari pull`), then the packs bundled with the installed distribution (or
the repository's `agents/` directory in a source checkout).

Each directory has an origin (`core/origin.py`): `maintained`, `hub` or `local`. An agent
found under a `local` directory takes the origin of where it sits, so a path that holds
`agents/` (a checkout) still finds the maintained agents as maintained.

Several versions of one id may be found, and the newest is served.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from mendari.core.origin import Origin
from mendari.core.pack import Pack, has_manifest, load_pack, manifests_under
from mendari.core.reference import BUNDLED_DIRS, bundled_dir
from mendari.core.spec import version_key  # also imported from here (the server, the hub)
from mendari.errors import MendariError, PackNotFoundError

_BUNDLED_DIRS = BUNDLED_DIRS  # `core/reference.py`
MAX_DEPTH = 4  # a manifest at most this many path parts below a search directory


class SearchPath(NamedTuple):
    path: Path
    origin: Origin


@dataclass(frozen=True)
class Discovery:
    packs: list[Pack]
    problems: dict[str, str]


def search_paths() -> list[SearchPath]:
    paths = [
        SearchPath(Path(p), origin_of(Path(p)))
        for p in os.environ.get("MENDARI_AGENTS_PATH", "").split(os.pathsep)
        if p
    ]
    from mendari.hub.client import packs_cache  # packs pulled from a registry, verified

    if packs_cache().is_dir():
        paths.append(SearchPath(packs_cache(), "hub"))
    bundled = bundled_dir()
    if bundled is not None:
        paths.append(SearchPath(bundled, "maintained"))
    return paths


def origin_of(path: str | Path) -> Origin:
    """The origin of a directory (a search directory, or a pack's) given without one: in the
    bundled agents, maintained; in the registry cache (`hub/client.py` `packs_cache`), hub;
    anywhere else, local."""
    from mendari.hub.client import packs_cache

    resolved = Path(path).resolve()
    if any(_inside(resolved, bundled) for bundled in _BUNDLED_DIRS):
        return "maintained"
    if _inside(resolved, packs_cache()):
        return "hub"
    return "local"


def is_within(path: str | Path, directory: str | Path) -> bool:
    """Whether the file or folder at `path` is `directory` or in it, by the file it is, however
    either path is spelt (`_inside`)."""
    return _inside(Path(path).resolve(), Path(directory))


def _inside(path: Path, directory: Path) -> bool:
    """Whether `path` (resolved) is `directory` or in it: by how it is spelt or, since one
    directory can be spelt two ways (on a volume that ignores case, or under a second name for
    the same volume), by the file it is. So a pack is never taken for a local one by the
    spelling of its path."""
    resolved = directory.resolve()
    if path.is_relative_to(resolved):
        return True
    if not resolved.is_dir():
        return False
    for parent in (path, *path.parents):
        try:
            if parent.samefile(resolved):
                return True
        except OSError:  # not there (a pack about to be written), or it cannot be read
            continue
    return False


def discover(paths: Sequence[Path | SearchPath] | None = None) -> Discovery:
    """Every pack on the search path (or `paths`; a plain path's origin is `origin_of` it),
    and why each other manifest found (`agent.yaml`) is not served, by directory. A pack never
    stops a discovery: whatever its content raises, it is listed by its directory and the
    others are found."""
    bases = [
        entry if isinstance(entry, SearchPath) else SearchPath(entry, origin_of(entry))
        for entry in (paths if paths is not None else search_paths())
    ]
    found: list[Pack] = []
    problems: dict[str, str] = {}
    seen: set[Path] = set()
    for base, base_origin in bases:
        for manifest in manifests_under(Path(base)):
            root = manifest.parent.resolve()
            depth = len(manifest.relative_to(base).parts)
            if root in seen or depth > MAX_DEPTH:
                continue
            origin = base_origin
            if base_origin == "local":
                # A local path holding agents/ (a checkout): each agent has the origin of where
                # it sits.
                origin = origin_of(root)
            seen.add(root)
            try:
                found.append(load_pack(root, origin=origin))
            except MendariError as error:
                problems[str(root)] = str(error)
            # Any other failure is the pack's content too (an outside pack's): it is listed by
            # its type alone, and the other packs are still found, so a pack never stops a server.
            except Exception as error:
                problems[str(root)] = f"the agent could not be loaded ({type(error).__name__})"
    return Discovery(packs=found, problems=problems)


def resolve_pack(ref: str, paths: Sequence[Path | SearchPath] | None = None) -> Pack:
    """Resolve a directory, `namespace/name` (latest version) or `namespace/name@version`."""
    as_path = Path(ref)
    if has_manifest(as_path):
        return load_pack(as_path, origin=origin_of(as_path))
    pack_id, _, version = ref.partition("@")
    matches = [
        pack
        for pack in discover(paths).packs
        if pack.id == pack_id and (not version or pack.version == version)
    ]
    if not matches:
        searched = paths if paths is not None else search_paths()
        where = [str(p.path if isinstance(p, SearchPath) else p) for p in searched]
        raise PackNotFoundError(
            f"no agent {ref!r} on the search path ({', '.join(where) or 'empty'})"
        )
    return max(matches, key=lambda pack: version_key(pack.version))
