"""Community packs (plan B1, B2): packs outside developers contribute by pull request, in
`community-packs/<name>/` with id `community/<name>`. Where a pack was found sets its origin
(`core/origin.py`); a community pack is served only with MENDARI_COMMUNITY_PACKS=on, never
automates, decides with local providers unless the deployment names it, keeps only a hash of its
inputs and is at most risk level 1. One id is one pack, whatever the versions."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import Decision, load_pack
from mendari.core import registry
from mendari.core.card import render_card
from mendari.core.origin import long_text
from mendari.core.policy import apply_policy
from mendari.core.registry import SearchPath, discover, origin_of, resolve_pack, search_paths
from mendari.core.spec import Outcome
from mendari.errors import MendariError
from mendari.providers import build_provider
from mendari.providers.base import Answer, ProviderInfo
from mendari.providers.rules import RulesProvider
from mendari.server.app import create_app
from mendari.settings import providers_for
from mendari.standards import cds_hooks
from tests.conftest import INBOX_DIR, ROOT
from tests.test_fhir import COMMUNICATION, PATIENT

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


BLOCK_RULE = {
    "name": "blk",
    "when": {"question": "topic", "labels": ["billing"]},
    "outcome": "BLOCK",
    "reason": "Billing is blocked.",
}


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
        ("visit-sorter", _set(policy__rules=[BLOCK_RULE]), "policy rule 'blk' has outcome BLOCK"),
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
        "block-rule",
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


@pytest.mark.parametrize("namespace", ["mendari", "openhealth", "someone"])
def test_a_pack_outside_community_packs_is_found_as_before_community_packs(
    tmp_path: Path, namespace: str
) -> None:
    """With community packs off, a deployment serves exactly what it served before: a local
    copy of a reference pack, under its own id, included. Only a community pack is held to
    the community namespace (the `reserved-namespace` case above)."""
    root = _copy(tmp_path / "mine", "sorter", _set(id=f"{namespace}/visit-sorter"))
    found = discover([tmp_path / "mine"])
    assert [(pack.id, pack.origin) for pack in found.packs] == [
        (f"{namespace}/visit-sorter", "local")
    ]
    assert not found.problems and not found.packs[0].community
    assert root.resolve() == found.packs[0].root


def test_a_pack_in_the_community_namespace_elsewhere_gets_the_community_limits(
    tmp_path: Path,
) -> None:
    _copy(tmp_path / "mine", "sorter")
    (pack,) = discover([tmp_path / "mine"]).packs
    assert (pack.origin, pack.shown_origin, pack.community) == ("local", "community", True)
    assert Decision(pack).decide(BILL).outcome is Outcome.HUMAN_REVIEW


def test_a_path_holding_packs_finds_them_as_maintained_with_community_packs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MENDARI_PACKS_PATH naming a checkout (CONTRIBUTING's `MENDARI_PACKS_PATH=<parent>`):
    before, every pack under it took the path's origin (local), so the five reference packs
    were refused for their namespace and then skipped as already seen: none was served."""
    monkeypatch.setenv("MENDARI_PACKS_PATH", str(ROOT))
    found = discover()
    # (A working copy may hold other packs, untracked: only the reference packs are asserted.)
    assert {pack.id for pack in found.packs if pack.origin == "maintained"} == SHIPPED
    assert {pack.origin for pack in found.packs if pack.id in SHIPPED} == {"maintained"}
    assert not [where for where in found.problems if "packs" in Path(where).parts]
    assert resolve_pack("mendari/referral-router").origin == "maintained"


def test_a_path_holding_community_packs_serves_them_only_when_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    base = _community(checkout)
    monkeypatch.setattr(registry, "_COMMUNITY_DIR", base)
    monkeypatch.setenv("MENDARI_PACKS_PATH", str(checkout))
    found = discover()
    assert SORTER_ID not in {pack.id for pack in found.packs} and not found.problems
    monkeypatch.setenv("MENDARI_COMMUNITY_PACKS", "on")
    found = discover()
    origins = {pack.id: pack.origin for pack in found.packs}
    assert origins[SORTER_ID] == "community" and not found.problems


