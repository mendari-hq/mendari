# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import Decision
from mendari import settings as mendari_settings
from mendari.cli import main
from mendari.core.registry import discover
from mendari.errors import MendariError
from mendari.evals.pack_tests import load_cases
from mendari.providers import jev, slots_without_model
from mendari.providers.jev import JevProvider
from mendari.server.app import create_app
from mendari.settings import default_providers, parse_providers, providers_for
from tests.conftest import INBOX_DIR, ROOT

FIXTURES = Path(__file__).parent / "fixtures" / "mini-agents"
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


def test_the_cli_refuses_settings_that_cannot_apply(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    trial = str(FIXTURES / "trial-mini")
    assert main(["test", trial, "--provider", "screen=llm"]) != 0
    assert "do not apply to mendari-test/trial-mini: ['screen=llm']" in capsys.readouterr().err
    # Typed, a setting that applies to nothing in the pack is a mistake (LP-15)...
    assert main(["test", trial, "--provider", "router=jev"]) == 1
    err = capsys.readouterr().err
    assert "provider setting router=jev: the agent has no slot 'router'" in err
    assert main(["test", trial, "--provider", "other/pack:screen=llm"]) == 1
    assert "names other/pack, not this agent" in capsys.readouterr().err
    # ...but MENDARI_PROVIDERS, a deployment's list for every pack, only warns.
    monkeypatch.setenv("MENDARI_PROVIDERS", "router=jev")
    assert main(["test", trial]) == 0
    assert "warning: MENDARI_PROVIDERS: provider setting router=jev" in capsys.readouterr().err


# -- with Jev set up, the packs JEV_DEFAULTS names run Mendari + Jev ------------------------------

# A release names no Jev defaults: its one bundled agent has no Jev slot. The mechanism is tested
# with the defaults the fixture packs had when they shipped, set in place of the product's.
JEV_DEFAULTS: dict[str, dict[str, str]] = {
    "mendari/patient-inbox-router": {"safety_screen": "rules_and_jev", "router": "cascade"},
    "mendari/referral-router": {"triage_screen": "rules_and_jev", "specialty_reader": "cascade"},
    "mendari/prior-auth-preflight": {"notes": "rules_and_jev"},
}
PRODUCT_JEV_DEFAULTS = mendari_settings.JEV_DEFAULTS  # read before any test sets the fixtures'
# The subprocess in test_the_vercel_app_runs_the_jev_defaults sets them the same way.
_SET_DEFAULTS = f"import mendari.settings as s; s.JEV_DEFAULTS.update({JEV_DEFAULTS!r}); "


@pytest.fixture(autouse=True)
def _fixture_jev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mendari_settings, "JEV_DEFAULTS", JEV_DEFAULTS)


def test_a_release_names_no_jev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundled demo agent has no Jev slot, so with Jev set up a release still runs no Jev
    default: a deployment chooses its own packs' variants (MENDARI_PROVIDERS)."""
    assert PRODUCT_JEV_DEFAULTS == {}
    monkeypatch.setattr(mendari_settings, "JEV_DEFAULTS", PRODUCT_JEV_DEFAULTS)
    set_up_jev(monkeypatch, JEV_API_KEY="test-key")
    assert default_providers() == {}


def set_up_jev(monkeypatch: pytest.MonkeyPatch, **settings: str) -> None:
    """Jev's settings, with no call ever made: building a provider never calls it."""
    values = {"JEV_API_URL": "https://jev.test/v1/decide", "JEV_MODEL": "jev-test-model"}
    for name, value in {**values, **settings}.items():
        monkeypatch.setenv(name, value)


def served(tmp_path: Path, providers: dict[str, str] | None = None) -> dict[str, object]:
    """/api/health of a server started as `mendari serve` and the Vercel app start one."""
    app = create_app(
        db_path=tmp_path / "a.db", providers=providers, default_providers=default_providers()
    )
    return dict(TestClient(app).get("/api/health").json())


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
    assert (default_providers() == JEV_DEFAULTS) is complete


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        ({"JEV_MODEL": "jev 1.13"}, "JEV_MODEL is not a Jev model id"),
        ({"JEV_API_URL": "http://jev.example.org/v1/decide"}, "JEV_API_URL must use https"),
    ],
)
def test_a_jev_setting_that_could_never_work_stops_the_server_at_startup(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str], problem: str
) -> None:
    """Rather than a server whose reference packs cannot be built, every decision refused."""
    set_up_jev(monkeypatch, JEV_API_KEY="test-key", **settings)
    with pytest.raises(MendariError, match=problem):
        default_providers()


