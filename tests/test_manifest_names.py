# SPDX-License-Identifier: Apache-2.0
"""The manifest's two names (`core/pack.py` `manifest_name`): `agent.yaml` is the name written
and documented; `pack.yaml`, the name it had before, still loads where it is the only one; both
together are refused. Every reader that finds the manifest by name is here: the loader, the
search path, the bundled agents' digests, the community check (and what it reads of the base),
the price-table guard, validation, signed archives, the registry cache and the agent store.
The fixtures under `tests/fixtures` are named `agent.yaml`, as every agent written now is, so the
rest of the suite is the new name's coverage, and this file the old name's: with one small
fixture kept as `pack.yaml` on purpose (`tests/fixtures/mini-agents/referral-mini`), which the
order-hook tests decide with (tests/test_order_sign.py)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari.cli import main
from mendari.core import reference
from mendari.core.decision import Decision
from mendari.core.pack import (
    LEGACY_MANIFEST,
    MANIFEST,
    MANIFEST_NAMES,
    has_manifest,
    load_pack,
    manifest_name,
    manifests_under,
    pack_digest,
)
from mendari.core.registry import discover, resolve_pack
from mendari.errors import HubError, MendariError, PackValidationError
from mendari.evals.pack_tests import run_pack_tests
from mendari.evals.runs import PRICES_FORMAT, load_prices
from mendari.evals.validation import problems
from mendari.hub import archive, client
from mendari.hub.client import pull
from mendari.server.app import create_app
from tests.conftest import DEMO_AGENT_DIR, INBOX_DIR, ROOT
from tests.test_hub_client import Hub, hub  # noqa: F401  (the registry served over HTTP)
from tests.test_store_uploads import ORG_A, Store, store_world, upload

BOTH = "agent.yaml and pack.yaml are both here"
# The one fixture whose manifest keeps the name it had before, on purpose.
KEPT = ROOT / "tests" / "fixtures" / "mini-agents" / "referral-mini"


def renamed(base: Path, name: str = "renamed", pack_id: str | None = None) -> Path:
    """A copy of the inbox fixture, whose manifest is `agent.yaml` (and, with `pack_id`, under
    that id)."""
    root = base / name
    shutil.copytree(INBOX_DIR, root)
    assert (root / MANIFEST).is_file() and not (root / LEGACY_MANIFEST).exists()
    if pack_id is not None:
        manifest = yaml.safe_load((root / MANIFEST).read_text())
        manifest["id"] = pack_id
        (root / MANIFEST).write_text(yaml.safe_dump(manifest, sort_keys=False))
    return root


def legacy(base: Path, name: str = "legacy", pack_id: str | None = None) -> Path:
    """The same files with the manifest called `pack.yaml`, the name it had before (and, with
    `pack_id`, under that id)."""
    root = renamed(base, name, pack_id)
    (root / MANIFEST).rename(root / LEGACY_MANIFEST)
    return root


def test_the_names() -> None:
    assert (MANIFEST, LEGACY_MANIFEST) == ("agent.yaml", "pack.yaml")
    assert MANIFEST_NAMES == (MANIFEST, LEGACY_MANIFEST)


def test_the_bundled_agent_and_every_scaffold_use_the_new_name(tmp_path: Path) -> None:
    assert manifest_name(DEMO_AGENT_DIR) == MANIFEST
    assert not (DEMO_AGENT_DIR / LEGACY_MANIFEST).exists()
    for flags in ([], ["--agent"]):
        target = tmp_path / ("scaffold" + "".join(flags))
        assert main(["new", str(target), *flags, "--id", "my-clinic/refills"]) == 0
        assert manifest_name(target) == MANIFEST and not (target / LEGACY_MANIFEST).exists()
        assert load_pack(target).manifest_file == MANIFEST
    community = tmp_path / "community-agents" / "refills"
    assert main(["new", str(community), "--community"]) == 0
    assert manifest_name(community) == MANIFEST and not (community / LEGACY_MANIFEST).exists()


def test_one_fixture_keeps_the_old_name_and_works_as_any_other(tmp_path: Path) -> None:
    """Every fixture agent's manifest is `agent.yaml` but one, kept as `pack.yaml` on purpose:
    an agent written before the rename. It loads, validates, passes its behaviour tests, is
    found on a search path and is served, under the name its manifest has."""
    manifests = [
        path
        for path in sorted((ROOT / "tests" / "fixtures").rglob("*.yaml"))
        if path.name.casefold() in MANIFEST_NAMES
    ]
    assert len(manifests) == 11
    assert [path for path in manifests if path.name != MANIFEST] == [KEPT / LEGACY_MANIFEST]
    pack = load_pack(KEPT)
    assert (pack.id, pack.manifest_file) == ("mendari-test/referral-mini", LEGACY_MANIFEST)
    assert manifest_name(KEPT) == LEGACY_MANIFEST and pack_digest(KEPT) == pack.digest
    outcomes = run_pack_tests(Decision(pack))
    assert outcomes and all(outcome.passed for outcome in outcomes)
    assert main(["validate", str(KEPT)]) == 0 and main(["test", str(KEPT)]) == 0
    found = {agent.id: agent.manifest_file for agent in discover([KEPT.parent]).packs}
    assert found[pack.id] == LEGACY_MANIFEST
    assert set(found.values()) == {MANIFEST, LEGACY_MANIFEST}  # beside those of the new name
    served = TestClient(create_app(db_path=tmp_path / "served.db", packs_paths=[KEPT.parent]))
    assert pack.id in [agent["id"] for agent in served.get("/api/agents").json()]


def test_a_folder_with_only_the_old_name_loads_as_before(tmp_path: Path) -> None:
    root = legacy(tmp_path)
    pack, shipped = load_pack(root), load_pack(INBOX_DIR)
    assert (pack.manifest_file, shipped.manifest_file) == (LEGACY_MANIFEST, MANIFEST)
    assert (pack.id, pack.version) == (shipped.id, shipped.version)
    assert pack.manifest == shipped.manifest
    assert resolve_pack(str(root)).digest == pack.digest == pack_digest(root)


def test_the_new_name_loads_the_same_agent_under_another_digest(tmp_path: Path) -> None:
    """The digest hashes each file's name with its bytes, so the same manifest under the other
    name is another digest: a renamed manifest is a new version of its agent."""
    old, new = load_pack(legacy(tmp_path)), load_pack(renamed(tmp_path))
    assert new.manifest_file == MANIFEST
    assert new.manifest == old.manifest and new.digest != old.digest
    assert pack_digest(new.root) == new.digest == load_pack(INBOX_DIR).digest


@pytest.mark.parametrize("extra", ["agent.yaml", "Agent.yaml", "AGENT.YAML"])
def test_both_names_in_one_folder_are_refused_saying_which(tmp_path: Path, extra: str) -> None:
    """`agent.yaml` beside `pack.yaml`, in any letter case: on a filesystem that ignores case
    `Agent.yaml` is `agent.yaml`, and on one that does not it would read as the manifest to a
    person while the engine decided with the other."""
    root = legacy(tmp_path)
    shutil.copy(root / LEGACY_MANIFEST, root / extra)
    with pytest.raises(PackValidationError) as refused:
        load_pack(root)
    (problem,) = refused.value.problems
    assert problem.startswith(f"{extra} and pack.yaml are both here: an agent has one manifest")
    assert "agent.yaml (pack.yaml is the name it had before); remove the other" in problem
    with pytest.raises(PackValidationError, match="are both here"):
        manifest_name(root)
    with pytest.raises(PackValidationError, match="are both here"):
        resolve_pack(str(root))  # a folder given by its path: refused, not looked up as an id
    assert has_manifest(root)  # it has one: the loader is what refuses it


def test_two_spellings_of_one_name_are_refused_where_they_are_two_files(tmp_path: Path) -> None:
    root = renamed(tmp_path)
    try:
        shutil.copy(root / MANIFEST, root / "Agent.yaml")
    except shutil.SameFileError:
        pytest.skip("this filesystem ignores case: the two spellings are one file")
    if len([name for name in os.listdir(root) if name.casefold() == MANIFEST]) != 2:
        pytest.skip("this filesystem ignores case: the two spellings are one file")
    with pytest.raises(PackValidationError, match=r"Agent\.yaml and agent\.yaml are both here"):
        load_pack(root)


def test_the_names_among_a_list_of_paths() -> None:
    """An archive's or a stored version's files: top-level names only."""
    assert manifest_name(["agent.yaml", "data/x.jsonl"]) == MANIFEST
    assert manifest_name(["pack.yaml", "logic/agent.yaml"]) == LEGACY_MANIFEST  # deeper: a file
    assert manifest_name(["data/pack.yaml", "README.md"]) is None
    assert manifest_name({"pack.yaml": b"", "x": b""}) == LEGACY_MANIFEST  # a mapping's keys
    for both in (
        ["agent.yaml", "pack.yaml"],
        ["Agent.yaml", "pack.yaml"],
        ["PACK.yaml", "pack.yaml"],
    ):
        with pytest.raises(PackValidationError, match="are both here"):
            manifest_name(both, "an archive")


def test_a_missing_manifest_is_named_by_the_new_name(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(PackValidationError, match=r"agent\.yaml not found in"):
        load_pack(tmp_path / "empty")
    assert manifest_name(tmp_path / "empty") is None and not has_manifest(tmp_path / "empty")
    assert manifest_name(tmp_path / "nowhere") is None
    (tmp_path / "folder" / "agent.yaml").mkdir(parents=True)  # a folder of that name is no file
    assert manifest_name(tmp_path / "folder") is None and not has_manifest(tmp_path / "folder")


def test_the_search_path_finds_either_name_and_lists_a_folder_with_both(tmp_path: Path) -> None:
    old = legacy(tmp_path / "found", "old")
    new = renamed(tmp_path / "found", "new", pack_id="my-clinic/inbox")
    both = renamed(tmp_path / "found", "both", pack_id="my-clinic/twice")
    shutil.copy(both / MANIFEST, both / LEGACY_MANIFEST)
    assert manifests_under(tmp_path / "found") == sorted(
        [old / LEGACY_MANIFEST, new / MANIFEST, both / MANIFEST, both / LEGACY_MANIFEST]
    )
    found = discover([tmp_path / "found"])
    assert {pack.id: pack.manifest_file for pack in found.packs} == {
        "mendari/patient-inbox-router": LEGACY_MANIFEST,
        "my-clinic/inbox": MANIFEST,
    }
    assert list(found.problems) == [str(both.resolve())]
    assert BOTH in found.problems[str(both.resolve())]
    assert resolve_pack("my-clinic/inbox", [tmp_path / "found"]).root == new.resolve()


def test_the_search_path_orders_folders_of_the_old_name_as_it_did(tmp_path: Path) -> None:
    """Where two copies tie, the first found is served: the order of a tree that holds only
    `pack.yaml` is the order `sorted(base.glob("**/pack.yaml"))` gave."""
    base = tmp_path / "tree"
    for name in ("b", "a", "a/nested", "c/deep/er"):
        (base / name).mkdir(parents=True, exist_ok=True)
        (base / name / LEGACY_MANIFEST).write_text("id: x/y\n")
    assert manifests_under(base) == sorted(base.glob(f"**/{LEGACY_MANIFEST}"))
    (base / LEGACY_MANIFEST).write_text("id: x/y\n")  # in the base itself, too
    assert manifests_under(base) == sorted(base.glob(f"**/{LEGACY_MANIFEST}"))


def test_the_bundled_agents_digests_are_read_under_either_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "bundled"
    old = load_pack(legacy(bundled, "old"))
    new = load_pack(renamed(bundled, "new", pack_id="mendari/renamed"))
    both = renamed(bundled, "both", pack_id="mendari/twice")
    shutil.copy(both / MANIFEST, both / LEGACY_MANIFEST)
    monkeypatch.setattr(reference, "BUNDLED_DIRS", (bundled,))
    reference.reference_packs.cache_clear()
    reference.reference_digests.cache_clear()
    try:
        assert reference.reference_digests() == {old.digest, new.digest}
        assert set(reference.reference_packs()) == {old.id, new.id}  # both names: no reference
    finally:
        reference.reference_packs.cache_clear()
        reference.reference_digests.cache_clear()


@pytest.mark.parametrize("make", [legacy, renamed])
def test_a_price_table_is_never_read_from_an_agents_folder(tmp_path: Path, make: Any) -> None:
    """`load_prices` refuses a table inside an agent's folder, found by its manifest: by either
    name, so no price an agent sets is used."""
    root = make(tmp_path)
    table = {"format": PRICES_FORMAT, "date": "2026-11-01", "source": "a test", "prices": {}}
    (root / "data" / "prices.json").write_text(json.dumps(table))
    with pytest.raises(MendariError, match="never read from Mendari's code or an agent"):
        load_prices(root / "data" / "prices.json")
    (tmp_path / "prices.json").write_text(json.dumps(table))
    assert load_prices(tmp_path / "prices.json").source == "a test"


@pytest.mark.parametrize("make", [legacy, renamed])
def test_a_problem_in_the_manifest_names_the_file_it_is_in(tmp_path: Path, make: Any) -> None:
    root = make(tmp_path)
    name = manifest_name(root)
    assert name is not None
    manifest = yaml.safe_load((root / name).read_text())
    manifest["providers"]["router"]["variants"]["naive_bayes"]["surprise"] = 1  # no such key
    (root / name).write_text(yaml.safe_dump(manifest, sort_keys=False))
    found = problems(load_pack(root))
    assert found and {problem.file for problem in found} == {name}
    assert found[0].place == "providers.router.variants.naive_bayes:".rstrip(":")


# -- signed archives, the registry and its cache --------------------------------------------------


def _key() -> archive.Ed25519PrivateKey:
    return archive.load_private_key(archive.generate_key()[0])


def _extracted(root: Path, dest: Path) -> Any:
    key = _key()
    data = archive.build(load_pack(root), key)
    trusted = {archive.key_id(key.public_key()): key.public_key()}
    return archive.extract(archive.verify(data, trusted), dest, origin="hub")


@pytest.mark.parametrize("make", [legacy, renamed])
def test_an_archive_holds_the_manifest_under_the_name_its_folder_has(
    tmp_path: Path, make: Any
) -> None:
    """The archive's format has no name for the manifest: it lists the agent's files. One built
    with `pack.yaml` inside verifies and loads as it always did."""
    root = make(tmp_path)
    name = manifest_name(root)
    assert name is not None
    # Under another id: a copy of a bundled agent with its id and version but other files (the
    # renamed manifest) is refused as taking its place (`core/pack.py` `_shadow_problem`).
    manifest = yaml.safe_load((root / name).read_text())
    manifest["id"] = "site-a/inbox"
    (root / name).write_text(yaml.safe_dump(manifest, sort_keys=False))
    pack = _extracted(root, tmp_path / "out")
    assert pack.manifest_file == name and pack.digest == load_pack(root).digest
    assert (tmp_path / "out" / name).is_file()
    assert not any((tmp_path / "out" / other).exists() for other in set(MANIFEST_NAMES) - {name})


def test_an_archive_with_both_names_verifies_and_is_refused_when_it_is_loaded(
    tmp_path: Path,
) -> None:
    """Signed by its publisher, every hash right, and still no agent: the extracted folder holds
    both names, so nothing loads it (a registry's publish, a pull, the agent store)."""
    root = legacy(tmp_path)
    pack = load_pack(root)  # loaded while it holds one name: the archive is built from its folder
    shutil.copy(root / LEGACY_MANIFEST, root / MANIFEST)
    key = _key()
    data = archive.build(pack, key)
    verified = archive.verify(data, {archive.key_id(key.public_key()): key.public_key()})
    assert {"agent.yaml", "pack.yaml"} <= set(verified.files)
    with pytest.raises(PackValidationError, match=BOTH):
        archive.extract(verified, tmp_path / "out", origin="hub")


def test_a_registry_takes_and_serves_either_name_and_refuses_both(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Published, pulled and cached: an archive with `pack.yaml` inside, as every archive
    published before the rename holds it, and one with `agent.yaml`. One with both is refused at
    publish, and nothing of it is listed."""
    token, key = hub.namespace("site-a")
    for make, name in ((legacy, "old"), (renamed, "new")):
        root = make(tmp_path, name)
        file = manifest_name(root)
        assert file is not None
        manifest = yaml.safe_load((root / file).read_text())
        manifest["id"] = f"site-a/{name}"
        (root / file).write_text(yaml.safe_dump(manifest, sort_keys=False))
        pack = load_pack(root)
        assert hub.client(token).push(pack, key)["digest"] == pack.digest
        pulled = pull(f"site-a/{name}", hub.client())
        assert (pulled.digest, pulled.manifest_file) == (pack.digest, file)
        assert (client.packs_cache() / "site-a" / name / pack.version / file).is_file()
        assert resolve_pack(f"site-a/{name}").manifest_file == file  # from the cache
    twice = legacy(tmp_path, "twice")
    manifest = yaml.safe_load((twice / LEGACY_MANIFEST).read_text())
    manifest["id"] = "site-a/twice"
    (twice / LEGACY_MANIFEST).write_text(yaml.safe_dump(manifest, sort_keys=False))
    pack = load_pack(twice)
    shutil.copy(twice / LEGACY_MANIFEST, twice / MANIFEST)
    with pytest.raises(HubError, match="are both here"):
        hub.client(token).push(pack, key)
    with pytest.raises(HubError, match="404"):
        hub.client().version("site-a", "twice")


def test_the_bundled_agent_is_published_and_pulled_with_its_manifest(
    hub: Hub,  # noqa: F811
) -> None:
    token, key = hub.namespace("mendari")
    bundled = load_pack(DEMO_AGENT_DIR)
    assert hub.client(token).push(bundled, key)["digest"] == bundled.digest
    pulled = pull(bundled.id, hub.client())
    assert (pulled.digest, pulled.manifest_file) == (bundled.digest, MANIFEST)
    assert (pulled.root / MANIFEST).is_file() and not (pulled.root / LEGACY_MANIFEST).exists()


# -- an organization's agent store ----------------------------------------------------------------


@pytest.fixture
def world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """The agent store on, serving one more agent from disk whose manifest is `pack.yaml`
    (the bundled ones' is `agent.yaml`)."""
    served = legacy(tmp_path / "served", "inbox", pack_id="local-clinic/inbox")
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(served.parent))
    yield from store_world(db_target, tmp_path, monkeypatch)


def _in_store(world: Store, name: str) -> Path:
    """The inbox agent as `clinic-a`'s, its manifest under `name`."""
    make = renamed if name == MANIFEST else legacy
    root = make(world.tmp, f"store-{name}")
    manifest = yaml.safe_load((root / name).read_text())
    manifest["id"] = f"{ORG_A}/inbox-router"
    (root / name).write_text(yaml.safe_dump(manifest, sort_keys=False))
    return root


@pytest.mark.parametrize("name", MANIFEST_NAMES)
def test_the_store_takes_either_name_and_an_edit_cannot_repoint_a_trained_file(
    world: Store, name: str
) -> None:
    """An upload under either name is stored, listed with its manifest's own name, and edited
    through it; and the guard that an edit never changes which files are trained reads the
    manifest the engine loads, whatever it is called: pointing a variant's model at a file the
    same request rewrites is refused under both."""
    author = world.as_(ORG_A, "pack_author")
    root = _in_store(world, name)
    made = upload(world, root)
    assert made.status_code == 201, made.text
    digest = made.json()["digest"]
    detail = world.client.get(f"/api/store/versions/{digest}", headers=author).json()
    assert name in {file["path"] for file in detail["files"]}
    assert not set(MANIFEST_NAMES) - {name} & {file["path"] for file in detail["files"]}
    text = (root / name).read_text()
    model = yaml.safe_load(text)["providers"]["router"]["variants"]["naive_bayes"]["model_file"]
    repointed = world.client.post(
        f"/api/store/versions/{digest}/edits",
        json={
            "files": [{"path": name, "content": text.replace(model, "logic/routing_rules.yaml")}]
        },
        headers=author,
    )
    assert repointed.status_code == 422 and "trained" in repointed.text
    reworded = world.client.post(
        f"/api/store/versions/{digest}/edits",
        json={"files": [{"path": name, "content": text + "\n# a comment\n"}]},
        headers=author,
    )
    assert reworded.status_code == 201, reworded.text
    other = next(iter(set(MANIFEST_NAMES) - {name}))
    added = world.client.post(  # an edit replaces files the version has: never a second manifest
        f"/api/store/versions/{digest}/edits",
        json={"files": [{"path": other, "content": text}]},
        headers=author,
    )
    assert added.status_code == 422 and "not a file of this version" in added.text


def test_the_store_refuses_an_upload_with_both_names(world: Store) -> None:
    root = _in_store(world, LEGACY_MANIFEST)
    pack = load_pack(root)
    shutil.copy(root / LEGACY_MANIFEST, root / MANIFEST)
    refused = world.client.post(
        "/api/store/versions",
        content=archive.build(pack, world.key),
        headers=world.as_(ORG_A, "pack_author"),
    )
    assert refused.status_code == 422 and "are both here" in refused.text
    listed = world.client.get("/api/store/packs", headers=world.as_(ORG_A, "pack_author"))
    assert listed.json() == []


def test_a_copy_of_a_served_agent_renames_the_id_in_its_own_manifest(world: Store) -> None:
    """The copy route rewrites the id in the manifest the served agent has: `agent.yaml` for
    one that uses the new name (it read `pack.yaml` only, and answered a 500), `pack.yaml` for
    one that keeps the old."""
    author = world.as_(ORG_A, "pack_author")
    for source, file in (
        ("mendari/patient-inbox-router", MANIFEST),
        ("local-clinic/inbox", LEGACY_MANIFEST),
    ):
        made = world.client.post(
            "/api/store/copies", json={"pack": source, "name": f"copy-{file[:4]}"}, headers=author
        )
        assert made.status_code == 201, made.text
        body = made.json()
        assert body["pack_id"] == f"{ORG_A}/copy-{file[:4]}" and body["valid"]
        read = world.client.get(
            f"/api/store/versions/{body['digest']}/file", params={"path": file}, headers=author
        )
        assert read.status_code == 200, read.text
        assert f"id: {ORG_A}/copy-{file[:4]}" in read.text
