# SPDX-License-Identifier: Apache-2.0
"""The pack store as a library (LP-20e, `core/pack_store.py`, ADR-0016): versions keyed by the
digest the loader hashed, never changed, on the bounded engine, each organization's own, kept
once per file and loaded from the database alone; `audit verify` holds after every event."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari.cli import main
from mendari.core.audit import AuditStore
from mendari.core.decision import Decision
from mendari.core.origin import ORIGIN_LABELS, deployment_config
from mendari.core.pack import load_pack, pack_digest
from mendari.core.pack_store import PackStore, StoreError
from mendari.core.spec import Outcome
from mendari.hub.archive import pack_files
from mendari.providers import build_provider
from mendari.settings import providers_for
from tests.conftest import TRIAGE_DIR, cpu_time
from tests.support.registry import (
    BOUND_S,
    EMOJI,
    ROUTER_RULES,
    long_message,
    slow_rules,
    triage_copy,
)

ORG_A, ORG_B = "clinic-a", "clinic-b"


def files_of(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in pack_files(root)}


def renamed(
    base: Path, pack_id: str = "clinic-a/inbox-router", changes: dict[str, Any] | None = None
) -> Path:
    """The triage fixture under an organization's id (so not a reference pack), its manifest
    changed by `changes` (a dotted key -> value)."""
    root = base / pack_id.replace("/", "-")
    shutil.copytree(TRIAGE_DIR, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["id"] = pack_id
    for dotted, value in (changes or {}).items():
        *path, last = dotted.split(".")
        target = manifest
        for key in path:
            target = target[key]
        target[last] = value
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return root


@pytest.fixture
def packs(store: AuditStore, tmp_path: Path) -> Iterator[PackStore]:
    yield PackStore(store, scratch=tmp_path / "scratch")


def test_a_version_is_keyed_by_the_digest_the_loader_hashed_and_never_one_it_is_told(
    packs: PackStore, tmp_path: Path
) -> None:
    root = renamed(tmp_path)
    files = files_of(root)
    stored = packs.put(ORG_A, files, by="user-1", source="upload")
    assert stored.digest == pack_digest(root) == load_pack(root).digest
    assert stored.state == "draft" and stored.valid
    # One byte more is another version, under its own digest.
    files[ROUTER_RULES] += b"\n# a comment\n"
    other = packs.put(ORG_A, files, by="user-1", source="upload")
    assert other.digest != stored.digest
    assert [v["digest"] for v in packs.versions(ORG_A)] == [other.digest, stored.digest]


@pytest.mark.parametrize(
    "extra",
    [
        {"docs": b"a file", "docs/notes.md": b"and a file in it"},
        {"docs/notes.md": b"a file in a directory", "docs": b"and a file of that name"},
        {"docs/" + "n" * 300 + ".md": b"a name longer than a filesystem takes"},
    ],
    ids=["file-then-directory", "directory-then-file", "name-too-long"],
)
def test_files_that_cannot_be_written_are_refused_never_a_crash(
    packs: PackStore, tmp_path: Path, extra: dict[str, bytes]
) -> None:
    """A path that cannot be a file on disk is a `StoreError` (a route's 422), as the archive's
    own extraction refuses one, never an `OSError` (a 500); nothing is stored."""
    files = {**files_of(renamed(tmp_path)), **extra}
    with pytest.raises(StoreError, match="cannot be written") as refused:
        packs.put(ORG_A, files, by="user-1", source="upload")
    assert refused.value.status == 422
    assert packs.versions(ORG_A) == []


def test_a_stored_version_never_changes(
    packs: PackStore, store: AuditStore, tmp_path: Path
) -> None:
    """A second put of the same pack is refused, and a row changed past the store (its files,
    its id, its state) fails `audit verify`."""
    files = files_of(renamed(tmp_path))
    stored = packs.put(ORG_A, files, by="user-1", source="upload")
    with pytest.raises(StoreError) as error:
        packs.put(ORG_A, files, by="user-2", source="upload")
    assert error.value.status == 409 and "never changes" in str(error.value)
    assert store.verify()["ok"]
    for column, value in (("files", "[]"), ("pack_id", "clinic-a/other"), ("state", "promoted")):
        with store._db.session(exclusive=True) as db:
            before = db.one(f"SELECT {column} FROM store_versions WHERE id = ?", (stored.id,))
            db.all(f"UPDATE store_versions SET {column} = ? WHERE id = ?", (value, stored.id))
        found = store.verify()
        assert not found["ok"] and found["reason"] == "store", column
        with store._db.session(exclusive=True) as db:
            db.all(
                f"UPDATE store_versions SET {column} = ? WHERE id = ?",
                (before[column], stored.id),
            )
        assert store.verify()["ok"]


def test_an_uploaded_card_and_benchmark_are_not_stored_and_the_card_is_rendered_here(
    packs: PackStore, tmp_path: Path
) -> None:
    root = renamed(tmp_path)
    files = files_of(root)
    files["DECISION_CARD.md"] = b"# Decision Card\n\nClinically validated on 1,000,000 cases.\n"
    files["evals/benchmark.json"] = json.dumps({"accuracy": 0.999, "n": 10**6}).encode()
    files["evals/leaderboard.json"] = b"{}"
    stored = packs.put(ORG_A, files, by="user-1", source="upload")
    kept = {item["path"] for item in packs.version(ORG_A, stored.digest)["files"]}
    assert not any(p.startswith("evals/") or p.endswith("DECISION_CARD.md") for p in kept)
    assert set(packs.files(ORG_A, stored.digest)) == kept
    card = packs.card(ORG_A, stored.digest)
    assert "1,000,000" not in card and "0.999" not in card
    assert "The store shows no figure it did not compute itself" in card


def test_a_store_pack_runs_on_the_bounded_engine_and_its_rules_stop_at_the_budget(
    packs: PackStore, tmp_path: Path
) -> None:
    root = triage_copy(tmp_path, "clinic-a/sorter", rules=slow_rules(), schema=long_message)
    stored = packs.put(ORG_A, files_of(root), by="user-1", source="upload")
    pack = packs.load(ORG_A, stored.digest)
    assert pack.origin == "store" and pack.bounded
    assert pack.origin_label == ORIGIN_LABELS["store"] == "From this organization's store"
    with cpu_time() as spent:
        result = Decision(pack).decide({"message": {"body": EMOJI}})
    assert spent() < BOUND_S
    assert set(result.errors.values()) == {"time_budget"}
    assert result.outcome is not Outcome.AUTO


def test_a_store_pack_never_takes_a_reference_packs_id_and_version(
    packs: PackStore, tmp_path: Path
) -> None:
    """A changed triage fixture under its own id and version is refused (it would take the
    reference pack's place); the reference pack itself, byte for byte, is that pack."""
    root = tmp_path / "triage"
    shutil.copytree(TRIAGE_DIR, root)
    rules = root / ROUTER_RULES
    rules.write_text(rules.read_text() + "\n# changed\n")
    with pytest.raises(StoreError) as error:
        packs.put(ORG_A, files_of(root), by="user-1", source="upload")
    assert "reference" in str(error.value) or "this release" in str(error.value)
    assert packs.versions(ORG_A) == []


def test_a_store_pack_does_not_set_its_own_cost_batching_or_temperature(
    packs: PackStore, tmp_path: Path
) -> None:
    """Its batching switch and an LLM's temperature are the deployment's; a pack from a
    registry keeps them (a change would change its decisions). Unscoped provider settings do not
    reach it: only a setting that names it."""
    variants = yaml.safe_load((TRIAGE_DIR / "agent.yaml").read_text())["providers"]
    llm = dict(variants["router"]["variants"]["llm"])
    llm |= {"batch": False, "temperature": 1.5}
    root = renamed(tmp_path, changes={"providers.router.variants.llm": llm})
    stored = packs.put(ORG_A, files_of(root), by="user-1", source="upload")
    pack = packs.load(ORG_A, stored.digest)
    provider: Any = build_provider(pack, "router", "llm")
    assert provider.settings.batch is True and provider.settings.temperature is None
    hub = load_pack(
        renamed(tmp_path / "hub", "someone/inbox-router", {"providers.router.variants.llm": llm}),
        origin="hub",
    )
    kept: Any = build_provider(hub, "router", "llm")
    assert kept.settings.batch is False and kept.settings.temperature == 1.5
    plain = {"type": "llm", "temperature": 1.5, "members": None}
    assert "temperature" not in deployment_config(plain, store=True)
    assert deployment_config(plain)["temperature"] == 1.5
    assert pack.named_settings_only and not hub.named_settings_only
    slots = {slot: set(spec.variants) for slot, spec in pack.manifest.providers.items()}
    assert providers_for(pack.id, slots, {"router": "llm"}, named_only=True)[0] == {}
    named = {f"{pack.id}:router": "llm"}
    assert providers_for(pack.id, slots, named, named_only=True)[0] == {"router": "llm"}


def test_a_version_of_one_organization_is_not_loaded_for_another(
    packs: PackStore, tmp_path: Path
) -> None:
    stored = packs.put(ORG_A, files_of(renamed(tmp_path)), by="user-1", source="upload")
    assert packs.load(ORG_A, stored.digest).digest == stored.digest  # cached for clinic-a now
    for read in (packs.load, packs.version, packs.files, packs.card):
        with pytest.raises(StoreError) as error:
            read(ORG_B, stored.digest)
        assert error.value.status == 404
    assert packs.versions(ORG_B) == []
    with pytest.raises(StoreError):
        packs.withdraw(ORG_B, stored.digest, by="user-9")


def test_the_same_file_is_kept_once_and_removed_with_its_last_version(
    packs: PackStore, store: AuditStore, tmp_path: Path
) -> None:
    files = files_of(renamed(tmp_path))
    first = packs.put(ORG_A, files, by="user-1", source="upload")
    files["tests/cases.yaml"] += b"\n# another\n"
    edited = packs.put(ORG_A, files, by="user-1", source="edit", parent=first.digest)
    also = packs.put(ORG_B, files, by="user-2", source="upload")  # the same files, elsewhere
    with store._db.session(exclusive=False) as db:
        kept = db.one("SELECT COUNT(*) AS n FROM store_files")["n"]
    # Every file of the first version once, and the one the edit changed.
    assert kept == len(packs.version(ORG_A, first.digest)["files"]) + 1
    for org, digest in ((ORG_A, first.digest), (ORG_A, edited.digest), (ORG_B, also.digest)):
        packs.withdraw(org, digest, by="user-1", reason="replaced")
    assert packs.purge() == 0  # withdrawn today: kept for 30 days
    assert packs.purge(now=datetime.now(UTC) + timedelta(days=31)) == kept
    with store._db.session(exclusive=False) as db:
        assert db.one("SELECT COUNT(*) AS n FROM store_files")["n"] == 0
    assert len(packs.versions(ORG_A)) == 2  # the rows and events stay
    assert store.verify()["ok"] and packs.verify() == {
        "ok": True,
        "files": 0,
        "versions": 0,
        "freed": 3,
        "problems": [],
    }


def test_an_organization_over_its_bytes_or_its_daily_uploads_is_refused(
    packs: PackStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = files_of(renamed(tmp_path))
    monkeypatch.setenv("MENDARI_STORE_ORG_BYTES", "1000")
    with pytest.raises(StoreError) as error:
        packs.put(ORG_A, files, by="user-1", source="upload")
    assert error.value.status == 413 and "MENDARI_STORE_ORG_BYTES" in str(error.value)
    monkeypatch.delenv("MENDARI_STORE_ORG_BYTES")
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "store_uploads=1")
    packs.put(ORG_A, files, by="user-1", source="upload")
    files["tests/cases.yaml"] += b"\n# another\n"
    with pytest.raises(StoreError) as error:
        packs.put(ORG_A, files, by="user-1", source="upload")
    assert error.value.status == 429
    packs.put(ORG_B, files, by="user-2", source="upload")  # another organization's own quota


def a_dataset(lines: int = 1) -> bytes:
    """The triage fixture's own test cases, `lines` of them, as JSON Lines."""
    kept = (TRIAGE_DIR / "data" / "test.jsonl").read_bytes().splitlines()[:lines]
    return b"\n".join(kept) + b"\n"


def files_bytes(store: AuditStore) -> int:
    with store._db.session(exclusive=False) as db:
        return int(db.one("SELECT SUM(size) AS n FROM store_files")["n"])


def test_a_datasets_bytes_count_against_the_organizations_store_with_the_files(
    packs: PackStore, store: AuditStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dataset is kept in the store's database as a version's files are: its bytes count
    toward MENDARI_STORE_ORG_BYTES with them, refused past it (413) whether the dataset or a
    version comes last. Before, datasets were bounded only by the per-person rate limit."""
    stored = packs.put(ORG_A, files_of(renamed(tmp_path)), by="user-1", source="upload")
    files = files_bytes(store)
    raw = a_dataset(3)
    monkeypatch.setenv("MENDARI_STORE_ORG_BYTES", str(files + len(raw) - 1))
    with pytest.raises(StoreError) as error:
        packs.add_dataset(ORG_A, raw, by="user-1", name="cases", digest=stored.digest)
    assert error.value.status == 413 and "MENDARI_STORE_ORG_BYTES" in str(error.value)
    assert packs.datasets(ORG_A) == []
    monkeypatch.setenv("MENDARI_STORE_ORG_BYTES", str(files + len(raw)))
    packs.add_dataset(ORG_A, raw, by="user-1", name="cases", digest=stored.digest)
    # The store is full: a version with one new file is refused for the dataset's bytes.
    edited = files_of(renamed(tmp_path / "edit"))
    edited["tests/cases.yaml"] += b"\n# another\n"
    with pytest.raises(StoreError) as error:
        packs.put(ORG_A, edited, by="user-1", source="edit", parent=stored.digest)
    assert error.value.status == 413
    # Another organization's store is its own.
    other = packs.put(ORG_B, files_of(renamed(tmp_path / "b", "clinic-b/inbox-router")),
                      by="user-2", source="upload")  # fmt: skip
    assert other.digest


def test_an_organizations_datasets_a_day_are_bounded_and_checked_before_the_costly_work(
    packs: PackStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store_datasets` in MENDARI_ORG_DAILY_LIMITS: past it a dataset is refused (429) before
    the PHI detector, the version's load and the dataset checks run; each organization has its
    own count."""
    from mendari.core import phi_detect

    a = packs.put(ORG_A, files_of(renamed(tmp_path)), by="user-1", source="upload")
    b = packs.put(ORG_B, files_of(renamed(tmp_path / "b", "clinic-b/inbox-router")),
                  by="user-2", source="upload")  # fmt: skip
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "store_datasets=1")
    packs.add_dataset(ORG_A, a_dataset(), by="user-1", name="one", digest=a.digest)

    def costly(*_: Any, **__: Any) -> Any:
        raise AssertionError("the costly work ran past the quota")

    with monkeypatch.context() as patched:
        patched.setattr(phi_detect, "detect_files", costly)
        patched.setattr(PackStore, "load", costly)
        with pytest.raises(StoreError) as error:
            packs.add_dataset(ORG_A, a_dataset(), by="user-1", name="two", digest=a.digest)
    assert error.value.status == 429 and "1 datasets for today" in str(error.value)
    assert [d["name"] for d in packs.datasets(ORG_A)] == ["one"]
    packs.add_dataset(ORG_B, a_dataset(), by="user-2", name="one", digest=b.digest)
    # The versions' own quota is apart: a version is still added today.
    edited = files_of(renamed(tmp_path / "edit"))
    edited["tests/cases.yaml"] += b"\n# another\n"
    packs.put(ORG_A, edited, by="user-1", source="edit", parent=a.digest)


def test_the_daily_versions_quota_is_checked_before_the_archive_is_loaded(
    packs: PackStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past `store_uploads`, an upload is refused (429) before the pack is loaded and
    validated on the bounded engine, which is the costly part, not after."""
    from mendari.core import pack_store

    files = files_of(renamed(tmp_path))
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "store_uploads=1")
    packs.put(ORG_A, files, by="user-1", source="upload")
    files["tests/cases.yaml"] += b"\n# another\n"

    def costly(*_: Any, **__: Any) -> Any:
        raise AssertionError("the pack was loaded past the quota")

    monkeypatch.setattr(pack_store, "_prepare", costly)
    for write in (packs.put, packs.admit):
        with pytest.raises(StoreError) as error:
            write(ORG_A, files, by="user-1", source="upload")
        assert error.value.status == 429 and "1 versions for today" in str(error.value)


def test_a_fresh_process_loads_a_version_from_the_database_alone(
    db_target: str, tmp_path: Path
) -> None:
    with AuditStore(db_target) as first:
        stored = PackStore(first, scratch=tmp_path / "one").put(
            ORG_A, files_of(renamed(tmp_path)), by="user-1", source="upload"
        )
    shutil.rmtree(tmp_path / "clinic-a-inbox-router")
    with AuditStore(db_target) as second:
        fresh = PackStore(second, scratch=tmp_path / "two")
        pack = fresh.load(ORG_A, stored.digest)
        assert pack.digest == stored.digest and pack.root.is_relative_to(tmp_path / "two")
        shutil.rmtree(tmp_path / "two")  # an instance's scratch disk, gone
        assert fresh.load(ORG_A, stored.digest).root.is_dir()


def test_audit_verify_passes_after_every_store_event(
    packs: PackStore, store: AuditStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = files_of(renamed(tmp_path))
    stored = packs.put(ORG_A, files, by="user-1", source="upload")
    assert store.verify()["ok"]
    with pytest.raises(StoreError):  # a refused write chains nothing
        packs.put(ORG_A, files, by="user-1", source="upload")
    assert store.verify()["ok"]
    packs.withdraw(ORG_A, stored.digest, by="user-1")
    assert store.verify()["ok"] and packs.verify()["ok"]
    events = store.subject_history("pack_version", stored.id, ORG_A)
    assert events is not None
    assert [e["event"] for e in events["events"]] == [
        "pack_version.created",
        "pack_version.withdrawn",
    ]
    assert all(e["payload"]["org"] == ORG_A for e in events["events"])
    assert all(e["payload"]["digest"] == stored.digest for e in events["events"])
    assert store.subject_history("pack_version", stored.id, ORG_B) is None


def test_the_operators_commands_list_verify_withdraw_and_purge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = str(tmp_path / "audit.db")
    with AuditStore(target) as store:
        stored = PackStore(store).put(
            ORG_A, files_of(renamed(tmp_path)), by="user-1", source="upload"
        )
    assert main(["store", "versions", "--db", target, "--org", ORG_A]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [v["digest"] for v in listed] == [stored.digest]
    assert main(["store", "verify", "--db", target]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert (
        main(
            ["store", "withdraw", stored.digest, "--db", target, "--org", ORG_A, "--reason", "test"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "withdrawn"
    assert main(["store", "purge", "--db", target]) == 0
    assert main(["audit", "verify", "--db", target]) == 0
