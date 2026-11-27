# SPDX-License-Identifier: Apache-2.0
"""Evaluation as a stored job, and draft against current (LP-20g, ADR-0016 Proposed).

There is no worker (owner decision 9): a job is a row that advances in slices, each slice one
request an author (or the command line) makes, and nothing runs when nobody asks.

- **A job** evaluates one version (a store version of the organization, by digest, or an agent this
  deployment serves from disk, by id: the "current" a draft is compared with) on one dataset
  (one the version declares, by name, or one of the organization's uploaded datasets). It is
  chained `store_job.created`.
- **A slice** is claimed in one transaction (`claimed_until`), runs as many cases as fit
  `SLICE_SECONDS`, each decision under the bounded engine's budget and the deployment's
  deadline, and keeps its ledger lines with the job. A slice asked for while another holds the
  claim does nothing; a claim that outlived its function expires and the next call repeats it.
  The lines are those `ledger.build` writes, with each model named instead of numbered, so the
  joined ledger is byte for byte the one a single run would write.
- **Finished**, the job writes one `eval_reports` row with its ledger through `save_report`
  (chained `eval.recorded`), and names it.
- **The comparison** is `paired.compare` over two finished jobs on the same dataset SHA-256, with
  each side's figures (`figures.report`: k of n with exact intervals) and the changed cases by
  the dataset's own ids. A figure under the method's floor reads "insufficient evidence".
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from mendari.core import clock, tenancy
from mendari.core.agent import Agent
from mendari.core.agent_store import WITHDRAWN, AgentStore, StoreError
from mendari.core.audit import AuditStore, _canonical, _now
from mendari.core.decision import Decision, DecisionResult
from mendari.errors import MendariError
from mendari.evals import figures, paired
from mendari.evals import ledger as ledgers
from mendari.evals.harness import Dataset, record_problems
from mendari.evals.result import result_from_ledger, result_sha256

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
SLICE_SECONDS = 20.0  # well under a function's limit (vercel.json: 300 s)
CLAIM_SECONDS = 120  # a slice not finished by then is repeated by the next call
REMOTE_LIMIT = 200  # a remote arm's cases, as the evaluation route caps them
MAX_CASES = 5000


def _lines(built: ledgers.Ledger) -> list[dict[str, Any]]:
    """A slice's ledger case lines, each model named instead of numbered."""
    models = built.header["models"]
    named = []
    for case in built.cases:
        q = {
            question: [*answer[:4], None if answer[4] is None else models[answer[4]], answer[5]]
            for question, answer in case["q"].items()
        }
        named.append({**case, "q": q})
    return named


def _joined(
    header: dict[str, Any],
    lines: list[dict[str, Any]],
    n: int,
    limit: int | None,
    order: Sequence[str],
) -> ledgers.Ledger:
    """The ledger a single run over every case writes: the slices' lines in order, models
    numbered in the order they first appear, reading each case's answers in the manifest's
    question order (`order`), as a single run does. The job keeps its lines and header with
    sorted keys, which would number models in another order when the questions are not in
    alphabetical order."""
    models: list[str] = []
    cases = []
    for line in lines:
        q = {}
        for question in [question for question in order if question in line["q"]]:
            answer = line["q"][question]
            model = answer[4]
            if model is not None and model not in models:
                models.append(model)
            q[question] = [*answer[:4], None if model is None else models.index(model), answer[5]]
        cases.append({**line, "q": q})
    joined = {
        **header,
        "dataset": {**header["dataset"], "n": n, "limit": limit},
        "cases": {**header["cases"], "n": len(cases)},
        "models": models,
    }
    return ledgers.Ledger(joined, cases)


