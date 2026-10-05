"""FHIR search against the store (standards/fhir_search.py, AuditStore.search_*), on SQLite and
Postgres: what SQL finds is what filtering the rendered resources finds, pages neither repeat nor
skip, every page is scoped to the organization, and a search never writes."""

from __future__ import annotations

import dataclasses
import json
import os
import random
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import yaml

from mendari import AuditStore, Decision, DecisionResult, Outcome, Pack
from mendari.core import audit, tenancy
from mendari.core.db import PostgresSession, SqliteSession
from mendari.core.phi import PhiPolicy
from mendari.core.query import OneOf, Query, Within
from mendari.standards import audit_fhir, fhir_search, fhir_structure, task_fhir
from mendari.standards import search as S
from mendari.standards.audit_fhir import EVENT_SYSTEM
from mendari.standards.fhir import DECISION_ID_SYSTEM, OUTCOME_SYSTEM, PACK_TAG_SYSTEM
from mendari.standards.terminology import Terminology
from tests.conftest import INBOX_DIR, calm_jev, message

BASE = "https://mendari.example/fhir"
SECRET = "SECRET-NOTE-TEXT"
START = datetime(2026, 9, 28, tzinfo=UTC)
ORGS = ("clinic-a", "clinic-b", "default")
LABELS = ("front_desk", "nurse", "physician", "billing", "medical_assistant")
PACK_TAG = "mendari/patient-inbox-router"
PACK_VERSION = yaml.safe_load((INBOX_DIR / "pack.yaml").read_text())["version"]


class Clock:
    """The store's clock (`audit._now`), set by the test: event times are chosen, not now."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> str:
        return self.now.isoformat(timespec="milliseconds")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(audit, "_now", fake)
    return fake


@pytest.fixture(scope="module")
def templates(inbox_pack: Pack) -> list[DecisionResult]:
    """One decision of each kind, decided once: seeding copies them with new ids and times. A
    policy BLOCK (blocked with an answered primary question) is an AUTO with its outcome
    changed; the refused input has no primary confidence. The urgency screen runs with a fake
    Jev, so the routine message is automated."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        decide = Decision(inbox_pack, providers=calm_jev(monkeypatch))
        bodies = ("Can you send a refill?", "Crushing chest pain")
        kinds = [decide(message(body)) for body in bodies]
        kinds += [decide(message("hello?")), decide({"message": {}})]
    blocked = dataclasses.replace(kinds[0], outcome=Outcome.BLOCK)
    assert [k.outcome.value for k in kinds] == ["AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK"]
    assert kinds[3].confidence is None and blocked.confidence is not None
    return [*kinds, blocked]


def record(
    store: AuditStore,
    clock: Clock,
    template: DecisionResult,
    created: datetime,
    org: str,
    rng: random.Random,
) -> str:
    """A copy of `template` decided at `created` for `org`, maybe reviewed, given an outcome
    and rated, each at a later time."""
    result = dataclasses.replace(
        template,
        id=str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        created_at=created.isoformat(timespec="milliseconds"),
    )
    with tenancy.acting_for(org):
        clock.now = created + timedelta(milliseconds=rng.randrange(1, 5))
        store.record(result, message("synthetic"))
        final = not result.needs_human
        if result.needs_human and rng.random() < 0.6:
            clock.now = created + timedelta(minutes=rng.randrange(5, 2000))
            answer = result.answers.get(result.primary)
            labels = sorted((answer.probabilities if answer else None) or LABELS)
            store.resolve_review(
                result.id,
                final_label=rng.choice(labels),
                reviewer=rng.choice(("rn-1", "fd-2")),
                note=SECRET,
            )
            final = True
        if final and rng.random() < 0.4:
            clock.now += timedelta(minutes=rng.randrange(1, 600))
            store.record_outcome(
                result.id,
                final_label=rng.choice(LABELS),
                source="ehr",
                recorded_by="rn-9",
                labels=lambda pack_id, question: LABELS,  # a refused input has no answer
            )
        if rng.random() < 0.3:
            clock.now += timedelta(minutes=rng.randrange(1, 600))
            store.record_feedback(result.id, rating="up", note=SECRET)
    return result.id


