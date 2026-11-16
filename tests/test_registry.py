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
from mendari.core.registry import resolve_pack
from mendari.evals import figures as figures_module
from mendari.evals import stats
from mendari.hub import archive
from mendari.hub import registry as registry_module
from mendari.hub.registry import RegistryStore
from tests.conftest import (
    INBOX_DIR,
    Registry,
    share_signed,
    shared_benchmark,
    site_ticket,
    verified_site,
)

REFERRAL = resolve_pack("mendari/referral-router")
GUARD = resolve_pack("mendari/ai-guard")
INBOX = load_pack(INBOX_DIR)


def test_a_signed_pack_is_published_once_and_read_back(registry: Registry) -> None:
    published = registry.publish()
    assert published.status_code == 201, published.text
    body = published.json()
    assert (body["id"], body["version"], body["digest"]) == (
        "mendari/referral-router",
        REFERRAL.version,
        REFERRAL.digest,
    )
    assert body["risk"] == {"level": 1, "category": "clinical information"}
    assert "CDS Hooks order-sign" in body["standards"] and "FHIR R4" in body["standards"]
    assert body["card_markdown"].startswith("# Decision Card: Referral Router")
    assert "benchmark" not in body and "leaderboard" not in body
    again = registry.publish()
    assert again.status_code == 409 and "immutable" in again.text
    pack = registry.client.get("/v1/packs/mendari/referral-router").json()
    assert pack["versions"] == [REFERRAL.version] and pack["latest"]["digest"] == REFERRAL.digest


def test_the_archive_downloads_byte_for_byte(registry: Registry) -> None:
    data = archive.build(REFERRAL, registry.key)
    registry.publish()
    response = registry.client.get(f"/v1/packs/mendari/referral-router/{REFERRAL.version}/archive")
    assert response.content == data
    assert response.headers["x-archive-sha256"] == hashlib.sha256(data).hexdigest()


def test_publishing_needs_the_namespaces_token(registry: Registry) -> None:
    other_token = registry.namespace("other")
    assert registry.publish(token=other_token).status_code == 403
    assert registry.publish(token="ohr_made_up").status_code == 403


def test_an_archive_signed_by_an_unregistered_key_is_refused(registry: Registry) -> None:
    stranger = archive.load_private_key(archive.generate_key()[0])
    refused = registry.publish(key=stranger)
    assert refused.status_code == 422 and "not trusted" in refused.text


def test_the_archive_must_be_the_pack_and_version_in_the_url(registry: Registry) -> None:
    refused = registry.publish(version="9.9.9")
    assert refused.status_code == 422 and "not this pack and version" in refused.text


def test_verification_sets_the_publisher_type(registry: Registry) -> None:
    registry.publish()
    patched = registry.operator.set_publisher_type("mendari", "verified", by="operator")
    assert patched["publisher_type"] == "verified"
    pack = registry.client.get("/v1/packs/mendari/referral-router").json()
    assert pack["latest"]["publisher_type"] == "verified"
    namespace = registry.client.get("/v1/namespaces/mendari").json()
    assert namespace["packs"] == ["mendari/referral-router"]
    assert namespace["keys"][0]["key_id"] == archive.key_id(registry.key.public_key())
    found = registry.client.get("/v1/search", params={"publisher_type": "verified"}).json()
    assert [p["id"] for p in found] == ["mendari/referral-router"]


def test_search_by_words_risk_and_standard(registry: Registry) -> None:
    registry.publish()
    registry.publish(GUARD)

    def ids(**params: Any) -> list[str]:
        return [p["id"] for p in registry.client.get("/v1/search", params=params).json()]

    assert ids() == ["mendari/ai-guard", "mendari/referral-router"]
    assert ids(q="referral specialty") == ["mendari/referral-router"]
    assert ids(risk=2) == ["mendari/ai-guard"]
    assert ids(standard="cds hooks order-sign") == ["mendari/referral-router"]
    assert ids(q="nothing-like-this") == []
    assert "card_markdown" not in registry.client.get("/v1/search").json()[0]


