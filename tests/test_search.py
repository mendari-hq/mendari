# SPDX-License-Identifier: Apache-2.0
"""FHIR search without a store: the parameter table, the parser, date and token semantics,
cursors and the searchset Bundle (standards/search.py)."""

from __future__ import annotations

import json
import random
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from mendari.standards import fhir_structure
from mendari.standards import search as S
from tests.conftest import ROOT

TASK, EVENTS, PROVENANCE, CODES = (
    S.SPECS[t] for t in ("Task", "AuditEvent", "Provenance", "CodeSystem")
)
BASE = "https://mendari.example/fhir"
SECRET = "SECRET-VALUE"
FIXTURE = ROOT / "tests" / "fixtures" / "fhir" / "r4-search-parameters.json"
R4_CORE = ROOT / "var" / "fhir-packages" / "hl7.fhir.r4.core-4.0.1.tgz"


def parse(items: list[tuple[str, str]], spec: S.ResourceSearch = TASK, **kw: Any) -> S.Search:
    return S.parse(spec, items, **kw)


def refused(
    items: list[tuple[str, str]], spec: S.ResourceSearch = TASK, **kw: Any
) -> list[dict[str, str]]:
    with pytest.raises(S.SearchError) as caught:
        S.parse(spec, items, **kw)
    return caught.value.issues


# -- the parameter table against R4 -----------------------------------------------------------


def r4_parameters() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["parameters"]


def test_every_supported_parameter_is_the_r4_search_parameter_it_claims() -> None:
    by_url = {p["url"]: p for p in r4_parameters()}
    for spec in S.SPECS.values():
        for param in spec.params:
            definition = by_url.get(param.definition)
            assert definition is not None, param.definition
            assert definition["code"] == param.name, param.definition
            assert definition["type"] == param.type, param.definition
            assert {spec.resource, "Resource", "DomainResource"} & set(definition["base"])
            assert param.documentation.strip()


def test_the_standard_parameters_are_r4s_for_each_type() -> None:
    """Unsupported standard parameters are a 400 (hybrid strictness): the list must be R4's."""
    parameters = r4_parameters()
    common = {p["code"] for p in parameters if {"Resource", "DomainResource"} & set(p["base"])}
    assert common == S.R4_COMMON
    for resource, codes in S.R4_PARAMETERS.items():
        assert codes == {p["code"] for p in parameters if resource in p["base"]}, resource
    for spec in S.SPECS.values():
        names = {p.name for p in spec.params}
        assert names <= S.R4_PARAMETERS[spec.resource] | S.R4_COMMON
        assert not names & spec.unsupported
        assert {"_sort", "_include", "_has"} <= spec.unsupported
    assert {"patient", "subject", "focus", "owner"} <= TASK.unsupported
    assert {"patient", "agent", "entity-name"} <= EVENTS.unsupported
    assert {"patient", "agent", "agent-type", "entity"} <= PROVENANCE.unsupported


@pytest.mark.skipif(not R4_CORE.exists(), reason="hl7.fhir.r4.core is not cached in var/")
def test_the_fixture_is_what_the_r4_core_package_says() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from r4_search_parameters import search_parameters
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    files: dict[str, Any] = {}
    with tarfile.open(R4_CORE) as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.startswith("package/SearchParameter-"):
                handle = archive.extractfile(member)
                files[member.name] = json.loads(handle.read()) if handle else {}
    assert search_parameters(files) == r4_parameters()


def test_capability_entries_come_from_the_table() -> None:
    entries = S.search_params(TASK)
    assert [e["name"] for e in entries] == [p.name for p in TASK.params]
    assert all(set(e) == {"name", "definition", "type", "documentation"} for e in entries)


# -- unknown, unsupported, modifiers ------------------------------------------------------------


def test_an_unknown_parameter_is_ignored_and_named_but_strict_refuses_it() -> None:
    search = parse([("stauts", SECRET), ("status", "completed")])
    assert search.ignored == ["stauts"]
    assert [c.name for c in search.clauses] == ["status"]
    assert search.used == [("status", "completed")]  # the self link leaves it out
    assert parse([("stauts", "x")], prefer="lenient").ignored == ["stauts"]
    issues = refused([("stauts", SECRET)], prefer="strict")
    assert issues[0]["code"] == "not-supported" and "stauts" in issues[0]["diagnostics"]


