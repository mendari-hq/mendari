# SPDX-License-Identifier: Apache-2.0
"""Provider configs are checked at load (LP-16e): every variant and composite member against its
provider's closed config and its pack files (`providers.config_problems`). A pack the deployment
does not own is refused for any problem and must name the fields its model variants send; an
owned one loads with the problems as warnings, and `mendari validate` refuses both. For a pack
the deployment does not own, the deployment chooses the model and caps the answer length, the
timeout and the effort (`core/origin.py` `deployment_config`). Generated packs hold load and
validate together, and reach only the deployment's endpoints, with its credentials. The packs
are the synthetic visit sorter (`tests/support/engine.py`), pulled from a registry or owned."""

from __future__ import annotations

import copy
import json
import shutil
import socket
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from mendari import Decision, load_pack
from mendari.cli import main
from mendari.core.origin import NOT_OWNED_CAPS, deployment_config
from mendari.core.pack import Pack
from mendari.core.registry import SearchPath
from mendari.core.spec import DEPLOYMENT_ENV_KEYS
from mendari.errors import PackValidationError
from mendari.evals.pack_tests import validate_pack
from mendari.providers import (
    MODEL_TYPES,
    PROVIDER_CONFIG_MODELS,
    PROVIDER_TYPES,
    build_provider,
    config_json_schema,
    config_problems,
)
from mendari.providers.base import Provider, ProviderInfo
from mendari.providers.llm import CALIBRATION_FORMAT
from mendari.server.app import create_app
from tests.conftest import DEMO_AGENT_DIR, FIXTURE_AGENTS, ROOT, fixture_agents
from tests.support.engine import SORTER_MANIFEST, sorter

PACKS = FIXTURE_AGENTS
SCHEMA_FILE = ROOT / "docs" / "spec" / "provider-configs.schema.json"
PACK_ID = "someone/config-test"
BILL = {"message": "I have a question about my last bill."}
SENT = ["message"]
TOPICS = ("billing", "scheduling", "other")

RULES = {"type": "rules", "config": "logic/rules.yaml"}
AGENT = {"type": "agent", "state_fields": SENT, "mock_file": "models/mock_agent.json"}
# A valid config of every provider type, in the `reader` slot (one question, `topic`).
VALID: dict[str, dict[str, Any]] = {
    "rules": RULES,
    "llm": {"type": "llm", "state_fields": SENT, "calibration": "models/calibration.json"},
    "agent": AGENT,
    "check": {"type": "check", "members": {"a": AGENT, "b": RULES}},
}


def manifest(reader: dict[str, Any] | None = None, *, pack_id: str = PACK_ID) -> dict[str, Any]:
    """The visit sorter: its `rules` default, and the variants given."""
    data = copy.deepcopy(SORTER_MANIFEST)
    data["id"] = pack_id
    data["providers"] = {
        "reader": {
            "answers": ["topic"],
            "use": "rules",
            "variants": {"rules": RULES, **(reader or {})},
        },
    }
    return data


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The files every variant above reads, once: tests copy them and write their manifest."""
    root = sorter(tmp_path_factory.mktemp("template"), pack_id="someone/pack")
    (root / "agent.yaml").unlink()
    (root / "models").mkdir()
    calibration = {
        "format": CALIBRATION_FORMAT,
        "model": "example/m",
        "temperatures": {"topic": 1.5},
    }
    (root / "models" / "calibration.json").write_text(json.dumps(calibration))
    mock_agent = {
        "model": "mock-agent",
        "agent_version": "0.1.0",
        "cases": [
            {
                "match": {"contains_any": ["bill"]},
                "response": {"answers": {"topic": {"value": "billing", "confidence": 0.9}}},
            }
        ],
        "default": {"answers": {"topic": {"value": "other", "confidence": 0.7}}},
    }
    (root / "models" / "mock_agent.json").write_text(json.dumps(mock_agent))
    return root


def write(template: Path, root: Path, data: dict[str, Any]) -> Path:
    shutil.copytree(template, root)
    (root / "agent.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return root


@pytest.fixture
def make(template: Path, tmp_path: Path) -> Callable[..., Path]:
    """A pack of the template with `manifest(...)`'s variants, in its own directory."""
    made = iter(range(1_000))

    def make(*args: Any, **kwargs: Any) -> Path:
        return write(template, tmp_path / f"pack{next(made)}", manifest(*args, **kwargs))

    return make


