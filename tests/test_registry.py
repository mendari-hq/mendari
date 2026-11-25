# SPDX-License-Identifier: Apache-2.0
"""The registry service (hub/registry.py), on SQLite and Postgres."""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

import mendari
from mendari.core.card import render_card
from mendari.core.pack import Pack, load_pack, pack_digest
from mendari.core.reference import bundled_dir
from mendari.evals import figures as figures_module
from mendari.evals import stats
from mendari.hub import archive
from mendari.hub import registry as registry_module
from mendari.hub.registry import RegistryStore
from tests.conftest import (
    CLINICAL_DIR,
    FIXTURE_NAMESPACE,
    TRIAGE_DIR,
    Registry,
    share_signed,
    shared_benchmark,
    verified_site,
)
from tests.support.registry import with_lookahead

# The agent `Registry.publish()` publishes, in its own namespace (`FIXTURE_NAMESPACE`), with its
# own token and key (`Registry.fixture_publisher`); and a second, at risk level 2.
TRIAGE = load_pack(TRIAGE_DIR)
CLINICAL = load_pack(CLINICAL_DIR)


def test_a_signed_pack_is_published_once_and_read_back(registry: Registry) -> None:
    published = registry.publish()
    assert published.status_code == 201, published.text
    body = published.json()
    assert (body["id"], body["version"], body["digest"]) == (
        TRIAGE.id,
        TRIAGE.version,
        TRIAGE.digest,
    )
    assert body["risk"] == {"level": 1, "category": "clinical information"}
    assert body["standards"] == ["CDS Hooks mendari-message-received", "FHIR R4"]
    # The publisher's name as plain text: its Markdown escaped.
    assert body["card_markdown"].startswith(r"# Decision Card: Synthetic Triage \(test fixture\)")
    assert "benchmark" not in body and "leaderboard" not in body
    again = registry.publish()
    assert again.status_code == 409 and "immutable" in again.text
    pack = registry.client.get(f"/v1/agents/{TRIAGE.id}").json()
    assert pack["versions"] == [TRIAGE.version] and pack["latest"]["digest"] == TRIAGE.digest


def test_the_archive_downloads_byte_for_byte(registry: Registry) -> None:
    data = archive.build(TRIAGE, registry.fixture_publisher()[1])
    registry.publish()
    response = registry.client.get(f"/v1/agents/{TRIAGE.id}/{TRIAGE.version}/archive")
    assert response.content == data
    assert response.headers["x-archive-sha256"] == hashlib.sha256(data).hexdigest()


def test_publishing_needs_the_namespaces_token(registry: Registry) -> None:
    other_token = registry.namespace("other")
    assert registry.publish(token=other_token).status_code == 403
    assert registry.publish(token="ohr_made_up").status_code == 403


def test_a_keys_body_is_read_only_for_the_namespaces_token_and_only_so_much(
    registry: Registry,
) -> None:
    """Registering a key reads its body after the token is checked, as every other write does:
    without the namespace's token any body is a 403 (it was parsed first, so a body that is not
    a key was a 422 to anyone), and with it a body past `MAX_SMALL_BYTES` is a 413."""
    path = "/v1/namespaces/mendari/keys"
    for headers in ({}, {"authorization": f"Bearer {registry.namespace('other')}"}):
        for content in (b"{" * 100, json.dumps({"public_pem": "x" * 50}).encode()):
            answer = registry.client.post(path, content=content, headers=headers)
            assert answer.status_code == 403, answer.text
    owner = {"authorization": f"Bearer {registry.token}"}
    large = json.dumps({"public_pem": "x" * registry_module.MAX_SMALL_BYTES}).encode()
    assert registry.client.post(path, content=large, headers=owner).status_code == 413
    not_a_key = registry.client.post(path, json={"public_pem": "x" * 50}, headers=owner)
    assert not_a_key.status_code == 422 and "not a PEM public key" in not_a_key.text
    short = registry.client.post(path, json={"public_pem": "x"}, headers=owner)
    assert short.status_code == 422
    public = archive.generate_key()[1].decode()
    added = registry.client.post(path, json={"public_pem": public}, headers=owner)
    assert added.status_code == 201 and added.json()["key_id"].startswith("ed25519:")


