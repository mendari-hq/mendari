# SPDX-License-Identifier: Apache-2.0
"""A running server honours its registries' recalls (LP-18e, `server/recalls.py`)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Decision
from mendari.cli import main
from mendari.core.origin import MARKER, ORIGIN_FORMAT
from mendari.core.spec import Outcome
from mendari.errors import MendariError
from mendari.hub import yank
from mendari.hub.client import agents_cache, pull
from mendari.server.app import create_app
from mendari.server.recalls import UNSIGNED, RecallWatch, fetch_signs, fetch_yanks
from tests.test_hub_client import Hub, copy_agent, hub  # noqa: F401 - the live registry fixture
from tests.test_registry_engine import BILL, outside_sorter

REGISTRY = "https://hub.example.org"


def no_key(_: str) -> bool:
    """A registry with no signing key."""
    return False


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _pulled(tmp_path: Path, registry: str | None = REGISTRY, **marked: Any) -> Any:
    agent = outside_sorter(tmp_path)
    record: dict[str, Any] = {"format": ORIGIN_FORMAT, **marked}
    if registry:
        record["registry"] = registry
    (agent.root / MARKER).write_text(json.dumps(record))
    return agent


def test_what_the_watch_holds_and_when(tmp_path: Path) -> None:
    agent = _pulled(tmp_path)
    listed: list[dict[str, Any]] = []
    clock = Clock()
    failing = [False]

    def fetch(registry: str) -> list[dict[str, Any]]:
        assert registry == REGISTRY
        if failing[0]:
            raise MendariError("unreachable")
        return listed

    watch = RecallWatch(
        [agent],
        fetch=fetch,
        fetch_key=no_key,
        clock=clock,
        check_s=300,
        stale_s=3600,
        background=False,
    )
    assert watch.held(agent) == "its registry has not been checked for recalls yet"
    watch.refresh(REGISTRY)
    assert watch.held(agent) is None
    listed.append({"agent": agent.id, "version": agent.version, "yank": {"severity": "high"}})
    watch.refresh(REGISTRY)
    assert watch.held(agent) == "recalled (high) by its registry"
    listed[0] = {"agent": agent.id, "version": agent.version, "yank": None, "deleted": {}}
    watch.refresh(REGISTRY)
    assert watch.held(agent) == "deleted by its registry"
    listed.clear()
    watch.refresh(REGISTRY)
    failing[0] = True
    clock.now += 3000
    watch.refresh(REGISTRY)  # a failed read keeps what was known until it is stale
    assert watch.held(agent) is None
    clock.now += 1000
    assert watch.held(agent) == "its registry could not be checked for recalls"
    assert watch.status() == {
        REGISTRY: {"checked_s_ago": 4000, "recalled": 0, "failing": "MendariError", "signs": False}
    }


@pytest.mark.parametrize(
    "unread",
    [
        {"id": "a/b", "version": "1.0.0", "yank": {"severity": "high"}},  # a key it does not read
        {"agent": "a/b", "yank": {"severity": "high"}},
        "a/b@1.0.0",
    ],
)
def test_a_recall_this_release_cannot_read_fails_the_read_and_never_lets_it_automate(
    tmp_path: Path, unread: Any
) -> None:
    """One item the feed lists that names no agent and version fails the whole read: what was
    known stands, and once it is stale every agent from the registry is held, so a recall in a
    shape this release does not read is never skipped while its version automates."""
    agent = _pulled(tmp_path)
    listed: list[Any] = [unread]
    clock = Clock()
    watch = RecallWatch(
        [agent], fetch=lambda _: listed, fetch_key=no_key, clock=clock, check_s=300,
        stale_s=3600, background=False,
    )  # fmt: skip
    watch.refresh(REGISTRY)
    assert watch.held(agent) == "its registry has not been checked for recalls yet"
    assert watch.status()[REGISTRY]["failing"] == "ValueError"
    listed[:] = [{"agent": agent.id, "version": agent.version, "yank": {"severity": "high"}}]
    watch.refresh(REGISTRY)
    assert watch.held(agent) == "recalled (high) by its registry"
    listed.append(unread)
    clock.now += 3000
    watch.refresh(REGISTRY)  # the recall read before stands
    assert watch.held(agent) == "recalled (high) by its registry"
    listed[:] = [unread]
    clock.now += 1000
    watch.refresh(REGISTRY)
    assert watch.held(agent) == "its registry could not be checked for recalls"


def test_an_agent_that_names_no_registry_is_held_and_others_are_not(tmp_path: Path) -> None:
    unnamed = _pulled(tmp_path / "a", registry=None)
    watch = RecallWatch(
        [unnamed], fetch=lambda _: [], fetch_key=no_key, check_s=300, stale_s=3600, background=False
    )
    assert watch.held(unnamed) == "its registry is not recorded beside it: pull it again"
    owned = outside_sorter(tmp_path / "b")
    owned = type(owned)(**{**owned.__dict__, "origin": "local"})
    assert watch.held(owned) is None


def test_a_watched_recall_stops_automation_and_its_own_block(tmp_path: Path) -> None:
    agent = _pulled(tmp_path)
    watch = RecallWatch(
        [agent], fetch=lambda _: [], fetch_key=no_key, check_s=300, stale_s=3600, background=False
    )
    decision = Decision(agent, origin_approval=lambda _: True, recall_source=watch.held)
    assert decision.decide(BILL).outcome is Outcome.HUMAN_REVIEW  # not checked yet
    watch.refresh(REGISTRY)
    assert decision.decide(BILL).outcome is Outcome.AUTO


def test_a_registry_is_read_over_https_only() -> None:
    with pytest.raises(MendariError, match="https"):
        fetch_yanks("http://hub.example.org")
    with pytest.raises(MendariError, match="https"):
        fetch_signs("http://hub.example.org")


def test_once_its_registry_has_a_key_a_copy_it_never_counter_signed_is_held(
    tmp_path: Path,
) -> None:
    """LP-19i's known limit, closed for servers (owner, 2026-11-10): a copy pulled while its
    registry had no key (`accepted: null`), or before LP-19i (no `accepted`), stops automating
    once the registry publishes a key, until it is pulled again counter-signed. A registry with
    no key changes nothing, and a read that fails keeps what was known."""
    unsigned = _pulled(tmp_path / "a", accepted=None)
    older = _pulled(tmp_path / "b")
    signed = _pulled(tmp_path / "c", accepted=True)
    clock = Clock()
    signs, failing = [False], [False]

    def fetch_key(registry: str) -> bool:
        if failing[0]:
            raise MendariError("unreachable")
        return signs[0]

    watch = RecallWatch(
        [unsigned, older, signed], fetch=lambda _: [], fetch_key=fetch_key, clock=clock,
        check_s=300, stale_s=3600, background=False,
    )  # fmt: skip
    watch.refresh(REGISTRY)
    assert [watch.held(p) for p in (unsigned, older, signed)] == [None, None, None]
    signs[0] = True
    watch.refresh(REGISTRY)
    assert [watch.held(p) for p in (unsigned, older, signed)] == [UNSIGNED, UNSIGNED, None]
    assert watch.status()[REGISTRY]["signs"] is True
    failing[0] = True
    clock.now += 600
    watch.refresh(REGISTRY)  # unreachable: what was known stands until it is stale
    assert watch.held(unsigned) == UNSIGNED
    assert watch.held(signed) is None
    failing[0], signs[0] = False, False
    watch.refresh(REGISTRY)  # a registry that drops its key: nothing it lists is held for it
    assert watch.held(unsigned) is None
    # The copy is pulled again, counter-signed: the marker beside it now says so, and the same
    # agent, as the server loaded it, automates again.
    signs[0] = True
    watch.refresh(REGISTRY)
    assert watch.held(unsigned) == UNSIGNED
    (unsigned.root / MARKER).write_text(
        json.dumps({"format": ORIGIN_FORMAT, "registry": REGISTRY, "accepted": True})
    )
    assert watch.held(unsigned) is None
    assert unsigned.root.is_dir()  # never removed


def test_a_recall_is_said_before_a_missing_counter_signature(tmp_path: Path) -> None:
    agent = _pulled(tmp_path, accepted=None)
    listed = [{"agent": agent.id, "version": agent.version, "yank": {"severity": "low"}}]
    watch = RecallWatch(
        [agent], fetch=lambda _: listed, fetch_key=lambda _: True, check_s=300, stale_s=3600,
        background=False,
    )  # fmt: skip
    watch.refresh(REGISTRY)
    assert watch.held(agent) == "recalled (low) by its registry"


def test_a_running_server_stops_automating_an_agent_its_registry_recalls(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, key = hub.namespace("someone")
    source = outside_sorter(tmp_path / "source")
    hub.client(token).push(source, key)
    pulled = pull("someone/visit-sorter", hub.client())
    assert pulled.pulled["registry"] == hub.url
    assert pulled.root.is_relative_to(agents_cache())
    monkeypatch.setenv("MENDARI_AGENTS", "someone/visit-sorter")
    with TestClient(create_app(db_path=tmp_path / "engine.db")) as client:
        watch = client.app.state.recall_watch  # type: ignore[attr-defined]
        watch.refresh(hub.url)
        body = {
            "agent_id": pulled.id,
            "agent_digest": pulled.digest,
            "reference": "minutes 9",
            "approver": "dr-a",
        }
        assert client.post("/api/registry-approvals", json=body).status_code == 201
        decide = "/api/agents/someone/visit-sorter/decide"
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


def test_a_running_server_holds_a_copy_its_registry_never_counter_signed(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    db_target: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On SQLite and Postgres: pulled while its registry has no key, the agent automates once
    approved; when the registry gets a key, the running server holds it with a reason that
    says to pull it again; pulled again, counter-signed, it automates as before."""
    monkeypatch.delenv(yank.KEY_ENV, raising=False)
    token, key = hub.namespace("someone")
    hub.client(token).push(outside_sorter(tmp_path / "source"), key)
    pulled = pull("someone/visit-sorter", hub.client())
    assert pulled.pulled["accepted"] is None
    monkeypatch.setenv("MENDARI_AGENTS", "someone/visit-sorter")
    decide = "/api/agents/someone/visit-sorter/decide"
    with TestClient(create_app(db_path=db_target)) as client:
        watch = client.app.state.recall_watch  # type: ignore[attr-defined]
        approval = {
            "agent_id": pulled.id,
            "agent_digest": pulled.digest,
            "reference": "minutes 9",
            "approver": "dr-a",
        }
        assert client.post("/api/registry-approvals", json=approval).status_code == 201
        watch.refresh(hub.url)  # a registry with no key: nothing changes
        assert client.post(decide, json=BILL).json()["outcome"] == "AUTO"
        monkeypatch.setenv(yank.KEY_ENV, base64.b64encode(os.urandom(32)).decode())
        watch.refresh(hub.url)
        held = client.post(decide, json=BILL).json()
        assert held["outcome"] == "HUMAN_REVIEW"
        assert any(UNSIGNED in reason for reason in held["reasons"])
        assert client.get("/api/health").json()["registry_recalls"][hub.url]["signs"] is True
        assert pulled.root.is_dir()  # held, never removed
        assert main(["registry", "countersign", "--db", str(tmp_path / "registry.db")]) == 0
        again = pull("someone/visit-sorter", hub.client())
        assert again.root == pulled.root and again.pulled["accepted"] is True
        assert client.post(decide, json=BILL).json()["outcome"] == "AUTO"
    shutil.rmtree(pulled.root)