@pytest.mark.parametrize(
    ("spec", "name"),
    [
        (TASK, "patient"),
        (TASK, "subject"),
        (TASK, "focus"),
        (TASK, "owner"),
        (TASK, "_sort"),
        (TASK, "_include"),
        (TASK, "_security"),
        (EVENTS, "agent"),
        (EVENTS, "patient"),
        (PROVENANCE, "agent-type"),
        (CODES, "_lastUpdated"),
    ],
)
def test_a_standard_parameter_mendari_lacks_is_refused_unless_lenient(
    spec: S.ResourceSearch, name: str
) -> None:
    """Ignoring it would return every decision while the client believes it filtered."""
    issues = refused([(name, SECRET)], spec)
    assert issues[0]["code"] == "not-supported"
    assert name in issues[0]["diagnostics"] and "lenient" in issues[0]["diagnostics"]
    assert refused([(name, "x")], spec, prefer="strict")
    assert parse([(name, "x")], spec, prefer="lenient").ignored == [name]


@pytest.mark.parametrize("prefer", [None, "lenient", "strict"])
@pytest.mark.parametrize(
    ("spec", "name"),
    [
        (TASK, "status:not"),
        (TASK, "status:text"),
        (TASK, "authored-on:missing"),
        (EVENTS, "entity:Task"),
        (EVENTS, "entity.identifier"),
        (PROVENANCE, "target:missing"),
        (CODES, "url:below"),
        (TASK, "_count:x"),
    ],
)
def test_an_unsupported_modifier_or_chain_is_always_refused(
    spec: S.ResourceSearch, name: str, prefer: S.Handling | None
) -> None:
    assert refused([(name, "x")], spec, prefer=prefer)[0]["code"] == "not-supported"


def test_identifier_is_the_one_modifier() -> None:
    for spec, name in ((EVENTS, "entity"), (PROVENANCE, "target")):
        clause = parse([(f"{name}:identifier", "s|abc")], spec).clauses[0]
        assert (clause.modifier, clause.values) == ("identifier", (S.Token("s", "abc"),))


def test_presentation_and_empty_parameters_filter_nothing() -> None:
    search = parse([("_format", "json"), ("_pretty", "true"), ("status", ""), ("bogus", "")])
    assert (search.clauses, search.ignored, search.used) == ([], [], [])


def test_errors_and_warnings_name_parameters_never_values() -> None:
    bad = [
        ("authored-on", f"x{SECRET}"),
        ("_count", SECRET),
        ("_total", SECRET),
        ("cursor", SECRET),
        ("patient", SECRET),
        ("status:not", SECRET),
        ("_summary", SECRET),
    ]
    issues = refused(bad)
    assert len(issues) == len(bad)  # every problem, not the first
    assert SECRET not in json.dumps(issues)
    long_name = "x" * 500
    assert len(parse([(long_name, "1")]).ignored[0]) <= 64


def test_comma_is_or_and_repetition_is_and() -> None:
    search = parse([("status", "requested,failed"), ("status", "failed")])
    assert [len(c.values) for c in search.clauses] == [2, 1]


# -- result parameters --------------------------------------------------------------------------


def test_count_defaults_clamps_and_zero_means_count_only() -> None:
    assert parse([]).count == S.DEFAULT_COUNT == 50
    assert parse([("_count", "900")]).count == S.MAX_COUNT == 500
    zero = parse([("_count", "0")])
    assert (zero.count, zero.summary_count, zero.counted) == (0, True, True)
    for bad in ("-1", "abc", "1.5", "1e3", "9999999999"):
        assert refused([("_count", bad)])[0]["code"] == "value"
    assert refused([("_count", "1"), ("_count", "2")])[0]["code"] == "invalid"


def test_total_only_when_accurate_is_asked() -> None:
    assert not parse([]).counted
    assert parse([("_total", "accurate")]).counted
    assert not parse([("_total", "estimate")]).counted  # an estimate may be none at all
    assert not parse([("_total", "none")]).counted
    assert refused([("_total", "all")])[0]["code"] == "value"


def test_summary_count_and_false_only() -> None:
    assert parse([("_summary", "count")]).summary_count
    assert not parse([("_summary", "false")]).summary_count
    assert refused([("_summary", "true")])[0]["code"] == "not-supported"
    assert parse([("_summary", "data")], prefer="lenient").ignored == ["_summary"]


