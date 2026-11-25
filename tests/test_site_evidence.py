# SPDX-License-Identifier: Apache-2.0
"""Site evidence (LP-22g, ADR-0017): sites' signed reports of a pack, side by side, per digest
and configuration, each site's k of n before any pooled figure. The reports are real
evaluations of the triage fixture on its synthetic test set (`tests/support/fixture_agents.py`),
each run on a different number of cases so the sites differ, signed by synthetic sites."""

from __future__ import annotations

import copy
import json
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Decision, load_pack
from mendari.core.pack import Pack
from mendari.evals import pooling, resolve_dataset
from mendari.evals.harness import evaluation
from mendari.evals.share import shareable
from mendari.hub import site_evidence
from tests.conftest import (
    CLINICAL_DIR,
    FIXTURE_NAMESPACE,
    TRIAGE_DIR,
    Registry,
    share_signed,
    site_ticket,
    verified_site,
)

# The agent `Registry.publish()` publishes, and the question its figures are of.
TRIAGE = load_pack(TRIAGE_DIR)
ACCURACY = f"accuracy:{TRIAGE.manifest.policy.primary}"
EVIDENCE = f"/v1/agents/{TRIAGE.id}/site-evidence"


@lru_cache(maxsize=8)
def _counts(cases: int, root: str) -> str:
    """The shared counts of the agent at `root` run on the first `cases` of its test set."""
    pack = load_pack(root)
    dataset = resolve_dataset(pack, "test").head(cases)
    return json.dumps(shareable(evaluation(Decision(pack), dataset).ledger))


def counts(cases: int, pack: Pack = TRIAGE) -> dict[str, Any]:
    return dict(json.loads(_counts(cases, str(pack.root))))


@pytest.fixture
def published(registry: Registry) -> Registry:
    assert registry.publish().status_code == 201
    return registry


def _share(registry: Registry, tmp_path: Path, org: str, report: dict[str, Any]) -> str:
    site = verified_site(registry, tmp_path, org=org)
    answer = share_signed(registry, site, report)
    assert answer.status_code == 201, answer.text
    return str(site.id)


def _evidence(registry: Registry) -> dict[str, Any]:
    answer = registry.client.get(EVIDENCE)
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


def _figure(group: dict[str, Any], key: str) -> dict[str, Any]:
    return next(f for f in group["figures"] if f["figure"] == key)


def test_each_site_is_k_of_n_with_its_interval_before_any_pooled_figure(
    published: Registry, tmp_path: Path
) -> None:
    for org, cases in (("north", 300), ("south", 200), ("east", 120)):
        _share(published, tmp_path, org, counts(cases))
    answer = _evidence(published)
    assert answer["format"] == "mendari.site-evidence/1" and "not part of any" in answer["note"]
    (group,) = answer["groups"]
    assert (group["sites"], group["cases"]) == (3, 620)
    figure = _figure(group, ACCURACY)
    order = list(figure)
    assert order.index("sites") < order.index("pooled") < order.index("random_effects")
    assert (figure["sites_pooled"], figure["cases_pooled"]) == (3, 620)
    assert [site["n"] for site in figure["sites"]] == [300, 200, 120]
    for site in figure["sites"]:
        assert site["low"] <= site["k"] / site["n"] <= site["high"]


def test_the_pooled_figure_is_the_summed_counts_and_says_so(
    published: Registry, tmp_path: Path
) -> None:
    for org, cases in (("north", 300), ("south", 200)):
        _share(published, tmp_path, org, counts(cases))
    (group,) = _evidence(published)["groups"]
    figure = _figure(group, ACCURACY)
    pooled = figure["pooled"]
    assert pooled["method"] == "pooled (fixed effect, summed counts)"
    assert pooled["k"] == sum(site["k"] for site in figure["sites"])
    assert pooled["n"] == sum(site["n"] for site in figure["sites"]) == 500
    assert figure["random_effects"]["method"].startswith("random effects (DerSimonian-Laird")