class _Registry(BaseHTTPRequestHandler):
    """A registry on this machine that answers `/<case>/v1/yanks` as each case says."""

    def do_GET(self) -> None:
        case = self.path.split("/")[1]
        if case == "drip":
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(b"[")
                for _ in range(40):
                    time.sleep(0.05)
                    self.wfile.write(b" ")
                    self.wfile.flush()
                self.wfile.write(b"]")
            except (BrokenPipeError, ConnectionResetError):  # the reader gave up, as it should
                pass
            return
        if self.path.endswith("/v1/registry-key"):
            status = {"keyed": 200, "keyless": 404, "moved": 302}.get(case, 500)
            self.send_response(status)
            if case == "moved":
                self.send_header("location", "http://169.254.169.254/")
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        body = {
            "ok": b'[{"agent": "a/b", "version": "1.0.0"}, 3]',
            "list": b'{"not": "a list"}',
            "huge": b"[" + b" " * 6_000_000 + b"]",
            "moved": b"[]",
        }.get(case, b"")
        status = {"moved": 302, "error": 500}.get(case, 200)
        self.send_response(status)
        if case == "moved":
            self.send_header("location", "http://169.254.169.254/")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def local_registry() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_a_registrys_answer_is_bounded_in_size_time_and_shape(local_registry: str) -> None:
    """The LP-18e review: a read had no total time limit and buffered any size before the cap.
    Now the whole answer has a deadline and a size cap, a redirect is never followed, and only a
    list of objects is read."""
    assert fetch_yanks(f"{local_registry}/ok") == [{"agent": "a/b", "version": "1.0.0"}]
    for case, problem in (
        ("error", "HTTP 500"),
        ("moved", "HTTP 302"),
        ("list", "not a list"),
        ("huge", "too large"),
    ):
        with pytest.raises(MendariError, match=problem):
            fetch_yanks(f"{local_registry}/{case}")
    started = time.monotonic()
    with pytest.raises(MendariError, match="took too long"):
        fetch_yanks(f"{local_registry}/drip", deadline_s=0.5)
    assert time.monotonic() - started < 1.9