def test_prefer_handling() -> None:
    assert S.handling("handling=strict") == "strict"
    assert S.handling("return=minimal; handling=lenient") == "lenient"
    assert S.handling('respond-async, Handling="STRICT"') == "strict"
    assert S.handling("handling=other") is None
    assert S.handling(None) is None


# -- tokens and references ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "tokens"),
    [
        ("code", [S.Token(None, "code")]),
        ("sys|code", [S.Token("sys", "code")]),
        ("|code", [S.Token("", "code")]),
        ("sys|", [S.Token("sys", None)]),
        ("a,b", [S.Token(None, "a"), S.Token(None, "b")]),
        ("a\\,b", [S.Token(None, "a,b")]),
        ("s\\|x|c", [S.Token("s|x", "c")]),
        ("a\\\\,b", [S.Token(None, "a\\"), S.Token(None, "b")]),
        ("c\\$d", [S.Token(None, "c$d")]),
        ("http://x/y|a|b", [S.Token("http://x/y", "a|b")]),
    ],
)
def test_token_forms_and_escapes(raw: str, tokens: list[S.Token]) -> None:
    assert list(parse([("status", raw)]).clauses[0].values) == tokens


def test_an_empty_value_in_a_list_is_refused() -> None:
    assert refused([("status", "a,,b")])[0]["code"] == "value"


@pytest.mark.parametrize(
    ("raw", "reference"),
    [
        ("Task/abc-1", S.Reference(None, "Task", "abc-1")),
        ("abc-1", S.Reference(None, None, "abc-1")),
        (f"{BASE}/Task/abc", S.Reference(BASE, "Task", "abc")),
        ("http://host/Task/abc", S.Reference("http://host", "Task", "abc")),
        ("Patient/p1", S.Reference(None, "Patient", "p1")),
    ],
)
def test_reference_forms(raw: str, reference: S.Reference) -> None:
    assert parse([("entity", raw)], EVENTS).clauses[0].values == (reference,)


@pytest.mark.parametrize(
    "raw",
    ["Task/", "/abc", "a/b/c", "task/abc", "https://host/abc", "Task/a b", "Task/x/_history/2"],
)
def test_malformed_references_are_refused(raw: str) -> None:
    assert refused([("target", raw)], PROVENANCE)[0]["code"] == "value"


def test_uri_and_version() -> None:
    clause = parse([("url", "http://x/cs|1.0,http://x/other")], CODES).clauses[0]
    assert clause.values == (S.Uri("http://x/cs", "1.0"), S.Uri("http://x/other", None))


# -- dates ----------------------------------------------------------------------------------------


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, micro: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "low", "high"),
    [
        ("2026", utc(2026, 1, 1), utc(2027, 1, 1)),
        ("2026-02", utc(2026, 2, 1), utc(2026, 3, 1)),
        ("2026-12", utc(2026, 12, 1), utc(2027, 1, 1)),
        ("2026-10-01", utc(2026, 10, 1), utc(2026, 10, 2)),  # the UTC day
        ("2026-10-01T21:00-04:00", utc(2026, 10, 2, 1), utc(2026, 10, 2, 1, 1)),
        ("2026-10-01T21:00:05Z", utc(2026, 10, 1, 21, 0, 5), utc(2026, 10, 1, 21, 0, 6)),
        (
            "2026-10-01T21:00:05.5Z",
            utc(2026, 10, 1, 21, 0, 5, 500000),
            utc(2026, 10, 1, 21, 0, 5, 600000),
        ),
        (
            "2026-10-01T21:00:05.123456+00:00",
            utc(2026, 10, 1, 21, 0, 5, 123456),
            utc(2026, 10, 1, 21, 0, 5, 123457),
        ),
    ],
)
def test_a_date_is_a_utc_range_at_its_precision(raw: str, low: datetime, high: datetime) -> None:
    value = S.parse_date(raw)
    assert (value.prefix, value.low, value.high) == ("eq", low, high)


@pytest.mark.parametrize(
    "raw",
    [
        "2026-10-01T10:00",  # a time without a zone: dates here are UTC
        "2026-10-01T10:00:00",
        "ap2026",
        "2026-13",
        "2026-02-30",
        "26-01-01",
        "2026-10-01T10Z",
        "2026-10-01 10:00Z",
        "2026-10-01T10:00+5",
        "2026-10-01T10:00+15:00",
        "2026-10-01T25:00Z",
        "2026-10-01T10:00:00.1234567Z",
        "gt",
        "0001-01-01T00:00+14:00",  # before year 1 in UTC
        "9999",  # its end is past the last year
    ],
)
def test_invalid_dates_are_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        S.parse_date(raw)
    assert refused([("authored-on", raw)])[0]["code"] == "value"


