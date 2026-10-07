"""Every surface says what a pack is (core/status.py), and questions read as words.

The five reference packs are synthetic examples, not clinically reviewed and not for patient
care. The line is derived from the manifest's `card.provenance`, never written by the pack;
without it a pack is only "not clinically reviewed". Questions and labels may carry the names a
person reads (`display`, `names`), with the key as words when they do not."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mendari import Decision, Pack
from mendari.core.card import render_card
from mendari.core.registry import discover
from mendari.core.spec import PackManifest, readable
from mendari.core.status import NOT_REVIEWED, SYNTHETIC_EXAMPLE, status_label, title
from mendari.server.app import create_app
from mendari.standards import cds_hooks
from tests.conftest import ROOT
from tests.test_fhir import COMMUNICATION, PATIENT

REFERENCE = sorted((p for p in discover().packs if p.origin == "maintained"), key=lambda p: p.id)


def _manifest(pack: Pack, edit: Any) -> PackManifest:
    raw = pack.manifest.model_dump(by_alias=True, exclude_none=True)
    edit(raw)
    return PackManifest.model_validate(raw)


@pytest.mark.parametrize("pack", REFERENCE, ids=lambda p: p.id)
def test_every_reference_pack_says_it_is_a_synthetic_example(pack: Pack) -> None:
    assert pack.manifest.card.provenance == "synthetic"
    assert pack.status_label == SYNTHETIC_EXAMPLE
    for card in (render_card(pack), (pack.root / "DECISION_CARD.md").read_text()):
        lines = card.splitlines()
        assert lines[0].startswith("# Decision Card: ")
        assert lines[2] == f"**{SYNTHETIC_EXAMPLE}**"


def test_the_line_follows_provenance_and_nothing_else(inbox_pack: Pack) -> None:
    def provenance(value: str | None) -> Any:
        def edit(raw: dict[str, Any]) -> None:
            raw["card"].pop("provenance", None)
            if value is not None:
                raw["card"]["provenance"] = value

        return edit

    assert status_label(_manifest(inbox_pack, provenance("synthetic"))) == SYNTHETIC_EXAMPLE
    for value in (None, "de_identified", "site_records"):
        assert status_label(_manifest(inbox_pack, provenance(value))) == NOT_REVIEWED
    with pytest.raises(ValidationError):
        _manifest(inbox_pack, provenance("clinically_reviewed"))


def test_the_api_says_it(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "s.db"))
    listed = client.get("/api/packs").json()
    assert listed and {p["status_label"] for p in listed} == {SYNTHETIC_EXAMPLE}
    detail = client.get("/api/packs/mendari/patient-inbox-router").json()
    assert detail["status_label"] == SYNTHETIC_EXAMPLE


def test_cds_hooks_say_it_in_discovery_and_on_every_card(router: Decision) -> None:
    service = cds_hooks.discovery([router.pack])["services"][0]
    assert service["usageRequirements"].startswith(SYNTHETIC_EXAMPLE)
    response = cds_hooks.invoke(
        router,
        {
            "hook": "mendari-message-received",
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {"communicationId": "msg-1", "patientId": "pat-1"},
            "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
        },
    )
    assert response["cards"][0]["detail"].splitlines()[0] == f"*{SYNTHETIC_EXAMPLE}*"


def test_the_readme_lists_the_reference_packs_as_synthetic_examples() -> None:
    assert SYNTHETIC_EXAMPLE in (ROOT / "README.md").read_text()


def test_display_names_are_checked(inbox_pack: Pack) -> None:
    def named(**fields: Any) -> Any:
        def edit(raw: dict[str, Any]) -> None:
            raw["questions"]["destination"].update(fields)

        return edit

    manifest = _manifest(inbox_pack, named(display="Team", names={"front_desk": "Reception"}))
    question = manifest.questions["destination"]
    assert title("destination", question) == "Team"
    assert question.name_of("front_desk") == "Reception"
    assert question.name_of("medical_assistant") == "Medical assistant"
    with pytest.raises(ValidationError, match="does not have"):
        _manifest(inbox_pack, named(names={"pharmacy": "Pharmacy"}))
    for display in ("", "x" * 81):
        with pytest.raises(ValidationError):
            _manifest(inbox_pack, named(display=display))


def test_a_0_1_pack_cannot_use_display_names_or_provenance(inbox_pack: Pack) -> None:
    def older(raw: dict[str, Any]) -> None:
        raw["spec_version"] = "0.1"
        raw["policy"]["automation_requires"] = {}

    with pytest.raises(ValidationError) as error:
        _manifest(inbox_pack, older)
    assert "card.provenance" in str(error.value)
    assert "question destination display or names" in str(error.value)


def test_a_key_reads_as_words() -> None:
    assert readable("medical_assistant") == "Medical assistant"
    assert readable("yes") == "Yes"
    assert readable("egfr_30_or_more") == "Egfr 30 or more"  # why packs name their questions
