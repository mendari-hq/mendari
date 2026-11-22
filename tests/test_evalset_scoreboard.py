# SPDX-License-Identifier: Apache-2.0
"""The hidden-set scoreboard through the existing grading route (LP-17k, `evals/hidden.py`
`scoreboard_graded`, `server/app.py` `add_grading_routes`; the grading method 1.0's sections 3
to 9): `POST /api/evalsets/{set_id}/runs` with `arms` and `declared` decides every declared arm
on the same cases in one grading read, and answers `mendari.hidden_scoreboard/1`, a closed
model of aggregates only.

- Each declared arm's configuration is refused before any case is read as `variants` is
  (`remote_provider`, `unknown_variant`, `wrong_pack`), or when it is not of the arm it is
  declared under (`wrong_arm`); `arms` comes with `declared` and never with `variants`.
- One read, one chained `evalset.graded`, one of the set's runs; every arm decides the same
  records in the same order, so their ledgers pair and the ranking can be on (off, "one arm
  graded", while only the deterministic arm is local).
- Undeclared arms read "not graded here" (their configurations are remote) or "not
  applicable".
- The answer holds the 17d figures (every proportion k of n with its interval, every mean with
  its bootstrap interval), the attestations (draft until the method is in effect), the evidence
  on distinct positives, the banner and the scope with the set's commitment; never cost,
  latency, subgroups, equal-mass bins, a case's id or hash, the dataset's name or hash, or a
  ledger's id. A set of twenty urgent positives is insufficient evidence. Weights reach the
  prevalence-dependent figures and never detection. Two reads give the same body, its run
  block apart.
- Without `arms`, the answer is `mendari.hidden_result/1`, as before.
- The results store holds the answer as it was answered (closed schema, named by its body,
  never recomputed), the page leaves it out, and a signature covers its run's number.
- A 600-case run fits the route's deadline, and `mendari audit verify --evalsets` passes after a
  run (SQLite and Postgres).
Keys are made per test, cases are hand-written synthetic text."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import secrets
import shlex
import shutil
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ValidationError

from mendari import Decision, Pack
from mendari.cli import main
from mendari.core import clock
from mendari.core.checkpoint import public_text
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import EvalsetStore, GradingRead
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals import arms, bands, grade, hidden, method, results_store, scoreboard, signing
from mendari.evals import stats as statistics
from mendari.evals.harness import Dataset, resolve_dataset
from mendari.evals.hidden import (
    HIDDEN_RESULT_FORMAT,
    HIDDEN_SCOREBOARD_FORMAT,
    AsSampled,
    AutomatedLabel,
    Band,
    Banner,
    Bound,
    Declared,
    DetectionBand,
    Distinct,
    Ece,
    Evidence,
    FalseAutomationCeiling,
    Family,
    FamilySeeds,
    FamilyTest,
    Figure,
    GradedConfiguration,
    GradeMethod,
    HiddenResult,
    HiddenScoreboard,
    Mean,
    NoCriticalAutomated,
    NotEstimable,
    RankBands,
    Ranked,
    RankingOff,
    RankingOn,
    RecallFloor,
    RunBlock,
    ScoredArm,
    ScoredCritical,
    ScoredDisclosure,
    ScoredFigures,
    ScoredGrade,
    ScoredScope,
    ScoredSet,
    ShortLabel,
    Term,
    Unranked,
    UnrankedReason,
    Weighted,
    WeightedShare,
    WeightedWithheld,
)
from mendari.server.app import GRADING_REFUSALS
from tests.conftest import ROOT, calm_jev, hand_written
from tests.test_evalset_grading import (
    LOCAL,
    UNTOUCHED,
    World,
    deployment,
    dump,
    graded_events,
    keys_of,
    private_of,
    started,
)
from tests.test_evalset_labelling import label_all, labellers
from tests.test_evalset_store import CLINIC, MARKS, a_set
from tests.test_scoreboard import Built, build, records_of, written
from tests.test_scoreboard_rank import (
    BOTH,
    CLASSIFIER,
    DETERMINISTIC,
    LLM,
    missed_urgent,
    worse_accuracy,
)
from tests.test_scoreboard_rank import arm as forged

# The inbox's declaration (`mendari.declared/1`, LP-17h): its deterministic arm's `local`
# configuration, the one a hidden set grades. The route cites its SHA-256 and never reads it; the
# results store holds it (`a_store`) and checks a new answer against it.
DECLARATION: dict[str, Any] = {
    "format": "mendari.declared/1",
    "pack": "mendari/patient-inbox-router",
    "date": "2026-10-17",
    "arms": {"deterministic": {"local": LOCAL}},
}
DECLARED = hashlib.sha256(json.dumps(DECLARATION).encode()).hexdigest()
DAY = datetime.date(2026, 10, 17)  # the day a test's answers are measured on, US Eastern
SCOREBOARD = {"arms": {"deterministic": LOCAL}, "declared": DECLARED}
URGENT_TEXT = "crushing chest pain spreading to my left arm and I cannot breathe"
ROUTINE_TEXT = "my knee aches after the long walk"
RED, CALM = f"{MARKS['stratum']}-red", f"{MARKS['stratum']}-calm"
RUNBOOK = ROOT / "docs" / "operations" / "evalsets.md"
VERCEL = ROOT / "vercel.json"
# Every closed model of the scoreboard's answer, with the fields it holds: a field added fails
# here until it is listed, and reviewed for what it would show (never cost, latency, a subgroup,
# an equal-mass bin, a case, its id or hash, the dataset's name or hash, or a ledger's id).
PINNED: dict[type[BaseModel], set[str]] = {
    HiddenScoreboard: {
        "format",
        "scope",
        "seeds",
        "arms",
        "banner",
        "ranking",
        "supersedes",
        "run",
        "body_sha256",
    },
    ScoredScope: {
        "pack",
        "status",
        "set",
        "method",
        "measured_on",
        "evaluation",
        "disclosure",
        "declared",
    },
    ScoredSet: {"kind", "id", "commitment", "n", "distinct_inputs", "norm"},
    ScoredDisclosure: {"status", "statements"},
    GradeMethod: {"version", "sha256", "in_effect"},
    RunBlock: {"run", "runs_left"},
    ScoredArm: {"arm", "name", "status", "configuration", "providers", "figures", "grade"},
    GradedConfiguration: {"providers", "local", "remote_types"},
    ScoredFigures: {
        "critical",
        "false_automation",
        "auto_error_rate",
        "automation",
        "accuracy",
        "macro_f1",
        "brier",
        "brier_reliability",
        "ece",
        "weighted",
    },
    ScoredCritical: {"question", "label", "detected", "automated", "distinct", "bound", "evidence"},
    Figure: {"k", "n", "low", "high"},
    Bound: {"k", "n", "low", "high", "level", "labels"},
    Distinct: {"positives", "detected"},
    Evidence: {"status", "needed"},
    Mean: {"estimate", "low", "high"},
    Term: {"status", "estimate", "low", "high"},
    NotEstimable: {"status", "reason"},
    Ece: {"status", "value"},
    AsSampled: {"status"},
    WeightedWithheld: {"status", "reason"},
    Weighted: {"status", "accuracy", "false_automation", "automation", "auto_error_rate"},
    WeightedShare: {"n", "estimate", "effective_n", "low", "high"},
    ScoredGrade: {"method", "attestations"},
    NoCriticalAutomated: {"attestation", "state", "labels", "needed", "draft"},
    AutomatedLabel: {"question", "label", "automated", "distinct_positives"},
    RecallFloor: {"attestation", "question", "label", "state", "floor", "bound", "needed", "draft"},
    FalseAutomationCeiling: {
        "attestation",
        "state",
        "ceiling",
        "false_automation",
        "level",
        "needed",
        "draft",
    },
    Banner: {"short"},
    ShortLabel: {"arm", "configuration", "question", "label", "positives", "needed"},
    RankingOff: {"status", "reason"},
    RankingOn: {"status", "method", "draft", "graded", "ranked", "unranked", "family"},
    Ranked: {"rank", "interval", "arm", "configuration", "bands"},
    RankBands: {"detection", "false_automation", "accuracy", "brier"},
    DetectionBand: {"question", "label", "value", "position", "band"},
    Band: {"value", "position", "band"},
    Unranked: {"arm", "configuration", "reasons"},
    UnrankedReason: {"attestation", "question", "label", "state"},
    Family: {"of", "alpha", "size", "seeds", "tests"},
    FamilySeeds: {"bootstrap", "resamples"},
    FamilyTest: {
        "name",
        "arms",
        "criterion",
        "question",
        "label",
        "n",
        "discordant",
        "difference",
        "p",
        "adjusted",
        "reject",
        "favours",
    },
}
# What a public entry, a `mendari.eval/2` result or a ledger holds of its cases or its dataset,
# and no scoreboard answer may.
PER_CASE = {
    "missed_cases",
    "misclassified",
    "subgroups",
    "dataset",
    "ledger",
    "inputs",
    "equal_mass",
    "bins",
    "case",
    "cases",
    "latency_ms",
    "cost_per_1000",
    "key",
    "models",
}


@pytest.fixture
def world(tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """The deployment on SQLite, its answers dated `DAY`."""
    monkeypatch.setattr(clock, "today", lambda: DAY)
    keys = deployment(monkeypatch, "memberships")
    with started(str(tmp_path / "audit.db"), inbox_pack, keys) as found:
        yield found


def inbox_cases(numbers: range, *, urgent: bool, stratum: str) -> list[dict[str, Any]]:
    """Hand-written cases numbered as `numbers` (the number is what a labeller reads), urgent
    (chest pain, which the rules flag) or routine (a knee), in `stratum`, every mark planted."""
    text = URGENT_TEXT if urgent else ROUTINE_TEXT
    return [
        {
            **hand_written(
                f"{MARKS['input']} case {number}: {text}",
                author=MARKS["author"],
                stratum=stratum,
                language=MARKS["language"],
            ),
            "labels": {"urgency": "urgent" if urgent else "routine"},
        }
        for number in numbers
    ]


def scored_set(
    world: World,
    *,
    urgent: int,
    routine: int,
    per_stratum: int | None = None,
    routine_destination: str = "nurse",
    max_runs: int = 3,
) -> str:
    """A frozen set of `urgent` chest-pain cases (stratum `RED`, labelled urgent) and `routine`
    knee cases (stratum `CALM`, labelled routine, sent to `routine_destination`), `per_stratum`
    drawn from each (all, by default: every weight 1), labelled blind by two labellers who
    agree; the undrawn dropped at freeze. Its id."""
    set_id = a_set(world.hidden, world.pack)
    world.hidden.add_cases(
        set_id,
        inbox_cases(range(urgent), urgent=True, stratum=RED)
        + inbox_cases(range(urgent, urgent + routine), urgent=False, stratum=CALM),
    )
    world.hidden.sample(set_id, per_stratum=per_stratum or max(urgent, routine))
    if CLINIC not in world.users:
        world.users[CLINIC] = labellers(world.store, 2, org=CLINIC)
    users = world.users[CLINIC]
    world.hidden.assign(set_id, users)

    def script(number: int, nth: int) -> Mapping[str, str]:
        if number < urgent:
            return {"destination": "nurse", "urgency": "urgent"}
        return {"destination": routine_destination, "urgency": "routine"}

    label_all(world.hidden, users, script)
    world.hidden.freeze(
        set_id, max_runs=max_runs, max_age_days=30, resamples=20, drop_unsampled=True
    )
    return set_id


def answered(world: World, set_id: str, grader: dict[str, str], **body: Any) -> dict[str, Any]:
    """A scoreboard run's answer, asserted a `200`."""
    sent = {**SCOREBOARD, **body}
    found = world.run(set_id, grader, **sent)
    assert found.status_code == 200, found.text
    shown: dict[str, Any] = found.json()
    assert shown["format"] == HIDDEN_SCOREBOARD_FORMAT
    return shown


