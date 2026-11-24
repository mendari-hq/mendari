# SPDX-License-Identifier: Apache-2.0
"""Names from before, which still work.

Mendari was called OpenHealth Hub until 2026-09-30. Deployments configured before the rename
keep working: a setting under the old `OPENHEALTH_` prefix is read as its `MENDARI_` name (the
`MENDARI_` name wins when both are set), and a setting that names a bundled agent by its old id
(`openhealth/…`) means the pack as it is now called (`mendari/…`).

What Mendari governs was called a Decision Pack until 2026-11. Four settings were named for it
(`RENAMED_SETTINGS`): each is read under its new name and, while that is unset, under the old
one (`setting`). The manifest's two names are `core/pack.py`'s (`manifest_name`), and the routes'
two spellings `server/paths.py`'s."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

OLD_PREFIX = "OPENHEALTH_"
NEW_PREFIX = "MENDARI_"
OLD_NAMESPACE = "openhealth/"
NEW_NAMESPACE = "mendari/"
# The settings renamed in 2026-11, each with the name it had before.
RENAMED_SETTINGS: Mapping[str, str] = {
    "MENDARI_AGENTS_PATH": "MENDARI_PACKS_PATH",
    "MENDARI_AGENTS": "MENDARI_PACKS",
    "MENDARI_AGENT_STORE": "MENDARI_PACK_STORE",
    "MENDARI_COMMUNITY_AGENTS": "MENDARI_COMMUNITY_PACKS",
}


def setting(name: str, environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """A renamed setting as the environment gives it: (the name it was read under, its value).
    Read under `name`, the current one; while that is unset, under the name it had before
    (`RENAMED_SETTINGS`); unset under both, (`name`, ""). The new name wins when both are set.
    An empty value is unset, as it is for every setting, so an image that declares the new name
    without a value still runs with a deployment that sets the old one. The name comes back so
    a refusal can say which variable holds the value it refuses."""
    environ = os.environ if environ is None else environ
    for known in (name, RENAMED_SETTINGS[name]):
        value = environ.get(known, "")
        if value.strip():
            return known, value
    return name, ""


def adopt_legacy_settings(environ: MutableMapping[str, str]) -> list[str]:
    """Copy every `OPENHEALTH_*` variable to its `MENDARI_*` name unless that is already set.
    Returns the new names it set."""
    adopted = []
    for name, value in list(environ.items()):
        if name.startswith(OLD_PREFIX):
            new = NEW_PREFIX + name.removeprefix(OLD_PREFIX)
            if new not in environ:
                environ[new] = value
                adopted.append(new)
    return sorted(adopted)


def current_pack_id(pack_id: str) -> str:
    """A pack id from before the rename (`openhealth/portal-message-agent`) as it is now
    (`mendari/portal-message-agent`); any other id unchanged."""
    if pack_id.startswith(OLD_NAMESPACE):
        return NEW_NAMESPACE + pack_id.removeprefix(OLD_NAMESPACE)
    return pack_id
