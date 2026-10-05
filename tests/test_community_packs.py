"""Community packs (plan B1, B2): packs outside developers contribute by pull request, in
`community-packs/<name>/` with id `community/<name>`. Where a pack was found sets its origin
(`core/origin.py`); a community pack is served only with MENDARI_COMMUNITY_PACKS=on, never
automates, decides with local providers unless the deployment names it, keeps only a hash of its
inputs and is at most risk level 1. One id is one pack, whatever the versions."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import Decision, load_pack
from mendari.core import registry
from mendari.core.policy import apply_policy
from mendari.core.registry import SearchPath, discover, origin_of, resolve_pack, search_paths
from mendari.core.spec import Outcome
from mendari.errors import MendariError
from mendari.providers import build_provider
from mendari.providers.base import Answer, ProviderInfo
from mendari.server.app import create_app
from mendari.settings import providers_for
from tests.conftest import INBOX_DIR, ROOT

PACKS = INBOX_DIR.parent
SORTER = ROOT / "tests" / "fixtures" / "community-packs" / "visit-sorter"
SORTER_ID = "community/visit-sorter"
BILL = {"message": "I have a question about my last bill."}
SHIPPED = {
    "mendari/ai-guard",
    "mendari/clinical-trial-prescreener",
    "mendari/patient-inbox-router",
    "mendari/prior-auth-preflight",
    "mendari/referral-router",
}


def _copy(
    base: Path,
    folder: str,
    edit: Callable[[dict[str, Any]], None] | None = None,
    source: Path = SORTER,
) -> Path:
    """A copy of a pack at `base/folder`, its manifest changed by `edit`."""
    root = base / folder
    shutil.copytree(source, root)
    if edit is not None:
        path = root / "pack.yaml"
        manifest = yaml.safe_load(path.read_text())
        edit(manifest)
        path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
    return root


def _set(**changes: Any) -> Callable[[dict[str, Any]], None]:
    def edit(manifest: dict[str, Any]) -> None:
        for dotted, value in changes.items():
            *parents, key = dotted.split("__")
            target = manifest
            for parent in parents:
                target = target.setdefault(parent, {})
            target[key] = value

    return edit


def _community(tmp_path: Path) -> Path:
    """A community-packs/ directory holding the example community pack."""
    base = tmp_path / "community-packs"
    _copy(base, "visit-sorter")
    return base


# -- where packs are searched for (B1) ----------------------------------------------------------


def test_community_packs_are_searched_only_when_turned_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _community(tmp_path)
    monkeypatch.setattr(registry, "_COMMUNITY_DIR", base)
    assert {entry.origin for entry in search_paths()} == {"maintained"}
    assert {pack.id for pack in discover().packs} == SHIPPED

    for off in ("off", "OFF", ""):
        monkeypatch.setenv("MENDARI_COMMUNITY_PACKS", off)
        assert SearchPath(base, "community") not in search_paths()
    # Off means off, even where MENDARI_PACKS_PATH names community-packs/.
    monkeypatch.setenv("MENDARI_PACKS_PATH", str(base))
    assert SORTER_ID not in {pack.id for pack in discover().packs}

    monkeypatch.delenv("MENDARI_PACKS_PATH")
    monkeypatch.setenv("MENDARI_COMMUNITY_PACKS", "on")
    assert search_paths()[-1] == SearchPath(base, "community")
    found = discover()
    assert {pack.id for pack in found.packs} == SHIPPED | {SORTER_ID}
    assert not found.problems
    origins = {pack.id: pack.origin for pack in found.packs}
    assert origins[SORTER_ID] == "community" and origins["mendari/ai-guard"] == "maintained"


def test_an_unknown_switch_value_fails_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MENDARI_COMMUNITY_PACKS", "yes")
    with pytest.raises(MendariError, match="MENDARI_COMMUNITY_PACKS must be on or off"):
        search_paths()


def test_a_directory_has_the_origin_of_where_it_sits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _community(tmp_path)
    monkeypatch.setattr(registry, "_COMMUNITY_DIR", base)
    assert origin_of(PACKS) == origin_of(INBOX_DIR) == "maintained"
    assert origin_of(base) == origin_of(base / "visit-sorter") == "community"
    assert origin_of(tmp_path) == origin_of(SORTER) == "local"
    monkeypatch.setenv("MENDARI_PACKS_PATH", str(tmp_path / "mine"))
    assert search_paths()[0] == SearchPath(tmp_path / "mine", "local")
    # A pack given by its directory carries that directory's origin.
    assert resolve_pack(str(base / "visit-sorter")).origin == "community"
    assert resolve_pack(str(INBOX_DIR)).origin == "maintained"


# -- what a community pack must be (B1) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "edit", "problem"),
    [
        ("nested/visit-sorter", None, "sits exactly one level deep"),
        ("other-name", None, "has the id community/other-name, not community/visit-sorter"),
        ("visit-sorter", _set(id="someone/visit-sorter"), "has the id community/visit-sorter"),
        ("Visit_Sorter", _set(id="community/visit-sorter"), "must be lowercase letters"),
        ("visit-sorter", _set(id="mendari/visit-sorter"), "namespace mendari/ is reserved"),
        ("visit-sorter", _set(risk__level=2), "a community pack is at most risk level 1"),
        ("visit-sorter", _set(audit__store_input="full"), "audit.store_input: hash"),
        (
            "visit-sorter",
            _set(providers__reader__use="llm"),
            "default variant 'llm' must be local",
        ),
    ],
    ids=[
        "too-deep",
        "folder-not-id",
        "not-community-namespace",
        "folder-name",
        "reserved-namespace",
        "risk-level",
        "store-input",
        "remote-default",
    ],
)
def test_a_community_pack_that_breaks_a_rule_is_listed_and_not_served(
    tmp_path: Path, folder: str, edit: Callable[[dict[str, Any]], None] | None, problem: str
) -> None:
    base = tmp_path / "community-packs"
    root = _copy(base, folder, edit)
    found = discover([SearchPath(base, "community")])
    assert found.packs == []
    assert problem in found.problems[str(root.resolve())]


def test_a_pack_at_the_root_of_community_packs_is_not_served(tmp_path: Path) -> None:
    base = tmp_path / "community-packs"
    shutil.copytree(SORTER, base)
    found = discover([SearchPath(base, "community")])
    assert found.packs == []
    assert "sits exactly one level deep" in found.problems[str(base.resolve())]


def test_a_composite_of_local_providers_is_local_and_one_with_a_remote_member_is_not(
    tmp_path: Path,
) -> None:
    def composite(member: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
        rules = {"type": "rules", "config": "logic/rules.yaml"}
        variant = {
            "type": "vote",
            "members": {"a": rules, "b": member},
            "weights": {"a": 1, "b": 1},
        }
        return _set(providers__reader__variants__vote=variant, providers__reader__use="vote")

    local = _copy(
        tmp_path / "a", "visit-sorter", composite({"type": "rules", "config": "logic/rules.yaml"})
    )
    remote = _copy(tmp_path / "b", "visit-sorter", composite({"type": "llm"}))
    assert [p.id for p in discover([SearchPath(local.parent, "community")]).packs] == [SORTER_ID]
    found = discover([SearchPath(remote.parent, "community")])
    assert found.packs == []
    assert "default variant 'vote' must be local" in found.problems[str(remote.resolve())]


@pytest.mark.parametrize("namespace", ["mendari", "openhealth", "community"])
def test_reserved_namespaces_are_refused_outside_their_origin(
    tmp_path: Path, namespace: str
) -> None:
    root = _copy(tmp_path / "mine", "sorter", _set(id=f"{namespace}/visit-sorter"))
    found = discover([tmp_path / "mine"])
    assert found.packs == []
    assert f"the namespace {namespace}/ is reserved" in found.problems[str(root.resolve())]


# -- one id, one pack (B1) ----------------------------------------------------------------------


def test_a_taken_id_is_never_replaced_whatever_the_version(tmp_path: Path) -> None:
    """Before, the newest version of an id won silently, so any directory on the search path
    could shadow a pack by claiming its id with a higher version."""
    first = _copy(tmp_path / "one", "sorter", _set(id="someone/sorter", version="0.1.0"))
    later = _copy(tmp_path / "two", "sorter", _set(id="someone/sorter", version="9.0.0"))
    found = discover([tmp_path / "one", tmp_path / "two"])
    assert [(pack.id, pack.version) for pack in found.packs] == [("someone/sorter", "0.1.0")]
    assert "already taken by the pack at" in found.problems[str(later.resolve())]
    assert "is served from here; also claimed by" in found.problems[str(first.resolve())]


def test_a_maintained_pack_keeps_its_id_against_an_earlier_directory(tmp_path: Path) -> None:
    claim = _copy(tmp_path / "mine", "sorter", _set(id="someone/sorter", version="9.0.0"))
    kept = _copy(tmp_path / "shipped", "sorter", _set(id="someone/sorter"))
    found = discover(
        [SearchPath(tmp_path / "mine", "local"), SearchPath(tmp_path / "shipped", "maintained")]
    )
    assert [(pack.origin, pack.version) for pack in found.packs] == [("maintained", "0.1.0")]
    assert "(maintained)" in found.problems[str(claim.resolve())]
    assert str(claim.resolve()) in found.problems[str(kept.resolve())]


def test_the_registry_cache_still_holds_versions_of_one_pack(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    for version in ("0.1.0", "0.2.0"):
        _copy(cache / "someone" / "sorter", version, _set(id="someone/sorter", version=version))
    found = discover([SearchPath(cache, "hub")])
    assert sorted(pack.version for pack in found.packs) == ["0.1.0", "0.2.0"]
    assert not found.problems
    assert resolve_pack("someone/sorter", [SearchPath(cache, "hub")]).version == "0.2.0"


def test_the_server_reports_both_claims_and_serves_the_first(tmp_path: Path) -> None:
    first = _copy(tmp_path / "one", "sorter", _set(id="someone/sorter", version="0.1.0"))
    later = _copy(tmp_path / "two", "sorter", _set(id="someone/sorter", version="9.0.0"))
    client = TestClient(
        create_app(db_path=tmp_path / "d.db", packs_paths=[tmp_path / "one", tmp_path / "two"])
    )
    health = client.get("/api/health").json()
    assert set(health["invalid"]) == {str(first.resolve()), str(later.resolve())}
    detail = client.get("/api/packs/someone/sorter").json()
    assert detail["manifest"]["version"] == "0.1.0"


# -- a community pack never stops a server (B1) -------------------------------------------------


def test_an_invalid_community_pack_is_listed_and_every_other_pack_decides(
    tmp_path: Path,
) -> None:
    base = _community(tmp_path)
    broken = _copy(base, "broken", _set(id="community/broken", risk__level=3))
    (base / "unreadable").mkdir()
    (base / "unreadable" / "pack.yaml").write_text("id: [")
    client = TestClient(
        create_app(db_path=tmp_path / "s.db", packs_paths=[PACKS, SearchPath(base, "community")])
    )
    health = client.get("/api/health").json()
    assert health["packs"] == len(SHIPPED) + 1
    assert set(health["invalid"]) == {str(broken.resolve()), str((base / "unreadable").resolve())}
    for pack_dir in sorted(PACKS.iterdir()):
        case = yaml.safe_load((pack_dir / "tests" / "cases.yaml").read_text())[0]
        response = client.post(f"/api/packs/mendari/{pack_dir.name}/decide", json=case["input"])
        assert response.status_code == 200, (pack_dir.name, response.text)
    response = client.post(f"/api/packs/{SORTER_ID}/decide", json=BILL)
    assert response.status_code == 200, response.text
    assert response.json()["recommendation"] == "billing"


# -- policy: recommend, never automate (B2) -----------------------------------------------------


def test_a_community_pack_recommends_and_never_automates(tmp_path: Path) -> None:
    """The same pack, outside the community namespace, automates this message."""
    outside = load_pack(_copy(tmp_path, "sorter", _set(id="someone/sorter")))
    assert not outside.community
    assert Decision(outside).decide(BILL).outcome is Outcome.AUTO

    for pack in (
        load_pack(SORTER),
        *discover([SearchPath(_community(tmp_path), "community")]).packs,
    ):
        assert pack.community
        result = Decision(pack).decide(BILL)
        assert result.outcome is Outcome.HUMAN_REVIEW
        assert result.recommendation == "billing"
        assert "a community pack (not clinically reviewed) recommends and never automates" in (
            result.reasons
        )


def test_a_rule_asking_for_auto_is_capped_for_a_community_pack() -> None:
    manifest = load_pack(SORTER).manifest
    info = ProviderInfo(slot="reader", variant="rules", type="rules")
    answers = {"topic": Answer("topic", "billing", 0.99, {"billing": 0.99}, info)}
    assert apply_policy(manifest, answers, {}).outcome is Outcome.AUTO
    capped = apply_policy(manifest, answers, {}, community=True)
    assert capped.outcome is Outcome.HUMAN_REVIEW and capped.recommendation == "billing"


# -- spend: only what the deployment names (B2) -------------------------------------------------


def test_unscoped_provider_settings_never_reach_a_community_pack() -> None:
    slots = {"reader": {"rules", "llm"}}
    unscoped = {"reader": "llm"}
    assert providers_for("someone/sorter", slots, unscoped) == ({"reader": "llm"}, [])
    assert providers_for(SORTER_ID, slots, unscoped, community=True) == ({}, [])
    scoped = {"reader": "rules", f"{SORTER_ID}:reader": "llm"}
    assert providers_for(SORTER_ID, slots, scoped, community=True) == ({"reader": "llm"}, [])
    wrong = {f"{SORTER_ID}:reader": "jev"}
    assert providers_for(SORTER_ID, slots, wrong, community=True) == (
        {},
        [f"{SORTER_ID}:reader=jev"],
    )


@pytest.mark.parametrize(
    ("settings", "variant"),
    [({"reader": "llm"}, "rules"), ({f"{SORTER_ID}:reader": "llm"}, "llm")],
    ids=["unscoped", "scoped"],
)
def test_the_server_runs_a_community_pack_on_what_names_it(
    tmp_path: Path, settings: dict[str, str], variant: str
) -> None:
    app = create_app(
        db_path=tmp_path / "p.db",
        packs_paths=[SearchPath(_community(tmp_path), "community")],
        providers=settings,
    )
    health = TestClient(app).get("/api/health").json()
    assert health["providers"][SORTER_ID] == {"reader": variant}
    assert SORTER_ID not in health.get("provider_settings_skipped", {})


def test_a_community_packs_pinned_model_and_max_tokens_give_way_to_the_deployments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_MODEL", "example/deployment-model")
    community = build_provider(load_pack(SORTER), "reader", "llm")
    assert community.info.model == "example/deployment-model"
    assert community.settings.max_tokens == 4096  # type: ignore[attr-defined]
    outside = load_pack(_copy(tmp_path, "sorter", _set(id="someone/sorter")))
    pinned = build_provider(outside, "reader", "llm")
    assert pinned.info.model == "example/pinned-model"
    assert pinned.settings.max_tokens == 30000  # type: ignore[attr-defined]


def test_a_composite_members_pinned_model_gives_way_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_MODEL", "example/deployment-model")
    member = {"type": "llm", "model": "example/pinned-model", "max_tokens": 30000}
    rules = {"type": "rules", "config": "logic/rules.yaml"}
    variant = {"type": "cascade", "accept_at": 0.9, "members": {"rules": rules, "llm": member}}
    root = _copy(tmp_path, "visit-sorter", _set(providers__reader__variants__mixed=variant))
    provider = build_provider(load_pack(root), "reader", "mixed")
    assert provider.info.model is not None
    assert provider.info.model.endswith("llm:example/deployment-model")