def test_with_jev_set_up_a_server_runs_mendari_and_jev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    set_up_jev(monkeypatch, JEV_API_KEY="test-key")
    with caplog.at_level(logging.INFO, logger="mendari.providers"):
        health = served(tmp_path)
    # Said once at startup: which slots the defaults turn on, by id and variant only.
    (started,) = [r for r in caplog.records if r.name == "mendari.providers"]
    assert "mendari/referral-router specialty_reader=cascade" in started.getMessage()
    assert "mendari/clinical-trial-prescreener" not in started.getMessage()
    for pack, slots in JEV_DEFAULTS.items():
        assert {slot: health["providers"][pack][slot] for slot in slots} == slots, pack  # type: ignore[index]
    assert health["provider_settings_skipped"] == {}
    assert health["jev"]["defaults"] is True  # type: ignore[index]
    app = create_app(db_path=tmp_path / "b.db", default_providers=default_providers())
    inbox = TestClient(app).get("/api/packs/mendari/patient-inbox-router").json()
    assert inbox["without_model"] == []  # what runs, not the pack's defaults
    # At risk level 2, automation rests on an approval of what runs: the deployment names it.
    assert health["without_model"] == {
        "mendari/ai-guard": ["screen"],
        "mendari/clinical-trial-prescreener": ["notes"],
    }


def test_each_jev_default_is_what_its_pack_needs_to_automate_with_the_rules_beside_jev() -> None:
    """Every default is a variant of its pack; in a slot the policy names in automation_requires,
    one it allows; and a screen keeps a `rules` member beside Jev, so a possible emergency is
    still flagged when Jev cannot answer."""
    packs = {pack.id: pack for pack in discover().packs}
    assert JEV_DEFAULTS == {
        "mendari/patient-inbox-router": {"safety_screen": "rules_and_jev", "router": "cascade"},
        "mendari/referral-router": {
            "triage_screen": "rules_and_jev",
            "specialty_reader": "cascade",
        },
        "mendari/prior-auth-preflight": {"notes": "rules_and_jev"},
    }
    for pack_id, slots in JEV_DEFAULTS.items():
        manifest = packs[pack_id].manifest
        assert manifest.risk.level <= 1, pack_id  # risk 2 automates on an approval: not here
        assert set(manifest.policy.automation_requires) <= set(slots), pack_id
        for slot, variant in slots.items():
            spec = manifest.providers[slot].variants[variant]
            if slot in manifest.policy.automation_requires:
                assert variant in manifest.policy.automation_requires[slot], (pack_id, slot)
                members = (spec.model_extra or {})["members"].values()
                assert {"rules", "jev"} <= {member["type"] for member in members}, (pack_id, slot)