def seed(
    store: AuditStore, clock: Clock, templates: list[DecisionResult], *, count: int, seed: int
) -> list[str]:
    """`count` decisions over three days across three organizations; about one in seven shares
    the previous one's time to the millisecond (paging breaks such ties by id)."""
    rng = random.Random(seed)
    ids: list[str] = []
    created = START
    for _ in range(count):
        if not ids or rng.random() > 0.15:
            created = START + timedelta(
                seconds=rng.randrange(3 * 86400), milliseconds=rng.randrange(1000)
            )
        ids.append(record(store, clock, rng.choice(templates), created, rng.choice(ORGS), rng))
    return ids


# -- the oracle: filter what is rendered --------------------------------------------------------

STATUS = S.TASK_STATUS_SYSTEM
PRIORITY = S.PRIORITY_SYSTEM


def _codings(concept: dict[str, Any]) -> list[tuple[str, str]]:
    return [(c["system"], c["code"]) for c in concept["coding"]]


def _identifier(reference: dict[str, Any]) -> tuple[str, str]:
    return reference["identifier"]["system"], reference["identifier"]["value"]


# What each token parameter reads in a rendered resource, as (system, code) pairs.
TOKENS: dict[tuple[str, str], Callable[[dict[str, Any]], list[tuple[str, str]]]] = {
    ("Task", "_id"): lambda r: [("", r["id"])],
    ("Task", "identifier"): lambda r: [(i["system"], i["value"]) for i in r["identifier"]],
    ("Task", "status"): lambda r: [(STATUS, r["status"])],
    ("Task", "business-status"): lambda r: _codings(r["businessStatus"]),
    ("Task", "priority"): lambda r: [(PRIORITY, r["priority"])],
    ("Task", "_tag"): lambda r: [(t["system"], t["code"]) for t in r["meta"]["tag"]],
    ("AuditEvent", "_id"): lambda r: [("", r["id"])],
    ("AuditEvent", "subtype"): lambda r: [(c["system"], c["code"]) for c in r["subtype"]],
    ("AuditEvent", "entity"): lambda r: [_identifier(e["what"]) for e in r["entity"]],
    ("Provenance", "_id"): lambda r: [("", r["id"])],
    ("Provenance", "target"): lambda r: [_identifier(t) for t in r["target"]],
}
DATES: dict[tuple[str, str], Callable[[dict[str, Any]], str]] = {
    ("Task", "authored-on"): lambda r: r["authoredOn"],
    ("Task", "_lastUpdated"): lambda r: r["meta"]["lastUpdated"],
    ("AuditEvent", "date"): lambda r: r["recorded"],
    ("AuditEvent", "_lastUpdated"): lambda r: r["meta"]["lastUpdated"],
    ("Provenance", "recorded"): lambda r: r["recorded"],
    ("Provenance", "_lastUpdated"): lambda r: r["meta"]["lastUpdated"],
}
REFERENCES: dict[tuple[str, str], Callable[[dict[str, Any]], list[str]]] = {
    ("AuditEvent", "entity"): lambda r: [e["what"]["reference"] for e in r["entity"]],
    ("Provenance", "target"): lambda r: [t["reference"] for t in r["target"]],
}


def satisfies(resource: dict[str, Any], search: S.Search) -> bool:
    """R4 search semantics, applied to the rendered resource: every clause, any of its values."""
    kind = resource["resourceType"]
    return all(
        any(_matches(resource, kind, clause, value) for value in clause.values)
        for clause in search.clauses
    )


def _matches(resource: dict[str, Any], kind: str, clause: S.Clause, value: Any) -> bool:
    key = (kind, clause.name)
    if isinstance(value, S.DateRange):
        return S.date_matches(datetime.fromisoformat(DATES[key](resource)), value)
    if isinstance(value, S.Reference):
        return any(_refers(url, value) for url in REFERENCES[key](resource))
    return any(
        value.system in (None, system) and value.code in (None, code)
        for system, code in TOKENS[key](resource)
    )