def test_an_archive_signed_by_an_unregistered_key_is_refused(registry: Registry) -> None:
    stranger = archive.load_private_key(archive.generate_key()[0])
    refused = registry.publish(key=stranger)
    assert refused.status_code == 422 and "not trusted" in refused.text


def test_the_archive_must_be_the_pack_and_version_in_the_url(registry: Registry) -> None:
    refused = registry.publish(version="9.9.9")
    assert refused.status_code == 422 and "not this agent and version" in refused.text


def test_verification_sets_the_publisher_type(registry: Registry) -> None:
    registry.publish()
    patched = registry.operator.set_publisher_type(FIXTURE_NAMESPACE, "verified", by="operator")
    assert patched["publisher_type"] == "verified"
    pack = registry.client.get(f"/v1/agents/{TRIAGE.id}").json()
    assert pack["latest"]["publisher_type"] == "verified"
    namespace = registry.client.get(f"/v1/namespaces/{FIXTURE_NAMESPACE}").json()
    assert namespace["packs"] == [TRIAGE.id]
    key = registry.fixture_publisher()[1]
    assert namespace["keys"][0]["key_id"] == archive.key_id(key.public_key())
    found = registry.client.get("/v1/search", params={"publisher_type": "verified"}).json()
    assert [p["id"] for p in found] == [TRIAGE.id]


def test_search_by_words_risk_and_standard(registry: Registry) -> None:
    registry.publish()
    registry.publish(CLINICAL)

    def ids(**params: Any) -> list[str]:
        return [p["id"] for p in registry.client.get("/v1/search", params=params).json()]

    assert ids() == [CLINICAL.id, TRIAGE.id]
    assert ids(q="message team") == [TRIAGE.id]
    assert ids(q="severity") == [CLINICAL.id]  # a question's key is searched too
    assert ids(risk=2) == [CLINICAL.id]
    assert ids(standard="cds hooks mendari-message-received") == [TRIAGE.id]
    assert ids(q="nothing-like-this") == []
    assert "card_markdown" not in registry.client.get("/v1/search").json()[0]


def test_unknown_things_are_404(registry: Registry) -> None:
    assert registry.client.get("/v1/agents/mendari/none").status_code == 404
    assert registry.client.get(f"/v1/agents/{TRIAGE.id}/0.0.1").status_code == 404
    assert registry.client.get("/v1/namespaces/nobody").status_code == 404


def test_tokens_are_stored_hashed(registry: Registry, db_target: str) -> None:
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        stored = [
            row["token_sha256"] for row in db.all("SELECT token_sha256 FROM hub_publisher_tokens")
        ]
    assert stored == [hashlib.sha256(registry.token.encode()).hexdigest()]
    assert registry.client.get("/v1/health").json()["status"] == "ok"


def test_sites_share_signed_counts_and_publisher_tokens_no_longer_do(
    registry: Registry, tmp_path: Path
) -> None:
    registry.publish()
    report = shared_benchmark()
    path = f"/v1/agents/{TRIAGE.id}/{TRIAGE.version}/reports"
    publisher = {"authorization": f"Bearer {registry.fixture_publisher()[0]}"}
    refused = registry.client.post(path, json={"report": report}, headers=publisher)
    assert refused.status_code == 401 and "mendari site init" in refused.text
    assert registry.client.post(path, json={"report": report}).status_code == 401
    site = verified_site(registry, tmp_path)
    shared = share_signed(registry, site, report)
    assert shared.status_code == 201, shared.text
    listed = registry.client.get(f"/v1/agents/{TRIAGE.id}/reports").json()
    assert set(listed) == {"id", "reports", "superseded"}  # no uploaded benchmark (ADR-0012)
    (served,) = listed["reports"]
    assert (served["site"], served["verified"], served["key_id"]) == (site.id, True, site.key_id)


def _rates(value: Any, path: str = "") -> list[str]:
    """Where a float that is not an interval's bound sits in an answer."""
    if isinstance(value, dict):
        return [p for key, item in value.items() for p in _rates(item, f"{path}.{key}")]
    if isinstance(value, list):
        return [p for i, item in enumerate(value) for p in _rates(item, f"{path}.{i}")]
    if isinstance(value, float) and not path.endswith((".low", ".high")):
        return [path]
    return []