def test_a_community_pack_from_a_git_submodule_is_not_served(tmp_path: Path) -> None:
    base = _community(tmp_path)
    (base / "visit-sorter" / ".git").write_text("gitdir: ../../.git/modules/visit-sorter\n")
    found = discover([SearchPath(base, "community")])
    assert found.packs == []
    assert "never a git submodule" in found.problems[str((base / "visit-sorter").resolve())]


# -- one id, one pack where a community pack takes part (B1) ------------------------------------


def test_a_pulled_reference_pack_is_served_as_before(tmp_path: Path) -> None:
    """README's hub flow: `mendari pull mendari/referral-router`, then
    `mendari eval mendari/referral-router` uses the newest version, the verified pulled one."""
    cache = tmp_path / "cache"
    pulled = PACKS / "referral-router"
    _copy(cache / "mendari" / "referral-router", "9.9.9", _set(version="9.9.9"), source=pulled)
    paths = [SearchPath(cache, "hub"), SearchPath(PACKS, "maintained")]
    found = discover(paths)
    versions = sorted(p.version for p in found.packs if p.id == "mendari/referral-router")
    assert versions == [load_pack(pulled).version, "9.9.9"] and not found.problems
    assert resolve_pack("mendari/referral-router", paths).version == "9.9.9"
    assert resolve_pack("mendari/referral-router@9.9.9", paths).origin == "hub"
    client = TestClient(create_app(db_path=tmp_path / "h.db", packs_paths=paths))
    detail = client.get("/api/packs/mendari/referral-router").json()
    assert detail["manifest"]["version"] == "9.9.9"


def test_ids_no_community_pack_claims_are_found_as_before(tmp_path: Path) -> None:
    _copy(tmp_path / "one", "sorter", _set(id="someone/sorter", version="0.1.0"))
    _copy(tmp_path / "two", "sorter", _set(id="someone/sorter", version="9.0.0"))
    found = discover([tmp_path / "one", tmp_path / "two"])
    assert sorted(pack.version for pack in found.packs) == ["0.1.0", "9.0.0"]
    assert not found.problems


def test_a_community_packs_id_is_never_replaced_whatever_the_version(tmp_path: Path) -> None:
    """A community pack never shares its id: the first claim holds it, and both are reported."""
    base = _community(tmp_path)
    later = _copy(tmp_path / "mine", "sorter", _set(version="9.0.0"))
    found = discover([SearchPath(base, "community"), tmp_path / "mine"])
    assert [(pack.id, pack.version, pack.origin) for pack in found.packs] == [
        (SORTER_ID, "0.1.0", "community")
    ]
    assert "already taken by the pack at" in found.problems[str(later.resolve())]
    first = (base / "visit-sorter").resolve()
    assert "is served from here; also claimed by" in found.problems[str(first)]


