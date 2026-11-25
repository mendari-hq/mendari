# SPDX-License-Identifier: Apache-2.0
"""An organization's pack store, its authors' routes (LP-20f, ADR-0016 Proposed).

Mounted only with `MENDARI_AGENT_STORE=on` in memberships mode, once the store's prerequisites
are met (`create_app`). The guard lets through only a person who signed in and holds `author`
(the reserved role `pack_author`, which an administrator gives, themself included): never a
key, a client, an EHR launch or open mode. The authors' keys are managed by the organization's
administrators (`keys`, people only). Every route acts for the organization the guard let the
person through for (`org_of`), never one a body, a query or a path names: another
organization's version, key or upload answers as one that does not exist (`404`).

A new version (an upload, a copy, an edit) passes, before any row is written, the archive's own
checks (size, signature by one of the organization's author keys, names, expansion; an upload),
the PHI detector over every text file (a refusal answers `422` with the report: kinds, files,
lines and counts, never a value), the load on the bounded engine, the namespace rule (its id is
`<organization>/<name>`), then the validation and dataset checks (kept with it) and the
hidden-set check: a version holding a case of any organization's hidden evaluation set is stored
`held`, and its uploader is told only that it is held for the operator's review.

No draft is served: no decide route reads the store (LP-20h adds promotion). The security log
names ids, digests and counts only; the access line names the route's template."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from mendari.core.decision import Decision
from mendari.core.pack import MANIFEST, Pack
from mendari.core.pack_store import DRAFT, HELD, PackStore, PhiRejected, StoreError
from mendari.errors import HubError, MendariError
from mendari.server import security_log
from mendari.server.auth import Auth, org_of

TAG = "Agent store"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_EDITED_FILES = 20
MAX_TEST_CASES = 500
HELD_MESSAGE = "held for the operator's review"
_TOP_LEVEL_ID = re.compile(r"^id:[^\n]*$", re.MULTILINE)


class UploadRequest(BaseModel):
    size: int = Field(gt=0, description="the archive's size in bytes")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="the archive's SHA-256, hex")


class CopyRequest(BaseModel):
    pack: str = Field(
        title="Agent", description="the id of an agent this deployment serves from disk"
    )
    name: str = Field(description="the copy's name: its id is <your organization>/<name>")


class EditedFile(BaseModel):
    path: str = Field(max_length=512)
    content: str = Field(max_length=2 * 1024 * 1024)


class EditRequest(BaseModel):
    files: list[EditedFile] = Field(min_length=1, max_length=MAX_EDITED_FILES)


class DatasetRequest(BaseModel):
    name: str = Field(description="its name: lower-case letters, digits and hyphens")
    version: str = Field(description="the digest of the version it is checked against")
    content: str | None = Field(default=None, description="the JSON Lines, under the body limit")
    upload: str | None = Field(default=None, description="or an upload finished in parts")


class JobRequest(BaseModel):
    version: str | None = Field(default=None, description="a store version's digest")
    pack: str | None = Field(
        default=None, title="Agent", description="or an agent served here: the current one"
    )
    dataset: dict[str, str] = Field(
        description='a dataset the agent declares, `{"name": "test"}`, or yours, `{"id": ...}`'
    )
    variants: dict[str, str] | None = Field(default=None, description="slot -> variant")


class ImprovementRequest(BaseModel):
    version: str = Field(description="the digest of the draft the dataset is checked against")
    limit: int = Field(default=1000, ge=1, le=5000, description="the newest corrections at most")


class ProposalDecision(BaseModel):
    accept: bool = Field(description="true: run it with the version's tests; false: dismiss it")


class PromotionRequest(BaseModel):
    version: str = Field(description="the digest of the version to serve")
    reports: list[str] = Field(min_length=1, max_length=20, description="evaluation report ids")
    reference: str = Field(max_length=500, description="where the sign-off is recorded")
    note: str | None = Field(default=None, max_length=2000)
    automate: bool = Field(default=False, description="whether it is meant to automate")


class KeyRequest(BaseModel):
    public_pem: str = Field(max_length=4096, description="an Ed25519 public key, PEM")


def _refused(error: StoreError | HubError) -> HTTPException:
    return HTTPException(getattr(error, "status", 422), str(error))


def _listed(row: Mapping[str, Any]) -> dict[str, Any]:
    """A version as a list shows it. A held one: its digest, its state and nothing else."""
    if row["state"] == HELD:
        return {
            "digest": row["digest"],
            "pack_id": row["pack_id"],
            "version": row["version"],
            "state": HELD,
            "message": HELD_MESSAGE,
            "created_at": row["created_at"],
        }
    return {
        "digest": row["digest"],
        "pack_id": row["pack_id"],
        "version": row["version"],
        "state": row["state"],
        "valid": not row["validation"],
        "source": row["source"],
        "parent_digest": row["parent_digest"],
        "copied_from": row["copied_from"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "state_at": row["state_at"],
        # A version added from a registry (`source` `hub`): where it came from and what was
        # checked when it was (the registry, the namespace, the signing key, when).
        **({"pulled": row["pulled"]} if row.get("pulled") else {}),
    }


def _detail(row: Mapping[str, Any]) -> dict[str, Any]:
    """A version as its own read shows it: its files (paths, hashes, sizes), what the validation
    and the dataset checks found. A held one shows no more than a list does."""
    if row["state"] == HELD:
        return _listed(row)
    return {**_listed(row), "files": row["files"], "validation": row["validation"],
            "checks": row["checks"]}  # fmt: skip


def _renamed(manifest: bytes, pack_id: str) -> bytes:
    """A manifest with its top-level `id` replaced; nothing else of it changes."""
    text = manifest.decode("utf-8")
    if not _TOP_LEVEL_ID.search(text):
        raise HTTPException(422, "the agent's manifest has no top-level id to rename")
    return _TOP_LEVEL_ID.sub(f"id: {pack_id}", text, count=1).encode("utf-8")


def add_store_routes(
    app: FastAPI,
    auth: Auth,
    store: PackStore,
    *,
    served: Callable[[], Mapping[str, Pack]],
    hidden: Any = None,
    claimed: Callable[[str, str], bool] | None = None,
    deadline_s: float | None = None,
    jobs: Any = None,
    limits: Callable[[], Mapping[str, int]] = dict,
    on_promotion: Callable[[], None] = lambda: None,
) -> None:
    """Mount the store's routes. `served`: the packs this deployment serves from disk (what a
    copy is made from); `hidden`: an `EvalsetStore` with the index key, or None (the hidden-set
    check is then "not applicable"); `claimed(org, namespace)`: whether an approved registry
    claim lets `org` use another namespace than its id (with the registry mounted)."""

    def person(request: Request) -> tuple[str, str]:
        """Who the guard let through, by id, and the organization they act for."""
        principal = auth.acting(request)
        if principal.kind != "user":  # the guard lets people only through: fail closed
            raise HTTPException(403, "only a person who signed in may do this")
        return principal.id, org_of(principal)

    def admitted(
        request: Request,
        org: str,
        by: str,
        files: Mapping[str, bytes],
        *,
        source: str,
        parent: str | None = None,
        copied_from: Mapping[str, str] | None = None,
        expect_digest: str | None = None,
    ) -> JSONResponse:
        """Every check a new version passes, then the version as the uploader may see it."""
        try:
            made = store.admit(
                org,
                files,
                by=by,
                source=source,
                parent=parent,
                copied_from=copied_from,
                hidden=hidden,
                claimed=claimed,
                expect_digest=expect_digest,
            )
        except PhiRejected as error:
            security_log.event(
                "store.version.refused",
                outcome="refused",
                reason="phi",
                actor=by,
                org=org,
                count=len(error.report["findings"]),
            )
            return JSONResponse({"detail": str(error), "phi": error.report}, status_code=422)
        except StoreError as error:
            security_log.event(
                "store.version.refused", outcome="refused", reason="checks", actor=by, org=org
            )
            raise _refused(error) from None
        if made.state == HELD:
            security_log.event("store.version.held", actor=by, org=org, ids=[made.digest])
        else:
            security_log.event("store.version.created", actor=by, org=org, ids=[made.digest])
        return JSONResponse(_detail(store.version(org, made.digest)), status_code=201)

    def owned(org: str, digest: str, *, states: tuple[str, ...] | None = None) -> dict[str, Any]:
        try:
            row = store.version(org, digest)
        except StoreError as error:
            raise _refused(error) from None
        if states is not None and row["state"] not in states:
            said = HELD_MESSAGE if row["state"] == HELD else row["state"]
            raise HTTPException(409, f"version {digest} is {said}")
        return row

    @app.get(
        "/api/store/agents",
        tags=[TAG],
        summary="Your organization's agents in the store",
        name="store_agents",
    )
    def store_packs(request: Request) -> list[dict[str, Any]]:
        """The agents in the store of the organization you act for, each with its versions,
        newest first: state, whether it validated, where it came from, who made it and when. A
        held version shows its state and nothing else."""
        _, org = person(request)
        lineages: dict[str, list[dict[str, Any]]] = {}
        for row in store.versions(org):
            lineages.setdefault(row["pack_id"], []).append(_listed(row))
        return [{"pack_id": pack_id, "versions": found} for pack_id, found in lineages.items()]

    @app.get(
        "/api/store/agents/{namespace}/{name}",
        tags=[TAG],
        summary="One agent's versions in the store",
        name="store_agent",
    )
    def store_pack(request: Request, namespace: str, name: str) -> dict[str, Any]:
        """One agent of your organization's store, its versions newest first, each with its
        files, validation and dataset checks. An unknown agent and another organization's are
        one `404`."""
        _, org = person(request)
        found = store.versions(org, f"{namespace}/{name}")
        if not found:
            raise HTTPException(404, "no such agent in this organization's store")
        return {"pack_id": f"{namespace}/{name}", "versions": [_detail(row) for row in found]}

    @app.get("/api/store/versions/{digest}", tags=[TAG], summary="One version, with its card")
    def store_version(request: Request, digest: str) -> dict[str, Any]:
        """One version of your organization's store: its files, validation, dataset checks and
        its Decision Card, rendered here from its files (nothing uploaded about it is shown). A
        held version shows its state and nothing else."""
        _, org = person(request)
        row = owned(org, digest)
        if row["state"] == HELD:
            return _detail(row)
        try:
            card = store.card(org, digest)
        except StoreError as error:
            raise _refused(error) from None
        return {**_detail(row), "card_markdown": card}

    @app.get("/api/store/versions/{digest}/file", tags=[TAG], summary="One text file of a version")
    def store_version_file(request: Request, digest: str, path: str) -> dict[str, Any]:
        """A text file of a version of your organization's store, for its editor (LP-20j): the
        manifest, policy, rules, criteria, wording or tests. Not a held version's, not a trained
        model, not a file that is not UTF-8 text, and at most 1 MiB. Nothing is recorded."""
        _, org = person(request)
        owned(org, digest, states=(DRAFT, "promoted", "retired"))
        try:
            files = store.files(org, digest)
        except StoreError as error:
            raise _refused(error) from None
        data = files.get(path)
        if data is None:
            raise HTTPException(404, "no such file in this version")
        if len(data) > 1024 * 1024:
            raise HTTPException(413, "a file of more than 1 MiB is not edited here")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, "not a text file") from None
        return {"digest": digest, "path": path, "text": text}

    @app.post("/api/store/versions", status_code=201, tags=[TAG], summary="Upload a small archive")
    async def upload_archive(request: Request) -> JSONResponse:
        """A signed agent archive (`mendari archive build`) in one request, under the body limit;
        a larger one goes in parts (`/api/store/uploads`). It must be signed by one of your
        organization's author keys. Answers the new version (`201`), `held` (for the operator's
        review) if it holds a hidden evaluation case, or a refusal naming the check."""
        by, org = person(request)
        body = await request.body()
        return await run_in_threadpool(checked_archive, request, org, by, body)

    def checked_archive(request: Request, org: str, by: str, data: bytes) -> JSONResponse:
        from mendari import settings
        from mendari.hub import archive

        limit = settings.store_max_archive_bytes()
        if len(data) > limit:
            raise HTTPException(413, f"an archive is at most {limit} bytes")
        trusted = store.trusted(org)
        if not trusted:
            raise HTTPException(
                422, "this organization has no author key: an administrator registers one"
            )
        try:
            verified = archive.verify(data, trusted)
            archive.refuse_site_cases(verified.files)
        except HubError as error:
            security_log.event(
                "store.version.refused", outcome="refused", reason="archive", actor=by, org=org
            )
            raise _refused(error) from None
        return admitted(
            request,
            org,
            by,
            verified.files,
            source="upload",
            expect_digest=str(verified.manifest.get("digest")),
        )

    @app.post("/api/store/uploads", status_code=201, tags=[TAG], summary="Begin an archive")
    def begin_upload(request: Request, body: UploadRequest) -> dict[str, Any]:
        """Begin an archive sent in parts: its size and SHA-256. At most three under way at
        once; one left for a day is dropped."""
        by, org = person(request)
        try:
            return store.begin_upload(org, by=by, size=body.size, sha256=body.sha256)
        except StoreError as error:
            raise _refused(error) from None

    @app.put("/api/store/uploads/{upload_id}/parts/{n}", tags=[TAG], summary="Send one part")
    async def upload_part(request: Request, upload_id: str, n: int) -> dict[str, Any]:
        """Part `n` (from 0) of an archive under way, the raw bytes, under the body limit. A part
        sent again replaces it; parts past the declared size are refused."""
        _, org = person(request)
        body = await request.body()
        try:
            return await run_in_threadpool(store.add_part, org, upload_id, n, body)
        except StoreError as error:
            raise _refused(error) from None

    @app.post(
        "/api/store/uploads/{upload_id}/complete",
        status_code=201,
        tags=[TAG],
        summary="Finish an archive and check it",
    )
    def finish_upload(request: Request, upload_id: str) -> JSONResponse:
        """Join the parts, check their size and SHA-256, then the archive as
        `POST /api/store/versions` checks one."""
        by, org = person(request)
        try:
            data = store.finish_upload(org, upload_id)
        except StoreError as error:
            raise _refused(error) from None
        return checked_archive(request, org, by, data)

    @app.post(
        "/api/store/copies",
        status_code=201,
        tags=[TAG],
        summary="Copy an agent into a draft",
        name="copy_agent",
    )
    def copy_pack(request: Request, body: CopyRequest) -> JSONResponse:
        """A draft of an agent this deployment serves from disk, renamed `<your organization>/
        <name>` (a store agent never takes a bundled agent's id) and recording what it was
        copied from (id, version, digest). It runs on the engine for agents the deployment does
        not own, so an agent whose patterns that engine cannot run is refused, saying why."""
        from mendari.hub.archive import pack_files

        by, org = person(request)
        if not NAME.fullmatch(body.name):
            raise HTTPException(422, "a name is lower-case letters, digits and hyphens")
        source = served().get(body.pack)
        if source is None:
            raise HTTPException(404, "no such agent served here to copy")
        files = {
            path.relative_to(source.root).as_posix(): path.read_bytes()
            for path in pack_files(source.root)
        }
        files[MANIFEST] = _renamed(files[MANIFEST], f"{org}/{body.name}")
        copied = {"id": source.id, "version": source.version, "digest": source.digest}
        return admitted(request, org, by, files, source="copy", copied_from=copied)

    @app.post(
        "/api/store/versions/{digest}/edits",
        status_code=201,
        tags=[TAG],
        summary="Edit text files into a new draft",
    )
    def edit_version(request: Request, digest: str, body: EditRequest) -> JSONResponse:
        """A new draft whose parent is this version, with these text files replaced (the
        manifest, policy, rules, criteria, wording, tests). Only files the version has, that are
        UTF-8 text and not a trained model or calibration: a model is trained and uploaded. The
        version itself never changes."""
        by, org = person(request)
        owned(org, digest, states=(DRAFT, "promoted", "retired"))
        try:
            files = store.files(org, digest)
            pack = store.load(org, digest)
        except StoreError as error:
            raise _refused(error) from None
        trained = _trained_files(pack)
        for edited in body.files:
            if edited.path not in files:
                raise HTTPException(422, f"{edited.path}: not a file of this version")
            if edited.path in trained:
                raise HTTPException(422, f"{edited.path}: a trained file is not edited here")
            try:
                files[edited.path].decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(422, f"{edited.path}: not a text file") from None
            files[edited.path] = edited.content.encode("utf-8")
        if _trained_paths(files.get(MANIFEST, b"")) != trained:
            raise HTTPException(422, "an edit does not change which files are trained")
        return admitted(request, org, by, files, source="edit", parent=digest)

    @app.post("/api/store/versions/{digest}/tests", tags=[TAG], summary="Run a draft's tests")
    def test_version(request: Request, digest: str) -> dict[str, Any]:
        """Run the version's behaviour tests on the engine it runs on, as `mendari test` does
        (its own, and those `mendari probe` found): each case's name, whether it passed and why
        not. Nothing is recorded."""
        from mendari.evals.pack_tests import every_case, run_pack_tests

        _, org = person(request)
        owned(org, digest, states=(DRAFT, "promoted", "retired"))
        try:
            pack = store.load(org, digest)
            cases = every_case(pack)
            if len(cases) > MAX_TEST_CASES:
                raise HTTPException(422, f"a version has at most {MAX_TEST_CASES} tests here")
            outcomes = run_pack_tests(Decision(pack, origin_cap=False, deadline_s=deadline_s))
        except (StoreError, HubError) as error:
            raise _refused(error) from None
        except MendariError as error:
            raise HTTPException(422, f"the tests cannot run: {error}") from None
        accepted = [
            item
            for dataset_id in {d for d, _ in store.proposals(org, digest)}
            for item in proposed_results(org, digest, dataset_id, only_accepted=True)
        ]
        return {
            "digest": digest,
            "passed": sum(outcome.passed for outcome in outcomes),
            "failed": sum(not outcome.passed for outcome in outcomes),
            "cases": [
                {"name": o.name, "passed": o.passed, "failures": list(o.failures)} for o in outcomes
            ],
            # The reviewers' corrections accepted as tests (LP-20i), shown apart.
            "proposed": {
                "passed": sum(item["passed"] for item in accepted),
                "failed": sum(not item["passed"] for item in accepted),
                "cases": accepted,
            },
        }

    @app.delete("/api/store/versions/{digest}", tags=[TAG], summary="Withdraw a draft")
    def withdraw_version(request: Request, digest: str) -> dict[str, Any]:
        """Withdraw a draft of your organization's store: its row and events stay, its files are
        freed 30 days later. A held version is the operator's to withdraw."""
        by, org = person(request)
        owned(org, digest, states=(DRAFT,))
        try:
            moved = store.withdraw(org, digest, by=by, allowed=(DRAFT,))
        except StoreError as error:
            raise _refused(error) from None
        security_log.event("store.version.withdrawn", actor=by, org=org, ids=[digest])
        return moved

    # -- datasets, jobs and the comparison (LP-20g) -------------------------------------------

    @app.get("/api/store/datasets", tags=[TAG], summary="Your organization's datasets")
    def store_datasets(request: Request) -> list[dict[str, Any]]:
        """The datasets of the organization you act for, newest first, each with its check
        report (ids and counts). A held one shows its state and nothing else. Never a case."""
        _, org = person(request)
        return store.datasets(org)

    @app.post("/api/store/datasets", status_code=201, tags=[TAG], summary="Upload a dataset")
    def add_dataset(request: Request, body: DatasetRequest) -> JSONResponse:
        """A labelled dataset (JSON Lines) for your organization's jobs, as `content` or an
        upload finished in parts (`upload`), checked against a version: the PHI detector (`422`
        with kinds, files and lines), the dataset checks (kept and shown; a schema or label
        problem refuses it) and the hidden-set check (held on any match, and you are told only
        that). Kept sealed under the deployment's PHI policy."""
        by, org = person(request)
        if (body.content is None) == (body.upload is None):
            raise HTTPException(422, "send the dataset as content or as an upload, not both")
        try:
            # The day's quota before the parts are joined (an upload refused today stays for
            # tomorrow); the size again, with the content's bytes, before it is parsed.
            store.refuse_over_dataset_quota(org)
            raw = (
                body.content.encode("utf-8")
                if body.content is not None
                else store.finish_upload(org, str(body.upload))
            )
            made = store.add_dataset(
                org, raw, by=by, name=body.name, digest=body.version, hidden=hidden
            )
        except PhiRejected as error:
            security_log.event(
                "store.dataset.refused", outcome="refused", reason="phi", actor=by, org=org,
                count=len(error.report["findings"]),
            )  # fmt: skip
            return JSONResponse({"detail": str(error), "phi": error.report}, status_code=422)
        except StoreError as error:
            raise _refused(error) from None
        security_log.event("store.dataset.created", actor=by, org=org, ids=[made["id"]])
        return JSONResponse(made, status_code=201)

    @app.get("/api/store/datasets/{dataset_id}", tags=[TAG], summary="One dataset")
    def store_dataset(request: Request, dataset_id: str) -> dict[str, Any]:
        """One of your organization's datasets and its check report. Never a case."""
        _, org = person(request)
        try:
            return store.dataset(org, dataset_id)
        except StoreError as error:
            raise _refused(error) from None

    def with_jobs() -> Any:
        if jobs is None:
            raise HTTPException(404, "evaluation jobs are not served here")
        return jobs

    @app.get("/api/store/jobs", tags=[TAG], summary="Your organization's evaluation jobs")
    def store_jobs(request: Request) -> list[dict[str, Any]]:
        """The jobs of the organization you act for, newest first: what each runs, on which
        dataset (by its SHA-256), its state and progress as counts."""
        _, org = person(request)
        return with_jobs().jobs(org)

    @app.post("/api/store/jobs", status_code=201, tags=[TAG], summary="Make an evaluation job")
    def make_job(request: Request, body: JobRequest) -> dict[str, Any]:
        """A job evaluating a version of your store (`version`) or an agent served here (`pack`,
        the current one a draft is compared with) on a dataset. Local variants unless named; a
        remote one needs your organization's evaluation quota set and runs on 200 cases at
        most. It runs when `…/advance` is called, a slice at a time. Chained."""
        by, org = person(request)
        try:
            made = with_jobs().create(
                org,
                by=by,
                digest=body.version,
                pack=body.pack,
                dataset=body.dataset,
                variants=body.variants,
                limits=limits(),
            )
        except StoreError as error:
            raise _refused(error) from None
        except MendariError as error:
            raise HTTPException(422, str(error)) from None
        security_log.event("store.job.created", actor=by, org=org, ids=[made["id"]])
        return dict(made)

    @app.get("/api/store/jobs/{job_id}", tags=[TAG], summary="One job's progress")
    def store_job(request: Request, job_id: str) -> dict[str, Any]:
        """One of your organization's jobs: its state, progress as counts, and its report's id
        once done."""
        _, org = person(request)
        try:
            return dict(with_jobs().job(org, job_id))
        except StoreError as error:
            raise _refused(error) from None

    @app.post("/api/store/jobs/{job_id}/advance", tags=[TAG], summary="Run a job's next slice")
    def advance_job(request: Request, job_id: str) -> dict[str, Any]:
        """Run the next slice of a job: as many cases as fit about 20 seconds. Safe to call
        twice: a slice another call holds is not run again. The last slice writes its report
        with its ledger (chained `eval.recorded`)."""
        _, org = person(request)
        try:
            return dict(with_jobs().advance(org, job_id))
        except StoreError as error:
            raise _refused(error) from None

    @app.get("/api/store/comparisons", tags=[TAG], summary="Draft against current")
    def store_comparison(request: Request, draft: str, current: str) -> dict[str, Any]:
        """Two finished jobs of your organization on the same dataset, paired on the same
        cases: each side's figures as k of n with exact intervals, each difference with the
        exact McNemar test, Holm-adjusted, the Brier difference with a paired bootstrap, and the
        cases that changed by your dataset's ids. A figure on too few cases says "insufficient
        evidence". Nothing is recorded."""
        from mendari.evals.store_jobs import compare_jobs

        _, org = person(request)
        try:
            return compare_jobs(with_jobs(), org, draft, current)
        except StoreError as error:
            raise _refused(error) from None
        except MendariError as error:
            raise HTTPException(422, str(error)) from None

    # -- the improvement dataset and proposed tests (LP-20i) ---------------------------------

    def proposed_results(
        org: str, digest: str, dataset_id: str, only_accepted: bool = False
    ) -> list[dict[str, Any]]:
        """Each case of an improvement dataset as a behaviour test of the version: its case id,
        what it expects (the reviewer's label of the primary question), whether the version
        decides it, and what the authors decided of it. Never its input."""
        pack = store.load(org, digest)
        decision = Decision(pack, origin_cap=False, deadline_s=deadline_s)
        decided = store.proposals(org, digest)
        primary = pack.manifest.policy.primary
        results = []
        for record in store.dataset_records(org, dataset_id):
            case = str(record.get("id"))
            state = decided.get((dataset_id, case), "proposed")
            if only_accepted and state != "accepted":
                continue
            wanted = (record.get("labels") or {}).get(primary)
            got = decision.decide(record["input"]).recommendation
            passed = got == wanted
            failures = [] if passed else [f"recommendation: expected {wanted!r}, got {got!r}"]
            results.append(
                {
                    "case": case,
                    "expect": {"recommendation": wanted},
                    "passed": passed,
                    "failures": failures,
                    "state": state,
                }
            )
        return results

    @app.post(
        "/api/store/agents/{namespace}/{name}/improvement",
        status_code=201,
        tags=[TAG],
        summary="Build the improvement dataset from reviewers' corrections",
    )
    def build_improvement(
        request: Request, namespace: str, name: str, body: ImprovementRequest
    ) -> JSONResponse:
        """A dataset of your organization's decisions of this agent (and of the agent it was
        copied from) that a reviewer resolved: each one's input and final label, with where the
        label came from (the decision, the reviewer by id, when, whether they overrode it). Needs
        `author` and the permission to read inputs: it reads patients' inputs, each read chained.
        Kept sealed in your organization's store; it can be a job's dataset and offers the draft
        proposed tests, and is never downloadable. Chained with counts only."""
        from mendari.evals.corrections import corrections_dataset, jsonl

        principal = auth.acting(request)
        by, org = person(request)
        if not principal.may("read_input"):
            raise HTTPException(
                403, "building it reads patients' inputs: it needs the permission to read them"
            )
        pack_id = f"{namespace}/{name}"
        row = owned(org, body.version)
        if row["pack_id"] != pack_id:
            raise HTTPException(422, "that version is not one of this agent's")
        sources = [pack_id]
        current: Mapping[str, Any] | None = row
        while current is not None:  # the pack its lineage was copied from
            if current["copied_from"]:
                sources.append(str(current["copied_from"]["id"]))
                break
            parent = current["parent_digest"]
            current = None if parent is None else store.version(org, parent)
        try:  # the quota before any patient's input is read (each read is chained)
            store.refuse_over_dataset_quota(org)
        except StoreError as error:
            raise _refused(error) from None
        records, skipped = corrections_dataset(
            store.store, org=org, pack_ids=sources, limit=body.limit, reader=principal.actor
        )
        if not records:
            raise HTTPException(422, f"no reviewer's correction to build from ({skipped} skipped)")
        try:
            made = store.add_dataset(
                org, jsonl(records), by=by, name="improvement", digest=body.version,
                kind="improvement",
            )  # fmt: skip
        except StoreError as error:
            raise _refused(error) from None
        security_log.event(
            "store.dataset.created", actor=by, org=org, ids=[made["id"]], count=len(records)
        )
        return JSONResponse({**made, "skipped": skipped}, status_code=201)

    @app.get(
        "/api/store/versions/{digest}/proposed-tests",
        tags=[TAG],
        summary="The reviewers' corrections as proposed tests",
    )
    def proposed_tests(request: Request, digest: str, dataset: str) -> dict[str, Any]:
        """Each case of an improvement dataset run against this version as a behaviour test:
        its case id, the label it expects, whether the version decides it, and whether you
        accepted or dismissed it. Never its input. They stay beside the version, never in its
        files. Nothing is recorded."""
        _, org = person(request)
        owned(org, digest, states=(DRAFT, "promoted", "retired"))
        try:
            if store.dataset(org, dataset).get("kind") != "improvement":
                raise HTTPException(422, "proposed tests come from an improvement dataset")
            results = proposed_results(org, digest, dataset)
        except StoreError as error:
            raise _refused(error) from None
        return {
            "digest": digest,
            "dataset": dataset,
            "passed": sum(r["passed"] for r in results),
            "failed": sum(not r["passed"] for r in results),
            "cases": results,
        }

    @app.post(
        "/api/store/versions/{digest}/proposed-tests/{dataset_id}/{case_id}",
        tags=[TAG],
        summary="Accept or dismiss a proposed test",
    )
    def decide_proposed_test(
        request: Request, digest: str, dataset_id: str, case_id: str, body: ProposalDecision
    ) -> dict[str, Any]:
        """Accept a proposed test (it runs with the version's own tests, shown apart) or dismiss
        it. Chained, naming the dataset and the case, never an input."""
        by, org = person(request)
        owned(org, digest, states=(DRAFT, "promoted", "retired"))
        try:
            return store.decide_proposal(
                org, digest, dataset_id, case_id, by=by, accept=body.accept
            )
        except StoreError as error:
            raise _refused(error) from None

    # -- promotion (LP-20h) -------------------------------------------------------------------

    @app.get("/api/store/promotions", tags=[TAG], summary="Promotions, and what may be promoted")
    def store_promotions(request: Request) -> dict[str, Any]:
        """For `governance` and administrators: the organization's promotions (in force and
        retired) and the versions that may be promoted (validated, not held or withdrawn), each
        with its authors and the reports of its finished jobs."""
        principal_id, org = person(request)
        finished: dict[str, list[str]] = {}
        for job in jobs.jobs(org) if jobs is not None else []:
            if job["state"] == "done" and "pack" not in job:
                finished.setdefault(str(job["digest"]), []).append(str(job["report_id"]))
        candidates = [
            {
                **_listed(row),
                "authors": store.authors(org, row["digest"]),
                "reports": finished.get(row["digest"], []),
                "may_promote": principal_id not in store.authors(org, row["digest"]),
            }
            for row in store.versions(org)
            if row["state"] in (DRAFT, "retired") and not row["validation"]
        ]
        return {"promotions": store.promotions(org), "versions": candidates}

    @app.post("/api/store/promotions", status_code=201, tags=[TAG], summary="Promote a version")
    def promote_version(request: Request, body: PromotionRequest) -> dict[str, Any]:
        """Serve a validated version of your organization's store to your organization, on the
        evaluation reports named (each one of this version's), with a reference and a note.
        Refused when you made it or any version it was made from. The promoted version of the
        same agent before it is retired. At risk levels 2 and 3 it still automates only with the
        automation approval of its digest. Chained `pack_version.promoted`."""
        by, org = person(request)
        try:
            made = store.promote(
                org,
                body.version,
                approver=by,
                reports=body.reports,
                reference=body.reference,
                note=body.note,
                automate=body.automate,
                served_ids=served(),
            )
        except StoreError as error:
            raise _refused(error) from None
        on_promotion()
        security_log.event("store.version.promoted", actor=by, org=org, ids=[made["id"]])
        return made

    @app.post(
        "/api/store/promotions/{promotion_id}/retire",
        tags=[TAG],
        summary="Withdraw a promotion",
    )
    def retire_promotion(request: Request, promotion_id: str) -> dict[str, Any]:
        """Stop serving a promoted version: the organization is served no version of that agent
        until another is promoted. A store agent's recall. Chained `pack_version.retired`."""
        by, org = person(request)
        try:
            retired = store.retire(org, promotion_id, by=by)
        except StoreError as error:
            raise _refused(error) from None
        on_promotion()
        security_log.event("store.version.retired", actor=by, org=org, ids=[promotion_id])
        return retired

    @app.get("/api/store/keys", tags=[TAG], summary="Your organization's author keys")
    def store_keys(request: Request) -> list[dict[str, Any]]:
        """The author keys of the organization you act for: id, who added it and when, and
        when revoked. Administrators only."""
        _, org = person(request)
        return store.keys(org)

    @app.post("/api/store/keys", status_code=201, tags=[TAG], summary="Register an author key")
    def add_store_key(request: Request, body: KeyRequest) -> dict[str, Any]:
        """Register an author's Ed25519 public key (PEM): an upload must be signed by one of
        your organization's keys that is not revoked. Chained `store_key.added`."""
        by, org = person(request)
        try:
            made = store.add_key(org, body.public_pem, by=by)
        except StoreError as error:
            raise _refused(error) from None
        security_log.event("admin.store_key.added", actor=by, org=org, ids=[made["key_id"]])
        return made

    @app.delete("/api/store/keys/{key_id}", tags=[TAG], summary="Revoke an author key")
    def revoke_store_key(request: Request, key_id: str) -> dict[str, Any]:
        """Revoke one of your organization's author keys: no upload signed by it is taken from
        now on. Chained `store_key.revoked`."""
        by, org = person(request)
        try:
            revoked = store.revoke_key(org, key_id, by=by)
        except StoreError as error:
            raise _refused(error) from None
        security_log.event("admin.store_key.revoked", actor=by, org=org, ids=[key_id])
        return revoked


def _trained_files(pack: Pack) -> set[str]:
    """The files a pack's variants and composite members name as trained (a model, a
    calibration): retrained and uploaded, never edited on the server."""
    found: set[str] = set()
    for slot in pack.manifest.providers.values():
        for variant in slot.variants.values():
            configs = [variant.model_extra or {}]
            members = configs[0].get("members")
            if isinstance(members, dict):
                configs += [m for m in members.values() if isinstance(m, dict)]
            for config in configs:
                for key in ("model_file", "calibration"):
                    if isinstance(config.get(key), str):
                        found.add(config[key])
    return found


def _trained_paths(manifest: bytes) -> set[str]:
    """The trained files an edited manifest names (as `_trained_files` reads a loaded pack's),
    so an edit cannot point a variant's model at a file it rewrites in the same request."""
    import yaml

    try:
        data = yaml.safe_load(manifest.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError):
        return set()
    found: set[str] = set()
    slots = data.get("providers") if isinstance(data, dict) else None
    for slot in slots.values() if isinstance(slots, dict) else []:
        variants = slot.get("variants") if isinstance(slot, dict) else None
        for variant in variants.values() if isinstance(variants, dict) else []:
            if not isinstance(variant, dict):
                continue
            configs = [variant]
            members = variant.get("members")
            if isinstance(members, dict):
                configs += [m for m in members.values() if isinstance(m, dict)]
            for config in configs:
                for key in ("model_file", "calibration"):
                    if isinstance(config.get(key), str):
                        found.add(config[key])
    return found