def _at_version(tmp_path: Path, source: Path, version: str) -> Pack:
    """The agent at `source` at another version (a copy published by the same namespace):
    another digest."""
    root = tmp_path / f"{source.name}-{version}"
    shutil.copytree(source, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["version"] = version
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return load_pack(root)


def test_two_digests_or_two_configurations_are_never_pooled(
    published: Registry, tmp_path: Path
) -> None:
    report = counts(300)
    _share(published, tmp_path, "north", report)
    remote = copy.deepcopy(report)
    remote["remote_consent"] = True  # the same run with consent recorded: another group
    _share(published, tmp_path, "south", remote)
    groups = _evidence(published)["groups"]
    assert len(groups) == 2 and all(g["sites"] == 1 for g in groups)
    assert {g["remote_consent"] for g in groups} == {True, False}
    # Two versions of one pack: two digests, never pooled.
    clinical = load_pack(CLINICAL_DIR)
    newer = _at_version(tmp_path, CLINICAL_DIR, "9.9.9")
    assert published.publish(clinical).status_code == 201
    assert published.publish(newer).status_code == 201
    _share(published, tmp_path, "east", counts(120, clinical))
    _share(published, tmp_path, "west", counts(120, newer))
    answer = published.client.get(f"/v1/agents/{clinical.id}/site-evidence").json()
    assert sorted(g["digest"] for g in answer["groups"]) == sorted([clinical.digest, newer.digest])
    assert all(g["sites"] == 1 for g in answer["groups"])
    assert all(
        f["pooled"]["status"] == "insufficient evidence"
        for g in answer["groups"]
        for f in g["figures"]
    )


def test_sites_that_disagree_raise_the_homogeneity_warning_and_alike_sites_do_not(
    published: Registry, tmp_path: Path
) -> None:
    alike = counts(300)
    _share(published, tmp_path, "north", alike)
    _share(published, tmp_path, "south", copy.deepcopy(alike))
    (group,) = _evidence(published)["groups"]
    assert all(f["warning"] is None for f in group["figures"])
    assert group["tests_run"] == len(group["figures"])
    # A third site that missed most of its critical cases.
    missing = copy.deepcopy(alike)
    critical = missing["decisions"]["critical"][0]
    critical["detected"] = critical["n"] // 4
    _share(published, tmp_path, "east", missing)
    (group,) = _evidence(published)["groups"]
    name = f"detected:{critical['question']}={critical['label']}"
    figure = _figure(group, name)
    assert figure["homogeneity"]["differ"] is True and figure["homogeneity"]["p"] < 0.05
    assert figure["warning"] and "not a summary" in figure["warning"]
    assert figure["homogeneity"]["method"] != "Pearson chi-square" or (
        figure["homogeneity"]["smallest_expected"] >= 5
    )


def test_the_publishers_own_site_is_listed_and_left_out_of_the_pool(
    published: Registry, tmp_path: Path
) -> None:
    with published.operator._db.session(exclusive=True) as db:  # its namespace's claim
        db.all(
            "INSERT INTO hub_claims (namespace, org, claimed_by, created_at, status) "
            "VALUES (?, 'mendari', 'u', '2026-01-01T00:00:00+00:00', 'approved')",
            (FIXTURE_NAMESPACE,),
        )
    own = _share(published, tmp_path, "mendari", counts(300))  # a site of the same organization
    other = _share(published, tmp_path, "north", counts(200))
    (group,) = _evidence(published)["groups"]
    assert group["sites"] == 1
    assert [(e["site"], e["reason"]) for e in group["left_out"]] == [
        (own, "the publisher's own site")
    ]
    assert [s["site"] for s in _figure(group, ACCURACY)["sites"]] == [other]


def test_unreported_tickets_and_superseded_reports_are_counted_beside_the_figures(
    published: Registry, tmp_path: Path
) -> None:
    report = counts(300)
    site = verified_site(published, tmp_path, org="north")
    assert share_signed(published, site, report).status_code == 201
    first = published.client.get(f"/v1/agents/{TRIAGE.id}/reports").json()["reports"][0]
    corrected = site_ticket(published, site, report, supersedes=first["id"])
    assert share_signed(published, site, report, corrected).status_code == 201
    site_ticket(published, site, report, commitment=site.commitment(b"never reported"))
    (group,) = _evidence(published)["groups"]
    assert (group["unreported_tickets"], group["superseded_reports"]) == (1, 1)
    assert [e["reason"] for e in group["left_out"]] == ["superseded by a correction"]
    assert group["sites"] == 1


def test_reports_under_a_compromised_key_leave_the_pool(
    published: Registry, tmp_path: Path
) -> None:
    site = verified_site(published, tmp_path, org="north")
    assert share_signed(published, site, counts(300)).status_code == 201
    published.operator.revoke_site_key(
        site.key_id, org=None, by="o", reason="lost", compromised=True
    )
    (group,) = _evidence(published)["groups"]
    assert group["sites"] == 0
    assert [e["reason"] for e in group["left_out"]] == ["signed with a key revoked as compromised"]


def test_too_few_sites_or_cases_read_insufficient_evidence(
    published: Registry, tmp_path: Path
) -> None:
    _share(published, tmp_path, "north", counts(300))
    (group,) = _evidence(published)["groups"]
    figure = _figure(group, ACCURACY)
    assert figure["pooled"]["status"] == "insufficient evidence"
    assert figure["homogeneity"]["status"] == "insufficient evidence"
    _share(published, tmp_path, "south", counts(200))
    (group,) = _evidence(published)["groups"]
    figure = _figure(group, ACCURACY)
    assert figure["pooled"]["status"] == "ok"
    assert figure["random_effects"]["status"] == "insufficient evidence"  # from 3 sites
    critical = next(f for f in group["figures"] if f["figure"].startswith("detected:"))
    assert all(site["insufficient_evidence"] == (site["n"] < 100) for site in critical["sites"])


def test_two_reads_give_the_same_bytes_and_the_seed_is_in_the_answer(
    published: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pooling, "MAX_TABLES", 1)  # every small-count test is resampled
    site_evidence._pooled.cache_clear()
    for org, cases in (("north", 300), ("south", 200), ("east", 120)):
        report = counts(cases)
        critical = report["decisions"]["critical"][0]
        critical["detected"] = critical["n"] - 1  # one miss each: fewer than five expected
        _share(published, tmp_path, org, report)
    first = published.client.get(EVIDENCE).content
    site_evidence._pooled.cache_clear()
    assert published.client.get(EVIDENCE).content == first
    (group,) = json.loads(first)["groups"]
    permuted = [
        f["homogeneity"]
        for f in group["figures"]
        if f["homogeneity"].get("method") == pooling.PERMUTATION
    ]
    assert permuted and all(isinstance(h["seed"], int) and h["resamples"] for h in permuted)
    site_evidence._pooled.cache_clear()


def test_no_rate_in_the_answer_came_from_a_site(published: Registry, tmp_path: Path) -> None:
    sent = {org: counts(cases) for org, cases in (("north", 300), ("south", 200))}
    ids = {org: _share(published, tmp_path, org, report) for org, report in sent.items()}
    (group,) = _evidence(published)["groups"]
    by_site = {site_id: sent[org] for org, site_id in ids.items()}
    for figure in group["figures"]:
        for site in figure["sites"]:
            report = by_site[site["site"]]
            kind, _, name = figure["figure"].partition(":")
            primary = report["questions"][report["primary"]]
            critical = {f"{c['question']}={c['label']}": c for c in report["decisions"]["critical"]}
            expected = {
                "accuracy": (primary["right"], primary["n"]),
                "false_automation": (report["decisions"]["false_automation"], primary["n"]),
                "detected": (
                    critical.get(name, {}).get("detected"),
                    critical.get(name, {}).get("n"),
                ),
                "automated": (
                    critical.get(name, {}).get("auto_missed"),
                    critical.get(name, {}).get("n"),
                ),
            }[kind]
            assert (site["k"], site["n"]) == expected
    for report in sent.values():  # and what was sent held no rate to take
        assert "accuracy" not in json.dumps(report) and "rate" not in json.dumps(report)


def test_site_evidence_of_a_pack_nobody_may_read_is_a_404(published: Registry) -> None:
    assert published.client.get("/v1/agents/mendari/no-such/site-evidence").status_code == 404
