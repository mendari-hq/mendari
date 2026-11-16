# SPDX-License-Identifier: Apache-2.0
"""The registry's chain (LP-18b, `hub/registry.py` `record_event`, `RegistryStore.verify`) and
reviews, on SQLite and Postgres."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mendari.cli import main
from mendari.core.registry import resolve_pack
from mendari.errors import HubError
from mendari.hub.registry import RegistryStore, ReviewRequest, token_id
from tests.conftest import Registry, share_signed, shared_benchmark, verified_site

REFERRAL = resolve_pack("mendari/referral-router")
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


def _share(registry: Registry, directory: Path) -> None:
    """A verified synthetic site shares the benchmark's counts, signed (LP-22f)."""
    site = verified_site(registry, directory)
    shared = share_signed(registry, site, shared_benchmark())
    assert shared.status_code == 201, shared.text


def test_every_write_is_chained_and_the_chain_verifies(
    registry: Registry, db_target: str, tmp_path: Path
) -> None:
    assert registry.publish().status_code == 201
    _share(registry, tmp_path)
    registry.operator.set_publisher_type("mendari", "verified", by="operator")
    registry.operator.review(
        "mendari", "referral-router", REFERRAL.version, risk_level=1, note="checked",
        reviewer="operator",
    )  # fmt: skip
    events = _events(db_target)
    assert [e["event"] for e in events] == [
        "namespace.created",
        "token.issued",
        "key.added",
        "pack.published",
        "site.registered",
        "site.key_added",
        "site.verified",
        "site.ticket_issued",
        "report.shared",
        "site.ticket_reported",
        "namespace.publisher_type_set",
        "pack.reviewed",
    ]
    published = json.loads(events[3]["payload"])
    assert published.pop("row")["kind"] == "versions"  # the row it wrote, hashed
    assert published == {
        "pack": REFERRAL.id,
        "version": REFERRAL.version,
        "digest": REFERRAL.digest,
        "archive_sha256": registry.client.get(VERSION_PATH).json()["archive_sha256"],
        "key_id": json.loads(events[2]["payload"])["key_id"],
        "by": f"token:{token_id(registry.token)}",  # the token's id, never the token
        "org": None,  # a namespace with no claim (LP-19c): linked to no organization, listed
        "listed": True,
    }
    assert registry.token not in json.dumps(events)
    assert _verify(db_target) == {
        "ok": True,
        "seq": None,
        "problem": None,
        "events": 12,
        "head": events[-1]["hash"],
    }


