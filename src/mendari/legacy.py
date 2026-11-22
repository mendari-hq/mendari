# SPDX-License-Identifier: Apache-2.0
"""Mendari was called OpenHealth Hub until 2026-09-30. Deployments configured before the rename
keep working: a setting under the old `OPENHEALTH_` prefix is read as its `MENDARI_` name (the
`MENDARI_` name wins when both are set), and a setting that names a bundled agent by its old id
(`openhealth/…`) means the pack as it is now called (`mendari/…`)."""

from __future__ import annotations

from collections.abc import MutableMapping

OLD_PREFIX = "OPENHEALTH_"
NEW_PREFIX = "MENDARI_"
OLD_NAMESPACE = "openhealth/"
NEW_NAMESPACE = "mendari/"


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