def test_the_registry_computes_every_rate_it_shows(registry: Registry, tmp_path: Path) -> None:
    """A shared report holds counts; every share the registry serves of it is k of n with its
    exact interval, worked out from those counts. No number in the answer is a rate the site
    sent."""
    registry.publish()
    report = shared_benchmark()
    assert share_signed(registry, verified_site(registry, tmp_path), report).status_code == 201
    (served,) = registry.client.get(f"/v1/agents/{TRIAGE.id}/reports").json()["reports"]
    figures = served["figures"]
    assert _rates(figures) == []
    assert figures == json.loads(json.dumps(figures_module.site(report)))
    primary = TRIAGE.manifest.policy.primary
    question = report["questions"][primary]
    accuracy = figures["questions"][primary]["accuracy"]
    assert (accuracy["k"], accuracy["n"]) == (question["right"], question["n"])
    assert (accuracy["low"], accuracy["high"]) == pytest.approx(
        stats.clopper_pearson(question["right"], question["n"])
    )
    for item, counted in zip(figures["critical"], report["decisions"]["critical"], strict=True):
        assert (item["detected"]["k"], item["detected"]["n"]) == (counted["detected"], counted["n"])
        assert item["automated"]["k"] == counted["auto_missed"]


# -- a publisher's content is refused with a reason, never a server error ------------------------


def test_a_pack_that_fails_to_load_in_any_way_is_refused_not_a_server_error(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: only a Mendari error from loading the published pack was caught; any other (raised by
    the publisher's content) was a 500. It is refused, named by its type alone."""

    def load(*args: Any, **options: Any) -> Any:
        raise RecursionError("text from the pack")

    monkeypatch.setattr(archive, "load_pack", load)
    refused = registry.publish()
    assert refused.status_code == 422, refused.text
    assert "the agent does not load (RecursionError)" in refused.text
    assert "text from the pack" not in refused.text


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("evals/benchmark.json", b"[1]"),
        ("evals/benchmark.json", b'{"questions": [1], "dataset": "x"}'),
        ("evals/benchmark.json", b"[" * 100_000 + b"]" * 100_000),
        ("evals/leaderboard.json", b'{"rows": "x"}'),
        ("DECISION_CARD.md", b"\xff\xfe junk"),
    ],
    ids=[
        "benchmark-a-list",
        "benchmark-odd-shapes",
        "benchmark-too-deep",
        "leaderboard-odd",
        "card-not-utf8",
    ],
)
def test_uploaded_attestations_of_any_shape_are_never_read(
    registry: Registry, tmp_path: Path, relative: str, content: bytes
) -> None:
    """T12 made a malformed benchmark or card a 500; since LP-18 the registry reads neither
    (ADR-0012): whatever the pack carries about itself publishes, unread, and shows nothing."""
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE.root, root)
    (root / relative).parent.mkdir(exist_ok=True)
    (root / relative).write_bytes(content)
    answer = registry.publish(load_pack(root))
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert "benchmark" not in body and "leaderboard" not in body
    assert body["card_markdown"] == _registry_card(root)


def test_an_over_long_version_is_refused_and_search_keeps_working(
    registry: Registry, tmp_path: Path
) -> None:
    """T10: a pack whose manifest named a version of 4,400 digits was stored, after which
    `GET /v1/search` compared it with `int()` and answered 500 for every caller."""
    assert registry.publish().status_code == 201
    huge = "1" + "0" * 4400 + ".0.0"
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE.root, root)
    manifest = (
        (root / "agent.yaml")
        .read_text()
        .replace(f"version: {TRIAGE.version}", f"version: '{huge}'", 1)
    )
    (root / "agent.yaml").write_text(manifest)
    signed = Pack(
        manifest=TRIAGE.manifest.model_copy(update={"version": huge}),
        root=root,
        digest=pack_digest(root),
        input_schema=TRIAGE.input_schema,
    )
    refused = registry.publish(signed)
    assert refused.status_code == 422, refused.text
    search = registry.client.get("/v1/search")
    assert search.status_code == 200
    assert [item["version"] for item in search.json()] == [TRIAGE.version]


