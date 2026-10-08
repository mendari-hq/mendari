# SPDX-License-Identifier: Apache-2.0
"""Shareable evaluation reports (evals/share.py): metrics leave the site, cases never do.

A shared report is `mendari.site-report/1`, a closed schema the client builds and the registry
checks: names and rounded numbers only, small bins dropped, no subgroup, no case id, no dataset
identity, no approver."""

from __future__ import annotations

import copy
import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari import Decision, Pack, load_pack
from mendari.core.policy import GovernanceOverride
from mendari.core.registry import resolve_pack
from mendari.errors import FAILURE_CODES, MendariError
from mendari.evals import evaluate, resolve_dataset
from mendari.evals.share import (
    MAX_MEMBERS,
    MIN_GROUP,
    SharedProvider,
    SiteReport,
    composite_members,
    shareable,
    summary,
)
from mendari.hub.registry import RegistryStore
from tests.conftest import INBOX_DIR, ROOT, SCREENED, Registry

BENCHMARK = json.loads(
    (resolve_pack("mendari/referral-router").root / "evals" / "benchmark.json").read_text()
)
FREE_TEXT = "Jane Doe, DOB 1960-02-01, called about chest pain"


def _cell(n: int, accuracy: float) -> dict[str, Any]:
    return {
        "n": n,
        "accuracy": accuracy,
        "auto_rate": 0.0,
        "abstention_rate": 0.0,
        "false_automation_rate": None,
    }


def test_case_ids_and_dataset_identity_stay_home() -> None:
    shared = shareable(BENCHMARK)
    text = json.dumps(shared)
    assert shared["format"] == "mendari.site-report/1"
    assert "misclassified" not in shared
    named = [case.get("case") or case["id"] for case in BENCHMARK["misclassified"]]
    assert named and all(name not in text for name in named)
    assert shared["dataset"] == {"n": BENCHMARK["dataset"]["n"]}  # no name, no digest
    assert "id" not in shared and "created_at" not in shared  # links to the site's store


def test_missed_critical_cases_leave_no_id() -> None:
    """The inbox pack's rules miss 16 of the 20 held-out emergencies: their ids were sent as
    `missed_ids` before, their ledger hashes are kept as `missed_cases` now. Only the counts
    leave."""
    pack = resolve_pack("mendari/patient-inbox-router")
    dataset = resolve_dataset(pack, "red_flags_holdout")
    report = evaluate(Decision(pack), dataset)
    missed = report["decisions"]["critical"][0]["missed_cases"]
    assert len(missed) >= 10
    shared = shareable(report)
    text = json.dumps(shared)
    assert all(record["id"] not in text for record in dataset.records)
    assert all(case not in text for case in missed)  # nor their hashes
    critical = shared["decisions"]["critical"][0]
    assert critical == {
        "question": "urgency",
        "label": "urgent",
        "n": 20,
        "detected": 20 - len(missed),
        "detection_rate": (20 - len(missed)) / 20,
        "auto_missed": 0,
    }


def test_an_override_is_shared_as_a_fact_not_a_name() -> None:
    pack = resolve_pack("mendari/clinical-trial-prescreener")
    approval = GovernanceOverride(approved_by="Dr. A. Person", reference="IRB-2026-0042")
    report = evaluate(Decision(pack, governance=approval), resolve_dataset(pack, "exclusions"))
    shared = shareable(report)
    assert shared["policy"]["governance_override"] is True
    assert "Person" not in json.dumps(shared) and "IRB-2026" not in json.dumps(shared)


