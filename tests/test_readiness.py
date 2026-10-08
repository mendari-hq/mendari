# SPDX-License-Identifier: Apache-2.0
"""Readiness (`GET /api/health/ready`, LP-21 requirement 1, `server/readiness.py`): one bounded
database read per instance every few seconds, the packs as start-up found them, an in-memory
limit per address, fixed codes only; liveness (`/api/health`) stays three fields and no
database work."""

from __future__ import annotations

import logging
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

from mendari.core import db as db_module
from mendari.core.registry import SearchPath
from mendari.server import readiness
from mendari.server.app import create_app
from mendari.server.readiness import Readiness, packs_state
from tests.test_auth import FakeIdp, signing_in_with

READY = "/api/health/ready"
PACKS = Path(__file__).parents[1] / "packs"
# A driver's message can carry the host, the user and the database: none of it may leave.
SECRET_TEXT = 'connection to server at "db.internal.example" (10.9.8.7) failed: user "mendari"'


@pytest.fixture
def pings(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every database ping from here on, as its timeout."""
    seen: list[float] = []
    for backend in (db_module.SqliteBackend, db_module.PostgresBackend):

        def spy(self: Any, sql: str, timeout_s: float, _original: Any = backend.ping) -> None:
            seen.append(timeout_s)
            _original(self, sql, timeout_s)

        monkeypatch.setattr(backend, "ping", spy)
    return seen


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Every database session opened from here on (the limiter's, a credential's lookup)."""
    opened: list[bool] = []
    for backend in (db_module.SqliteBackend, db_module.PostgresBackend):

        def spy(self: Any, *, exclusive: bool, _original: Any = backend.session) -> Any:
            opened.append(exclusive)
            return _original(self, exclusive=exclusive)

        monkeypatch.setattr(backend, "session", spy)
    return opened


def break_sqlite(path: Path, how: str) -> None:
    """The database goes away under a running server: its file overwritten, or deleted."""
    for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        sidecar.unlink(missing_ok=True)
    if how == "garbage":
        path.write_bytes(b"not a database " * 512)
    else:
        path.unlink()


class Relay:
    """A TCP relay to the test Postgres that can be cut, as a network outage would: the server
    starts through it; cut, every connection, open or new, is dropped; restored, it reconnects
    (so its pool is left as healthy as any other test's)."""

    def __init__(self, host: str, port: int):
        self.upstream = (host, port)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.sockets: list[socket.socket] = []
        self.down = False
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return  # closed
            if self.down:
                client.close()
                continue
            server = socket.create_connection(self.upstream)
            with self.lock:
                self.sockets += [client, server]
            for source, sink in ((client, server), (server, client)):
                threading.Thread(target=self._pump, args=(source, sink), daemon=True).start()

    @staticmethod
    def _pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for end in (source, sink):
                end.close()

    def url(self, target: str) -> str:
        parts = urlsplit(target)
        user = parts.netloc.rpartition("@")[0]
        netloc = f"{user}@127.0.0.1:{self.port}" if user else f"127.0.0.1:{self.port}"
        return urlunsplit(parts._replace(netloc=netloc))

    def cut(self) -> None:
        with self.lock:
            self.down = True
            for end in self.sockets:
                end.close()
            self.sockets.clear()

    def restore(self) -> None:
        self.down = False


@contextmanager
def server_on(db_target: str, tmp_path: Path) -> Iterator[tuple[TestClient, Any]]:
    """A server on `db_target`, and how to take its database away (`break_db(how)`)."""
    if db_target.startswith("postgresql"):
        parts = urlsplit(db_target)
        relay = Relay(parts.hostname or "localhost", parts.port or 5432)
        try:
            yield TestClient(create_app(db_path=relay.url(db_target))), lambda how: relay.cut()
        finally:
            relay.restore()
        return
    path = Path(db_target)
    yield TestClient(create_app(db_path=path)), lambda how: break_sqlite(path, how)


# -- the acceptance test -----------------------------------------------------------------------


@pytest.mark.parametrize("how", ["garbage", "deleted"])
def test_with_the_database_unreachable_the_readiness_route_fails(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    monkeypatch.setattr(readiness, "READY_SECONDS", 0.0)  # every request checks again
    monkeypatch.setattr(readiness, "READY_TIMEOUT_S", 1.0)
    with server_on(db_target, tmp_path) as (client, break_db):
        answer = client.get(READY)
        assert answer.status_code == 200
        assert answer.json()["status"] == "ready"
        assert answer.json()["checks"] == {"database": "ok", "packs": "ok"}
        break_db(how)
        refused = client.get(READY)
        assert refused.status_code == 503
        assert refused.json()["status"] == "unready"
        assert refused.json()["checks"] == {"database": "unreachable", "packs": "ok"}
        assert refused.headers["cache-control"] == "no-store"
        assert client.head(READY).status_code == 503
    if how == "deleted" and not db_target.startswith("postgresql"):
        assert not Path(db_target).exists()  # the check never creates an empty database


def test_liveness_stays_up_through_the_outage_that_readiness_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With sign-in on, anonymous `/api/health` does no database work, so a database outage
    leaves it green: that is why status checks call readiness."""
    monkeypatch.setattr(readiness, "READY_SECONDS", 0.0)
    with signing_in_with(FakeIdp(), tmp_path, monkeypatch) as client:
        break_sqlite(tmp_path / "a.db", "garbage")
        assert client.get("/api/health").status_code == 200
        assert client.get(READY).status_code == 503


# -- one read per window -----------------------------------------------------------------------


def test_readiness_reads_the_database_once_per_window_whoever_asks(
    tmp_path: Path, pings: list[float], sessions: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    sessions.clear()
    for _ in range(10):
        assert client.get(READY).status_code == 200
    assert pings == [readiness.READY_TIMEOUT_S]  # one bounded read, kept for the window
    assert sessions == []  # and nothing else: no limiter row, no credential lookup
    monkeypatch.setattr(readiness, "READY_SECONDS", 0.0)
    client.get(READY)
    client.head(READY)
    assert len(pings) == 3  # a window that has passed is read again


def test_callers_during_a_check_wait_for_it_and_a_failure_is_kept_too() -> None:
    calls: list[float] = []
    release = threading.Event()

    def slow_and_down(timeout_s: float) -> None:
        calls.append(timeout_s)
        release.wait(5)
        raise OSError("down")

    check = Readiness(slow_and_down, "ok")
    answers: list[int] = []
    threads = [
        threading.Thread(target=lambda: answers.append(check.answer().status_code))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    release.set()
    for thread in threads:
        thread.join(5)
    assert calls == [readiness.READY_TIMEOUT_S]  # one check for all eight
    assert answers == [503] * 8
    assert check.answer().status_code == 503 and len(calls) == 1  # the failure is kept


# -- what it says, and what it never says -----------------------------------------------------


def test_readiness_tells_no_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db"))

    def down(self: Any, sql: str, timeout_s: float) -> None:
        raise ConnectionRefusedError(SECRET_TEXT)

    monkeypatch.setattr(db_module.SqliteBackend, "ping", down)
    with caplog.at_level(logging.WARNING, logger="mendari.ready"):
        answer = client.get(READY)
    assert answer.status_code == 503
    assert answer.json() == {
        "status": "unready",
        "version": answer.json()["version"],
        "checks": {"database": "unreachable", "packs": "ok"},
    }
    for text in (answer.text, caplog.text):
        assert "db.internal" not in text and "10.9.8.7" not in text and 'user "' not in text
    assert "ConnectionRefusedError" in caplog.text  # the operator learns the kind of failure


def test_packs_that_did_not_load_make_readiness_degraded_and_no_pack_unready(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "broken"
    shutil.copytree(PACKS / "patient-inbox-router", broken)
    (broken / "fhir" / "mapping.yaml").write_text("fhir_version: R5\n")  # no terminology
    served = [SearchPath(broken, "maintained"), SearchPath(PACKS / "ai-guard", "maintained")]
    degraded = TestClient(create_app(db_path=tmp_path / "a.db", packs_paths=served))
    answer = degraded.get(READY)
    assert answer.status_code == 200
    assert answer.json()["status"] == "degraded"
    assert answer.json()["checks"] == {"database": "ok", "packs": "degraded"}
    assert "patient-inbox-router" not in answer.text  # signed-in health names it, not this

    (tmp_path / "empty").mkdir()
    nothing = create_app(db_path=tmp_path / "b.db", packs_paths=[tmp_path / "empty"])
    answer = TestClient(nothing).get(READY)
    assert answer.status_code == 503
    assert answer.json()["checks"] == {"database": "ok", "packs": "none"}


def test_packs_state() -> None:
    assert packs_state(built=3, problems=0) == "ok"
    assert packs_state(built=3, problems=1) == "degraded"
    assert packs_state(built=0, problems=0) == "none"
    assert packs_state(built=0, problems=2) == "none"


def test_head_answers_as_get_does(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    head = client.head(READY)
    assert head.status_code == client.get(READY).status_code == 200
    assert head.content == b""


def test_readiness_is_public_with_sign_in_and_looks_up_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sessions: list[bool]
) -> None:
    with signing_in_with(FakeIdp(), tmp_path, monkeypatch) as client:
        sessions.clear()
        assert client.get(READY).status_code == 200
        junk = client.get(READY, headers={"authorization": "Bearer ohk_unknown"})
        assert junk.status_code == 200  # a public route reads no credential
        assert client.head(READY).status_code == 200
        assert sessions == []


# -- the limit ---------------------------------------------------------------------------------


def test_readiness_is_limited_per_address_in_memory_through_an_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sessions: list[bool]
) -> None:
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,ready=2/min")
    monkeypatch.setattr(readiness, "READY_SECONDS", 0.0)
    app = create_app(db_path=tmp_path / "a.db")
    client = TestClient(app, client=("10.0.0.1", 50000))
    break_sqlite(tmp_path / "a.db", "garbage")
    sessions.clear()
    assert [client.get(READY).status_code for _ in range(2)] == [503, 503]
    over = client.get(READY)
    assert over.status_code == 429  # refused by the limit, not a 500 from a database limiter
    assert int(over.headers["retry-after"]) >= 1
    assert sessions == []  # the limit counted in memory
    other = TestClient(app, client=("10.0.0.2", 50000))
    assert other.get(READY).status_code == 503  # another address, its own bucket


def test_readiness_is_not_limited_when_limits_are_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "off,ready=1/min")
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert [client.get(READY).status_code for _ in range(3)] == [200, 200, 200]


def test_health_shows_the_readiness_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,ready=120/min:60")
    health = TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/health").json()
    assert health["rate_limits"]["limits"]["ready"] == "120/min:60"