def _nested(depth: int) -> Any:
    value: Any = 1
    for _ in range(depth):
        value = [value]
    return value


def test_a_deeply_nested_attestation_is_shown_as_none_and_search_keeps_working(
    registry: Registry, tmp_path: Path
) -> None:
    """`describe` stored `dataset.name`, `n`, `accuracy` and the leaderboard as any JSON value.
    One nested 300 levels deep was committed, then the response serializer gave up (about 255
    levels): the publish was a 500, and so was `GET /v1/search` for everyone, for good."""
    assert registry.publish(CLINICAL).status_code == 201
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE.root, root)
    (root / "evals").mkdir()
    primary = {"n": True, "accuracy": float("nan")}
    benchmark = {
        "dataset": {"name": _nested(300)},
        "questions": {TRIAGE.manifest.policy.primary: primary},
    }
    (root / "evals" / "benchmark.json").write_text(json.dumps(benchmark))
    (root / "evals" / "leaderboard.json").write_text(json.dumps({"rows": _nested(300)}))
    answer = registry.publish(load_pack(root))
    assert answer.status_code == 201, answer.text
    assert "benchmark" not in answer.json() and "leaderboard" not in answer.json()
    assert registry.client.get("/v1/search").status_code == 200
    for path in ("", f"/{TRIAGE.version}"):
        assert registry.client.get(f"/v1/agents/{TRIAGE.id}{path}").status_code == 200


