# SPDX-License-Identifier: Apache-2.0
"""Shareable evaluations (evals/share.py, LP-22c): counts leave the site, cases never do.

A shared report is `mendari.site-report/1`, a closed schema the client builds from a run's
ledger and the registry checks: whole counts, names and flags only, no rate, small cells
suppressed by one function both sides run, no subgroup, no case id, no dataset identity, no
approver."""

from __future__ import annotations

import copy
import itertools
import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, TypeAdapter, ValidationError

from mendari import Agent, Decision, load_agent
from mendari.core.policy import GovernanceOverride
from mendari.core.registry import resolve_agent
from mendari.errors import FAILURE_CODES, MendariError
from mendari.evals import resolve_dataset
from mendari.evals.harness import Evaluation, evaluation
from mendari.evals.result import OUTCOMES
from mendari.evals.share import (
    MAX_MEMBERS,
    MIN_DATASET,
    SUPPRESSION_RULES,
    Count,
    ErrorKey,
    SharedProvider,
    SiteReportV1,
    composite_members,
    counted_report,
    shareable,
    suppress,
    unchanged_by_suppression,
)
from tests.conftest import (
    CHART_DIR,
    CLINICAL_DIR,
    ROOT,
    TRIAGE_DIR,
    Registry,
    share_signed,
    shared_benchmark,
    site_ticket,
    verified_site,
)

# The test fixtures' agents; the first is the one `shared_benchmark()` shares by default.
TRIAGE, CLINICAL, CHART = (load_agent(d) for d in (TRIAGE_DIR, CLINICAL_DIR, CHART_DIR))
AGENTS = (TRIAGE.id, CLINICAL.id, CHART.id)
SHARED = shared_benchmark()
FREE_TEXT = "Jane Doe, DOB 1960-02-01, called about chest pain"


def _run(agent_id: str, dataset: str, **options: Any) -> Evaluation:
    agent = resolve_agent(agent_id)
    return evaluation(Decision(agent, **options), resolve_dataset(agent, dataset))


def test_case_ids_and_dataset_identity_stay_home(tmp_path: Path) -> None:
    agent = resolve_agent(TRIAGE.id)
    site_file = tmp_path / "north-clinic-cases.jsonl"  # a site's own file, named as it names it
    shutil.copyfile(TRIAGE_DIR / "data" / "test.jsonl", site_file)
    dataset = resolve_dataset(agent, str(site_file))
    run = evaluation(Decision(agent), dataset)
    shared = shareable(run.ledger)
    text = json.dumps(shared)
    assert shared["format"] == "mendari.site-report/1"
    assert all(record["id"] not in text for record in dataset.records)
    assert all(case["c"] not in text for case in run.ledger.cases)  # nor their hashes
    assert shared["dataset"] == {"n": len(dataset.records)}  # no name, no digest
    assert dataset.name == "north-clinic-cases"
    assert dataset.sha256 not in text and dataset.name not in text
    assert "id" not in shared and "created_at" not in shared and "subgroups" not in shared


def test_an_override_is_shared_as_a_fact_not_a_name() -> None:
    approval = GovernanceOverride(approved_by="Dr. A. Person", reference="IRB-2026-0042")
    run = _run(CLINICAL.id, "test", governance=approval)  # risk level 2
    shared = shareable(run.ledger)
    assert shared["policy"]["governance_override"] is True
    assert "Person" not in json.dumps(shared) and "IRB-2026" not in json.dumps(shared)


def _leaves(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, (*path, index))
    else:
        yield path, value


@pytest.mark.parametrize("agent_id", AGENTS)
def test_a_shared_report_holds_whole_counts_and_no_rate(agent_id: str) -> None:
    shared = shared_benchmark(agent_id)
    for path, leaf in _leaves(shared):
        assert leaf is None or isinstance(leaf, int | str | bool), path
        assert not isinstance(leaf, float), path
    text = json.dumps(shared)
    for rate in ("accuracy", "macro_f1", "ece", "brier", "precision", "recall", "latency", "usd"):
        assert f'"{rate}' not in text


def _at(value: Any, path: tuple[Any, ...]) -> Any:
    for part in path:
        value = value[part]
    return value


