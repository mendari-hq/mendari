# SPDX-License-Identifier: Apache-2.0
"""Local hub: find Decision Packs on the search path.

Search order: `MENDARI_PACKS_PATH` (os.pathsep-separated), then packs pulled from a registry
(the verified cache, `mendari pull`), then the packs bundled with the installed distribution
(or the repository's `packs/` directory in a source checkout), then, with
`MENDARI_COMMUNITY_PACKS=on`, the repository's `community-packs/`.

Each directory has an origin (`core/origin.py`): `maintained`, `community`, `hub` or `local`.
A pack found under a `local` directory takes the origin of where it sits, so a path that holds
`packs/` (a checkout) still finds the maintained packs as maintained, and `community-packs/` as
community packs, served only when they are on.

A community pack never shares its id: when one claims an id another pack also claims, the first
claim holds it, whatever the versions, and both are reported. Packs from anywhere else are found
as they were before community packs existed (several versions of one id may be found, and the
newest is served).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from mendari.core.origin import (
    Origin,
    community_problems,
    namespace_problem,
    performer_problem,
)
from mendari.core.pack import MANIFEST, Pack, load_pack
from mendari.core.reference import BUNDLED_DIRS, bundled_dir
from mendari.core.spec import version_key  # also imported from here (the server, the hub)
from mendari.errors import MendariError, PackNotFoundError
from mendari.settings import community_packs_enabled

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_BUNDLED_DIRS = BUNDLED_DIRS  # `core/reference.py`; `community/check.py` reads this name
_COMMUNITY_DIR = _PACKAGE_DIR.parents[1] / "community-packs"
MAX_DEPTH = 4  # `pack.yaml` at most this many path parts below a search directory


class SearchPath(NamedTuple):
    path: Path
    origin: Origin


@dataclass(frozen=True)
class Discovery:
    packs: list[Pack]
    problems: dict[str, str]


def search_paths() -> list[SearchPath]:
    community = community_packs_enabled()  # an invalid value fails here, at startup
    paths = [
        SearchPath(Path(p), origin_of(Path(p)))
        for p in os.environ.get("MENDARI_PACKS_PATH", "").split(os.pathsep)
        if p
    ]
    # Off means off: `MENDARI_PACKS_PATH` naming community-packs/ does not serve them either.
    paths = [entry for entry in paths if community or entry.origin != "community"]
    from mendari.hub.client import packs_cache  # packs pulled from a registry, verified

    if packs_cache().is_dir():
        paths.append(SearchPath(packs_cache(), "hub"))
    bundled = bundled_dir()
    if bundled is not None:
        paths.append(SearchPath(bundled, "maintained"))
    if community and _COMMUNITY_DIR.is_dir():
        paths.append(SearchPath(_COMMUNITY_DIR, "community"))
    return paths


def origin_of(path: str | Path) -> Origin:
    """The origin of a directory (a search directory, or a pack's) given without one: in the
    bundled packs, maintained; in the repository's community-packs/, community; in the
    registry cache, hub; anywhere else, local."""
    from mendari.hub.client import packs_cache

    resolved = Path(path).resolve()
    if any(_inside(resolved, bundled) for bundled in _BUNDLED_DIRS):
        return "maintained"
    if _inside(resolved, _COMMUNITY_DIR):
        return "community"
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


def _manifests(base: Path) -> Iterator[Path]:
    """Prune the search at its declared depth before entering unrelated project trees."""
    for parent, children, _ in os.walk(base):
        folder = Path(parent)
        if len(folder.relative_to(base).parts) + 1 >= MAX_DEPTH:
            children.clear()
        manifest = folder / MANIFEST
        if os.path.lexists(manifest):
            yield manifest


def discover(paths: Sequence[Path | SearchPath] | None = None) -> Discovery:
    """Every pack on the search path (or `paths`; a plain path's origin is `origin_of` it),
    and why each other `pack.yaml` found is not served, by directory. A pack never stops a
    discovery: whatever its content raises, it is listed by its directory and the others are
    found."""
    bases = [
        entry if isinstance(entry, SearchPath) else SearchPath(entry, origin_of(entry))
        for entry in (paths if paths is not None else search_paths())
    ]
    found: list[tuple[int, Pack]] = []
    problems: dict[str, str] = {}
    seen: set[Path] = set()
    community_on: bool | None = None
    for order, (base, base_origin) in enumerate(bases):
        for manifest in sorted(_manifests(base)):
            root = manifest.parent.resolve()
            depth = len(manifest.relative_to(base).parts)
            if root in seen:
                continue
            origin = base_origin
            if base_origin == "local":
                # A local path holding packs/ or community-packs/: each pack has the origin of
                # where it sits. A community pack is skipped while they are off (off means off).
                origin = origin_of(root)
                if origin == "community":
                    if community_on is None:
                        community_on = community_packs_enabled()
                    if not community_on:
                        continue
                    depth = len((root / MANIFEST).relative_to(_COMMUNITY_DIR.resolve()).parts)
            seen.add(root)
            if origin == "community" and depth != 2:
                problems[str(root)] = (
                    "a community pack sits exactly one level deep: community-packs/<name>/"
                    f"{MANIFEST}"
                )
                continue
            if origin == "community" and (root / ".git").exists():
                problems[str(root)] = (
                    "a community pack is a folder of this repository, never a git submodule"
                )
                continue
            try:
                pack = load_pack(root, origin=origin)
                refused = [namespace_problem(pack.id, origin)]
                if origin == "community":
                    refused += community_problems(pack.manifest, root.name)
                    refused.append(performer_problem(pack.id, performer_system(pack)))
                if any(refused):
                    raise MendariError(
                        f"{pack.id} is not served:\n"
                        + "\n".join(f"  - {problem}" for problem in refused if problem)
                    )
                found.append((order, pack))
            except MendariError as error:
                problems[str(root)] = str(error)
            # Any other failure is the pack's content too (an outside pack's): it is listed by
            # its type alone, and the other packs are still found, so a pack never stops a server.
            except Exception as error:
                problems[str(root)] = f"the pack could not be loaded ({type(error).__name__})"
    return Discovery(packs=_one_pack_per_id(found, problems), problems=problems)


def performer_system(pack: Pack) -> str | None:
    """The `task.performer_system` of a pack's FHIR mapping, if it has one."""
    if pack.manifest.inputs.fhir is None:
        return None
    from mendari.standards.fhir import FhirAdapter

    return FhirAdapter.for_pack(pack).mapping.task.performer_system


def _one_pack_per_id(found: list[tuple[int, Pack]], problems: dict[str, str]) -> list[Pack]:
    """The packs, without any second claim to an id a community pack takes part in: a
    maintained pack holds its id first, then the earlier search directory does, whatever the
    versions. Every pack refused, and the one kept, is reported in `problems`. Ids no community
    pack claims are left as they were found (the newest version is served)."""
    claims: dict[str, list[tuple[int, Pack]]] = {}
    for order, pack in found:
        claims.setdefault(pack.id, []).append((order, pack))
    refused: set[Path] = set()
    for pack_id, claimants in claims.items():
        if not any(pack.origin == "community" for _, pack in claimants):
            continue
        claimants.sort(key=lambda claim: (claim[1].origin != "maintained", claim[0]))
        first_order, first = claimants[0]
        others = [
            pack
            for order, pack in claimants[1:]
            if not (first.origin == pack.origin == "hub" and order == first_order)
        ]
        if not others:
            continue
        for pack in others:
            refused.add(pack.root)
            problems[str(pack.root)] = (
                f"{pack_id} is not served: the id is already taken by the pack at {first.root} "
                f"({first.origin}); a pack never replaces another, whatever its version"
            )
        problems[str(first.root)] = f"{pack_id} is served from here; also claimed by " + ", ".join(
            f"{pack.root} ({pack.origin})" for pack in others
        )
    return [pack for _, pack in found if pack.root not in refused]


def resolve_pack(
    ref: str,
    paths: Sequence[Path | SearchPath] | None = None,
    *,
    missing_models_ok: bool = False,
) -> Pack:
    """Resolve a directory, `namespace/name` (latest version) or `namespace/name@version`.
    `missing_models_ok` applies to a directory: a pack about to be trained."""
    as_path = Path(ref)
    if (as_path / MANIFEST).is_file():
        return load_pack(as_path, missing_models_ok=missing_models_ok, origin=origin_of(as_path))
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
            f"no pack {ref!r} on the search path ({', '.join(where) or 'empty'})"
        )
    return max(matches, key=lambda pack: version_key(pack.version))