def test_the_registry_cache_still_holds_versions_of_one_pack(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    for version in ("0.1.0", "0.2.0"):
        _copy(cache / "someone" / "sorter", version, _set(id="someone/sorter", version=version))
    found = discover([SearchPath(cache, "hub")])
    assert sorted(pack.version for pack in found.packs) == ["0.1.0", "0.2.0"]
    assert not found.problems
    assert resolve_pack("someone/sorter", [SearchPath(cache, "hub")]).version == "0.2.0"


def test_the_server_reports_both_claims_of_a_community_id_and_serves_the_first(
    tmp_path: Path,
) -> None:
    base = _community(tmp_path)
    later = _copy(tmp_path / "mine", "sorter", _set(version="9.0.0"))
    first = (base / "visit-sorter").resolve()
    client = TestClient(
        create_app(
            db_path=tmp_path / "d.db",
            packs_paths=[SearchPath(base, "community"), tmp_path / "mine"],
        )
    )
    health = client.get("/api/health").json()
    assert set(health["invalid"]) == {str(first), str(later.resolve())}
    detail = client.get(f"/api/packs/{SORTER_ID}").json()
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


def test_a_community_pack_never_blocks_on_its_own(tmp_path: Path) -> None:
    """A rule's BLOCK is final at once: no person would see the case. A community pack loaded
    by path (its load in community-packs/ refuses such a rule) sends it to a person instead;
    any other pack still blocks."""
    outside = load_pack(
        _copy(tmp_path / "a", "sorter", _set(id="someone/sorter", policy__rules=[BLOCK_RULE]))
    )
    assert Decision(outside).decide(BILL).outcome is Outcome.BLOCK
    pack = load_pack(_copy(tmp_path / "b", "sorter", _set(policy__rules=[BLOCK_RULE])))
    assert pack.community
    result = Decision(pack).decide(BILL)
    assert result.outcome is Outcome.HUMAN_REVIEW and result.recommendation == "billing"
    assert any("never blocks on its own" in reason for reason in result.reasons)
    card = render_card(pack)
    assert "`BLOCK`" not in card.split("## Decision policy")[1].split("## Providers")[0]


def test_a_community_pack_reads_texts_no_longer_than_its_patterns_were_timed_on() -> None:
    """Before any pattern runs (the schema's included): `re` has no time limit."""
    long = {"message": "x" * 10_001}
    result = Decision(load_pack(SORTER)).decide(long)
    assert result.outcome is Outcome.BLOCK
    assert result.reasons == (
        "input rejected: message: longer than 10,000 characters, the most a community pack reads",
    )
    assert Decision(load_pack(SORTER)).decide({"message": "x" * 2_000}).outcome is not None
    maintained = Decision(load_pack(INBOX_DIR)).decide({"message": {"body": "x" * 10_001}})
    assert not any("community pack" in reason for reason in maintained.reasons)


def test_a_community_pack_holds_its_texts_together_to_that_length() -> None:
    """A rule matches its text fields joined, so two texts within the length each are refused
    when, joined, they are longer; texts that join to exactly the length are read."""
    halves = {"message": "x" * 5_000, "subject": "y" * 5_000}
    result = Decision(load_pack(SORTER)).decide(halves)
    assert result.reasons == (
        "input rejected: the input's texts together: longer than 10,000 characters, the most a "
        "community pack reads",
    )
    assert long_text(["x" * 5_000, "y" * 4_999]) is None
    assert long_text([["x"] * 2_500, "y" * 5_000]) is None  # a separator each
    assert long_text([["x"] * 2_501, "y" * 5_000]) == "the input's texts together"


def test_a_community_pack_counts_keys_with_its_texts() -> None:
    """The schema's patterns run on every key (`patternProperties`, `propertyNames`): before,
    keys were held to the length one by one, so one input ran them on many long keys."""
    keys = {"k" * 9_000 + str(i): 1 for i in range(50)}
    result = Decision(load_pack(SORTER)).decide({"message": "hello", "extra": keys})
    assert result.reasons == (
        "input rejected: the input's texts together: longer than 10,000 characters, the most a "
        "community pack reads",
    )
    assert long_text({"k" * 4_999: "v" * 5_000}) is None  # with a separator between
    assert long_text({"k" * 4_999: "v" * 5_001}) == "the input's texts together"


def test_a_community_packs_typo_corrections_never_lengthen_its_text_past_the_length(
    tmp_path: Path,
) -> None:
    """A correction can add a character ("invoce" is "invoice"): before, a 10,000-character
    text grew past the length the pack's patterns were timed on. Other packs correct all."""
    community = _copy(tmp_path / "a", "sorter")
    outside = _copy(tmp_path / "b", "sorter", _set(id="someone/sorter"))
    for root in (community, outside):
        rules = root / "logic" / "rules.yaml"
        rules.write_text(rules.read_text() + "typo_tolerance: {min_length: 4}\n")
    text = "invoce " * 1_428  # 9,996 characters
    readers = [build_provider(load_pack(root), "reader") for root in (community, outside)]
    assert all(isinstance(reader, RulesProvider) for reader in readers)
    capped, fixes = cast(RulesProvider, readers[0])._normalize(text)
    assert len(capped) <= 10_000 and fixes and "invoce" in capped
    full, all_fixes = cast(RulesProvider, readers[1])._normalize(text)
    assert len(full) == 9_996 + 1_428 and len(all_fixes) == 1_428


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


# -- labels on every surface (B3) ---------------------------------------------------------------

COMMUNITY_LABEL = "Community · not clinically reviewed"
INBOX_ID = "mendari/patient-inbox-router"
INBOX_PERFORMERS = "https://mendari.co/fhir/CodeSystem/inbox-destination"
MESSAGE_HOOK = {
    "hook": "mendari-message-received",
    "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
}


OWN_PERFORMERS = "https://mendari.co/packs/community/inbox-copy/CodeSystem/performer"


def _inbox_as_community(base: Path) -> Path:
    """The inbox router as a community pack would be: same content, community id and rules,
    and its performer CodeSystem under its own url."""
    root = _copy(
        base,
        "inbox-copy",
        _set(id="community/inbox-copy", risk__level=0, audit__store_input="hash"),
        source=INBOX_DIR,
    )
    mapping = root / "fhir" / "mapping.yaml"
    mapping.write_text(mapping.read_text().replace(INBOX_PERFORMERS, OWN_PERFORMERS))
    return root


@pytest.fixture
def labelled(tmp_path: Path) -> TestClient:
    """The shipped packs and two community packs (one a CDS service) on one server."""
    base = _community(tmp_path)
    _inbox_as_community(base)
    app = create_app(db_path=tmp_path / "l.db", packs_paths=[PACKS, SearchPath(base, "community")])
    return TestClient(app)


def test_the_api_names_every_packs_origin_by_where_it_was_found(labelled: TestClient) -> None:
    listed = {pack["id"]: pack for pack in labelled.get("/api/packs").json()}
    assert {listed[pack_id]["origin"] for pack_id in SHIPPED} == {"maintained"}
    assert {listed[pack_id]["origin_label"] for pack_id in SHIPPED} == {"Maintained by Mendari"}
    for pack_id in (SORTER_ID, "community/inbox-copy"):
        assert listed[pack_id]["origin"] == "community"
        assert listed[pack_id]["origin_label"] == COMMUNITY_LABEL
    detail = labelled.get(f"/api/packs/{SORTER_ID}").json()
    assert detail["origin"] == "community" and detail["origin_label"] == COMMUNITY_LABEL
    assert f"| Source | {COMMUNITY_LABEL} |" in detail["card_markdown"]


def test_health_counts_the_packs_by_origin(labelled: TestClient) -> None:
    health = labelled.get("/api/health").json()
    assert health["invalid"] == {}
    assert health["packs"] == len(SHIPPED) + 2
    assert health["packs_by_origin"] == {"maintained": len(SHIPPED), "community": 2}


def test_a_pack_in_the_community_namespace_is_labelled_community_wherever_it_was_loaded() -> None:
    pack = load_pack(SORTER)
    assert pack.origin == "local"
    assert (pack.shown_origin, pack.origin_label) == ("community", COMMUNITY_LABEL)
    maintained = load_pack(INBOX_DIR, origin="maintained")
    assert (maintained.shown_origin, maintained.origin_label) == (
        "maintained",
        "Maintained by Mendari",
    )


def test_the_decision_card_labels_a_community_pack_and_says_it_never_automates() -> None:
    card = render_card(load_pack(SORTER, origin="community"))
    assert f"| Source | {COMMUNITY_LABEL} |" in card
    assert "| Automation | Never: a community pack recommends, and a person confirms" in card
    assert "AUTO permitted" not in card
    assert "published by Mendari tests (community, self-declared)" in card
    # The policy section says the same: never "AUTO when confidence >= 0.90".
    policy = card.split("## Decision policy")[1].split("## Providers")[0]
    assert "- Never automated: a person reviews from `topic` confidence ≥ **0.40**" in policy
    assert "AUTO" not in policy
    maintained = render_card(load_pack(INBOX_DIR, origin="maintained"))
    assert "- AUTO when `destination` confidence" in maintained


@pytest.mark.parametrize("pack_dir", sorted(PACKS.glob("*/pack.yaml")), ids=lambda p: p.parent.name)
def test_every_shipped_card_is_what_it_renders_and_says_maintained(pack_dir: Path) -> None:
    pack = resolve_pack(str(pack_dir.parent))
    card = render_card(pack)
    assert "| Source | Maintained by Mendari |" in card
    assert "self-declared)" in card
    assert (pack_dir.parent / "DECISION_CARD.md").read_text(encoding="utf-8") == card


def test_cds_discovery_and_cards_say_a_community_pack_is_not_clinically_reviewed(
    tmp_path: Path, labelled: TestClient
) -> None:
    services = {s["id"]: s for s in labelled.get("/cds-services").json()["services"]}
    community = services["community.inbox-copy"]
    assert COMMUNITY_LABEL in community["description"]
    assert community["usageRequirements"].startswith(
        f"{COMMUNITY_LABEL}: it recommends, and a person confirms every decision. "
    )
    maintained = services["mendari.patient-inbox-router"]
    assert COMMUNITY_LABEL not in maintained["description"] + maintained["usageRequirements"]

    pack = load_pack(_inbox_as_community(tmp_path), origin="community")
    card = cds_hooks.invoke(Decision(pack), MESSAGE_HOOK)["cards"][0]
    assert card["source"]["label"] == (
        f"Mendari · {pack.manifest.name} {pack.version} · {COMMUNITY_LABEL}"
    )
    shipped = cds_hooks.invoke(Decision(load_pack(INBOX_DIR)), MESSAGE_HOOK)["cards"][0]
    assert COMMUNITY_LABEL not in shipped["source"]["label"]


def test_the_docs_landing_labels_community_packs_in_open_mode(labelled: TestClient) -> None:
    text = labelled.get("/docs").text
    link = f'<a href="/?pack={SORTER_ID}#card">'
    assert link in text
    item = text[text.index(link) : text.index("</li>", text.index(link))]
    assert COMMUNITY_LABEL in item
    inbox = f'<a href="/?pack={INBOX_ID}#card">'
    assert COMMUNITY_LABEL not in text[text.index(inbox) : text.index("</li>", text.index(inbox))]


# -- the contribution flow's documents (B7) -----------------------------------------------------


def test_the_contribution_forms_and_guide_hang_together() -> None:
    """The issue form parses and is labelled with labels the owner creates (B8), the pack PR
    template asks for the sign-off, and the guide and ADR-0007 are in the docs site."""
    form = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE/propose-pack.yml").read_text())
    assert form["name"] == "Propose a pack" and form["labels"] == ["pack", "idea"]
    assert {item["id"] for item in form["body"] if "id" in item} >= {"name", "risk", "data"}
    template = (ROOT / ".github/PULL_REQUEST_TEMPLATE/pack.md").read_text()
    assert "git commit -s" in template and "community-check" in template
    nav = (ROOT / "mkdocs.yml").read_text()
    assert "guides/contributing-a-pack.md" in nav
    assert "decisions/ADR-0007-community-packs-by-pull-request.md" in nav
    guide = (ROOT / "docs/guides/contributing-a-pack.md").read_text()
    assert COMMUNITY_LABEL in guide and "`mendari/patient-inbox-router` | Yes" in guide


# -- FHIR terminology: its own url, labelled (B3) -----------------------------------------------


def test_a_community_pack_cannot_write_into_a_codesystem_of_mendaris(tmp_path: Path) -> None:
    """Before, a community pack using the inbox router's performer system had its definitions
    served as Mendari's own, ahead of the router's (`community/` sorts before `mendari/`)."""
    from mendari.standards.terminology import Terminology

    base = tmp_path / "community-packs"
    root = _copy(
        base,
        "inbox-copy",
        _set(id="community/inbox-copy", risk__level=0, audit__store_input="hash"),
        source=INBOX_DIR,
    )
    found = discover([SearchPath(base, "community")])
    assert found.packs == []
    assert "task.performer_system" in found.problems[str(root.resolve())]
    assert "https://mendari.co/packs/community/inbox-copy/" in found.problems[str(root.resolve())]

    # Loaded by path, past the registry: the maintained router keeps its CodeSystem as it is.
    inbox = load_pack(INBOX_DIR, origin="maintained")
    alone, _ = Terminology.serving([inbox])
    both, problems = Terminology.serving([load_pack(root, origin="community"), inbox])
    assert both.by_url(INBOX_PERFORMERS) == alone.by_url(INBOX_PERFORMERS)
    assert "a community pack's systems are its own" in problems["community/inbox-copy"]


def test_a_community_packs_codesystems_say_who_published_them_and_that_they_are_unreviewed(
    tmp_path: Path,
) -> None:
    from mendari.standards import davinci, terminology

    sorter = load_pack(SORTER, origin="community")
    inbox = load_pack(_inbox_as_community(tmp_path), origin="community")
    found = {r.get("url"): r for r in terminology.resources([sorter, inbox])}
    for url in (davinci.question_system(SORTER_ID, "topic"), OWN_PERFORMERS):
        system = found[url]
        assert system["publisher"] != "Mendari" and system["experimental"] is True
        assert COMMUNITY_LABEL in system["title"] and COMMUNITY_LABEL in system["description"]
    assert found[davinci.question_system(SORTER_ID, "topic")]["publisher"] == "Mendari tests"
    shipped = {r.get("url"): r for r in terminology.resources([load_pack(INBOX_DIR)])}
    assert shipped[INBOX_PERFORMERS]["publisher"] == "Mendari"
    assert shipped[INBOX_PERFORMERS]["experimental"] is False


def test_a_community_packs_dtr_questionnaire_is_labelled(tmp_path: Path) -> None:
    from mendari.standards import davinci

    pack = load_pack(
        _copy(tmp_path, "pa", _set(id="community/pa"), source=PACKS / "prior-auth-preflight")
    )
    form = davinci.questionnaire(pack, {"service": "lumbar_mri", "payer": "acme"})
    assert form is not None
    assert COMMUNITY_LABEL in form["title"] and form["experimental"] is True
    assert form["publisher"] == pack.manifest.publisher.name
    shipped = davinci.questionnaire(
        load_pack(PACKS / "prior-auth-preflight"), {"service": "lumbar_mri", "payer": "acme"}
    )
    assert shipped is not None and "experimental" not in shipped


# -- evaluations through the API spend only what the deployment named (B2) ---------------------


def test_the_eval_endpoint_runs_a_community_pack_remotely_only_where_named(
    tmp_path: Path, labelled: TestClient
) -> None:
    """Before, POST /api/packs/<community>/evals with `providers` ran any variant: up to 200
    paid calls per request, which the deployment never chose for the pack."""
    url = "/api/packs/community/inbox-copy/evals"
    remote = labelled.post(url, json={"dataset": "red_flags", "providers": {"router": "llm"}})
    assert remote.status_code == 400
    assert "runs a remote variant only where the deployment names it" in remote.json()["detail"]
    local = labelled.post(url, json={"dataset": "red_flags", "providers": {"router": "rules"}})
    assert local.status_code == 200, local.text
    assert local.json()["providers"]["router"]["variant"] == "rules"
    # A maintained pack's evaluation takes any variant, as before (here, a local one).
    shipped = labelled.post(
        f"/api/packs/{INBOX_ID}/evals",
        json={"dataset": "red_flags", "providers": {"router": "rules"}},
    )
    assert shipped.status_code == 200

    # The deployment named the pack for that variant: then the evaluation may run it too.
    base = tmp_path / "named" / "community-packs"
    _inbox_as_community(base)
    app = create_app(
        db_path=tmp_path / "n.db",
        packs_paths=[SearchPath(base, "community")],
        providers={"community/inbox-copy:router": "llm"},
    )
    named = TestClient(app).post(
        url, json={"dataset": "red_flags", "limit": 1, "providers": {"router": "llm"}}
    )
    assert named.status_code != 400 or "names it" not in named.text
