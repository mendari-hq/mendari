# SPDX-License-Identifier: Apache-2.0
"""A site code map per organization (LP-23i, `core/site_codes.py`, `MENDARI_SITE_CODES`): a
site's own codes translate into the codes an agent's code sets declare, one organization at a
time. Unset, nothing changes. Set, it is checked at startup against the agents found, a
translation lands only on a code its set declares, each FHIR decision records its organization's
map by version and SHA-256 (and its data coverage reads with the same translations), another
organization's map is never applied, any change to a map changes its digest, and a decision made
through a translation never automates.

On the chart-summary test fixture (`mendari-test/chart-summary`), whose code set
`follow_up_conditions` holds the ICD-10-CM codes of the conditions it follows up, and on a copy
of it (`site_agent`) that reads a condition's follow-up flag on two paths and has a second code
set. Every site code here is fictional, in the fixture's made-up site system; synthetic data,
not clinically reviewed."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_agent
from mendari.cli import main
from mendari.core import tenancy
from mendari.core.policy import GovernanceOverride
from mendari.core.site_codes import (
    ENV,
    FORMAT,
    MAX_TRANSLATIONS,
    SiteCodeMaps,
    canonical,
    check,
    parse,
    site_codes,
)
from mendari.core.spec import Outcome
from mendari.errors import MendariError
from mendari.server.app import create_app
from mendari.standards import data_coverage, fhir
from mendari.standards.fhir import CodeSet, FhirAdapter
from tests.conftest import CHART_DIR, FIXTURE_AGENTS, TRIAGE_DIR, encrypt_mode
from tests.support.chart_fixture import CHART_ID, COPY_ID, SERVICE, chart_copy, hook
from tests.support.fixture_records import (
    CODE_SET,
    ICD10CM,
    SHOWCASE,
    SITE_CONDITION_CODES,
    SITE_DIAGNOSES,
)

ICD = ICD10CM
SITE = SITE_DIAGNOSES  # the fixture's fictional site's own diagnosis codes
SET = CODE_SET  # `follow_up_conditions`: the agent's own codes for the conditions it follows up
OTHER_SET = "self_limited"  # the copy's second set (`site_agent`)
SITE_CODE = SITE_CONDITION_CODES["E11.9"]  # the site's own code for type 2 diabetes (fictional)
OTHER_CODE = "DX-1091"  # another site's code for it (fictional)
T2D = {"system": SITE, "code": SITE_CODE, "as": "E11.9"}
VERSION = "2026-10-19.1"
DECIDE = f"/api/agents/{CHART_ID}/fhir/decide"
COPY_DECIDE = f"/api/agents/{COPY_ID}/fhir/decide"
PATIENT_ID = "site-codes-1"
# The words the agent's mapping writes for a condition of its code set (`flag`'s map), which its
# canned answers read: `flag-routine` for E11.9 and I10, `flag-soon` for I50.9.
MAPPING = yaml.safe_load((CHART_DIR / "fhir" / "mapping.yaml").read_text(encoding="utf-8"))
FLAGS = MAPPING["lists"]["conditions"]["fields"]["flag"]["map"]
# The copy's conditions: the follow-up flag from a condition's code, else from its evidence (two
# paths, through the agent's code set), and a code of its second set, in a field of its own.
CONDITIONS = {
    "resource": "Condition",
    "fields": {
        "flag": {"paths": ["code.coding", "evidence.code.coding"], "code_set": SET, "map": FLAGS},
        OTHER_SET: {"path": "code.coding", "code_set": OTHER_SET},
    },
}
SELF_LIMITED = {"system": ICD, "codes": ["J06.9", "M54.50"]}


def site_agent(
    base: Path, *, level: int = 1, manifest: Callable[[dict[str, Any]], None] | None = None
) -> Path:
    """A copy of the chart-summary agent under its own id (`mendari-test/chart-copy`) in
    `base/agents`, at risk `level` (1 may automate): it reads a condition's follow-up flag from
    its code, else from its evidence (two paths, its code set `follow_up_conditions`), and a
    code of its second set, `self_limited`, into a field of its own."""

    def edited(data: dict[str, Any]) -> None:
        data["risk"]["level"] = level
        if manifest is not None:
            manifest(data)

    def schema(document: dict[str, Any]) -> None:
        items = document["properties"]["conditions"]["items"]["properties"]
        items[OTHER_SET] = {"type": "string", "maxLength": 64}

    changes = {"code_sets": {OTHER_SET: SELF_LIMITED}, "lists": {"conditions": CONDITIONS}}
    return chart_copy(base / "agents", changes, manifest=edited, schema=schema)


def coded(
    system: str,
    code: str,
    *,
    evidence: tuple[str, str] | None = None,
    also: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """A synthetic chart: an adult Patient and a Condition coded `system`/`code` (with an
    evidence coding), then a Condition for each of `also`."""
    subject = {"reference": f"Patient/{PATIENT_ID}"}
    patient = {
        "resourceType": "Patient",
        "id": PATIENT_ID,
        "gender": "female",
        "birthDate": "1961-03-01",
    }
    conditions: list[dict[str, Any]] = [
        {
            "resourceType": "Condition",
            "id": f"c{n}",
            "subject": subject,
            "code": {"coding": [{"system": given_system, "code": given_code}]},
        }
        for n, (given_system, given_code) in enumerate(((system, code), *also), start=1)
    ]
    if evidence is not None:
        conditions[0]["evidence"] = [
            {"code": [{"coding": [{"system": evidence[0], "code": evidence[1]}]}]}
        ]
    entries = [{"resource": resource} for resource in (patient, *conditions)]
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def message(body: str) -> dict[str, Any]:
    """A synthetic patient message as the triage fixture reads it from FHIR: a Communication."""
    communication = {
        "resourceType": "Communication",
        "id": "message-1",
        "status": "completed",
        "payload": [{"contentString": body}],
    }
    return {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": communication}]}


def mapped(sets: dict[str, Any], *, org: str = "default", version: str = VERSION) -> dict[str, Any]:
    return {org: {"version": version, "sets": sets}}


def maps_of(given: dict[str, Any], *, signed_in: bool = False) -> SiteCodeMaps:
    return parse(json.dumps(given), signed_in=signed_in)


def decision_of(root: Path, maps: SiteCodeMaps | None = None, **kwargs: Any) -> Decision:
    agent = load_agent(root)
    if maps is not None:
        check(maps, [agent])
    return Decision(agent, site_codes=maps, **kwargs)


def searched(tmp_path: Path) -> list[Path]:
    """The test fixtures' agents, and this test's copy of the chart agent when it made one."""
    copies = tmp_path / "agents"
    return [FIXTURE_AGENTS, *([copies] if copies.is_dir() else [])]


def serve(tmp_path: Path, name: str = "a.db") -> TestClient:
    return TestClient(create_app(db_path=tmp_path / name, agents_paths=searched(tmp_path)))


# -- unset ------------------------------------------------------------------------------------


def test_unset_means_every_decision_as_before(tmp_path: Path) -> None:
    """With `MENDARI_SITE_CODES` unset (the suite removes it, `LIVE_SETTINGS`) there is no map,
    health says so, and every decision the server makes, FHIR or JSON, is the engine's without a
    map: the same outcome, reasons and data coverage, `site_codes` null. A site's own code is
    not read as the agent's. (The FHIR golden, `tests/test_fhir_decisions.py`, pins the test
    fixtures' FHIR decisions, unchanged.)"""
    agent = load_agent(CHART_DIR)
    assert site_codes([agent], signed_in=False) is None
    with serve(tmp_path) as app:
        assert app.get("/api/health").json()["site_codes"] is None
        for record in (coded(ICD, "E11.9"), coded(SITE, SITE_CODE)):
            answer = app.post(DECIDE, json=record).json()["decision"]
            engine, _ = Decision(agent).decide_fhir(record)
            assert answer["site_codes"] is None and engine.site_codes is None
            assert answer["outcome"] == engine.outcome.value
            assert answer["recommendation"] == engine.recommendation
            assert answer["reasons"] == list(engine.reasons)
            assert answer["data_coverage"] == engine.data_coverage
        own, site = (
            Decision(agent).decide_fhir(record)[0]
            for record in (coded(ICD, "E11.9"), coded(SITE, SITE_CODE))
        )
        assert own.outcome is Outcome.AUTO and own.recommendation == "routine"
        assert site.answers["follow_up"].label == "none"  # the site's code is not the agent's
        plain = app.post(f"/api/agents/{CHART_ID}/decide", json=SHOWCASE.input()).json()
        assert plain["site_codes"] is None