def _set(value: Any, path: tuple[Any, ...], new: Any) -> Any:
    copied = copy.deepcopy(value)
    _at(copied, path[:-1])[path[-1]] = new
    return copied


COUNTS = [
    path for path, leaf in _leaves(SHARED) if isinstance(leaf, int) and not isinstance(leaf, bool)
]


@pytest.mark.parametrize("path", COUNTS, ids=lambda p: ".".join(map(str, p)))
def test_a_rate_or_a_fraction_anywhere_is_refused_on_both_sides(path: tuple[Any, ...]) -> None:
    """Every number is a whole count: a rate, a fraction, or a whole number written as a float
    or a string is refused by the model both sides validate with."""
    for bad in (0.5, 0.9876, 3.0, "3", True):
        with pytest.raises(ValidationError):
            SiteReportV1.model_validate(_set(SHARED, path, bad))


# A hand-made run with small cells, to try to recover each suppressed one.
_BINS = [(0, 0)] * 7 + [(30, 11), (40, 10), (25, 0)]  # rights 95, wrongs 21
FIXTURE: dict[str, Any] = {
    "format": "mendari.site-report/1",
    "agent": {"id": "mendari-test/hand-made", "version": "1.0.0", "digest": "sha256:" + "0" * 64},
    "providers": {"router": {"variant": "rules", "type": "rules", "model": None}},
    "policy": {
        "risk_level": 2,
        "thresholds": {"auto_bp": 9000, "review_bp": 6000},
        "governance_override": False,
    },
    "remote_consent": False,
    "dataset": {"n": 120},
    "primary": "triage",
    "questions": {
        "triage": {
            "n": 120,
            "right": 95,
            "confusion": {
                "routine": {"routine": 60, "soon": 4, "urgent": 0},
                "soon": {"routine": 12, "soon": 27, "urgent": 2},
                "urgent": {"routine": 3, "soon": 0, "urgent": 8},
            },
            "calibration": [
                {"bin": i, "right": right, "wrong": wrong} for i, (right, wrong) in enumerate(_BINS)
            ],
        }
    },
    "decisions": {
        "outcomes": {"AUTO": 30, "HUMAN_REVIEW": 84, "ABSTAIN": 5, "BLOCK": 1},
        "false_automation": 2,
        "critical": [
            {"question": "triage", "label": "urgent", "n": 12, "detected": 8, "auto_missed": 1}
        ],
    },
    "tokens": {"input": 0, "output": 0},
}


def _original(null: tuple[Any, ...]) -> int:
    question = FIXTURE["questions"]["triage"]
    if null[0] == "confusion":
        return int(question["confusion"][null[1]][null[2]])
    if null[0] == "bins":
        return int(question["calibration"][null[1]][null[2]])
    return int(FIXTURE["decisions"]["outcomes"][null[1]])