def test_whether_a_registry_has_a_key_is_read_from_its_status_only(local_registry: str) -> None:
    assert fetch_signs(f"{local_registry}/keyed") is True
    assert fetch_signs(f"{local_registry}/keyless") is False
    for case, problem in (("error", "HTTP 500"), ("moved", "HTTP 302")):
        with pytest.raises(MendariError, match=problem):
            fetch_signs(f"{local_registry}/{case}")


def test_a_lost_read_is_started_again_and_one_reads_at_a_time(tmp_path: Path) -> None:
    agent = _pulled(tmp_path)
    clock = Clock()
    calls: list[str] = []
    gate = threading.Event()

    def fetch(registry: str) -> list[dict[str, Any]]:
        calls.append(registry)
        gate.wait(5)
        return []

    watch = RecallWatch(
        [agent], fetch=fetch, fetch_key=no_key, clock=clock, check_s=300, stale_s=3600
    )
    for _ in range(5):
        watch.held(agent)  # one background read in flight, however many decisions
    time.sleep(0.2)
    assert len(calls) == 1
    clock.now += 61  # that read is taken as lost: another starts
    watch.held(agent)
    time.sleep(0.2)
    assert len(calls) == 2
    gate.set()


def test_the_settings_are_checked_together(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(MendariError, match="at least twice"):
        RecallWatch([], check_s=300, stale_s=400)
    monkeypatch.setenv("MENDARI_REGISTRY_RECALL_CHECK_S", "0")
    with pytest.raises(MendariError, match="from 1 to 604800"):
        RecallWatch([])
