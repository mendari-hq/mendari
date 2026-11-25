# SPDX-License-Identifier: Apache-2.0
"""Evaluation as a stored job with a ledger, and draft against current (LP-20g,
`evals/store_jobs.py`): slices a caller asks for, one report with its ledger, the same bytes as a
single run, the comparison paired on the same cases with figures computed independently, and
nothing of one organization's jobs read by another."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from scipy.stats import beta, binomtest

from mendari.core.decision import Decision
from mendari.core.registry import resolve_pack
from mendari.evals import store_jobs
from mendari.evals.harness import evaluation, load_dataset
from mendari.evals.ledger import Ledger
from mendari.evals.store_jobs import JobStore
from tests.conftest import TRIAGE_DIR
from tests.support.registry import SCREEN_RULES, TRIAGE
from tests.test_store_uploads import ORG_A, ORG_B, SECRET_PHONE, Store, store_world

SERVED = TRIAGE.id  # an agent this deployment serves from disk: the triage fixture


@pytest.fixture
def world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    yield from store_world(db_target, tmp_path, monkeypatch)


CASES = (TRIAGE_DIR / "data" / "test.jsonl").read_text().splitlines()


def _author(world: Store, org: str = ORG_A) -> dict[str, str]:
    return world.as_(org, "pack_author")


def _copy(world: Store, org: str = ORG_A, name: str = "inbox-router") -> str:
    made = world.client.post(
        "/api/store/copies", json={"pack": SERVED, "name": name}, headers=_author(world, org)
    )
    assert made.status_code == 201, made.text
    return str(made.json()["digest"])


def _dataset(world: Store, digest: str, n: int = 8, org: str = ORG_A, **body: Any) -> Any:
    content = "\n".join(CASES[:n]) + "\n"
    return world.client.post(
        "/api/store/datasets",
        json={"name": "sample", "version": digest, "content": content, **body},
        headers=_author(world, org),
    )


def _job(world: Store, org: str = ORG_A, **body: Any) -> dict[str, Any]:
    made = world.client.post("/api/store/jobs", json=body, headers=_author(world, org))
    assert made.status_code == 201, made.text
    return dict(made.json())


def _run(world: Store, job_id: str, org: str = ORG_A) -> dict[str, Any]:
    for _ in range(1000):
        answer = world.client.post(f"/api/store/jobs/{job_id}/advance", headers=_author(world, org))
        assert answer.status_code == 200, answer.text
        if answer.json()["state"] in ("done", "failed"):
            return dict(answer.json())
    raise AssertionError("the job never finished")


def _ledger(world: Store, report_id: str) -> bytes:
    with world.db._db.session(exclusive=False) as db:
        row = db.one("SELECT ledger FROM eval_reports WHERE id = ?", (report_id,))
    return str(row["ledger"]).encode()


def test_a_job_is_stored_runs_in_slices_and_ends_in_one_report_with_its_ledger(
    world: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_jobs, "SLICE_SECONDS", 0.0)  # one case a slice
    digest = _copy(world)
    dataset = _dataset(world, digest).json()
    assert dataset["cases"] == 8 and dataset["report"]["schema_problems"]["count"] == 0
    job = _job(world, version=digest, dataset={"id": dataset["id"]})
    assert (job["state"], job["done"], job["total"]) == ("queued", 0, 8)
    seen = []
    for _ in range(8):
        step = world.client.post(f"/api/store/jobs/{job['id']}/advance", headers=_author(world))
        seen.append(step.json()["done"])
    assert seen == [1, 2, 3, 4, 5, 6, 7, 8]
    done = world.client.get(f"/api/store/jobs/{job['id']}", headers=_author(world)).json()
    assert done["state"] == "done" and done["report_id"]
    ledger = Ledger.loads(_ledger(world, done["report_id"]))
    assert ledger.header["pack"]["digest"] == digest and len(ledger.cases) == 8
    with world.db._db.session(exclusive=False) as db:
        events = [r["event"] for r in db.all("SELECT event FROM audit_events")]
    assert "store_job.created" in events and "eval.recorded" in events
    assert world.db.verify()["ok"]


def test_two_runs_give_the_same_ledger_bytes(world: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sliced or not, twice: the bytes a single run writes."""
    digest = _copy(world)
    dataset = _dataset(world, digest).json()
    first = _run(world, _job(world, version=digest, dataset={"id": dataset["id"]})["id"])
    monkeypatch.setattr(store_jobs, "SLICE_SECONDS", 0.0)
    second = _run(world, _job(world, version=digest, dataset={"id": dataset["id"]})["id"])
    assert _ledger(world, first["report_id"]) == _ledger(world, second["report_id"])
    pack = world.client.app.state.pack_store.load(ORG_A, digest)  # type: ignore[attr-defined]
    raw = ("\n".join(CASES[:8]) + "\n").encode()
    one = evaluation(Decision(pack, origin_cap=False), load_dataset("sample.jsonl", name="sample",
                     raw=raw, label="sample.jsonl"))  # fmt: skip
    assert one.ledger.dumps() == _ledger(world, first["report_id"])