def test_metadata_that_cannot_be_served_is_refused_before_it_is_stored(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def describe(pack: Any, verified: Any) -> dict[str, Any]:
        return {"pack_name": "x", "deep": _nested(300)}

    monkeypatch.setattr(registry_module, "describe", describe)
    refused = registry.publish()
    assert refused.status_code == 422, refused.text
    assert "the agent's metadata cannot be served (PydanticSerializationError)" in refused.text
    monkeypatch.undo()
    assert registry.client.get("/v1/search").json() == []
    assert registry.publish().status_code == 201


def test_a_composite_whose_members_cannot_run_is_refused_at_publish(
    registry: Registry, tmp_path: Path
) -> None:
    """Every variant and member is checked when a pack loads (LP-16e): a composite whose
    members are a list, or keyed by what YAML reads as a date (once a 500, then published
    without them), is refused naming the variants, before anything is stored; the owned copy
    it was made from loads, with them as warnings, and without them it publishes, its
    composites' members named. On the triage fixture, whose patterns all run on RE2: a changed
    copy (at a new version: at its own it is refused, F) is checked on the engine for packs from
    a registry (ADR-0009)."""
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE.root, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["version"] = "9.9.9"
    variants = manifest["providers"]["screen"]["variants"]
    rules, agent = variants["rules"], variants["agent"]
    variants["dated"] = {
        "type": "check",
        "members": {datetime.date(2024, 1, 1): rules, "agent": agent},
    }
    variants["listed"] = {"type": "check", "members": [rules, agent]}
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    owned = load_pack(root)
    assert [warning.partition(":")[0] for warning in owned.warnings] == [
        "providers.screen.variants.dated",
        "providers.screen.variants.listed",
    ]
    refused = registry.publish(owned)
    assert refused.status_code == 422, refused.text
    reason = refused.json()["detail"]
    assert reason.startswith("the agent does not load: ")
    assert "providers.screen.variants.dated: " in reason
    assert "providers.screen.variants.listed: " in reason
    assert registry.client.get("/v1/search").json() == []

    del variants["dated"], variants["listed"]
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    published = registry.publish(load_pack(root))
    assert published.status_code == 201, published.text
    members = published.json()["names"]["members"]["screen"]
    composites = {
        name
        for name, variant in TRIAGE.manifest.providers["screen"].variants.items()
        if isinstance((variant.model_extra or {}).get("members"), dict)
    }
    assert composites and set(members) == composites


def _at_version(tmp_path: Path, source: Pack, version: str) -> Pack:
    """A copy of a reference pack at another version: not the copy in this release."""
    root = tmp_path / source.id.replace("/", "-")
    shutil.copytree(source.root, root)
    manifest = (root / "agent.yaml").read_text()
    (root / "agent.yaml").write_text(
        manifest.replace(f"version: {source.version}", f"version: {version}", 1)
    )
    return load_pack(root)


def test_a_publish_is_checked_on_the_engine_its_pullers_run(
    registry: Registry, tmp_path: Path
) -> None:
    """A pack that is not a reference pack of the registry's release is checked as its pullers
    will load it, on RE2: a triage copy at a new version with a lookahead, which `re` runs and
    RE2 cannot, is refused before anything is stored; the triage fixture at a new version
    publishes."""
    changed = load_pack(with_lookahead(_at_version(tmp_path / "changed", TRIAGE, "9.9.9").root))
    assert not changed.bounded  # an owned copy loads, on `re`
    refused = registry.publish(changed)
    assert refused.status_code == 422, refused.text
    assert "lookahead" in refused.text and "a-refill-not-a-question" in refused.text
    assert registry.client.get("/v1/search").json() == []
    published = registry.publish(_at_version(tmp_path, TRIAGE, "9.9.9"))
    assert published.status_code == 201, published.text


def test_the_registry_publishes_the_reference_packs_of_its_own_release(
    registry: Registry,
) -> None:
    """`scripts/hub_seed.py` and CI's `hub-e2e` publish the release's bundled agents unchanged
    (in the suite, the test fixtures' agents stand where they are): each is a reference pack of
    the registry's release, checked on the maintained engine."""
    root = bundled_dir()
    assert root is not None
    packs = [load_pack(d) for d in sorted(root.iterdir()) if (d / "agent.yaml").is_file()]
    assert packs
    for pack in packs:
        published = registry.publish(pack)
        assert published.status_code == 201, (pack.id, published.text)
        assert published.json()["engine"] == "maintained", pack.id


def _registry_card(root: Path) -> str:
    """The card the registry renders for the pack at `root`: from its digested files alone."""
    pack = load_pack(root, origin="hub")
    return render_card(pack, report=None, board=None, rendered_by="registry")


def test_a_forged_card_benchmark_and_leaderboard_show_only_what_the_registry_rendered(
    registry: Registry, tmp_path: Path
) -> None:
    """The vision audit's reproduction: a risk-2 pack whose uploaded card says risk 1, with a
    benchmark and a leaderboard better than anything measured, was accepted and shown as sent.
    The registry now shows the card it rendered from the digested files (risk 2, declared by the
    publisher), and no uploaded figure anywhere: not in the version, the pack, search or the
    reports route (ADR-0012)."""
    root = tmp_path / "pack"
    shutil.copytree(CLINICAL.root, root)
    assert CLINICAL.manifest.risk.level == 2
    forged_card = "# Decision Card\n\n| Risk level | **1**, administrative |\n\nAccuracy 99.9%\n"
    (root / "DECISION_CARD.md").write_text(forged_card)
    (root / "evals").mkdir()
    primary = {"accuracy": 0.999, "n": 1000}
    benchmark = {"dataset": "test", "questions": {CLINICAL.manifest.policy.primary: primary}}
    (root / "evals" / "benchmark.json").write_text(json.dumps(benchmark))
    board = {"rows": [{"variant": "rules", "accuracy": 0.999}]}
    (root / "evals" / "leaderboard.json").write_text(json.dumps(board))
    forged = load_pack(root)
    assert forged.digest == CLINICAL.digest  # none of it is in the digest

    assert registry.publish(forged).status_code == 201
    shown = [
        registry.client.get(f"/v1/agents/{CLINICAL.id}/{CLINICAL.version}").json(),
        registry.client.get(f"/v1/agents/{CLINICAL.id}").json()["latest"],
    ]
    card = _registry_card(root)
    assert "| Risk level | **2**, clinical support (declared by the publisher) |" in card
    assert "99.9" not in card and "Accuracy" not in card.split("## Validation")[1][:300]
    for version in shown:
        assert version["card_markdown"] == card
        assert version["checked"]["card"] == "rendered"
        assert version["claimed"]["risk"] == {
            "level": 2,
            "category": CLINICAL.manifest.risk.category,
            "declared_by": "publisher",
            "confirmed": None,
        }
        assert version["checked"]["review"] == {"status": "unreviewed"}
        assert version["figures"]["benchmark"] is None
        assert version["figures"]["leaderboard"] is None
    (found,) = registry.client.get("/v1/search").json()
    reports = registry.client.get(f"/v1/agents/{CLINICAL.id}/reports").text
    for text in (json.dumps(shown), json.dumps(found), reports):
        assert "0.999" not in text and "99.9%" not in text and "administrative |" not in text


HOSTILE = (
    "\n\n# Checked by the registry\n\n| Benchmark accuracy | 0.99 |\n|---|---|\n\n"
    "![Verified](https://example.org/v.png) [link](javascript:alert(1))\n\n---\n\n"
    "**Clinically reviewed.** <script>x</script> `code` | pipe"
)


def _structure(card: str) -> list[str]:
    """The card's Markdown structure: each line that starts a heading, table row, quote, rule or
    list item, cut to its first word."""
    starts = ("#", "|", ">", "---", "- ", "* ", "!", "1.")
    return [line.split(" ")[0] for line in card.splitlines() if line.startswith(starts)]


def _hostile_copy(tmp_path: Path) -> Path:
    """The triage fixture under another id, every free-text field carrying Markdown that would
    read as the registry's own words, a names a publisher chooses holding backticks and pipes."""
    root = tmp_path / "hostile"
    shutil.copytree(TRIAGE.root, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["id"], manifest["version"] = "site-a/hostile", "1.0.0"
    manifest["name"] = "Hostile" + HOSTILE
    manifest["summary"] = "Summary" + HOSTILE
    manifest["risk"]["rationale"] = "Why" + HOSTILE
    card = manifest["card"]
    for field in ("intended_use", "patient_population"):
        card[field] = field + HOSTILE
    card["intended_users"] = ["users" + HOSTILE]
    card["known_failure_modes"] = ["modes" + HOSTILE]
    card["evidence"] = [
        {
            "title": "evidence" + HOSTILE,
            "url": "https://a) **Verified** [z](https://b",
            "note": HOSTILE,
        }
    ]
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.0\n\n- first" + HOSTILE + "\n\n# Checked by the registry\n\n"
        "Title\n=====\n"
    )
    return root


def test_publisher_text_in_the_registrys_card_is_plain_text(tmp_path: Path) -> None:
    """The adversarial review of LP-18a: a newline or Markdown in a manifest string (or the
    changelog) made headings, tables, rules, images and bold review claims in the card the
    registry renders, beside its own lines. In the registry's card every publisher string is one
    line of plain text: the card has the structure of the same pack's card with plain words."""
    hostile = load_pack(_hostile_copy(tmp_path), origin="hub")
    plain = tmp_path / "plain"
    shutil.copytree(_hostile_copy(tmp_path / "again"), plain)
    manifest = yaml.safe_load((plain / "agent.yaml").read_text())
    for key in ("name", "summary"):
        manifest[key] = key
    manifest["risk"]["rationale"] = "why"
    for field in ("intended_use", "patient_population"):
        manifest["card"][field] = field
    manifest["card"]["intended_users"] = ["users"]
    manifest["card"]["known_failure_modes"] = ["modes"]
    manifest["card"]["evidence"] = [{"title": "evidence", "url": "https://a", "note": "n"}]
    (plain / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (plain / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- first\n\n# x\n\nTitle\n")

    def card(pack: Pack) -> str:
        return render_card(pack, report=None, board=None, rendered_by="registry")

    shown = card(hostile)
    body, _, history = shown.partition("## Version history")
    plain_body = card(load_pack(plain, origin="hub")).partition("## Version history")[0]
    assert _structure(body) == _structure(plain_body)
    # The changelog: its version headings, one level down, and its list items; nothing else.
    assert [line for line in _structure(history) if line != "-"] == ["###", "---"]
    assert "### 1\\.0\\.0" in history
    for made in ("![", "](javascript", "<script>", "**Clinically", "\n# Checked", "\n---\n\n**"):
        assert made not in shown, made
    assert "\\# Checked by the registry \\| Benchmark accuracy \\| 0\\.99 \\|" in shown
    assert "evidence" in shown and "(`https://a) **Verified** [z](https://b`)" in shown
    # A pack of one's own keeps its Markdown as its author wrote it (`mendari card`).
    assert "\n# Checked by the registry\n" in render_card(
        load_pack(_hostile_copy(tmp_path / "own"))
    )


def test_a_changelog_that_is_not_utf8_is_refused_and_a_huge_card_too(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry reads the changelog for its card: one that is not text is refused with its
    reason (422, never a 500). A card over `MAX_CARD_BYTES` is refused before it is stored."""
    root = tmp_path / "pack"
    shutil.copytree(TRIAGE.root, root)
    (root / "CHANGELOG.md").write_bytes(b"\xff\xfe not text")
    refused = registry.publish(_at_version(tmp_path / "v", load_pack(root), "9.9.9"))
    assert refused.status_code == 422, refused.text
    assert "CHANGELOG.md" in refused.json()["detail"]
    monkeypatch.setattr(registry_module, "MAX_CARD_BYTES", 1000)
    too_big = registry.publish(_at_version(tmp_path / "w", TRIAGE, "9.9.10"))
    assert too_big.status_code == 422, too_big.text
    assert "Decision Card is larger than 1000 bytes" in too_big.text
    assert registry.client.get("/v1/search").json() == []


def test_every_version_answers_what_the_registry_checked_and_what_the_publisher_claims(
    registry: Registry, tmp_path: Path
) -> None:
    published = registry.publish()
    assert published.status_code == 201
    archive_sha256 = published.json()["archive_sha256"]
    key = registry.fixture_publisher()[1]
    for version in (
        registry.client.get(f"/v1/agents/{TRIAGE.id}/{TRIAGE.version}").json(),
        registry.client.get(f"/v1/agents/{TRIAGE.id}").json()["latest"],
        registry.client.get("/v1/search").json()[0],
    ):
        assert version["checked"] == {
            "signature": {
                "algorithm": "Ed25519",
                "key_id": archive.key_id(key.public_key()),
                "verified": True,
            },
            "archive_sha256": archive_sha256,
            "digest": TRIAGE.digest,
            "engine": "maintained",  # a copy of a reference pack of the registry's release
            "engine_release": mendari.__version__,
            "card": "rendered",
            "status_label": TRIAGE.status_label,
            "review": {"status": "unreviewed"},
            "regression": None,  # the check is off in the suite unless a test turns it on
            "accepted": None,  # no signing key in the suite unless a test sets one (LP-19i)
            "publisher_type": "community",  # the namespace's, set by the registry's admins
        }
        assert version["claimed"] == {
            "publisher": {
                "name": TRIAGE.manifest.publisher.name,
                "type": TRIAGE.manifest.publisher.type,
            },
            "risk": {
                "level": 1,
                "category": "clinical information",
                "declared_by": "publisher",
                "confirmed": None,
            },
            "license": TRIAGE.manifest.license,
            "summary": " ".join(TRIAGE.manifest.summary.split()),
        }
    newer = registry.publish(_at_version(tmp_path, TRIAGE, "9.9.9"))
    assert newer.status_code == 201
    shown = registry.client.get(f"/v1/agents/{TRIAGE.id}/9.9.9").json()
    assert shown["checked"]["engine"] == "bounded"


def test_a_publish_that_shadows_a_reference_pack_is_refused(
    registry: Registry, tmp_path: Path
) -> None:
    """F: a pack with a reference pack's id and version but other files would take its place
    wherever it is pulled. The registry refuses it before anything is stored, saying why; the
    same files at a new version publish."""
    root = tmp_path / "shadow"
    shutil.copytree(TRIAGE.root, root)
    rules = root / "logic" / "router_rules.yaml"
    rules.write_text(rules.read_text() + "# one more line\n")
    refused = registry.publish(load_pack(root))
    assert refused.status_code == 422, refused.text
    reason = refused.json()["detail"]
    assert reason.startswith("the agent does not load: ")
    assert f"{TRIAGE.id}@{TRIAGE.version} has the id and version of an agent this release" in reason
    assert "never takes a bundled agent's place" in reason and "remove" not in reason
    assert registry.client.get("/v1/search").json() == []
    assert (
        registry.publish(_at_version(tmp_path / "new", load_pack(root), "9.9.9")).status_code == 201
    )