def _refers(url: str, value: S.Reference) -> bool:
    """A relative or bare search value resolves against this server's base; a Provenance
    target's version does not matter to an unversioned value."""
    base, kind, ident = re.sub(r"/_history/\d+$", "", url).rsplit("/", 2)
    return value.id == ident and value.type in (None, kind) and (value.base or BASE) == base


def rendered(store: AuditStore, ids: list[str], resource: str) -> list[dict[str, Any]]:
    """Every resource of a type this organization may read, rendered from `item()`, in the
    order search returns them."""
    items = [item for item in (store.item(i) for i in ids) if item is not None]
    if resource == "Task":
        tasks = [task_fhir.current_task(item, BASE) for item in items]
        return sorted(tasks, key=lambda t: (t["authoredOn"], t["id"]), reverse=True)
    found = []
    for item in items:
        versions = audit_fhir.task_versions(item["events"])
        for event in audit_fhir.joined(item):
            if resource == "AuditEvent":
                found.append((event["seq"], audit_fhir.audit_event(event, base=BASE)))
            elif event["event"] in audit_fhir.VERSION_EVENTS:
                version = versions[event["seq"]]
                found.append(
                    (event["seq"], audit_fhir.provenance(event, base=BASE, version=version))
                )
    return [resource for _, resource in sorted(found, key=lambda pair: pair[0])]


