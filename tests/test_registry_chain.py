# SPDX-License-Identifier: Apache-2.0
"""The registry's chain (LP-18b, `hub/registry.py` `record_event`, `RegistryStore.verify`) and
reviews, on SQLite and Postgres."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mendari.cli import main
from mendari.core.registry import resolve_pack
from mendari.evals.share import shareable
from mendari.hub.registry import RegistryStore, token_id
from tests.conftest import REGISTRY_ADMIN, Registry

REFERRAL = resolve_pack("mendari/referral-router")
ADMIN = {"authorization": f"Bearer {REGISTRY_ADMIN}"}
VERSION_PATH = f"/v1/packs/{REFERRAL.id}/{REFERRAL.version}"


def _events(db_target: str) -> list[dict[str, Any]]:
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        return db.all("SELECT * FROM hub_events ORDER BY seq")


def _verify(db_target: str) -> dict[str, Any]:
    with RegistryStore(db_target) as store:
        return store.verify()


def _write(db_target: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    with RegistryStore(db_target) as store, store._db.session(exclusive=True) as db:
        db.all(sql, params)


def _share(registry: Registry) -> None:
    report = shareable(json.loads((REFERRAL.root / "evals" / "benchmark.json").read_text()))
    shared = registry.client.post(
        f"{VERSION_PATH}/reports",
        json={"site": "Site A", "report": report},
        headers={"authorization": f"Bearer {registry.token}"},
    )
    assert shared.status_code == 201, shared.text


def test_every_write_is_chained_and_the_chain_verifies(registry: Registry, db_target: str) -> None:
    assert registry.publish().status_code == 201
    _share(registry)
    patched = registry.client.patch(
        "/v1/namespaces/mendari", json={"publisher_type": "verified"}, headers=ADMIN
    )
    assert patched.status_code == 200
    reviewed = registry.client.post(
        f"{VERSION_PATH}/review", json={"risk_level": 1, "note": "checked"}, headers=ADMIN
    )
    assert reviewed.status_code == 201, reviewed.text
    events = _events(db_target)
    assert [e["event"] for e in events] == [
        "namespace.created",
        "token.issued",
        "key.added",
        "pack.published",
        "report.shared",
        "namespace.publisher_type_set",
        "pack.reviewed",
    ]
    published = json.loads(events[3]["payload"])
    assert published == {
        "pack": REFERRAL.id,
        "version": REFERRAL.version,
        "digest": REFERRAL.digest,
        "archive_sha256": registry.client.get(VERSION_PATH).json()["archive_sha256"],
        "key_id": json.loads(events[2]["payload"])["key_id"],
        "by": f"token:{token_id(registry.token)}",  # the token's id, never the token
    }
    assert registry.token not in json.dumps(events)
    assert _verify(db_target) == {"ok": True, "events": 7, "seq": None, "problem": None}


def test_the_command_line_verifies_read_only(
    registry: Registry, db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert registry.publish().status_code == 201
    assert main(["registry", "verify", "--db", db_target]) == 0
    assert "registry chain verified: 4 events" in capsys.readouterr().out
    _write(db_target, "UPDATE hub_events SET payload = ? WHERE event = ?", ("{}", "key.added"))
    assert main(["registry", "verify", "--db", db_target]) == 1
    assert "registry chain broken at event 3: an event was changed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("tamper", "problem"),
    [
        (
            ("DELETE FROM hub_events WHERE event = ?", ("token.issued",)),
            "an event is missing or out of order",
        ),
        (
            ("UPDATE hub_versions SET archive_sha256 = ?", ("0" * 64,)),
            "1 of the versions are not the versions their chained publish names",
        ),
        (
            (
                "INSERT INTO hub_tokens (token_sha256, namespace, created_at) VALUES (?, ?, ?)",
                ("f" * 64, "mendari", "2026-01-01T00:00:00+00:00"),
            ),
            "1 of the tokens have no chained event",
        ),
        (
            (
                "INSERT INTO hub_namespaces (name, publisher_type, display_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("planted", "verified", "Planted", "2026-01-01T00:00:00+00:00"),
            ),
            "1 of the namespaces have no chained event",
        ),
    ],
    ids=["event-deleted", "archive-swapped", "token-planted", "namespace-planted"],
)
def test_a_changed_chain_or_an_unchained_row_does_not_verify(
    registry: Registry, db_target: str, tamper: tuple[str, tuple[Any, ...]], problem: str
) -> None:
    """A write straight to the database, past the API, is found: an event removed, a version's
    archive replaced, a token or a namespace (a publisher type of `verified`) planted."""
    assert registry.publish().status_code == 201
    assert _verify(db_target)["ok"]
    _write(db_target, *tamper)
    result = _verify(db_target)
    assert not result["ok"] and result["problem"] == problem


def test_a_registry_from_before_the_chain_records_what_it_held(
    registry: Registry, db_target: str
) -> None:
    """A registry that held rows before it had a chain records them once, when it is next
    opened (`registry.chain_started`), and verifies; a row added later without an event does
    not."""
    assert registry.publish().status_code == 201
    _write(db_target, "DELETE FROM hub_events")
    events = _events(db_target)  # opening the store again migrates
    assert [e["event"] for e in events] == ["registry.chain_started"]
    held = json.loads(events[0]["payload"])
    assert held["namespaces"] == ["mendari"] and len(held["versions"]) == 1
    assert _verify(db_target)["ok"]
    assert [e["event"] for e in _events(db_target)] == ["registry.chain_started"]  # once


def test_a_review_confirms_or_corrects_the_declared_risk_level(registry: Registry) -> None:
    assert registry.publish().status_code == 201
    unreviewed = registry.client.get(VERSION_PATH).json()
    assert unreviewed["checked"]["review"] == {"status": "unreviewed"}
    assert unreviewed["claimed"]["risk"]["confirmed"] is None
    publisher = {"authorization": f"Bearer {registry.token}"}
    body = {"risk_level": 2, "note": "reads clinical notes"}
    assert registry.client.post(f"{VERSION_PATH}/review", json=body).status_code == 401
    refused = registry.client.post(f"{VERSION_PATH}/review", json=body, headers=publisher)
    assert refused.status_code == 401
    for bad in ({"risk_level": 5}, {"risk_level": 1, "note": "two\nlines"}):
        assert (
            registry.client.post(f"{VERSION_PATH}/review", json=bad, headers=ADMIN).status_code
            == 422
        )
    corrected = registry.client.post(f"{VERSION_PATH}/review", json=body, headers=ADMIN)
    assert corrected.status_code == 201, corrected.text
    review = corrected.json()["checked"]["review"]
    assert {k: review[k] for k in ("status", "risk_level", "matches_declared", "reviewer")} == {
        "status": "reviewed",
        "risk_level": 2,
        "matches_declared": False,
        "reviewer": "admin",
    }
    assert corrected.json()["claimed"]["risk"]["level"] == 1  # the publisher's word is kept
    assert corrected.json()["claimed"]["risk"]["confirmed"] is False
    again = registry.client.post(
        f"{VERSION_PATH}/review", json={"risk_level": 1}, headers=ADMIN
    ).json()
    assert again["checked"]["review"]["matches_declared"] is True  # the newest review is shown
    for shown in (
        registry.client.get(f"/v1/packs/{REFERRAL.id}").json()["latest"],
        registry.client.get("/v1/search").json()[0],
    ):
        assert shown["claimed"]["risk"]["confirmed"] is True
    missing = registry.client.post(
        f"/v1/packs/{REFERRAL.id}/0.0.1/review", json={"risk_level": 1}, headers=ADMIN
    )
    assert missing.status_code == 404


def test_a_review_is_chained_and_planted_reviews_do_not_verify(
    registry: Registry, db_target: str, tmp_path: Path
) -> None:
    assert registry.publish().status_code == 201
    registry.client.post(f"{VERSION_PATH}/review", json={"risk_level": 1}, headers=ADMIN)
    assert _verify(db_target)["ok"]
    _write(
        db_target,
        "INSERT INTO hub_reviews (id, namespace, name, version, digest, risk_level, reviewer, "
        "note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("planted", "mendari", "referral-router", REFERRAL.version, REFERRAL.digest, 0, "admin",
         "", "2099-01-01T00:00:00.000+00:00"),
    )  # fmt: skip
    assert _verify(db_target)["problem"] == "1 of the reviews have no chained event"