def graded_arm(answer: Mapping[str, Any]) -> dict[str, Any]:
    (found,) = [item for item in answer["arms"] if item["status"] == hidden.GRADED]
    return dict(found)


def hidden_dataset(records: Sequence[Mapping[str, Any]]) -> Dataset:
    """`records` as a grading read hands a hidden set's cases over: in memory, named by the
    set's id, with no hash and a random case key."""
    set_id = str(uuid.uuid4())
    return Dataset(
        name=set_id,
        path=Path(set_id),
        records=[dict(record) for record in records],
        sha256="",
        case_key=secrets.token_bytes(32),
    )


def declared(pack: Pack, providers: dict[str, str], arm: str | None = None) -> Declared:
    """An arm as the route declares it: `providers`' configuration and its local Decision. With
    `arm`, a local configuration standing in for that arm (a test's own: under method 1.0 only
    the deterministic arm is local), so more than one arm can be graded in one read."""
    decision = Decision(pack, providers=providers, origin_cap=False)
    configuration = hidden.configuration_of(decision)
    assert configuration is not None and configuration.local
    if arm is not None:
        configuration = arms.Configuration(dict(configuration.providers), arm, True, ())
    return Declared(configuration.arm, configuration, decision)


# -- one read, every declared arm ------------------------------------------------------------------