def test_spec_examples() -> None:
    """search.html 3.1.1.4.7, with stored instants as points."""

    def matches(value: str, instant: str) -> bool:
        return S.date_matches(datetime.fromisoformat(instant), S.parse_date(value))

    assert matches("eq2013-01-14", "2013-01-14T10:00:00+00:00")
    assert not matches("eq2013-01-14", "2013-01-15T00:00:00+00:00")
    assert matches("ne2013-01-14", "2013-01-15T00:00:00+00:00")
    assert matches("lt2013-01-14T10:00Z", "2013-01-14T09:59:59.999+00:00")
    assert not matches("lt2013-01-14T10:00Z", "2013-01-14T10:00:00+00:00")
    assert matches("sa2013-03-14", "2013-03-15T00:00:00+00:00")
    assert not matches("sa2013-03-14", "2013-03-14T23:00:00+00:00")
    assert matches("eb2013-03-14", "2013-03-13T23:59:59.999+00:00")
    assert not matches("eb2013-03-14", "2013-03-14T00:00:00+00:00")


_FORMS = ("%Y", "%Y-%m", "%Y-%m-%d", "%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ")
_PREFIXES = ("eq", "ne", "gt", "lt", "ge", "le", "sa", "eb")


def random_value(rng: random.Random, start: datetime) -> str:
    moment = start + timedelta(seconds=rng.randrange(0, 400 * 86400))
    if rng.random() < 0.2:  # a fraction and an offset
        zone = rng.choice(("+05:30", "-04:00", "Z", "+00:00"))
        digits = rng.randint(1, 6)
        fraction = str(rng.randrange(10**digits)).zfill(digits)
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction}{zone}"
    return moment.strftime(rng.choice(_FORMS))


def test_date_prefixes_partition_every_instant() -> None:
    """For random instants and values: eq and ne, ge and lt, gt and le are complements; eq is
    ge and le; sa is gt and eb is lt. And the stored-text bounds agree with the semantics."""
    rng = random.Random(7)
    start = utc(2026, 1, 1)
    for _ in range(5000):
        instant = start + timedelta(
            seconds=rng.randrange(0, 400 * 86400), milliseconds=rng.randrange(1000)
        )
        value = random_value(rng, start)
        got = {p: S.date_matches(instant, S.parse_date(p + value)) for p in _PREFIXES}
        assert got["eq"] != got["ne"] and got["ge"] != got["lt"] and got["gt"] != got["le"]
        assert got["eq"] == (got["ge"] and got["le"])
        assert (got["gt"], got["lt"]) == (got["sa"], got["eb"])
        text = S.stored(instant)  # how the store keeps it: text, to the millisecond
        for prefix, expected in got.items():
            low, high, outside = S.bounds(S.parse_date(prefix + value))
            inside = (low is None or text >= low) and (high is None or text < high)
            assert (inside != outside) == expected, (prefix, value, text)


def test_stored_rounds_up_to_the_millisecond() -> None:
    assert S.stored(utc(2026, 1, 1, 0, 0, 0, 123400)) == "2026-01-01T00:00:00.124+00:00"
    assert S.stored(utc(2026, 1, 1, 0, 0, 0, 123000)) == "2026-01-01T00:00:00.123+00:00"


# -- cursors --------------------------------------------------------------------------------------


def test_a_cursor_round_trips_and_is_revalidated() -> None:
    key = {"t": "2026-10-01T14:00:00.004+00:00", "id": "2b3f6c1e-8d4a"}
    assert S.decode_cursor(TASK, S.encode_cursor(key)) == key
    assert parse([("cursor", S.encode_cursor(key))]).cursor == key
    assert S.decode_cursor(EVENTS, S.encode_cursor({"seq": 12})) == {"seq": 12}