def _solutions(suppressed: dict[str, Any]) -> dict[tuple[Any, ...], set[int]]:
    """Every value each null cell can take, given all the payload states: the confusion's
    diagonal adds up to `right`, as do the bins' `right`; its off-diagonal cells and the bins'
    `wrong` count the same cases; the outcomes add up to n; a critical label's row holds at
    most its n cases (its own cell is `detected`); and no count is negative. Worked out here
    from the payload alone, not with the module's own equations."""
    question = suppressed["questions"]["triage"]
    confusion, bins = question["confusion"], question["calibration"]
    critical = suppressed["decisions"]["critical"][0]
    outcomes = suppressed["decisions"]["outcomes"]
    n = suppressed["dataset"]["n"]
    labels = list(confusion)
    nulls: list[tuple[Any, ...]] = [
        ("confusion", t, p) for t in labels for p in labels if confusion[t][p] is None
    ]
    nulls += [
        ("bins", i, k) for i, b in enumerate(bins) for k in ("right", "wrong") if b[k] is None
    ]
    nulls += [("outcomes", o) for o in OUTCOMES if outcomes[o] is None]

    def get(values: dict[tuple[Any, ...], int], *where: Any) -> int:
        if where[0] == "confusion":
            known = confusion[where[1]][where[2]]
        elif where[0] == "bins":
            known = bins[where[1]][where[2]]
        else:
            known = outcomes[where[1]]
        return values[where] if known is None else int(known)

    def holds(values: dict[tuple[Any, ...], int]) -> bool:
        diagonal = sum(get(values, "confusion", t, t) for t in labels)
        off = sum(get(values, "confusion", t, p) for t in labels for p in labels if p != t)
        rights = sum(get(values, "bins", i, "right") for i in range(10))
        wrongs = sum(get(values, "bins", i, "wrong") for i in range(10))
        total = sum(get(values, "outcomes", o) for o in OUTCOMES)
        row = sum(get(values, "confusion", "urgent", p) for p in labels)
        return (
            diagonal == question["right"] == rights
            and off == wrongs
            and total == n
            and row <= critical["n"]
        )

    # Each equation's cells are its own, so each is solved on its own (the critical row's
    # bound is within the off-diagonal one).
    groups: dict[str, list[tuple[Any, ...]]] = {"diagonal": [], "right": [], "off": [], "out": []}
    for null in nulls:
        if null[0] == "confusion":
            groups["diagonal" if null[1] == null[2] else "off"].append(null)
        elif null[0] == "bins":
            groups["right" if null[2] == "right" else "off"].append(null)
        else:
            groups["out"].append(null)
    found: dict[tuple[Any, ...], set[int]] = {null: set() for null in nulls}
    for group in groups.values():
        rest = [null for null in nulls if null not in group]
        truth = {null: _original(null) for null in rest}  # the other groups at their values
        for values in itertools.product(range(0, 31), repeat=len(group)):
            assignment = {**truth, **dict(zip(group, values, strict=True))}
            if holds(assignment):
                for null in group:
                    found[null].add(assignment[null])
    return found


def test_a_suppressed_cell_cannot_be_recovered_from_the_totals() -> None:
    suppressed = suppress(FIXTURE)
    assert suppressed == suppress(suppressed)  # applied twice, it changes nothing
    question = suppressed["questions"]["triage"]
    # 1 to 9 is suppressed; the critical label's own cell is its detected count, so it shows.
    assert question["confusion"]["routine"]["soon"] is None
    assert question["confusion"]["urgent"]["routine"] is None
    assert question["confusion"]["urgent"]["urgent"] == 8
    assert suppressed["decisions"]["outcomes"]["BLOCK"] is None
    found = _solutions(suppressed)
    assert len(found) >= 5
    for null, values in found.items():
        assert len(values) > 1, (null, values)  # never worked out by subtraction or bounds
        assert _original(null) in values  # and the truth is among them


def test_one_small_cell_alone_in_its_equation_gets_a_complement() -> None:
    lone = copy.deepcopy(FIXTURE)
    lone["decisions"]["outcomes"] = {"AUTO": 30, "HUMAN_REVIEW": 85, "ABSTAIN": 5, "BLOCK": 0}
    outcomes = suppress(lone)["decisions"]["outcomes"]
    # ABSTAIN (5) alone would be n - the rest: the smallest shown cell (BLOCK, a zero) goes too.
    assert outcomes == {"AUTO": 30, "HUMAN_REVIEW": 85, "ABSTAIN": None, "BLOCK": None}


def test_safety_counts_are_shared_exactly_and_small_runs_are_not_shared() -> None:
    suppressed = suppress(FIXTURE)
    assert suppressed["decisions"]["critical"] == FIXTURE["decisions"]["critical"]
    assert suppressed["decisions"]["false_automation"] == 2  # under 10, shared exactly
    small = copy.deepcopy(FIXTURE)
    small["dataset"]["n"] = MIN_DATASET - 1
    with pytest.raises(MendariError, match="fewer than 100 cases"):
        suppress(small)
    with pytest.raises(ValidationError):
        SiteReportV1.model_validate(small)
    run = _run(TRIAGE.id, "critical")  # 40 cases
    with pytest.raises(MendariError, match="fewer than 100 cases"):
        shareable(run.ledger)


def test_the_receiver_refuses_what_suppression_would_change() -> None:
    assert unchanged_by_suppression(SiteReportV1.model_validate(SHARED))
    assert unchanged_by_suppression(SiteReportV1.model_validate(suppress(FIXTURE)))
    for changed in (FIXTURE, _set(suppress(FIXTURE), ("decisions", "outcomes", "BLOCK"), 1)):
        assert not unchanged_by_suppression(SiteReportV1.model_validate(changed))


