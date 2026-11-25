# SPDX-License-Identifier: Apache-2.0
"""An organization's pack store, its authors' routes (LP-20f, `server/store_routes.py`): the
switch, the author role, signed uploads (whole or in parts), copies and edits, the PHI check,
and one organization's store invisible to another. The packs uploaded are copies of the triage
fixture under an organization's ids."""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari.core.audit import AuditStore
from mendari.core.pack import load_pack
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.hub import archive
from mendari.server.app import create_app
from tests.conftest import DASHBOARD, TRIAGE_DIR, encrypt_mode, encrypted_store, member
from tests.support.registry import HTTPS, ROUTER_RULES, edit_yaml, with_lookahead
from tests.test_auth import CLIENT, IDP

ORG_A, ORG_B = "clinic-a", "clinic-b"
SECRET_PHONE = "617-555-2368"  # outside the fictional range: a phone the detector finds
TRIAGE = load_pack(TRIAGE_DIR)


@dataclass
class Store:
    client: TestClient
    db: AuditStore
    people: dict[tuple[str, str], dict[str, str]]  # (org, role) -> headers
    key: Any  # clinic-a's author key (registered)
    tmp: Path

    def as_(self, org: str, role: str) -> dict[str, str]:
        found = self.people.get((org, role))
        if found is None:
            found = member(self.db, org=org, roles=[role], sub=f"{role}-{org}")
            self.people[(org, role)] = found
        return found


def _env(monkeypatch: pytest.MonkeyPatch, *, store: str | None = "on") -> None:
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    if store is not None:
        monkeypatch.setenv("MENDARI_AGENT_STORE", store)


@pytest.fixture
def world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    yield from store_world(db_target, tmp_path, monkeypatch)


def store_world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """A memberships-mode server with the pack store on, and clinic-a's author key registered
    by its administrator."""
    _env(monkeypatch)
    app = create_app(db_path=db_target)
    with (
        TestClient(app, base_url=HTTPS, headers=DASHBOARD) as client,
        encrypted_store(db_target) as db,
    ):
        private, public = archive.generate_key()
        made = Store(client, db, {}, archive.load_private_key(private), tmp_path)
        added = client.post(
            "/api/store/keys",
            json={"public_pem": public.decode()},
            headers=made.as_(ORG_A, "admin"),
        )
        assert added.status_code == 201, added.text
        yield made