def test_subgroups_never_leave_and_small_bins_are_left_out() -> None:
    """A subgroup's values come from a site's data: a clinic, a clinician, a patient seen often
    enough, a date. None of them leave, whatever the cell's size."""
    report = {
        **BENCHMARK,
        "subgroups": {
            "patient": {"Jane Doe MRN 00123 DOB 1985.03.14": _cell(12, 0.9)},
            "admit_date": {"2026-03-14": _cell(12, 0.9)},
            "age": {"90+": _cell(2, 0.5), "40-64": _cell(50, 0.9)},
        },
    }
    shared = shareable(report)
    assert "subgroups" not in shared
    assert "Jane" not in json.dumps(shared) and "2026-03-14" not in json.dumps(shared)
    bins = shared["questions"]["next_step"]["calibration"]["bins"]
    assert bins and all(b["n"] >= MIN_GROUP for b in bins)
    assert shared["questions"]["next_step"]["calibration"]["ece"] == round(
        BENCHMARK["questions"]["next_step"]["calibration"]["ece"], 4
    )


def test_numbers_are_rounded_on_both_sides() -> None:
    report = copy.deepcopy(BENCHMARK)
    report["questions"]["next_step"]["brier"] = 0.1234567890123456
    report["run"]["latency_ms"]["max"] = 123456789.12345678
    shared = shareable(report)
    assert shared["questions"]["next_step"]["brier"] == 0.123
    assert shared["latency_ms"]["max"] == 123456789.123
    exact = copy.deepcopy(shared)
    exact["decisions"]["recommendation_accuracy"] = 0.987654321
    received = SiteReport.model_validate(exact).model_dump(mode="json")
    assert received["decisions"]["recommendation_accuracy"] == 0.9877


def test_the_receiver_refuses_subgroups_and_small_bins() -> None:
    shared = shareable(BENCHMARK)
    with pytest.raises(ValidationError):
        SiteReport.model_validate({**shared, "subgroups": {"age": {"40-64": _cell(50, 0.9)}}})
    small = copy.deepcopy(shared)
    small["questions"]["next_step"]["calibration"]["bins"].append(
        {"low": 0.0, "high": 0.1, "n": 3, "confidence": 0.05, "accuracy": 0.0}
    )
    with pytest.raises(ValidationError):
        SiteReport.model_validate(small)


