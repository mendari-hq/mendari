# SPDX-License-Identifier: Apache-2.0
"""A site code map per organization (LP-23i, `core/site_codes.py`, `MENDARI_SITE_CODES`): a
site's own codes translate into the codes a spec 0.3 pack's code sets declare, one organization
at a time. Unset, nothing changes. Set, it is checked at startup against the packs found, a
translation lands only on a code its set declares, each FHIR decision records its organization's
map by version and SHA-256 (and its data coverage reads with the same translations), another
organization's map is never applied, any change to a map changes its digest, a decision made
through a translation never automates, and records from before still verify. Every code here is
fictional; the site's system is a made-up one."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_pack
from mendari.cli import main
from mendari.core import clock, tenancy
from mendari.core.decision import DecisionResult
from mendari.core.pack import Pack
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
from tests.conftest import INBOX_DIR, encrypt_mode
from tests.test_spec_03 import ICD, PATIENT, SITE, mini

PACKS = INBOX_DIR.parent  # the reference packs
PACK_ID = "mendari-test/prescreen-mini"
DECIDE = f"/api/packs/{PACK_ID}/fhir/decide"
SERVICE = "/cds-services/mendari-test.prescreen-mini"
LOINC = "http://loinc.org"
SITE_CODE = "SITE-T2D-4471"  # a site's own code for type 2 diabetes (fictional)
OTHER_CODE = "SITE-DM2-0915"  # another site's code for it (fictional)
T2D = {"system": SITE, "code": SITE_CODE, "as": "E11.9"}
VERSION = "2026-10-19.1"
# The mapping the test pack reads conditions with: a code of the `t2d` set, else of the
# condition's evidence; a code of the `t1d` set (spec 0.3, `code_set`).
CODE_SETS = {
    "t2d": {"system": ICD, "codes": ["E11.9", "E11.65"]},
    "t1d": {"system": ICD, "codes": ["E10.9"]},
}
CONDITIONS = {
    "resource": "Condition",
    "fields": {
        "code": {"paths": ["code.coding", "evidence.code.coding"], "code_set": "t2d"},
        "type_1": {"path": "code.coding", "code_set": "t1d"},
    },
}


def site_pack(base: Path, *, level: int = 1) -> Path:
    """The prescreen fixture at spec 0.3, reading conditions through code sets, at risk `level`
    (1 may automate), automating a candidate at 0.80 (its criteria give 0.83)."""
    root = mini(
        base / "packs", {"code_sets": CODE_SETS, "lists": {"conditions": CONDITIONS}}, spec="0.3"
    )
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["risk"]["level"] = level
    manifest["policy"]["thresholds"]["auto"] = 0.8
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return root


def coded(system: str, code: str, *, evidence: tuple[str, str] | None = None) -> dict[str, Any]:
    """A synthetic record: the fixture's patient, one Condition coded `system`/`code` (with an
    evidence coding), and an HbA1c in range today."""
    condition: dict[str, Any] = {
        "resourceType": "Condition",
        "id": "c1",
        "code": {"coding": [{"system": system, "code": code}]},
    }
    if evidence is not None:
        condition["evidence"] = [
            {"code": [{"coding": [{"system": evidence[0], "code": evidence[1]}]}]}
        ]
    observation = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "code": {"coding": [{"system": LOINC, "code": "4548-4"}]},
        "valueQuantity": {"value": 8.0, "unit": "%"},
        "effectiveDateTime": clock.today().isoformat(),
    }
    entries = [{"resource": resource} for resource in (PATIENT, condition, observation)]
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def mapped(sets: dict[str, Any], *, org: str = "default", version: str = VERSION) -> dict[str, Any]:
    return {org: {"version": version, "sets": sets}}


def maps_of(given: dict[str, Any], *, signed_in: bool = False) -> SiteCodeMaps:
    return parse(json.dumps(given), signed_in=signed_in)


def decision_of(root: Path, maps: SiteCodeMaps | None = None, **kwargs: Any) -> Decision:
    pack = load_pack(root)
    if maps is not None:
        check(maps, [pack])
    return Decision(pack, site_codes=maps, **kwargs)


def serve(tmp_path: Path, name: str = "a.db") -> TestClient:
    return TestClient(create_app(db_path=tmp_path / name, packs_paths=[PACKS, tmp_path / "packs"]))


# -- unset ------------------------------------------------------------------------------------


def test_unset_means_every_decision_as_before(tmp_path: Path) -> None:
    """With `MENDARI_SITE_CODES` unset (the suite removes it, `LIVE_SETTINGS`) there is no map,
    health says so, and every decision the server makes, FHIR or JSON, is the engine's without a
    map: the same outcome, reasons and data coverage, `site_codes` null. A site's own code is
    not read as the pack's. (The 23a golden, `tests/test_fhir_decisions.py`, pins every
    reference pack's FHIR decisions, unchanged.)"""
    root = site_pack(tmp_path)
    assert site_codes([load_pack(root)], signed_in=False) is None
    with serve(tmp_path) as app:
        assert app.get("/api/health").json()["site_codes"] is None
        for record in (coded(ICD, "E11.9"), coded(SITE, SITE_CODE)):
            answer = app.post(DECIDE, json=record).json()["decision"]
            engine, _ = Decision(load_pack(root)).decide_fhir(record)
            assert answer["site_codes"] is None and engine.site_codes is None
            assert answer["outcome"] == engine.outcome.value
            assert answer["recommendation"] == engine.recommendation
            assert answer["reasons"] == list(engine.reasons)
            assert answer["data_coverage"] == engine.data_coverage
        own, site = (
            Decision(load_pack(root)).decide_fhir(record)[0]
            for record in (coded(ICD, "E11.9"), coded(SITE, SITE_CODE))
        )
        assert own.outcome is Outcome.AUTO and own.recommendation == "candidate"
        assert site.answers["t2d_diagnosis"].label == "unknown"
        plain = app.post("/api/packs/mendari/patient-inbox-router/decide", json={
            "message": {"body": "Please call me about my bill."}
        }).json()  # fmt: skip
        assert plain["site_codes"] is None