class JobStore:
    """Evaluation jobs over an `AgentStore` (its database and chain). `served()` is what this
    deployment serves from disk, a job's "current" when nothing is promoted; `decision_for` builds
    a version's `Decision` (its variants, the deployment's deadline and budget)."""

    def __init__(
        self,
        agents: AgentStore,
        *,
        served: Callable[[], Mapping[str, Agent]],
        decision_for: Callable[[Agent, Mapping[str, str]], Decision],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.agents = agents
        self.store: AuditStore = agents.store
        self.served = served
        self.decision_for = decision_for
        self.clock = clock

    # -- what a job runs ---------------------------------------------------------------------

    def _target(
        self, org: str, digest: str | None, agent: str | None
    ) -> tuple[Agent, dict[str, Any]]:
        """The agent a job evaluates, and how the job names it."""
        if (digest is None) == (agent is None):
            raise StoreError("a job names a version (its digest) or an agent served here, not both")
        if digest is not None:
            row = self.agents.version(org, digest)
            if row["state"] == WITHDRAWN:
                raise StoreError(f"version {digest} is {row['state']}", 409)
            return self.agents.load(org, digest), {"digest": digest}
        found = self.served().get(str(agent))
        if found is None:
            raise StoreError("no such agent served here", 404)
        return found, {"agent": found.id, "digest": found.digest}

    def _dataset(self, org: str, agent: Agent, ref: Mapping[str, str]) -> Dataset:
        """A job's dataset: one the agent declares (`{"name": ...}`) or one of the organization's
        uploaded datasets (`{"id": ...}`), loaded with its case key."""
        from mendari.evals.harness import declared_dataset, load_dataset

        if "name" in ref:
            if ref["name"] not in agent.manifest.evaluation.datasets:
                raise StoreError(f"{agent.id} declares no dataset {ref['name']!r}")
            return declared_dataset(agent, ref["name"])
        raw, name = self.agents.dataset_bytes(org, str(ref.get("id")))
        return load_dataset(f"{name}.jsonl", name=name, raw=raw, label=name)

    # -- writes ------------------------------------------------------------------------------

    def create(
        self,
        org: str,
        *,
        by: str,
        digest: str | None = None,
        agent: str | None = None,
        dataset: Mapping[str, str],
        variants: Mapping[str, str] | None = None,
        limits: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """A queued job of `org` evaluating a version (or an agent served here) on a dataset, with
        `variants` (default: the version's own). A remote variant needs the organization's
        evaluation quota for its provider type (`eval_llm`) to be set, and runs on
        `REMOTE_LIMIT` cases at most. Chained `store_job.created`."""
        target, named = self._target(org, digest, agent)
        data = self._dataset(org, target, dataset)
        problems = record_problems(target.manifest, data)
        if problems:
            raise StoreError(f"the dataset does not fit the agent: {problems[0]}")
        slots = target.manifest.providers
        for slot, variant in (variants or {}).items():
            if slot not in slots or variant not in slots[slot].variants:
                raise StoreError(f"{target.id} has no variant {variant!r} in slot {slot!r}")
        decision = self.decision_for(target, variants or {})
        total = min(len(data.records), MAX_CASES)
        if decision.is_remote:
            types = {info.type for info in decision.provider_info.values()} & {"llm"}
            unset = sorted(t for t in types if f"eval_{t}" not in (limits or {}))
            if unset:
                raise StoreError(
                    "a remote variant needs this organization's evaluation quota "
                    f"({', '.join('eval_' + t for t in unset)} in MENDARI_ORG_DAILY_LIMITS)"
                )
            total = min(total, REMOTE_LIMIT)
        job_id, now = str(uuid.uuid4()), _now()
        config = {"variants": dict(sorted((variants or {}).items()))}
        quota = (limits or {}).get("store_jobs")
        day = clock.today().isoformat()
        with self.store._db.session(exclusive=True) as db:
            used = db.one("SELECT jobs FROM store_usage WHERE org = ? AND day = ?", (org, day))
            if quota is not None and (int(used["jobs"]) if used else 0) >= quota:
                raise StoreError(f"this organization has made its {quota} jobs for today", 429)
            db.all(
                "INSERT INTO store_usage (org, day, uploads, bytes, jobs) VALUES (?, ?, 0, 0, 1) "
                "ON CONFLICT (org, day) DO UPDATE SET jobs = store_usage.jobs + 1",
                (org, day),
            )
            db.all(
                "INSERT INTO store_jobs (id, org, target, dataset, dataset_sha256, dataset_cases, "
                "config, state, done, total, lines, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (job_id, org, _canonical(named), _canonical(dict(dataset)), data.sha256,
                 len(data.records), _canonical(config), QUEUED, total, "[]", by, now),
            )  # fmt: skip
            payload = {"job": job_id, **named, "dataset_sha256": data.sha256, "by": by}
            chained = self.store._bound("store_job.created", payload, org)
            self.store._append(db, f"store_job:{job_id}", "store_job.created", chained)
        return self.job(org, job_id)

    def advance(self, org: str, job_id: str, *, reader: str | None = None) -> dict[str, Any]:
        """Run the job's next slice, if no other call holds it: claim it in one transaction,
        decide as many cases as fit `SLICE_SECONDS`, keep their ledger lines, and, on the last
        slice, write the report with its ledger (`save_report`, chained `eval.recorded`). Called
        twice at once, the second does nothing; each case is decided once. With a `reader` (a
        job on the improvement dataset, whose cases are decisions' inputs), each case decided is
        chained as their read of that decision's input (`chain_input_reads`)."""
        claim, now = uuid.uuid4().hex, datetime.now(UTC)
        with self.store._db.session(exclusive=True) as db:
            row = self._row(db, org, job_id)
            if row["state"] not in (QUEUED, RUNNING):
                return self._shown(row)
            until = row["claimed_until"]
            if until is not None and datetime.fromisoformat(str(until)) > now:
                return self._shown(row)  # another call holds this slice
            db.all(
                "UPDATE store_jobs SET state = ?, claim = ?, claimed_until = ? WHERE id = ?",
                (RUNNING, claim, (now + timedelta(seconds=CLAIM_SECONDS)).isoformat(), job_id),
            )
            done, total = int(row["done"]), int(row["total"])
        try:
            lines, header, latencies, cases = self._slice(org, row, done, total)
        except (MendariError, StoreError) as error:
            return self._fail(org, job_id, claim, str(error))
        if reader is not None:
            self.store.chain_input_reads(org, cases, reader=reader)
        with self.store._db.session(exclusive=True) as db:
            row = self._row(db, org, job_id)
            if row["claim"] != claim:  # this slice outlived its claim: the next call has it
                return self._shown(row)
            kept = json.loads(row["lines"]) + lines
            timing = json.loads(row["latencies"] or "[]") + latencies
            db.all(
                "UPDATE store_jobs SET done = ?, lines = ?, latencies = ?, header = ?, "
                "claim = NULL, claimed_until = NULL WHERE id = ?",
                (done + len(lines), _canonical(kept), _canonical(timing),
                 row["header"] or _canonical(header), job_id),
            )  # fmt: skip
        if done + len(lines) >= total:  # the last slice's header lists the manifest's order
            return self._finish(org, job_id, list(header["questions"]))
        return self.job(org, job_id)

    def _slice(
        self, org: str, row: Mapping[str, Any], done: int, total: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[int], list[str]]:
        """Decide the next cases (one at least) within `SLICE_SECONDS`: their ledger lines (models
        named), the ledger's header, each decision's latency, and the cases' own ids."""
        named = json.loads(row["target"])
        target, _ = self._target(org, *_named(named))
        if target.digest != named["digest"]:
            raise StoreError("the agent this job runs is no longer the one it was created for")
        data = self._dataset(org, target, json.loads(row["dataset"]))
        if data.sha256 != row["dataset_sha256"]:
            raise StoreError("the job's dataset changed since it was created")
        decision = self.decision_for(target, json.loads(row["config"])["variants"])
        started = self.clock()
        results: list[DecisionResult] = []
        for record in data.records[done:total]:
            if results and self.clock() - started > SLICE_SECONDS:
                break
            results.append(decision.decide(record["input"]))
        part = replace(data, records=data.records[done : done + len(results)])
        built = ledgers.build(decision, part, results)
        ids = [str(record.get("id")) for record in part.records]
        return _lines(built), built.header, [int(r.latency_ms) for r in results], ids

    def _finish(self, org: str, job_id: str, order: Sequence[str]) -> dict[str, Any]:
        """Write the report of a job whose every case is decided, once, in one transaction with
        the job's own state. `order` is the manifest's question order."""
        from mendari.evals import metrics

        with self.store._db.session(exclusive=False) as db:
            row = self._row(db, org, job_id)
        if row["state"] != RUNNING or row["report_id"] is not None:
            return self._shown(row)
        header, total = json.loads(row["header"]), int(row["total"])
        n = int(row["dataset_cases"])
        joined = _joined(header, json.loads(row["lines"]), n, None if total == n else total, order)
        found = result_from_ledger(joined)
        run = {
            "id": str(uuid.uuid4()),
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "latency_ms": metrics.percentiles(json.loads(row["latencies"] or "[]")),
            "engine": _engine(),
            "job": job_id,
        }
        report = {**found, "run": run, "result_sha256": result_sha256(found)}
        with tenancy.acting_for(org), self.store._db.session(exclusive=True) as db:
            row = self._row(db, org, job_id)
            if row["state"] != RUNNING or row["report_id"] is not None:
                return self._shown(row)
            recorded = self.store.report_record(report, org)
            self.store._insert_report(db, report, run, recorded, org, joined.dumps(), None)
            db.all(
                "UPDATE store_jobs SET state = ?, report_id = ?, finished_at = ?, lines = ?, "
                "claim = NULL, claimed_until = NULL WHERE id = ?",
                (DONE, run["id"], _now(), "[]", job_id),
            )
        return self.job(org, job_id)

    def _fail(self, org: str, job_id: str, claim: str, why: str) -> dict[str, Any]:
        with self.store._db.session(exclusive=True) as db:
            row = self._row(db, org, job_id)
            if row["claim"] == claim:
                db.all(
                    "UPDATE store_jobs SET state = ?, error = ?, finished_at = ?, claim = NULL, "
                    "claimed_until = NULL WHERE id = ?",
                    (FAILED, why[:300], _now(), job_id),
                )
        return self.job(org, job_id)

    # -- reads -------------------------------------------------------------------------------

    def _row(self, db: Any, org: str, job_id: str) -> Any:
        found = db.one("SELECT * FROM store_jobs WHERE id = ? AND org = ?", (job_id, org))
        if found is None:
            raise StoreError("no such job in this organization", 404)
        return found

    def _shown(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            **json.loads(row["target"]),
            "dataset": json.loads(row["dataset"]),
            "dataset_sha256": row["dataset_sha256"],
            "config": json.loads(row["config"]),
            "state": row["state"],
            "done": int(row["done"]),
            "total": int(row["total"]),
            "report_id": row["report_id"],
            "error": row["error"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
        }

    def job(self, org: str, job_id: str) -> dict[str, Any]:
        """One of `org`'s jobs: its state and progress as counts; never a case."""
        with self.store._db.session(exclusive=False) as db:
            return self._shown(self._row(db, org, job_id))

    def jobs(self, org: str) -> list[dict[str, Any]]:
        with self.store._db.session(exclusive=False) as db:
            rows = db.all(
                "SELECT * FROM store_jobs WHERE org = ? ORDER BY created_at DESC, id", (org,)
            )
        return [self._shown(row) for row in rows]


def _named(target: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(digest, agent) as `_target` takes them, from how a job names what it runs."""
    if "agent" in target:
        return None, str(target["agent"])
    return str(target["digest"]), None


def _engine() -> str:
    from mendari import __version__

    return __version__


def compare_jobs(jobs: JobStore, org: str, draft: str, current: str) -> dict[str, Any]:
    """Draft against current: `paired.compare` of two of `org`'s finished jobs on the same
    dataset (by its SHA-256), each side's figures as k of n with exact intervals
    (`figures.report`), and the cases that changed by the dataset's own ids. A figure on too few
    cases reads "insufficient evidence" (the method's floor)."""
    a, b = jobs.job(org, draft), jobs.job(org, current)
    for name, job in (("draft", a), ("current", b)):
        if job["state"] != DONE:
            raise StoreError(f"the {name} job has not finished ({job['state']})", 409)
    if a["dataset_sha256"] != b["dataset_sha256"]:
        raise StoreError(
            "the two jobs ran on different datasets: compare runs on the same cases", 422
        )
    reports = {}
    with jobs.store._db.session(exclusive=False) as db:
        for side, job in (("draft", a), ("current", b)):
            row = db.one(
                "SELECT report, ledger FROM eval_reports WHERE id = ? AND org = ?",
                (job["report_id"], org),
            )
            if row is None or row["ledger"] is None:
                raise StoreError(f"the {side} job's report is not kept", 409)
            reports[side] = (
                json.loads(row["report"]),
                ledgers.Ledger.loads(row["ledger"].encode()),
            )
    compared = paired.compare(
        reports["draft"][1],
        reports["current"][1],
        dataset_sha256=a["dataset_sha256"],
        lineage=True,
    )
    target, _ = jobs._target(org, *_named({k: a[k] for k in ("digest", "agent") if k in a}))
    data = jobs._dataset(org, target, a["dataset"])
    ids = {
        ledgers.case_hash(data.case_key, record["id"]): str(record["id"]) for record in data.records
    }
    compared["changed"] = [
        {"case": ids.get(item["case"], item["case"]), "changes": item["changes"]}
        for item in compared["changed"]
    ]
    return {
        **compared,
        "draft_job": a["id"],
        "current_job": b["id"],
        "figures_draft": figures.report(reports["draft"][0]),
        "figures_current": figures.report(reports["current"][0]),
    }
