# SPDX-License-Identifier: Apache-2.0
"""The committee packet (mendari.committee-packet/1): everything a hospital's AI governance
committee reads about one agent, at one version, as this deployment sees it.

The evidence a committee needs sits in five places (the Decision Card, the publisher's
evaluation, the site's own decisions and shadow pilot, monitoring, the approvals). `build`
gathers it into one structured document, in the order a committee reads; `render_html` turns
that document into a single page that prints: no script, nothing loaded, readable in black and
white, a page break before each section, and the agent, its version and digest and the time it
was generated at the foot of every page.

**What it never holds.** Case text or patient data: it is counts, ids, label codes, the names of
configurations and dates. The only people it names are those who recorded an approval, whom the
deployment already shows to every role that may read the packet.

**What it never does.** Fill a gap. Every number carries what it is out of and, where the engine
computes one, its exact interval; a section with nothing in it says why; and whatever a
framework asks for that neither the publisher nor the site has stated is listed under "Not
stated" (`crosswalk.yaml` holds the list), never guessed.

**The crosswalk** is data (`crosswalk.yaml`), read here and never written in code: for each
section, the parts of four published frameworks it holds information for, each with its source,
an address and the day it was checked. It is a reading aid, and says so above its table.

The caller sets the organization the store reads for (`tenancy.acting_for`), as for any other
read, and gives the holds in force as that organization may see them.

**Without a deployment** (`build_alone`): a publisher hands the packet to a hospital before any
deployment exists, so it is also made from the agent's files alone. Everything the publisher
states and the software enforces is there as in any packet. Whatever only a deployment holds
(the review queue and the holds, a site's decisions and evaluations, what the monitor found,
what runs and ran, how inputs are kept, the approvals) is null in the document, and its section
says in words that no deployment was read (`no_deployment`, on the packet and on each such
section): never a zero, which would read as "nothing happened".
"""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from mendari import __version__, notices
from mendari.core import validation_brief
from mendari.core.audit import AuditStore
from mendari.core.card import RISK_AUTOMATION, RISK_NAMES, read_benchmark
from mendari.core.origin import COMMUNITY_AUTOMATION
from mendari.core.pack import Pack
from mendari.core.status import title as question_title
from mendari.errors import MendariError
from mendari.evals import figures, monitoring
from mendari.evals.result import normalize

FORMAT = "mendari.committee-packet/1"
DEFAULT_DAYS = 90
MAX_EVALUATIONS = 5  # of an organization's saved evaluations, the newest: a packet is read
MIN_DAYS, MAX_DAYS = monitoring.MIN_DAYS, monitoring.MAX_DAYS
# The sections, in the order a committee reads: (key, title). The crosswalk names them by key.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("identity", "What it is and who made it"),
    ("intended_use", "Intended use, users and population, and what it must not be used for"),
    ("risk", "Risk level and what the runtime enforces because of it"),
    ("oversight", "How a person stays in the loop"),
    ("publisher_evidence", "The publisher's evidence"),
    ("site_evidence", "This site's evidence"),
    ("limits", "Known failure modes and limits"),
    ("monitoring", "Monitoring and what triggers an alert"),
    ("change_control", "Change control"),
    ("data_handling", "Data handling and privacy"),
    ("approvals", "Who approved what and when"),
)
APPENDICES: tuple[tuple[str, str], ...] = (
    ("not_stated", "Not stated"),
    ("crosswalk", "Crosswalk to published frameworks"),
)
ABOUT = (
    "Prepared by the software from what this deployment holds about the agent when the packet "
    "was asked for. It holds counts, ids, label codes, names of configurations and dates: no "
    "case text and no patient data. It is not signed, and it is not a certification, an "
    "attestation of compliance or legal advice. Where something is not known, it says so."
)
# The same, for a packet made from the agent's files alone (`build_alone`).
ABOUT_ALONE = (
    "Prepared by the software from the agent's files alone, with no deployment's database. It "
    "holds what the publisher states and what the software enforces, and nothing that any site "
    "has decided, reviewed, measured or approved: no case text and no patient data. It is not "
    "signed, and it is not a certification, an attestation of compliance or legal advice. "
    "Where something is not known, it says so."
)
# Why a packet holds no deployment's data, on its cover and as its `no_deployment`.
NO_DEPLOYMENT = (
    "This packet was made without a deployment's database, from the agent's files alone, as a "
    "publisher makes it before any site runs the agent. The sections on a site's review "
    "queue, evidence, monitoring, configuration and approvals say so in place of figures: "
    "that is not a count of zero. A site makes the packet again from its own deployment."
)
# What each section that reads a deployment says in place of what it did not read.
WITHOUT_DEPLOYMENT = {
    "oversight": (
        "No review queue and no hold is shown: this packet was made without a deployment's "
        "database, so there was none to read. It is not a count of zero."
    ),
    "site_evidence": (
        "No site's evidence is shown: this packet was made without a deployment's database, so "
        "no decision, review or evaluation of any site was read. It does not say that there "
        "is none."
    ),
    "monitoring": (
        "Nothing was monitored for this packet: it was made without a deployment's database, "
        "so there are no weeks and no alerts to show. Where alerts go and how often the "
        "monitor runs are each deployment's settings."
    ),
    "change_control": (
        "What a site runs, and which version ran there since when, is not shown: this packet "
        "was made without a deployment's database."
    ),
    "data_handling": (
        "What is kept of an input and for how long, which addresses may be called, who can "
        "read a decision and how the audit record is signed are each deployment's settings: "
        "this packet was made without a deployment, so none is shown."
    ),
    "approvals": (
        "No approval is shown: this packet was made without a deployment's database, so none "
        "was read. It does not say that none was recorded."
    ),
}
# The note of an item a site's records could state, in a packet that read none.
_NOT_READ = "Not read: this packet was made without a deployment's database."
CONFIRMED, TO_VERIFY = "confirmed", "to_verify"
STATUS_WORDS = {CONFIRMED: "Checked against the source", TO_VERIFY: "To verify"}
_STATUS_SHORT = {CONFIRMED: "Checked", TO_VERIFY: "To verify"}  # in a table's cell
WHO_WORDS = {"publisher": "The publisher", "site": "This site"}
# Provider types that call a service off this deployment (`Provider.remote`).
_CALLS_OUT = frozenset({"agent", "llm", "jev", "cql"})
OUTCOMES = (
    ("AUTO", "The recommendation is acted on without a person, within the limits of its risk."),
    ("HUMAN_REVIEW", "A person decides. The recommendation and its reasons are shown to them."),
    ("ABSTAIN", "The agent recommends nothing, and a person decides."),
    (
        "BLOCK",
        "The request is stopped at once: an input the agent does not accept, or a rule of its "
        "policy. It goes to no queue.",
    ),
)
PHI_MODES = {
    "off": (
        "Inputs are kept in the audit record as written. This mode is for synthetic data: a "
        "deployment with sign-in never runs it."
    ),
    "hash": (
        "No input is kept: only a keyed hash of it, so the same input can be recognized and "
        "nothing can be read back."
    ),
    "encrypt": (
        "Inputs are kept sealed, apart from the audit record. Only a role that may read inputs "
        "opens one, and each such read is recorded in the audit chain."
    ),
}


@dataclass(frozen=True)
class Deployment:
    """What the deployment says of itself, for the sections on data handling, monitoring and
    change control. A server knows all of it. The command line knows what its environment
    sets and what the database records; what it cannot know is None, and the packet says so."""

    phi_mode: str
    input_hash: str
    retention_days: int
    # Whether the scheduled jobs run here (the purge, the monitor, the checkpoint).
    scheduled: bool | None = None
    sign_in: bool | None = None
    accounts: str | None = None  # "claims" or "memberships"
    session_idle_minutes: int | None = None
    session_max_hours: int | None = None
    checkpoint_key: str | None = None  # the id of the key that signs the daily checkpoint
    alert_destination: bool = False
    alerts_signed: bool = False
    # The origins remote providers may be called at; None: no list is set.
    provider_allowed_origins: Sequence[str] | None = None
    # Whether an agent's own service is configured (None: not known from here).
    agent_endpoint: bool | None = None
    # The variant each slot runs here, and its model where known; None: not known.
    providers: Mapping[str, str] | None = None
    models: Mapping[str, str | None] | None = None
    seen_from: str = "server"  # or "command line"


# What the builders that only ask a deployment what it knows read when there is none: nothing
# is known. Its required settings are never read from it (`build_alone` shows none of them).
_NO_DEPLOYMENT = Deployment(phi_mode="", input_hash="", retention_days=0, seen_from="nowhere")