def pack_dir(base: Path, pack_id: str = f"{ORG_A}/inbox-router", **extra: Any) -> Path:
    """The triage fixture under `pack_id`, with `extra` lines appended to its behaviour
    tests."""
    root = base / pack_id.replace("/", "-")
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(TRIAGE_DIR, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["id"] = pack_id
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    if extra.get("case") is not None:
        cases = root / "tests" / "cases.yaml"
        case = {"name": "planted", "input": extra["case"], "expect": {"not_outcome": "BLOCK"}}
        cases.write_text(cases.read_text() + "\n" + yaml.safe_dump([case], sort_keys=False))
    return root


def upload(world: Store, root: Path, org: str = ORG_A, key: Any = None) -> Any:
    data = archive.build(load_pack(root), key or world.key)
    return world.client.post(
        "/api/store/versions", content=data, headers=world.as_(org, "pack_author")
    )


def test_without_the_switch_nothing_is_mounted_and_packs_are_served_as_before(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, store=None)
    with TestClient(create_app(db_path=db_target), base_url=HTTPS, headers=DASHBOARD) as client:
        routes: Any = client.app
        assert not [r for r in routes.routes if getattr(r, "path", "").startswith("/api/store")]
        with encrypted_store(db_target) as db:
            author = member(db, org=ORG_A, roles=["pack_author", "viewer"], sub="ada")
        assert client.get("/api/store/agents", headers=author).status_code == 404
        assert "store" not in client.get("/api/auth/me", headers=author).json()
        packs = client.get("/api/agents", headers=author).json()
        assert {p["id"] for p in packs} == {p.id for p in map(resolve_pack, _ids(packs))}
        with encrypted_store(db_target) as db, db._db.session(exclusive=False) as session:
            assert session.one("SELECT COUNT(*) AS n FROM store_versions")["n"] == 0


def _ids(packs: list[dict[str, Any]]) -> list[str]:
    return [p["id"] for p in packs]


def test_the_store_refuses_to_start_outside_memberships_mode_or_with_a_prerequisite_missing(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    monkeypatch.setenv("MENDARI_ACCOUNTS", "claims")
    with pytest.raises(MendariError, match="memberships mode"):
        create_app(db_path=db_target)
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    from mendari.core import origin

    monkeypatch.setattr(origin, "NOT_OWNED", frozenset({"hub"}))
    with pytest.raises(MendariError, match="LP-16"):
        create_app(db_path=db_target)
    monkeypatch.setenv("MENDARI_AGENT_STORE", "maybe")
    with pytest.raises(MendariError, match="MENDARI_AGENT_STORE is on or off"):
        create_app(db_path=db_target)


def test_an_author_uploads_a_signed_archive_and_it_is_a_draft(world: Store) -> None:
    root = pack_dir(world.tmp)
    made = upload(world, root)
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["pack_id"], body["state"], body["digest"]) == (
        f"{ORG_A}/inbox-router",
        "draft",
        load_pack(root).digest,
    )
    assert body["valid"]
    me = world.client.get("/api/auth/me", headers=world.as_(ORG_A, "pack_author")).json()
    assert me["store"] is True
    listed = world.client.get("/api/store/agents", headers=world.as_(ORG_A, "pack_author")).json()
    assert listed[0]["pack_id"] == f"{ORG_A}/inbox-router"
    read = world.client.get(
        f"/api/store/versions/{body['digest']}", headers=world.as_(ORG_A, "pack_author")
    ).json()
    assert read["card_markdown"].startswith("# Decision Card")
    assert world.db.verify()["ok"]


def test_an_archive_signed_by_no_key_of_this_organization_is_refused(world: Store) -> None:
    stranger = archive.load_private_key(archive.generate_key()[0])
    refused = upload(world, pack_dir(world.tmp), key=stranger)
    assert refused.status_code == 422
    # clinic-b has no key at all; and clinic-a's key is not clinic-b's.
    root_b = pack_dir(world.tmp, f"{ORG_B}/inbox-router")
    refused_b = upload(world, root_b, org=ORG_B)
    assert refused_b.status_code == 422 and "no author key" in refused_b.text
    # A revoked key signs nothing from then on.
    key_id = archive.key_id(world.key.public_key())
    gone = world.client.delete(f"/api/store/keys/{key_id}", headers=world.as_(ORG_A, "admin"))
    assert gone.status_code == 200
    assert upload(world, pack_dir(world.tmp)).status_code == 422


def test_an_upload_with_phi_like_content_is_rejected_with_a_report_that_holds_no_value(
    world: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    root = pack_dir(world.tmp, case={"message": {"body": f"please call me at {SECRET_PHONE}"}})
    refused = upload(world, root)
    assert refused.status_code == 422, refused.text
    report = refused.json()["phi"]
    assert report["counts"] == {"phone": 1}
    assert report["findings"][0]["file"] == "tests/cases.yaml"
    assert SECRET_PHONE not in refused.text and SECRET_PHONE not in caplog.text
    with world.db._db.session(exclusive=False) as db:
        assert db.one("SELECT COUNT(*) AS n FROM store_versions")["n"] == 0
        assert db.one("SELECT COUNT(*) AS n FROM store_files")["n"] == 0


def test_a_draft_in_organization_a_is_invisible_to_organization_b(world: Store) -> None:
    digest = upload(world, pack_dir(world.tmp)).json()["digest"]
    other = world.as_(ORG_B, "pack_author")
    assert world.client.get("/api/store/agents", headers=other).json() == []
    for method, path, body in (
        ("GET", f"/api/store/versions/{digest}", None),
        ("GET", f"/api/store/agents/{ORG_A}/inbox-router", None),
        ("POST", f"/api/store/versions/{digest}/tests", None),
        ("POST", f"/api/store/versions/{digest}/edits", {"files": [{"path": "x", "content": ""}]}),
        ("DELETE", f"/api/store/versions/{digest}", None),
    ):
        answer = world.client.request(method, path, json=body, headers=other)
        assert answer.status_code == 404, (method, path, answer.text)
        assert digest not in answer.text or "no version" in answer.text
    # The copy source is what this deployment serves from disk, never a store version.
    copied = world.client.post(
        "/api/store/copies", json={"pack": f"{ORG_A}/inbox-router", "name": "x"}, headers=other
    )
    assert copied.status_code == 404
    keys = world.client.get("/api/store/keys", headers=world.as_(ORG_B, "admin")).json()
    assert keys == []


def test_a_copy_takes_a_new_id_in_the_organizations_namespace_and_records_its_source(
    world: Store,
) -> None:
    served = resolve_pack(TRIAGE.id)
    made = world.client.post(
        "/api/store/copies",
        json={"pack": served.id, "name": "inbox-router"},
        headers=world.as_(ORG_A, "pack_author"),
    )
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["pack_id"] == f"{ORG_A}/inbox-router" and body["source"] == "copy"
    assert body["copied_from"] == {
        "id": served.id,
        "version": served.version,
        "digest": served.digest,
    }
    assert body["digest"] != served.digest and body["valid"]
    for bad in ("Not A Name", "../x"):
        refused = world.client.post(
            "/api/store/copies",
            json={"pack": served.id, "name": bad},
            headers=world.as_(ORG_A, "pack_author"),
        )
        assert refused.status_code == 422
    # Mendari's namespace is never a store pack's.
    mine = pack_dir(world.tmp, "mendari/inbox-router-x")
    assert upload(world, mine).status_code == 422


def test_a_copy_of_a_pack_the_bounded_engine_refuses_says_why(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent this deployment serves from its own files whose router has a lookahead: owned,
    it runs on `re`; copied into the store, it would run on the engine for agents the deployment
    does not own, which cannot run it."""
    local = pack_dir(tmp_path / "local", "site/lookahead-router")
    with_lookahead(local)
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(local.parent))
    for world in store_world(db_target, tmp_path, monkeypatch):
        refused = world.client.post(
            "/api/store/copies",
            json={"pack": "site/lookahead-router", "name": "lookahead-copy"},
            headers=world.as_(ORG_A, "pack_author"),
        )
        assert refused.status_code == 422
        assert "lookahead" in refused.text


def test_an_edit_is_a_new_version_and_its_parent_is_unchanged(world: Store) -> None:
    author = world.as_(ORG_A, "pack_author")
    first = upload(world, pack_dir(world.tmp)).json()
    before = world.client.get(f"/api/store/versions/{first['digest']}", headers=author).json()
    rules = (TRIAGE_DIR / ROUTER_RULES).read_text() + "\n# tightened\n"
    edited = world.client.post(
        f"/api/store/versions/{first['digest']}/edits",
        json={"files": [{"path": ROUTER_RULES, "content": rules}]},
        headers=author,
    )
    assert edited.status_code == 201, edited.text
    second = edited.json()
    assert second["parent_digest"] == first["digest"] and second["digest"] != first["digest"]
    after = world.client.get(f"/api/store/versions/{first['digest']}", headers=author).json()
    assert after == before
    tested = world.client.post(f"/api/store/versions/{second['digest']}/tests", headers=author)
    assert tested.status_code == 200 and tested.json()["failed"] == 0
    for path in ("nothing.txt", "data/nothing.jsonl"):  # no file of this version
        refused = world.client.post(
            f"/api/store/versions/{first['digest']}/edits",
            json={"files": [{"path": path, "content": "{}"}]},
            headers=author,
        )
        assert refused.status_code == 422, path


def test_an_archive_in_parts_is_joined_checked_and_bounded(
    world: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    author = world.as_(ORG_A, "pack_author")
    data = archive.build(load_pack(pack_dir(world.tmp)), world.key)
    begun = world.client.post(
        "/api/store/uploads",
        json={"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        headers=author,
    )
    assert begun.status_code == 201, begun.text
    upload_id = begun.json()["upload"]
    half = len(data) // 2
    for n, part in enumerate((data[:half], data[half:])):
        sent = world.client.put(
            f"/api/store/uploads/{upload_id}/parts/{n}", content=part, headers=author
        )
        assert sent.status_code == 200, sent.text
    done = world.client.post(f"/api/store/uploads/{upload_id}/complete", headers=author)
    assert done.status_code == 201 and done.json()["state"] == "draft"
    # A wrong hash, a part past the size, an archive over the limit.
    wrong = world.client.post(
        "/api/store/uploads", json={"size": len(data), "sha256": "0" * 64}, headers=author
    ).json()["upload"]
    world.client.put(f"/api/store/uploads/{wrong}/parts/0", content=data, headers=author)
    assert (
        world.client.post(f"/api/store/uploads/{wrong}/complete", headers=author).status_code == 422
    )
    small = world.client.post(
        "/api/store/uploads", json={"size": 10, "sha256": "0" * 64}, headers=author
    ).json()["upload"]
    over = world.client.put(
        f"/api/store/uploads/{small}/parts/0", content=b"x" * 11, headers=author
    )
    assert over.status_code == 413
    monkeypatch.setenv("MENDARI_STORE_MAX_ARCHIVE_BYTES", "100")
    too_big = world.client.post(
        "/api/store/uploads", json={"size": 101, "sha256": "0" * 64}, headers=author
    )
    assert too_big.status_code == 413


def test_a_key_a_client_and_an_ehr_launch_never_author(world: Store) -> None:
    from mendari.server.auth import PEOPLE_ONLY, PERMISSIONS

    assert "author" in PEOPLE_ONLY and PERMISSIONS["pack_author"] == frozenset({"author"})
    key = world.client.post(
        "/api/auth/keys", json={"role": "service", "name": "x"}, headers=world.as_(ORG_A, "admin")
    ).json()["key"]
    machine = {"authorization": f"Bearer {key}"}
    assert world.client.get("/api/store/agents", headers=machine).status_code == 403
    assert (
        world.client.post("/api/store/versions", content=b"x", headers=machine).status_code == 403
    )


def test_a_viewer_a_reviewer_and_an_administrator_without_the_role_cannot_upload(
    world: Store,
) -> None:
    data = archive.build(load_pack(pack_dir(world.tmp)), world.key)
    for role in ("viewer", "reviewer", "governance", "admin"):
        refused = world.client.post(
            "/api/store/versions", content=data, headers=world.as_(ORG_A, role)
        )
        assert refused.status_code == 403, role
    # Authors' keys are the administrators': an author may not register one.
    pem = archive.generate_key()[1].decode()
    assert (
        world.client.post(
            "/api/store/keys", json={"public_pem": pem}, headers=world.as_(ORG_A, "pack_author")
        ).status_code
        == 403
    )


def test_a_withdrawn_version_is_never_tested_edited_or_served(world: Store) -> None:
    author = world.as_(ORG_A, "pack_author")
    draft = upload(world, pack_dir(world.tmp)).json()["digest"]
    assert world.client.delete(f"/api/store/versions/{draft}", headers=author).status_code == 200
    rules = {"files": [{"path": ROUTER_RULES, "content": "x"}]}
    for method, path, body in (
        ("POST", f"/api/store/versions/{draft}/tests", None),
        ("POST", f"/api/store/versions/{draft}/edits", rules),
        ("DELETE", f"/api/store/versions/{draft}", None),
    ):
        answer = world.client.request(method, path, json=body, headers=author)
        assert answer.status_code == 409, (method, path, answer.text)
    served = world.client.get("/api/agents", headers=world.as_(ORG_A, "viewer")).json()
    assert not any(p["id"].startswith(f"{ORG_A}/") for p in served)


def test_another_sites_page_cannot_upload(world: Store) -> None:
    data = archive.build(load_pack(pack_dir(world.tmp)), world.key)
    headers = {key: value for key, value in world.as_(ORG_A, "pack_author").items()} | {
        "origin": "https://elsewhere.example.org",
        "sec-fetch-site": "cross-site",
    }
    world.client.headers.pop(next(iter(DASHBOARD)), None)
    refused = world.client.post("/api/store/versions", content=data, headers=headers)
    assert refused.status_code == 403


def test_an_edit_never_changes_which_files_are_trained_and_reserved_prefixes_are_refused(
    world: Store,
) -> None:
    author = world.as_(ORG_A, "pack_author")
    root = pack_dir(world.tmp)
    first = upload(world, root).json()

    # An edit that names a file of the version as a variant's calibration, which is trained.
    def calibrated(manifest: dict[str, Any]) -> None:
        manifest["providers"]["screen"]["variants"]["agent"]["calibration"] = ROUTER_RULES

    edit_yaml(root / "agent.yaml", calibrated)
    pointed = (root / "agent.yaml").read_text()
    refused = world.client.post(
        f"/api/store/versions/{first['digest']}/edits",
        json={"files": [{"path": "agent.yaml", "content": pointed}]},
        headers=author,
    )
    assert refused.status_code == 422 and "trained" in refused.text
    db = world.db
    member(db, org="openhealth-hub", roles=["pack_author"], sub="oh")
    from mendari.core.pack_store import _reserved

    assert _reserved("openhealth-hub") and _reserved("mendari") and not _reserved("clinic-a")