def test_every_declared_arm_is_decided_on_the_same_cases_in_one_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the route: one grading read, one chained `evalset.graded` (one of the set's
    runs), every case decided once by the declared arm, the configuration it declared. In
    memory, with a second local configuration standing in for another arm: both decide every
    record of the one read in the same order, their ledgers pair (one case key), so the
    ranking is on, over two arms, and their entries are of the same cases."""
    set_id = scored_set(world, urgent=4, routine=4)
    decided: list[Any] = []
    reads: list[str] = []
    real_decide, real_read = Decision.decide, EvalsetStore.grade_read

    def deciding(self: Decision, inputs: Any, **kwargs: Any) -> Any:
        decided.append((id(self), json.dumps(inputs, sort_keys=True)))
        return real_decide(self, inputs, **kwargs)

    def reading(self: EvalsetStore, read_id: str, *args: Any, **kwargs: Any) -> Any:
        reads.append(read_id)
        return real_read(self, read_id, *args, **kwargs)

    with monkeypatch.context() as spying:
        spying.setattr(Decision, "decide", deciding)
        spying.setattr(EvalsetStore, "grade_read", reading)
        shown = answered(world, set_id, world.grader())
    assert reads == [set_id]
    (read,) = graded_events(world, set_id)
    assert (read["payload"]["n"], read["payload"]["run"]) == (8, 1)
    assert (shown["run"], shown["scope"]["set"]["n"]) == ({"run": 1, "runs_left": 2}, 8)
    assert len(decided) == 8 and len({who for who, _ in decided}) == 1
    graded = graded_arm(shown)
    assert graded["arm"] == "deterministic"
    assert graded["configuration"] == {"providers": LOCAL, "local": True, "remote_types": []}
    assert shown["ranking"] == {"status": "off", "reason": bands.ONE_ARM}

    pack = resolve_pack(scoreboard_pack := "mendari/patient-inbox-router")
    dataset = hidden_dataset(resolve_dataset(pack, "red_flags_holdout").records)
    both = [
        declared(pack, {"safety_screen": "rules", "router": "rules"}, arm="llm"),
        declared(pack, LOCAL),
    ]
    decided.clear()
    with monkeypatch.context() as spying:
        spying.setattr(Decision, "decide", deciding)
        found = hidden.scoreboard_graded(
            both,
            dataset,
            GradingRead(dataset.name, "c" * 64, len(dataset.records), 1, 0),
            declaration=DECLARED,
            measured_on=DAY,
        ).model_dump(mode="json")
    seen: dict[int, list[str]] = {}
    for who, inputs in decided:
        seen.setdefault(who, []).append(inputs)
    expected = [json.dumps(record["input"], sort_keys=True) for record in dataset.records]
    assert list(seen.values()) == [expected, expected]  # one arm, then the other, same records
    assert [item["arm"] for item in found["arms"] if item["status"] == hidden.GRADED] == [
        "deterministic",
        "llm",
    ]
    assert found["ranking"]["status"] == "on" and found["ranking"]["graded"] == 2
    assert found["scope"]["pack"]["id"] == scoreboard_pack
    counts = {
        item["arm"]: [c["distinct"]["positives"] for c in item["figures"]["critical"]]
        for item in found["arms"]
        if item["status"] == hidden.GRADED
    }
    assert counts == {"deterministic": [20], "llm": [20]}
    with pytest.raises(MendariError, match="one configuration per arm"):
        hidden.scoreboard_graded(
            [declared(pack, LOCAL), declared(pack, LOCAL)],
            dataset,
            GradingRead(dataset.name, "c" * 64, len(dataset.records), 1, 0),
            declaration=DECLARED,
            measured_on=DAY,
        )


# -- refused before any case is read ---------------------------------------------------------------


def test_a_declared_remote_arm_is_refused_before_any_case_is_read(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the deployment running a slot remotely: a declared arm that calls a remote provider
    (Jev, an LLM, a composite of them, or the deployment's own choice for a slot it leaves out),
    alone or beside a local arm, is a `422` `remote_provider`; a local configuration declared
    under another arm is `wrong_arm`; a slot or variant the pack lacks `unknown_variant`, another
    pack `wrong_pack`. Each sentence names `arms`, the field the run sent, never only
    `variants`, which a scoreboard run may not send. `arms` without `declared`, `declared`
    without `arms`, both beside `variants`, an arm that is not one and a declaration that is not
    a SHA-256 are `422`s of the body. Each opens the set's header at most: no case is read, no
    run counted, nothing chained. Naming every slot local in the deterministic arm is
    answered."""
    monkeypatch.setattr(clock, "today", lambda: DAY)
    keys = deployment(monkeypatch, "memberships")
    db = str(tmp_path / "audit.db")
    with started(db, inbox_pack, keys, providers=calm_jev(monkeypatch)) as world:
        set_id = world.frozen(2)
        grader = world.grader()
        opened: list[str] = []
        real = EvalsetKeys.open

        def spied(keys: EvalsetKeys, kind: Any, *bound: Any) -> tuple[Any, str]:
            opened.append(kind)
            return real(keys, kind, *bound)

        monkeypatch.setattr(EvalsetKeys, "open", spied)
        before = dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events"))
        jev = {"safety_screen": "rules", "router": "jev"}
        for arms_sent, reason in (
            ({"classifier": jev}, "remote_provider"),
            ({"deterministic": LOCAL, "classifier": jev}, "remote_provider"),
            ({"deterministic": {"router": "naive_bayes"}}, "remote_provider"),  # rules_and_jev
            ({"llm": {"safety_screen": "rules", "router": "cascade"}}, "remote_provider"),
            ({"llm": LOCAL}, "wrong_arm"),
            ({"classifier_and_llm": {"safety_screen": "rules", "router": "rules"}}, "wrong_arm"),
            ({"deterministic": {**LOCAL, "screen": "rules"}}, "unknown_variant"),
        ):
            opened.clear()
            refused = world.run(set_id, grader, arms=arms_sent, declared=DECLARED)
            assert (refused.status_code, refused.json()["reason"]) == (422, reason), arms_sent
            assert refused.json()["detail"] == GRADING_REFUSALS[reason][1]
            assert "`arms`" in refused.json()["detail"], reason  # the field this run sent
            assert opened == ["header"], arms_sent
        refused = world.run(set_id, grader, pack="mendari/ai-guard", **SCOREBOARD)
        assert (refused.status_code, refused.json()["reason"]) == (422, "wrong_pack")
        for body in (
            {"arms": {"deterministic": LOCAL}},
            {"declared": DECLARED},
            {**SCOREBOARD, "variants": LOCAL},
            {"arms": {"deterministic_plus": LOCAL}, "declared": DECLARED},
            {"arms": {}, "declared": DECLARED},
            {"arms": {"deterministic": LOCAL}, "declared": "D" * 64},
        ):
            opened.clear()
            refused = world.run(set_id, grader, **body)
            assert refused.status_code == 422 and "reason" not in refused.json(), body
            assert opened == [], body
        assert dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events")) == before
        assert graded_events(world, set_id) == [] and world.state(set_id) == "frozen"
        shown = answered(world, set_id, grader)
        assert graded_arm(shown)["configuration"]["providers"] == LOCAL