def test_a_slice_called_twice_evaluates_each_case_once(world: Store) -> None:
    digest = _copy(world)
    dataset = _dataset(world, digest).json()
    job = _job(world, version=digest, dataset={"id": dataset["id"]})
    with world.db._db.session(exclusive=True) as db:  # another call holds this slice
        db.all(
            "UPDATE store_jobs SET claim = 'other', claimed_until = '2999-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (job["id"],),
        )
    held = world.client.post(f"/api/store/jobs/{job['id']}/advance", headers=_author(world))
    assert held.json()["done"] == 0
    with world.db._db.session(exclusive=True) as db:  # that call's function ended: it expired
        db.all(
            "UPDATE store_jobs SET claimed_until = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )
    finished = _run(world, job["id"])
    ledger = Ledger.loads(_ledger(world, finished["report_id"]))
    assert len({case["c"] for case in ledger.cases}) == len(ledger.cases) == 8


def test_a_job_stopped_half_way_continues_from_its_ledger(
    world: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_jobs, "SLICE_SECONDS", 0.0)
    digest = _copy(world)
    dataset = _dataset(world, digest).json()
    whole = _run(world, _job(world, version=digest, dataset={"id": dataset["id"]})["id"])
    job = _job(world, version=digest, dataset={"id": dataset["id"]})
    for _ in range(3):
        world.client.post(f"/api/store/jobs/{job['id']}/advance", headers=_author(world))
    # A fresh process: another JobStore over the same database continues it.
    app_jobs = world.client.app.state  # type: ignore[attr-defined]
    fresh = JobStore(
        app_jobs.pack_store,
        served=lambda: {SERVED: resolve_pack(SERVED)},
        decision_for=lambda pack, variants: Decision(
            pack, providers=dict(variants), origin_cap=False
        ),
    )
    state = fresh.job(ORG_A, job["id"])
    assert state["done"] == 3
    while state["state"] != "done":
        state = fresh.advance(ORG_A, job["id"])
    assert _ledger(world, state["report_id"]) == _ledger(world, whole["report_id"])


def _compared(world: Store) -> tuple[dict[str, Any], dict[str, Any], str]:
    """A draft whose same-day screen never flags (its rules' default score raised), and the
    triage fixture it was copied from, each run on the organization's dataset; the comparison."""
    digest = _copy(world)
    rules = resolve_pack(SERVED).read_bytes(SCREEN_RULES).decode()
    assert rules.count("default_score: 2.0") == 1  # the urgency question's
    edited = world.client.post(
        f"/api/store/versions/{digest}/edits",
        json={
            "files": [
                {
                    "path": SCREEN_RULES,
                    "content": rules.replace("default_score: 2.0", "default_score: 50.0"),
                }
            ]
        },
        headers=_author(world),
    )
    assert edited.status_code == 201, edited.text
    dataset = _dataset(world, edited.json()["digest"], n=120).json()
    version = edited.json()["digest"]
    draft = _run(world, _job(world, version=version, dataset={"id": dataset["id"]})["id"])
    current = _run(world, _job(world, pack=SERVED, dataset={"id": dataset["id"]})["id"])
    compared = world.client.get(
        "/api/store/comparisons",
        params={"draft": draft["id"], "current": current["id"]},
        headers=_author(world),
    )
    assert compared.status_code == 200, compared.text
    return compared.json(), draft, dataset["id"]


def test_draft_against_current_is_paired_on_the_same_cases(world: Store) -> None:
    compared, draft, _ = _compared(world)
    assert compared["dataset"]["paired"] == 120
    assert compared["dataset"]["only_in_a"] == compared["dataset"]["only_in_b"] == 0
    other = _run(world, _job(world, pack=SERVED, dataset={"name": "critical"})["id"])
    refused = world.client.get(
        "/api/store/comparisons",
        params={"draft": draft["id"], "current": other["id"]},
        headers=_author(world),
    )
    assert refused.status_code == 422 and "same cases" in refused.text


def test_every_figure_matches_an_independent_computation(world: Store) -> None:
    compared, _, _ = _compared(world)
    for figure in compared["figures"]:
        k, n = figure["a"]["k"], figure["a"]["n"]
        if n:
            low = 0.0 if k == 0 else beta.ppf(0.025, k, n - k + 1)
            high = 1.0 if k == n else beta.ppf(0.975, k + 1, n - k)
            assert figure["a"]["low"] == pytest.approx(low, abs=1e-6)
            assert figure["a"]["high"] == pytest.approx(high, abs=1e-6)
        discordant = figure["only_a"] + figure["only_b"]
        expected = 1.0 if discordant == 0 else binomtest(figure["only_a"], discordant).pvalue
        assert figure["p"] == pytest.approx(min(1.0, expected), abs=1e-9)
    adjusted = sorted(f["p_adjusted"] for f in compared["figures"])
    assert all(0 <= p <= 1 for p in adjusted)
    for side in ("figures_draft", "figures_current"):
        accuracy = compared[side]["accuracy"]
        assert accuracy is None or {"k", "n", "low", "high"} <= set(accuracy)


def test_too_few_cases_reads_insufficient_evidence(world: Store) -> None:
    compared, _, _ = _compared(world)
    assert all(
        f["evidence"] == "insufficient"
        for f in compared["figures"]
        if f["figure"].startswith("critical")
    )
    assert any(f["evidence"] == "insufficient" for f in compared["figures"])


def test_the_changed_cases_are_listed_by_id_and_nothing_else_of_them(world: Store) -> None:
    compared, _, _ = _compared(world)
    ids = {json.loads(line)["id"] for line in CASES[:120]}
    assert compared["changed"], "the edit changed no case"
    for item in compared["changed"]:
        assert set(item) == {"case", "changes"} and item["case"] in ids
    shown = json.dumps(compared)
    assert not any(json.loads(line)["input"]["message"]["body"] in shown for line in CASES[:120])


def test_a_draft_runs_under_the_budget_and_a_slow_rule_ends_as_a_persons_decision(
    world: Store,
) -> None:
    from tests.support.registry import EMOJI, long_message, slow_rules, triage_copy
    from tests.test_store_uploads import upload

    root = triage_copy(world.tmp, f"{ORG_A}/sorter", rules=slow_rules(), schema=long_message)
    slow = {"id": "s1", "input": {"message": {"body": EMOJI}}, "labels": {"destination": "billing"}}
    (root / "data" / "slow.jsonl").write_text(json.dumps(slow) + "\n")
    digest = upload(world, root).json()["digest"]
    raw = (root / "data" / "slow.jsonl").read_text()
    dataset = world.client.post(
        "/api/store/datasets",
        json={"name": "slow", "version": digest, "content": raw},
        headers=_author(world),
    ).json()
    done = _run(world, _job(world, version=digest, dataset={"id": dataset["id"]})["id"])
    case = Ledger.loads(_ledger(world, done["report_id"])).cases[0]
    assert case["o"] != "AUTO"
    assert "time_budget" in json.dumps(case["q"])


def test_a_remote_arm_needs_the_organizations_quota_and_stops_at_it(world: Store) -> None:
    digest = _copy(world)
    dataset = _dataset(world, digest).json()
    refused = world.client.post(
        "/api/store/jobs",
        json={
            "version": digest,
            "dataset": {"id": dataset["id"]},
            "variants": {"router": "llm"},
        },
        headers=_author(world),
    )
    assert refused.status_code == 422 and "eval_llm" in refused.text
    jobs = JobStore(
        world.client.app.state.pack_store,  # type: ignore[attr-defined]
        served=lambda: {},
        decision_for=lambda pack, variants: Decision(
            pack, providers=dict(variants), origin_cap=False
        ),
    )
    made = jobs.create(
        ORG_A,
        by="user-1",
        digest=digest,
        dataset={"name": "test"},
        variants={"router": "llm"},
        limits={"eval_llm": 10},
    )
    assert made["total"] == store_jobs.REMOTE_LIMIT < len(CASES)


def test_a_dataset_with_a_hidden_case_or_phi_like_content_is_held_or_refused(
    world: Store,
) -> None:
    digest = _copy(world)
    phi = json.dumps({"id": "p1", "input": {"message": {"body": f"call {SECRET_PHONE}"}},
                      "labels": {}})  # fmt: skip
    rejected = world.client.post(
        "/api/store/datasets",
        json={"name": "phi", "version": digest, "content": phi + "\n"},
        headers=_author(world),
    )
    assert rejected.status_code == 422 and SECRET_PHONE not in rejected.text
    assert rejected.json()["phi"]["counts"] == {"phone": 1}


def test_another_organization_reads_no_job_report_dataset_or_comparison(world: Store) -> None:
    digest = _copy(world)
    dataset = _dataset(world, digest).json()
    job = _run(world, _job(world, version=digest, dataset={"id": dataset["id"]})["id"])
    other = _author(world, ORG_B)
    for method, path, kwargs in (
        ("GET", f"/api/store/jobs/{job['id']}", {}),
        ("POST", f"/api/store/jobs/{job['id']}/advance", {}),
        ("GET", f"/api/store/datasets/{dataset['id']}", {}),
        ("GET", "/api/store/comparisons", {"params": {"draft": job["id"], "current": job["id"]}}),
        (
            "POST",
            "/api/store/jobs",
            {"json": {"version": digest, "dataset": {"id": dataset["id"]}}},
        ),
    ):
        answer = world.client.request(method, path, headers=other, **kwargs)
        assert answer.status_code == 404, (method, path, answer.text)
        assert job["id"] not in answer.text and dataset["id"] not in answer.text
    assert world.client.get("/api/store/jobs", headers=other).json() == []
    assert world.client.get("/api/store/datasets", headers=other).json() == []
    reports = world.client.get("/api/evals", headers=world.as_(ORG_B, "viewer")).json()
    assert job["report_id"] not in json.dumps(reports)


def test_audit_verify_passes_after_a_job(world: Store) -> None:
    digest = _copy(world)
    dataset = _dataset(world, digest).json()
    _run(world, _job(world, version=digest, dataset={"id": dataset["id"]})["id"])
    assert world.db.verify()["ok"]


def test_a_dataset_is_sealed_and_a_phi_key_rotation_reseals_it(
    db_target: str, tmp_path: Path
) -> None:
    """Kept sealed under the PHI policy, as a decision's input is; `mendari phi rotate` moves it
    to the new key with the inputs and notes, and `--check` counts it."""
    import secrets

    from mendari.core.audit import AuditStore
    from mendari.core.pack_store import PackStore
    from mendari.core.phi import PhiPolicy, key_id
    from tests.test_pack_store import files_of, renamed

    old, new, hashed = (secrets.token_bytes(32) for _ in range(3))
    raw = ("\n".join(CASES[:3]) + "\n").encode()
    body = json.loads(CASES[0])["input"]["message"]["body"]
    with AuditStore(db_target, phi=PhiPolicy("encrypt", old, hashed)) as store:
        packs = PackStore(store, scratch=tmp_path / "scratch")
        stored = packs.put(ORG_A, files_of(renamed(tmp_path)), by="u", source="upload")
        made = packs.add_dataset(ORG_A, raw, by="u", name="sample", digest=stored.digest)
        with store._db.session(exclusive=False) as db:
            kept = str(db.one("SELECT data FROM store_datasets")["data"])
        # Sealed (untagged, as every token is before a database's first rotation).
        assert made["sealed"] and body not in kept and "message" not in kept
    with AuditStore(db_target, phi=PhiPolicy("encrypt", new, hashed, (old,))) as store:
        assert store.rotate_phi(to=key_id(new))["datasets"] == 1
        with store._db.session(exclusive=False) as db:
            assert str(db.one("SELECT data FROM store_datasets")["data"]).startswith(
                f"v2:{key_id(new)}:"
            )
        assert PackStore(store).dataset_bytes(ORG_A, made["id"])[0] == raw
        assert store.phi_check()["by_key"]["datasets"] == {key_id(new): 1}
        assert store.verify()["ok"]


def test_past_the_daily_quota_an_uploaded_dataset_is_not_joined_and_stays(
    world: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store_datasets` is checked before an upload's parts are joined (which deletes the
    upload): refused today (429), the same upload is a dataset once the quota allows it."""
    import hashlib

    digest = _copy(world)
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "store_datasets=1")
    assert _dataset(world, digest).status_code == 201
    data = ("\n".join(CASES[:4]) + "\n").encode()
    author = _author(world)
    begun = world.client.post(
        "/api/store/uploads",
        json={"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        headers=author,
    )
    upload = begun.json()["upload"]
    sent = world.client.put(f"/api/store/uploads/{upload}/parts/0", content=data, headers=author)
    assert sent.status_code == 200, sent.text
    body = {"name": "parts", "version": digest, "upload": upload}
    refused = world.client.post("/api/store/datasets", json=body, headers=author)
    assert refused.status_code == 429, refused.text
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "store_datasets=2")
    made = world.client.post("/api/store/datasets", json=body, headers=author)
    assert made.status_code == 201 and made.json()["cases"] == 4, made.text
