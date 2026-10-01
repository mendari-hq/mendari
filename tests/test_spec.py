from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from mendari import Pack, load_pack
from mendari.core.pack import pack_digest
from mendari.core.registry import resolve_pack
from mendari.errors import PackNotFoundError, PackValidationError


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