def test_a_declared_arm_names_every_slot(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a deployment whose own choice for the slot left out is local, a declared arm that
    names only some of the pack's slots would run that choice, so one cited declaration would
    grade other configurations on other deployments: it is `wrong_arm`, before any case is read,
    and uses no run. The same arm with every slot named is answered, and runs what it sent."""
    set_id = scored_set(world, urgent=2, routine=2)
    grader = world.grader()
    opened: list[str] = []
    real = EvalsetKeys.open

    def spied(keys: EvalsetKeys, kind: Any, *bound: Any) -> tuple[Any, str]:
        opened.append(kind)
        return real(keys, kind, *bound)

    monkeypatch.setattr(EvalsetKeys, "open", spied)
    partial = {"deterministic": {"router": LOCAL["router"]}}
    refused = world.run(set_id, grader, arms=partial, declared=DECLARED)
    assert (refused.status_code, refused.json()["reason"]) == (422, "wrong_arm"), refused.text
    assert refused.json()["detail"] == GRADING_REFUSALS["wrong_arm"][1]
    assert opened == ["header"] and graded_events(world, set_id) == []
    shown = answered(world, set_id, grader)
    assert graded_arm(shown)["configuration"]["providers"] == LOCAL


def test_a_failed_scoreboard_run_counts_and_is_told_by_its_code_only(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A scoreboard run whose answer would hold a field the closed schema does not (here a
    critical label's missed cases, with a case's text in them) fails after its read: `500`
    `run_failed`, its fixed sentence only, naming nothing of the set; the read stays chained,
    so it counts as one of the set's runs (one `evalset.graded`); the operator's log names the
    set and the error's type only."""
    caplog.set_level(logging.WARNING, logger="mendari.evalsets")
    set_id = scored_set(world, urgent=2, routine=2)
    real = hidden._figures

    def leaking(entry: Mapping[str, Any], ledger: Any) -> dict[str, Any]:
        found = real(entry, ledger)
        (critical, *_) = found["critical"]
        missed = [f"{MARKS['input']} case 0: {URGENT_TEXT}"]
        return {**found, "critical": [{**critical, "missed_cases": missed}]}

    monkeypatch.setattr(hidden, "_figures", leaking)
    refused = world.run(set_id, world.grader(), **SCOREBOARD)
    assert (refused.status_code, refused.json()["reason"]) == (500, "run_failed")
    assert refused.json()["detail"] == GRADING_REFUSALS["run_failed"][1]
    assert [mark for mark in private_of(world, set_id) if mark in refused.text] == []
    assert URGENT_TEXT not in refused.text
    assert [e["payload"]["run"] for e in graded_events(world, set_id)] == [1]
    (warned,) = [r for r in caplog.records if r.name == "mendari.evalsets"]
    assert warned.getMessage() == (
        f"a grading run of evaluation set {set_id} failed (MendariError): it counts as a run"
    )


def test_undeclared_arms_say_not_graded_here_or_not_applicable(world: World) -> None:
    """The answer lists every arm, in the scoreboard's order: the declared one graded, with its
    configuration, providers, figures and grade; each other arm the pack builds "not graded
    here" (its configurations call a remote provider), with none of them; and, for a pack that
    cannot build an arm, "not applicable" (AI Guard has no "+ both"). Never "not measured"."""
    shown = answered(world, scored_set(world, urgent=2, routine=2), world.grader())
    statuses = [(item["arm"], item["name"], item["status"]) for item in shown["arms"]]
    assert statuses == [
        ("deterministic", "Mendari only", "graded"),
        ("classifier", "+ specialised classifier", "not graded here"),
        ("llm", "+ general LLM", "not graded here"),
        ("classifier_and_llm", "+ both", "not graded here"),
    ]
    for item in shown["arms"][1:]:
        assert [item[key] for key in ("configuration", "providers", "figures", "grade")] == [
            None
        ] * 4
    guard = resolve_pack("mendari/ai-guard")
    assert hidden.arm_statuses(guard, ["deterministic"]) == {
        "deterministic": "graded",
        "classifier": "not graded here",
        "llm": "not graded here",
        "classifier_and_llm": "not applicable",
    }
    with pytest.raises(ValidationError, match="graded arm has"):
        ScoredArm.model_validate({**shown["arms"][1], "status": "graded"})
    with pytest.raises(ValidationError, match="graded arm has"):
        ScoredArm.model_validate({**shown["arms"][0], "status": "not graded here"})


# -- what the answer holds -------------------------------------------------------------------------


def test_the_scoreboard_answer_holds_no_per_case_field(world: World) -> None:
    """Every answer model forbids a field it does not list and lists exactly those pinned. The
    answer holds no per-case field (no cost, latency, subgroup, equal-mass bin, missed case or
    ledger), no planted mark, no case id or keyed hash, no name or hash of the set's cases and
    no ledger's id: its only `sha256:` values are the pack's digest and its own body's, and the
    set is named by its id and commitment. Every proportion is k of n with its 95% interval,
    and ECE a single number. A field added to it is refused, and a run that would answer one
    is `run_failed`, naming nothing
    (`test_a_failed_scoreboard_run_counts_and_is_told_by_its_code_only`). Every share is
    rounded to 4 places and every mean, score and effective n to 3, as
    `mendari.hidden_result/1`'s figures are (its Brier is the plain run's); the method's levels
    are kept as it fixes them, and a stored answer with a 6-place figure is not an answer's."""
    for model, fields in PINNED.items():
        assert model.model_config.get("extra") == "forbid", model.__name__
        assert set(model.model_fields) == fields, model.__name__
    set_id = scored_set(world, urgent=12, routine=12)
    found = world.run(set_id, world.grader(), **SCOREBOARD)
    assert found.status_code == 200, found.text
    shown = found.json()
    HiddenScoreboard.model_validate(shown)
    assert not keys_of(shown) & PER_CASE
    assert [mark for mark in private_of(world, set_id) if mark in found.text] == []
    assert "hmac-sha256:" not in found.text
    (frozen,) = [e for e in graded_events(world, set_id)]
    assert set(re.findall(r"sha256:[0-9a-f]{64}", found.text)) == {
        world.pack.digest,
        shown["body_sha256"],
    }
    assert shown["scope"]["set"] == {
        "kind": "hidden",
        "id": set_id,
        "commitment": frozen["payload"]["commitment"],
        "n": 24,
        "distinct_inputs": 24,
        "norm": "norm/v1",
    }
    figures = graded_arm(shown)["figures"]
    for name in ("false_automation", "auto_error_rate", "automation", "accuracy"):
        assert set(figures[name]) == {"k", "n", "low", "high"}, name
    (critical,) = figures["critical"]
    assert critical["detected"]["k"] == critical["detected"]["n"] == 12
    assert (
        set(figures["brier"]) == {"estimate", "low", "high"} and figures["brier"]["low"] is not None
    )
    assert figures["ece"] == {"status": "not_estimable", "reason": figures["ece"]["reason"]}
    assert shown["scope"]["evaluation"] == scoreboard.NOT_CLINICAL
    assert shown["scope"]["disclosure"] == {
        "status": scoreboard.NO_MODEL_PROVIDER,
        "statements": {},
    }
    assert shown["scope"]["declared"] == DECLARED
    with pytest.raises(ValidationError, match="Extra inputs"):
        HiddenScoreboard.model_validate({**shown, "cases": []})
    arm = graded_arm(shown)
    with pytest.raises(ValidationError, match="Extra inputs"):
        ScoredFigures.model_validate({**arm["figures"], "latency_ms": {}})
    with pytest.raises(ValidationError, match="Extra inputs"):
        ScoredCritical.model_validate({**critical, "missed_cases": ["a case"]})
    # A Brier interval over fewer than ten scored cases would be single cases' scores.
    small = answered(world, scored_set(world, urgent=2, routine=2), world.grader())
    assert graded_arm(small)["figures"]["brier"]["low"] is None
    graded_arm(small)["figures"]["brier"]["estimate"]  # the mean is still shown

    # Rounded as `mendari.hidden_result/1` rounds: shares to 4 places, means to 3. The same
    # set's plain run answers the same Brier score.
    def at_most(value: float | None, digits: int) -> bool:
        return value is None or round(value, digits) == value

    proportions = [figures[name] for name in ("false_automation", "auto_error_rate", "accuracy")]
    proportions += [figures["automation"], critical["detected"], critical["automated"]]
    proportions.append(critical["bound"])
    assert all(at_most(found[bound], 4) for found in proportions for bound in ("low", "high"))
    for mean in (figures["brier"], figures["macro_f1"]):
        assert all(at_most(mean[part], 3) for part in ("estimate", "low", "high"))
    plain = world.run(set_id, world.grader(), variants=LOCAL)
    assert plain.status_code == 200, plain.text
    question = plain.json()["questions"][plain.json()["primary"]]
    assert question["brier"] == figures["brier"]["estimate"]
    level = method.simultaneous_level(3)  # 0.98333...: the method's, kept as it is
    bound = {"k": 0, "n": 0, "low": None, "high": None, "level": level, "labels": 3}
    assert Bound.model_validate(bound).level == level
    # A stored answer with a 6-place figure, its body hash made again, is not an answer's.
    six = json.loads(json.dumps(shown))
    six["arms"][0]["figures"]["brier"]["estimate"] = figures["brier"]["estimate"] + 0.000123
    six["body_sha256"] = hidden.scoreboard_body_sha256(six)
    assert hidden.scoreboard_problem(six) == (
        f"it is not {HIDDEN_SCOREBOARD_FORMAT} as an answer holds it"
    )


def test_a_set_with_twenty_urgent_positives_says_insufficient_evidence(world: World) -> None:
    """Twenty urgent positives, every one detected: raw 20 of 20 with its interval and 20
    distinct positives, short of the 100 the method needs, so its evidence is insufficient, the
    banner names it, and every attestation is insufficient evidence (no critical case was
    automated, but 20 positives could not show it; 30 labelled cases are short of the 183 the
    false-automation ceiling needs). Nothing is ranked: one arm is graded."""
    shown = answered(world, scored_set(world, urgent=20, routine=10), world.grader())
    (critical,) = graded_arm(shown)["figures"]["critical"]
    assert (critical["question"], critical["label"]) == ("urgency", "urgent")
    assert critical["detected"] == {
        "k": 20,
        "n": 20,
        "low": round(statistics.clopper_pearson(20, 20, 0.95)[0], 4),
        "high": 1.0,
    }
    assert critical["distinct"] == {"positives": 20, "detected": 20}
    assert critical["evidence"] == {"status": "insufficient evidence", "needed": 100}
    assert shown["banner"] == {
        "short": [
            {
                "arm": "deterministic",
                "configuration": "router=naive_bayes,safety_screen=rules",
                "question": "urgency",
                "label": "urgent",
                "positives": 20,
                "needed": 100,
            }
        ]
    }
    attestations = graded_arm(shown)["grade"]["attestations"]
    assert [(a["attestation"], a["state"]) for a in attestations] == [
        ("no_critical_automated", "insufficient evidence"),
        ("recall_floor", "insufficient evidence"),
        ("false_automation_ceiling", "insufficient evidence"),
    ]
    assert attestations[2]["needed"] == 183 and attestations[2]["false_automation"]["n"] == 30
    assert shown["ranking"] == {"status": "off", "reason": "one arm graded"}


def weighted_share(hits: Sequence[bool], weights: Sequence[float]) -> dict[str, Any]:
    """A Korn-Graubard share of `hits` with `weights`, rounded as an answer holds it."""
    found = statistics.weighted_proportion(hits, weights).to_dict()
    return WeightedShare.model_validate(found).model_dump(mode="json")


def test_weights_reach_prevalence_dependent_figures_and_never_recall(world: World) -> None:
    """Ten urgent cases drawn of ten (weight 1) and ten routine ones of thirty (weight 3), the
    routine ones sent to the front desk (the router says nurse, so they are wrong): the
    weighted accuracy, false automation, automation and AUTO error rate are the Korn-Graubard
    figures of the decisions with those weights, beside the unweighted k of n (accuracy 10 of
    20, weighted 0.25, effective n 16); detection is 10 of 10 and has no weighted figure."""
    set_id = scored_set(
        world, urgent=10, routine=30, per_stratum=10, routine_destination="front_desk"
    )
    shown = answered(world, set_id, world.grader())
    figures = graded_arm(shown)["figures"]
    assert figures["accuracy"]["k"] == 10 and figures["accuracy"]["n"] == 20
    weights = [1.0] * 10 + [3.0] * 10
    expected = weighted_share([True] * 10 + [False] * 10, weights)
    assert figures["weighted"]["status"] == "weighted"
    assert figures["weighted"]["accuracy"] == expected
    assert expected["estimate"] == 0.25 and expected["effective_n"] == 16.0
    nothing = weighted_share([False] * 20, weights)
    assert figures["weighted"]["automation"] == nothing  # no case is decided AUTO
    assert figures["weighted"]["false_automation"] == nothing
    assert figures["weighted"]["auto_error_rate"]["n"] == 0
    (critical,) = figures["critical"]
    assert critical["detected"]["k"] == critical["detected"]["n"] == 10
    assert "weighted" not in critical and not keys_of(critical) & {"estimate", "effective_n"}


def test_weights_carried_by_few_cases_are_withheld(world: World) -> None:
    """Four urgent cases drawn of four (weight 1) and four routine ones of twelve (weight 3),
    the routine ones wrong: beside accuracy 4 of 8, a weighted 0.25 would be solved by one split
    only, all four cases of weight 1 right and all four of weight 3 wrong: a stratum's figures
    over four cases (a weight is a stratum's, N_h / n_h). Fewer than ten cases of a share carry
    each weight, so the weighted figures are withheld, with the reason; the unweighted k of n
    stay."""
    set_id = scored_set(
        world, urgent=4, routine=12, per_stratum=4, routine_destination="front_desk"
    )
    figures = graded_arm(answered(world, set_id, world.grader()))["figures"]
    assert figures["accuracy"]["k"] == 4 and figures["accuracy"]["n"] == 8
    assert figures["weighted"] == {"status": "withheld", "reason": scoreboard.WEIGHTS_WITHHELD}
    assert method.SUBGROUP_MIN == 10  # the reason's number, as the closed model holds it
    splits = [
        (right_of_one, 4 - right_of_one)
        for right_of_one in range(5)
        if (right_of_one + 3 * (4 - right_of_one)) / 16 == 0.25
    ]
    assert splits == [(4, 0)]  # what the weighted estimate would have given away


def test_two_reads_of_a_set_give_the_same_body(world: World) -> None:
    """Two runs over one frozen set, on one day: the same body, so the same `body_sha256`; only
    the run block differs (run 1 then 2, with the runs left)."""
    set_id = scored_set(world, urgent=12, routine=12)
    grader = world.grader()
    first, second = (answered(world, set_id, grader) for _ in range(2))
    assert (first["run"], second["run"]) == (
        {"run": 1, "runs_left": 2},
        {"run": 2, "runs_left": 1},
    )
    assert first["body_sha256"] == second["body_sha256"]
    assert hidden.scoreboard_body(first) == hidden.scoreboard_body(second)
    assert first["body_sha256"] == hidden.scoreboard_body_sha256(second)
    assert [e["payload"]["run"] for e in graded_events(world, set_id)] == [1, 2]


def test_no_grade_is_final_before_the_method_is_in_effect(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the grading method has no effective date, the scope says it is not in effect and
    every attestation and the grade's method are draft; with an effective date on or before the
    day measured (a test's own), none is. Both states are the test's own, so it holds once the
    owner sets the repository's date (the runbook's step 4)."""
    set_id = scored_set(world, urgent=2, routine=2)
    grader = world.grader()
    monkeypatch.setattr(method, "EFFECTIVE", None)
    shown = answered(world, set_id, grader)
    assert shown["scope"]["method"]["in_effect"] is False
    attested = graded_arm(shown)["grade"]
    assert attested["method"]["in_effect"] is False
    assert all(item["draft"] for item in attested["attestations"])
    monkeypatch.setattr(method, "EFFECTIVE", DAY - datetime.timedelta(days=1))
    shown = answered(world, set_id, grader)
    assert shown["scope"]["method"]["in_effect"] is True
    attested = graded_arm(shown)["grade"]
    assert attested["method"]["in_effect"] is True
    assert not any(item["draft"] for item in attested["attestations"])


def test_without_arms_the_answer_is_unchanged(world: World) -> None:
    """A run without `arms` (and with `variants`) is answered `mendari.hidden_result/1`, field
    for field as LP-11l answers it, with nothing of the scoreboard's."""
    set_id = world.frozen(4)
    found = world.run(set_id, world.grader(), variants={"router": "naive_bayes"})
    assert found.status_code == 200, found.text
    shown = found.json()
    assert shown["format"] == HIDDEN_RESULT_FORMAT
    assert set(shown) == set(HiddenResult.model_fields)
    HiddenResult.model_validate(shown)
    assert not {"arms", "ranking", "scope", "body_sha256", "banner"} & set(shown)


def test_a_ranking_of_graded_arms_fits_the_answer_without_cost(tmp_path: Path) -> None:
    """With more than one local arm (each a test's own stand-in, forged as `bands.ranking`'s own
    tests forge them), on the Referral Router's 224 cases with 113 and 124 distinct positives:
    the ranking is on, each ranked arm with its rank, interval and bands, the band tuple without
    cost (0 for every local arm) and without its key; an arm that missed an urgent referral is
    unranked with its reason; the Holm family is named with its size; and it all fits the
    answer, holding no per-case field."""
    records = records_of("mendari/referral-router", "test")

    def positive(record: Mapping[str, Any]) -> bool:
        labels = record["labels"]
        return labels.get("urgency") == "urgent" or labels.get("next_step") == "urgent_review"

    others = {r["id"] for r in [r for r in records if not positive(r)][:100]}
    kept = [r for r in records if positive(r) or r["id"] in others]
    base: Built = build("mendari/referral-router", written(tmp_path, "hidden", kept), DETERMINISTIC)
    set_id = str(uuid.uuid4())
    graded = []
    for providers, edit, arm_id in (
        (DETERMINISTIC, None, "deterministic"),
        (BOTH, None, "classifier_and_llm"),
        (CLASSIFIER, worse_accuracy, "classifier"),
        (LLM, missed_urgent, "llm"),
    ):
        ledger = base.ledger if providers == DETERMINISTIC else forged(base, providers, edit)[1]
        entry = scoreboard.hidden_entry(
            ledger,
            base.dataset,
            pack=scoreboard.PackScope.of(base.pack),
            arm=arm_id,
            configuration=arms.Configuration(dict(providers), arm_id, True, ()),
            set_id=set_id,
            commitment="c" * 64,
            measured_on=DAY,
        )
        graded.append((entry, ledger))
    ranking = bands.ranking(
        [entry for entry, _ in graded],
        ledgers=[ledger for _, ledger in graded],
        records=base.dataset.records,
    )
    read = GradingRead(set_id, "c" * 64, len(kept), 1, 0)
    statuses = {arm_id: hidden.GRADED for arm_id in arms.ARMS}
    found = hidden.hidden_scoreboard(
        graded, ranking, read, statuses=statuses, declaration=DECLARED
    ).model_dump(mode="json")
    shown = found["ranking"]
    assert shown["status"] == "on" and shown["graded"] == 4 and shown["draft"] is True
    assert [(row["arm"], row["rank"]) for row in shown["ranked"]] == [
        (row["arm"], row["rank"]) for row in ranking["ranked"]
    ]
    for row in shown["ranked"]:
        assert list(row["bands"]) == ["detection", "false_automation", "accuracy", "brier"]
        assert "key" not in row and bands.COST not in row["bands"]
    (unranked,) = shown["unranked"]
    assert unranked["arm"] == "llm"
    assert {reason["attestation"] for reason in unranked["reasons"]} == {"recall_floor"}
    assert shown["family"]["of"] == bands.FAMILY
    assert shown["family"]["size"] == len(shown["family"]["tests"]) == 3 * (2 + 3)
    assert not keys_of(found) & PER_CASE
    assert all(item["status"] == hidden.GRADED for item in found["arms"])
    grades = {entry["arm"]: grade.grade(entry) for entry, _ in graded}

    def states(attested: Mapping[str, Any] | None) -> list[tuple[str, str, bool]]:
        assert attested is not None
        return [(a["attestation"], a["state"], a["draft"]) for a in attested["attestations"]]

    assert {arm_id: states(attested) for arm_id, attested in grades.items()} == {
        item["arm"]: states(item["grade"]) for item in found["arms"]
    }
    # The store's check: its grades, banner and ranking agree with its own figures, and a
    # ranking edited to say otherwise does not.
    assert hidden.scoreboard_disagreement(found) is None
    for part, value, match in (
        ("draft", False, "ranking's method or draft"),
        ("unranked", [], "ranking's arms"),
        ("graded", 3, "ranking's arms"),
    ):
        edited = json.loads(json.dumps(found))
        edited["ranking"][part] = value
        assert match in (hidden.scoreboard_disagreement(edited) or ""), part


# -- the results store, signing, the page ----------------------------------------------------------


def in_memory(
    dataset: Dataset, *, measured_on: datetime.date = DAY, run: int = 1
) -> dict[str, Any]:
    """The answer a scoreboard run of the inbox's deterministic arm over `dataset` gives, as a
    grading read numbered `run` would have it."""
    pack = resolve_pack("mendari/patient-inbox-router")
    read = GradingRead(dataset.name, "c" * 64, len(dataset.records), run, 3 - run)
    return hidden.scoreboard_graded(
        [declared(pack, LOCAL)], dataset, read, declaration=DECLARED, measured_on=measured_on
    ).model_dump(mode="json")


def a_store(
    tmp_path: Path, declaration: Mapping[str, Any] | None = DECLARATION
) -> results_store.Store:
    """A results store with no result, the repository's keys file and the inbox's
    `declaration` (none: no declaration), whose SHA-256 is `DECLARED`."""
    root = tmp_path / "scoreboard"
    root.mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "fixtures" / "scoreboard" / "keys.json", root / "keys.json")
    store = results_store.Store.at(root)
    if declaration is not None:
        path = store.declaration_path(DECLARATION["pack"])
        path.parent.mkdir()
        path.write_text(json.dumps(declaration))
    return store


def test_the_store_holds_a_hidden_answer_as_it_was_answered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari scoreboard add` stores a hidden set's answer as it was answered, under its
    body's SHA-256 (its run block left out), and the store's check keeps it as it is, never
    recomputed: there is no ledger of it. A second read of the same set is the same result,
    stored already; a later day's answer supersedes it with `--supersedes`. Refused: a changed
    figure (its body is not its hash), a field the closed schema does not hold, an answer under
    another method or measured after today, a pack Mendari does not maintain, and an entry of
    `mendari.scoreboard/1` that calls its set hidden (still checked as a public result). The
    page of public sets leaves it out."""
    store = a_store(tmp_path)
    pack = resolve_pack("mendari/patient-inbox-router")
    dataset = hidden_dataset(resolve_dataset(pack, "red_flags_holdout").records)
    first = in_memory(dataset)
    name = store.add(first, packs=lambda _: pack, today=DAY)
    assert f"sha256:{name}" == first["body_sha256"] == hidden.scoreboard_body_sha256(first)
    stored = store.results()[name]
    assert (stored.is_hidden, stored.set_name, stored.shown) == (
        True,
        dataset.name,
        f"{pack.id} {dataset.name}",
    )
    assert signing.read((store.root / "results" / f"{name}.json").read_text()) == first
    found = results_store.check(store, base={}, packs=lambda _: pack, today=DAY)
    assert found.problems == [] and found.kept == [f"{name}: {results_store.HIDDEN_KEPT}"]
    again = in_memory(dataset, run=2)
    assert again["run"] != first["run"] and again["body_sha256"] == first["body_sha256"]
    with pytest.raises(MendariError, match="stored already"):
        store.add(again, packs=lambda _: pack, today=DAY)

    later_day = DAY + datetime.timedelta(days=1)
    later = in_memory(dataset, measured_on=later_day, run=3)
    path = tmp_path / "later.json"
    path.write_text(signing.dump(later))
    capsys.readouterr()
    argv = ["scoreboard", "add", str(path), "--supersedes", name, "--store", str(store.root)]
    assert main(argv) == 0
    (newer,) = set(store.results()) - {name}
    assert capsys.readouterr().out.strip() == f"stored: results/{newer}.json"
    assert results_store.superseded(store.results()) == {name: newer}
    assert list(results_store.heads(store.results())) == [newer]
    base = {name: (store.root / "results" / f"{name}.json").read_bytes()}
    found = results_store.check(store, base=base, packs=lambda _: pack, today=later_day)
    assert found.problems == [] and len(found.kept) == 2

    def refused(result: dict[str, Any], match: str, **options: Any) -> None:
        with pytest.raises(MendariError, match=match):
            store.add(result, packs=options.get("packs", lambda _: pack), today=DAY)

    changed = json.loads(json.dumps(first))
    changed["arms"][0]["figures"]["accuracy"]["k"] -= 1
    refused(changed, "its body_sha256 is not its body's")
    refused({**first, "cases": []}, "does not fit mendari.hidden_scoreboard/1")
    other = hidden_dataset(resolve_dataset(pack, "red_flags_holdout").records)  # another set
    refused(in_memory(other, measured_on=DAY + datetime.timedelta(days=5)), "after today")

    def not_maintained(ref: str) -> Pack:
        raise MendariError(f"{ref} is not a pack Mendari maintains")

    refused(in_memory(other), "not a pack Mendari maintains", packs=not_maintained)
    drafted = in_memory(other)
    drafted["scope"]["method"]["sha256"] = "0" * 64
    drafted["body_sha256"] = hidden.scoreboard_body_sha256(drafted)
    refused(drafted, "under the current method")
    # A public set's entry of the repository's store, which holds hidden sets' answers too once
    # the owner grades them (the runbook's step 9).
    entry = next(
        found
        for found in (
            json.loads(path.read_text())
            for path in sorted(
                (ROOT / "tests" / "fixtures" / "scoreboard" / "results").glob("*.json")
            )
        )
        if found["format"] == scoreboard.FORMAT
    )
    entry["scope"]["set"]["kind"] = "hidden"
    entry["body_sha256"] = scoreboard.body_sha256(entry)
    refused(entry, "not a public set's result")

    page = _page_script().page(store)
    empty = _page_script().page(a_store(tmp_path / "empty"))
    assert page == empty  # the page shows public sets only


def test_a_stored_answer_must_agree_with_its_own_figures(tmp_path: Path) -> None:
    """The store checks a hidden set's answer against its own figures, never against a ledger
    (none is kept): each edit below is made to fit, its body hash made again, and each still
    fits the closed schema, yet it is refused. Every attestation met and final, with the method
    in effect, on 20 distinct positives where 100 are needed, before the method is in effect
    (the review's probe); the method said to be in effect on its day, every draft dropped to
    fit; an attestation met, or its draft dropped, with the method not in effect; a grade's
    method in effect alone; an interval, the evidence or the banner that its k and n, or its
    distinct positives, do not give. `results_store.check` finds such an answer among the
    base's results too, under the current method. The answer as answered agrees."""
    store = a_store(tmp_path)
    pack = resolve_pack("mendari/patient-inbox-router")
    first = in_memory(hidden_dataset(resolve_dataset(pack, "red_flags_holdout").records))
    assert hidden.scoreboard_disagreement(first) is None
    (critical,) = graded_arm(first)["figures"]["critical"]
    assert critical["distinct"]["positives"] == 20 and not first["scope"]["method"]["in_effect"]

    def in_effect(found: dict[str, Any]) -> None:
        found["scope"]["method"]["in_effect"] = True
        found["arms"][0]["grade"]["method"]["in_effect"] = True
        for item in found["arms"][0]["grade"]["attestations"]:
            item["draft"] = False

    def probe(found: dict[str, Any]) -> None:
        in_effect(found)
        for item in found["arms"][0]["grade"]["attestations"]:
            item["state"] = "met"

    def met(found: dict[str, Any]) -> None:
        for item in found["arms"][0]["grade"]["attestations"]:
            item["state"] = "met"

    def final(found: dict[str, Any]) -> None:
        found["arms"][0]["grade"]["attestations"][1]["draft"] = False

    def grade_in_effect(found: dict[str, Any]) -> None:
        found["arms"][0]["grade"]["method"]["in_effect"] = True

    def interval(found: dict[str, Any]) -> None:
        accuracy = found["arms"][0]["figures"]["accuracy"]
        accuracy["low"] = accuracy["high"]  # an interval of no width, on hundreds of cases

    def evidence(found: dict[str, Any]) -> None:
        found["arms"][0]["figures"]["critical"][0]["evidence"]["status"] = "sufficient"

    def banner(found: dict[str, Any]) -> None:
        found["banner"] = None

    def edited(edit: Any) -> dict[str, Any]:
        found = json.loads(json.dumps(first))
        edit(found)
        found["body_sha256"] = hidden.scoreboard_body_sha256(found)
        assert hidden.scoreboard_problem(found) is None, edit.__name__  # it fits the schema
        return found

    for edit, match in (
        (probe, "says the method is in effect on 2026-10-17"),
        (in_effect, "says the method is in effect on 2026-10-17"),
        (met, "deterministic arm's grade is not what its figures give"),
        (final, "deterministic arm's grade is not what its figures give"),
        (grade_in_effect, "deterministic arm's grade is not what its figures give"),
        (interval, "deterministic arm's intervals are not what their k and n give"),
        (evidence, "deterministic arm's evidence is not what its distinct positives give"),
        (banner, "its banner is not what its arms' evidence gives"),
    ):
        with pytest.raises(MendariError, match=match):
            store.add(edited(edit), packs=lambda _: pack, today=DAY)
    assert store.results() == {}

    # Written behind the store's back, as if it were in the base already.
    kept = edited(met)
    name = results_store.name_of(kept)
    (store.root / "results").mkdir(exist_ok=True)
    path = store.root / "results" / f"{name}.json"
    path.write_text(signing.dump(kept))
    found = results_store.check(
        store, base={name: path.read_bytes()}, packs=lambda _: pack, today=DAY
    )
    assert found.problems == [
        f"results/{name}.json (mendari/patient-inbox-router {first['scope']['set']['id']}): "
        "its deterministic arm's grade is not what its figures give"
    ]


def test_a_stored_answer_grades_its_packs_declared_local_configuration(tmp_path: Path) -> None:
    """A new hidden set's answer is stored only as its pack's declaration (`mendari.declared/1`,
    LP-17h) declares it: the store holds the declaration, the answer cites its SHA-256 as the
    store holds it, and it grades each arm the declaration names a `local` configuration for
    (the one a hidden set grades), with that configuration, all in its one read. Refused: no
    declaration; another declaration's SHA-256; a declaration that names another local
    configuration of the arm, or none for it (a remote one only); and an answer that leaves out
    an arm declared local, which the store's check also names in the declaration, since no other
    arm of the inbox is local. An answer stored before its declaration changed is kept as it
    was added; the same answer, new, is refused."""
    pack = resolve_pack("mendari/patient-inbox-router")
    answer = in_memory(hidden_dataset(resolve_dataset(pack, "red_flags_holdout").records))
    rules = {"safety_screen": "rules", "router": "rules"}  # the arm's other local configuration
    jev = {"safety_screen": "rules", "router": "jev"}

    def citing(declaration: Mapping[str, Any]) -> dict[str, Any]:
        found = json.loads(json.dumps(answer))
        found["scope"]["declared"] = hashlib.sha256(json.dumps(declaration).encode()).hexdigest()
        found["body_sha256"] = hidden.scoreboard_body_sha256(found)
        assert hidden.scoreboard_problem(found) is None  # it fits the schema
        return found

    shown = "declared/patient-inbox-router.json"
    for name, declaration, match in (
        ("none", None, f"it cites a declaration, and the store has none \\({shown}\\)"),
        ("other", {**DECLARATION, "date": "2026-10-16"}, f"not {shown} as it is"),
        (
            "rules",
            {**DECLARATION, "arms": {"deterministic": {"local": rules}}},
            f"its deterministic ran router=naive_bayes,safety_screen=rules, not the local "
            f"configuration {shown} declares for it",
        ),
        (
            "remote",
            {**DECLARATION, "arms": {"classifier": {"remote": jev}}},
            f"it grades deterministic, for which {shown} declares no local configuration",
        ),
        (
            "two",
            {
                **DECLARATION,
                "arms": {"deterministic": {"local": LOCAL}, "classifier": {"local": jev}},
            },
            f"it leaves out classifier, whose local configuration {shown} declares",
        ),
    ):
        store = a_store(tmp_path / name, declaration)
        sent = answer if name in ("none", "other") else citing(declaration or {})
        with pytest.raises(MendariError, match=match):
            store.add(sent, packs=lambda _: pack, today=DAY)
        assert store.results() == {}, name
    found = results_store.check(store, base={}, packs=lambda _: pack, today=DAY)
    assert found.problems == [
        f"{shown}: its local configuration of classifier, router=jev,safety_screen=rules, is remote"
    ]

    store = a_store(tmp_path / "declared")
    name = store.add(answer, packs=lambda _: pack, today=DAY)
    # The declaration rewritten after the answer was stored: kept as it was added, refused new.
    store.declaration_path(pack.id).write_text(json.dumps({**DECLARATION, "date": "2026-10-16"}))
    base = {name: (store.root / "results" / f"{name}.json").read_bytes()}
    found = results_store.check(store, base=base, packs=lambda _: pack, today=DAY)
    assert found.problems == [] and found.kept == [f"{name}: {results_store.HIDDEN_KEPT}"]
    found = results_store.check(store, base={}, packs=lambda _: pack, today=DAY)
    assert len(found.problems) == 1 and f"not {shown} as it is" in found.problems[0]


def test_a_signature_covers_the_answer_and_its_run(tmp_path: Path) -> None:
    """A hidden set's answer is signed and verified as any result is (LP-17e): the signature
    covers its body and its run block, so its run's number cannot change unnoticed, while the
    name the store gives it (its body's SHA-256) stays the same."""
    pack = resolve_pack("mendari/patient-inbox-router")
    answer = in_memory(hidden_dataset(resolve_dataset(pack, "red_flags_holdout").records))
    key = Ed25519PrivateKey.from_private_bytes(secrets.token_bytes(32))
    keys = signing.given_key(public_text(key.public_key()))
    signed = signing.sign(answer, key)
    assert signing.problem(signed, keys) is None
    assert hidden.scoreboard_problem(signed) is None
    assert results_store.name_of(signed) == results_store.name_of(answer)
    moved = {**signed, "run": {"run": 2, "runs_left": 1}}
    assert signing.problem(moved, keys) == (
        "its signature does not hold: it changed after it was signed"
    )


def _page_script() -> Any:
    from tests.test_scoreboard_page import page_script

    return page_script


# -- the route's deadline, the chain, the runbook --------------------------------------------------


def test_a_600_case_run_fits_the_routes_deadline(world: World) -> None:
    """Through the route, in one request: a frozen set of 600 hand-written cases (300 urgent,
    300 routine), its scoreboard run of the deterministic arm: the grading read
    (each case's input, attributes and labels opened, the content recomputed against the
    commitment, the read chained, in one exclusive transaction) and the run, every bootstrap at
    2,000 resamples (about 5 s where it was measured). Then, in memory, the same arm over 600 of
    the pack's own cases, whose varied texts and three sampling weights cost more to compute
    (about 5 s). The two together, more than one run does, take under a fifth of the
    function's deadline (vercel.json's `maxDuration`, 300 s), so the run stays in the route and
    no figure is dropped."""
    deadline = json.loads(VERCEL.read_text())["functions"]["app.py"]["maxDuration"]
    set_id = scored_set(world, urgent=300, routine=300)
    grader = world.grader()
    started_at = time.perf_counter()
    shown = answered(world, set_id, grader)
    through_the_route = time.perf_counter() - started_at
    assert shown["scope"]["set"]["n"] == 600
    figures = graded_arm(shown)["figures"]
    assert figures["weighted"]["status"] == "weighted"
    assert figures["brier_reliability"]["status"] == "ok" and figures["ece"]["status"] == "ok"
    (read,) = graded_events(world, set_id)
    assert read["payload"]["n"] == 600
    assert hidden.scoreboard_disagreement(shown) is None  # as the store will check it

    pack = resolve_pack("mendari/patient-inbox-router")
    records = [
        {**record, "attributes": {**record.get("attributes", {}), "weight": 1.0 + number % 3}}
        for number, record in enumerate(resolve_dataset(pack, "test").records[:600])
    ]
    assert len(records) == 600
    started_at = time.perf_counter()
    answer = in_memory(hidden_dataset(records))
    in_memory_only = time.perf_counter() - started_at
    assert through_the_route + in_memory_only < deadline / 5, (through_the_route, in_memory_only)
    figures = graded_arm(answer)["figures"]
    assert figures["weighted"]["status"] == "weighted"
    assert figures["brier_reliability"]["status"] == "ok" and figures["ece"]["status"] == "ok"
    assert figures["macro_f1"]["low"] is not None and figures["brier"]["low"] is not None


@pytest.fixture
def on_either(db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """The deployment on SQLite and on Postgres (`db_target`)."""
    monkeypatch.setattr(clock, "today", lambda: DAY)
    with started(db_target, inbox_pack, deployment(monkeypatch, "memberships")) as found:
        yield found


def test_audit_verify_passes_after_a_scoreboard_run(
    on_either: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """After a scoreboard run (one `evalset.graded`, its set active), the chain and the sealed
    rows verify, at the store and through `mendari audit verify --evalsets`: the run read the
    set and wrote nothing but its chained read."""
    world = on_either
    set_id = scored_set(world, urgent=3, routine=3)
    before = dump(world.store)
    shown = answered(world, set_id, world.grader())
    assert shown["scope"]["set"]["n"] == 6
    assert dump(world.store) == before  # nothing stored but the chain's reads and the set's state
    assert world.state(set_id) == "active" and len(graded_events(world, set_id)) == 1
    assert world.store.verify(evalsets=world.keys)["ok"]
    capsys.readouterr()
    assert main(["audit", "verify", "--evalsets", "--db", world.db]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] and verified["evalsets"]["frozen"] == 1


def test_the_runbooks_scoreboard_run_runs_as_written(world: World) -> None:
    """The runbook's scoreboard run (the `curl` body that declares its arms, with a
    declaration's SHA-256 in its place) on a frozen set: answered `mendari.hidden_scoreboard/1`,
    citing the declaration, its arm the one the body declares."""
    set_id = world.frozen(2)
    text = RUNBOOK.read_text().replace("\\\n", " ")
    posted = [
        line
        for line in re.findall(r"curl -sS -X POST \"\$MENDARI/api/evalsets/<set id>/runs\".*", text)
        if '"arms"' in line
    ]
    assert len(posted) == 1
    body = json.loads(shlex.split(posted[0].split(" > ")[0])[-1].replace("<sha256>", DECLARED))
    found = world.client.post(f"/api/evalsets/{set_id}/runs", json=body, headers=world.grader())
    assert found.status_code == 200, found.text
    shown = found.json()
    assert shown["format"] == HIDDEN_SCOREBOARD_FORMAT and shown["scope"]["declared"] == DECLARED
    assert graded_arm(shown)["configuration"]["providers"]["router"] == "naive_bayes"
