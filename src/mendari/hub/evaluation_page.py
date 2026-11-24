# SPDX-License-Identifier: Apache-2.0
"""An agent's public evaluation (`mendari.agent-evaluation/1`): what the Agent Hub's evaluation
page shows and what the badge says (`GET /v1/agents/{namespace}/{name}/evaluation`, and
`.../badge.svg`).

It is worked out on every read, from the publisher's signed evaluations of one version
(`hub/evaluations.py`) and from nothing else. Nothing here is stored, and nothing is a number a
publisher sent as a rate: every share is k of n with its exact interval, from the counts.

**Only what a signed record states.** Each stored evaluation is checked again as it is read
(`evaluations.checked`). One that verifies, signed with a key that is still the namespace's, is
shown with its figures. One that does not verify, or whose key was revoked since, is listed in
`not_shown` with why and with nothing its record says, and takes no part in a figure, the
comparison or the badge. An agent whose evaluations are all such reads `not_verified`, never
`evaluated`.

**The version.** The newest version that is not recalled, or the one asked for (`version`).
Evaluations belong to a version: a new version starts with none, and says so (`versions.evaluated`
names the others that have one).

**The comparison.** The evaluations of one dataset are the same cases, so they can be set side
by side. They are listed in the order of `mendari.rank/1` (`evals/ranking.py`), without its
cost, which is a publisher's own figure: fewer critical cases automated, then the worst critical
label's detection, then false automation, then accuracy, and never accuracy alone. `comparison`
says which is first and what decided it against the second, with the two figures, and whether
their intervals keep apart (`separated`): when they overlap, the difference could be chance and
the answer says so. As in `mendari.rank/1`, nothing is named first while no critical label has
100 positive cases. It compares the configurations of one agent on one dataset its publisher
chose: it ranks no agent against another and is not a grade.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from mendari.core.https_only import is_https_or_local
from mendari.core.registry import version_key
from mendari.core.status import SYNTHETIC_EXAMPLE
from mendari.errors import HubError, MendariError
from mendari.evals import figures as served_figures
from mendari.evals import ranking
from mendari.hub import badge, evaluations

if TYPE_CHECKING:
    from mendari.hub.registry import RegistryStore, Viewer

FORMAT = "mendari.agent-evaluation/1"
NOTE = (
    "The publisher's own evaluation, on a dataset it chose and published: a configuration can "
    "be tuned to it. It compares the configurations of one agent. It ranks no agent against "
    "another, and it is not evidence of how the agent does on a site's own cases."
)
# How a comparison reads (`comparison.status`).
ORDERED = "ordered"  # one configuration is first, by `decided_by`
TIED = "tied"  # the first ones did the same on every measure
ONE = "one_configuration"
NO_CRITICAL_LABEL = "no_critical_label"  # the agent names none: nothing is named first
INSUFFICIENT = "insufficient_evidence"  # no critical label has `MIN_POSITIVES` positives
# Why a stored evaluation is not shown (`not_shown.reason`): `evaluations.checked`'s reasons,
# and a key revoked since.
KEY_REVOKED = "key_revoked"

# The public address of the Agent Hub that shows this registry (the site built from `hub/`),
# for the link the registry gives to an agent's evaluation page. Unset: it gives none.
HUB_URL_ENV = "MENDARI_REGISTRY_HUB_URL"


def hub_url() -> str | None:
    """`MENDARI_REGISTRY_HUB_URL` without a trailing slash, or None when unset. It is printed
    and linked, so it is an address a person can open: https (http on this machine), with no
    user, password, query or fragment. Anything else stops the registry, naming the setting
    and never the value."""
    raw = os.environ.get(HUB_URL_ENV, "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        odd = bool(parts.query or parts.fragment) or any(c.isspace() for c in raw)
    except ValueError:
        odd = True
    if odd or not is_https_or_local(raw):
        raise MendariError(
            f"{HUB_URL_ENV} is the Agent Hub's public address: https://host[/path] (http only "
            "on this machine), with no user, password, query or fragment"
        )
    return raw.rstrip("/")


def page_url(namespace: str, name: str) -> str | None:
    """Where the Agent Hub shows an agent's evaluation, or None when the registry names no
    Hub."""
    base = hub_url()
    if base is None:
        return None
    return f"{base}/agents/{quote(namespace, safe='')}/{quote(name, safe='')}/evaluation"


def configuration_label(configuration: Mapping[str, str]) -> str:
    """`slot=variant` pairs by slot, as the command line writes a configuration."""
    return ", ".join(f"{slot}={variant}" for slot, variant in sorted(configuration.items()))


def evaluation(
    store: RegistryStore,
    namespace: str,
    name: str,
    viewer: Viewer,
    *,
    version: str | None = None,
) -> dict[str, Any]:
    """`mendari.agent-evaluation/1` for an agent the viewer reads (a `HubError` 404 otherwise,
    as for an agent that does not exist): the version shown, its datasets with their
    configurations compared, what is not shown and why, and the badge's words."""
    versions = store.versions(namespace, name, viewer)
    if not versions:
        raise HubError(f"no agent {namespace}/{name}", 404)
    current = [v for v in versions if v["yank"] is None and v["deleted"] is None]
    if version is None:
        shown = current[-1] if current else None
    else:
        shown = next((v for v in versions if v["version"] == version), None)
        if shown is None:
            raise HubError(f"no agent {namespace}/{name}@{version}", 404)
    publisher = store.namespace(namespace, viewer)
    evaluated = store.evaluated_versions(namespace, name)
    answer: dict[str, Any] = {
        "format": FORMAT,
        "id": f"{namespace}/{name}",
        "note": NOTE,
        "publisher": {
            "namespace": namespace,
            "display_name": publisher["display_name"],
            "publisher_type": publisher["publisher_type"],
        },
        "versions": {
            "shown": None if shown is None else shown["version"],
            "latest": current[-1]["version"] if current else None,
            # The versions that hold an evaluation and are not recalled, the newest first.
            "evaluated": sorted(
                (v["version"] for v in current if v["version"] in evaluated),
                key=version_key,
                reverse=True,
            ),
        },
        "agent": None,
        "datasets": [],
        "not_shown": [],
        "superseded": 0,
        "links": {"page": page_url(namespace, name)},
    }
    if shown is None or shown not in current:  # every version recalled, or this one is
        answer["state"] = badge.RECALLED
        answer["badge"] = _badge(badge.RECALLED, None)
        return answer
    answer["agent"] = {
        "name": shown.get("pack_name"),
        "version": shown["version"],
        "digest": shown.get("digest"),
        "published_at": shown.get("published_at"),
        # Derived at publish from the signed archive's manifest (`core/status.py`).
        "status_label": shown.get("status_label"),
        "synthetic": shown.get("status_label") == SYNTHETIC_EXAMPLE,
        "primary": shown.get("primary"),
    }
    rows, superseded = store.live_evaluations(namespace, name, shown["version"])
    keys = store.evaluation_keys(namespace)
    revoked = store.revoked_keys(namespace)
    verified: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for row in rows:
        record, problem = evaluations.checked(row, keys)
        if record is None or str(row["key_id"]) in revoked:
            answer["not_shown"].append(
                {
                    # The row's own names, never what an unverified record says of itself.
                    "id": row["id"],
                    "configuration": configuration_label(evaluations.held_configuration(row)),
                    "dataset": row["dataset"],
                    "key_id": row["key_id"],
                    "reason": problem if record is None else KEY_REVOKED,
                }
            )
        else:
            verified.append((row, record))
    answer["superseded"] = superseded
    answer["datasets"] = _datasets(verified, str(shown.get("digest") or ""))
    # Evaluated only by what is shown: evaluations that are there and show nothing are not.
    state = badge.EVALUATED if answer["datasets"] else badge.NOT_VERIFIED if rows else badge.NONE
    answer["state"] = state
    answer["badge"] = _badge(state, totals(answer["datasets"], answer["agent"]["synthetic"]))
    return answer


