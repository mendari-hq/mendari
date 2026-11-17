# SPDX-License-Identifier: Apache-2.0
"""An organization's pack store, its authors' routes (LP-20f, ADR-0016 Proposed).

Mounted only with `MENDARI_PACK_STORE=on` in memberships mode, once the store's prerequisites
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

from mendari.core.decision import Decision
from mendari.core.pack import Pack
from mendari.core.pack_store import DRAFT, HELD, PackStore, PhiRejected, StoreError
from mendari.errors import HubError, MendariError
from mendari.server import security_log
from mendari.server.auth import Auth, org_of

TAG = "Pack store"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_EDITED_FILES = 20
MAX_TEST_CASES = 500
HELD_MESSAGE = "held for the operator's review"
_TOP_LEVEL_ID = re.compile(r"^id:[^\n]*$", re.MULTILINE)


class UploadRequest(BaseModel):
    size: int = Field(gt=0, description="the archive's size in bytes")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="the archive's SHA-256, hex")


class CopyRequest(BaseModel):
    pack: str = Field(description="the id of a pack this deployment serves from disk")
    name: str = Field(description="the copy's name: its id is <your organization>/<name>")


class EditedFile(BaseModel):
    path: str = Field(max_length=512)
    content: str = Field(max_length=2 * 1024 * 1024)


class EditRequest(BaseModel):
    files: list[EditedFile] = Field(min_length=1, max_length=MAX_EDITED_FILES)


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
        raise HTTPException(422, "the pack's manifest has no top-level id to rename")
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

    @app.get("/api/store/packs", tags=[TAG], summary="Your organization's packs in the store")
    def store_packs(request: Request) -> list[dict[str, Any]]:
        """The packs in the store of the organization you act for, each with its versions,
        newest first: state, whether it validated, where it came from, who made it and when. A
        held version shows its state and nothing else."""
        _, org = person(request)
        lineages: dict[str, list[dict[str, Any]]] = {}
        for row in store.versions(org):
            lineages.setdefault(row["pack_id"], []).append(_listed(row))
        return [{"pack_id": pack_id, "versions": found} for pack_id, found in lineages.items()]

    @app.get(
        "/api/store/packs/{namespace}/{name}",
        tags=[TAG],
        summary="One pack's versions in the store",
    )
    def store_pack(request: Request, namespace: str, name: str) -> dict[str, Any]:
        """One pack of your organization's store, its versions newest first, each with its
        files, validation and dataset checks. An unknown pack and another organization's are
        one `404`."""
        _, org = person(request)
        found = store.versions(org, f"{namespace}/{name}")
        if not found:
            raise HTTPException(404, "no such pack in this organization's store")
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

    @app.post("/api/store/versions", status_code=201, tags=[TAG], summary="Upload a small archive")
    async def upload_archive(request: Request) -> JSONResponse:
        """A signed pack archive (`mendari pack build`) in one request, under the body limit;
        a larger one goes in parts (`/api/store/uploads`). It must be signed by one of your
        organization's author keys. Answers the new version (`201`), `held` (for the operator's
        review) if it holds a hidden evaluation case, or a refusal naming the check."""
        by, org = person(request)
        return checked_archive(request, org, by, await request.body())

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
        try:
            return store.add_part(org, upload_id, n, await request.body())
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

    @app.post("/api/store/copies", status_code=201, tags=[TAG], summary="Copy a pack into a draft")
    def copy_pack(request: Request, body: CopyRequest) -> JSONResponse:
        """A draft of a pack this deployment serves from disk, renamed `<your organization>/
        <name>` (a store pack never takes a reference pack's id) and recording what it was
        copied from (id, version, digest). It runs on the engine for packs the deployment does
        not own, so a pack whose patterns that engine cannot run is refused, saying why."""
        from mendari.hub.archive import pack_files

        by, org = person(request)
        if not NAME.fullmatch(body.name):
            raise HTTPException(422, "a name is lower-case letters, digits and hyphens")
        source = served().get(body.pack)
        if source is None:
            raise HTTPException(404, "no such pack served here to copy")
        files = {
            path.relative_to(source.root).as_posix(): path.read_bytes()
            for path in pack_files(source.root)
        }
        files["pack.yaml"] = _renamed(files["pack.yaml"], f"{org}/{body.name}")
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
        if _trained_paths(files.get("pack.yaml", b"")) != trained:
            raise HTTPException(422, "an edit does not change which files are trained")
        return admitted(request, org, by, files, source="edit", parent=digest)

    @app.post("/api/store/versions/{digest}/tests", tags=[TAG], summary="Run a draft's tests")
    def test_version(request: Request, digest: str) -> dict[str, Any]:
        """Run the version's behaviour tests on the engine it runs on, as `mendari test` does:
        each case's name, whether it passed and why not. Nothing is recorded."""
        from mendari.evals.pack_tests import load_cases, run_pack_tests

        _, org = person(request)
        owned(org, digest, states=(DRAFT, "promoted", "retired"))
        try:
            pack = store.load(org, digest)
            cases = load_cases(pack)
            if len(cases) > MAX_TEST_CASES:
                raise HTTPException(422, f"a version has at most {MAX_TEST_CASES} tests here")
            outcomes = run_pack_tests(Decision(pack, origin_cap=False, deadline_s=deadline_s))
        except (StoreError, HubError) as error:
            raise _refused(error) from None
        except MendariError as error:
            raise HTTPException(422, f"the tests cannot run: {error}") from None
        return {
            "digest": digest,
            "passed": sum(outcome.passed for outcome in outcomes),
            "failed": sum(not outcome.passed for outcome in outcomes),
            "cases": [
                {"name": o.name, "passed": o.passed, "failures": list(o.failures)} for o in outcomes
            ],
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