@pytest.fixture(autouse=True)
def _no_model_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model settings a provider reads at build are the deployment's, never the shell's."""
    for name in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def refused(root: Path, origin: str = "hub") -> list[str]:
    with pytest.raises(PackValidationError) as error:
        load_pack(root, origin=origin)  # type: ignore[arg-type]
    return error.value.problems


def validates(pack: Pack) -> bool:
    try:
        validate_pack(pack)
    except PackValidationError:
        return False
    return True


# -- the closed configs ---------------------------------------------------------------------------


def test_every_provider_type_has_a_closed_config_and_checks_it() -> None:
    """Each type's closed model forbids other keys, and each provider checks its own configs
    (`Provider.config_problems` checks nothing)."""
    assert set(PROVIDER_CONFIG_MODELS) == set(PROVIDER_TYPES) == set(VALID)
    for kind, model in PROVIDER_CONFIG_MODELS.items():
        assert model.model_config.get("extra") == "forbid", kind
        own = [c for c in PROVIDER_TYPES[kind].__mro__ if "config_problems" in vars(c)]
        assert own[0] is not Provider, kind


def test_the_template_pack_has_no_problem_wherever_it_comes_from(make: Callable[..., Path]) -> None:
    root = make({f"v_{kind}": config for kind, config in VALID.items()})
    for origin in ("local", "hub"):
        pack = load_pack(root, origin=origin)
        assert pack.bounded is (origin == "hub")
        assert pack.warnings == () and config_problems(pack) == []
        validate_pack(pack)


@pytest.mark.parametrize("kind", sorted(VALID))
def test_every_variant_is_checked_at_load_against_its_closed_config(
    make: Callable[..., Path], kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A key no provider reads: a pack from a registry is refused naming the variant; an owned
    one loads, warned, and `mendari validate` refuses it."""
    root = make({"broken": {**VALID[kind], "x": 1}})
    where = "providers.reader.variants.broken"
    problems = refused(root)
    assert len(problems) == 1 and problems[0].startswith(f"{where}: "), problems
    assert "x\n  Extra inputs are not permitted" in problems[0]
    owned = load_pack(root)
    assert owned.warnings == tuple(problems)
    assert main(["validate", str(root)]) == 1
    assert where in capsys.readouterr().err


def test_a_member_is_checked_as_its_own_type(make: Callable[..., Path]) -> None:
    member = {**VALID["llm"], "x": 1}
    root = make({"screen": {**VALID["check"], "members": {"a": RULES, "b": member}}})
    (problem,) = refused(root)
    assert problem.startswith("providers.reader.variants.screen.members.b: 1 validation error")


def test_a_broken_non_default_variant_is_refused_for_a_registry_pack_and_warned_for_an_owned_one(
    make: Callable[..., Path], template: Path, tmp_path: Path
) -> None:
    """A variant that cannot run, though the pack's default never uses it: a label its rules
    answer that the question does not have. The owned pack loads and decides with its default
    as before; the variant still fails when chosen, as it always did."""
    rules = yaml.safe_load((template / "logic" / "rules.yaml").read_text())
    rules["questions"]["topic"]["default"] = "maybe"
    root = make({"other": {"type": "rules", "config": "logic/other.yaml"}})
    (root / "logic" / "other.yaml").write_text(yaml.safe_dump(rules))
    wanted = [
        "providers.reader.variants.other: logic/other.yaml: unknown labels ['maybe'] for 'topic'"
    ]
    assert refused(root) == wanted
    owned = load_pack(root)
    assert list(owned.warnings) == wanted
    assert Decision(owned).decide(BILL).recommendation == "billing"
    with pytest.raises(PackValidationError, match=r"unknown labels \['maybe'\]"):
        build_provider(owned, "reader", "other")
    assert not validates(owned)


