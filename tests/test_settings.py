from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mendari.cli import main
from mendari.server.app import create_app
from mendari.settings import providers_for
from tests.conftest import INBOX_DIR

FIXTURES = Path(__file__).parent / "fixtures" / "packs"
INBOX_SLOTS = {
    "router": {"naive_bayes", "rules", "jev"},
    "safety_screen": {"rules", "rules_and_jev"},
}


def test_unscoped_settings_apply_only_where_the_slot_and_variant_exist() -> None:
    chosen, skipped = providers_for(
        "mendari/patient-inbox-router",
        INBOX_SLOTS,
        {"router": "jev", "notes": "jev", "safety_screen": "llm"},
    )
    assert chosen == {"router": "jev"}  # `notes` is another pack's slot: not applicable here
    assert skipped == ["safety_screen=llm"]  # this pack's slot, but no such variant


def test_pack_scoped_settings_win_and_stay_in_their_pack() -> None:
    settings = {
        "router": "jev",
        "mendari/patient-inbox-router:router": "rules",
        "mendari/clinical-trial-prescreener:notes": "jev",
        "mendari/patient-inbox-router:nope": "x",
    }
    chosen, skipped = providers_for("mendari/patient-inbox-router", INBOX_SLOTS, settings)
    assert chosen == {"router": "rules"}
    assert skipped == ["mendari/patient-inbox-router:nope=x"]


def test_the_server_reports_settings_it_skipped(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "audit.db",
        packs_paths=[INBOX_DIR.parent, FIXTURES],
        providers={
            "safety_screen": "rules",
            "mendari/patient-inbox-router:router": "rules",
            "mendari-test/trial-mini:screen": "llm",
        },
    )
    health = TestClient(app).get("/api/health").json()
    assert health["providers"]["mendari/patient-inbox-router"]["router"] == "rules"
    assert health["providers"]["mendari-test/trial-mini"] == {"screen": "rules"}
    assert health["provider_settings_skipped"] == {
        "mendari-test/trial-mini": ["mendari-test/trial-mini:screen=llm"]
    }


def test_the_cli_refuses_settings_that_cannot_apply(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["test", str(FIXTURES / "trial-mini"), "--provider", "screen=llm"]) != 0
    assert "do not apply to mendari-test/trial-mini: ['screen=llm']" in capsys.readouterr().err
    assert main(["test", str(FIXTURES / "trial-mini"), "--provider", "router=jev"]) == 0  # n/a