# -- the setting, checked at startup ----------------------------------------------------------

BAD: dict[str, tuple[Any, str]] = {
    "not JSON": ("{not json", "is not JSON"),
    "neither JSON nor a file": ("[1]", "neither inline JSON"),
    "no organization": ({}, "1 to 100 organizations"),
    "a key twice": (
        '{"default": {"version": "v1", "sets": {"follow_up_conditions": []}}, "default": {}}',
        "gives a key twice",
    ),
    "an organization with a space": (mapped({SET: [T2D]}, org="a b"), "visible ASCII"),
    "another organization with sign-in off": (
        mapped({SET: [T2D]}, org="clinic-a"),
        "with sign-in off, the one organization is default",
    ),
    "a map that is a list": ({"default": [T2D]}, "an object of version and sets"),
    "a field of its own": (
        {"default": {"version": VERSION, "sets": {SET: [T2D]}, "owner": "it"}},
        "an object of version and sets",
    ),
    "a version with a space": (mapped({SET: [T2D]}, version="v 1"), "version must be"),
    "no set": (mapped({}), "one or more code sets"),
    "a set name not as an agent writes one": (mapped({"Follow_up": [T2D]}), "code set 1's name"),
    "no translation": (mapped({SET: []}), "one or more translations"),
    "a translation of its own key": (
        mapped({SET: [{**T2D, "display": "Type 2"}]}),
        "translation 1 must be an object of system, code and as",
    ),
    "a system that is no uri": (mapped({SET: [{**T2D, "system": "SITE"}]}), "must be a uri"),
    "a code with a space at its end": (
        mapped({SET: [{**T2D, "code": f"{SITE_CODE} "}]}),
        "code must be 1 to 256 printable",
    ),
    "an as that is no text": (mapped({SET: [{**T2D, "as": 5}]}), "as must be 1 to 256"),
    "a site code twice": (
        mapped({SET: [T2D, {**T2D, "as": "I10"}]}),
        "translation 2 translates a site code the set translates already",
    ),
    "too many": (
        mapped({SET: [{**T2D, "code": f"S{n}"} for n in range(MAX_TRANSLATIONS + 1)]}),
        "more than 10,000 translations",
    ),
    "a set no agent declares": (mapped({"allergies": [T2D]}), "code set 'allergies': no agent"),
    "an as the set does not declare": (
        mapped({SET: [T2D, {**T2D, "code": OTHER_CODE, "as": "E11.0"}]}),
        "code set 'follow_up_conditions', translation 2: it translates into a code the set does "
        "not declare",
    ),
    "a code of another set": (
        mapped({SET: [{**T2D, "as": "J06.9"}]}),
        "translation 1: it translates into a code the set does not declare",
    ),
    "a file that is not there": ("/nonexistent/site-codes.json", "neither inline JSON"),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_the_map_is_checked_at_startup(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each thing wrong in the setting stops the server at startup, named by the organization,
    the code set and the translation's position, never by a system or a code; the setting is
    checked against the code sets of the agents found. Inline JSON and a file give the same map,
    and a signed-in server takes any organization."""
    root = site_agent(tmp_path)
    given, problem = BAD[case]
    monkeypatch.setenv(ENV, given if isinstance(given, str) else json.dumps(given))
    with pytest.raises(MendariError) as refused:
        site_codes([load_agent(root)], signed_in=False)
    message = str(refused.value)
    assert problem in message and message.startswith(ENV)
    assert SITE_CODE not in message and SITE not in message and "E11" not in message
    with pytest.raises(MendariError, match=ENV):
        create_app(db_path=tmp_path / "a.db", agents_paths=searched(tmp_path))


def test_the_setting_is_inline_json_or_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = load_agent(CHART_DIR)
    text = json.dumps(mapped({SET: [T2D]}))
    monkeypatch.setenv(ENV, text)
    inline = site_codes([agent], signed_in=False)
    path = tmp_path / "site-codes.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(ENV, str(path))
    from_file = site_codes([agent], signed_in=False)
    assert inline is not None and from_file is not None
    assert from_file.maps["default"] == inline.maps["default"]
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(MendariError, match="not UTF-8"):
        site_codes([agent], signed_in=False)
    signed = maps_of(mapped({SET: [T2D]}, org="clinic-a"), signed_in=True)
    assert list(signed.maps) == ["clinic-a"]


# -- translations ------------------------------------------------------------------------------


def test_a_site_code_is_translated_only_into_a_code_its_set_declares(tmp_path: Path) -> None:
    """At startup a translation must land on a code its set declares (above). Read past that
    check, a translation into a code its set does not list, or into another set's code, gives
    the field nothing and translates nothing; a translation feeds its own set's fields only; and
    the set's own code wins over a translated one wherever it is, on the same path or a later
    one (spec 0.3 §2.5), so a record whose own codes give a field its value reads the same with
    the map as without it. A translated code counts only when no path gives an own one, the
    first path's then."""
    adapter = FhirAdapter.for_agent(load_agent(site_agent(tmp_path)))

    def read(record: dict[str, Any], sets: dict[str, Any]) -> tuple[list[Any], tuple[str, ...]]:
        mapped_in = adapter.read(adapter.index(record), site_codes=sets)
        return mapped_in.value["conditions"], mapped_in.translated

    site = coded(SITE, SITE_CODE)
    assert read(site, {}) == ([], ())
    assert read(site, {SET: [T2D]}) == ([{"flag": "flag-routine"}], (SET,))
    unlisted = {SET: [{**T2D, "as": "E11.0"}], OTHER_SET: [{**T2D, "as": "E11.9"}]}
    assert read(site, unlisted) == ([], ())  # neither is a code of its own set
    assert read(site, {OTHER_SET: [{**T2D, "as": "J06.9"}]}) == (
        [{OTHER_SET: "J06.9"}],
        (OTHER_SET,),
    )
    both = coded(SITE, SITE_CODE, evidence=(ICD, "I50.9"))  # the own code on the fallback path
    assert read(both, {SET: [T2D]}) == read(both, {}) == ([{"flag": "flag-soon"}], ())
    later = coded(ICD, "E10.9", evidence=(SITE, SITE_CODE))  # a site code on the fallback only
    assert read(later, {SET: [T2D]}) == ([{"flag": "flag-routine"}], (SET,))
    twice = coded(SITE, SITE_CODE, evidence=(SITE, OTHER_CODE))
    other = {**T2D, "code": OTHER_CODE, "as": "I50.9"}
    assert read(twice, {SET: [T2D, other]}) == ([{"flag": "flag-routine"}], (SET,))
    own = coded(ICD, "I50.9")
    assert read(own, {SET: [{**T2D, "code": "I50.9", "system": ICD, "as": "E11.9"}]}) == (
        [{"flag": "flag-soon"}],
        (),
    )


def test_the_data_coverage_reads_with_the_same_translations_as_the_input(tmp_path: Path) -> None:
    """The data-coverage record of a decision made with a map reads the record with the same
    translations as the input (the 23g merge's note): a field whose first path a translation
    gives a value reads its fallback path for the set's own code, as the input does, and a
    condition coded only by the site counts as an item, as the input has it. Without the map
    the record says what the input read without it, the same as with the map when the record's
    own code gives the field its value."""
    root = site_agent(tmp_path)
    maps = maps_of(mapped({SET: [T2D]}))
    decision = decision_of(root, maps)
    adapter = FhirAdapter.for_agent(decision.agent)
    systems = [use.url for use in decision.agent.manifest.code_systems]  # as the decision's
    assert ICD in systems and SITE not in systems  # the site's system is not the agent's
    for record, values, same in (
        (coded(SITE, SITE_CODE, evidence=(ICD, "I10")), 2, True),  # both paths, the own code
        (coded(SITE, SITE_CODE), 1, False),  # the fallback path finds nothing
    ):
        result, _ = decision.decide_fhir(record)
        assert result.data_coverage is not None
        indexed = adapter.index(record)
        translations = maps.maps["default"].sets
        with_map = data_coverage.measure(
            adapter.mapping,
            indexed,
            unreadable={},
            systems=systems,
            sources={},
            site_codes=translations,
        )
        without = data_coverage.measure(
            adapter.mapping, indexed, unreadable={}, systems=systems, sources={}
        )
        conditions = result.data_coverage["lists"]["conditions"]
        assert conditions == with_map["lists"]["conditions"]
        assert (
            conditions["items"]
            == 1
            == len(adapter.read(indexed, site_codes=translations).value["conditions"])
        )
        assert conditions["fields"]["flag"]["values"] == values
        unmapped = without["lists"]["conditions"]
        assert unmapped["items"] == len(adapter.to_input(indexed)["conditions"])
        assert (unmapped == conditions) is same


def test_a_full_size_map_is_read_once_per_record_not_per_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A map's translations become a lookup table once per reading of a record (its input, its
    data coverage), never once per field, path or resource: a record of 300 conditions builds as
    many tables as a record of one, so a map of the full 10,000 translations costs a long
    problem list nothing more. (Built for each coded field read, a CDS hook on such a record
    took seconds, which no deadline bounds.)"""
    root = site_agent(tmp_path)
    full = [T2D, *({**T2D, "code": f"S{n}"} for n in range(MAX_TRANSLATIONS - 1))]
    decision = decision_of(root, maps_of(mapped({SET: full})))
    built: list[str] = []
    original = fhir.translation_table

    def counted(translations: Any, code_set: CodeSet) -> dict[tuple[str, str], str]:
        built.append(code_set.system)
        return original(translations, code_set)

    monkeypatch.setattr(fhir, "translation_table", counted)

    def decided(conditions: int) -> tuple[int, Outcome, int]:
        record = coded(SITE, SITE_CODE)
        first = record["entry"][1]["resource"]
        record["entry"][1:2] = [{"resource": {**first, "id": f"c{n}"}} for n in range(conditions)]
        built.clear()
        result, _ = decision.decide_fhir(record)
        assert result.data_coverage is not None
        return len(built), result.outcome, result.data_coverage["lists"]["conditions"]["items"]

    tables = 2 * 2  # the input's reading and the data coverage's, each of the two sets once
    assert decided(1) == (tables, Outcome.HUMAN_REVIEW, 1)
    assert decided(300) == (tables, Outcome.HUMAN_REVIEW, 300)


# -- each decision records its organization's map -----------------------------------------------


def test_each_decision_records_the_maps_version_and_digest_for_its_organization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every FHIR decision of an organization with a map records the map's version and SHA-256
    (its canonical form's) in its chained record: through `fhir/decide` and a CDS hook alike, an
    agent that declares no code set included (the map in force), and health shows it. A JSON
    decision records none."""
    given = mapped({SET: [T2D]})
    monkeypatch.setenv(ENV, json.dumps(given))
    digest = hashlib.sha256(canonical("default", VERSION, given["default"]["sets"])).hexdigest()
    stamp = {"version": VERSION, "sha256": digest}
    with serve(tmp_path) as app:
        assert app.get("/api/health").json()["site_codes"] == stamp
        answer = app.post(DECIDE, json=coded(SITE, SITE_CODE)).json()["decision"]
        assert answer["site_codes"] == stamp
        assert app.get(f"/api/audit/{answer['id']}").json()["record"]["site_codes"] == stamp
        [card] = app.post(SERVICE, json=hook(SHOWCASE)).json()["cards"]
        found = app.get(f"/api/audit/{card['extension']['mendari.decision_id']}").json()
        assert found["record"]["site_codes"] == stamp
        triage = load_agent(TRIAGE_DIR).id
        answer = app.post(
            f"/api/agents/{triage}/fhir/decide", json=message("Please call me about my bill.")
        ).json()
        assert answer["decision"]["site_codes"] == stamp  # it reads no code set: the map in force
        plain = app.post(f"/api/agents/{CHART_ID}/decide", json=SHOWCASE.input()).json()
        assert plain["site_codes"] is None


def test_another_organizations_map_is_never_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two organizations, each with its map, translating different site codes: a decision reads
    with the map of the organization it acts for and records that map, an organization with
    none reads none, nor does the command line (the default organization, which has none here).
    One organization's map given to another has another digest. Through a signed-in server, each
    organization's key decides with its own map and health shows its own."""
    given = {
        **mapped({SET: [T2D]}, org="clinic-a", version="a-1"),
        **mapped({SET: [{**T2D, "code": OTHER_CODE}]}, org="clinic-b", version="b-1"),
    }
    maps = maps_of(given, signed_in=True)
    decision = decision_of(CHART_DIR, maps)
    read: dict[str | None, list[Any]] = {}
    for org in ("clinic-a", "clinic-b", "clinic-c", None):
        with tenancy.acting_for(org):
            results = [
                decision.decide_fhir(coded(SITE, code))[0] for code in (SITE_CODE, OTHER_CODE)
            ]
        read[org] = [
            (result.answers["follow_up"].label, result.site_codes and result.site_codes["version"])
            for result in results
        ]
    assert read == {
        "clinic-a": [("routine", "a-1"), ("none", "a-1")],
        "clinic-b": [("none", "b-1"), ("routine", "b-1")],
        "clinic-c": [("none", None), ("none", None)],
        None: [("none", None), ("none", None)],
    }
    swapped = maps_of({"clinic-b": given["clinic-a"]}, signed_in=True).maps["clinic-b"]
    assert swapped.sha256 != maps.maps["clinic-a"].sha256 != maps.maps["clinic-b"].sha256

    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)
    monkeypatch.setenv(ENV, json.dumps(given))
    db = tmp_path / "signed.db"
    with TestClient(
        create_app(db_path=db, agents_paths=searched(tmp_path)), base_url="https://mendari.test"
    ) as app:
        store = AuditStore(db)
        for org in ("clinic-a", "clinic-b", "clinic-c"):
            key = store.create_api_key(name=f"key {org}", role="service", org=org, created_by="t")
            headers = {"authorization": f"Bearer {key['key']}"}
            expected = maps.maps[org].stamp() if org in maps.maps else None
            assert app.get("/api/health", headers=headers).json()["site_codes"] == expected
            for code in (SITE_CODE, OTHER_CODE):
                answer = app.post(DECIDE, json=coded(SITE, code), headers=headers)
                assert answer.status_code == 200, answer.text
                decided = answer.json()["decision"]
                assert decided["site_codes"] == expected
                own = (org, code) in (("clinic-a", SITE_CODE), ("clinic-b", OTHER_CODE))
                assert decided["answers"]["follow_up"]["label"] == ("routine" if own else "none")
        store.close()


