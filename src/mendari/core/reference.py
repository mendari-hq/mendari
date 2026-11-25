# SPDX-License-Identifier: Apache-2.0
"""The bundled agents of the installed release: the agents Mendari ships, and their digests.

An agent pulled from a registry runs on the engine for agents the deployment does not own (RE2,
the caps, the input limits and a CPU budget per decision: `core/origin.py` `bounded`), unless
its digest, the one `load_agent` hashed from its files, is the digest of one of these. The digest
covers every file in the agent except those under its `evals/`, any `DECISION_CARD.md`, and
hidden entries and `__pycache__` at any depth (`agent_digest`), and what an agent decides with is
read only from the files its digest covers (`Agent.read_bytes`): equal digests mean equal
behaviour. So a copy of a bundled agent keeps the maintained engine wherever it was pulled
from, and one changed byte puts it on the other (ADR-0009).

A pack from a registry never takes a bundled agent's place: one with a bundled agent's id and
a version that ties its (`version_key`) but other files is refused at load (`core/agent.py`
`_shadow_problem`), so at pull and at publish too; `reference_agents` is the index it reads.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from mendari.core.agent import agent_digest, manifest_name, parse_yaml
from mendari.core.spec import is_agent_id, is_version
from mendari.errors import AgentValidationError

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
# Where the bundled agents are: in the wheel, or the repository's `agents/` in a checkout.
BUNDLED_DIRS: tuple[Path, ...] = (_PACKAGE_DIR / "_agents", _PACKAGE_DIR.parents[1] / "agents")


def bundled_dir() -> Path | None:
    """The directory of the agents this release bundles: the first of `BUNDLED_DIRS` there."""
    return next((path for path in BUNDLED_DIRS if path.is_dir()), None)


class ReferenceAgent(NamedTuple):
    """An agent this release bundles: its manifest's version, and the digest of its files."""

    version: str
    digest: str


@functools.cache
def reference_agents() -> Mapping[str, tuple[ReferenceAgent, ...]]:
    """The agents of the installed release by id (each directory of `bundled_dir()` holding a
    manifest, one level deep), with their versions and digests; computed once per process.
    The id and version are read from the manifest alone, not by loading the agent. A directory
    that cannot be hashed, that holds two spellings of the manifest's name, or whose manifest
    names no id or version, is left out, never raised: it is no reference."""
    root = bundled_dir()
    if root is None:
        return MappingProxyType({})
    found: dict[str, list[ReferenceAgent]] = {}
    for directory in sorted(root.iterdir()):
        try:
            manifest = manifest_name(directory)
            if manifest is None:
                continue
            digest = agent_digest(directory)
            raw = parse_yaml((directory / manifest).read_bytes(), manifest, str(directory))
        except (OSError, AgentValidationError):
            continue
        agent_id = raw.get("id") if isinstance(raw, dict) else None
        version = raw.get("version") if isinstance(raw, dict) else None
        if is_agent_id(agent_id) and is_version(version):
            found.setdefault(agent_id, []).append(ReferenceAgent(version, digest))
    return MappingProxyType({agent_id: tuple(agents) for agent_id, agents in found.items()})


@functools.cache
def reference_digests() -> frozenset[str]:
    """The digests of the agents of the installed release (`reference_agents`), computed once per
    process (about 0.04 s for five)."""
    return frozenset(agent.digest for agents in reference_agents().values() for agent in agents)
