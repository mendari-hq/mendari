from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from mendari import Pack, load_pack
from mendari.core.pack import pack_digest
from mendari.core.registry import resolve_pack
from mendari.errors import PackNotFoundError, PackValidationError
from tests.conftest import INBOX_DIR


def test_reference_pack_loads(inbox_pack: Pack) -> None:
    assert inbox_pack.id == "mendari/patient-inbox-router"
    assert inbox_pack.manifest.policy.primary == "destination"
    assert inbox_pack.digest.startswith("sha256:")


def test_resolve_by_id_and_version(inbox_pack: Pack) -> None:
    assert resolve_pack("mendari/patient-inbox-router").root == inbox_pack.root
    pinned = f"mendari/patient-inbox-router@{inbox_pack.version}"
    assert resolve_pack(pinned).version == inbox_pack.version
    with pytest.raises(PackNotFoundError):
        resolve_pack("mendari/patient-inbox-router@9.9.9")


def test_output_schema_lists_primary_labels(inbox_pack: Pack) -> None:
    schema = inbox_pack.output_schema
    assert set(schema["properties"]["recommendation"]["enum"]) == {
        "front_desk",
        "medical_assistant",
        "nurse",
        "physician",
        "billing",
        None,
    }


def test_unknown_question_in_provider_slot(pack_copy: Callable[..., Path]) -> None:
    def edit(m: dict) -> None:
        m["providers"]["router"]["answers"] = ["destination", "missing"]

    with pytest.raises(PackValidationError, match="unknown question 'missing'"):
        load_pack(pack_copy(manifest=edit))


def test_question_answered_twice(pack_copy: Callable[..., Path]) -> None:
    def edit(m: dict) -> None:
        m["providers"]["safety_screen"]["answers"] = ["urgency", "destination"]

    with pytest.raises(PackValidationError, match="answered by both"):
        load_pack(pack_copy(manifest=edit))


def test_rule_recommendation_must_be_a_primary_label(pack_copy: Callable[..., Path]) -> None:
    def edit(p: dict) -> None:
        p["rules"][0]["recommendation"] = "pharmacist"

    with pytest.raises(PackValidationError, match="pharmacist"):
        load_pack(pack_copy(policy=edit))


def test_thresholds_must_be_ordered(pack_copy: Callable[..., Path]) -> None:
    def edit(p: dict) -> None:
        p["thresholds"] = {"auto": 0.5, "review": 0.8}

    with pytest.raises(PackValidationError, match="review must be <="):
        load_pack(pack_copy(policy=edit))


def test_clinical_support_pack_cannot_declare_auto_rules(pack_copy: Callable[..., Path]) -> None:
    def manifest(m: dict) -> None:
        m["risk"]["level"] = 2

    def policy(p: dict) -> None:
        p["rules"][0]["outcome"] = "AUTO"

    with pytest.raises(PackValidationError, match="risk level 2 forbids"):
        load_pack(pack_copy(manifest=manifest, policy=policy))


def test_paths_cannot_escape_the_pack(pack_copy: Callable[..., Path]) -> None:
    def edit(m: dict) -> None:
        m["inputs"]["schema"] = "../../etc/passwd"

    with pytest.raises(PackValidationError, match="escapes the pack directory"):
        load_pack(pack_copy(manifest=edit))


def test_missing_files_are_reported(pack_copy: Callable[..., Path]) -> None:
    root = pack_copy()
    (root / "fhir" / "mapping.yaml").unlink()
    with pytest.raises(PackValidationError, match=r"missing file 'fhir/mapping\.yaml'"):
        load_pack(root)


def test_digest_tracks_behaviour_but_not_attestations(pack_copy: Callable[..., Path]) -> None:
    root = pack_copy()
    before = pack_digest(root)
    (root / "evals" / "benchmark.json").write_text("{}")
    (root / "DECISION_CARD.md").write_text("changed")
    assert pack_digest(root) == before
    policy = root / "policy.yaml"
    policy.write_text(policy.read_text().replace("auto: 0.90", "auto: 0.91"))
    assert pack_digest(root) != before


# -- a pack reads only its own files: no symbolic link out of it --------------------------------

CANARY = "AWS_SECRET_ACCESS_KEY=canary-outside-the-pack"


@pytest.fixture
def inbox_copy(tmp_path: Path) -> Path:
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    return root


@pytest.mark.parametrize(
    "link", ["CHANGELOG.md", "pack.yaml", "logic/extra", "notes", ".leak", "logic/.x", "a/.b/c"]
)
def test_a_symbolic_link_out_of_the_pack_is_refused(
    inbox_copy: Path, tmp_path: Path, link: str
) -> None:
    # A CHANGELOG.md link to /proc/self/environ once put the server's environment in the card
    # that GET /api/packs/{id} returns; a link to a directory would do the same for its files.
    # Hidden entries are checked too: nothing should read them, and nothing then can.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(CANARY)
    root = inbox_copy
    path = root / link
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    path.symlink_to(outside if link in ("notes", ".leak") else outside / "secret.txt")
    with pytest.raises(PackValidationError) as raised:
        load_pack(root)
    assert raised.value.problems == [f"{link}: a symbolic link that leads outside the pack"]


def test_a_link_loop_is_refused(inbox_copy: Path) -> None:
    (inbox_copy / "loop").symlink_to(inbox_copy / "loop")
    with pytest.raises(PackValidationError, match="loop: a symbolic link that leads outside"):
        load_pack(inbox_copy)


def test_a_symbolic_link_inside_the_pack_is_allowed(inbox_copy: Path) -> None:
    root = inbox_copy
    (root / "notes").symlink_to(root / "logic")
    (root / "HISTORY.md").symlink_to("CHANGELOG.md")
    assert load_pack(root).id == "mendari/patient-inbox-router"


def test_the_card_reads_the_changelog_only_inside_the_pack(
    inbox_copy: Path, tmp_path: Path
) -> None:
    from mendari.core.card import render_card

    secret = tmp_path / "secret.txt"
    secret.write_text(CANARY)
    root = inbox_copy
    pack = load_pack(root)
    (root / "CHANGELOG.md").unlink()
    (root / "CHANGELOG.md").symlink_to(secret)  # after the load's own check
    with pytest.raises(PackValidationError, match="escapes the pack directory"):
        render_card(pack)


def test_card_write_never_writes_through_a_symbolic_link(
    inbox_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mendari.cli import main

    root = inbox_copy
    (root / "notes.md").write_text("mine\n")
    (root / "DECISION_CARD.md").unlink()
    (root / "DECISION_CARD.md").symlink_to("notes.md")
    assert main(["card", str(root), "--write"]) != 0
    assert "symbolic link" in capsys.readouterr().err
    assert (root / "notes.md").read_text() == "mine\n"