def test_the_command_line_reads_no_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari decide --fhir` reads no site code map, as the guide says: with the setting set,
    a valid map or not, a site's own code is unknown and the decision records `site_codes:
    null`; the setting is not read, so not checked either."""
    case = tmp_path / "record.json"
    case.write_text(json.dumps(coded(SITE, SITE_CODE)), encoding="utf-8")
    for setting in (json.dumps(mapped({SET: [T2D]})), "{not json"):
        monkeypatch.setenv(ENV, setting)
        assert main(["decide", str(CHART_DIR), "--fhir", "--input", str(case)]) == 0
        decided = json.loads(capsys.readouterr().out)["decision"]
        assert decided["site_codes"] is None
        assert decided["answers"]["follow_up"]["label"] == "none"


def test_a_changed_map_is_told_apart_by_digest() -> None:
    """A map's digest is the SHA-256 of its canonical form, pinned here byte for byte. The same
    map written in another order, or with other spacing, has the same digest; a changed version,
    code, `as`, set, organization or one translation more each gives another."""
    base = mapped(
        {SET: [T2D, {**T2D, "code": OTHER_CODE}], OTHER_SET: [{**T2D, "as": "J06.9", "code": "T1"}]}
    )
    pinned = (
        b'{"format":"mendari.site-codes/1","org":"default","sets":{'
        b'"follow_up_conditions":['
        b'{"as":"E11.9","code":"DX-1001","system":"https://site.example/fhir/CodeSystem/conditions"},'
        b'{"as":"E11.9","code":"DX-1091","system":"https://site.example/fhir/CodeSystem/conditions"}],'
        b'"self_limited":['
        b'{"as":"J06.9","code":"T1","system":"https://site.example/fhir/CodeSystem/conditions"}]},'
        b'"version":"2026-10-19.1"}'
    )
    found = maps_of(base, signed_in=True).maps["default"]
    assert canonical("default", VERSION, found.sets) == pinned
    assert found.sha256 == hashlib.sha256(pinned).hexdigest() and FORMAT.encode() in pinned
    reordered = {"default": {"sets": {
        OTHER_SET: [{"code": "T1", "as": "J06.9", "system": SITE}],
        SET: [{**T2D, "code": OTHER_CODE}, T2D],
    }, "version": VERSION}}  # fmt: skip
    spaced = json.dumps(reordered, indent=4)
    assert parse(spaced, signed_in=True).maps["default"].sha256 == found.sha256

    def changed(change: Any) -> str:
        edited = copy.deepcopy(base)
        change(edited)
        org = next(iter(edited))
        return maps_of(edited, signed_in=True).maps[org].sha256

    digests = {
        "version": changed(lambda m: m["default"].update(version="2026-10-19.2")),
        "code": changed(lambda m: m["default"]["sets"][SET][0].update(code="DX-1002")),
        "as": changed(lambda m: m["default"]["sets"][SET][0].update({"as": "I10"})),
        "system": changed(lambda m: m["default"]["sets"][OTHER_SET][0].update(system=f"{SITE}/v2")),
        "one more": changed(
            lambda m: m["default"]["sets"][OTHER_SET].append({**T2D, "as": "J06.9"})
        ),
        "a set less": changed(lambda m: m["default"]["sets"].pop(OTHER_SET)),
        "organization": changed(lambda m: m.update({"clinic-a": m.pop("default")})),
    }
    assert len(set(digests.values())) == len(digests) and found.sha256 not in digests.values()


# -- never automated --------------------------------------------------------------------------


def test_a_decision_made_through_a_site_code_is_never_automated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `site_code` cap: a decision that read the agent's code through the site's translation
    goes to a person (HUMAN_REVIEW, the recommendation kept, the reason naming the code set),
    where the same record coded the agent's own way automates. Governance does not lift it, a
    hold keeps its own reason beside it, a record whose own code gives the value on a later path
    is decided as without the map, a rule's BLOCK on a translated value goes to a person too, an
    outcome that is neither AUTO nor a rule's BLOCK is left as it was, and through the server it
    is the same."""
    root = site_agent(tmp_path)
    maps = maps_of(mapped({SET: [T2D]}))
    decision = decision_of(root, maps)
    own, _ = decision.decide_fhir(coded(ICD, "E11.9"))
    site, _ = decision.decide_fhir(coded(SITE, SITE_CODE))
    assert own.outcome is Outcome.AUTO and own.site_codes == maps.maps["default"].stamp()
    assert not any("site code" in reason for reason in own.reasons)
    assert site.outcome is Outcome.HUMAN_REVIEW and site.recommendation == "routine"
    assert site.reasons[-1] == "site code translated (follow_up_conditions): a person decides"
    labels = [{key: a.label for key, a in r.answers.items()} for r in (own, site)]
    assert labels[0] == labels[1]  # the same answers: only the outcome differs
    # The record's own code on a later path wins over the site's on the first: nothing was read
    # through the map, so the decision is the agent's alone, and it automates.
    fallback, _ = decision.decide_fhir(coded(SITE, SITE_CODE, evidence=(ICD, "E11.9")))
    alone, _ = Decision(decision.agent).decide_fhir(coded(SITE, SITE_CODE, evidence=(ICD, "E11.9")))
    assert fallback.outcome is alone.outcome is Outcome.AUTO
    assert fallback.reasons == alone.reasons and fallback.site_codes == own.site_codes

    # A rule's BLOCK on a value read through a translation goes to a person too: a block is
    # final at once and seen by nobody. The same rule on the agent's own code still blocks.
    def blocking(data: dict[str, Any]) -> None:
        data["policy"]["rules"].append(
            {
                "name": "routine-stop",
                "when": {"question": "follow_up", "labels": ["routine"]},
                "outcome": "BLOCK",
                "reason": "a fixture's block",
            }
        )

    blocker = decision_of(site_agent(tmp_path / "block", manifest=blocking), maps)
    blocked, _ = blocker.decide_fhir(coded(ICD, "E11.9"))
    to_person, _ = blocker.decide_fhir(coded(SITE, SITE_CODE))
    assert blocked.outcome is Outcome.BLOCK and blocked.rule == "routine-stop"
    assert to_person.outcome is Outcome.HUMAN_REVIEW and to_person.rule == "routine-stop"
    assert to_person.reasons[:2] == (
        "rule routine-stop: a fixture's block",
        "site code translated (follow_up_conditions): a person decides instead of its rule's BLOCK",
    )

    governed = decision_of(
        site_agent(tmp_path / "level-2", level=2),
        maps,
        governance=GovernanceOverride(approved_by="governance@example.org", reference="GOV-1"),
    )
    assert governed.decide_fhir(coded(ICD, "E11.9"))[0].outcome is Outcome.AUTO
    lifted, _ = governed.decide_fhir(coded(SITE, SITE_CODE))
    assert lifted.outcome is Outcome.HUMAN_REVIEW and "governance override" in lifted.reasons[-2]

    held = decision_of(root, maps, hold_source=lambda agent: "hold h-1")
    both, _ = held.decide_fhir(coded(SITE, SITE_CODE))
    assert both.outcome is Outcome.HUMAN_REVIEW
    assert both.reasons[-2:] == (
        "site code translated (follow_up_conditions): a person decides",
        "automation is on hold: hold h-1",
    )

    # Whatever label it gives: a chart with a long-term condition coded the agent's way (I10)
    # beside a site-coded one is a routine follow-up with the map or without. Without the map
    # nothing was translated, and it automates; read through the map, a person decides.
    beside = coded(ICD, "I10", also=((SITE, SITE_CODE),))
    unmapped, _ = Decision(decision.agent).decide_fhir(beside)
    through, _ = decision.decide_fhir(beside)
    assert (unmapped.outcome, unmapped.recommendation) == (Outcome.AUTO, "routine")
    assert (through.outcome, through.recommendation) == (Outcome.HUMAN_REVIEW, "routine")
    # An outcome that is not AUTO is left as it was: a record part of which could not be read.
    unread, _ = decision.decide_fhir(coded(SITE, SITE_CODE), unreadable={"notes": "timeout"})
    assert unread.outcome is Outcome.ABSTAIN
    assert unread.reasons == ("could not read notes: timeout",)

    monkeypatch.setenv(ENV, json.dumps(mapped({SET: [T2D]})))
    with serve(tmp_path, "served.db") as app:
        decided = [
            app.post(COPY_DECIDE, json=record).json()["decision"]
            for record in (coded(ICD, "E11.9"), coded(SITE, SITE_CODE))
        ]
    assert [d["outcome"] for d in decided] == ["AUTO", "HUMAN_REVIEW"]
    assert decided[1]["reasons"][-1] == (
        "site code translated (follow_up_conditions): a person decides"
    )
