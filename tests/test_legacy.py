# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mendari.legacy import adopt_legacy_settings, current_pack_id
from mendari.settings import enabled_packs, parse_providers, providers_for


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


def test_old_pack_ids_mean_the_renamed_packs() -> None:
    assert current_pack_id("openhealth/referral-router") == "mendari/referral-router"
    assert current_pack_id("mendari/referral-router") == "mendari/referral-router"
    assert current_pack_id("acme/openhealth/x") == "acme/openhealth/x"


def test_pack_scoped_provider_settings_from_before_the_rename_still_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MENDARI_PROVIDERS", "router=jev,openhealth/referral-router:specialty_reader=jev"
    )
    settings = parse_providers(None)
    assert settings == {"router": "jev", "mendari/referral-router:specialty_reader": "jev"}
    slots = {"specialty_reader": {"naive_bayes", "jev"}}
    assert providers_for("mendari/referral-router", slots, settings) == (
        {"specialty_reader": "jev"},
        [],
    )


def test_enabled_packs_from_before_the_rename_still_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MENDARI_PACKS", "openhealth/referral-router, mendari/ai-guard")
    assert enabled_packs() == {"mendari/referral-router", "mendari/ai-guard"}