def _badge(state: str, of: Mapping[str, Any] | None) -> dict[str, Any]:
    words, tone = badge.message(state, of)
    return {"state": state, "label": badge.LABEL, "message": words, "tone": tone}


def _datasets(
    verified: list[tuple[Mapping[str, Any], dict[str, Any]]], digest: str
) -> list[dict[str, Any]]:
    """The verified evaluations by the dataset they ran on (its name, SHA-256 and size), each
    dataset's configurations in the comparison's order."""
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row, record in verified:
        shown = _shown(row, record)
        if shown is None:
            continue
        dataset = record["dataset"]
        groups.setdefault((dataset["name"], dataset["sha256"], dataset["n"]), []).append(shown)
    datasets = []
    for (name, sha256, n), members in sorted(groups.items()):
        # Each record names the version's digest (checked when it was published).
        ordered, compared = compare(members, digest or members[0]["row"]["pack_digest"])
        stated = [m["dataset"] for m in members]
        provenance = {d.get("provenance") for d in stated} - {None}
        datasets.append(
            {
                "name": name,
                "sha256": sha256,
                "n": n,
                # What the signed records state of it (`mendari.publisher-evaluation/2`); null:
                # none states it. Who labelled it is each publisher's own word, once each.
                "provenance": next(iter(provenance)) if len(provenance) == 1 else None,
                "labelled_by": sorted({d["labelled_by"] for d in stated if d.get("labelled_by")}),
                "critical": [
                    {"question": c["question"], "label": c["label"], "n": c["detected"]["n"]}
                    for c in ordered[0]["figures"]["critical"]
                ],
                "comparison": compared,
                "configurations": [
                    {key: value for key, value in member.items() if key not in ("dataset", "row")}
                    for member in ordered
                ],
            }
        )
    return datasets