def walk(
    store: AuditStore,
    resource: str,
    items: list[tuple[str, str]],
    *,
    terminology: Terminology | None = None,
    between: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every page of a search, following each Bundle's `next` link as a client would; the
    resources found, and the Bundles."""
    found: list[dict[str, Any]] = []
    bundles: list[dict[str, Any]] = []
    while True:
        search = S.parse(S.SPECS[resource], items)
        page = fhir_search.find(store, search, base=BASE, terminology=terminology)
        bundle = S.searchset(
            base=BASE,
            search=search,
            resources=page.resources,
            total=page.total,
            next_key=page.next_key,
            timestamp="2026-10-02T00:00:00.000+00:00",
        )
        bundles.append(bundle)
        found += page.resources
        following = [link["url"] for link in bundle["link"] if link["relation"] == "next"]
        if not following:
            return found, bundles
        assert len(bundles) < 200, "a walk that does not end"
        items = parse_qsl(urlsplit(following[0]).query)
        if between is not None:
            between()


# -- random searches ----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Seeded:
    """What random searches draw values from: every decision and event (other organizations'
    too, which must never match), mostly those this organization sees, and their times."""

    ids: list[str]
    keys: list[tuple[str, int]]
    visible_ids: list[str]
    visible_keys: list[tuple[str, int]]
    times: list[datetime]

    def decision(self, rng: random.Random) -> str:
        return rng.choice(self.visible_ids if rng.random() < 0.8 else self.ids)

    def event(self, rng: random.Random) -> tuple[str, int]:
        return rng.choice(self.visible_keys if rng.random() < 0.8 else self.keys)


def random_date(rng: random.Random, seeded: Seeded) -> str:
    """A date value near the seeded times (most of them) at a random precision and prefix."""
    jitter = timedelta(seconds=rng.choice((0, 0, 1, 60, 3600, -1, -60)))
    moment = rng.choice(seeded.times) + jitter
    if rng.random() < 0.15:
        moment = START + timedelta(seconds=rng.randrange(-86400, 5 * 86400))
    form = rng.choice(("%Y-%m", "%Y-%m-%d", "%Y-%m-%d", "%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"))
    text = moment.strftime(form)
    if rng.random() < 0.2:  # to the millisecond, sometimes with an offset
        offset = rng.choice((timedelta(0), timedelta(hours=-4), timedelta(hours=5, minutes=30)))
        local = moment.astimezone(timezone(offset))
        text = local.isoformat(timespec="milliseconds").replace(
            "+00:00", rng.choice(("Z", "+00:00"))
        )
    return rng.choice(("", "eq", "ne", "gt", "lt", "ge", "le", "sa", "eb")) + text


def random_values(rng: random.Random, name: str, seeded: Seeded) -> str:
    decision = seeded.decision(rng)
    did, seq = seeded.event(rng)
    choices: dict[str, list[str]] = {
        "_id:Task": [decision, decision, f"|{decision}", "not-an-id", f"x|{decision}"],
        "identifier": [
            f"{DECISION_ID_SYSTEM}|{decision}",
            decision,
            f"other|{decision}",
            f"{DECISION_ID_SYSTEM}|",
        ],
        "status": [
            "requested",
            "completed",
            "failed",
            "ready",
            f"{STATUS}|completed",
            f"{STATUS}|",
            "x|failed",
            "|failed",
        ],
        "business-status": [
            "AUTO",
            "HUMAN_REVIEW",
            "ABSTAIN",
            "BLOCK",
            f"{OUTCOME_SYSTEM}|ABSTAIN",
            f"{OUTCOME_SYSTEM}|",
            "x|AUTO",
        ],
        "priority": ["routine", "stat", "urgent", f"{PRIORITY}|stat", "x|routine"],
        "_tag": [
            f"{PACK_TAG_SYSTEM}|{PACK_TAG}@{PACK_VERSION}",
            f"{PACK_TAG_SYSTEM}|{PACK_TAG}@9.9.9",
            f"{PACK_TAG_SYSTEM}|",
            "no-version",
        ],
        "_id:AuditEvent": [f"{did}-{seq}", f"{did}-{seq}", f"{did}-p{seq}", "x-1", "abc"],
        "_id:Provenance": [f"{did}-p{seq}", f"{did}-p{seq}", f"{did}-{seq}", "x-p1"],
        "subtype": [
            "decision.created",
            "review.resolved",
            f"{EVENT_SYSTEM}|outcome.recorded",
            "feedback.recorded",
            "input.read",
            f"{EVENT_SYSTEM}|",
            "x|decision.created",
        ],
        "reference": [
            f"Task/{decision}",
            decision,
            f"{BASE}/Task/{decision}",
            f"https://other.example/fhir/Task/{decision}",
            f"Patient/{decision}",
        ],
        "identifier-modifier": [
            f"{DECISION_ID_SYSTEM}|{decision}",
            decision,
            f"x|{decision}",
            f"{DECISION_ID_SYSTEM}|",
            f"|{decision}",
        ],
    }
    pool = choices.get(name)
    if pool is None:
        return ",".join(random_date(rng, seeded) for _ in range(rng.randint(1, 2)))
    return ",".join(rng.choice(pool) for _ in range(rng.randint(1, 2)))


PARAMETERS = {
    "Task": [
        ("_id", "_id:Task"),
        ("identifier", "identifier"),
        ("status", "status"),
        ("business-status", "business-status"),
        ("priority", "priority"),
        ("_tag", "_tag"),
        ("authored-on", "date"),
        ("_lastUpdated", "date"),
    ],
    "AuditEvent": [
        ("_id", "_id:AuditEvent"),
        ("date", "date"),
        ("_lastUpdated", "date"),
        ("subtype", "subtype"),
        ("entity", "reference"),
        ("entity:identifier", "identifier-modifier"),
    ],
    "Provenance": [
        ("_id", "_id:Provenance"),
        ("recorded", "date"),
        ("_lastUpdated", "date"),
        ("target", "reference"),
        ("target:identifier", "identifier-modifier"),
    ],
}


def random_search(rng: random.Random, resource: str, seeded: Seeded) -> list[tuple[str, str]]:
    chosen = rng.sample(PARAMETERS[resource], rng.choice((1, 1, 2, 2, 3)))
    items = [(name, random_values(rng, kind, seeded)) for name, kind in chosen]
    if rng.random() < 0.5:
        items.append(("_total", "accurate"))
    items.append(("_count", rng.choice(("2", "7", "500"))))
    return items


@pytest.mark.parametrize("org", ["clinic-a", None])
def test_sql_finds_what_filtering_the_rendered_resources_finds(
    store: AuditStore, clock: Clock, templates: list[DecisionResult], org: str | None
) -> None:
    """The key property: for random searches over seeded decisions and events, the store's
    answer, page by page, is exactly the rendered resources that satisfy the search in Python,
    in order, with the same total."""
    ids = seed(store, clock, templates, count=28, seed=11)
    keys = [(e["decision_id"], e["seq"]) for e in store.iter_events()]  # open mode: all
    rng = random.Random(f"oracle-{org}")
    with tenancy.acting_for(org):
        universe = {r: rendered(store, ids, r) for r in PARAMETERS}
        seeded = Seeded(
            ids,
            keys,
            [t["id"] for t in universe["Task"]],
            [(e["decision_id"], e["seq"]) for e in store.iter_events()],
            [
                datetime.fromisoformat(r["meta"]["lastUpdated"])
                for rs in universe.values()
                for r in rs
            ],
        )
        selective = 0
        for resource in PARAMETERS:
            for _ in range(60):
                items = random_search(rng, resource, seeded)
                search = S.parse(S.SPECS[resource], items)
                expected = [r for r in universe[resource] if satisfies(r, search)]
                found, bundles = walk(store, resource, items)
                assert found == expected, items
                if search.total:
                    assert {b["total"] for b in bundles} == {len(expected)}, items
                selective += 0 < len(expected) < len(universe[resource])
        assert selective >= 45  # a quarter of the searches keep some rows and drop some


def test_rendered_and_searched_status_agree_on_every_kind(
    store: AuditStore, clock: Clock, templates: list[DecisionResult]
) -> None:
    """Task.status renders from `task_status` and searches with the store's SQL predicate."""
    ids = seed(store, clock, templates, count=30, seed=3)
    tasks = rendered(store, ids, "Task")
    assert {t["status"] for t in tasks} == {"requested", "completed", "failed"}
    assert {
        t["businessStatus"]["coding"][0]["code"] for t in tasks if t["status"] == "completed"
    } >= {
        "AUTO",
        "BLOCK",  # a policy BLOCK is completed; only a refused input failed
    }
    for status in ("requested", "completed", "failed"):
        found, _ = walk(store, "Task", [("status", status)])
        assert [t["id"] for t in found] == [t["id"] for t in tasks if t["status"] == status]


def test_last_updated_is_the_latest_version(
    store: AuditStore, clock: Clock, templates: list[DecisionResult]
) -> None:
    """Feedback chains an event but makes no version: it must not move _lastUpdated."""
    result = dataclasses.replace(
        templates[1], id=str(uuid.uuid4()), created_at=f"{START:%Y-%m-%dT%H:%M}:00.000+00:00"
    )
    store.record(result, message("synthetic"))
    clock.now = START + timedelta(hours=2)
    store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")
    clock.now = START + timedelta(days=1)
    store.record_feedback(result.id, rating="down", note=SECRET)
    (task,) = rendered(store, [result.id], "Task")
    assert task["meta"]["lastUpdated"] == "2026-09-28T02:00:00.000+00:00"
    for value, hit in (
        ("eq2026-09-28T02:00:00Z", True),
        ("gt2026-09-28T01:59Z", True),
        ("lt2026-09-29", True),
        ("ge2026-09-29", False),
    ):
        found, _ = walk(store, "Task", [("_lastUpdated", value)])
        assert [t["id"] for t in found] == ([result.id] if hit else []), value


# -- paging -------------------------------------------------------------------------------------


def test_task_pages_neither_repeat_nor_skip_while_decisions_arrive(
    store: AuditStore, clock: Clock, templates: list[DecisionResult]
) -> None:
    ids = seed(store, clock, templates, count=20, seed=5)
    rng = random.Random(9)
    with tenancy.acting_for("clinic-a"):

        def arrive() -> None:  # newer decisions, here and elsewhere, and reviews of their own
            later = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(seconds=rng.randrange(86400))
            ids.append(record(store, clock, rng.choice(templates), later, rng.choice(ORGS), rng))

        for count in ("1", "2", "3"):
            before = [t["id"] for t in rendered(store, ids, "Task")]
            found, bundles = walk(store, "Task", [("_count", count)], between=arrive)
            assert [t["id"] for t in found] == before, count  # the newer ones sort before
            assert len(bundles) == -(-len(before) // int(count))


def test_event_pages_neither_repeat_nor_skip_while_events_are_chained(
    store: AuditStore, clock: Clock, templates: list[DecisionResult]
) -> None:
    ids = seed(store, clock, templates, count=12, seed=8)
    rng = random.Random(4)
    with tenancy.acting_for("clinic-b"):
        before = [r["id"] for r in rendered(store, ids, "AuditEvent")]
        mine = [i for i in ids if store.item(i) is not None]

        def chain() -> None:
            store.record_feedback(rng.choice(mine), rating="up")

        found, _ = walk(store, "AuditEvent", [("_count", "3")], between=chain)
        walked = [r["id"] for r in found]
        assert len(walked) == len(set(walked))
        assert walked[: len(before)] == before  # all of them, in order; then the new ones
        assert walked == [r["id"] for r in rendered(store, ids, "AuditEvent")]


def test_a_cursor_replayed_by_another_organization_shows_only_its_own(
    store: AuditStore, clock: Clock, templates: list[DecisionResult]
) -> None:
    """The cursor is not signed: tenancy is applied again on every page."""
    ids = seed(store, clock, templates, count=24, seed=21)
    for resource in ("Task", "AuditEvent", "Provenance"):
        with tenancy.acting_for("clinic-a"):
            _, bundles = walk(store, resource, [("_count", "2")])
            assert len(bundles) > 1
            following = next(x["url"] for x in bundles[0]["link"] if x["relation"] == "next")
            cursor = dict(parse_qsl(urlsplit(following).query))["cursor"]
        with tenancy.acting_for("clinic-b"):
            theirs = {r["id"] for r in rendered(store, ids, resource)}
            found, _ = walk(store, resource, [("_count", "500"), ("cursor", cursor)])
            assert found and {r["id"] for r in found} <= theirs, resource


def test_count_only_and_totals(
    store: AuditStore, clock: Clock, templates: list[DecisionResult]
) -> None:
    ids = seed(store, clock, templates, count=15, seed=2)
    with tenancy.acting_for("clinic-a"):
        expected = len(rendered(store, ids, "Provenance"))
        for items in (
            [("_summary", "count")],
            [("_count", "0")],
            [("_total", "accurate"), ("_count", "1")],
        ):
            found, bundles = walk(store, "Provenance", items)
            assert bundles[0]["total"] == expected, items
        found, bundles = walk(store, "Provenance", [("_count", "0")])
        assert (found, len(bundles), "entry" in bundles[0]) == ([], 1, False)
        _, bundles = walk(store, "Provenance", [("_count", "500")])
        assert "total" not in bundles[0]


# -- what a search costs, and what it never does -----------------------------------------------


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every SQL statement the store runs, from now on."""
    ran: list[str] = []
    for session in (SqliteSession, PostgresSession):
        original = session.all

        def counted(self: Any, sql: str, params: Any = (), _original: Any = original) -> Any:
            ran.append(sql)
            return _original(self, sql, params)

        monkeypatch.setattr(session, "all", counted)
    return ran


def test_a_page_costs_the_same_statements_however_long(
    store: AuditStore, clock: Clock, templates: list[DecisionResult], statements: list[str]
) -> None:
    """One query for the page, one for its events, one for the total: no query per entry
    (each would be a round trip to Neon)."""
    seed(store, clock, templates, count=16, seed=6)
    costs = {}
    for resource in ("Task", "AuditEvent", "Provenance"):
        for count in ("2", "15"):
            statements.clear()
            search = S.parse(S.SPECS[resource], [("_count", count), ("_total", "accurate")])
            found = fhir_search.find(store, search, base=BASE)
            assert len(found.resources) == int(count)
            costs[(resource, count)] = len(statements)
        assert costs[(resource, "2")] == costs[(resource, "15")] <= 3, costs


@pytest.mark.parametrize("mode", ["off", "encrypt"])
def test_a_search_never_writes(
    db_target: str, clock: Clock, templates: list[DecisionResult], mode: str
) -> None:
    """Under PHI `encrypt`, `get(reader=...)` chains an `input.read`: search never calls it."""
    key = os.urandom(32) if mode == "encrypt" else b""
    with AuditStore(db_target, phi=PhiPolicy(mode=mode, key=key)) as store:  # type: ignore[arg-type]
        seed(store, clock, templates, count=8, seed=1)
        length = store.verify()["events"]
        for resource in ("Task", "AuditEvent", "Provenance"):
            found, _ = walk(store, resource, [("_count", "3"), ("_total", "accurate")])
            assert found
            assert SECRET not in json.dumps(found)
            assert "synthetic" not in json.dumps(found)
        after = store.verify()
        assert after["ok"] and after["events"] == length


def test_searchsets_are_structurally_valid_r4(
    store: AuditStore, clock: Clock, templates: list[DecisionResult]
) -> None:
    seed(store, clock, templates, count=10, seed=12)
    for resource in ("Task", "AuditEvent", "Provenance"):
        _, bundles = walk(
            store, resource, [("_count", "4"), ("bogus", "1"), ("_total", "accurate")]
        )
        for bundle in bundles:
            assert fhir_structure.check(bundle) == [], resource
            assert all(
                e["search"]["mode"] == "match" or e["resource"]["issue"][0]["severity"] == "warning"
                for e in bundle["entry"]
            )


# -- the store's own queries --------------------------------------------------------------------


def test_store_conditions(store: AuditStore, clock: Clock, templates: list[DecisionResult]) -> None:
    ids = seed(store, clock, templates, count=10, seed=13)
    every = store.search_decisions(Query(limit=500))
    assert len(every.rows) == len(ids) and every.after is None
    assert store.search_decisions(Query(where=((),), limit=500)).rows == []  # an empty OR
    assert store.search_decisions(Query(where=((OneOf("id", frozenset()),),), limit=500)).rows == []
    one = store.search_decisions(Query(where=((OneOf("id", frozenset({ids[0]})),),), count=True))
    assert ([r["id"] for r in one.rows], one.total) == ([ids[0]], 1)
    middle = sorted(r["created_at"] for r in every.rows)[5]
    inside = store.search_decisions(Query(where=((Within("created", low=middle),),), limit=500))
    outside = store.search_decisions(
        Query(where=((Within("created", low=middle, outside=True),),), limit=500)
    )
    assert len(inside.rows) + len(outside.rows) == len(ids)
    assert all(r["created_at"] >= middle for r in inside.rows)
    counted = store.search_events(
        Query(where=((OneOf("kind", frozenset({"decision.created"})),),), limit=0, count=True)
    )
    assert (counted.rows, counted.total) == ([], len(ids))
    with pytest.raises(KeyError):
        store.search_decisions(Query(where=((OneOf("org", frozenset({"x"})),),)))


# -- CodeSystem, in memory -----------------------------------------------------------------------


def test_code_system_search(store: AuditStore, inbox_pack: Pack) -> None:
    terminology = Terminology.for_packs([inbox_pack])
    systems = sorted(
        (r for r in terminology.resources if r["resourceType"] == "CodeSystem"),
        key=lambda r: r["id"],
    )
    outcome = next(r for r in systems if r["url"] == OUTCOME_SYSTEM)

    def ids(items: list[tuple[str, str]]) -> list[str]:
        found, _ = walk(store, "CodeSystem", items, terminology=terminology)
        return [r["id"] for r in found]

    assert ids([("url", OUTCOME_SYSTEM)]) == [outcome["id"]]
    assert ids([("url", f"{OUTCOME_SYSTEM}|{outcome['version']}")]) == [outcome["id"]]
    assert ids([("url", f"{OUTCOME_SYSTEM}|0.0.0")]) == []
    assert ids([("version", outcome["version"]), ("url", OUTCOME_SYSTEM)]) == [outcome["id"]]
    assert ids([("_id", outcome["id"])]) == [outcome["id"]]
    assert ids([("_count", "2")]) == [r["id"] for r in systems]  # every page, in id order
    _, bundles = walk(store, "CodeSystem", [("_summary", "count")], terminology=terminology)
    assert bundles[0]["total"] == len(systems)