def _types(annotation: Any) -> list[Any]:
    found = [annotation]
    for argument in getattr(annotation, "__args__", ()) or ():
        found += _types(argument)
    return found


def _field_paths(model: type[BaseModel], prefix: str = "") -> Iterator[str]:
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        nested = [
            kind
            for kind in _types(field.annotation)
            if isinstance(kind, type) and issubclass(kind, BaseModel)
        ]
        if nested:
            for kind in nested:
                yield from _field_paths(kind, path + ".")
        else:
            yield path


def test_every_field_has_a_written_suppression_rule() -> None:
    spec = (ROOT / "docs" / "spec" / "evaluation-reports.md").read_text()
    paths = list(_field_paths(SiteReportV1))
    assert "questions.calibration.wrong" in paths and "decisions.critical.n" in paths
    for path in paths:
        rule = next(
            (key for key in SUPPRESSION_RULES if path == key or path.startswith(key + ".")), None
        )
        assert rule is not None, path
        assert f"| `{rule}` |" in spec, rule


def test_the_counts_are_the_ledgers() -> None:
    """Each count is counted from the ledger, and agrees with the report computed from it."""
    run = _run(TRIAGE.id, "test")
    counts, report = counted_report(run.ledger), run.report
    for name, question in counts["questions"].items():
        full = report["questions"][name]
        assert question["n"] == full["n"] and question["right"] == full["proportion"]["k"]
        assert question["confusion"] == {
            truth: {label: count for label, count in row.items() if label != "none"}
            for truth, row in full["confusion"].items()
        }
        binned = {round(b["low"] * 10): b for b in full["calibration"]["bins"]}
        for b in question["calibration"]:
            n = b["right"] + b["wrong"]
            assert n == (binned[b["bin"]]["n"] if b["bin"] in binned else 0)
            if n:
                assert b["right"] == round(binned[b["bin"]]["accuracy"] * n)
    decisions = report["decisions"]
    assert counts["decisions"]["outcomes"] == decisions["outcome_counts"]
    assert counts["decisions"]["false_automation"] == report["proportions"]["false_automation"]["k"]
    assert counts["decisions"]["critical"] == [
        {key: item[key] for key in ("question", "label", "n", "detected", "auto_missed")}
        for item in decisions["critical"]
    ]
    assert counts["tokens"] == {
        "input": report["cost"]["input_tokens"],
        "output": report["cost"]["output_tokens"],
    }
    assert counts["dataset"]["n"] == report["dataset"]["n"]


@pytest.mark.parametrize("agent_id", AGENTS)
def test_every_shipped_benchmark_can_be_shared(agent_id: str) -> None:
    shared = SiteReportV1.model_validate(shared_benchmark(agent_id))
    assert shared.agent.id == agent_id
    assert unchanged_by_suppression(shared)


