from __future__ import annotations

import os
import subprocess
import sys

from mendari.legacy import adopt_legacy_settings


def test_old_prefix_settings_are_read_under_the_new_name() -> None:
    environ = {"OPENHEALTH_PHI_MODE": "encrypt", "OPENHEALTH_TIMEZONE": "UTC", "PATH": "/bin"}
    assert adopt_legacy_settings(environ) == ["MENDARI_PHI_MODE", "MENDARI_TIMEZONE"]
    assert environ["MENDARI_PHI_MODE"] == "encrypt"
    assert environ["MENDARI_TIMEZONE"] == "UTC"


def test_the_new_name_wins_when_both_are_set() -> None:
    environ = {"OPENHEALTH_TIMEZONE": "UTC", "MENDARI_TIMEZONE": "America/Chicago"}
    assert adopt_legacy_settings(environ) == []
    assert environ["MENDARI_TIMEZONE"] == "America/Chicago"


def test_importing_the_package_adopts_old_settings_before_they_are_read() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MENDARI_", "OPENHEALTH_"))}
    env["OPENHEALTH_TIMEZONE"] = "America/Chicago"
    code = "from mendari.settings import display_timezone; print(display_timezone())"
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "America/Chicago"