def test_an_owned_packs_unread_key_warns_and_it_still_serves(
    make: Callable[..., Path],
    template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Conflict 17: a key nothing reads, on the variant an owned pack decides with, warns and
    keeps it serving; nothing changes in what it decides. The warning is said where an operator
    looks: `mendari list` (on stderr), the server's start-up log and /api/health
    (`pack_warnings`, beside `invalid`, which it is not in)."""
    served = tmp_path / "served"
    root = write(template, served / "pack", manifest({"rules": {**RULES, "x": 1}}))
    owned = load_pack(root)
    assert owned.warnings and owned.warnings[0].startswith("providers.reader.variants.rules: ")
    clean = load_pack(make())
    assert (
        Decision(owned).decide(BILL).to_dict()["answers"]
        == Decision(clean).decide(BILL).to_dict()["answers"]
    )

    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(served))
    assert main(["list"]) == 0
    listed = capsys.readouterr()
    assert PACK_ID in listed.out and "warnings" not in listed.out
    assert f"warnings for {PACK_ID}@{owned.version} at {root.resolve()}:" in listed.err
    assert f"  - {owned.warnings[0]}" in listed.err

    with caplog.at_level("WARNING", logger="mendari.providers"):
        app = create_app(db_path=tmp_path / "w.db", packs_paths=[SearchPath(served, "local")])
    assert any(
        f"{PACK_ID}@{owned.version} loads with problems" in record.getMessage()
        and owned.warnings[0] in record.getMessage()
        for record in caplog.records
    )
    client = TestClient(app)
    health = client.get("/api/health").json()
    assert health["pack_warnings"] == {PACK_ID: list(owned.warnings)}
    assert PACK_ID not in str(health["invalid"])
    decided = client.post(f"/api/agents/{PACK_ID}/decide", json=BILL)
    assert decided.status_code == 200, decided.text
    assert decided.json()["recommendation"] == "billing"
    clean_app = create_app(db_path=tmp_path / "c.db", packs_paths=[SearchPath(PACKS, "maintained")])
    assert TestClient(clean_app).get("/api/health").json()["pack_warnings"] == {}


def test_trained_files_are_read_only_when_trained(make: Callable[..., Path]) -> None:
    """`load_pack(missing_models_ok=True)` does not read a `calibration`; a broken one is a
    problem otherwise. A calibration file is checked whichever model it was fitted on: which
    model runs is the deployment's."""
    other = {**VALID["llm"], "calibration": "models/mock_agent.json"}
    root = make({"llm": VALID["llm"], "other": other})
    (root / "models" / "calibration.json").write_text(
        json.dumps({"format": CALIBRATION_FORMAT, "model": "elsewhere/m", "temperatures": [1]})
    )
    assert load_pack(root, origin="hub", missing_models_ok=True).warnings == ()
    assert refused(root) == [
        "providers.reader.variants.llm: the calibration file's temperatures are not a mapping",
        "providers.reader.variants.other: not a calibration file",
    ]


# -- packs the deployment does not own -------------------------------------------------------


def test_a_registry_packs_remote_variant_needs_state_fields(make: Callable[..., Path]) -> None:
    llm, agent = {"type": "llm"}, {"type": "agent"}
    root = make(
        {
            "llm": llm,
            "agent": agent,
            "screen": {**VALID["check"], "members": {"llm": llm, "agent": agent}},
            "nothing": {"type": "llm", "state_fields": []},
        }
    )
    rule = (
        "a registry or store agent's {} variant must name its state_fields: the input "
        "fields it sends"
    )
    assert refused(root) == [
        f"providers.reader.variants.llm: {rule.format('llm')}",
        f"providers.reader.variants.agent: {rule.format('agent')}",
        f"providers.reader.variants.screen.members.llm: {rule.format('llm')}",
        f"providers.reader.variants.screen.members.agent: {rule.format('agent')}",
        f"providers.reader.variants.nothing: {rule.format('llm')}",
    ]
    owned = load_pack(root)  # an owned pack may send the whole input, as before
    assert owned.warnings == ()
    assert {"llm", "agent"} <= MODEL_TYPES


def test_a_registry_packs_pinned_model_gives_way_to_the_deployments(
    make: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = {"llm": {"type": "llm", "model": "example/pinned-model", "state_fields": SENT}}
    root = make(pinned)
    hub, owned = load_pack(root, origin="hub"), load_pack(root)
    monkeypatch.setenv("LLM_MODEL", "example/deployment-model")
    assert build_provider(hub, "reader", "llm").info.model == "example/deployment-model"
    assert build_provider(owned, "reader", "llm").info.model == "example/pinned-model"
    # No LLM_MODEL: there is no built-in model (LP-10c-ii), and a pack from a registry never
    # falls back to its own pinned one, so it has no model and nothing is called.
    monkeypatch.delenv("LLM_MODEL")
    assert build_provider(hub, "reader", "llm").info.model is None
    assert build_provider(owned, "reader", "llm").info.model == "example/pinned-model"
    decided = Decision(hub, providers={"reader": "llm"}).decide(BILL)
    assert decided.errors == {"topic": "not_configured"}
    assert decided.outcome.value != "AUTO"


def test_a_registry_packs_answer_length_timeout_and_effort_are_capped(
    make: Callable[..., Path],
) -> None:
    big = {
        "type": "llm",
        "max_tokens": 30000,
        "timeout_ms": 120_000,
        "reasoning_effort": "high",
        "state_fields": SENT,
    }
    small = {"type": "llm", "max_tokens": 1000, "reasoning_effort": "low", "state_fields": SENT}
    slow = {**AGENT, "timeout_seconds": 60.0}
    root = make({"big": big, "small": small, "slow": slow})
    hub, owned = load_pack(root, origin="hub"), load_pack(root)

    def settings_of(pack: Pack, variant: str) -> Any:
        provider: Any = build_provider(pack, "reader", variant)
        return provider.settings

    capped = settings_of(hub, "big")
    assert (capped.max_tokens, capped.timeout_ms, capped.reasoning_effort) == (4096, 30_000, None)
    kept = settings_of(hub, "small")
    assert (kept.max_tokens, kept.timeout_ms, kept.reasoning_effort) == (1000, 30_000, "low")
    assert settings_of(hub, "slow").timeout_seconds == 15.0
    own = settings_of(owned, "big")  # an owned pack's stand
    assert (own.max_tokens, own.timeout_ms, own.reasoning_effort) == (30000, 120_000, "high")
    assert settings_of(owned, "slow").timeout_seconds == 60.0
    assert NOT_OWNED_CAPS["llm"] == {"max_tokens": 4096, "timeout_ms": 30_000}
    assert NOT_OWNED_CAPS["agent"] == {"timeout_seconds": 15.0}
    # An absent key stays absent, and the members are held to the same.
    composite = {"type": "check", "members": {"a": {**big, "model": "x/y"}, "b": {"type": "agent"}}}
    assert deployment_config(composite) == {
        "type": "check",
        "members": {
            "a": {"type": "llm", "max_tokens": 4096, "timeout_ms": 30_000, "state_fields": SENT},
            "b": {"type": "agent"},
        },
    }
    for effort, kept_effort in (("none", True), ("low", True), ("medium", False), (None, False)):
        held = deployment_config({"type": "llm", "reasoning_effort": effort})
        assert ("reasoning_effort" in held) is kept_effort, effort
    # Numbers written as text ("120000") or whole floats (120000.0): capped all the same.
    for written in (str, float):
        capped_however_written(make, written)


def capped_however_written(make: Callable[..., Path], written: Callable[[int], Any]) -> None:
    """The config models read `"32000"` and `32000.0` as 32000 (pydantic's lax mode), so a pack
    from a registry that writes its numbers so loads, and is held to the caps all the same, as
    a variant and as a composite's member; the cap compares the number the model reads, never
    the text. An owned pack's number written so builds as before."""
    llm = {
        "type": "llm",
        "max_tokens": written(32000),
        "timeout_ms": written(120_000),
        "state_fields": SENT,
    }
    agent = {**AGENT, "timeout_seconds": written(60)}
    composite = {"type": "check", "members": {"llm": llm, "agent": agent}}
    root = make({"llm": llm, "agent": agent, "both": composite})
    hub, owned = load_pack(root, origin="hub"), load_pack(root)
    assert hub.bounded and hub.warnings == () and owned.warnings == ()

    def built(pack: Pack, variant: str) -> dict[str, Any]:
        """Each model leaf's capped settings: an LLM's answer length and timeout, an agent's
        timeout."""
        provider: Any = build_provider(pack, "reader", variant)
        providers = provider.members if variant == "both" else {variant: provider}
        return {
            name: (
                (p.settings.max_tokens, p.settings.timeout_ms)
                if name == "llm"
                else p.settings.timeout_seconds
            )
            for name, p in providers.items()
        }

    capped = {"llm": (4096, 30_000), "agent": 15.0}
    assert built(hub, "llm") == {"llm": capped["llm"]}
    assert built(hub, "agent") == {"agent": capped["agent"]}
    assert built(hub, "both") == capped
    assert built(owned, "llm") == {"llm": (32000, 120_000)}
    assert built(owned, "agent") == {"agent": 60.0}
    assert built(owned, "both") == {"llm": (32000, 120_000), "agent": 60.0}
    # A lower number written so is the model's number; what the model refuses is left as it is
    # (`load_pack` refused a pack with it).
    assert deployment_config({"type": "llm", "max_tokens": written(1000)})["max_tokens"] == 1000
    for refused_value in (True, "x", 1000.5, [1000]):
        held = deployment_config({"type": "llm", "max_tokens": refused_value})
        assert held["max_tokens"] == refused_value


def test_a_registry_packs_invalid_values_are_reported_before_the_deployment_chooses(
    make: Callable[..., Path],
) -> None:
    """Its own values are checked before the deployment's replace them: a `max_tokens` the
    provider never accepts is refused, not capped into one it does; so is a malformed model."""
    root = make(
        {
            "long": {"type": "llm", "max_tokens": 99_999_999, "state_fields": SENT},
            "named": {"type": "llm", "model": "not a model!", "state_fields": SENT},
            "effort": {"type": "llm", "reasoning_effort": "extreme", "state_fields": SENT},
        }
    )
    problems = refused(root)
    assert [p.partition(":")[0] for p in problems] == [
        "providers.reader.variants.long",
        "providers.reader.variants.named",
        "providers.reader.variants.effort",
    ]
    assert "max_tokens" in problems[0] and "less than or equal to 32000" in problems[0]
    assert "model" in problems[1] and "reasoning_effort" in problems[2]


def test_a_pulled_reference_copy_keeps_its_config(tmp_path: Path) -> None:
    """A pulled copy of a reference pack (its digest one of this release's) is owned: every
    variant runs with the config the bundled pack runs with, its pinned values included."""
    for source in sorted(PACKS.iterdir()):
        if not (source / "agent.yaml").is_file():
            continue
        root = tmp_path / source.name
        shutil.copytree(source, root)
        maintained, pulled = load_pack(source, origin="maintained"), load_pack(root, origin="hub")
        assert not pulled.bounded and pulled.warnings == ()
        for slot, spec in maintained.manifest.providers.items():
            for variant in spec.variants:
                ours = build_provider(maintained, slot, variant)
                theirs = build_provider(pulled, slot, variant)
                assert (theirs.config, theirs.info) == (ours.config, ours.info), (slot, variant)


# -- every shipped pack ----------------------------------------------------------------------------


def shipped(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """The bundled demo agent and every agent the tests bundle, the scaffold as `mendari new`
    writes it, and every test fixture's agent, with the origin each is served from."""
    yield DEMO_AGENT_DIR, "local"
    for source in sorted(PACKS.iterdir()):
        if (source / "agent.yaml").is_file():
            yield source, "maintained"
    plain = tmp_path / "plain"
    assert main(["new", str(plain), "--id", "someone/plain", "--publisher", "Test"]) == 0
    yield plain, "local"
    for root in fixture_agents():
        yield root, "local"


def test_every_shipped_pack_template_and_fixture_has_no_config_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None warns where it is served, and none would be refused as a pack the deployment does
    not own: every model variant names what it sends."""
    found = list(shipped(tmp_path))
    assert len(found) >= len(fixture_agents()) + 2
    for root, origin in found:
        pack = load_pack(root, origin=origin)  # type: ignore[arg-type]
        assert pack.warnings == (), root
    monkeypatch.setattr(Pack, "bounded", property(lambda self: True))
    for root, origin in found:
        assert config_problems(load_pack(root, origin=origin)) == [], root  # type: ignore[arg-type]


# -- the published JSON Schema -----------------------------------------------------------------


def test_the_provider_config_schema_is_generated() -> None:
    """docs/spec/provider-configs.schema.json is what `scripts/provider_config_schema.py`
    writes, and it accepts every variant and member a shipped pack has, and nothing more."""
    generated = json.dumps(config_json_schema(), indent=2, sort_keys=True) + "\n"
    assert SCHEMA_FILE.read_text() == generated, "run scripts/provider_config_schema.py"
    schema = json.loads(generated)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    configs = [
        {"type": variant.type, **(variant.model_extra or {})}
        for source in [DEMO_AGENT_DIR, *sorted(PACKS.iterdir())]
        if (source / "agent.yaml").is_file()
        for slot in load_pack(source).manifest.providers.values()
        for variant in slot.variants.values()
    ]
    assert configs and all(validator.is_valid(config) for config in configs)
    assert all(validator.is_valid(config) for config in VALID.values())
    for config in VALID.values():
        assert not validator.is_valid({**config, "x": 1}), config
    nested = {**VALID["check"], "members": {"a": RULES, "b": VALID["check"]}}
    assert not validator.is_valid(nested)


# -- generated packs (hypothesis, LP-16) ---------------------------------------------------------

# Each generated variant is a valid one of its type with one change at most: a key set to one of
# the values below, or one of its own keys removed, so that each kind of problem shows up on its
# own, in a pack that is otherwise sound. Values are drawn from short lists, never built, so
# every one of them is tried.
FILES = (
    "logic/rules.yaml",
    "models/calibration.json",
    "models/mock_agent.json",
    "models/missing.json",
)
URLS = ("https://evil.test/v1", "https://llm.test.evil.test/v1", "http://127.0.0.1:9/x")
MODELS = ("https://evil.test/v1", "example/m", "evil.test:443", "m:latest", "bad model", "")
ENV_NAMES = ("PACK_SAFETY_URL", "PACK_SAFETY_KEY", "LLM_API_KEY", "MENDARI_AGENT_TOKEN")
ENV_KEYS = sorted({key for keys in DEPLOYMENT_ENV_KEYS.values() for key in keys})
ODD: tuple[Any, ...] = (None, True, -1, 0.5, "x", [], {}, ["message"], {"topic": 1}, "64", 64.0)
TYPICAL: dict[str, tuple[Any, ...]] = {
    **dict.fromkeys(("config", "calibration", "mock_file"), FILES),
    "model": MODELS,
    "agent_id": ("my-agent", "org/agent:v1", "bad agent!", ""),
    "system_prompt": ("Classify the input.", ""),
    "instructions": ("Be concise.", ""),
    "timeout_seconds": (0, 0.5, 15.0, 30.5, 120.0, 500.0, "30.5", True),
    **dict.fromkeys((*ENV_KEYS, "url", "endpoint", "base_url", "api_key"), URLS + ENV_NAMES),
    "state_fields": (["message"], [], ["x"], ["message", "x"], "message"),
    **dict.fromkeys(
        ("timeout_ms", "max_tokens"),
        (
            *(0, 64, 999, 1000, 4096, 5000, 10_000, 30_000, 120_000, 10**8),
            # As text, or as whole floats: the config models read them as numbers (lax mode).
            *("120000", 120000.0, "32000", 32000.0, "10000", 10000.0, "1000"),
        ),
    ),
    "temperature": (0, 0.5, 1, 2, 3),
    "reasoning_effort": ("none", "low", "medium", "high", "extreme"),
    "batch": (True, False),
    "repeat": (1, 2, 3, 5, 6),
}


def pairs(keys: list[str]) -> list[tuple[str, Any]]:
    """Every (key, value) a generated variant may set: each key's typical values and some odd
    ones."""
    return [(key, value) for key in keys for value in (*TYPICAL.get(key, ()), *ODD)]


def changed(base: dict[str, Any], keys: list[str]) -> st.SearchStrategy[dict[str, Any]]:
    """`base` as it is, with one key of `keys` set, or with one of its own keys (not its type)
    removed."""
    own = sorted(set(base) - {"type"})
    return st.one_of(
        st.just(base),
        st.sampled_from(pairs(keys)).map(lambda pair: {**base, pair[0]: pair[1]}),
        st.sampled_from(own).map(lambda key: {k: v for k, v in base.items() if k != key}),
    )


def config_of(
    leaves: list[str], composites: list[str], keys: list[str]
) -> st.SearchStrategy[dict[str, Any]]:
    """A variant: one of `leaves` (changed), or a composite (changed) whose members are its
    own or generated ones of `leaves`, a nested composite among them."""
    leaf = st.sampled_from(leaves).flatmap(lambda kind: changed(VALID[kind], keys))
    members = st.dictionaries(
        st.sampled_from(["a", "b", "c"]),
        st.one_of(leaf, st.just(VALID["check"])),
        min_size=1,
        max_size=3,
    )

    def composite(kind: str) -> st.SearchStrategy[dict[str, Any]]:
        return st.one_of(
            changed(VALID[kind], keys),
            members.map(lambda chosen: {**VALID[kind], "members": chosen}),
        )

    return st.one_of(leaf, st.sampled_from(composites).flatmap(composite))


COMPOSITES = ["check"]
LEAVES = sorted(set(VALID) - set(COMPOSITES))
# Every key a provider config may have, the variables a pack may not name, and a few others.
FIELD_NAMES = sorted(
    {name for model in PROVIDER_CONFIG_MODELS.values() for name in model.model_fields}
    | set(ENV_KEYS)
    | {"x", "url", "endpoint"}
)
KEYS = [key for key in FIELD_NAMES if key not in ("type", "members")]
ANY_CONFIG = config_of(LEAVES, COMPOSITES, KEYS)
GENERATED = settings(
    derandomize=True,
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def builds(pack: Pack, config: dict[str, Any]) -> Provider | None:
    """`config` built as `build_provider` builds it in `pack` (with the deployment's keys for a
    pack it does not own), or None: any exception is a config that does not build."""
    kind = config["type"]
    questions = {q: pack.manifest.questions[q] for q in pack.manifest.providers["reader"].answers}
    info = ProviderInfo(slot="reader", variant="g", type=kind)
    try:
        return PROVIDER_TYPES[kind](
            pack, info, questions, deployment_config(config) if pack.bounded else config
        )
    except Exception:
        return None


def held_to_the_deployments(provider: Provider) -> bool:
    """Whether every model leaf of a provider built for a pack the deployment does not own runs
    on the deployment's model and endpoint, within the caps (`NOT_OWNED_CAPS`) and an effort of
    `low` or less."""
    for leaf in provider.leaves():
        settings: Any = getattr(leaf, "settings", None)
        if leaf.type not in MODEL_TYPES:
            continue
        if getattr(settings, "model", None) is not None:
            return False
        if getattr(settings, "endpoint", None) is not None:
            return False
        if any(getattr(settings, key) > cap for key, cap in NOT_OWNED_CAPS[leaf.type].items()):
            return False
        if getattr(settings, "reasoning_effort", None) not in (None, "none", "low"):
            return False
    return True


def single_changes(kind: str) -> list[dict[str, Any]]:
    """`VALID[kind]`, and every config one change away from it (`changed`)."""
    base = VALID[kind]
    return [
        base,
        *({**base, key: value} for key, value in pairs(KEYS)),
        *({k: v for k, v in base.items() if k != key} for key in sorted(set(base) - {"type"})),
    ]


def test_every_config_a_provider_refuses_is_a_problem_at_load(
    template: Path, tmp_path: Path
) -> None:
    """What parity rests on, over every config one change away from a valid one, of every type
    and as a composite's member: one that cannot be built (the environment cleared) is one
    `Provider.config_problems` finds a problem with, in a pack the deployment owns or not, and
    the check itself never raises. One that builds for a pack the deployment does not own runs
    on its model, within its caps, however the pack wrote its numbers."""
    root = write(template, tmp_path / "pack", manifest())
    packs = (load_pack(root), load_pack(root, origin="hub"))
    assert [pack.bounded for pack in packs] == [False, True]
    configs = [config for kind in sorted(VALID) for config in single_changes(kind)]
    configs += [
        {**VALID["check"], "members": {"a": RULES, "b": member}}
        for kind in LEAVES
        for member in single_changes(kind)
    ]
    seen = {"built": 0, "refused": 0, "warned only": 0}
    for pack in packs:
        for config in configs:
            questions = {
                q: pack.manifest.questions[q] for q in pack.manifest.providers["reader"].answers
            }
            problems = PROVIDER_TYPES[config["type"]].config_problems(
                pack, "w", config, questions, bounded=pack.bounded, trained=True
            )
            provider = builds(pack, config)
            assert provider is not None or problems, (pack.origin, config)
            if provider is not None and pack.bounded:
                assert held_to_the_deployments(provider), config
            seen["refused" if provider is None else "warned only" if problems else "built"] += 1
    assert len(configs) > 2_000 and min(seen.values()) >= 100, seen


def place(configs: list[dict[str, Any]]) -> dict[str, Any]:
    """The generated configs as variants `g0`, `g1`, ... of the reader slot."""
    return {f"g{number}": config for number, config in enumerate(configs)}


def test_load_and_validate_agree_on_provider_configs(
    template: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The LP-16 parity test: a pack `mendari validate` refuses is never one `load_pack` lists as
    valid. With any generated variants and members, a pack the deployment does not own is
    refused at load exactly when validate refuses it, and an owned one is warned exactly when
    validate refuses it (it still loads: conflict 17). What an owned pack cannot load with
    (a file a variant names that is missing, say), no pack loads with."""
    outcomes: dict[str, int] = {"refused": 0, "loaded": 0, "warned": 0, "unloadable": 0}

    @GENERATED
    @given(st.lists(ANY_CONFIG, min_size=1, max_size=3))
    @example([{**VALID["llm"], "calibration": "models/mock_agent.json"}])  # not a calibration file
    @example([{**AGENT, "mock_file": "models/calibration.json"}])  # not canned answers
    @example([{**RULES, "config": "models/mock_agent.json"}])  # not keyword rules
    @example([{**VALID["check"], "members": {"a": RULES, "b": {"type": "llm"}}}])
    @example([{**VALID["llm"], "max_tokens": 10**8}, {**AGENT, "timeout_seconds": 500.0}])
    @example([{"type": "gpt"}, {**VALID["check"], "members": {"a": RULES}}])
    @example([{**VALID["llm"], "calibration": "models/missing.json"}])  # a file it does not have
    @example([{**AGENT, "api_key_env": "LLM_API_KEY"}])  # a variable of the deployment's
    def check(configs: list[dict[str, Any]]) -> None:
        root = write(template, tmp_path_factory.mktemp("parity") / "pack", manifest(place(configs)))
        try:
            try:
                owned = load_pack(root)
            except PackValidationError:
                outcomes["unloadable"] += 1
                with pytest.raises(PackValidationError):
                    load_pack(root, origin="hub")
                return
            try:
                load_pack(root, origin="hub")
                hub_refused = False
            except PackValidationError:
                hub_refused = True
            outcomes["refused" if hub_refused else "loaded"] += 1
            outcomes["warned"] += bool(owned.warnings)
            assert validates(owned) is (owned.warnings == ())
            not_owned = replace(owned, origin="hub", warnings=())
            assert not_owned.bounded
            assert validates(not_owned) is not hub_refused
        finally:
            shutil.rmtree(root.parent)

    check()
    assert all(count >= 5 for count in outcomes.values()), outcomes


# The deployment's endpoints and keys in the egress test: its LLM and its agent service.
LLM_ORIGIN, AGENT_ORIGIN = "https://llm.test", "https://agent.test"
ENDPOINTS = {LLM_ORIGIN: "Bearer k-llm", AGENT_ORIGIN: "Bearer k-agent"}
# What a pack could try to choose where its model calls go, and with which key.
REMOTE_KEYS = sorted(
    {"model", "state_fields", "max_tokens", "timeout_ms", "reasoning_effort", "batch"}
    | {"agent_id", "timeout_seconds", "mock_file"}
    | set(ENV_KEYS)
    | {"url", "endpoint", "base_url", "api_key"}
)
REMOTE_CONFIG = config_of(["llm", "agent"], COMPOSITES, REMOTE_KEYS)


def answered(request: httpx.Request) -> httpx.Response:
    """A valid answer to an LLM or agent request: every label equally likely."""
    body = json.loads(request.content)
    if "messages" in body:
        asked = body["response_format"]["json_schema"]["schema"]["properties"]
        content = {
            key: {label: 1 / len(spec["properties"]) for label in spec["properties"]}
            for key, spec in asked.items()
        }
        return httpx.Response(
            200,
            json={
                "model": "example/answered",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    answers = {
        question["key"]: {"value": question["choices"][0], "confidence": 0.5}
        for question in body["questions"]
    }
    return httpx.Response(200, json={"model": "agent-answered", "answers": answers})


def test_a_generated_pack_only_reaches_the_deployments_endpoints_with_its_credentials(
    template: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LP-16's hypothesis test: whatever a pack's model variants and members say (variables to
    read the endpoint or key from, URLs, models that look like URLs), from a registry or owned,
    every request a decision makes goes to the deployment's LLM or agent service with the key
    the deployment set for that endpoint, or, for an agent the deployment owns that names its
    own `endpoint`, to that endpoint with no key at all. Requests are answered by the transport
    every httpx client uses, and no socket opens."""
    monkeypatch.setenv("LLM_BASE_URL", f"{LLM_ORIGIN}/v1")
    monkeypatch.setenv("LLM_API_KEY", "k-llm")
    monkeypatch.setenv("MENDARI_AGENT_URL", f"{AGENT_ORIGIN}/v1/decide")
    monkeypatch.setenv("MENDARI_AGENT_TOKEN", "k-agent")
    sent: list[tuple[httpx.Request, str]] = []
    asking: list[str] = []  # the origin of the pack deciding now

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        sent.append((request, asking[-1]))
        return answered(request)

    def no_socket(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a request left through a socket")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    decided = {"hub": 0, "local": 0}

    evil = "https://evil.test/v1"
    llm, agent = VALID["llm"], VALID["agent"]

    @GENERATED
    @given(st.lists(REMOTE_CONFIG, min_size=1, max_size=3), st.sampled_from(["hub", "local"]))
    @example([{**llm, "model": evil}, {**agent, "agent_id": evil}], "local")
    @example([{**llm, "model": evil}, {**agent, "agent_id": evil}], "hub")
    @example([{**VALID["check"], "members": {"a": RULES, "b": {**llm, "model": evil}}}], "local")
    @example([{**llm, "url": evil}, {**agent, "endpoint": evil, "api_key": "k-other"}], "local")
    @example([{**agent, "endpoint": evil}], "local")
    @example([{**llm, "base_url_env": "PACK_SAFETY_URL", "api_key_env": "LLM_API_KEY"}], "local")
    @example([{**agent, "endpoint_env": "PACK_SAFETY_URL", "token_env": "x"}], "hub")
    def check(configs: list[dict[str, Any]], origin: str) -> None:
        reader = place(configs)
        root = write(template, tmp_path_factory.mktemp("egress") / "pack", manifest(reader))
        try:
            try:
                pack = load_pack(root, origin=origin)  # type: ignore[arg-type]
            except PackValidationError:
                return
            for name in reader:
                try:
                    decision = Decision(pack, providers={"reader": name})
                except PackValidationError:
                    continue  # a variant that cannot be built calls nothing
                before = len(sent)
                asking.append(origin)
                decision.decide(BILL)
                decided[origin] += len(sent) > before
            for request, by in sent:
                origin_seen = f"{request.url.scheme}://{request.url.netloc.decode()}"
                if origin_seen in ENDPOINTS:
                    assert request.headers.get("authorization") == ENDPOINTS[origin_seen]
                    assert urlsplit(str(request.url)).path in ("/v1/chat/completions", "/v1/decide")
                else:  # an owned agent's own endpoint: never with a key of the deployment's
                    assert by == "local", request.url
                    assert "authorization" not in request.headers, request.url
        finally:
            shutil.rmtree(root.parent)

    check()
    assert decided["hub"] >= 5 and decided["local"] >= 5, decided
    assert {f"{r.url.scheme}://{r.url.netloc.decode()}" for r, _ in sent} >= set(ENDPOINTS)
