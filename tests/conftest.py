from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from openhealth import AuditStore, Decision, Pack, load_pack

ROOT = Path(__file__).resolve().parents[1]
INBOX_DIR = ROOT / "packs" / "patient-inbox-router"
LOCAL_DATASET = ROOT / "datasets" / "local-site-example.jsonl"
# Postgres tests run when this points at a disposable database, e.g.
# postgresql://postgres:openhealth-test@127.0.0.1:56432/openhealth_test (see CLAUDE.md).
POSTGRES_URL = os.environ.get("OPENHEALTH_TEST_DATABASE_URL", "").strip()


@pytest.fixture(params=["sqlite", "postgres"])
def db_target(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """An empty audit database: a SQLite file, or a fresh Postgres schema dropped afterwards."""
    if request.param == "sqlite":
        yield str(tmp_path / "audit.db")
        return
    if not POSTGRES_URL:
        pytest.skip("set OPENHEALTH_TEST_DATABASE_URL to run the Postgres backend tests")
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


def message(body: str, **extra: Any) -> dict[str, Any]:
    return {"message": {"body": body, **extra}}