def test_the_command_line_verifies_read_only(
    registry: Registry, db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert registry.publish().status_code == 201
    head = registry.client.get("/v1/chain/head").json()
    assert head["events"] == 4
    assert main(["registry", "verify", "--db", db_target]) == 0
    assert f"registry chain verified: 4 events, head {head['head']}" in capsys.readouterr().out
    _write(db_target, "UPDATE hub_events SET payload = ? WHERE event = ?", ("{}", "key.added"))
    assert main(["registry", "verify", "--db", db_target]) == 1
    assert "registry chain broken at event 3: an event was changed" in capsys.readouterr().err


PLANTED = "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    ("tamper", "problem"),
    [
        (
            ("DELETE FROM hub_events WHERE event = ?", ("token.issued",)),
            "an event is missing or out of order",
        ),
        (
            (
                "INSERT INTO hub_tokens (token_sha256, namespace, created_at) VALUES (?, ?, ?)",
                ("f" * 64, "mendari", PLANTED),
            ),
            "1 of the tokens have no chained event",
        ),
        (
            (
                "INSERT INTO hub_namespaces (name, publisher_type, display_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("planted", "verified", "Planted", PLANTED),
            ),
            "1 of the namespaces have no chained event",
        ),
        (
            (
                "UPDATE hub_namespaces SET publisher_type = ? WHERE name = ?",
                ("institutional", "mendari"),
            ),
            "1 of the namespaces changed since their chained event",
        ),
        (
            ("UPDATE hub_publisher_tokens SET namespace = ?", ("other",)),
            "1 of the publisher_tokens changed since their chained event",
        ),
        (
            ("UPDATE hub_versions SET archive_sha256 = ?", ("0" * 64,)),
            "1 of the versions changed since their chained event",
        ),
        (
            (
                "UPDATE hub_versions SET metadata = REPLACE(metadata, ?, ?)",
                ('"level": 1', '"level": 0'),
            ),
            "1 of the versions changed since their chained event",
        ),
        (("DELETE FROM hub_versions", ()), "1 of the versions were removed"),
        (("DELETE FROM hub_keys", ()), "1 of the keys were removed"),
        (
            ("UPDATE hub_archives SET data = ?", (b"other bytes",)),
            "an archive is not the bytes its name hashes",
        ),
        (("DELETE FROM hub_archives", ()), "a version's archive is missing"),
        (
            (
                "INSERT INTO hub_claims (namespace, org, claimed_by, created_at, status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("mendari", "planted", "user:planted", PLANTED, "approved"),
            ),
            "1 of the claims have no chained event",
        ),
        (
            ("UPDATE hub_claims SET status = ?, decided_by = ?", ("approved", "user:planted")),
            "1 of the claims changed since their chained event",
        ),
        (("DELETE FROM hub_claims", ()), "1 of the claims were removed"),
        (
            (
                "INSERT INTO hub_site_keys (key_id, site, public_pem, created_at, added_by, "
                "compromised) VALUES (?, ?, ?, ?, ?, 0)",
                ("ed25519:" + "f" * 16, "0" * 16, "planted", PLANTED, "user:planted"),
            ),
            "1 of the site_keys have no chained event",
        ),
        (
            (
                "UPDATE hub_sites SET status = ?, decided_by = ? WHERE status = 'pending'",
                ("verified", "user:planted"),
            ),
            "1 of the sites changed since their chained event",
        ),
    ],
    ids=[
        "event-deleted",
        "token-planted",
        "namespace-planted",
        "publisher-type-changed",
        "token-moved",
        "archive-name-swapped",
        "metadata-edited",
        "version-removed",
        "key-removed",
        "archive-bytes-replaced",
        "archive-removed",
        "claim-planted",
        "claim-approved-past-the-api",
        "claim-removed",
        "site-key-planted",
        "site-verified-past-the-api",
    ],
)
def test_a_changed_chain_or_a_row_written_past_the_api_does_not_verify(
    registry: Registry,
    db_target: str,
    tamper: tuple[str, tuple[Any, ...]],
    problem: str,
    tmp_path: Path,
) -> None:
    """The adversarial review of LP-18b: `verify` checked only that rows were named. Each row
    the chain covers must now be the row its newest event hashed: a write straight to the
    database (a planted, changed, moved or removed row, an archive's bytes) is found."""
    assert registry.publish().status_code == 201
    _share(registry, tmp_path)
    with RegistryStore(db_target) as store:  # a pending claim (LP-19c): its row is covered too
        store.claim("acme", org="acme", by="user:3f9a")
        store.register_site(org="acme", name="Acme (synthetic)", by="user:3f9a")  # LP-22d
    assert _verify(db_target)["ok"]
    _write(db_target, *tamper)
    result = _verify(db_target)
    assert not result["ok"] and result["problem"] == problem


def test_a_key_whose_id_is_not_its_key_does_not_verify(registry: Registry, db_target: str) -> None:
    """A key replaced under its old id, the row re-hashed into its event as an attacker who could
    rewrite one event could not do without breaking the chain: found by the key's own id."""
    from mendari.hub import archive

    assert registry.publish().status_code == 201
    other = archive.generate_key()[1].decode()
    _write(db_target, "UPDATE hub_keys SET public_pem = ?", (other,))
    assert _verify(db_target)["problem"] == "1 of the keys changed since their chained event"


