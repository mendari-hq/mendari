# SPDX-License-Identifier: Apache-2.0
"""Site evidence (`mendari.site-evidence/1`, LP-22g, ADR-0017): what sites' signed reports say
about one agent, side by side, worked out on read from the reports the registry verified.

Per agent digest and configuration (and the site's consent to a remote arm), never across them:
two digests are two agents as far as evidence goes, and a remote configuration is never pooled
with a local one. For each pooled figure (owner decision 8 of the plan: the primary question's
accuracy, each critical label's detection, each critical label's cases automated, and false
automation):

- each site's k of n with its exact interval, first;
- then `evals.pooling`: the pooled fixed-effect figure from summed counts (so named), the
  DerSimonian-Laird random-effects estimate beside it (from 3 sites), the range of the sites'
  rates, and the homogeneity test (chi-square only when every expected count is 5 or more; the
  exact test or a seeded permutation test otherwise, its seed in the answer) with a warning in
  words when the sites differ by more than chance;
- the number of sites and the total cases pooled.

Each figure is tested alone at 0.05, and the answer says how many tests were run (owner
decision 11's default). Out of the pool, and listed with the reason: the publisher's own site
(its organization holds the agent's namespace, owner decision 2), a report signed with a key
later revoked as compromised (decision 9), and a superseded report. Beside the figures: how
many tickets for the group are unreported (open, or expired with no report), how many were
withdrawn, and how many reports were superseded.

Every number is computed here from counts the registry verified (`mendari.site-report/1`);
none is a rate a site sent. The pooling of a set of counts is cached, so a read never redoes
the exact or permutation test for counts it has seen.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from mendari.evals import pooling, stats
from mendari.evals.result import MIN_POSITIVES
from mendari.hub.sites import _iso, _status

if TYPE_CHECKING:
    from mendari.hub.registry import RegistryStore

FORMAT = "mendari.site-evidence/1"
WARNING = (
    "The sites disagree by more than chance (p = {p}): the pooled figure is not a summary of "
    "them. Read each site's figure."
)
NOTE = (
    "These are sites' own data, shared as signed counts. They are not part of any rank, and a "
    "ticket binds a dataset before its run, not how its cases were chosen."
)
LEFT_OUT = {
    "own": "the publisher's own site",
    "compromised": "signed with a key revoked as compromised",
    "superseded": "superseded by a correction",
}


def _figure(k: int, n: int) -> dict[str, Any]:
    if n == 0:
        return {"k": 0, "n": 0, "low": None, "high": None}
    low, high = stats.interval(k, n)
    return {"k": k, "n": n, "low": low, "high": high}


@lru_cache(maxsize=512)
def _pooled(sites: tuple[tuple[int, int], ...]) -> dict[str, Any]:
    """The pooled statistics of a set of site counts, cached: the same counts are never pooled
    twice by one process."""
    usable = [site for site in sites if site[1] > 0]
    homogeneity = pooling.homogeneity(usable)
    return {
        "sites_pooled": len(usable),
        "cases_pooled": sum(n for _, n in usable),
        "pooled": pooling.pooled_fixed(usable),
        "random_effects": pooling.random_effects(usable),
        "range": pooling.range_of(usable),
        "homogeneity": homogeneity,
        "warning": WARNING.format(p=homogeneity["p"])
        if homogeneity.get("status") == "ok" and homogeneity["differ"]
        else None,
    }


def _counts(report: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    """Each pooled figure of one report, as (k, n) from its counts (owner decision 8)."""
    found: dict[str, tuple[int, int]] = {}
    primary = report["questions"].get(report["primary"])
    if primary is not None:
        found[f"accuracy:{report['primary']}"] = (primary["right"], primary["n"])
        found["false_automation"] = (report["decisions"]["false_automation"], primary["n"])
    for item in report["decisions"]["critical"]:
        name = f"{item['question']}={item['label']}"
        found[f"detected:{name}"] = (item["detected"], item["n"])
        found[f"automated:{name}"] = (item["auto_missed"], item["n"])
    return found


def _label(key: str) -> str:
    kind, _, name = key.partition(":")
    return {
        "accuracy": f"accuracy of {name}",
        "false_automation": "automated with a wrong recommendation",
        "detected": f"critical {name} detected",
        "automated": f"critical {name} automated",
    }[kind]


def evidence(store: RegistryStore, namespace: str, name: str) -> dict[str, Any]:
    """`mendari.site-evidence/1` of one agent (see the module)."""
    reports = store.site_reports(namespace, name)
    records = {
        report["id"]: store.signed_report(namespace, name, report["id"])["record"]["report"]
        for report in reports
    }
    tickets = store.agent_ticket_rows(namespace, name)
    groups: dict[tuple[str, str, bool], dict[str, Any]] = {}

    def group_of(digest: str, configuration: Any, consent: bool, version: str) -> dict[str, Any]:
        key = (digest, json.dumps(configuration, sort_keys=True), consent)
        return groups.setdefault(
            key,
            {
                "version": version,
                "digest": digest,
                "configuration": configuration,
                "remote_consent": consent,
                "reports": [],
                "left_out": [],
                "unreported_tickets": 0,
                "withdrawn_tickets": 0,
                "superseded_reports": 0,
            },
        )

    for report in reports:
        group = group_of(
            report["digest"], report["configuration"], report["remote_consent"], report["version"]
        )
        reason = None
        if report["superseded_by"] is not None:
            reason = "superseded"
            group["superseded_reports"] += 1
        elif report["compromised_key"]:
            reason = "compromised"
        elif store.own_site(report["site"], namespace):
            reason = "own"
        entry = {"report": report["id"], "site": report["site"], "site_name": report["site_name"]}
        if reason is None:
            group["reports"].append(entry)
        else:
            group["left_out"].append({**entry, "reason": LEFT_OUT[reason]})
    now = _iso(datetime.now(UTC))
    for row in tickets:
        group = group_of(
            row["digest"],
            json.loads(row["configuration"]),
            bool(row["remote_consent"]),
            row["version"],
        )
        status = _status(row, now)
        if status in ("open", "expired"):
            group["unreported_tickets"] += 1
        elif status == "withdrawn":
            group["withdrawn_tickets"] += 1

    answer_groups = []
    for group in groups.values():
        pooled_reports = group.pop("reports")
        counted = {entry["report"]: _counts(records[entry["report"]]) for entry in pooled_reports}
        names = sorted({key for found in counted.values() for key in found})
        figures = []
        for key in names:
            per_site = [
                {
                    "report": entry["report"],
                    "site": entry["site"],
                    "site_name": entry["site_name"],
                    **_figure(*counted[entry["report"]][key]),
                    "insufficient_evidence": counted[entry["report"]][key][1]
                    < (MIN_POSITIVES if key.startswith(("detected", "automated")) else 1),
                }
                for entry in pooled_reports
                if key in counted[entry["report"]]
            ]
            sites = tuple((item["k"], item["n"]) for item in per_site)
            figures.append(
                {"figure": key, "label": _label(key), "sites": per_site, **_pooled(sites)}
            )
        answer_groups.append(
            {
                **group,
                "sites": len(pooled_reports),
                "cases": sum(records[e["report"]]["dataset"]["n"] for e in pooled_reports),
                "figures": figures,
                "tests_run": sum(1 for f in figures if f["homogeneity"].get("status") == "ok"),
            }
        )
    return {
        "format": FORMAT,
        "agent": f"{namespace}/{name}",
        "note": NOTE,
        "alpha": pooling.ALPHA,
        "groups": answer_groups,
    }


def figure_keys(groups: Sequence[Mapping[str, Any]]) -> list[str]:
    """The figures an answer's groups pool, in order (for readers that list them)."""
    return sorted({f["figure"] for group in groups for f in group["figures"]})