def test_when_jev_cannot_answer_the_defaults_still_suggest_and_automate_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Jev outage or an exhausted budget: every case of each pack's behaviour tests gets the
    suggestion the offline readers give, and none is automated."""
    set_up_jev(monkeypatch, JEV_API_KEY="test-key")
    down = httpx.MockTransport(lambda request: httpx.Response(503))
    original = JevProvider.__init__

    def unreachable(self: JevProvider, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **{**kwargs, "client": httpx.Client(transport=down)})

    monkeypatch.setattr(JevProvider, "__init__", unreachable)
    packs = {pack.id: pack for pack in discover().packs}
    for pack_id, slots in JEV_DEFAULTS.items():
        pack = packs[pack_id]
        defaults, offline = Decision(pack, providers=slots), Decision(pack)
        for case in load_cases(pack):
            jev_down, floor = defaults(case["input"]), offline(case["input"])
            assert jev_down.outcome.value != "AUTO", (pack_id, case.get("name"))
            if floor.recommendation is not None:
                assert jev_down.recommendation is not None, (pack_id, case.get("name"))
                assert jev_down.priority == floor.priority, (pack_id, case.get("name"))


def test_without_jev_nothing_changes(tmp_path: Path) -> None:
    health = served(tmp_path)
    assert health["jev"]["defaults"] is False  # type: ignore[index]
    assert health["providers"]["mendari/patient-inbox-router"] == {  # type: ignore[index]
        "safety_screen": "rules",
        "router": "naive_bayes",
    }


def test_settings_decide_every_slot_they_apply_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setting wins in every pack it applies to; one another pack's variant names (a community
    pack's, a typo) is skipped and leaves the Jev default in place."""
    set_up_jev(monkeypatch, JEV_API_KEY="test-key")
    monkeypatch.setenv(
        "MENDARI_PROVIDERS",
        "safety_screen=rules,notes=rules,router=keyword,"
        "mendari/referral-router:specialty_reader=naive_bayes",
    )
    health = served(tmp_path, parse_providers(None))
    providers = health["providers"]
    inbox = {"safety_screen": "rules", "router": "cascade"}  # `keyword` is no inbox router
    assert providers["mendari/patient-inbox-router"] == inbox  # type: ignore[index]
    referral = providers["mendari/referral-router"]  # type: ignore[index]
    assert (referral["specialty_reader"], referral["triage_screen"]) == (
        "naive_bayes",
        "rules_and_jev",
    )
    assert providers["mendari/prior-auth-preflight"]["notes"] == "rules"  # type: ignore[index]
    assert health["provider_settings_skipped"] == {
        "mendari/patient-inbox-router": ["router=keyword"]
    }


def test_mendari_serve_runs_the_jev_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from mendari.server import app as app_module

    set_up_jev(monkeypatch, JEV_API_KEY="test-key")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")  # `serve` sets it; restored after
    built: dict[str, object] = {}
    started = SimpleNamespace(state=SimpleNamespace(provider_settings_not_taken=[]))
    monkeypatch.setattr(app_module, "create_app", lambda **kwargs: built.update(kwargs) or started)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    flags = ["--provider", "router=naive_bayes"]
    assert main(["serve", "--db", str(tmp_path / "a.db"), *flags]) == 0
    assert built["providers"] == {"router": "naive_bayes"}
    assert built["default_providers"] == JEV_DEFAULTS


def test_the_vercel_app_runs_the_jev_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entrypoint as Vercel imports it, in a process of its own (its app is built at import)."""
    set_up_jev(monkeypatch, JEV_API_KEY="test-key")
    for name in ("VERCEL", "DATABASE_URL", "MENDARI_PROVIDERS"):
        monkeypatch.delenv(name, raising=False)
    # A process of its own does not see the tests' fixture packs unless told where they are.
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(ROOT / "tests" / "fixtures" / "reference-agents"))
    script = (
        _SET_DEFAULTS + "import json; from fastapi.testclient import TestClient; "
        "from mendari.server.vercel import app; "
        "print(json.dumps(TestClient(app).get('/api/health').json()['providers']))"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    providers = json.loads(done.stdout.strip().splitlines()[-1])
    for pack, slots in JEV_DEFAULTS.items():
        assert {slot: providers[pack][slot] for slot in slots} == slots, pack


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


@pytest.mark.parametrize("item", [":reader=rules", "mendari/portal-message-agent:=rules"])
def test_a_scoped_override_with_no_pack_or_no_slot_is_refused(item: str) -> None:
    """It applied nowhere and was never reported as skipped: a typo left the default running."""
    with pytest.raises(MendariError, match="namespace/name:slot=variant"):
        parse_providers([item])


def test_an_empty_daily_limit_is_unset_not_a_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other setting reads an empty value as unset; these two refused to start."""
    monkeypatch.setenv("MENDARI_JEV_DAILY_LIMIT", "")
    monkeypatch.setenv("MENDARI_LLM_DAILY_LIMIT", "  ")
    assert mendari_settings.daily_limits() == {
        "jev": mendari_settings.DEFAULT_JEV_DAILY_LIMIT,
        "llm": mendari_settings.DEFAULT_LLM_DAILY_LIMIT,
    }
