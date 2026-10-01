"""Mendari was called OpenHealth Hub until 2026-09-30. Deployments configured before the rename
keep working: a setting under the old `OPENHEALTH_` prefix is read as its `MENDARI_` name, and
the `MENDARI_` name wins when both are set."""

from __future__ import annotations

from collections.abc import MutableMapping

OLD_PREFIX = "OPENHEALTH_"
NEW_PREFIX = "MENDARI_"


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
