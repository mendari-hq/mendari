from __future__ import annotations

import json
import os
import secrets
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import AuditStore, Decision, Pack, load_pack
from mendari.server import sessions

ROOT = Path(__file__).resolve().parents[1]
INBOX_DIR = ROOT / "packs" / "patient-inbox-router"
LOCAL_DATASET = ROOT / "datasets" / "local-site-example.jsonl"
# Postgres tests run when this points at a disposable database, e.g.
# postgresql://postgres:mendari-test@127.0.0.1:56432/mendari_test (see CLAUDE.md).
POSTGRES_URL = os.environ.get("MENDARI_TEST_DATABASE_URL", "").strip()


@pytest.fixture(autouse=True)
def _own_hub_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test has its own Hub config and pack cache, never the developer's (the cache is on
    the pack search path)."""
    monkeypatch.setenv("MENDARI_CONFIG_DIR", str(tmp_path / "hub-config"))
    monkeypatch.setenv("MENDARI_CACHE_DIR", str(tmp_path / "hub-cache"))
    monkeypatch.delenv("MENDARI_REGISTRY_URL", raising=False)
    monkeypatch.delenv("MENDARI_REGISTRY_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _no_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests make many requests as one principal: rate limits are off unless a test turns them
    on (tests/test_limits.py), and no organization has a quota unless a test gives it one."""
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "off")
    monkeypatch.delenv("MENDARI_ORG_DAILY_LIMITS", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)


@pytest.fixture(params=["sqlite", "postgres"])
def db_target(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """An empty audit database: a SQLite file, or a fresh Postgres schema dropped afterwards."""
    if request.param == "sqlite":
        yield str(tmp_path / "audit.db")
        return
    if not POSTGRES_URL:
        pytest.skip("set MENDARI_TEST_DATABASE_URL to run the Postgres backend tests")
    import psycopg

    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in POSTGRES_URL else "?"
    try:
        yield f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def store(db_target: str) -> Iterator[AuditStore]:
    with AuditStore(db_target) as audit_store:
        yield audit_store


@pytest.fixture(scope="session")
def inbox_pack() -> Pack:
    return load_pack(INBOX_DIR)


@pytest.fixture(scope="session")
def router(inbox_pack: Pack) -> Decision:
    return Decision(inbox_pack)


@pytest.fixture
def pack_copy(tmp_path: Path) -> Callable[..., Path]:
    """Copy the reference pack and apply edits to pack.yaml / policy.yaml before loading."""

    def make(
        manifest: Callable[[dict[str, Any]], None] | None = None,
        policy: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        target = tmp_path / "pack"
        shutil.copytree(INBOX_DIR, target, ignore=shutil.ignore_patterns("data"))
        (target / "data").mkdir()
        for split in ("train", "calibration", "test"):
            # Tiny stand-ins: most tests do not need the real splits.
            source = (INBOX_DIR / "data" / f"{split}.jsonl").read_text().splitlines()[:20]
            (target / "data" / f"{split}.jsonl").write_text("\n".join(source) + "\n")
        for name, edit in (("pack.yaml", manifest), ("policy.yaml", policy)):
            if edit is None:
                continue
            path = target / name
            data = yaml.safe_load(path.read_text())
            edit(data)
            path.write_text(yaml.safe_dump(data, sort_keys=False))
        return target

    return make


def security_lines(stdout: str) -> list[dict[str, Any]]:
    """The security log's lines in captured stdout (server/security_log.py), parsed: each one
    must be a JSON object on its own line."""
    return [json.loads(line) for line in stdout.splitlines() if line.startswith('{"ts":')]


def without_request_id(body: dict[str, Any], response: Any) -> dict[str, Any]:
    """A JSON error body without its `request_id`, which must be the response's
    `X-Request-Id`."""
    assert body.get("request_id") == response.headers["x-request-id"]
    return {key: value for key, value in body.items() if key != "request_id"}


def message(body: str, **extra: Any) -> dict[str, Any]:
    return {"message": {"body": body, **extra}}


def signed_in(
    store: AuditStore, *, role: str, org: str = "default", host: str = "mendari.test"
) -> dict[str, str]:
    """Request headers for a person signed in with `role` in `org` on `host`: a session row as
    `/auth/callback` makes one (the chain names them `<role>@<org>.example.org`, a verified
    email), under the cookie name that host uses. Review is a person's (keys are viewer or
    service)."""
    sid = secrets.token_urlsafe(32)
    email = f"{role}@{org}.example.org"
    claims = {"roles": [role], "email": email, "email_verified": True}
    row = {"sub": f"{org}-{role}", "name": f"{org} {role}", "actor": email, "claims": claims}
    session, expires_at = sessions.begin(
        {**row, "roles": [role], "org": org}, int(time.time()), sessions.Lifetime()
    )
    store.smart_put(sid, "user", session, expires_at)
    return {"cookie": f"{sessions.cookie_name(sessions.SESSION, host)}={sid}"}
