# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mendari import load_pack
from mendari import settings as mendari_settings
from mendari.cli import main
from mendari.errors import MendariError
from mendari.providers import slots_without_model
from mendari.server.app import create_app
from mendari.settings import parse_providers, providers_for
from tests.conftest import CHART_DIR, CLINICAL_DIR, TRIAGE_DIR

# The triage fixture's slots and their variants (its manifest), the slots these settings name.
TRIAGE_SLOTS = {
    "router": {"rules", "agent", "llm"},
    "screen": {"rules", "agent", "checked", "repeated", "llm"},
}
TRIAGE = "mendari-test/triage"
CLINICAL = "mendari-test/clinical-review"
CHART = "mendari-test/chart-summary"


def test_unscoped_settings_apply_only_where_the_slot_and_variant_exist() -> None:
    chosen, skipped = providers_for(
        TRIAGE,
        TRIAGE_SLOTS,
        {"router": "llm", "reader": "agent", "screen": "keyword"},
    )
    assert chosen == {"router": "llm"}  # `reader` is another agent's slot: not applicable here
    assert skipped == ["screen=keyword"]  # this agent's slot, but no such variant


def test_pack_scoped_settings_win_and_stay_in_their_pack() -> None:
    settings = {
        "router": "llm",
        f"{TRIAGE}:router": "rules",
        f"{CLINICAL}:reader": "agent",
        f"{TRIAGE}:nope": "x",
    }
    chosen, skipped = providers_for(TRIAGE, TRIAGE_SLOTS, settings)
    assert chosen == {"router": "rules"}
    assert skipped == [f"{TRIAGE}:nope=x"]


def test_the_server_reports_settings_it_skipped(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "audit.db",
        packs_paths=[TRIAGE_DIR, CLINICAL_DIR],
        providers={
            "screen": "checked",
            f"{TRIAGE}:router": "rules",
            f"{CLINICAL}:reader": "llm",  # the clinical-review fixture has no `llm` reader
        },
    )
    health = TestClient(app).get("/api/health").json()
    assert health["providers"][TRIAGE] == {"router": "rules", "screen": "checked"}
    assert health["providers"][CLINICAL] == {"reader": "rules"}
    assert health["provider_settings_skipped"] == {CLINICAL: [f"{CLINICAL}:reader=llm"]}


def test_the_cli_refuses_settings_that_cannot_apply(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    triage = str(TRIAGE_DIR)
    assert main(["test", triage, "--provider", "screen=keyword"]) != 0
    assert f"do not apply to {TRIAGE}: ['screen=keyword']" in capsys.readouterr().err
    # Typed, a setting that applies to nothing in the pack is a mistake (LP-15)...
    assert main(["test", triage, "--provider", "reader=llm"]) == 1
    err = capsys.readouterr().err
    assert "provider setting reader=llm: the agent has no slot 'reader'" in err
    assert main(["test", triage, "--provider", "other/pack:screen=llm"]) == 1
    assert "names other/pack, not this agent" in capsys.readouterr().err
    # ...but MENDARI_PROVIDERS, a deployment's list for every pack, only warns.
    monkeypatch.setenv("MENDARI_PROVIDERS", "reader=llm")
    assert main(["test", triage]) == 0
    assert "warning: MENDARI_PROVIDERS: provider setting reader=llm" in capsys.readouterr().err


def served(tmp_path: Path, providers: dict[str, str] | None = None) -> dict[str, object]:
    """/api/health of a server started as `mendari serve` starts one, serving the test fixtures'
    agents (only they: whatever else is bundled has slots of its own)."""
    app = create_app(
        db_path=tmp_path / "a.db",
        providers=providers,
        packs_paths=[TRIAGE_DIR, CLINICAL_DIR, CHART_DIR],
    )
    return dict(TestClient(app).get("/api/health").json())


def test_settings_decide_every_slot_they_apply_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setting wins in every pack it applies to; one another pack's variant names (a typo) is
    skipped and leaves the pack's default in place; a pack-scoped one wins over an unscoped one."""
    monkeypatch.setenv(
        "MENDARI_PROVIDERS",
        f"screen=checked,reader=agent,router=keyword,{CLINICAL}:reader=rules",
    )
    health = served(tmp_path, parse_providers(None))
    providers = health["providers"]
    triage = {"router": "rules", "screen": "checked"}  # `keyword` is no triage router
    assert providers[TRIAGE] == triage  # type: ignore[index]
    assert providers[CLINICAL] == {"reader": "rules"}  # type: ignore[index]
    assert providers[CHART] == {"reader": "agent"}  # type: ignore[index]
    assert health["provider_settings_skipped"] == {TRIAGE: ["router=keyword"]}


def test_a_slot_runs_without_a_model_only_where_its_pack_offers_one(
    agent_copy: Callable[..., Path],
) -> None:
    triage = load_pack(TRIAGE_DIR)
    assert slots_without_model(triage, {}) == ["router", "screen"]
    # A composite with a model member reads with a model: the screen's agent, checked by rules.
    assert slots_without_model(triage, {"screen": "checked"}) == ["router"]
    assert slots_without_model(triage, {"screen": "agent", "router": "llm"}) == []

    def rules_only(manifest: dict[str, object]) -> None:
        variants = manifest["providers"]["router"]["variants"]  # type: ignore[index]
        for name in ("agent", "llm"):
            del variants[name]

    offline = load_pack(agent_copy(manifest=rules_only))
    assert slots_without_model(offline, {}) == ["screen"]  # no model to add to its router


@pytest.mark.parametrize("item", [":reader=rules", "mendari/portal-message-agent:=rules"])
def test_a_scoped_override_with_no_pack_or_no_slot_is_refused(item: str) -> None:
    """It applied nowhere and was never reported as skipped: a typo left the default running."""
    with pytest.raises(MendariError, match="namespace/name:slot=variant"):
        parse_providers([item])


def test_an_empty_daily_limit_is_unset_not_a_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other setting reads an empty value as unset; this one refused to start."""
    monkeypatch.setenv("MENDARI_LLM_DAILY_LIMIT", "  ")
    assert mendari_settings.daily_limits()["llm"] == mendari_settings.DEFAULT_LLM_DAILY_LIMIT