def _shown(row: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any] | None:
    """One verified evaluation as the page shows it; None when its counts give no figures (a
    record no format lets through, so never a failed page)."""
    report = record["report"]
    worked = served_figures.guarded(lambda: evaluations.figures(report))
    if worked is None:
        return None
    primary = report["questions"].get(report["primary"])
    decisions = report["decisions"]
    critical = decisions["critical"]
    answered = primary["n"] if primary else 0
    return {
        "id": row["id"],
        "configuration": dict(record["configuration"]),
        "label": configuration_label(record["configuration"]),
        # Each slot's variant, type and model, as the run recorded them: names the agent
        # declares, and a model's name.
        "providers": {slot: dict(provider) for slot, provider in report["providers"].items()},
        "models": list(record.get("models") or []),
        "engine": record.get("engine"),
        "record_format": record["format"],
        "signed": {"key_id": row["key_id"], "at": record["created_at"], "verified": True},
        "rerun": row["rerun"],
        "figures": worked,
        # Totals over the agent's critical labels, as the order's first measure counts them.
        "critical_cases": sum(c["n"] for c in critical),
        "critical_missed": sum(c["n"] - c["detected"] for c in critical),
        "critical_automated": sum(c["auto_missed"] for c in critical),
        "tokens": dict(report["tokens"]),
        "claimed": dict(record.get("claimed") or {}),
        "position": None,
        "dataset": record["dataset"],
        # A row as `ranking.grouped` orders it: counts only, and no cost.
        "row": {
            "config": configuration_label(record["configuration"]),
            "dataset": record["dataset"]["name"],
            "dataset_sha256": record["dataset"]["sha256"],
            "n": record["dataset"]["n"],
            "pack_digest": record["pack"]["digest"],
            "critical": [
                {k: c[k] for k in ("question", "label", "n", "detected", "auto_missed")}
                for c in critical
            ],
            "false_automation": {"k": decisions["false_automation"], "n": answered},
            "accuracy_proportion": {"k": primary["right"], "n": answered} if primary else None,
            "cost_per_decision_usd": None,
            "evaluation": row["id"],
        },
    }


