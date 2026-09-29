from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openhealth.cli import main
from openhealth.server.app import create_app
from openhealth.settings import providers_for
from tests.conftest import INBOX_DIR

FIXTURES = Path(__file__).parent / "fixtures" / "packs"
INBOX_SLOTS = {
    "router": {"naive_bayes", "rules", "jev"},
    "safety_screen": {"rules", "rules_and_jev"},
}


def test_unscoped_settings_apply_only_where_the_slot_and_variant_exist() -> None:
    chosen, skipped = providers_for(
        "openhealth/patient-inbox-router",
        INBOX_SLOTS,
        {"router": "jev", "notes": "jev", "safety_screen": "llm"},
    )
    assert chosen == {"router": "jev"}  # `notes` is another pack's slot: not applicable here
    assert skipped == ["safety_screen=llm"]  # this pack's slot, but no such variant


def test_pack_scoped_settings_win_and_stay_in_their_pack() -> None:
    settings = {
        "router": "jev",
        "openhealth/patient-inbox-router:router": "rules",
        "openhealth/clinical-trial-prescreener:notes": "jev",
        "openhealth/patient-inbox-router:nope": "x",
    }
    chosen, skipped = providers_for("openhealth/patient-inbox-router", INBOX_SLOTS, settings)
    assert chosen == {"router": "rules"}
    assert skipped == ["openhealth/patient-inbox-router:nope=x"]


def test_the_server_reports_settings_it_skipped(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "audit.db",
        packs_paths=[INBOX_DIR.parent, FIXTURES],
        providers={
            "safety_screen": "rules",
            "openhealth/patient-inbox-router:router": "rules",
            "openhealth-test/trial-mini:screen": "llm",
        },
    )
    health = TestClient(app).get("/api/health").json()
    assert health["providers"]["openhealth/patient-inbox-router"]["router"] == "rules"
    assert health["providers"]["openhealth-test/trial-mini"] == {"screen": "rules"}
    assert health["provider_settings_skipped"] == {
        "openhealth-test/trial-mini": ["openhealth-test/trial-mini:screen=llm"]
    }


def test_the_cli_refuses_settings_that_cannot_apply(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["test", str(FIXTURES / "trial-mini"), "--provider", "screen=llm"]) != 0
    assert "do not apply to openhealth-test/trial-mini: ['screen=llm']" in capsys.readouterr().err
    assert main(["test", str(FIXTURES / "trial-mini"), "--provider", "router=jev"]) == 0  # n/a