def _objects(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(value, dict):
        yield path
        for key, item in value.items():
            yield from _objects(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _objects(item, (*path, index))


def _strings(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(value, str):
        yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*path, index))


def _at(value: Any, path: tuple[Any, ...]) -> Any:
    for part in path:
        value = value[part]
    return value


SHARED = shareable(BENCHMARK)
OBJECTS = list(_objects(SHARED))
STRINGS = list(_strings(SHARED))


@pytest.mark.parametrize("path", OBJECTS, ids=lambda p: ".".join(map(str, p)) or "root")
def test_an_unknown_key_anywhere_is_refused(path: tuple[Any, ...]) -> None:
    payload = copy.deepcopy(SHARED)
    target = _at(payload, path)
    target["note"] = FREE_TEXT
    # Where the keys are names (questions, labels, subgroup values), a key of free text is
    # refused by its pattern; everywhere else any extra key is.
    target[FREE_TEXT] = target[next(iter(target))] if target else 1
    with pytest.raises(ValidationError):
        SiteReport.model_validate(payload)
    # The sender either leaves the key out (fields it rebuilds) or refuses the report.
    try:
        sent = json.dumps(shareable({**BENCHMARK, **payload}))
    except MendariError:
        return
    assert FREE_TEXT not in sent and '"note"' not in sent


@pytest.mark.parametrize("path", STRINGS, ids=lambda p: ".".join(map(str, p)))
def test_free_text_in_any_string_is_refused(path: tuple[Any, ...]) -> None:
    payload = copy.deepcopy(SHARED)
    parent = _at(payload, path[:-1])
    parent[path[-1]] = FREE_TEXT
    with pytest.raises(ValidationError):
        SiteReport.model_validate(payload)


def test_a_report_that_cannot_be_shared_names_fields_never_values() -> None:
    report = copy.deepcopy(BENCHMARK)
    report["providers"]["triage_screen"]["model"] = FREE_TEXT
    with pytest.raises(MendariError) as error:
        shareable(report)
    assert "providers.<key>.model" in str(error.value)
    assert "Jane" not in str(error.value)
    keyed = copy.deepcopy(BENCHMARK)
    keyed["questions"][FREE_TEXT] = keyed["questions"]["next_step"]
    with pytest.raises(MendariError) as error:
        shareable(keyed)
    assert "questions.<key>" in str(error.value) and "Jane" not in str(error.value)
    coded = copy.deepcopy(BENCHMARK)
    coded["errors"] = {FREE_TEXT: "not a count"}
    with pytest.raises(MendariError) as error:
        shareable(coded)
    assert "errors.<key>" in str(error.value) and "Jane" not in str(error.value)


def test_the_summary_names_the_numbers_a_comparison_shows() -> None:
    numbers = summary(shareable(BENCHMARK))
    assert numbers["primary"] == "next_step" and numbers["n"] == BENCHMARK["dataset"]["n"]
    assert numbers["accuracy"] == BENCHMARK["questions"]["next_step"]["accuracy"]
    assert numbers["automation_rate"] == 0.0
    assert numbers["ece"] is not None


@pytest.mark.parametrize(
    "pack_id",
    [
        p
        for p in (
            "ai-guard",
            "clinical-trial-prescreener",
            "patient-inbox-router",
            "prior-auth-preflight",
            "referral-router",
        )
    ],
)
def test_every_shipped_benchmark_can_be_shared(pack_id: str) -> None:
    benchmark = json.loads(
        (resolve_pack(f"mendari/{pack_id}").root / "evals" / "benchmark.json").read_text()
    )
    shared = SiteReport.model_validate(shareable(benchmark))
    assert shared.pack.id == f"mendari/{pack_id}"


# -- composite providers -------------------------------------------------------------------------


def _composite_report(tmp_path: Path) -> tuple[Pack, dict[str, Any]]:
    """The inbox pack with its router a cascade of its two local variants (naive Bayes, then
    rules) and its shipped any_of screen (rules and Jev), evaluated where Jev is not set up:
    Jev fails without a call. At a new version: changed at its own, a registry refuses it (it
    would take the reference pack's place)."""
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    manifest["version"] = "9.9.9"
    variants = manifest["providers"]["router"]["variants"]
    variants["nb_then_rules"] = {
        "type": "cascade",
        "accept_at": 0.9,
        "members": {"naive_bayes": variants["naive_bayes"], "rules": variants["rules"]},
    }
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    pack = load_pack(root)
    decision = Decision(pack, providers={"router": "nb_then_rules", **SCREENED})
    return pack, evaluate(decision, resolve_dataset(pack, "red_flags_holdout"))


def test_a_composite_is_shared_as_recorded_and_the_registry_accepts_it(
    registry: Registry, tmp_path: Path
) -> None:
    """Each composite's model lists its members in order, joined as its type joins them, and a
    member that failed names itself in the error count: the shared report says what the
    evaluation recorded. The registry accepts it for the pack whose members those are, and
    refuses member names that are not the pack's own, as it does every other name."""
    pack, report = _composite_report(tmp_path)
    shared = shareable(report)
    assert shared["providers"] == report["providers"]
    received = SiteReport.model_validate(shared).providers
    assert received["router"].type == "cascade" and " → " in (received["router"].model or "")
    assert composite_members(received["router"]) == ["naive_bayes", "rules"]
    assert received["safety_screen"].type == "any_of"
    assert composite_members(received["safety_screen"]) == ["rules", "jev"]
    assert shared["errors"] == report["errors"] == {"jev:not_configured": report["dataset"]["n"]}

    assert registry.publish(pack).status_code == 201
    publisher = {"authorization": f"Bearer {registry.token}"}
    path = f"/v1/packs/{pack.id}/{pack.version}/reports"
    accepted = registry.client.post(
        path, json={"site": "Site A", "report": shared}, headers=publisher
    )
    assert accepted.status_code == 201, accepted.text

    def renamed(slot: str, model: str) -> dict[str, Any]:
        providers = {**shared["providers"], slot: {**shared["providers"][slot], "model": model}}
        return {**shared, "providers": providers}

    router = shared["providers"]["router"]["model"]
    screen = shared["providers"]["safety_screen"]["model"]
    first, second = router.split(" → ")
    refused = [
        renamed("router", router.replace("rules:", "Jane_Doe:")),
        renamed("router", f"{second} → {first}"),  # the pack's members, out of order
        renamed("router", f"{first} → {second} → {second}"),  # one listed twice
        renamed("safety_screen", screen.replace("jev:", "naive_bayes:")),  # the router's member
        {**shared, "errors": {"Jane_Doe:not_configured": 1}},
        {**shared, "errors": {"jev:not_configured, Jane_Doe:timeout": 1}},
        # As a composite records its failed members: each once, in its order, all its own.
        {**shared, "errors": {"llm:timeout": 1}},  # rules_and_llm's member: it did not run
        {**shared, "errors": {"jev:timeout, rules:timeout": 1}},
        {**shared, "errors": {"jev:timeout, jev:not_configured": 1}},
        {**shared, "errors": {"naive_bayes:timeout, jev:timeout": 1}},
    ]
    for body in refused:
        answer = registry.client.post(
            path, json={"site": "Site A", "report": body}, headers=publisher
        )
        assert answer.status_code == 422, answer.text
        assert "a composite member" in answer.text and "Jane" not in answer.text
    failed = {**shared, "errors": {"rules:timeout, jev:not_configured": 1, "timeout": 2}}
    answer = registry.client.post(
        path, json={"site": "Site B", "report": failed}, headers=publisher
    )
    assert answer.status_code == 201, answer.text
    listed = registry.client.get(f"/v1/packs/{pack.id}/reports").json()["sites"]
    assert [site["site"] for site in listed] == ["Site A", "Site B"]


def test_a_version_published_before_members_were_recorded_refuses_a_composite(
    registry: Registry, tmp_path: Path, db_target: str
) -> None:
    """Its members cannot be checked, so a report from a composite is refused with the reason
    (publish a new version); a report from single providers is still received."""
    pack, report = _composite_report(tmp_path)
    shared = shareable(report)
    assert registry.publish(pack).status_code == 201
    namespace, name = pack.id.split("/")
    key = (namespace, name, pack.version)
    with RegistryStore(db_target) as store, store._db.session(exclusive=True) as db:
        where = "WHERE namespace = ? AND name = ? AND version = ?"
        row = db.one(f"SELECT metadata FROM hub_versions {where}", key)
        assert row is not None
        metadata = json.loads(row["metadata"])
        del metadata["names"]["members"]
        db.all(f"UPDATE hub_versions SET metadata = ? {where}", (json.dumps(metadata), *key))
    publisher = {"authorization": f"Bearer {registry.token}"}
    path = f"/v1/packs/{pack.id}/{pack.version}/reports"
    single = {
        **shared,
        "providers": {
            "safety_screen": {"variant": "rules", "type": "rules", "model": None},
            "router": {"variant": "naive_bayes", "type": "naive_bayes", "model": None},
        },
        "errors": {},
    }
    for body in (shared, {**single, "errors": {"jev:not_configured": 1}}):
        answer = registry.client.post(
            path, json={"site": "Site A", "report": body}, headers=publisher
        )
        assert answer.status_code == 409 and "publish a new version" in answer.text
    answer = registry.client.post(
        path, json={"site": "Site A", "report": single}, headers=publisher
    )
    assert answer.status_code == 201, answer.text


def _provider(type_: str, model: str) -> dict[str, Any]:
    return {"variant": "combo", "type": type_, "model": model}


@pytest.mark.parametrize(
    ("type_", "model"),
    [
        ("any_of", "rules:rules@661449489daf + jev:jev-1.13.0"),
        ("vote", "nb:naive_bayes@37cb3086c82a + llm:anthropic/claude-sonnet-5 + jev:jev"),
        ("cascade", "naive_bayes:naive_bayes@37cb3086c82a → jev:jev-1.13.0"),
        ("cascade", " → ".join(f"m{i}:rules" for i in range(MAX_MEMBERS))),
        ("llm", "anthropic/claude-sonnet-5"),
        ("naive_bayes", "naive_bayes@37cb3086c82a"),
    ],
)
def test_a_composite_lists_its_members_as_its_type_joins_them(type_: str, model: str) -> None:
    assert SharedProvider.model_validate(_provider(type_, model)).model == model


@pytest.mark.parametrize(
    ("type_", "model"),
    [
        ("rules", "rules:rules + jev:jev"),  # only a composite lists members
        ("llm", "anthropic/claude-sonnet-5 → jev"),
        ("cascade", "naive_bayes:naive_bayes + jev:jev"),  # each type has its own joiner
        ("any_of", "rules:rules → jev:jev"),
        ("any_of", "rules:rules"),  # a composite has at least two members
        ("any_of", "rules + jev"),  # each a member, a colon and a model
        ("any_of", "rules:rules +  jev:jev"),
        ("any_of", "rules:rules + jev:jev + "),
        ("vote", "Jane Doe:rules + chest:pain"),
        ("vote", "rules:rules + jev:Jane Doe called"),
        ("vote", " + ".join(f"m{i}:rules" for i in range(MAX_MEMBERS + 1))),
        ("vote", f"rules:rules + jev:{'j' * 129}"),
        ("vote", "rules:rules + jev:jev\n"),  # "$" would admit a final newline
        ("llm", "anthropic/claude-sonnet-5\n"),
    ],
)
def test_anything_else_in_a_model_is_refused(type_: str, model: str) -> None:
    with pytest.raises(ValidationError) as error:
        SharedProvider.model_validate(_provider(type_, model))
    assert "Jane" not in str(error.value.errors(include_input=False))


@pytest.mark.parametrize(
    ("key", "shareable_"),
    [
        ("not_configured", True),
        ("jev:not_configured", True),
        ("jev:not_configured, llm:timeout", True),  # two members failed on the same cases
        (", ".join(f"m{i}:timeout" for i in range(MAX_MEMBERS)), True),
        (", ".join(f"m{i}:timeout" for i in range(MAX_MEMBERS + 1)), False),
        ("jev:not_configured,llm:timeout", False),
        ("jev:not_configured, timeout", False),
        ("jev:not_configured, ", False),
        ("jev:not:configured", False),
        ("Jane Doe:chest_pain", False),
        ("jev:not_configured\n", False),
        ("not_configured\n", False),
        # Mendari's codes only (a closed set), however much a name looks like one.
        ("derived_input_unavailable", True),
        ("Jane_Doe", False),
        ("jev:Jane_Doe", False),
        ("jev:timeout, llm:Jane_Doe", False),
        ("server_error", False),
        ("Timeout", False),
    ],
)
def test_an_error_is_a_code_or_the_members_that_failed(key: str, shareable_: bool) -> None:
    report = {**SHARED, "errors": {key: 1}}
    if shareable_:
        assert SiteReport.model_validate(report).errors == {key: 1}
    else:
        with pytest.raises(ValidationError):
            SiteReport.model_validate(report)


@pytest.mark.parametrize("code", sorted(FAILURE_CODES))
def test_every_error_code_mendari_records_can_be_shared(code: str) -> None:
    errors = {code: 1, f"jev:{code}": 2, f"rules:{code}, jev:{code}": 3}
    assert SiteReport.model_validate({**SHARED, "errors": errors}).errors == errors


def test_the_spec_lists_every_error_code() -> None:
    spec = (ROOT / "docs" / "spec" / "decision-pack-0.2.md").read_text()
    listed = spec[spec.index("a provider error with one of these") : spec.index("No other code")]
    assert set(re.findall(r"`([a-z_]+)`", listed)) == FAILURE_CODES