def compare(
    members: list[dict[str, Any]], digest: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The evaluations of one dataset in the order they are shown, and what the comparison
    says (module docstring). Every member ran on the same records, so `ranking.grouped` puts
    them in one group: its order, and its rule for when nothing ranks."""
    by_id = {member["id"]: member for member in members}
    grouped = ranking.grouped([member["row"] for member in members], digest)
    rows = [row for _, group in grouped for row in group]
    ordered = [by_id[row["evaluation"]] for row in rows]
    ranked = [row for row in rows if row["rank"] is not None]
    compared: dict[str, Any] = {
        "method": ranking.METHOD,
        "measures": list(ranking.MEASURES),
        "status": ORDERED,
        "first": None,
        "second": None,
        "tied": [],
        "decided_by": None,
        "figures": None,
        "separated": None,
    }
    if len(rows) < 2:
        compared["status"] = ONE
    elif not ranked:
        named = any(row["critical"] for row in rows)
        compared["status"] = INSUFFICIENT if named else NO_CRITICAL_LABEL
    if compared["status"] != ORDERED:
        return ordered, compared
    for position, row in enumerate(ranked, start=1):
        by_id[row["evaluation"]]["position"] = position
    first, second = ranked[0], ranked[1]
    ahead, behind = ranking.measures(first), ranking.measures(second)
    decided = next((i for i in range(len(ahead)) if ahead[i] != behind[i]), None)
    if decided is None:
        compared["status"] = TIED
        compared["tied"] = [row["evaluation"] for row in ranked if ranking.measures(row) == ahead]
        return ordered, compared
    measure = ranking.MEASURES[decided]
    one, other = _measured(first, measure), _measured(second, measure)
    bounds = (one["low"], one["high"], other["low"], other["high"])
    compared.update(
        first=first["evaluation"],
        second=second["evaluation"],
        decided_by=measure,
        figures={"first": one, "second": other},
        # Whether the two intervals keep apart; null when one of them is of no cases.
        separated=None
        if None in bounds
        else one["high"] < other["low"] or other["high"] < one["low"],
    )
    return ordered, compared


def _measured(row: Mapping[str, Any], measure: str) -> dict[str, Any]:
    """A row's k of n on one of the order's measures, with its exact interval (and, for the
    worst critical label's detection, which label that is); of no cases when the row holds no
    count for it."""
    if measure == "critical_automated":
        labels = row["critical"]
        return served_figures.of(sum(c["auto_missed"] for c in labels), sum(c["n"] for c in labels))
    if measure == "critical_detection":
        worst = ranking.worst_critical(row)
        if worst is None:
            return served_figures.of(0, 0)
        of = served_figures.of(worst["detected"], worst["n"])
        return {**of, "question": worst["question"], "label": worst["label"]}
    counted = row["false_automation" if measure == "false_automation" else "accuracy_proportion"]
    if counted is None:
        return served_figures.of(0, 0)
    return served_figures.of(counted["k"], counted["n"])


def totals(datasets: list[dict[str, Any]], synthetic: bool) -> dict[str, Any]:
    """What the badge counts of the evaluations shown: the configurations and the datasets,
    the cases of the one dataset, whether the agent's data is synthetic, and the fewest and
    the most critical cases a configuration automated (null: the agent names no critical
    label)."""
    shown = [c for dataset in datasets for c in dataset["configurations"]]
    automated = [c["critical_automated"] for c in shown if c["critical_cases"]]
    named = any(dataset["critical"] for dataset in datasets)
    return {
        "configurations": len({c["label"] for c in shown}),
        "datasets": len(datasets),
        "cases": datasets[0]["n"] if len(datasets) == 1 else None,
        "synthetic": synthetic,
        "least_automated": min(automated) if named and automated else None,
        "most_automated": max(automated) if named and automated else None,
    }