def _objects(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(value, dict):
        yield path
        for key, item in value.items():
            yield from _objects(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _objects(item, (*path, index))


OBJECTS = list(_objects(SHARED))
STRINGS = [path for path, leaf in _leaves(SHARED) if isinstance(leaf, str)]


@pytest.mark.parametrize("path", OBJECTS, ids=lambda p: ".".join(map(str, p)) or "root")
def test_an_unknown_key_anywhere_is_refused(path: tuple[Any, ...]) -> None:
    payload = copy.deepcopy(SHARED)
    target = _at(payload, path)
    target["note"] = FREE_TEXT
    # Where the keys are names (questions, labels), a key of free text is refused by its
    # pattern; everywhere else any extra key is.
    target[FREE_TEXT] = target[next(iter(target))] if target else 1
    with pytest.raises(ValidationError):
        SiteReportV1.model_validate(payload)


@pytest.mark.parametrize("path", STRINGS, ids=lambda p: ".".join(map(str, p)))
def test_free_text_in_any_string_is_refused(path: tuple[Any, ...]) -> None:
    with pytest.raises(ValidationError):
        SiteReportV1.model_validate(_set(SHARED, path, FREE_TEXT))


def test_a_run_that_cannot_be_shared_names_fields_never_values() -> None:
    run = _run(TRIAGE.id, "test")
    header = copy.deepcopy(run.ledger.header)
    slot = next(iter(header["providers"]))
    header["providers"][slot]["model"] = FREE_TEXT
    with pytest.raises(MendariError) as error:
        shareable(type(run.ledger)(header, run.ledger.cases))
    assert "providers.<key>.model" in str(error.value)
    assert "Jane" not in str(error.value)


# -- composite providers -------------------------------------------------------------------------


def _composite_run(tmp_path: Path) -> tuple[Agent, Evaluation]:
    """The triage fixture with each slot on a `check` composite (each slot's agent and its keyword
    rules; the router's members named apart from the screen's), evaluated offline on their
    canned answers. At a new version: changed at its own, a registry refuses it (it would take
    the reference agent's place)."""
    root = tmp_path / "agent"
    shutil.copytree(TRIAGE_DIR, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["version"] = "9.9.9"
    variants = manifest["providers"]["router"]["variants"]
    variants["checked"] = {
        "type": "check",
        "members": {"service": variants["agent"], "keywords": variants["rules"]},
    }
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    agent = load_agent(root)
    decision = Decision(agent, providers={"router": "checked", "screen": "checked"})
    return agent, evaluation(decision, resolve_dataset(agent, "test"))


def test_a_composite_is_shared_as_recorded_and_the_registry_accepts_it(
    registry: Registry, tmp_path: Path
) -> None:
    """Each composite's model lists its members in order, joined as its type joins them: the
    shared report says what the evaluation recorded. The registry accepts it for the agent whose
    members those are, and refuses member names that are not the agent's own, as it does every
    other name."""
    agent, run = _composite_run(tmp_path)
    shared = shareable(run.ledger, remote_consent=True)  # agents: off the site's machines
    assert shared["providers"] == run.report["providers"]
    received = SiteReportV1.model_validate(shared).providers
    assert received["router"].type == "check" and " + " in (received["router"].model or "")
    assert composite_members(received["router"]) == ["service", "keywords"]
    assert received["screen"].type == "check"
    assert composite_members(received["screen"]) == ["agent", "second"]

    assert registry.publish(agent).status_code == 201
    site = verified_site(registry, tmp_path)
    accepted = share_signed(registry, site, shared)
    assert accepted.status_code == 201, accepted.text
    other = verified_site(registry, tmp_path, org="south-clinic")
    issued = site_ticket(registry, other, shared)

    def renamed(slot: str, model: str) -> dict[str, Any]:
        providers = {**shared["providers"], slot: {**shared["providers"][slot], "model": model}}
        return {**shared, "providers": providers}

    router = shared["providers"]["router"]["model"]
    screen = shared["providers"]["screen"]["model"]
    first, second = router.split(" + ")
    refused = [
        renamed("router", router.replace("keywords:", "Jane_Doe:")),
        renamed("router", f"{second} + {first}"),  # the agent's members, out of order
        renamed("router", f"{first} + {second} + {second}"),  # one listed twice
        renamed("screen", screen.replace("second:", "keywords:")),  # the router's member
    ]
    for body in refused:
        answer = share_signed(registry, other, body, issued)
        assert answer.status_code == 422, answer.text
        assert "a composite member" in answer.text and "Jane" not in answer.text


def _provider(type_: str, model: str) -> dict[str, Any]:
    return {"variant": "combo", "type": type_, "model": model}


@pytest.mark.parametrize(
    ("type_", "model"),
    [
        ("check", "agent:triage-screen@2.0.0 + second:rules@661449489daf"),
        ("check", "agent:anthropic/claude-sonnet-5 + second:llm-2"),
        ("check", " + ".join(f"m{i}:rules" for i in range(MAX_MEMBERS))),
        ("llm", "anthropic/claude-sonnet-5"),
        ("rules", "rules@661449489daf"),
    ],
)
def test_a_composite_lists_its_members_as_its_type_joins_them(type_: str, model: str) -> None:
    assert SharedProvider.model_validate(_provider(type_, model)).model == model


@pytest.mark.parametrize(
    ("type_", "model"),
    [
        ("rules", "rules:rules + agent:agent"),  # only a composite lists members
        ("llm", "anthropic/claude-sonnet-5 → agent"),
        ("check", "rules:rules → agent:agent"),  # a composite joins its members its own way
        ("check", "rules:rules"),  # a composite has at least two members
        ("check", "rules + agent"),  # each a member, a colon and a model
        ("check", "rules:rules +  agent:agent"),
        ("check", "rules:rules + agent:agent + "),
        ("check", "Jane Doe:rules + chest:pain"),
        ("check", "rules:rules + agent:Jane Doe called"),
        ("check", " + ".join(f"m{i}:rules" for i in range(MAX_MEMBERS + 1))),
        ("check", f"rules:rules + agent:{'j' * 129}"),
        ("check", "rules:rules + agent:agent\n"),  # "$" would admit a final newline
        ("llm", "anthropic/claude-sonnet-5\n"),
    ],
)
def test_anything_else_in_a_model_is_refused(type_: str, model: str) -> None:
    with pytest.raises(ValidationError) as error:
        SharedProvider.model_validate(_provider(type_, model))
    assert "Jane" not in str(error.value.errors(include_input=False))


ERRORS = TypeAdapter(dict[ErrorKey, Count])  # errors counted by their key


@pytest.mark.parametrize(
    ("key", "valid"),
    [
        ("not_configured", True),
        ("agent:not_configured", True),
        ("agent:not_configured, llm:timeout", True),  # two members failed on the same cases
        (", ".join(f"m{i}:timeout" for i in range(MAX_MEMBERS)), True),
        (", ".join(f"m{i}:timeout" for i in range(MAX_MEMBERS + 1)), False),
        ("agent:not_configured,llm:timeout", False),
        ("agent:not_configured, timeout", False),
        ("agent:not_configured, ", False),
        ("agent:not:configured", False),
        ("Jane Doe:chest_pain", False),
        ("agent:not_configured\n", False),
        ("not_configured\n", False),
        # Mendari's codes only (a closed set), however much a name looks like one.
        ("credential_unavailable", True),
        ("Jane_Doe", False),
        ("agent:Jane_Doe", False),
        ("agent:timeout, llm:Jane_Doe", False),
        ("server_error", False),
        ("Timeout", False),
    ],
)
def test_an_error_is_a_code_or_the_members_that_failed(key: str, valid: bool) -> None:
    if valid:
        assert ERRORS.validate_python({key: 1}) == {key: 1}
    else:
        with pytest.raises(ValidationError):
            ERRORS.validate_python({key: 1})


@pytest.mark.parametrize("code", sorted(FAILURE_CODES))
def test_every_error_code_mendari_records_can_be_counted(code: str) -> None:
    errors = {code: 1, f"agent:{code}": 2, f"rules:{code}, agent:{code}": 3}
    assert ERRORS.validate_python(errors) == errors


def test_the_spec_lists_every_error_code() -> None:
    spec = (ROOT / "docs" / "spec" / "agent-manifest-0.2.md").read_text()
    listed = spec[spec.index("a provider error with one of these") : spec.index("No other code")]
    assert set(re.findall(r"`([a-z_]+)`", listed)) == FAILURE_CODES


@pytest.mark.parametrize(
    "where",
    [
        # Cells of equations the benchmark's report leaves less slack in (its suppressed cells
        # add up to fewer than 20) than any of these values would need.
        ("questions", TRIAGE.manifest.policy.primary, "calibration", 9, "wrong"),
        ("questions", TRIAGE.manifest.policy.primary, "calibration", 8, "right"),
        ("questions", TRIAGE.manifest.policy.primary, "right"),
        ("decisions", "outcomes", "HUMAN_REVIEW"),
    ],
    ids=lambda w: ".".join(map(str, w)),
)
@pytest.mark.parametrize("value", [20, 200, 10_000])
def test_counts_that_do_not_add_up_are_refused_at_once(where: tuple[Any, ...], value: int) -> None:
    """The adversarial review of LP-22f: counts that passed the model but could not add up kept
    `suppress` running for good (inside the registry's write lock, once sites sent them). They
    are refused, by the model or by `suppress`, in one pass."""
    changed = _set(SHARED, where, value)
    try:
        report = SiteReportV1.model_validate(changed)
    except ValidationError:
        return
    assert not unchanged_by_suppression(report)
    with pytest.raises(MendariError, match="do not add up"):
        suppress(changed)