def test_unknown_things_are_404(registry: Registry) -> None:
    assert registry.client.get("/v1/packs/mendari/none").status_code == 404
    assert registry.client.get("/v1/packs/mendari/referral-router/0.0.1").status_code == 404
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
    path = f"/v1/packs/mendari/referral-router/{REFERRAL.version}/reports"
    publisher = {"authorization": f"Bearer {registry.token}"}
    refused = registry.client.post(path, json={"report": report}, headers=publisher)
    assert refused.status_code == 401 and "mendari site init" in refused.text
    assert registry.client.post(path, json={"report": report}).status_code == 401
    site = verified_site(registry, tmp_path)
    shared = share_signed(registry, site, report)
    assert shared.status_code == 201, shared.text
    listed = registry.client.get("/v1/packs/mendari/referral-router/reports").json()
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
    (served,) = registry.client.get("/v1/packs/mendari/referral-router/reports").json()["reports"]
    figures = served["figures"]
    assert _rates(figures) == []
    assert figures == json.loads(json.dumps(figures_module.site(report)))
    question = report["questions"]["next_step"]
    accuracy = figures["questions"]["next_step"]["accuracy"]
    assert (accuracy["k"], accuracy["n"]) == (question["right"], question["n"])
    assert (accuracy["low"], accuracy["high"]) == pytest.approx(
        stats.clopper_pearson(question["right"], question["n"])
    )
    for item, counted in zip(figures["critical"], report["decisions"]["critical"], strict=True):
        assert (item["detected"]["k"], item["detected"]["n"]) == (counted["detected"], counted["n"])
        assert item["automated"]["k"] == counted["auto_missed"]


def test_a_site_report_of_the_old_format_is_refused_and_names_the_new_one(
    registry: Registry, db_target: str, tmp_path: Path
) -> None:
    registry.publish()
    site = verified_site(registry, tmp_path)
    issued = site_ticket(registry, site, shared_benchmark())
    old = {**shared_benchmark(), "format": "mendari.site-report/1"}
    answer = registry.client.post(
        f"/v1/packs/mendari/referral-router/{REFERRAL.version}/reports",
        json={"ticket": issued["ticket"]["id"], "report": old},
        headers={"authorization": f"Bearer {issued['submit_secret']}"},
    )
    assert answer.status_code == 422
    assert "mendari.site-report/2" in answer.json()["detail"]
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        assert db.all("SELECT id FROM hub_site_reports") == []


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
    assert "the pack does not load (RecursionError)" in refused.text
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
    shutil.copytree(REFERRAL.root, root)
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
    shutil.copytree(REFERRAL.root, root)
    manifest = (
        (root / "pack.yaml")
        .read_text()
        .replace(f"version: {REFERRAL.version}", f"version: '{huge}'", 1)
    )
    (root / "pack.yaml").write_text(manifest)
    signed = Pack(
        manifest=REFERRAL.manifest.model_copy(update={"version": huge}),
        root=root,
        digest=pack_digest(root),
        input_schema=REFERRAL.input_schema,
    )
    refused = registry.publish(signed)
    assert refused.status_code == 422, refused.text
    search = registry.client.get("/v1/search")
    assert search.status_code == 200
    assert [item["version"] for item in search.json()] == [REFERRAL.version]


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
    assert registry.publish(GUARD).status_code == 201
    root = tmp_path / "pack"
    shutil.copytree(REFERRAL.root, root)
    benchmark = json.loads((root / "evals" / "benchmark.json").read_text())
    benchmark["dataset"] = {"name": _nested(300)}
    primary = benchmark["questions"][REFERRAL.manifest.policy.primary]
    primary["n"], primary["accuracy"] = True, float("nan")
    (root / "evals" / "benchmark.json").write_text(json.dumps(benchmark))
    (root / "evals" / "leaderboard.json").write_text(json.dumps({"rows": _nested(300)}))
    answer = registry.publish(load_pack(root))
    assert answer.status_code == 201, answer.text
    assert "benchmark" not in answer.json() and "leaderboard" not in answer.json()
    assert registry.client.get("/v1/search").status_code == 200
    for path in ("", f"/{REFERRAL.version}"):
        assert registry.client.get(f"/v1/packs/{REFERRAL.id}{path}").status_code == 200