# -- the crosswalk, as data ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def crosswalk_data() -> dict[str, Any]:
    """`crosswalk.yaml`, read once and checked: every row names a section of the packet and a
    part of a framework that exist, every part a source of its framework, and every status is
    one the packet knows how to show."""
    text = resources.files("mendari.core").joinpath("crosswalk.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    sections = {key for key, _ in SECTIONS}
    parts: dict[str, dict[str, Any]] = {}
    for framework in data["frameworks"]:
        sources = {source["id"] for source in framework["sources"]}
        statuses = [framework["status"], *(note["status"] for note in framework.get("notes", []))]
        for part in framework["parts"]:
            if part["source"] not in sources or part["id"] in parts:
                raise MendariError(f"crosswalk.yaml: part {part['id']} has no source of its own")
            parts[part["id"]] = part
            statuses.append(part["status"])
        if any(status not in STATUS_WORDS for status in statuses):
            raise MendariError(f"crosswalk.yaml: a status of {framework['id']} is not known")
    for row in data["rows"]:
        if row["section"] not in sections or row["part"] not in parts:
            raise MendariError(f"crosswalk.yaml: a row names {row['section']} / {row['part']}")
    for item in data["items"]:
        if item["who"] not in WHO_WORDS or not set(item["asked_by"]) <= set(parts):
            raise MendariError(f"crosswalk.yaml: item {item['key']} names what is not there")
    return dict(data)


def _part_index(data: Mapping[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Each part by id, with its framework."""
    return {
        part["id"]: (framework, part)
        for framework in data["frameworks"]
        for part in framework["parts"]
    }


def _crosswalk() -> dict[str, Any]:
    """The crosswalk as the packet carries it: each row whole, with its section's number and
    title, the framework's part as its source names it, and that source's title, address and
    the day it was checked."""
    data = crosswalk_data()
    index = _part_index(data)
    numbers = {key: (number, title) for number, (key, title) in enumerate(SECTIONS, start=1)}
    rows = []
    for row in data["rows"]:
        framework, part = index[row["part"]]
        source = next(s for s in framework["sources"] if s["id"] == part["source"])
        number, title = numbers[row["section"]]
        rows.append(
            {
                "section": row["section"],
                "section_number": number,
                "section_title": title,
                "framework": framework["id"],
                "framework_name": framework["short"],
                "ref": part["ref"],
                "part": " ".join(str(part["title"]).split()),
                "holds": " ".join(str(row["holds"]).split()),
                "status": row.get("status", part["status"]),
                "source": {
                    "title": " ".join(str(source["title"]).split()),
                    "url": source["url"],
                    "checked_on": source["checked_on"],
                },
            }
        )
    frameworks = [
        {
            "id": framework["id"],
            "name": " ".join(str(framework["name"]).split()),
            "short": framework["short"],
            "publisher": framework["publisher"],
            "edition": " ".join(str(framework["edition"]).split()),
            "applies_to": " ".join(str(framework["applies_to"]).split()),
            "status": framework["status"],
            "sources": [
                {
                    "title": " ".join(str(source["title"]).split()),
                    "url": source["url"],
                    "checked_on": source["checked_on"],
                }
                for source in framework["sources"]
            ],
            "notes": [
                {"text": " ".join(str(note["text"]).split()), "status": note["status"]}
                for note in framework.get("notes", [])
            ],
            # The framework's parts, as its source names them: what each row above refers to.
            "parts": [
                {
                    "ref": part["ref"],
                    "title": " ".join(str(part["title"]).split()),
                    "status": part["status"],
                }
                for part in framework["parts"]
            ],
        }
        for framework in data["frameworks"]
    ]
    return {
        "format": data["format"],
        "checked_on": data["checked_on"],
        "disclaimer": " ".join(str(data["disclaimer"]).split()),
        "frameworks": frameworks,
        "rows": rows,
    }


def _not_stated(stated: Mapping[str, bool | str]) -> list[dict[str, Any]]:
    """Every item a framework asks for that nobody has stated. `stated` answers for the items
    a record or a manifest field can hold: True (stated, so not listed), or why it is not
    (listed, with that as its note). An item no field holds yet is always listed."""
    data = crosswalk_data()
    index = _part_index(data)
    listed = []
    for item in data["items"]:
        found = stated.get(item["stated_by"], False) if item.get("stated_by") else False
        if found is True:
            continue
        note = found if isinstance(found, str) else None
        if not item.get("stated_by"):
            note = "No field or record in this deployment holds it yet."
        listed.append(
            {
                "key": item["key"],
                "item": item["item"],
                "who": item["who"],
                "asked_by": [
                    {"framework": index[part][0]["short"], "ref": index[part][1]["ref"]}
                    for part in item["asked_by"]
                ],
                "note": note,
            }
        )
    return listed


# -- gathering -----------------------------------------------------------------------------------


def _hex(digest: str, length: int = 12) -> str:
    return digest.removeprefix("sha256:")[:length]


def _covering(holds: Sequence[Mapping[str, Any]], pack: Pack) -> list[Mapping[str, Any]]:
    """The holds in force that cover this version of this agent."""
    return [
        hold
        for hold in holds
        if hold.get("pack_id") in (None, pack.id) and hold.get("pack_digest") in (None, pack.digest)
    ]


def _running(pack: Pack, deployment: Deployment | None) -> list[dict[str, Any]]:
    """Each slot as it runs here (or, where that is not known, as the agent sets it by
    default): its variant and type, its model where known, and whether it calls a service off
    this deployment, with the input fields an agent's own service is sent. Without a
    deployment (None) nothing is known to run: every slot is the agent's own default."""
    deployment = deployment or _NO_DEPLOYMENT
    slots = []
    for slot, spec in pack.manifest.providers.items():
        known = deployment.providers is not None and slot in deployment.providers
        name = deployment.providers[slot] if deployment.providers and known else spec.use
        variant = spec.variants.get(name)
        extra = (variant.model_extra or {}) if variant is not None else {}
        members = extra.get("members")
        kinds = {variant.type} if variant is not None else set()
        if isinstance(members, dict):
            kinds |= {str(m.get("type")) for m in members.values() if isinstance(m, dict)}
        fields = extra.get("state_fields")
        agent = variant is not None and variant.type == "agent"
        slots.append(
            {
                "slot": slot,
                "answers": list(spec.answers),
                "variant": name,
                "known": known,
                "type": None if variant is None else variant.type,
                "model": (deployment.models or {}).get(slot),
                "calls_out": bool(kinds & _CALLS_OUT),
                "sends": [str(f) for f in fields] if agent and isinstance(fields, list) else None,
                # An agent's own service, when none is configured: its canned examples answer.
                "canned": agent and deployment.agent_endpoint is False,
            }
        )
    return slots


def _identity(pack: Pack) -> dict[str, Any]:
    m = pack.manifest
    critical: dict[str, list[str]] = {}
    for item in m.evaluation.critical:
        critical.setdefault(item.question, []).append(item.label)
    return {
        "name": m.name,
        "id": m.id,
        "version": m.version,
        "digest": pack.digest,
        "summary": m.summary,
        "status": pack.status_label,
        "publisher": {"name": m.publisher.name, "type": m.publisher.type, "url": m.publisher.url},
        "license": m.license,
        "source": {"origin": pack.shown_origin, "label": pack.origin_label},
        "questions": [
            {
                "key": key,
                "title": question_title(key, question),
                "type": question.type,
                "instructions": question.instructions,
                "labels": [
                    {"label": label, "criterion": text} for label, text in question.criteria.items()
                ],
                "primary": key == m.policy.primary,
                "critical": critical.get(key, []),
            }
            for key, question in m.questions.items()
        ],
    }


def _intended_use(pack: Pack) -> dict[str, Any]:
    card = pack.manifest.card
    return {
        "intended_use": card.intended_use,
        "intended_users": list(card.intended_users),
        "patient_population": card.patient_population,
        "out_of_scope": list(card.out_of_scope),
        "stated_by": "publisher",
    }


def _risk(pack: Pack, deployment: Deployment | None) -> dict[str, Any]:
    m = pack.manifest
    deployment = deployment or _NO_DEPLOYMENT
    level = int(m.risk.level)
    enforced = []
    if pack.community:
        enforced.append(
            "A community agent never decides alone, at any risk level: it recommends, and a "
            "person confirms every decision."
        )
    elif level <= 1:
        enforced.append(
            f"At risk level {level} a decision may be automated when the agent's confidence "
            "reaches its threshold. The runtime asks nothing else of the organization first."
        )
    elif level in (2, 3):
        enforced.append(
            f"At risk level {level} a person reviews every decision. The organization can allow "
            "automation only by recording an approval of this exact version, with a reference "
            "and an expiry; when it expires or is revoked, review returns."
        )
    else:
        enforced.append(
            "At risk level 4 no decision is ever automated, and no approval changes that."
        )
    if pack.origin == "hub":
        enforced.append(
            "It came from a registry: at every risk level it automates nothing until the "
            "organization approves this exact version, and it stops if the registry recalls it "
            "or cannot be checked."
        )
    enforced += [
        "The limits of a risk level are the runtime's, not the agent's: nothing in an agent's "
        "files loosens them.",
        "Every limit only lowers automation: a decision that would be automated goes to a "
        "person, never the reverse.",
        "When the agent or its model fails, nothing is automated: the decision goes to a person.",
    ]
    if deployment.sign_in:
        enforced.append(
            "With sign-in on, answers read from an agent's canned examples are never automated."
        )
    return {
        "level": level,
        "name": RISK_NAMES[level],
        "category": m.risk.category,
        "rationale": m.risk.rationale,
        "declared_by": "publisher",
        "automation": COMMUNITY_AUTOMATION if pack.community else RISK_AUTOMATION[level],
        "enforced": enforced,
    }


def _oversight(
    pack: Pack, stats: Mapping[str, Any] | None, holds: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """`stats` and `holds` None: no deployment was read, so the review queue and the holds
    are null, not empty."""
    policy = pack.manifest.policy
    covering = []
    for hold in _covering(holds or (), pack):
        reason = str(hold.get("reason") or "")
        covers = "this version" if hold.get("pack_digest") else "this agent"
        covering.append(
            {
                "id": hold["id"],
                "covers": covers if hold.get("pack_id") else "every agent",
                "scope": "organization" if hold.get("org") else "deployment",
                "set_at": hold.get("set_at"),
                # None: the deployment operator's own, which an organization is not shown.
                "reason": reason or None,
                "shadow_pilot": reason.lower().startswith(("shadow pilot:", "[shadow]")),
            }
        )
    return {
        "outcomes": [{"outcome": outcome, "meaning": meaning} for outcome, meaning in OUTCOMES],
        "primary": policy.primary,
        "thresholds": {"auto": policy.thresholds.auto, "review": policy.thresholds.review},
        "never_automated": [label for label, rule in policy.labels.items() if not rule.allow_auto],
        "label_thresholds": {
            label: rule.auto_threshold
            for label, rule in policy.labels.items()
            if rule.allow_auto and rule.auto_threshold is not None
        },
        "automation_requires": {slot: list(v) for slot, v in policy.automation_requires.items()},
        "rules": [
            {
                "name": rule.name,
                "outcome": rule.outcome.value,
                "priority": None if rule.priority is None else rule.priority.value,
                "reason": rule.reason,
            }
            for rule in policy.rules
        ],
        "fallback": policy.fallback,
        "review_queue": (
            None
            if stats is None
            else {
                "waiting": int(stats["pending_reviews"]),
                "resolved": int(stats["reviews_resolved"]),
                "decisions": int(stats["decisions"]),
            }
        ),
        "holds": None if holds is None else covering,
    }


def _evaluation(report: Mapping[str, Any], pack: Pack) -> dict[str, Any] | None:
    """An evaluation report as the packet shows it: counts with intervals, the critical cases
    and the configuration it ran on. None when it is not a report the figures can be read of."""
    try:
        r = normalize(dict(report))
        shown = figures.report(r)
        measured = r["pack"]
        return {
            "dataset": {
                "name": str(r["dataset"]["name"]),
                "n": r["dataset"]["n"],
                "sha256": r["dataset"].get("sha256"),
            },
            "run_at": r["run"]["created_at"],
            "measured_on": {
                "version": measured.get("version"),
                "digest": measured.get("digest"),
                "this_version": measured.get("digest") == pack.digest,
            },
            "configuration": {
                str(slot): {"variant": info.get("variant"), "model": info.get("model")}
                for slot, info in r["providers"].items()
            },
            "accuracy": shown["accuracy"],
            "outcomes": shown["outcomes"],
            "automated_correct": shown["automated_correct"],
            "false_automation": shown["false_automation"],
            "expected_override": shown["expected_override"],
            "critical": shown["critical"],
            "subgroups": {
                str(attribute): {str(value): cell["accuracy"] for value, cell in values.items()}
                for attribute, values in shown["subgroups"].items()
            },
        }
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, ArithmeticError):
        return None


def _publisher_evidence(pack: Pack) -> dict[str, Any]:
    m = pack.manifest
    evaluation, reason, source = None, None, "agent_files"
    registry = pack.pulled.get("registry") if pack.origin == "hub" else None
    if pack.origin == "hub":
        source = "registry"
        reason = (
            "This agent came from a registry. Its publisher's signed evaluations are published "
            "there, and this deployment shows no figure it did not compute itself, so none is "
            "copied here: read them on the agent's page at the registry."
        )
    elif pack.origin == "store":
        source = "store"
        reason = (
            "This version comes from the organization's own store, which keeps no benchmark "
            "uploaded with an agent: its evaluations run here are in the next section."
        )
    else:
        report, problem = read_benchmark(pack)
        if report is not None:
            evaluation = _evaluation(report, pack)
        if evaluation is None:
            reason = (
                f"The benchmark the agent names could not be read ({problem})."
                if problem
                else "The agent carries no evaluation by its publisher: its manifest names no "
                "benchmark report."
            )
    return {
        "source": source,
        "registry": registry if isinstance(registry, str) else None,
        "evaluation": evaluation,
        "reason": reason,
        "datasets": sorted(m.evaluation.datasets),
        "benchmark_dataset": m.evaluation.benchmark_dataset,
        "critical_declared": [
            {"question": item.question, "label": item.label} for item in m.evaluation.critical
        ],
        "subgroups_declared": list(m.evaluation.subgroups),
        "provenance": m.card.provenance,
        "evidence_cited": [
            {"title": cited.title, "url": cited.url, "note": cited.note}
            for cited in m.card.evidence
        ],
    }


def _site_evidence(
    pack: Pack,
    brief: Mapping[str, Any],
    stats: Mapping[str, Any],
    reports: Sequence[Mapping[str, Any]],
    days: int,
) -> dict[str, Any]:
    volume = brief["volume"]
    total = int(stats["decisions"])
    pilot = brief["shadow_pilot"]
    # The newest evaluations saved here (the store gives them newest first), and how many more.
    evaluations = [
        found
        for report in reports[:MAX_EVALUATIONS]
        if (found := _evaluation(report, pack)) is not None
    ]
    empty = None
    if not total and not evaluations:
        empty = (
            "This deployment has made no decision with this agent and has run no evaluation of "
            "it for this organization."
        )
    return {
        "empty": empty,
        "window_days": days,
        "decisions": {
            "all_time": total,
            "outcomes": {
                outcome: figures.maybe(stats["outcomes"].get(outcome, 0), total)
                for outcome in figures.OUTCOMES
            },
            "in_window": int(volume["total"]),
            "automated": figures.maybe(volume["automated"], volume["total"]),
            "would_automate": figures.maybe(volume["would_automate"], volume["total"]),
        },
        "shadow_pilot": {
            "active": bool(pilot["active"]),
            "since": pilot.get("set_at"),
            # Decided under the shadow header; those held by a pilot's hold count as held.
            "shadow_decisions": figures.maybe(volume["shadow_decisions"], volume["total"]),
        },
        "review": {
            "waiting": int(volume["pending_reviews"]),
            "overrides": figures.maybe(volume["overrides"], volume["reviewed"]),
            "agreement": figures.maybe(volume["agreed"], volume["labelled"]),
            "patterns": [
                {
                    "recommended": str(cluster["recommended"]),
                    "chosen": str(cluster["chosen"]),
                    "count": int(cluster["count"]),
                    "of": int(volume["overrides"]),
                }
                for cluster in brief["override_clusters"]
            ],
        },
        "ratings": {
            "automated": int(volume["automated"]),
            "rated": int(volume["auto_rated"]),
            "rated_wrong": int(volume["down_on_auto"]),
            "with_outcome": int(volume["auto_labelled"]),
        },
        "gates": [
            {
                "key": gate["key"],
                "label": gate["label"],
                "status": gate["status"],
                "words": validation_brief.GATE_WORDS[gate["status"]],
                "detail": gate["detail"],
            }
            for gate in brief["gates"]
        ],
        "gates_line": validation_brief.readiness_line(brief["gates"]),
        "evaluations": evaluations,
        "evaluations_saved": len(reports),
    }


def _limits(
    pack: Pack,
    running: Sequence[Mapping[str, Any]],
    publisher: Mapping[str, Any],
    *,
    deployed: bool = True,
) -> dict:
    """`deployed` False: the packet read no deployment, and says that first among its limits."""
    m = pack.manifest
    of_evidence = []
    if not deployed:
        of_evidence.append(
            "This packet was made without a deployment: it shows no site's decisions, reviews "
            "or evaluations, and so says nothing of how the agent does at any site."
        )
    if m.card.provenance == "synthetic":
        of_evidence.append(
            "The agent's own data is synthetic: an evaluation on it says how the agent does on "
            "written examples, not on "
            + ("this site's patients." if deployed else "a site's patients.")
        )
    measured = (publisher.get("evaluation") or {}).get("measured_on")
    if measured and not measured["this_version"]:
        of_evidence.append(
            "The publisher's evaluation measured another version of the agent than the one "
            + ("served here." if deployed else "this packet describes.")
        )
    if any(slot["canned"] for slot in running):
        of_evidence.append(
            "No service is configured for the agent here, so it answers from its canned "
            "examples: the decisions counted in this packet say nothing of a live agent."
        )
    if any(slot["type"] == "agent" for slot in running):
        of_evidence.append(
            "The model behind an agent's own service is not visible to this software. What "
            "this packet says of it is its publisher's statement."
        )
    of_evidence.append(
        "The software cannot yet record a clinician's review of a version, so every agent is "
        "shown as not clinically reviewed."
    )
    return {
        "known_failure_modes": list(m.card.known_failure_modes),
        "stated_by": "publisher",
        "status": pack.status_label,
        "of_the_evidence": of_evidence,
    }


def _checks(tolerance: float) -> list[dict[str, str]]:
    """What the monitor checks, what raises an alert, and the evidence each check needs
    (`evals/monitoring.py`'s own constants)."""
    weekly = monitoring.MIN_FOR_OPERATIONS
    return [
        {
            "check": "Accuracy against the benchmark",
            "alert": (
                "Reviewed or automated decisions of the last 7 days are less accurate than the "
                f"benchmark by more than {tolerance * 100:.1f} points, even at the top of their "
                "95% interval."
            ),
            "needs": (
                f"A comparable benchmark, and {monitoring.MIN_REVIEWED} reviewed decisions or "
                f"{monitoring.MIN_LABELLED} automated ones with a recorded outcome."
            ),
            "severity": "high",
        },
        {
            "check": "Confidence drift",
            "alert": (
                "The population stability index between this week's confidences and the "
                f"benchmark's is {monitoring.DRIFT_ALERT} or more."
            ),
            "needs": f"A comparable benchmark and {monitoring.MIN_FOR_DRIFT} confidences.",
            "severity": "medium",
        },
        {
            "check": "Automated decisions rated wrong",
            "alert": "Someone rated an automated decision of the last 7 days wrong.",
            "needs": "One rating.",
            "severity": "high",
        },
        {
            "check": "Reviewer overrides",
            "alert": "The share of recommendations reviewers changed doubled in a week.",
            "needs": f"{monitoring.MIN_REVIEWED} reviews in each of the two weeks.",
            "severity": "medium",
        },
        {
            "check": "Provider errors",
            "alert": (
                f"{monitoring.ERROR_SHARE:.0%} or more of this week's decisions had a provider "
                "error, and the share doubled."
            ),
            "needs": f"{weekly} decisions this week.",
            "severity": "medium",
        },
        {
            "check": "Abstentions",
            "alert": (
                "The share of abstentions doubled and rose "
                f"{monitoring.ABSTAIN_RISE * 100:.0f} points in a week."
            ),
            "needs": f"{weekly} decisions in each of the two weeks.",
            "severity": "medium",
        },
        {
            "check": "Latency",
            "alert": (
                f"{monitoring.SLOW_SHARE:.0%} or more of this week's decisions took twice the "
                f"week before's 95th percentile, and {monitoring.SLOW_MS / 1000:.0f} seconds or "
                "more."
            ),
            "needs": f"{weekly} decisions in each of the two weeks.",
            "severity": "medium",
        },
    ]


def _monitoring(mon: Mapping[str, Any], deployment: Deployment) -> dict[str, Any]:
    benchmark = mon["benchmark"]
    recent = mon["last_7_days"]

    def week(row: Mapping[str, Any]) -> dict[str, Any]:
        shown = row["figures"]
        return {
            "decisions": int(row["decisions"]),
            "refused": int(row["refused"]),
            "automated": shown["automated"],
            "overrides": shown["overrides"],
            "accuracy": shown["accuracy"],
            "provider_errors": shown["provider_errors"],
            "abstained": shown["abstained"],
            "awaiting_review": int(row["awaiting_review"]),
        }

    return {
        "days": int(mon["days"]),
        "checks": _checks(float(benchmark["tolerance"])),
        "benchmark": {
            "comparable": bool(benchmark["comparable"]),
            "reason": (
                None
                if benchmark["comparable"]
                else validation_brief.NOT_COMPARED.get(
                    str(benchmark["reason"]), "no benchmark is comparable here"
                )
            ),
        },
        "last_7_days": week(recent),
        "weeks": [{"from": row["from"], "to": row["to"], **week(row)} for row in mon["weeks"]],
        "alerts": [
            {"kind": alert["kind"], "severity": alert["severity"], "message": alert["message"]}
            for alert in mon["alerts"]
        ],
        "other_versions": {str(v): int(count) for v, count in mon["other_versions"].items()},
        "alert_destination": {
            "set": deployment.alert_destination,
            "signed": deployment.alerts_signed,
        },
        "scheduled": deployment.scheduled,
    }


def _change_control(
    pack: Pack,
    deployment: Deployment | None,
    running: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]] | None,
    recall: str | None,
) -> dict[str, Any]:
    """Without a deployment (None): nothing is known to run, and no registry was asked."""
    deployed = deployment is not None
    deployment = deployment or _NO_DEPLOYMENT
    pulled = pack.pulled
    signed = None
    if pack.origin == "hub":
        signed = {
            "registry": pulled.get("registry"),
            "namespace": pulled.get("namespace"),
            "publisher_type": pulled.get("publisher_type"),
            "key_id": pulled.get("key_id"),
            "archive_sha256": pulled.get("archive_sha256"),
            "pulled_at": pulled.get("pulled_at"),
            "counter_signed": pulled.get("accepted") is True,
            "recalled_when_pulled": pulled.get("yank") is not None,
            # Why its registry stops it automating now, or None. Only a running server asks
            # its registry: a command checks nothing, and the packet says that.
            "held_now": recall,
            "recall_checked": deployment.seen_from == "server",
        }
    on_change = [
        "The digest is a SHA-256 over every file that defines the agent's behaviour: change "
        "one, and the digest changes.",
        "Every decision records the version and the digest that made it.",
        "An approval names one digest. A changed agent has another, so no approval covers it "
        "until the organization approves it again.",
        "A benchmark is the monitor's reference only for this digest, with the providers and "
        "models that run here. After a change the monitor says that none is comparable, and "
        "compares only decisions made since.",
    ]
    if pack.origin == "hub":
        on_change += [
            "A version from a registry arrives as a signed archive: its hash, its publisher's "
            "signature and the registry's counter-signature are checked when it is pulled.",
            "When the registry recalls this version, it stops automating here, and what its "
            "rules would block goes to a person.",
        ]
    elif deployed:
        on_change.append(
            "This agent is served from the deployment's own files: no signature is checked, "
            "and whoever can change those files can change the agent. The digest shows that "
            "they did."
        )
    else:
        on_change.append(
            "This packet read the agent from files, not from a registry's signed archive: no "
            "signature was checked. A site that pulls it from a registry has its signature "
            "checked; one that serves it from its own files can compare the digest above."
        )
    if any(slot["type"] == "agent" for slot in running):
        on_change.append(
            "A change of the model behind an agent's own service changes no file here, so the "
            "digest stays the same: only the monitor can show it."
        )
    return {
        "version": pack.version,
        "digest": pack.digest,
        "signed": signed,
        "running": list(running),
        "running_known": deployment.providers is not None,
        # Which version ran where since when; None: the deployment operator's to read.
        "records": (
            None
            if records is None
            else [
                {
                    "started_at": record["started_at"],
                    "version": record["pack_version"],
                    "digest": record["pack_digest"],
                    "this_version": record["pack_digest"] == pack.digest,
                    "providers": dict(record["providers"]),
                    "environment": record.get("environment"),
                    "release": record.get("release"),
                }
                for record in records
            ]
        ),
        "on_change": on_change,
    }


def _schema_fields(schema: Any, prefix: str = "") -> list[str]:
    """The names of the input's fields, as the agent's schema declares them (never a value)."""
    if not isinstance(schema, dict):
        return []
    found: list[str] = []
    for name, prop in (schema.get("properties") or {}).items():
        path = f"{prefix}{name}"
        if isinstance(prop, dict) and prop.get("type") == "object":
            found += _schema_fields(prop, f"{path}.") or [path]
        else:
            found.append(path)
    return found


def _data_handling(
    pack: Pack, deployment: Deployment, running: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    m = pack.manifest
    return {
        "inputs": {
            "fields": _schema_fields(pack.input_schema),
            "fhir": m.inputs.fhir is not None,
            # "hash": the agent asks that its inputs are never kept, whatever the mode.
            "kept": m.audit.store_input,
        },
        "phi_mode": deployment.phi_mode,
        "phi_mode_meaning": PHI_MODES.get(deployment.phi_mode, "Not a mode this packet knows."),
        "input_hash": deployment.input_hash,
        "retention": {"days": deployment.retention_days, "scheduled": deployment.scheduled},
        "calls_out": [
            {
                "slot": slot["slot"],
                "variant": slot["variant"],
                "type": slot["type"],
                "model": slot["model"],
                "sends": slot["sends"],
                "canned": slot["canned"],
            }
            for slot in running
            if slot["calls_out"]
        ],
        "allowed_origins": (
            None
            if deployment.provider_allowed_origins is None
            else list(deployment.provider_allowed_origins)
        ),
        "access": {
            "sign_in": deployment.sign_in,
            "accounts": deployment.accounts,
            "session_idle_minutes": deployment.session_idle_minutes,
            "session_max_hours": deployment.session_max_hours,
        },
        "audit": {"checkpoint_key": deployment.checkpoint_key},
        "seen_from": deployment.seen_from,
    }


def _approvals_required(pack: Pack, *, deployed: bool = True) -> list[str]:
    """What this agent needs before it automates, by its risk level and where it came from: the
    runtime's rules, the same with or without a deployment to read."""
    level = int(pack.manifest.risk.level)
    required = []
    if pack.community:
        required.append("A community agent never automates. No approval changes that.")
    elif level >= 4:
        required.append("At risk level 4 nothing is automated. No approval is possible.")
    elif level in (2, 3):
        required.append(
            f"At risk level {level}, automation needs a recorded approval of this exact "
            "version, with a reference and an expiry."
        )
    else:
        required.append(
            f"At risk level {level} the runtime asks for no approval before automation, and "
            + ("this deployment" if deployed else "a deployment")
            + " records none for it."
        )
    if pack.origin == "hub":
        required.append(
            "An agent from a registry needs the organization's approval of its exact version "
            "before it automates anything."
        )
    return required


def _approvals(
    pack: Pack,
    governance: Sequence[Mapping[str, Any]],
    registry: Sequence[Mapping[str, Any]],
    active: Mapping[str, Any] | None,
    registry_active: Mapping[str, Any] | None,
) -> dict[str, Any]:
    required = _approvals_required(pack)

    def shown(row: Mapping[str, Any], *, expires: bool) -> dict[str, Any]:
        this_version = row["pack_digest"] == pack.digest
        live = active if expires else registry_active
        return {
            "id": row["id"],
            "approver": row["approver"],
            "reference": row["reference"],
            "approved_at": row["created_at"],
            "expires_at": row.get("expires_at") if expires else None,
            "revoked_at": row.get("revoked_at"),
            "revoked_by": row.get("revoked_by"),
            "digest": row["pack_digest"],
            "this_version": this_version,
            "in_force": live is not None and live["id"] == row["id"],
        }

    return {
        "required": required,
        "governance": [shown(row, expires=True) for row in governance],
        "registry": [shown(row, expires=False) for row in registry],
        "in_force": {
            "governance": active is not None,
            "registry": registry_active is not None if pack.origin == "hub" else None,
        },
    }


def _publisher_stated(pack: Pack, publisher: Mapping[str, Any]) -> dict[str, bool | str]:
    """Which of the items a publisher can state it has stated, for the "Not stated" appendix:
    True, or why not (`_not_stated`)."""
    evaluation = publisher["evaluation"]
    card = pack.manifest.card
    return {
        "publisher_evaluation": (
            True
            if evaluation is not None
            else "Held by the registry, if its publisher published one: not read here."
            if pack.origin == "hub"
            else False
        ),
        "evidence_cited": bool(card.evidence),
        "out_of_scope": bool(card.out_of_scope),
        "known_failure_modes": bool(card.known_failure_modes),
        "publisher_subgroups": bool(evaluation and evaluation["subgroups"]),
    }


def _digest_of(packet: Mapping[str, Any]) -> str:
    """SHA-256 of the packet's canonical JSON, its own `sha256` left out."""
    body = {key: value for key, value in packet.items() if key != "sha256"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(
    store: AuditStore,
    pack: Pack,
    *,
    deployment: Deployment,
    days: int = DEFAULT_DAYS,
    organization: str | None = None,
    holds: Sequence[Mapping[str, Any]] = (),
    records: Sequence[Mapping[str, Any]] | None = None,
    recall: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The packet for `pack` as served here, over the last `days` days (14 to 180, the
    monitor's bounds), for the organization the store reads for.

    `holds` are the holds in force as that organization may see them; `records` the deployment
    records of the agent (None: its operator's, as in memberships mode); `recall` why an
    agent's registry stops it automating now (None: it does not, or the agent came from none).
    Reads only: nothing is recorded."""
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise MendariError(f"a packet covers {MIN_DAYS} to {MAX_DAYS} days, not {days}")
    now = now or datetime.now(UTC)
    mon = monitoring.monitor(
        store, pack, days=days, now=now, providers=deployment.providers, models=deployment.models
    )
    active = store.active_approval(pack.id, pack.digest)
    from_registry = pack.origin == "hub"
    registry_active = store.registry_approval(pack.id, pack.digest) if from_registry else None
    brief = validation_brief.build(
        store,
        pack,
        days=days,
        holds=holds,
        governance_active=active,
        registry_approval={"active": registry_active} if from_registry else None,
        monitoring=mon,
    )
    stats = store.stats(pack.id)
    reports = store.reports(pack.id)
    running = _running(pack, deployment)
    publisher = _publisher_evidence(pack)
    site = _site_evidence(pack, brief, stats, reports, days)
    approvals = _approvals(
        pack,
        store.approvals(pack.id),
        store.registry_approvals(pack.id) if from_registry else [],
        active,
        registry_active,
    )
    content = {
        "identity": _identity(pack),
        "intended_use": _intended_use(pack),
        "risk": _risk(pack, deployment),
        "oversight": _oversight(pack, stats, holds),
        "publisher_evidence": publisher,
        "site_evidence": site,
        "limits": _limits(pack, running, publisher),
        "monitoring": _monitoring(mon, deployment),
        "change_control": _change_control(pack, deployment, running, records, recall),
        "data_handling": _data_handling(pack, deployment, running),
        "approvals": approvals,
    }
    stated: dict[str, bool | str] = {
        **_publisher_stated(pack, publisher),
        "local_evidence": site["empty"] is None,
        "local_subgroups": any(found["subgroups"] for found in site["evaluations"]),
        "approval_record": (
            True
            if active is not None or registry_active is not None
            else "This deployment records an approval only for risk level 2 or 3 and for an "
            "agent from a registry: it has no record for this one."
            if int(pack.manifest.risk.level) < 2 and not from_registry
            else False
        ),
    }
    packet = {
        "format": FORMAT,
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_by": {"software": "Mendari", "version": __version__},
        "notice": notices.NOTICE,
        "about": ABOUT,
        "organization": organization,
        "window_days": days,
        "agent": {
            "id": pack.id,
            "name": pack.manifest.name,
            "version": pack.version,
            "digest": pack.digest,
            "status": pack.status_label,
        },
        "sections": [
            {"key": key, "number": number, "title": title, "content": content[key]}
            for number, (key, title) in enumerate(SECTIONS, start=1)
        ],
        "not_stated": _not_stated(stated),
        "crosswalk": _crosswalk(),
    }
    return {**packet, "sha256": _digest_of(packet)}


def build_alone(pack: Pack, *, now: datetime | None = None) -> dict[str, Any]:
    """The packet for `pack` from its files alone, with no deployment to read: what a publisher
    hands a hospital before any deployment exists.

    The sections the agent's own files fill (what it is, its intended use, its risk level and
    what the runtime enforces, its policy, the publisher's evidence, its known limits, what
    the monitor checks, how a change shows, what it reads and sends) are as in any packet.
    Everything only a deployment holds is null, and each such section carries `no_deployment`,
    the sentence the page shows in its place (`WITHOUT_DEPLOYMENT`); the packet's own
    `no_deployment` says it once for the whole document, and it names no organization and no
    window of days. Reads the agent's files only: no database, no setting of this machine."""
    now = now or datetime.now(UTC)
    running = _running(pack, None)
    publisher = _publisher_evidence(pack)
    content: dict[str, dict[str, Any]] = {
        "identity": _identity(pack),
        "intended_use": _intended_use(pack),
        "risk": _risk(pack, None),
        "oversight": _oversight(pack, None, None),
        "publisher_evidence": publisher,
        "site_evidence": {
            "empty": WITHOUT_DEPLOYMENT["site_evidence"],
            "window_days": None,
            "decisions": None,
            "shadow_pilot": None,
            "review": None,
            "ratings": None,
            "gates": None,
            "gates_line": None,
            "evaluations": None,
            "evaluations_saved": None,
        },
        "limits": _limits(pack, running, publisher, deployed=False),
        "monitoring": {
            "days": None,
            "checks": _checks(float(pack.manifest.evaluation.degradation_tolerance)),
            "benchmark": None,
            "last_7_days": None,
            "weeks": None,
            "alerts": None,
            "other_versions": None,
            "alert_destination": None,
            "scheduled": None,
        },
        "change_control": _change_control(pack, None, running, None, None),
        "data_handling": {
            "inputs": {
                "fields": _schema_fields(pack.input_schema),
                "fhir": pack.manifest.inputs.fhir is not None,
                "kept": pack.manifest.audit.store_input,
            },
            "phi_mode": None,
            "phi_mode_meaning": None,
            "input_hash": None,
            "retention": None,
            "calls_out": [
                {
                    "slot": slot["slot"],
                    "variant": slot["variant"],
                    "type": slot["type"],
                    "model": slot["model"],
                    "sends": slot["sends"],
                    "canned": slot["canned"],
                }
                for slot in running
                if slot["calls_out"]
            ],
            "allowed_origins": None,
            "access": None,
            "audit": None,
            "seen_from": "agent files",
        },
        "approvals": {
            "required": _approvals_required(pack, deployed=False),
            "governance": None,
            "registry": None,
            "in_force": None,
        },
    }
    for key, why in WITHOUT_DEPLOYMENT.items():
        content[key]["no_deployment"] = why
    stated: dict[str, bool | str] = {
        **_publisher_stated(pack, publisher),
        "local_evidence": _NOT_READ,
        "local_subgroups": _NOT_READ,
        "approval_record": _NOT_READ,
    }
    packet = {
        "format": FORMAT,
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_by": {"software": "Mendari", "version": __version__},
        "notice": notices.NOTICE,
        "about": ABOUT_ALONE,
        "no_deployment": NO_DEPLOYMENT,
        "organization": None,
        "window_days": None,
        "agent": {
            "id": pack.id,
            "name": pack.manifest.name,
            "version": pack.version,
            "digest": pack.digest,
            "status": pack.status_label,
        },
        "sections": [
            {"key": key, "number": number, "title": title, "content": content[key]}
            for number, (key, title) in enumerate(SECTIONS, start=1)
        ],
        "not_stated": _not_stated(stated),
        "crosswalk": _crosswalk(),
    }
    return {**packet, "sha256": _digest_of(packet)}


# -- the packet's shape --------------------------------------------------------------------------

_FIGURE = {
    "type": ["object", "null"],
    "required": ["k", "n", "low", "high"],
    "properties": {
        "k": {"type": "integer", "minimum": 0},
        "n": {"type": "integer", "minimum": 0},
        "low": {"type": ["number", "null"]},
        "high": {"type": ["number", "null"]},
    },
}
_TEXT = {"type": "string", "minLength": 1}
_TEXTS = {"type": "array", "items": _TEXT}
_REQUIRED = {
    "identity": ["name", "id", "version", "digest", "summary", "status", "publisher", "questions"],
    "intended_use": ["intended_use", "intended_users", "patient_population", "out_of_scope"],
    "risk": ["level", "name", "category", "rationale", "automation", "enforced"],
    "oversight": ["outcomes", "thresholds", "rules", "fallback", "review_queue", "holds"],
    "publisher_evidence": ["source", "evaluation", "reason", "critical_declared"],
    "site_evidence": ["empty", "decisions", "shadow_pilot", "review", "gates", "evaluations"],
    "limits": ["known_failure_modes", "status", "of_the_evidence"],
    "monitoring": ["checks", "benchmark", "last_7_days", "weeks", "alerts", "alert_destination"],
    "change_control": ["version", "digest", "signed", "running", "records", "on_change"],
    "data_handling": ["inputs", "phi_mode", "retention", "calls_out", "access", "audit"],
    "approvals": ["required", "governance", "registry", "in_force"],
}
_SOURCE = {
    "type": "object",
    "required": ["title", "url", "checked_on"],
    "properties": {
        "title": _TEXT,
        "url": {"type": "string", "pattern": "^https://"},
        "checked_on": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
    },
}
_STATUS = {"enum": [CONFIRMED, TO_VERIFY]}
SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Mendari committee packet",
    "type": "object",
    "required": [
        "format",
        "generated_at",
        "generated_by",
        "notice",
        "about",
        "organization",
        "window_days",
        "agent",
        "sections",
        "not_stated",
        "crosswalk",
        "sha256",
    ],
    "additionalProperties": False,
    "properties": {
        "format": {"const": FORMAT},
        "generated_at": _TEXT,
        "generated_by": {"type": "object", "required": ["software", "version"]},
        "notice": _TEXT,
        "about": _TEXT,
        # Only on a packet made from the agent's files alone (`build_alone`): why it holds no
        # deployment's data. Such a packet names no organization and no window of days.
        "no_deployment": _TEXT,
        "organization": {"type": ["string", "null"]},
        "window_days": {"type": ["integer", "null"], "minimum": MIN_DAYS, "maximum": MAX_DAYS},
        "agent": {
            "type": "object",
            "required": ["id", "name", "version", "digest", "status"],
            "additionalProperties": False,
            "properties": {
                "id": _TEXT,
                "name": _TEXT,
                "version": _TEXT,
                "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "status": _TEXT,
            },
        },
        # Every section, in the order a committee reads, and no other.
        "sections": {
            "type": "array",
            "minItems": len(SECTIONS),
            "items": False,
            "prefixItems": [
                {
                    "type": "object",
                    "required": ["key", "number", "title", "content"],
                    "additionalProperties": False,
                    "properties": {
                        "key": {"const": key},
                        "number": {"const": number},
                        "title": {"const": title},
                        "content": {"type": "object", "required": _REQUIRED[key]},
                    },
                }
                for number, (key, title) in enumerate(SECTIONS, start=1)
            ],
        },
        "not_stated": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key", "item", "who", "asked_by", "note"],
                "additionalProperties": False,
                "properties": {
                    "key": _TEXT,
                    "item": _TEXT,
                    "who": {"enum": sorted(WHO_WORDS)},
                    "asked_by": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "object", "required": ["framework", "ref"]},
                    },
                    "note": {"type": ["string", "null"]},
                },
            },
        },
        "crosswalk": {
            "type": "object",
            "required": ["format", "checked_on", "disclaimer", "frameworks", "rows"],
            "properties": {
                "disclaimer": _TEXT,
                "frameworks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["id", "name", "edition", "applies_to", "status", "sources"],
                        "properties": {
                            "status": _STATUS,
                            "sources": {"type": "array", "minItems": 1, "items": _SOURCE},
                            "notes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["text", "status"],
                                    "properties": {"text": _TEXT, "status": _STATUS},
                                },
                            },
                        },
                    },
                },
                "rows": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": [
                            "section",
                            "section_number",
                            "framework",
                            "ref",
                            "part",
                            "holds",
                            "status",
                            "source",
                        ],
                        "properties": {
                            "section": {"enum": [key for key, _ in SECTIONS]},
                            "status": _STATUS,
                            "source": _SOURCE,
                            "holds": _TEXT,
                        },
                    },
                },
            },
        },
        "sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    },
    # A deployment's packet covers a window of days. One made without a deployment says so on
    # the packet and on each section that would have read one, and names no window.
    "if": {"required": ["no_deployment"]},
    "then": {
        "properties": {
            "organization": {"type": "null"},
            "window_days": {"type": "null"},
            "sections": {
                "prefixItems": [
                    {
                        "properties": {
                            "content": {
                                "required": ["no_deployment"] if key in WITHOUT_DEPLOYMENT else []
                            }
                        }
                    }
                    for key, _ in SECTIONS
                ]
            },
        }
    },
    "else": {"properties": {"window_days": {"type": "integer"}}},
    "$defs": {"figure": _FIGURE, "texts": _TEXTS},
}