def test_a_registry_from_before_the_chain_records_what_it_held_once(
    registry: Registry, db_target: str
) -> None:
    """A registry that held rows before it had a chain (no chain tables) hashes them once, the
    first time it opens with one (`registry.chain_started`), and verifies. A chain emptied
    later is never started again: it is reported, so emptying it cannot whitelist a planted
    row (the adversarial review of LP-18b)."""
    assert registry.publish().status_code == 201
    _write(db_target, "DROP TABLE hub_events")
    _write(db_target, "DROP TABLE hub_meta")
    events = _events(db_target)  # opening the store again creates the tables and migrates
    assert [e["event"] for e in events] == ["registry.chain_started"]
    held = json.loads(events[0]["payload"])["rows"]
    assert list(held["namespaces"]) == ["mendari"] and len(held["versions"]) == 1
    assert _verify(db_target)["ok"]
    assert [e["event"] for e in _events(db_target)] == ["registry.chain_started"]  # once

    _write(
        db_target,
        "INSERT INTO hub_namespaces (name, publisher_type, display_name, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("planted", "verified", "Planted", PLANTED),
    )
    _write(db_target, "DELETE FROM hub_events")
    assert _events(db_target) == []  # opened again: not started anew
    assert _verify(db_target)["problem"] == "the chain was emptied"


def test_a_review_confirms_or_corrects_the_declared_risk_level(registry: Registry) -> None:
    assert registry.publish().status_code == 201
    unreviewed = registry.client.get(VERSION_PATH).json()
    assert unreviewed["checked"]["review"] == {"status": "unreviewed"}
    assert unreviewed["claimed"]["risk"]["confirmed"] is None
    publisher = {"authorization": f"Bearer {registry.token}"}
    body = {"risk_level": 2, "note": "reads clinical notes"}
    # Nobody reviews over the registry's own routes (LP-19g): a reviewer, under the engine, or
    # the operator's command, which checks the same body.
    for headers in ({}, publisher):
        gone = registry.client.post(f"{VERSION_PATH}/review", json=body, headers=headers)
        assert gone.status_code in (404, 405)
    for bad in (
        {"risk_level": 5},
        {"risk_level": 1, "note": "two\nlines"},
        {"risk_level": 1, "note": "a \u202e reversed"},
        {"risk_level": 1, "note": "x" * 301},
    ):
        with pytest.raises(ValidationError):
            ReviewRequest.model_validate(bad)

    def review(risk_level: int, note: str = "") -> dict[str, Any]:
        return registry.operator.review(
            "mendari", "referral-router", REFERRAL.version, risk_level=risk_level, note=note,
            reviewer="operator",
        )  # fmt: skip

    corrected = review(2, "reads clinical notes")
    shown = registry.client.get(VERSION_PATH).json()
    assert shown["checked"]["review"] == corrected["checked"]["review"]
    review_shown = shown["checked"]["review"]
    assert {
        k: review_shown[k] for k in ("status", "risk_level", "matches_declared", "reviewer")
    } == {
        "status": "reviewed",
        "risk_level": 2,
        "matches_declared": False,
        "reviewer": "operator",
    }
    assert shown["claimed"]["risk"]["level"] == 1  # the publisher's word is kept
    assert shown["claimed"]["risk"]["confirmed"] is False
    review(1)
    again = registry.client.get(VERSION_PATH).json()
    assert again["checked"]["review"]["matches_declared"] is True  # the newest review is shown
    for shown in (
        registry.client.get(f"/v1/packs/{REFERRAL.id}").json()["latest"],
        registry.client.get("/v1/search").json()[0],
    ):
        assert shown["claimed"]["risk"]["confirmed"] is True
    with pytest.raises(HubError) as missing:
        registry.operator.review(
            "mendari", "referral-router", "0.0.1", risk_level=1, note="", reviewer="operator"
        )
    assert missing.value.status == 404


def test_a_review_is_chained_and_planted_reviews_do_not_verify(
    registry: Registry, db_target: str, tmp_path: Path
) -> None:
    assert registry.publish().status_code == 201
    registry.operator.review(
        "mendari", "referral-router", REFERRAL.version, risk_level=1, note="", reviewer="operator"
    )
    assert _verify(db_target)["ok"]
    _write(
        db_target,
        "INSERT INTO hub_reviews (id, namespace, name, version, digest, risk_level, reviewer, "
        "note, created_at, ordinal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("planted", "mendari", "referral-router", REFERRAL.version, REFERRAL.digest, 0, "admin",
         "", "2099-01-01T00:00:00.000+00:00", 99),
    )  # fmt: skip
    assert _verify(db_target)["problem"] == "1 of the reviews have no chained event"
