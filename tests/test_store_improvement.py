# SPDX-License-Identifier: Apache-2.0
"""The improvement dataset from reviewers' corrections, and proposed behaviour tests (LP-20i,
`evals/corrections.py`): each label with its provenance, sealed in the organization's store,
never returned by a route, never in a pack's files, never another organization's."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mendari.core.pack_store import PackStore
from tests.conftest import member
from tests.test_store_uploads import ORG_A, ORG_B, Store, store_world

INBOX = "mendari/patient-inbox-router"
BODIES = [
    "I need to move my appointment next week to Friday morning please",
    "Is there a charge for the forms my employer asked you to fill in",
    "My rash is spreading up my arm and getting warm to the touch",
]


@pytest.fixture
def world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    yield from store_world(db_target, tmp_path, monkeypatch)


def _corrected(
    world: Store, org: str = ORG_A, labels: tuple[str, ...] = ("front_desk", "billing", "nurse")
) -> list[str]:
    """Decisions of the inbox pack in `org`, each resolved by a reviewer with a label."""
    reviewer = world.as_(org, "reviewer")
    made = []
    for body, label in zip(BODIES, labels, strict=True):
        decided = world.client.post(
            f"/api/packs/{INBOX}/decide",
            json={"message": {"body": f"{body} ({org})"}},
            headers=reviewer,
        ).json()
        resolved = world.client.post(
            f"/api/review/{decided['id']}",
            json={"final_label": label, "reviewer": "x", "note": "checked"},
            headers=reviewer,
        )
        assert resolved.status_code == 200, resolved.text
        made.append(decided["id"])
    return made


def _author_reader(world: Store, org: str = ORG_A) -> dict[str, str]:
    return member(world.db, org=org, roles=["pack_author", "reviewer"], sub=f"reader-{org}")


def _draft(world: Store, headers: dict[str, str]) -> str:
    made = world.client.post(
        "/api/store/copies", json={"pack": INBOX, "name": "inbox-router"}, headers=headers
    )
    assert made.status_code == 201, made.text
    return str(made.json()["digest"])


def _build(world: Store, headers: dict[str, str], digest: str, org: str = ORG_A) -> Any:
    return world.client.post(
        f"/api/store/packs/{org}/inbox-router/improvement",
        json={"version": digest},
        headers=headers,
    )


def test_each_label_names_the_decision_the_reviewer_and_whether_it_overrode(
    world: Store,
) -> None:
    decided = _corrected(world)
    reader = _author_reader(world)
    digest = _draft(world, reader)
    made = _build(world, reader, digest)
    assert made.status_code == 201, made.text
    assert (made.json()["kind"], made.json()["cases"]) == ("improvement", 3)
    packs: PackStore = world.client.app.state.pack_store  # type: ignore[attr-defined]
    records = {r["id"]: r for r in packs.dataset_records(ORG_A, made.json()["id"])}
    assert set(records) == set(decided)
    for decision_id, record in records.items():
        provenance = record["provenance"]["destination"]
        assert provenance["decision"] == decision_id
        assert provenance["reviewer"] and "@" not in provenance["reviewer"]
        assert isinstance(provenance["overrode"], bool) and provenance["at"]
        assert str(provenance["digest"]).startswith("sha256:")


def test_a_hashed_input_is_skipped_and_counted(world: Store) -> None:
    from mendari.core import tenancy
    from mendari.evals.corrections import corrections_dataset

    _corrected(world)
    with world.db._db.session(exclusive=True) as db:  # one kept only as its hash
        first = db.one("SELECT decision_id FROM decision_inputs ORDER BY decision_id")
        db.all("DELETE FROM decision_inputs WHERE decision_id = ?", (first["decision_id"],))
    with tenancy.acting_for(ORG_A):
        records, skipped = corrections_dataset(
            world.db, org=ORG_A, pack_ids=[INBOX], reader="reviewer@clinic-a"
        )
    assert (len(records), skipped) == (2, 1)


def test_the_dataset_is_sealed_and_no_route_returns_its_cases(world: Store) -> None:
    _corrected(world)
    reader = _author_reader(world)
    digest = _draft(world, reader)
    made = _build(world, reader, digest).json()
    assert made["sealed"] is True
    with world.db._db.session(exclusive=False) as db:
        kept = str(db.one("SELECT data FROM store_datasets WHERE id = ?", (made["id"],))["data"])
    assert not any(body in kept for body in BODIES)
    for path in (
        "/api/store/datasets",
        f"/api/store/datasets/{made['id']}",
        f"/api/store/versions/{digest}/proposed-tests?dataset={made['id']}",
    ):
        answer = world.client.get(path, headers=reader)
        assert answer.status_code == 200, (path, answer.text)
        assert not any(body in answer.text for body in BODIES), path


def test_an_author_who_may_not_read_inputs_cannot_build_it(world: Store) -> None:
    _corrected(world)
    author = world.as_(ORG_A, "pack_author")
    digest = _draft(world, author)
    refused = _build(world, author, digest)
    assert refused.status_code == 403 and "read" in refused.text


def test_another_organizations_corrections_are_never_in_it(world: Store) -> None:
    mine = _corrected(world)
    theirs = _corrected(world, org=ORG_B)
    reader = _author_reader(world)
    made = _build(world, reader, _draft(world, reader)).json()
    packs: PackStore = world.client.app.state.pack_store  # type: ignore[attr-defined]
    ids = {r["id"] for r in packs.dataset_records(ORG_A, made["id"])}
    assert ids == set(mine) and not ids & set(theirs)
    # And clinic-b's author cannot read clinic-a's.
    other = _author_reader(world, ORG_B)
    assert world.client.get(f"/api/store/datasets/{made['id']}", headers=other).status_code == 404


def test_a_proposed_test_runs_against_the_draft_and_is_not_in_the_packs_files(
    world: Store,
) -> None:
    _corrected(world)
    reader = _author_reader(world)
    digest = _draft(world, reader)
    made = _build(world, reader, digest).json()
    packs: PackStore = world.client.app.state.pack_store  # type: ignore[attr-defined]
    files_before = packs.version(ORG_A, digest)["files"]
    shown = world.client.get(
        f"/api/store/versions/{digest}/proposed-tests",
        params={"dataset": made["id"]},
        headers=reader,
    ).json()
    assert len(shown["cases"]) == 3
    assert {c["state"] for c in shown["cases"]} == {"proposed"}
    assert all(set(c) == {"case", "expect", "passed", "failures", "state"} for c in shown["cases"])
    assert packs.version(ORG_A, digest)["files"] == files_before
    assert packs.version(ORG_A, digest)["digest"] == digest


def test_an_accepted_proposal_runs_with_the_drafts_tests(world: Store) -> None:
    _corrected(world)
    reader = _author_reader(world)
    digest = _draft(world, reader)
    made = _build(world, reader, digest).json()
    shown = world.client.get(
        f"/api/store/versions/{digest}/proposed-tests",
        params={"dataset": made["id"]},
        headers=reader,
    ).json()
    first, second = shown["cases"][0]["case"], shown["cases"][1]["case"]
    for case, accept in ((first, True), (second, False)):
        decided = world.client.post(
            f"/api/store/versions/{digest}/proposed-tests/{made['id']}/{case}",
            json={"accept": accept},
            headers=reader,
        )
        assert decided.status_code == 200, decided.text
    tested = world.client.post(f"/api/store/versions/{digest}/tests", headers=reader).json()
    assert [c["case"] for c in tested["proposed"]["cases"]] == [first]
    assert tested["proposed"]["passed"] + tested["proposed"]["failed"] == 1


def test_no_chained_event_or_log_holds_an_input(
    world: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _corrected(world)
    reader = _author_reader(world)
    digest = _draft(world, reader)
    made = _build(world, reader, digest).json()
    shown = world.client.get(
        f"/api/store/versions/{digest}/proposed-tests",
        params={"dataset": made["id"]},
        headers=reader,
    ).json()
    world.client.post(
        f"/api/store/versions/{digest}/proposed-tests/{made['id']}/{shown['cases'][0]['case']}",
        json={"accept": True},
        headers=reader,
    )
    with world.db._db.session(exclusive=False) as db:
        chain = json.dumps([r["payload"] for r in db.all("SELECT payload FROM audit_events")])
    for body in BODIES:
        assert body not in chain and body not in caplog.text
    assert world.db.verify()["ok"]


def test_past_the_daily_quota_no_input_is_read(
    world: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store_datasets` in MENDARI_ORG_DAILY_LIMITS is checked before the reviewers' corrections
    are read: each read of a patient's input is chained, so a refused build reads none."""
    from mendari.evals import corrections

    _corrected(world)
    reader = _author_reader(world)
    digest = _draft(world, reader)
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "store_datasets=1")
    assert _build(world, reader, digest).status_code == 201

    def reads(*_: Any, **__: Any) -> Any:
        raise AssertionError("inputs were read past the quota")

    monkeypatch.setattr(corrections, "corrections_dataset", reads)
    refused = _build(world, reader, digest)
    assert refused.status_code == 429 and "1 datasets for today" in refused.text