def problems(packet: Any) -> list[str]:
    """Why `packet` is not a committee packet (its shape, and its own hash), or nothing."""
    found = [
        f"{'/'.join(str(part) for part in error.absolute_path) or 'packet'}: {error.message}"
        for error in Draft202012Validator(SCHEMA).iter_errors(packet)
    ]
    if not found and packet["sha256"] != _digest_of(packet):
        found.append("sha256: it is not the hash of this packet")
    return found


# -- the page ------------------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
html { font-size: 11pt; }
body { margin: 0 auto; padding: 24px 28px 48px; max-width: 900px; color: #000; background: #fff;
  font-family: Georgia, "Times New Roman", serif; line-height: 1.4; }
h1, h2, h3, th, .label, .meta, .foot, nav { font-family: Arial, Helvetica, sans-serif; }
h1 { font-size: 22pt; margin: 0 0 4px; }
h2 { font-size: 15pt; margin: 0 0 10px; padding-bottom: 4px; border-bottom: 2px solid #000; }
h3 { font-size: 11.5pt; margin: 18px 0 6px; }
p { margin: 6px 0; }
ul, ol { margin: 6px 0; padding-left: 22px; }
section { margin-top: 36px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 12px; font-size: 10pt; }
th, td { border: 1px solid #000; padding: 4px 7px; text-align: left; vertical-align: top; }
thead th { border-bottom: 2px solid #000; }
table.facts th { width: 30%; font-weight: bold; }
td.num { text-align: right; white-space: nowrap; }
code { font-family: "Courier New", Courier, monospace; font-size: 9.5pt; overflow-wrap: anywhere; }
a { color: #000; overflow-wrap: anywhere; }
.cover { border: 2px solid #000; padding: 18px 20px; }
.meta { font-size: 10pt; }
.status { border: 1px solid #000; padding: 6px 10px; margin: 12px 0; font-weight: bold; }
.label { display: inline-block; border: 1px solid #000; padding: 0 5px; font-size: 8.5pt;
  font-weight: bold; text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap; }
.none { border-left: 4px solid #000; padding: 4px 10px; margin: 8px 0; font-style: italic; }
.by { font-family: Arial, Helvetica, sans-serif; font-size: 9pt; font-style: italic; }
.notice { border: 2px solid #000; padding: 8px 12px; margin: 10px 0; font-weight: bold; }
.foot { font-size: 8pt; border-top: 1px solid #000; padding-top: 3px; margin-top: 36px; }
nav ol { columns: 2; column-gap: 32px; font-size: 10pt; }
nav li { break-inside: avoid; }
.appendix table { font-size: 9pt; }
@media print {
  body { padding: 0; max-width: none; }
  section { break-before: page; margin-top: 0; }
  h2, h3 { break-after: avoid; }
  tr, li, .none, .status, .notice { break-inside: avoid; }
  thead { display: table-header-group; }
  .foot { display: none; }
}
"""
# The characters a CSS string holds as they are; every other one is written as its code point,
# so nothing a publisher named an agent can end the string, or the style element it is in.
_CSS_PLAIN = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,:;()/_+=@-"
)
_FOOT_NAME_CHARS = 60  # of an agent's name in the foot of a page, which is one line
# The cover's word for no organization: a deployment without sign-in, or a command without --org.
_EVERY_ORGANIZATION = "none named: everything this deployment holds for the agent"


def _css_string(text: str) -> str:
    return '"' + "".join(c if c in _CSS_PLAIN else f"\\{ord(c):X} " for c in text) + '"'


def _page_rule(foot: str) -> str:
    """The printed page: its margins, `foot` at the bottom of every page and the page's number
    at the top. Margin boxes are what CSS gives a page's foot (Chrome and Edge print them); a
    browser without them prints its own header, which carries the page's title: the same
    words."""
    small = "font: 7.5pt Arial, Helvetica, sans-serif; color: #000;"
    return (
        "@page { margin: 16mm 15mm 20mm; "
        f"@bottom-left {{ content: {_css_string(foot)}; {small} }} "
        '@bottom-right { content: "Page " counter(page) " of " counter(pages); '
        f"{small} white-space: nowrap; }} "
        f'@top-right {{ content: "Committee packet, prepared by the software"; {small} }} }}'
    )


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _figure(found: Mapping[str, Any] | None) -> str:
    """A share of cases in words: what it is out of, and its exact 95% interval."""
    if found is None:
        return "not known"
    k, n = int(found["k"]), int(found["n"])
    if not n:
        return "no cases (0 of 0)"
    low, high = found.get("low"), found.get("high")
    share = f"{100 * k / n:.1f}%"
    if isinstance(low, int | float) and isinstance(high, int | float):
        return f"{k} of {n} ({share}; 95% interval {low * 100:.1f}% to {high * 100:.1f}%)"
    return f"{k} of {n} ({share})"


def _when(value: Any) -> str:
    """A stored instant as a committee reads it: its day and minute, in UTC."""
    if not value:
        return "not recorded"
    try:
        instant = datetime.fromisoformat(str(value))
    except ValueError:
        return _e(value)
    if instant.tzinfo is None:
        return _e(instant.strftime("%Y-%m-%d %H:%M"))
    return _e(instant.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"))


def _code(value: Any) -> str:
    return f"<code>{_e(value)}</code>"


def _label(words: Any) -> str:
    return f'<span class="label">{_e(words)}</span>'


def _none(reason: str) -> str:
    return f'<p class="none">{_e(reason)}</p>'


def _by(who: str) -> str:
    return f'<p class="by">{_e(who)}</p>'


def _facts(rows: Sequence[tuple[str, str]]) -> str:
    """A two-column table: what, and its value (already escaped)."""
    body = "".join(
        f'<tr><th scope="row">{_e(name)}</th><td>{value}</td></tr>' for name, value in rows
    )
    return f'<table class="facts"><tbody>{body}</tbody></table>'


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], numeric: Sequence[int] = ()
) -> str:
    head = "".join(f'<th scope="col">{_e(header)}</th>' for header in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="num">{cell}</td>' if index in numeric else f"<td>{cell}</td>"
            for index, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _items(values: Sequence[Any]) -> str:
    return "<ul>" + "".join(f"<li>{_e(value)}</li>" for value in values) + "</ul>"


def _yes(value: bool | None, yes: str, no: str, unknown: str = "Not known from here.") -> str:
    return _e(unknown if value is None else yes if value else no)


def _html_identity(c: Mapping[str, Any]) -> str:
    publisher = c["publisher"]
    said = f"{_e(publisher['name'])} ({_e(publisher['type'])}; its own declaration)"
    if publisher.get("url"):
        said += f", {_e(publisher['url'])}"
    out = _facts(
        [
            ("Name", _e(c["name"])),
            ("Id and version", f"{_code(c['id'])} version {_code(c['version'])}"),
            ("Digest", _code(c["digest"])),
            ("Publisher", said),
            ("Licence", _e(c["license"])),
            ("Where this copy came from", _e(c["source"]["label"])),
            ("Status", _e(c["status"])),
        ]
    )
    out += f"<h3>What it does</h3><p>{_e(c['summary'])}</p>{_by('Written by the publisher.')}"
    out += "<h3>What it answers</h3>"
    for question in c["questions"]:
        marks = " (the answer its decisions follow)" if question["primary"] else ""
        out += (
            f"<p><b>{_e(question['title'])}</b> {_code(question['key'])}{_e(marks)}: "
            f"{_e(question['instructions'])}</p>"
        )
        rows = [
            [
                _code(label["label"])
                + (" " + _label("critical") if label["label"] in question["critical"] else ""),
                _e(label["criterion"]),
            ]
            for label in question["labels"]
        ]
        out += _table(["Answer", "When, in the publisher's words"], rows)
    return out


def _html_intended_use(c: Mapping[str, Any]) -> str:
    out = f"<h3>Intended use</h3><p>{_e(c['intended_use'])}</p>"
    out += "<h3>Intended users</h3>" + _items(c["intended_users"])
    out += f"<h3>Patient population</h3><p>{_e(c['patient_population'])}</p>"
    out += "<h3>What it must not be used for</h3>"
    out += (
        _items(c["out_of_scope"])
        if c["out_of_scope"]
        else _none("Not stated: the publisher names no use it cautions against.")
    )
    return out + _by("All of this section is the publisher's own statement, from the manifest.")


def _html_risk(c: Mapping[str, Any]) -> str:
    out = _facts(
        [
            ("Risk level", f"<b>{_e(c['level'])}</b> of 0 to 4: {_e(c['name'])}"),
            ("Category, in the publisher's words", _e(c["category"])),
            ("Why this level, in the publisher's words", _e(c["rationale"])),
            ("Automation at this level", _e(c["automation"])),
        ]
    )
    out += _by(
        "The level is declared by the publisher. The software has no way to record that anyone "
        "reviewed it."
    )
    return out + "<h3>What the runtime enforces</h3>" + _items(c["enforced"])


def _html_oversight(c: Mapping[str, Any]) -> str:
    out = "<h3>Every decision ends in one of four outcomes</h3>"
    out += _table(
        ["Outcome", "What happens"],
        [[_code(o["outcome"]), _e(o["meaning"])] for o in c["outcomes"]],
    )
    thresholds = c["thresholds"]
    rows = [
        (
            "Decides alone when",
            f"its confidence in {_code(c['primary'])} is {thresholds['auto']:.2f} or more, and "
            "nothing below lowers it",
        ),
        ("A person decides when", f"it is below that and at least {thresholds['review']:.2f}"),
        ("It recommends nothing when", f"it is below {thresholds['review']:.2f}"),
        (
            "Answers never automated",
            ", ".join(_code(label) for label in c["never_automated"]) or "none named",
        ),
        ("When it cannot answer", _e(c["fallback"])),
    ]
    for label, threshold in c["label_thresholds"].items():
        rows.append((f"Decides alone on {label} only from", f"{threshold:.2f}"))
    for slot, variants in c["automation_requires"].items():
        rows.append((f"Automates only while {slot} runs", ", ".join(_code(v) for v in variants)))
    out += "<h3>When the agent decides alone</h3>" + _facts(rows)
    out += "<h3>Rules that send a decision to a person, whatever the confidence</h3>"
    out += (
        _table(
            ["Rule", "Outcome", "Priority", "Why, in the publisher's words"],
            [
                [
                    _code(rule["name"]),
                    _code(rule["outcome"]),
                    _e(rule["priority"] or "the default"),
                    _e(rule["reason"]),
                ]
                for rule in c["rules"]
            ],
        )
        if c["rules"]
        else _none("The agent's policy has no such rule.")
    )
    queue = c["review_queue"]
    out += "<h3>The review queue</h3>"
    out += (
        "<p>Decisions that go to a person wait in the review queue. A reviewer confirms the "
        "recommendation or chooses another answer, and that choice is recorded with the "
        "decision.</p>"
    )
    if queue is not None:
        out += _facts(
            [
                ("Waiting for review now", _e(queue["waiting"])),
                (
                    "Reviews resolved so far",
                    f"{_e(queue['resolved'])} of {_e(queue['decisions'])} decisions",
                ),
            ]
        )
    out += "<h3>The automation hold</h3>"
    out += (
        "<p>Anyone who may approve automation can hold it at once, for one version, one agent "
        "or every agent of the organization. While a hold is in force, every decision that "
        "would be automated goes to a person instead. A shadow pilot is such a hold.</p>"
    )
    if c.get("no_deployment"):
        return out + "<h3>The queue and the holds at a site</h3>" + _none(c["no_deployment"])
    if not c["holds"]:
        return out + _none("No hold covers this agent now.")
    return out + _table(
        ["Covers", "Set", "Reason"],
        [
            [
                _e(hold["covers"]) + (" " + _label("shadow pilot") if hold["shadow_pilot"] else ""),
                _when(hold["set_at"]),
                _e(
                    hold["reason"]
                    or "set by the deployment's operator, who gives it no reason here"
                ),
            ]
            for hold in c["holds"]
        ],
    )


def _html_evaluation(found: Mapping[str, Any]) -> str:
    dataset, measured = found["dataset"], found["measured_on"]
    configuration = ", ".join(
        f"{_e(slot)} = {_code(info['variant'])}"
        + (f" (model {_code(info['model'])})" if info.get("model") else "")
        for slot, info in found["configuration"].items()
    )
    rows = [
        ("Test set", f"{_code(dataset['name'])}, {_e(dataset['n'])} cases"),
        ("Run on", _when(found["run_at"])),
        (
            "Version measured",
            f"{_code(measured['version'])} "
            + (
                "(the version served here)"
                if measured["this_version"]
                else "(not the version served here)"
            ),
        ),
        ("Configuration it ran on", configuration or "not recorded"),
        ("Right answers", _e(_figure(found["accuracy"]))),
        ("Automated, and right", _e(_figure(found["automated_correct"]))),
        # Under the names the evaluation report and the guides give the two risks.
        ("False automation (automated, and wrong)", _e(_figure(found["false_automation"]))),
        ("Changed by a reviewer, expected", _e(_figure(found["expected_override"]))),
    ]
    rows += [
        (f"Ended as {outcome}", _e(_figure(share))) for outcome, share in found["outcomes"].items()
    ]
    out = _facts(rows)
    if found["critical"]:
        out += "<h3>Critical cases</h3>" + _table(
            [
                "Answer that must not be missed",
                "Detected",
                "Critical misses (automated, so no person read them)",
            ],
            [
                [
                    f"{_code(item['question'])} = {_code(item['label'])}"
                    + (
                        " " + _label("too few cases to judge")
                        if item["insufficient_evidence"]
                        else ""
                    ),
                    _e(_figure(item["detected"])),
                    f"{_e(item['auto_missed'])} of {_e(item['detected']['n'])}",
                ]
                for item in found["critical"]
            ],
        )
    for attribute, values in found["subgroups"].items():
        out += f"<h3>By {_e(attribute)}</h3>" + _table(
            ["Subgroup", "Right answers"],
            [[_e(value), _e(_figure(share))] for value, share in values.items()],
        )
    return out


def _html_publisher_evidence(c: Mapping[str, Any]) -> str:
    out = ""
    if c["evaluation"] is not None:
        out += _by("Measured by the publisher, and carried in the agent's files.")
        out += _html_evaluation(c["evaluation"])
    else:
        out += _none(c["reason"])
        if c["registry"]:
            out += f"<p>Registry: {_e(c['registry'])}</p>"
    declared = ", ".join(
        f"{_code(item['question'])} = {_code(item['label'])}" for item in c["critical_declared"]
    )
    out += "<h3>What the publisher set up to be measured</h3>" + _facts(
        [
            (
                "Test sets the agent carries",
                ", ".join(_code(name) for name in c["datasets"]) or "none",
            ),
            ("Kind of data", _e(c["provenance"] or "not stated")),
            ("Answers that must never be missed", declared or "none declared"),
            (
                "Subgroups it asks to be measured",
                ", ".join(_code(name) for name in c["subgroups_declared"]) or "none declared",
            ),
        ]
    )
    out += "<h3>Evidence the publisher cites</h3>"
    if not c["evidence_cited"]:
        return out + _none("Not stated: the publisher cites no study, guideline or publication.")
    return (
        out
        + "<ul>"
        + "".join(
            f"<li>{_e(cited['title'])}"
            + (f", {_e(cited['url'])}" if cited.get("url") else "")
            + (f": {_e(cited['note'])}" if cited.get("note") else "")
            + "</li>"
            for cited in c["evidence_cited"]
        )
        + "</ul>"
    )


def _html_site_evidence(c: Mapping[str, Any]) -> str:
    if c.get("no_deployment"):
        return _none(c["no_deployment"]) + _by(
            "A site fills this section: its decisions so far, its shadow pilot, what its "
            "reviewers changed, the validation brief's gates and the evaluations it ran."
        )
    if c["empty"]:
        return _none(c["empty"]) + _html_gates(c)
    decisions, review, ratings = c["decisions"], c["review"], c["ratings"]
    days = c["window_days"]
    out = "<h3>Decisions so far</h3>"
    rows = [("Decisions made here, in all", _e(decisions["all_time"]))]
    rows += [
        (f"Ended as {outcome}", _e(_figure(share)))
        for outcome, share in decisions["outcomes"].items()
    ]
    rows += [
        (f"Decisions in the last {days} days", _e(decisions["in_window"])),
        ("Of those, automated", _e(_figure(decisions["automated"]))),
        (
            "Of those, automated or held when they would have been",
            _e(_figure(decisions["would_automate"])),
        ),
    ]
    out += _facts(rows)
    pilot = c["shadow_pilot"]
    out += "<h3>The shadow pilot</h3>" + _facts(
        [
            (
                "Shadow pilot",
                f"running since {_when(pilot['since'])}: automation is held while people decide"
                if pilot["active"]
                else "none is running now",
            ),
            (f"Decided in shadow mode, last {days} days", _e(_figure(pilot["shadow_decisions"]))),
        ]
    )
    out += f"<h3>What reviewers did, last {days} days</h3>" + _facts(
        [
            ("Waiting for review", _e(review["waiting"])),
            ("Recommendations a reviewer changed", _e(_figure(review["overrides"]))),
            (
                "Recommendations that matched the reviewer or the recorded outcome",
                _e(_figure(review["agreement"])),
            ),
            (
                "Automated decisions anyone checked",
                f"{_e(ratings['rated'])} rated and {_e(ratings['with_outcome'])} with a recorded "
                f"outcome, of {_e(ratings['automated'])} automated; {_e(ratings['rated_wrong'])} "
                "rated wrong",
            ),
        ]
    )
    if review["patterns"]:
        out += "<h3>What reviewers changed most</h3>" + _table(
            ["The agent recommended", "The reviewer chose", "Times"],
            [
                [
                    _code(pattern["recommended"]),
                    _code(pattern["chosen"]),
                    f"{_e(pattern['count'])} of {_e(pattern['of'])}",
                ]
                for pattern in review["patterns"]
            ],
            numeric=(2,),
        )
    out += _html_gates(c)
    out += "<h3>Evaluations run here</h3>"
    if not c["evaluations"]:
        return out + _none("None: no evaluation of this agent is saved for this organization.")
    shown, saved = len(c["evaluations"]), c["evaluations_saved"]
    if saved > shown:
        out += f"<p>The {shown} most recent of the {_e(saved)} saved here, newest first.</p>"
    for found in c["evaluations"]:
        out += _html_evaluation(found)
    return out


def _html_gates(c: Mapping[str, Any]) -> str:
    out = f"<h3>The validation brief's gates</h3><p>{_e(c['gates_line'])}.</p>"
    out += _table(
        ["Gate", "State", "What was measured"],
        [[_e(gate["label"]), _label(gate["words"]), _e(gate["detail"])] for gate in c["gates"]],
    )
    return out + _by(
        "A gate with nothing to measure says so: it is not a pass. The gates are a summary, not "
        "a decision."
    )


def _html_limits(c: Mapping[str, Any]) -> str:
    out = "<h3>Known failure modes</h3>"
    out += (
        _items(c["known_failure_modes"]) + _by("Written by the publisher.")
        if c["known_failure_modes"]
        else _none("Not stated: the publisher names no failure mode.")
    )
    return out + "<h3>Limits of what this packet shows</h3>" + _items(c["of_the_evidence"])


def _html_monitoring(c: Mapping[str, Any]) -> str:
    out = "<h3>What is checked, and what raises an alert</h3>"
    out += _table(
        ["Check", "An alert is raised when", "It needs", "Severity"],
        [[_e(x["check"]), _e(x["alert"]), _e(x["needs"]), _e(x["severity"])] for x in c["checks"]],
    )
    if c.get("no_deployment"):
        return (
            out
            + _by("These are the checks a deployment's monitor runs on this agent's decisions.")
            + _none(c["no_deployment"])
        )
    benchmark = c["benchmark"]
    destination = c["alert_destination"]
    out += _facts(
        [
            (
                "Compared with a benchmark",
                "yes: the agent's benchmark measured this version as it runs here"
                if benchmark["comparable"]
                else f"no: {_e(benchmark['reason'])}",
            ),
            (
                "Where alerts go",
                (
                    "an alert destination is set"
                    + (
                        ", and alerts are signed"
                        if destination["signed"]
                        else ", and alerts are not signed"
                    )
                )
                if destination["set"]
                else "nowhere: no alert destination is set, so an alert is seen only by whoever "
                "opens the monitoring view",
            ),
            (
                "How often it runs",
                _yes(
                    c["scheduled"],
                    "The server's scheduled job runs it.",
                    "The server's scheduled job is off. Unless a scheduler outside the server "
                    "runs the monitor, it runs only when someone opens the monitoring view.",
                ),
            ),
        ]
    )
    out += "<h3>Alerts now</h3>"
    out += (
        _table(
            ["Kind", "Severity", "What was found"],
            [[_e(a["kind"]), _e(a["severity"]), _e(a["message"])] for a in c["alerts"]],
        )
        if c["alerts"]
        else _none("None. With few decisions most checks cannot run: see what each needs, above.")
    )
    out += f"<h3>The last {_e(len(c['weeks']))} weeks</h3>"
    if not any(week["decisions"] for week in c["weeks"]):
        out += _none("No decision of this version in these weeks.")
    else:
        out += _table(
            ["Week from", "Decisions", "Automated", "Changed by a reviewer", "Matched the label"],
            [
                [
                    _e(week["from"]),
                    _e(week["decisions"]),
                    _e(_figure(week["automated"])),
                    _e(_figure(week["overrides"])),
                    _e(_figure(week["accuracy"])),
                ]
                for week in c["weeks"]
            ],
            numeric=(1,),
        )
    if c["other_versions"]:
        others = ", ".join(f"{_code(v)}: {_e(n)}" for v, n in c["other_versions"].items())
        out += f"<p>Decisions of other versions in the same weeks, not counted above: {others}.</p>"
    return out


def _html_change_control(c: Mapping[str, Any]) -> str:
    out = _facts([("Version", _code(c["version"])), ("Digest", _code(c["digest"]))])
    out += "<h3>Signed versions and recalls</h3>"
    signed = c["signed"]
    if signed is None:
        out += _none(
            "This agent did not come from a registry: no signature was checked, and no registry "
            "can recall it."
        )
    else:
        held = signed["held_now"]
        out += _facts(
            [
                ("Registry", _e(signed["registry"] or "not recorded")),
                ("Namespace", _code(signed["namespace"] or "not recorded")),
                ("Publisher's signing key", _code(signed["key_id"] or "not recorded")),
                (
                    "Registry's counter-signature",
                    "checked when it was pulled" if signed["counter_signed"] else "not recorded",
                ),
                ("Archive SHA-256", _code(signed["archive_sha256"] or "not recorded")),
                ("Pulled", _when(signed["pulled_at"])),
                (
                    "Recall",
                    "recalled when it was pulled"
                    if signed["recalled_when_pulled"]
                    else f"its registry stops it automating now: {_e(held)}"
                    if held
                    else "none standing at the server's last check of its registry"
                    if signed["recall_checked"]
                    else "not checked from the command line: a running server asks the registry",
                ),
            ]
        )
    alone = c.get("no_deployment")
    out += "<h3>What runs, by the agent's own defaults</h3>" if alone else "<h3>What runs here</h3>"
    if alone:
        out += _by(
            "No deployment was read: these are the parts as the agent sets them. A site can "
            "run another of the agent's variants, and its own packet shows which."
        )
    elif not c["running_known"]:
        out += _by(
            "What runs is not known from here: these are the agent's own defaults. A server's "
            "packet shows what it runs."
        )
    out += _table(
        ["Part", "Answers", "Runs", "Model"],
        [
            [
                _code(slot["slot"]),
                ", ".join(_code(answer) for answer in slot["answers"]),
                f"{_code(slot['variant'])} ({_e(slot['type'] or 'unknown')})"
                + (" " + _label("canned examples") if slot["canned"] else ""),
                _code(slot["model"]) if slot["model"] else "not known to this software",
            ]
            for slot in c["running"]
        ],
    )
    out += (
        "<h3>What ran at a site, since when</h3>" if alone else "<h3>What ran here, since when</h3>"
    )
    if alone:
        out += _none(alone)
    elif c["records"] is None:
        out += _none(
            "Not shown: the deployment's records belong to its operator, who serves several "
            "organizations."
        )
    elif not c["records"]:
        out += _none(
            "No deployment record for this agent yet: a server writes one when it first serves it."
        )
    else:
        out += _table(
            ["Since", "Version", "Digest", "Runs"],
            [
                [
                    _when(record["started_at"]),
                    _code(record["version"])
                    + (" " + _label("this version") if record["this_version"] else ""),
                    _code(_hex(str(record["digest"]))),
                    ", ".join(f"{_e(s)} = {_code(v)}" for s, v in record["providers"].items()),
                ]
                for record in c["records"]
            ],
        )
    return (
        out + "<h3>What happens when the agent or its model changes</h3>" + _items(c["on_change"])
    )


def _html_calls_out(calls: Sequence[Mapping[str, Any]]) -> str:
    return _table(
        ["Part", "Calls", "Is sent"],
        [
            [
                _code(call["slot"]),
                (
                    "nothing here: it answers from canned examples"
                    if call["canned"]
                    else f"a {_e(call['type'])} service"
                )
                + (f", model {_code(call['model'])}" if call["model"] else ""),
                ", ".join(_code(name) for name in call["sends"])
                if call["sends"]
                else "the agent's input",
            ]
            for call in calls
        ],
    )


def _html_data_alone(c: Mapping[str, Any]) -> str:
    """Data handling in a packet made without a deployment: what the agent reads and what its
    own defaults call, and that everything else is each deployment's setting."""
    inputs = c["inputs"]
    rows = [
        (
            "Fields the agent reads",
            ", ".join(_code(name) for name in inputs["fields"]) or "none declared",
        ),
        ("Reads FHIR resources", "yes, through the agent's mapping" if inputs["fhir"] else "no"),
    ]
    if inputs["kept"] == "hash":
        rows.append(
            (
                "What the agent asks of a deployment",
                "That its inputs are never kept, whatever the deployment's own setting.",
            )
        )
    out = "<h3>What it reads</h3>" + _facts(rows)
    out += "<h3>What leaves a deployment that runs it, by the agent's own defaults</h3>"
    out += (
        _html_calls_out(c["calls_out"])
        if c["calls_out"]
        else _none("Nothing: by its defaults, every part of the agent runs inside a deployment.")
    )
    out += "<h3>What is kept, what may be called, and who can read it</h3>"
    out += _none(c["no_deployment"])
    return out + _facts(
        [
            (
                "This packet",
                "Counts, ids, label codes, names of configurations and dates. No case text and "
                "no patient data.",
            )
        ]
    )


def _html_data_handling(c: Mapping[str, Any]) -> str:
    if c.get("no_deployment"):
        return _html_data_alone(c)
    inputs, retention, access = c["inputs"], c["retention"], c["access"]
    kept = c["phi_mode_meaning"]
    if inputs["kept"] == "hash":
        kept += " This agent asks that its inputs are never kept, whatever the mode."
    purge = _yes(
        retention["scheduled"],
        "by the server's scheduled purge",
        "when someone, or a scheduler outside the server, runs the purge: the server's own "
        "scheduled purge is off",
        "whether a purge is scheduled is not known from here",
    )
    session = "not known from here"
    if access["session_idle_minutes"] is not None:
        session = (
            f"ended after {_e(access['session_idle_minutes'])} minutes without a request, and "
            f"after {_e(access['session_max_hours'])} hours at most"
        )
    out = "<h3>What it reads and what is kept</h3>" + _facts(
        [
            (
                "Fields the agent reads",
                ", ".join(_code(name) for name in inputs["fields"]) or "none declared",
            ),
            (
                "Reads FHIR resources",
                "yes, through the agent's mapping" if inputs["fhir"] else "no",
            ),
            ("What is kept of an input", f"{_label(c['phi_mode'])} {_e(kept)}"),
            ("How an input is recognized again", _code(c["input_hash"])),
            ("How long a kept input stays", f"{_e(retention['days'])} days, deleted {purge}"),
        ]
    )
    out += "<h3>What leaves this deployment</h3>"
    if not c["calls_out"]:
        out += _none("Nothing: every part of the agent runs inside this deployment.")
    else:
        out += _html_calls_out(c["calls_out"])
    origins = c["allowed_origins"]
    out += _facts(
        [
            (
                "Addresses a remote service may be called at",
                "no list is set: any address the deployment's settings name may be called"
                if origins is None
                else (
                    ", ".join(_code(origin) for origin in origins) or "none: nothing may be called"
                ),
            )
        ]
    )
    out += "<h3>Who can read it, and the record</h3>" + _facts(
        [
            (
                "Sign-in",
                _yes(
                    access["sign_in"],
                    "Required: people sign in through the organization's identity provider, "
                    "and each role sees only what it may.",
                    "Off: anyone who reaches the deployment can read and decide. This is for a "
                    "sandbox on synthetic data.",
                ),
            ),
            ("Browser sessions", session),
            (
                "The audit record",
                "Every decision, review, approval and hold is an event in a hash chain: an "
                "event changed or removed afterwards breaks the chain.",
            ),
            (
                "Daily signed checkpoint",
                f"signed with the key {_code(c['audit']['checkpoint_key'])}"
                if c["audit"]["checkpoint_key"]
                else "not signed: no checkpoint key is set",
            ),
            (
                "This packet",
                "Counts, ids, label codes, names of configurations and dates. No case text and "
                "no patient data.",
            ),
        ]
    )
    if c["seen_from"] != "server":
        out += _by(
            "Made from the command line: it shows the settings of the environment the command "
            "ran in, which may not be the server's."
        )
    return out


_COMMITTEE_BLOCK = (
    ("Decision", "&nbsp;"),
    ("Conditions", "&nbsp;"),
    ("Next review", "&nbsp;"),
    ("Signed, and date", "&nbsp;"),
)


def _html_approvals(c: Mapping[str, Any]) -> str:
    out = "<h3>What this agent needs before it automates</h3>" + _items(c["required"])
    if c.get("no_deployment"):
        out += "<h3>Approvals recorded</h3>" + _none(c["no_deployment"])
        out += "<h3>For the committee</h3>"
        out += (
            "<p>A deployment records the approvals of automation. It has no record for a "
            "committee's own decision, its conditions or its next review date: write them "
            "here.</p>"
        )
        return out + _facts(_COMMITTEE_BLOCK)

    def rows(found: Sequence[Mapping[str, Any]], *, expires: bool) -> list[list[str]]:
        listed = []
        for row in found:
            state = (
                "in force"
                if row["in_force"]
                else "revoked"
                if row["revoked_at"]
                else "not in force"
            )
            cells = [
                _e(row["approver"]),
                _e(row["reference"]),
                _when(row["approved_at"]),
                _code(_hex(str(row["digest"])))
                + (" " + _label("this version") if row["this_version"] else " (another version)"),
                _label(state)
                + (
                    f" on {_when(row['revoked_at'])} by {_e(row['revoked_by'] or 'not recorded')}"
                    if row["revoked_at"]
                    else ""
                ),
            ]
            if expires:
                cells.insert(3, _when(row["expires_at"]))
            listed.append(cells)
        return listed

    out += "<h3>Approvals of automation at this risk level</h3>"
    out += (
        _table(
            ["Approved by", "Reference", "On", "Expires", "Version", "State"],
            rows(c["governance"], expires=True),
        )
        if c["governance"]
        else _none("None recorded for this agent in this organization.")
    )
    if c["in_force"]["registry"] is not None or c["registry"]:
        out += "<h3>Approvals of this agent from its registry</h3>"
        out += (
            _table(
                ["Approved by", "Reference", "On", "Version", "State"],
                rows(c["registry"], expires=False),
            )
            if c["registry"]
            else _none("None recorded: this agent automates nothing here until one is.")
        )
    out += "<h3>For the committee</h3>"
    out += (
        "<p>The software records the approvals above. It has no record for a committee's own "
        "decision, its conditions or its next review date: write them here.</p>"
    )
    return out + _facts(_COMMITTEE_BLOCK)


_RENDER = {
    "identity": _html_identity,
    "intended_use": _html_intended_use,
    "risk": _html_risk,
    "oversight": _html_oversight,
    "publisher_evidence": _html_publisher_evidence,
    "site_evidence": _html_site_evidence,
    "limits": _html_limits,
    "monitoring": _html_monitoring,
    "change_control": _html_change_control,
    "data_handling": _html_data_handling,
    "approvals": _html_approvals,
}


def _html_not_stated(listed: Sequence[Mapping[str, Any]], *, deployed: bool = True) -> str:
    """`deployed` False: no site was read, so the second table is what a site that adopts the
    agent has yet to state, not what one has failed to."""
    if deployed:
        out = (
            "<p>Each line below is something one of the four frameworks in Appendix B asks "
            "for and that neither the publisher nor this site has stated. Nothing here is a "
            "finding against the agent: it is what a committee would have to ask for. The "
            "software never fills one in with a guess.</p>"
        )
    else:
        out = (
            "<p>Each line below is something one of the four frameworks in Appendix B asks "
            "for. The first table is what the publisher has not stated. The second is what "
            "only a site can state: this packet read no deployment, so every such line is "
            "listed. Nothing here is a finding against the agent: it is what a committee "
            "would have to ask for. The software never fills one in with a guess.</p>"
        )
    for who, words in WHO_WORDS.items():
        found = [item for item in listed if item["who"] == who]
        if deployed or who != "site":
            out += f"<h3>{_e(words)} has not stated ({len(found)})</h3>"
        else:
            out += f"<h3>A site that adopts it has yet to state ({len(found)})</h3>"
        if not found:
            out += _none("Nothing.")
            continue
        out += _table(
            ["Not stated", "Asked for by", "Note"],
            [
                [
                    _e(item["item"]),
                    "; ".join(f"{_e(a['framework'])}, {_e(a['ref'])}" for a in item["asked_by"]),
                    _e(item["note"] or ""),
                ]
                for item in found
            ],
        )
    return out


def _html_crosswalk(crosswalk: Mapping[str, Any]) -> str:
    out = f'<p class="notice">{_e(crosswalk["disclaimer"])}</p>'
    out += f"<p>The frameworks were read on {_e(crosswalk['checked_on'])}.</p>"
    out += "<h3>The four frameworks</h3>"
    for framework in crosswalk["frameworks"]:
        out += f"<p><b>{_e(framework['name'])}</b>. {_e(framework['publisher'])}. "
        out += f"{_e(framework['edition'])}. {_label(STATUS_WORDS[framework['status']])}</p>"
        out += f"<p>{_e(framework['applies_to'])}</p><ul>"
        for note in framework["notes"]:
            out += f"<li>{_e(note['text'])} {_label(STATUS_WORDS[note['status']])}</li>"
        for source in framework["sources"]:
            out += (
                f'<li>Source: <a href="{_e(source["url"])}" rel="noopener noreferrer">'
                f"{_e(source['title'])}</a>, {_e(source['url'])}, read on "
                f"{_e(source['checked_on'])}.</li>"
            )
        out += "</ul>"
        out += _table(
            ["Part", "What it asks for, as its source names it", "Checked"],
            [
                [_e(part["ref"]), _e(part["title"]), _label(_STATUS_SHORT[part["status"]])]
                for part in framework["parts"]
            ],
        )
    out += "<h3>Where each section holds information for a framework</h3>"
    for number, (key, title) in enumerate(SECTIONS, start=1):
        rows = [row for row in crosswalk["rows"] if row["section"] == key]
        out += f"<h3>{number}. {_e(title)}</h3>"
        if not rows:
            out += _none("This section holds nothing a framework names.")
            continue
        out += _table(
            ["Framework and part", "What this section holds, and what it does not", "Checked"],
            [
                [
                    f"{_e(row['framework_name'])}, {_e(row['ref'])}",
                    _e(row["holds"]),
                    _label(_STATUS_SHORT[row["status"]]) + f" {_e(row['source']['checked_on'])}",
                ]
                for row in rows
            ],
        )
    return out


def render_html(packet: Mapping[str, Any]) -> str:
    """The packet as one page: no script, nothing loaded from anywhere, and every word that a
    publisher or a person wrote escaped as plain text. It prints in black and white, a page
    break before each section, with the agent, its version and digest and the time it was
    generated at the foot of every page."""
    agent = packet["agent"]
    name = str(agent["name"])
    if len(name) > _FOOT_NAME_CHARS:
        name = name[: _FOOT_NAME_CHARS - 1] + "…"
    # The foot of every page, and the page's title (a browser's own print header shows it).
    foot = (
        f"{name} · {agent['id']} version {agent['version']} · "
        f"sha256:{_hex(agent['digest'], 16)} · generated {_when(packet['generated_at'])}"
    )
    contents = "".join(
        f'<li><a href="#s-{_e(section["key"])}">{_e(section["title"])}</a></li>'
        for section in packet["sections"]
    )
    appendices = "".join(
        f'<li><a href="#s-{key}">Appendix {letter}. {_e(title)}</a></li>'
        for letter, (key, title) in zip("AB", APPENDICES, strict=True)
    )
    # A packet made from the agent's files alone names no organization and covers no days: it
    # says so on its cover, once, before anything else is read.
    alone = packet.get("no_deployment")
    if alone:
        covering = (
            "Deployment: none, made from the agent's files alone<br>"
            f"Generated {_when(packet['generated_at'])}<br>"
        )
    else:
        covering = (
            f"Organization: {_e(packet['organization'] or _EVERY_ORGANIZATION)}<br>"
            f"Generated {_when(packet['generated_at'])}, covering the last "
            f"{_e(packet['window_days'])} days<br>"
        )
    told = f'<p class="notice">{_e(alone)}</p>' if alone else ""
    cover = (
        '<header class="cover">'
        '<p class="meta">Committee packet</p>'
        f"<h1>{_e(agent['name'])}</h1>"
        f'<p class="meta">{_code(agent["id"])} version {_code(agent["version"])}<br>'
        f"Digest {_code(agent['digest'])}<br>"
        f"{covering}"
        f"By {_e(packet['generated_by']['software'])} {_e(packet['generated_by']['version'])}</p>"
        f'<p class="status">{_e(agent["status"])}</p>'
        f"<p>{_e(packet['notice'])}</p>"
        f"<p>{_e(packet['about'])}</p>{told}"
        "<p>How to read it: sections 1 to 4 say what the agent is and how it is held, 5 and 6 "
        "what has been measured and by whom, 7 to 11 how it is watched, changed, protected and "
        "approved. Appendix A lists what nobody has stated. Appendix B shows where each "
        "section relates to four published frameworks.</p>"
        f"<nav><ol>{contents}</ol><ul>{appendices}</ul></nav>"
        "</header>"
    )
    body = [cover]
    for section in packet["sections"]:
        body.append(
            f'<section id="s-{_e(section["key"])}"><h2>{_e(section["number"])}. '
            f"{_e(section['title'])}</h2>{_RENDER[section['key']](section['content'])}</section>"
        )
    body.append(
        '<section id="s-not_stated" class="appendix"><h2>Appendix A. Not stated</h2>'
        f"{_html_not_stated(packet['not_stated'], deployed=not alone)}</section>"
    )
    body.append(
        '<section id="s-crosswalk" class="appendix">'
        "<h2>Appendix B. Crosswalk to published frameworks</h2>"
        f"{_html_crosswalk(packet['crosswalk'])}"
        f'<p class="meta">This packet\'s own hash: {_code(packet["sha256"])}</p></section>'
    )
    csp = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        '<meta name="referrer" content="no-referrer">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(foot)} · Committee packet</title>"
        f"<style>{_STYLE}{_page_rule(foot)}</style></head><body>"
        + "".join(body)
        + f'<div class="foot">{_e(foot)} · Committee packet, prepared by the software: not a '
        "certification</div></body></html>\n"
    )
