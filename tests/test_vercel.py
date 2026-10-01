from __future__ import annotations

import pytest

from mendari.errors import MendariError
from mendari.server.vercel import database_target


def test_database_url_is_used_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    assert database_target() == "postgresql://example/db"


def test_vercel_refuses_to_run_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(MendariError, match="DATABASE_URL is not set"):
        database_target()


def test_local_runs_fall_back_to_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    assert database_target() == "var/mendari.db"
