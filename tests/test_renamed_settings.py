# SPDX-License-Identifier: Apache-2.0
"""The four settings named for Decision Packs until 2026-11 (`legacy.RENAMED_SETTINGS`): each is
read under its new name and, while that is unset, under the old one; the new one wins when both
are set; and what ships in the repository (the image, the compose files, the workflows, the
scripts) sets the new names only."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mendari import legacy
from mendari.core.registry import search_paths
from mendari.errors import MendariError
from mendari.legacy import RENAMED_SETTINGS, setting
from mendari.server.app import create_app
from mendari.settings import community_packs_enabled, enabled_packs, pack_store_on
from tests.conftest import INBOX_DIR, ROOT

NAMES = [
    ("MENDARI_AGENTS_PATH", "MENDARI_PACKS_PATH"),
    ("MENDARI_AGENTS", "MENDARI_PACKS"),
    ("MENDARI_AGENT_STORE", "MENDARI_PACK_STORE"),
    ("MENDARI_COMMUNITY_AGENTS", "MENDARI_COMMUNITY_PACKS"),
]


@pytest.fixture(autouse=True)
def _neither_name_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for new, old in NAMES:
        monkeypatch.delenv(new, raising=False)
        monkeypatch.delenv(old, raising=False)


def test_the_renamed_settings_are_the_plans_four() -> None:
    assert list(RENAMED_SETTINGS.items()) == NAMES


@pytest.mark.parametrize("new, old", NAMES)
def test_one_helper_reads_the_new_name_then_the_old(new: str, old: str) -> None:
    assert setting(new, {}) == (new, "")
    assert setting(new, {old: "before"}) == (old, "before")
    assert setting(new, {new: "now"}) == (new, "now")
    assert setting(new, {new: "now", old: "before"}) == (new, "now")  # the new name wins
    # An empty value is unset, as for every setting: an image that declares the new name with
    # no value still runs with a deployment that sets the old one.
    assert setting(new, {new: "", old: "before"}) == (old, "before")
    assert setting(new, {new: "  ", old: "before"}) == (old, "before")
    assert setting(new, {new: "", old: " "}) == (new, "")
    with pytest.raises(KeyError):
        setting(old, {old: "before"})  # asked for by its current name only


def test_the_agents_served(monkeypatch: pytest.MonkeyPatch) -> None:
    assert enabled_packs() == frozenset()
    monkeypatch.setenv("MENDARI_PACKS", "a/one, openhealth/referral-router")
    assert enabled_packs() == {"a/one", "mendari/referral-router"}
    monkeypatch.setenv("MENDARI_AGENTS", "b/two")
    assert enabled_packs() == {"b/two"}


@pytest.mark.parametrize("read", [community_packs_enabled, pack_store_on])
def test_a_switch_is_read_under_either_name_and_a_refusal_names_the_one_read(
    monkeypatch: pytest.MonkeyPatch, read: object
) -> None:
    new, old = {
        community_packs_enabled: ("MENDARI_COMMUNITY_AGENTS", "MENDARI_COMMUNITY_PACKS"),
        pack_store_on: ("MENDARI_AGENT_STORE", "MENDARI_PACK_STORE"),
    }[read]  # type: ignore[index]
    assert callable(read) and read() is False
    monkeypatch.setenv(old, "on")
    assert read() is True
    monkeypatch.setenv(new, "off")  # the new name wins
    assert read() is False
    monkeypatch.setenv(new, "On")
    monkeypatch.setenv(old, "off")
    assert read() is True
    monkeypatch.setenv(new, "yes")
    with pytest.raises(MendariError, match=f"^{new} "):
        read()
    monkeypatch.delenv(new)
    monkeypatch.setenv(old, "yes")
    with pytest.raises(MendariError, match=f"^{old} "):
        read()


def test_the_search_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before, now = tmp_path / "before", tmp_path / "now"
    monkeypatch.setenv("MENDARI_PACKS_PATH", str(before))
    assert search_paths()[0].path == before
    monkeypatch.setenv("MENDARI_AGENTS_PATH", os.pathsep.join([str(now), str(tmp_path / "too")]))
    assert [entry.path for entry in search_paths()[:2]] == [now, tmp_path / "too"]
    assert before not in [entry.path for entry in search_paths()]  # never both lists


def test_a_server_serves_what_either_name_asks_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served = tmp_path / "served" / "inbox"
    shutil.copytree(INBOX_DIR, served)
    text = (served / "pack.yaml").read_text()
    (served / "pack.yaml").write_text(
        text.replace("id: mendari/patient-inbox-router", "id: my-clinic/inbox", 1)
    )
    for path_name, only_name in (
        ("MENDARI_PACKS_PATH", "MENDARI_PACKS"),
        ("MENDARI_AGENTS_PATH", "MENDARI_AGENTS"),
        ("MENDARI_AGENTS_PATH", "MENDARI_PACKS"),
    ):
        for new, old in NAMES:
            monkeypatch.delenv(new, raising=False)
            monkeypatch.delenv(old, raising=False)
        monkeypatch.setenv(path_name, str(served.parent))
        monkeypatch.setenv(only_name, "my-clinic/inbox")
        app = create_app(db_path=tmp_path / f"{path_name}-{only_name}.db")
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            assert [agent["id"] for agent in client.get("/api/packs").json()] == [
                "my-clinic/inbox"
            ]
    monkeypatch.setenv("MENDARI_AGENTS", "my-clinic/absent")
    with pytest.raises(MendariError, match=r"^MENDARI_AGENTS: agents not installed here"):
        create_app(db_path=tmp_path / "absent.db")
    monkeypatch.delenv("MENDARI_AGENTS")
    monkeypatch.setenv("MENDARI_PACKS", "my-clinic/absent")
    with pytest.raises(MendariError, match=r"^MENDARI_PACKS: agents not installed here"):
        create_app(db_path=tmp_path / "absent-too.db")


def test_a_setting_from_before_both_renames_is_still_read() -> None:
    """`OPENHEALTH_PACKS` (before 2026-09-30) is adopted as `MENDARI_PACKS` when the package is
    imported, which is read while `MENDARI_AGENTS` is unset."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MENDARI_", "OPENHEALTH_"))}
    env["OPENHEALTH_PACKS"] = "openhealth/referral-router"
    code = "from mendari.settings import enabled_packs; print(sorted(enabled_packs()))"
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "['mendari/referral-router']"


SHIPPED = (
    "Dockerfile",
    "docker-compose.yml",
    "deploy",
    "sandbox",
    "scripts",
    ".github/workflows",
    "vercel.json",
    "app.py",
)


def test_what_the_repository_ships_sets_the_new_names_only() -> None:
    """The image, the compose files, the deploy templates, the sandbox, the scripts and the
    workflows: none sets or names a setting by the name it had before."""
    old_names = [old for _, old in NAMES]
    found = []
    for name in SHIPPED:
        path = ROOT / name
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for file in files:
            if file.suffix in (".png", ".jpg", ".ico", ".woff2", ".gz", ".pyc"):
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
            for old in old_names:
                # `MENDARI_PACKS` is the start of `MENDARI_PACKS_PATH`: whole names only.
                if any(
                    not (after[:1].isalnum() or after[:1] == "_") for after in text.split(old)[1:]
                ):
                    found.append(f"{file.relative_to(ROOT)}: {old}")
    assert not found, found


def test_the_legacy_module_says_what_it_keeps() -> None:
    assert "RENAMED_SETTINGS" in (legacy.__doc__ or "")
