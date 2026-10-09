# SPDX-License-Identifier: Apache-2.0
"""A running server honours its registries' recalls (LP-18e, `server/recalls.py`)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Decision
from mendari.core.origin import MARKER, ORIGIN_FORMAT
from mendari.core.spec import Outcome
from mendari.errors import MendariError
from mendari.hub.client import packs_cache, pull
from mendari.server.app import create_app
from mendari.server.recalls import RecallWatch, fetch_yanks
from tests.test_hub_client import Hub, copy_pack, hub  # noqa: F401 - the live registry fixture
from tests.test_registry_engine import BILL, outside_sorter

REGISTRY = "https://hub.example.org"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _pulled(tmp_path: Path, registry: str | None = REGISTRY) -> Any:
    pack = outside_sorter(tmp_path)
    record: dict[str, Any] = {"format": ORIGIN_FORMAT}
    if registry:
        record["registry"] = registry
    (pack.root / MARKER).write_text(json.dumps(record))
    return pack


def test_what_the_watch_holds_and_when(tmp_path: Path) -> None:
    pack = _pulled(tmp_path)
    listed: list[dict[str, Any]] = []
    clock = Clock()
    failing = [False]

    def fetch(registry: str) -> list[dict[str, Any]]:
        assert registry == REGISTRY
        if failing[0]:
            raise MendariError("unreachable")
        return listed

    watch = RecallWatch(
        [pack], fetch=fetch, clock=clock, check_s=300, stale_s=3600, background=False
    )
    assert watch.held(pack) == "its registry has not been checked for recalls yet"
    watch.refresh(REGISTRY)
    assert watch.held(pack) is None
    listed.append({"pack": pack.id, "version": pack.version, "yank": {"severity": "high"}})
    watch.refresh(REGISTRY)
    assert watch.held(pack) == "recalled (high) by its registry"
    listed[0] = {"pack": pack.id, "version": pack.version, "yank": None, "deleted": {}}
    watch.refresh(REGISTRY)
    assert watch.held(pack) == "deleted by its registry"
    listed.clear()
    watch.refresh(REGISTRY)
    failing[0] = True
    clock.now += 3000
    watch.refresh(REGISTRY)  # a failed read keeps what was known until it is stale
    assert watch.held(pack) is None
    clock.now += 1000
    assert watch.held(pack) == "its registry could not be checked for recalls"
    assert watch.status() == {REGISTRY: {"checked_s_ago": 4000, "recalled": 0}}


def test_a_pack_that_names_no_registry_is_held_and_others_are_not(tmp_path: Path) -> None:
    unnamed = _pulled(tmp_path / "a", registry=None)
    watch = RecallWatch([unnamed], fetch=lambda _: [], check_s=300, stale_s=3600, background=False)
    assert watch.held(unnamed) == "its registry is not recorded beside it: pull it again"
    owned = outside_sorter(tmp_path / "b")
    owned = type(owned)(**{**owned.__dict__, "origin": "local"})
    assert watch.held(owned) is None


def test_a_watched_recall_stops_automation_and_its_own_block(tmp_path: Path) -> None:
    pack = _pulled(tmp_path)
    watch = RecallWatch([pack], fetch=lambda _: [], check_s=300, stale_s=3600, background=False)
    decision = Decision(pack, origin_approval=lambda _: True, recall_source=watch.held)
    assert decision.decide(BILL).outcome is Outcome.HUMAN_REVIEW  # not checked yet
    watch.refresh(REGISTRY)
    assert decision.decide(BILL).outcome is Outcome.AUTO


def test_a_registry_is_read_over_https_only() -> None:
    with pytest.raises(MendariError, match="https"):
        fetch_yanks("http://hub.example.org")


def test_a_running_server_stops_automating_a_pack_its_registry_recalls(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, key = hub.namespace("someone")
    source = outside_sorter(tmp_path / "source")
    hub.client(token).push(source, key)
    pulled = pull("someone/visit-sorter", hub.client())
    assert pulled.pulled["registry"] == hub.url
    assert pulled.root.is_relative_to(packs_cache())
    monkeypatch.setenv("MENDARI_PACKS", "someone/visit-sorter")
    with TestClient(create_app(db_path=tmp_path / "engine.db")) as client:
        watch = client.app.state.recall_watch  # type: ignore[attr-defined]
        watch.refresh(hub.url)
        body = {
            "pack_id": pulled.id,
            "pack_digest": pulled.digest,
            "reference": "minutes 9",
            "approver": "dr-a",
        }
        assert client.post("/api/registry-approvals", json=body).status_code == 201
        decide = "/api/packs/someone/visit-sorter/decide"
        assert client.post(decide, json=BILL).json()["outcome"] == "AUTO"
        hub.client(token).yank(
            "someone", "visit-sorter", pulled.version, reason="misroutes", severity="high", key=key
        )
        watch.refresh(hub.url)
        recalled = client.post(decide, json=BILL).json()
        assert recalled["outcome"] == "HUMAN_REVIEW"
        assert any("recalled (high) by its registry" in r for r in recalled["reasons"])
        health = client.get("/api/health").json()
        assert health["registry_recalls"][hub.url]["recalled"] == 1
    shutil.rmtree(pulled.root)