def test_metadata_that_cannot_be_served_is_refused_before_it_is_stored(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def describe(pack: Any, verified: Any) -> dict[str, Any]:
        return {"pack_name": "x", "deep": _nested(300)}

    monkeypatch.setattr(registry_module, "describe", describe)
    refused = registry.publish()
    assert refused.status_code == 422, refused.text
    assert "the pack's metadata cannot be served (PydanticSerializationError)" in refused.text
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
    composites' members named. On the inbox pack, whose patterns all run on RE2: a changed copy
    (at a new version: at its own it is refused, F) is checked on the engine for packs from a
    registry (ADR-0009)."""
    root = tmp_path / "pack"
    shutil.copytree(INBOX.root, root)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["version"] = "9.9.9"
    variants = manifest["providers"]["safety_screen"]["variants"]
    rules, jev = variants["rules"], variants["jev"]
    escalate = {"urgency": "urgent"}
    variants["dated"] = {
        "type": "any_of",
        "escalate": escalate,
        "members": {datetime.date(2024, 1, 1): rules, "jev": jev},
    }
    variants["listed"] = {"type": "any_of", "escalate": escalate, "members": [rules, rules]}
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    owned = load_pack(root)
    assert [warning.partition(":")[0] for warning in owned.warnings] == [
        "providers.safety_screen.variants.dated",
        "providers.safety_screen.variants.listed",
    ]
    refused = registry.publish(owned)
    assert refused.status_code == 422, refused.text
    reason = refused.json()["detail"]
    assert reason.startswith("the pack does not load: ")
    assert "providers.safety_screen.variants.dated: " in reason
    assert "providers.safety_screen.variants.listed: " in reason
    assert registry.client.get("/v1/search").json() == []

    del variants["dated"], variants["listed"]
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    published = registry.publish(load_pack(root))
    assert published.status_code == 201, published.text
    members = published.json()["names"]["members"]["safety_screen"]
    composites = {
        name
        for name, variant in INBOX.manifest.providers["safety_screen"].variants.items()
        if isinstance((variant.model_extra or {}).get("members"), dict)
    }
    assert composites and set(members) == composites


def _at_version(tmp_path: Path, source: Pack, version: str) -> Pack:
    """A copy of a reference pack at another version: not the copy in this release."""
    root = tmp_path / source.id.replace("/", "-")
    shutil.copytree(source.root, root)
    manifest = (root / "pack.yaml").read_text()
    (root / "pack.yaml").write_text(
        manifest.replace(f"version: {source.version}", f"version: {version}", 1)
    )
    return load_pack(root)


def test_a_publish_is_checked_on_the_engine_its_pullers_run(
    registry: Registry, tmp_path: Path
) -> None:
    """A pack that is not a reference pack of the registry's release is checked as its pullers
    will load it, on RE2: a referral pack at a new version, whose lookahead RE2 cannot run, is
    refused before anything is stored; an inbox pack at a new version publishes."""
    refused = registry.publish(_at_version(tmp_path, REFERRAL, "9.9.9"))
    assert refused.status_code == 422, refused.text
    assert "lookahead" in refused.text and "not the copy of it in this release" in refused.text
    assert registry.client.get("/v1/search").json() == []
    published = registry.publish(_at_version(tmp_path, INBOX, "9.9.9"))
    assert published.status_code == 201, published.text


def test_the_registry_publishes_the_reference_packs_of_its_own_release(
    registry: Registry,
) -> None:
    """`scripts/hub_seed.py` and CI's `hub-e2e` publish the five bundled packs unchanged: each is
    a reference pack of the registry's release, checked on the maintained engine."""
    root = bundled_dir()
    assert root is not None
    packs = [load_pack(d) for d in sorted(root.iterdir()) if (d / "pack.yaml").is_file()]
    assert len(packs) == 5
    for pack in packs:
        published = registry.publish(pack)
        assert published.status_code == 201, (pack.id, published.text)


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
    shutil.copytree(GUARD.root, root)
    assert GUARD.manifest.risk.level == 2
    forged_card = "# Decision Card\n\n| Risk level | **1**, administrative |\n\nAccuracy 99.9%\n"
    (root / "DECISION_CARD.md").write_text(forged_card)
    benchmark = json.loads((root / "evals" / "benchmark.json").read_text())
    primary = benchmark["questions"][GUARD.manifest.policy.primary]
    primary["accuracy"], primary["n"] = 0.999, 1000
    (root / "evals" / "benchmark.json").write_text(json.dumps(benchmark))
    board = json.loads((root / "evals" / "leaderboard.json").read_text())
    board["rows"][0]["accuracy"] = 0.999
    (root / "evals" / "leaderboard.json").write_text(json.dumps(board))
    forged = load_pack(root)
    assert forged.digest == GUARD.digest  # none of it is in the digest

    assert registry.publish(forged).status_code == 201
    shown = [
        registry.client.get(f"/v1/packs/{GUARD.id}/{GUARD.version}").json(),
        registry.client.get(f"/v1/packs/{GUARD.id}").json()["latest"],
    ]
    card = _registry_card(root)
    assert "| Risk level | **2**, clinical support (declared by the publisher) |" in card
    assert "99.9" not in card and "Accuracy" not in card.split("## Validation")[1][:300]
    for version in shown:
        assert version["card_markdown"] == card
        assert version["checked"]["card"] == "rendered"
        assert version["claimed"]["risk"] == {
            "level": 2,
            "category": GUARD.manifest.risk.category,
            "declared_by": "publisher",
            "confirmed": None,
        }
        assert version["checked"]["review"] == {"status": "unreviewed"}
        assert version["figures"]["benchmark"] is None
        assert version["figures"]["leaderboard"] is None
    (found,) = registry.client.get("/v1/search").json()
    reports = registry.client.get(f"/v1/packs/{GUARD.id}/reports").text
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
    """The inbox pack under another id, every free-text field carrying Markdown that would read
    as the registry's own words, a names a publisher chooses holding backticks and pipes."""
    root = tmp_path / "hostile"
    shutil.copytree(INBOX.root, root)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
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
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
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
    manifest = yaml.safe_load((plain / "pack.yaml").read_text())
    for key in ("name", "summary"):
        manifest[key] = key
    manifest["risk"]["rationale"] = "why"
    for field in ("intended_use", "patient_population"):
        manifest["card"][field] = field
    manifest["card"]["intended_users"] = ["users"]
    manifest["card"]["known_failure_modes"] = ["modes"]
    manifest["card"]["evidence"] = [{"title": "evidence", "url": "https://a", "note": "n"}]
    (plain / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
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
    shutil.copytree(INBOX.root, root)
    (root / "CHANGELOG.md").write_bytes(b"\xff\xfe not text")
    refused = registry.publish(_at_version(tmp_path / "v", load_pack(root), "9.9.9"))
    assert refused.status_code == 422, refused.text
    assert "CHANGELOG.md" in refused.json()["detail"]
    monkeypatch.setattr(registry_module, "MAX_CARD_BYTES", 1000)
    too_big = registry.publish(_at_version(tmp_path / "w", INBOX, "9.9.10"))
    assert too_big.status_code == 422, too_big.text
    assert "Decision Card is larger than 1000 bytes" in too_big.text
    assert registry.client.get("/v1/search").json() == []


def test_every_version_answers_what_the_registry_checked_and_what_the_publisher_claims(
    registry: Registry, tmp_path: Path
) -> None:
    published = registry.publish()
    assert published.status_code == 201
    archive_sha256 = published.json()["archive_sha256"]
    for version in (
        registry.client.get(f"/v1/packs/{REFERRAL.id}/{REFERRAL.version}").json(),
        registry.client.get(f"/v1/packs/{REFERRAL.id}").json()["latest"],
        registry.client.get("/v1/search").json()[0],
    ):
        assert version["checked"] == {
            "signature": {
                "algorithm": "Ed25519",
                "key_id": archive.key_id(registry.key.public_key()),
                "verified": True,
            },
            "archive_sha256": archive_sha256,
            "digest": REFERRAL.digest,
            "engine": "maintained",  # a copy of a reference pack of the registry's release
            "engine_release": mendari.__version__,
            "card": "rendered",
            "status_label": REFERRAL.status_label,
            "review": {"status": "unreviewed"},
            "regression": None,  # the check is off in the suite unless a test turns it on
            "accepted": None,  # no signing key in the suite unless a test sets one (LP-19i)
            "publisher_type": "community",  # the namespace's, set by the registry's admins
        }
        assert version["claimed"] == {
            "publisher": {
                "name": REFERRAL.manifest.publisher.name,
                "type": REFERRAL.manifest.publisher.type,
            },
            "risk": {
                "level": 1,
                "category": "clinical information",
                "declared_by": "publisher",
                "confirmed": None,
            },
            "license": REFERRAL.manifest.license,
            "summary": " ".join(REFERRAL.manifest.summary.split()),
        }
    inbox = registry.publish(_at_version(tmp_path, INBOX, "9.9.9"))
    assert inbox.status_code == 201
    shown = registry.client.get(f"/v1/packs/{INBOX.id}/9.9.9").json()
    assert shown["checked"]["engine"] == "bounded"


def test_a_version_published_before_the_registry_rendered_cards_serves_nothing_uploaded(
    registry: Registry, db_target: str
) -> None:
    """Versions are immutable, so one stored before LP-18 keeps its metadata, but the card,
    benchmark and leaderboard it stored as the pack carried them are never served (ADR-0012)."""
    assert registry.publish().status_code == 201
    with RegistryStore(db_target) as store, store._db.session(exclusive=True) as db:
        row = db.one("SELECT metadata FROM hub_versions")
        assert row is not None
        stored = json.loads(row["metadata"])
        del stored["format"], stored["engine"]
        stored["card_markdown"] = "# Uploaded card, risk 0"
        stored["benchmark"] = {"dataset": "x", "n": 1000, "accuracy": 0.999}
        stored["leaderboard"] = {"rows": [{"accuracy": 0.999}]}
        db.all("UPDATE hub_versions SET metadata = ?", (json.dumps(stored),))
    for version in (
        registry.client.get(f"/v1/packs/{REFERRAL.id}/{REFERRAL.version}").json(),
        registry.client.get(f"/v1/packs/{REFERRAL.id}").json()["latest"],
        registry.client.get("/v1/search").json()[0],
    ):
        assert version.get("card_markdown") is None
        assert "benchmark" not in version and "leaderboard" not in version
        assert version["checked"]["card"] == "not_rendered"
        assert version["checked"]["engine"] is None
        assert version["figures"]["benchmark"] is None
        assert "0.999" not in json.dumps(version) and "Uploaded card" not in json.dumps(version)


def test_a_publish_that_shadows_a_reference_pack_is_refused(
    registry: Registry, tmp_path: Path
) -> None:
    """F: a pack with a reference pack's id and version but other files would take its place
    wherever it is pulled. The registry refuses it before anything is stored, saying why; the
    same files at a new version publish."""
    root = tmp_path / "shadow"
    shutil.copytree(INBOX.root, root)
    rules = root / "logic" / "routing_rules.yaml"
    rules.write_text(rules.read_text() + "# one more line\n")
    refused = registry.publish(load_pack(root))
    assert refused.status_code == 422, refused.text
    reason = refused.json()["detail"]
    assert reason.startswith("the pack does not load: ")
    assert f"{INBOX.id}@{INBOX.version} has the id and version of a reference pack" in reason
    assert "never takes a reference pack's place" in reason and "remove" not in reason
    assert registry.client.get("/v1/search").json() == []
    assert (
        registry.publish(_at_version(tmp_path / "new", load_pack(root), "9.9.9")).status_code == 201
    )