# -- the setting, checked at startup ----------------------------------------------------------

BAD: dict[str, tuple[Any, str]] = {
    "not JSON": ("{not json", "is not JSON"),
    "neither JSON nor a file": ("[1]", "neither inline JSON"),
    "no organization": ({}, "1 to 100 organizations"),
    "a key twice": (
        '{"default": {"version": "v1", "sets": {"t2d": []}}, "default": {}}',
        "gives a key twice",
    ),
    "an organization with a space": (mapped({"t2d": [T2D]}, org="a b"), "visible ASCII"),
    "another organization with sign-in off": (
        mapped({"t2d": [T2D]}, org="clinic-a"),
        "with sign-in off, the one organization is default",
    ),
    "a map that is a list": ({"default": [T2D]}, "an object of version and sets"),
    "a field of its own": (
        {"default": {"version": VERSION, "sets": {"t2d": [T2D]}, "owner": "it"}},
        "an object of version and sets",
    ),
    "a version with a space": (mapped({"t2d": [T2D]}, version="v 1"), "version must be"),
    "no set": (mapped({}), "one or more code sets"),
    "a set name not as a pack writes one": (mapped({"T2D": [T2D]}), "code set 1's name"),
    "no translation": (mapped({"t2d": []}), "one or more translations"),
    "a translation of its own key": (
        mapped({"t2d": [{**T2D, "display": "Type 2"}]}),
        "translation 1 must be an object of system, code and as",
    ),
    "a system that is no uri": (mapped({"t2d": [{**T2D, "system": "SITE"}]}), "must be a uri"),
    "a code with a space at its end": (
        mapped({"t2d": [{**T2D, "code": f"{SITE_CODE} "}]}),
        "code must be 1 to 256 printable",
    ),
    "an as that is no text": (mapped({"t2d": [{**T2D, "as": 5}]}), "as must be 1 to 256"),
    "a site code twice": (
        mapped({"t2d": [T2D, {**T2D, "as": "E11.65"}]}),
        "translation 2 translates a site code the set translates already",
    ),
    "too many": (
        mapped({"t2d": [{**T2D, "code": f"S{n}"} for n in range(MAX_TRANSLATIONS + 1)]}),
        "more than 10,000 translations",
    ),
    "a set no pack declares": (mapped({"payer": [T2D]}), "code set 'payer': no agent"),
    "an as the set does not declare": (
        mapped({"t2d": [T2D, {**T2D, "code": OTHER_CODE, "as": "E11.0"}]}),
        "code set 't2d', translation 2: it translates into a code the set does not declare",
    ),
    "a code of another set": (
        mapped({"t2d": [{**T2D, "as": "E10.9"}]}),
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
    checked against the code sets of the packs found. Inline JSON and a file give the same map,
    and a signed-in server takes any organization."""
    root = site_pack(tmp_path)
    given, problem = BAD[case]
    monkeypatch.setenv(ENV, given if isinstance(given, str) else json.dumps(given))
    with pytest.raises(MendariError) as refused:
        site_codes([load_pack(root)], signed_in=False)
    message = str(refused.value)
    assert problem in message and message.startswith(ENV)
    assert SITE_CODE not in message and SITE not in message and "E11" not in message
    with pytest.raises(MendariError, match=ENV):
        create_app(db_path=tmp_path / "a.db", packs_paths=[PACKS, tmp_path / "packs"])


def test_the_setting_is_inline_json_or_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = site_pack(tmp_path)
    text = json.dumps(mapped({"t2d": [T2D]}))
    monkeypatch.setenv(ENV, text)
    inline = site_codes([load_pack(root)], signed_in=False)
    path = tmp_path / "site-codes.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(ENV, str(path))
    from_file = site_codes([load_pack(root)], signed_in=False)
    assert inline is not None and from_file is not None
    assert from_file.maps["default"] == inline.maps["default"]
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(MendariError, match="not UTF-8"):
        site_codes([load_pack(root)], signed_in=False)
    signed = maps_of(mapped({"t2d": [T2D]}, org="clinic-a"), signed_in=True)
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
    adapter = FhirAdapter.for_pack(load_pack(site_pack(tmp_path)))

    def read(record: dict[str, Any], sets: dict[str, Any]) -> tuple[list[Any], tuple[str, ...]]:
        mapped_in = adapter.read(adapter.index(record), site_codes=sets)
        return mapped_in.value["conditions"], mapped_in.translated

    site = coded(SITE, SITE_CODE)
    assert read(site, {}) == ([], ())
    assert read(site, {"t2d": [T2D]}) == ([{"code": "E11.9"}], ("t2d",))
    unlisted = {"t2d": [{**T2D, "as": "E11.0"}], "t1d": [{**T2D, "as": "E11.9"}]}
    assert read(site, unlisted) == ([], ())  # neither is a code of its own set
    assert read(site, {"t1d": [{**T2D, "as": "E10.9"}]}) == ([{"type_1": "E10.9"}], ("t1d",))
    both = coded(SITE, SITE_CODE, evidence=(ICD, "E11.65"))  # the own code on the fallback path
    assert read(both, {"t2d": [T2D]}) == read(both, {}) == ([{"code": "E11.65"}], ())
    later = coded(ICD, "E10.0", evidence=(SITE, SITE_CODE))  # a site code on the fallback only
    assert read(later, {"t2d": [T2D]}) == ([{"code": "E11.9"}], ("t2d",))
    twice = coded(SITE, SITE_CODE, evidence=(SITE, OTHER_CODE))
    other = {**T2D, "code": OTHER_CODE, "as": "E11.65"}
    assert read(twice, {"t2d": [T2D, other]}) == ([{"code": "E11.9"}], ("t2d",))
    own = coded(ICD, "E11.65")
    assert read(own, {"t2d": [{**T2D, "code": "E11.65", "system": ICD, "as": "E11.9"}]}) == (
        [{"code": "E11.65"}],
        (),
    )


def test_the_data_coverage_reads_with_the_same_translations_as_the_input(tmp_path: Path) -> None:
    """The data-coverage record of a decision made with a map reads the record with the same
    translations as the input (the 23g merge's note): a field whose first path a translation
    gives a value reads its fallback path for the set's own code, as the input does, and a
    condition coded only by the site counts as an item, as the input has it. Without the map
    the record says what the input read without it, the same as with the map when the record's
    own code gives the field its value."""
    root = site_pack(tmp_path)
    maps = maps_of(mapped({"t2d": [T2D]}))
    decision = decision_of(root, maps)
    adapter = FhirAdapter.for_pack(decision.pack)
    for record, values, same in (
        (coded(SITE, SITE_CODE, evidence=(ICD, "E11.65")), 2, True),  # both paths, the own code
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
            systems=[ICD, SITE],
            sources={},
            site_codes=translations,
        )
        without = data_coverage.measure(
            adapter.mapping, indexed, unreadable={}, systems=[ICD, SITE], sources={}
        )
        conditions = result.data_coverage["lists"]["conditions"]
        assert conditions == with_map["lists"]["conditions"]
        assert (
            conditions["items"]
            == 1
            == len(adapter.read(indexed, site_codes=translations).value["conditions"])
        )
        assert conditions["fields"]["code"]["values"] == values
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
    root = site_pack(tmp_path)
    full = [T2D, *({**T2D, "code": f"S{n}"} for n in range(MAX_TRANSLATIONS - 1))]
    decision = decision_of(root, maps_of(mapped({"t2d": full})))
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

    tables = 2 * len(CODE_SETS)  # the input's reading and the data coverage's, each set once
    assert decided(1) == (tables, Outcome.HUMAN_REVIEW, 1)
    assert decided(300) == (tables, Outcome.HUMAN_REVIEW, 300)


# -- each decision records its organization's map -----------------------------------------------


def test_each_decision_records_the_maps_version_and_digest_for_its_organization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every FHIR decision of an organization with a map records the map's version and SHA-256
    (its canonical form's) in its chained record: through `fhir/decide` and a CDS hook alike, a
    pack that declares no code set included (the map in force), and health shows it. A JSON
    decision records none."""
    site_pack(tmp_path)
    given = mapped({"t2d": [T2D]})
    monkeypatch.setenv(ENV, json.dumps(given))
    digest = hashlib.sha256(canonical("default", VERSION, given["default"]["sets"])).hexdigest()
    stamp = {"version": VERSION, "sha256": digest}
    with serve(tmp_path) as app:
        assert app.get("/api/health").json()["site_codes"] == stamp
        answer = app.post(DECIDE, json=coded(SITE, SITE_CODE)).json()["decision"]
        assert answer["site_codes"] == stamp
        assert app.get(f"/api/audit/{answer['id']}").json()["record"]["site_codes"] == stamp
        prefetch = {
            "patient": PATIENT,
            "conditions": {"resourceType": "Bundle", "type": "searchset", "entry": [
                {"resource": coded(ICD, "E11.9")["entry"][1]["resource"]}]},
            "observations": {"resourceType": "Bundle", "type": "searchset", "entry": [
                {"resource": coded(ICD, "E11.9")["entry"][2]["resource"]}]},
            "notes": {"resourceType": "Bundle", "type": "searchset", "entry": []},
        }  # fmt: skip
        hook = {
            "hook": "patient-view",
            "hookInstance": "5f3c2a10-0000-4000-8000-000000000023",
            "context": {"patientId": "p1", "userId": "Practitioner/1"},
            "prefetch": prefetch,
        }
        [card] = app.post(SERVICE, json=hook).json()["cards"]
        found = app.get(f"/api/audit/{card['extension']['mendari.decision_id']}").json()
        assert found["record"]["site_codes"] == stamp
        referral = json.loads(
            (Path(__file__).parent / "fixtures" / "us-core" / "referral.json").read_text()
        )
        answer = app.post("/api/packs/mendari/referral-router/fhir/decide", json=referral).json()
        assert answer["decision"]["site_codes"] == stamp  # it reads no code set: the map in force
        plain = app.post("/api/packs/mendari/patient-inbox-router/decide", json={
            "message": {"body": "Please call me about my bill."}
        }).json()  # fmt: skip
        assert plain["site_codes"] is None


def test_another_organizations_map_is_never_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two organizations, each with its map, translating different site codes: a decision reads
    with the map of the organization it acts for and records that map, an organization with
    none reads none, nor does the command line (the default organization, which has none here).
    One organization's map given to another has another digest. Through a signed-in server, each
    organization's key decides with its own map and health shows its own."""
    root = site_pack(tmp_path)
    given = {
        **mapped({"t2d": [T2D]}, org="clinic-a", version="a-1"),
        **mapped({"t2d": [{**T2D, "code": OTHER_CODE}]}, org="clinic-b", version="b-1"),
    }
    maps = maps_of(given, signed_in=True)
    decision = decision_of(root, maps)
    read: dict[str | None, list[Any]] = {}
    for org in ("clinic-a", "clinic-b", "clinic-c", None):
        with tenancy.acting_for(org):
            results = [
                decision.decide_fhir(coded(SITE, code))[0] for code in (SITE_CODE, OTHER_CODE)
            ]
        read[org] = [
            (
                result.answers["t2d_diagnosis"].label,
                result.site_codes and result.site_codes["version"],
            )
            for result in results
        ]
    assert read == {
        "clinic-a": [("yes", "a-1"), ("unknown", "a-1")],
        "clinic-b": [("unknown", "b-1"), ("yes", "b-1")],
        "clinic-c": [("unknown", None), ("unknown", None)],
        None: [("unknown", None), ("unknown", None)],
    }
    swapped = maps_of({"clinic-b": given["clinic-a"]}, signed_in=True).maps["clinic-b"]
    assert swapped.sha256 != maps.maps["clinic-a"].sha256 != maps.maps["clinic-b"].sha256

    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)
    monkeypatch.setenv(ENV, json.dumps(given))
    db = tmp_path / "signed.db"
    with TestClient(
        create_app(db_path=db, packs_paths=[PACKS, tmp_path / "packs"]),
        base_url="https://mendari.test",
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
                assert decided["answers"]["t2d_diagnosis"]["label"] == ("yes" if own else "unknown")
        store.close()


def test_the_command_line_reads_no_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari decide --fhir` reads no site code map, as the guide says: with the setting set,
    a valid map or not, a site's own code is unknown and the decision records `site_codes:
    null`; the setting is not read, so not checked either."""
    root = site_pack(tmp_path)
    case = tmp_path / "record.json"
    case.write_text(json.dumps(coded(SITE, SITE_CODE)), encoding="utf-8")
    for setting in (json.dumps(mapped({"t2d": [T2D]})), "{not json"):
        monkeypatch.setenv(ENV, setting)
        assert main(["decide", str(root), "--fhir", "--input", str(case)]) == 0
        decided = json.loads(capsys.readouterr().out)["decision"]
        assert decided["site_codes"] is None
        assert decided["answers"]["t2d_diagnosis"]["label"] == "unknown"


def test_a_changed_map_is_told_apart_by_digest() -> None:
    """A map's digest is the SHA-256 of its canonical form, pinned here byte for byte. The same
    map written in another order, or with other spacing, has the same digest; a changed version,
    code, `as`, set, organization or one translation more each gives another."""
    base = mapped(
        {"t2d": [T2D, {**T2D, "code": OTHER_CODE}], "t1d": [{**T2D, "as": "E10.9", "code": "T1"}]}
    )
    pinned = (
        b'{"format":"mendari.site-codes/1","org":"default","sets":{'
        b'"t1d":[{"as":"E10.9","code":"T1","system":"https://site.example/fhir/CodeSystem/conditions"}],'
        b'"t2d":[{"as":"E11.9","code":"SITE-DM2-0915","system":"https://site.example/fhir/CodeSystem/conditions"},'
        b'{"as":"E11.9","code":"SITE-T2D-4471","system":"https://site.example/fhir/CodeSystem/conditions"}]},'
        b'"version":"2026-10-19.1"}'
    )
    found = maps_of(base, signed_in=True).maps["default"]
    assert canonical("default", VERSION, found.sets) == pinned
    assert found.sha256 == hashlib.sha256(pinned).hexdigest() and FORMAT.encode() in pinned
    reordered = {"default": {"sets": {
        "t1d": [{"code": "T1", "as": "E10.9", "system": SITE}],
        "t2d": [{**T2D, "code": OTHER_CODE}, T2D],
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
        "code": changed(lambda m: m["default"]["sets"]["t2d"][0].update(code="SITE-T2D-4472")),
        "as": changed(lambda m: m["default"]["sets"]["t2d"][0].update({"as": "E11.65"})),
        "system": changed(lambda m: m["default"]["sets"]["t1d"][0].update(system=f"{SITE}/v2")),
        "one more": changed(lambda m: m["default"]["sets"]["t1d"].append({**T2D, "as": "E10.9"})),
        "a set less": changed(lambda m: m["default"]["sets"].pop("t1d")),
        "organization": changed(lambda m: m.update({"clinic-a": m.pop("default")})),
    }
    assert len(set(digests.values())) == len(digests) and found.sha256 not in digests.values()


# -- never automated --------------------------------------------------------------------------


def test_a_decision_made_through_a_site_code_is_never_automated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `site_code` cap: a decision that read the pack's code through the site's translation
    goes to a person (HUMAN_REVIEW, the recommendation kept, the reason naming the code set),
    where the same record coded the pack's own way automates. Governance does not lift it, a
    hold keeps its own reason beside it, a record whose own code gives the value on a later path
    is decided as without the map, a rule's BLOCK on a translated value goes to a person too, an
    outcome that is neither AUTO nor a rule's BLOCK is left as it was, and through the server it
    is the same."""
    root = site_pack(tmp_path)
    maps = maps_of(mapped({"t2d": [T2D]}))
    decision = decision_of(root, maps)
    own, _ = decision.decide_fhir(coded(ICD, "E11.9"))
    site, _ = decision.decide_fhir(coded(SITE, SITE_CODE))
    assert own.outcome is Outcome.AUTO and own.site_codes == maps.maps["default"].stamp()
    assert not any("site code" in reason for reason in own.reasons)
    assert site.outcome is Outcome.HUMAN_REVIEW and site.recommendation == "candidate"
    assert site.reasons[-1] == "site code translated (t2d): a person decides"
    labels = [{key: a.label for key, a in r.answers.items()} for r in (own, site)]
    assert labels[0] == labels[1]  # the same answers: only the outcome differs
    # The record's own code on a later path wins over the site's on the first: nothing was read
    # through the map, so the decision is the pack's alone, and it automates.
    fallback, _ = decision.decide_fhir(coded(SITE, SITE_CODE, evidence=(ICD, "E11.65")))
    alone, _ = Decision(decision.pack).decide_fhir(coded(SITE, SITE_CODE, evidence=(ICD, "E11.65")))
    assert fallback.outcome is alone.outcome is Outcome.AUTO
    assert fallback.reasons == alone.reasons and fallback.site_codes == own.site_codes

    # A rule's BLOCK on a value read through a translation goes to a person too: a block is
    # final at once and seen by nobody. The same rule on the pack's own code still blocks.
    blocking = site_pack(tmp_path / "block")
    manifest = yaml.safe_load((blocking / "agent.yaml").read_text())
    manifest["policy"]["rules"] = [
        {
            "name": "t2d_stop",
            "when": {"question": "t2d_diagnosis", "labels": ["yes"]},
            "outcome": "BLOCK",
            "reason": "a fixture's block",
        }
    ]
    (blocking / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    blocker = decision_of(blocking, maps)
    blocked, _ = blocker.decide_fhir(coded(ICD, "E11.9"))
    to_person, _ = blocker.decide_fhir(coded(SITE, SITE_CODE))
    assert blocked.outcome is Outcome.BLOCK and blocked.rule == "t2d_stop"
    assert to_person.outcome is Outcome.HUMAN_REVIEW and to_person.rule == "t2d_stop"
    assert to_person.reasons[:2] == (
        "rule t2d_stop: a fixture's block",
        "site code translated (t2d): a person decides instead of its rule's BLOCK",
    )

    governed = decision_of(
        site_pack(tmp_path / "level-2", level=2),
        maps,
        governance=GovernanceOverride(approved_by="governance@example.org", reference="GOV-1"),
    )
    assert governed.decide_fhir(coded(ICD, "E11.9"))[0].outcome is Outcome.AUTO
    lifted, _ = governed.decide_fhir(coded(SITE, SITE_CODE))
    assert lifted.outcome is Outcome.HUMAN_REVIEW and "governance override" in lifted.reasons[-2]

    held = decision_of(root, maps, hold_source=lambda pack: "hold h-1")
    both, _ = held.decide_fhir(coded(SITE, SITE_CODE))
    assert both.outcome is Outcome.HUMAN_REVIEW
    assert both.reasons[-2:] == (
        "site code translated (t2d): a person decides",
        "automation is on hold: hold h-1",
    )

    # Whatever label it gives: with no HbA1c the record needs a chart review, which this pack
    # automates. Without the map nothing was translated (the diagnosis is unknown), and it
    # automates; read through the map (the diagnosis known), a person decides.
    no_lab = coded(SITE, SITE_CODE)
    no_lab["entry"] = no_lab["entry"][:2]
    unmapped, _ = Decision(decision.pack).decide_fhir(no_lab)
    through, _ = decision.decide_fhir(no_lab)
    assert (unmapped.outcome, unmapped.recommendation) == (Outcome.AUTO, "needs_review")
    assert (through.outcome, through.recommendation) == (Outcome.HUMAN_REVIEW, "needs_review")
    # An outcome that is not AUTO is left as it was: a record part of which could not be read.
    unread, _ = decision.decide_fhir(coded(SITE, SITE_CODE), unreadable={"notes": "timeout"})
    assert unread.outcome is Outcome.ABSTAIN
    assert unread.reasons == ("could not read notes: timeout",)

    monkeypatch.setenv(ENV, json.dumps(mapped({"t2d": [T2D]})))
    with serve(tmp_path, "served.db") as app:
        decided = [
            app.post(DECIDE, json=record).json()["decision"]
            for record in (coded(ICD, "E11.9"), coded(SITE, SITE_CODE))
        ]
    assert [d["outcome"] for d in decided] == ["AUTO", "HUMAN_REVIEW"]
    assert decided[1]["reasons"][-1] == "site code translated (t2d): a person decides"


# -- records from before ----------------------------------------------------------------------


def test_old_records_still_verify(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record chained before LP-23i has no `site_codes`; one after has it (null without a
    map, and on JSON input). The chain verifies across all of them, on both databases."""
    root = site_pack(tmp_path)
    store = AuditStore(db_target)
    pack: Pack = load_pack(root)
    maps = maps_of(mapped({"t2d": [T2D]}))
    check(maps, [pack])
    decision = Decision(pack, store=store, site_codes=maps)
    inbox = Decision.from_hub("mendari/patient-inbox-router", store=store)
    original = DecisionResult.to_record

    def before(self: DecisionResult) -> dict[str, Any]:
        record = original(self)
        del record["site_codes"]  # the shape from before
        return record

    monkeypatch.setattr(DecisionResult, "to_record", before)
    old, _ = decision.decide_fhir(coded(SITE, SITE_CODE))
    monkeypatch.setattr(DecisionResult, "to_record", original)
    new, _ = decision.decide_fhir(coded(SITE, SITE_CODE))
    unmapped, _ = Decision(pack, store=store).decide_fhir(coded(ICD, "E11.9"))
    plain = inbox.decide({"message": {"body": "Please call me about my bill."}})
    shapes = {}
    for result in (old, new, unmapped, plain):
        item = store.get(result.id)
        assert item is not None
        shapes[result.id] = item["record"].get("site_codes", "absent")
    assert shapes[old.id] == "absent"
    assert shapes[new.id] == maps.maps["default"].stamp()
    assert shapes[unmapped.id] is None and shapes[plain.id] is None
    assert store.verify()["ok"] is True
    store.close()