@pytest.mark.parametrize(
    ("spec", "key"),
    [
        (TASK, {"t": "2026-10-01T14:00:00.004+00:00"}),  # a field missing
        (TASK, {"t": "2026-10-01T14:00:00.004+00:00", "id": "x", "org": "b"}),  # one more
        (TASK, {"t": "yesterday", "id": "x"}),
        (TASK, {"t": "2026-10-01T14:00:00.004+00:00", "id": "x' OR 1=1 --"}),
        (EVENTS, {"seq": -1}),
        (EVENTS, {"seq": True}),
        (EVENTS, {"seq": "12"}),
        (EVENTS, {"seq": 2**63}),
        (EVENTS, {"t": "2026-10-01T14:00:00.004+00:00", "id": "x"}),  # another type's
    ],
)
def test_a_tampered_cursor_is_refused(spec: S.ResourceSearch, key: dict[str, Any]) -> None:
    assert refused([("cursor", S.encode_cursor(key))], spec)[0]["code"] == "value"


@pytest.mark.parametrize("text", ["%%%", "bm90IGpzb24", "W10", "eyJzZXEiOjF9eyJ9"])
def test_a_cursor_that_is_not_one_is_refused(text: str) -> None:
    assert refused([("cursor", text)], EVENTS)[0]["code"] == "value"


# -- the Bundle -----------------------------------------------------------------------------------


def task(task_id: str) -> dict[str, Any]:
    return {"resourceType": "Task", "id": task_id, "status": "completed", "intent": "order"}


def query(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query)


def test_a_page_with_more_has_a_next_link_with_the_parameters_used() -> None:
    search = parse(
        [("status", "completed"), ("bogus", SECRET), ("_count", "900"), ("_total", "accurate")]
    )
    bundle = S.searchset(
        base=BASE + "/",
        search=search,
        resources=[task("a"), task("b")],
        total=7,
        next_key={"t": "2026-10-01T14:00:00.004+00:00", "id": "b"},
        timestamp="2026-10-02T00:00:00.000+00:00",
    )
    assert (bundle["type"], bundle["total"]) == ("searchset", 7)
    links = {link["relation"]: link["url"] for link in bundle["link"]}
    assert links["self"].startswith(f"{BASE}/Task?")
    assert query(links["self"]) == [
        ("status", "completed"),
        ("_total", "accurate"),
        ("_count", "500"),
    ]
    following = query(links["next"])
    assert following[:-1] == query(links["self"]) and following[-1][0] == "cursor"
    assert S.decode_cursor(TASK, following[-1][1])["id"] == "b"
    matches = [e for e in bundle["entry"] if e["search"]["mode"] == "match"]
    assert [e["fullUrl"] for e in matches] == [f"{BASE}/Task/a", f"{BASE}/Task/b"]
    (outcome,) = [e for e in bundle["entry"] if e["search"]["mode"] == "outcome"]
    issue = outcome["resource"]["issue"][0]
    assert (issue["severity"], issue["code"]) == ("warning", "not-supported")
    assert "bogus" in issue["diagnostics"] and SECRET not in json.dumps(bundle)
    assert outcome["fullUrl"].startswith("urn:uuid:")
    assert fhir_structure.check(bundle) == []


def test_a_continued_page_links_itself_with_its_cursor() -> None:
    cursor = S.encode_cursor({"seq": 4})
    search = parse([("subtype", "review.resolved"), ("cursor", cursor)], EVENTS)
    bundle = S.searchset(
        base=BASE,
        search=search,
        resources=[],
        total=None,
        next_key=None,
        timestamp="2026-10-02T00:00:00Z",
    )
    assert "total" not in bundle and "entry" not in bundle
    assert [link["relation"] for link in bundle["link"]] == ["self"]
    assert query(bundle["link"][0]["url"])[-1] == ("cursor", cursor)


@pytest.mark.parametrize("items", [[("_summary", "count")], [("_count", "0")]])
def test_count_only_has_the_total_and_no_entries(items: list[tuple[str, str]]) -> None:
    bundle = S.searchset(
        base=BASE,
        search=parse(items),
        resources=[task("a")],
        total=3,
        next_key={"t": "2026-10-01T14:00:00.004+00:00", "id": "a"},
        timestamp="2026-10-02T00:00:00Z",
    )
    assert bundle["total"] == 3
    assert "entry" not in bundle
    assert [link["relation"] for link in bundle["link"]] == ["self"]


def test_the_outcome_entry_is_the_same_for_the_same_names() -> None:
    def outcome(items: list[tuple[str, str]]) -> dict[str, Any]:
        bundle = S.searchset(
            base=BASE, search=parse(items), resources=[], total=None, next_key=None, timestamp="x"
        )
        return bundle["entry"][-1]

    assert outcome([("b", "1"), ("a", "2")]) == outcome([("a", "3"), ("b", "4"), ("a", "5")])
