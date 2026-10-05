from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mendari.cli import main
from mendari.core.registry import discover
from mendari.providers import jev, slots_without_model
from mendari.providers.jev import JevProvider
from mendari.server.app import create_app
from mendari.settings import JEV_DEFAULTS, providers_for, server_providers
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


# -- with Jev set up, the reference packs run Mendari + Jev --------------------------------------


def set_up_jev(monkeypatch: pytest.MonkeyPatch, **settings: str) -> None:
    """Jev's settings, with no call ever made: building a provider never calls it."""
    values = {"JEV_API_URL": "https://jev.test/v1/decide", "JEV_MODEL": "jev-test-model"}
    for name, value in {**values, **settings}.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    "settings",
    [
        {"JEV_API_KEY": "test-key"},
        {"JEV_CONNECTOR": "jev/mendari"},
        {"JEV_API_KEY": "test-key", "JEV_API_URL": " "},
        {"JEV_API_KEY": "test-key", "JEV_MODEL": ""},
        {},
    ],
)
def test_jev_is_set_up_with_an_endpoint_a_model_and_a_key(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str]
) -> None:
    set_up_jev(monkeypatch, **settings)
    complete = bool(settings) and all(value.strip() for value in settings.values())
    assert jev.deployment_configured() is complete
    assert (server_providers(None) != {}) is complete


def test_with_jev_set_up_a_server_runs_mendari_and_jev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up_jev(monkeypatch, JEV_API_KEY="test-key")
    providers = server_providers(None)
    assert providers == {
        f"{pack}:{slot}": variant
        for pack, slots in JEV_DEFAULTS.items()
        for slot, variant in slots.items()
    }
    health = TestClient(create_app(db_path=tmp_path / "a.db", providers=providers)).get(
        "/api/health"
    )
    served = health.json()["providers"]
    for pack, slots in JEV_DEFAULTS.items():
        assert {slot: served[pack][slot] for slot in slots} == slots, pack
    assert health.json()["provider_settings_skipped"] == {}
    # Every screen keeps the keyword rules; AI Guard's screen is an LLM's, not Jev's.
    assert health.json()["without_model"] == {"mendari/ai-guard": ["screen"]}


def test_settings_still_decide_every_slot_they_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up_jev(monkeypatch, JEV_API_KEY="test-key")
    monkeypatch.setenv(
        "MENDARI_PROVIDERS",
        "safety_screen=rules,notes=rules,mendari/referral-router:specialty_reader=naive_bayes",
    )
    app = create_app(db_path=tmp_path / "a.db", providers=server_providers(None))
    served = TestClient(app).get("/api/health").json()["providers"]
    assert served["mendari/patient-inbox-router"] == {"safety_screen": "rules", "router": "jev"}
    referral = served["mendari/referral-router"]
    assert (referral["specialty_reader"], referral["triage_screen"]) == (
        "naive_bayes",
        "rules_and_jev",
    )
    assert served["mendari/prior-auth-preflight"]["notes"] == "rules"
    assert served["mendari/clinical-trial-prescreener"]["notes"] == "rules"
    flags = ["router=naive_bayes"]  # `serve --provider`: replaces MENDARI_PROVIDERS, as before
    assert server_providers(flags)["mendari/patient-inbox-router:safety_screen"] == "rules_and_jev"
    assert "mendari/patient-inbox-router:router" not in server_providers(flags)


def test_mendari_serve_runs_the_jev_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from mendari.server import app as app_module

    set_up_jev(monkeypatch, JEV_API_KEY="test-key")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")  # `serve` sets it; restored after
    built: dict[str, object] = {}
    monkeypatch.setattr(app_module, "create_app", lambda **kwargs: built.update(kwargs))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    assert main(["serve", "--db", str(tmp_path / "a.db")]) == 0
    assert built["providers"] == server_providers(None) != {}


def test_commands_never_run_the_jev_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A benchmark, a pack's tests or a decision on the command line never calls Jev unasked."""
    set_up_jev(monkeypatch, JEV_API_KEY="test-key")

    def no_jev(*args: object, **kwargs: object) -> None:
        raise AssertionError("Jev was used")

    monkeypatch.setattr(JevProvider, "__init__", no_jev)
    assert main(["test", "mendari/patient-inbox-router"]) == 0
    assert main(["decide", "mendari/patient-inbox-router", "--text", "Can I pay my bill?"]) == 0
    assert '"variant": "rules"' in capsys.readouterr().out


def test_a_slot_runs_without_a_model_only_where_its_pack_offers_one() -> None:
    packs = {pack.id: pack for pack in discover().packs}
    inbox = packs["mendari/patient-inbox-router"]
    assert slots_without_model(inbox, {}) == ["safety_screen", "router"]
    # A composite with a model member reads with a model: the cascade asks Jev when unsure.
    assert slots_without_model(inbox, {"router": "cascade"}) == ["safety_screen"]
    assert slots_without_model(inbox, {"safety_screen": "rules_and_llm", "router": "llm"}) == []
    referral = packs["mendari/referral-router"]
    assert "checklist" not in slots_without_model(referral, {})  # computed in code: no model to add
