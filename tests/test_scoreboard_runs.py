# SPDX-License-Identifier: Apache-2.0
"""Remote arms on public sets (LP-17h; the grading method's sections 9, 10 and 12): a remote
configuration runs 3 to 5 replicates, by hand and never in CI, as its arm's configuration in a
declaration committed before the run; replicate 1 is the headline, with the case flip rate and
each figure's spread; a local configuration is one replicate with no flips; another model
reported, by a later run, a replicate or a canary, and a canary's drift mark its results stale;
the canary never runs in CI; and an entry whose provider type has no disclosure statement shows
its scope and none of its figures. Every provider here is a fake (`tests/conftest.py`'s
`calm_jev`, `tests/test_llm.py`'s `install`); every set is synthetic."""

from __future__ import annotations

import datetime
import itertools
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from mendari import Pack
from mendari.cli import main
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals import arms, method, results_store, runs, scoreboard, signing, stats
from mendari.evals.ledger import Ledger
from mendari.evals.render import scoreboard_markdown
from mendari.evals.results_store import Store
from mendari.providers.jev import JevProvider
from mendari.providers.llm import LlmProvider
from tests.conftest import CALM, ROOT, calm_jev
from tests.test_llm import completion, install
from tests.test_scoreboard import bare_percentages
from tests.test_scoreboard_signing import keys_file

INBOX = "mendari/patient-inbox-router"
SET = "red_flags_holdout"  # 30 cases
DATE = datetime.date(2026, 10, 18)
JEV = ["router=jev"]  # Jev answers the primary question, the destination
JEV_MODEL = "jev-9.9.9"
_JEV_INIT = JevProvider.__init__  # as the module is imported, before any test replaces it


@pytest.fixture(autouse=True)
def _outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests stand where the owner runs them: outside CI. A test of the CI refusal sets
    the settings itself."""
    for name in signing.CI_SETTINGS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def inbox() -> Pack:
    return resolve_pack(INBOX)


def jev(pack: Pack) -> arms.Configuration:
    return results_store.configuration_of(pack, JEV)


def fake_jev(
    monkeypatch: pytest.MonkeyPatch,
    *,
    urgent: Callable[[int], bool] = lambda call: False,
    model: Callable[[int], str] = lambda call: JEV_MODEL,
) -> list[int]:
    """`calm_jev`'s answers, except that call number `n` (from 1) answers "urgent" and the
    billing team, confidently, when `urgent(n)`, and reports the model `model(n)`. Returns the
    calls made, as they are made."""
    original = _JEV_INIT  # never a fake's: a test may set up another fake after one
    calm_jev(monkeypatch)  # its settings
    calls: list[int] = []
    counter = itertools.count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        number = next(counter)
        calls.append(number)
        answers = {}
        for key in json.loads(request.content)["questions"]:
            spread = dict(CALM[key])
            if urgent(number):
                spread = {
                    label: 0.95 if label in ("urgent", "billing") else 0.0 for label in spread
                }
                spread = {
                    label: value or 0.05 / (len(spread) - 1) for label, value in spread.items()
                }
            choice = max(spread, key=lambda label: spread[label])
            answers[key] = {
                "type": "choice",
                "choice": choice,
                "confidence": spread[choice],
                "probabilities": spread,
            }
        usage = {"input_tokens": 100, "output_tokens": 10}
        body = {"model": model(number), "answers": answers, "usage": usage}
        return httpx.Response(200, json=body)

    def init(self: JevProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(JevProvider, "__init__", init)
    return calls


def readings(ledger: Ledger) -> dict[str, tuple[Any, str]]:
    """Each case's primary prediction and outcome, by ledger hash: worked out here, apart from
    `runs`."""
    primary = ledger.header["primary"]
    return {
        case["c"]: ((case["q"].get(primary) or [None, None])[1], case["o"]) for case in ledger.cases
    }


def standalone(measured: results_store.Measured, pack: Pack, number: int) -> dict[str, Any]:
    """Replicate `number`'s own entry, made from its ledger and run record alone."""
    ledger, run = measured.inputs[number - 1]
    return scoreboard.entry(
        ledger,
        results_store.resolve_dataset(pack, SET),
        run,
        pack=scoreboard.PackScope.of(pack),
        arm=arms.CLASSIFIER,
        configuration=jev(pack),
        prices=None,
        disclosures=None,
    )


def git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            *("-c", "user.name=Mendari Test", "-c", "user.email=test@example.org"),
            *("-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"),
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """A results store inside a git checkout of its own, as the repository's is."""
    repository = tmp_path / "repository"
    root = repository / "scoreboard"
    root.mkdir(parents=True)
    (root / "keys.json").write_text(json.dumps(keys_file()), encoding="utf-8")
    git(repository, "init", "-q")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", "the store")
    return Store(root)


def declaration(pack_id: str, arm: str, providers: dict[str, str]) -> bytes:
    """A declaration of `providers` as `arm`'s remote configuration of the pack."""
    data = {
        "format": runs.DECLARED_FORMAT,
        "pack": pack_id,
        "date": DATE.isoformat(),
        "arms": {arm: {"remote": providers}},
    }
    return (json.dumps(data, indent=2) + "\n").encode()


def declare(store: Store, pack: Pack, arm: str, providers: dict[str, str]) -> Path:
    """Write the pack's declaration of `providers` as `arm`'s remote configuration."""
    path = store.declaration_path(pack.id)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(declaration(pack.id, arm, providers))
    return path


def commit(store: Store) -> None:
    git(store.root.parent, "add", "-A")
    git(store.root.parent, "commit", "-q", "-m", "a declaration")


def prices(tmp_path: Path) -> Path:
    path = tmp_path / "prices.json"
    table = {
        "format": runs.PRICES_FORMAT,
        "date": DATE.isoformat(),
        "source": "a test",
        "prices": {"jev": {JEV_MODEL: {"input_per_1000": 0.001, "output_per_1000": 0.002}}},
    }
    path.write_text(json.dumps(table), encoding="utf-8")
    return path


def test_replicates_report_their_spread_and_flip_rate(
    inbox: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replicate 2 answers two calls "urgent": the flip rate counts the cases whose primary
    prediction or outcome differs between any two replicates, matched by ledger hash, and each
    figure's spread is its minimum to maximum over the replicates' own entries."""
    fake_jev(monkeypatch, urgent=lambda call: call in (35, 48))
    measured = results_store.measure(inbox, SET, jev(inbox), date=DATE, replicates=3)
    held = measured.entry["figures"]["replicates"]
    ledgers = [ledger for ledger, _ in measured.inputs]
    read = [readings(ledger) for ledger in ledgers]
    flipped = sum(1 for case in read[0] if len({one[case] for one in read}) > 1)
    assert flipped >= 1  # the urgent answers changed what was decided
    assert held["count"] == 3 and held["headline"] == 1
    assert held["flip_rate"] == stats.proportion(flipped, 30, method.LEVEL).to_dict()
    # Each figure's spread is the minimum to maximum of the replicates' own figures.
    own = [standalone(measured, inbox, number)["figures"] for number in (1, 2, 3)]
    automation = [found["automation"]["k"] / found["automation"]["n"] for found in own]
    assert held["spread"]["Automation"] == {
        "min": round(min(automation), stats.DIGITS),
        "max": round(max(automation), stats.DIGITS),
        "replicates": 3,
    }
    latency = [found["latency_ms"]["p50"]["estimate"] for found in own]
    assert held["spread"]["Latency p50"]["min"] == round(min(latency), stats.DIGITS)
    assert held["spread"]["Cost per 1,000 decisions"] == {"status": runs.NOT_ESTIMABLE}
    assert held["models"] == {"jev": [JEV_MODEL]}
    # The inputs list every replicate's ledger and run record, replicate 1 first.
    assert measured.entry["inputs"]["replicates"] == [
        {"replicate": number, "ledger": ledger.id, "run": runs.run_sha256(run)}
        for number, (ledger, run) in enumerate(measured.inputs, start=1)
    ]
    # The cases are matched by ledger hash, whatever their order in a ledger.
    shuffled = Ledger(ledgers[1].header, list(reversed(ledgers[1].cases)))
    again = runs.replicates(
        [ledgers[0], shuffled],
        [measured.inputs[0][1], {**measured.inputs[1][1], "ledger": shuffled.id}],
        [{}, {}],
    )
    pair = sum(1 for case in read[0] if read[0][case] != read[1][case])
    assert again["flip_rate"]["k"] == pair
    # The board shows them, and names no case.
    text = scoreboard_markdown(
        scoreboard.board(
            [
                {
                    **measured.entry,
                    "scope": {
                        **measured.entry["scope"],
                        "disclosure": {"status": "stated", "statements": {"jev": "None."}},
                    },
                }
            ],
            {INBOX: scoreboard.Current.of(inbox)},
        )
    )
    assert "| Replicates | 3, replicate 1 the headline |" in text
    assert f"| Case flip rate | {flipped} of 30 [" in text
    assert "Latency p50 " in text and bare_percentages(text) == []


def test_a_local_configuration_has_one_replicate_and_no_flips(inbox: Pack) -> None:
    local = results_store.configuration_of(inbox, ["router=naive_bayes"])
    measured = results_store.measure(inbox, SET, local, date=DATE)
    # Its entry is as it was before replicates: one run record, replicate 1, no replicates field.
    assert "replicates" not in measured.entry["inputs"]
    assert "replicates" not in measured.entry["figures"]
    assert measured.run["replicate"] == 1 and measured.inputs == [(measured.ledger, measured.run)]
    one = runs.replicates([measured.ledger], [measured.run], [{"Accuracy": 0.5}])
    assert one["count"] == 1 and one["flip_rate"] == stats.proportion(0, 30).to_dict()
    assert one["spread"]["Accuracy"] == {"min": 0.5, "max": 0.5, "replicates": 1}
    # Asking a local configuration for replicates is refused: it is deterministic.
    for count in (0, 2, 3):
        with pytest.raises(MendariError, match="local, so deterministic: one replicate, no flips"):
            results_store.refuse_unmeasurable(inbox, SET, local, replicates=count)
    with pytest.raises(MendariError, match="local, so deterministic"):
        scoreboard.entry(
            measured.ledger,
            results_store.resolve_dataset(inbox, SET),
            measured.run,
            pack=scoreboard.PackScope.of(inbox),
            arm=local.arm,
            configuration=local,
            prices=None,
            disclosures=None,
            replicates=[(measured.ledger, {**measured.run, "replicate": 2})],
        )
    (table,) = scoreboard.board([measured.entry], {INBOX: scoreboard.Current.of(inbox)})["tables"]
    text = scoreboard_markdown({"method": {"version": "v", "sha256": "s"}, "tables": [table]})
    assert "| Replicates | 1 (local: deterministic) |" in text
    assert "| Case flip rate | none: one replicate |" in text


def test_a_remote_entry_needs_three_to_five_replicates(
    store: Store,
    inbox: Pack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = fake_jev(monkeypatch)
    for count in (0, 1, 2, 6):
        with pytest.raises(MendariError, match="it runs 3 to 5 replicates"):
            results_store.refuse_unmeasurable(inbox, SET, jev(inbox), replicates=count)
    for count in (3, 4, 5):
        results_store.refuse_unmeasurable(inbox, SET, jev(inbox), replicates=count)
    assert calls == []
    # From the command line, without --replicates (or with too few): refused before any call.
    declare(store, inbox, arms.CLASSIFIER, jev(inbox).providers)
    commit(store)
    argv = ["scoreboard", "run", INBOX, "--dataset", SET, "--config", *JEV]
    argv += ["--prices", str(prices(tmp_path)), "--store", str(store.root)]
    assert main(argv) == 1
    assert main([*argv, "--replicates", "2"]) == 1
    errors = capsys.readouterr().err.splitlines()
    assert errors[0].endswith("it runs 3 to 5 replicates (--replicates N), not none")
    assert errors[1].endswith("not 2")
    assert calls == [] and not (store.root / "results").exists()
    # A remote result of one replicate is never stored, by `add` or past it.
    single = results_store.measure(inbox, SET, jev(inbox), date=DATE, replicates=3)
    one = scoreboard.entry(
        single.ledger,
        results_store.resolve_dataset(inbox, SET),
        single.run,
        pack=scoreboard.PackScope.of(inbox),
        arm=arms.CLASSIFIER,
        configuration=jev(inbox),
        prices=None,
        disclosures=None,
        declared=store.declaration(INBOX).sha256,  # type: ignore[union-attr]
    )
    store.put_ledger(single.ledger)
    store.put_run(single.run)
    with pytest.raises(MendariError, match="holds 3 to 5 replicates, not one"):
        store.add(one)
    with pytest.raises(MendariError, match="1 to 5"):
        runs.replicates([single.ledger] * 6, [single.run] * 6, [{}] * 6)


def test_the_headline_is_the_declared_first_replicate(
    inbox: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replicate 1's figures are the entry's, whichever replicate looks better; a headline that
    is another replicate, or replicates out of order, are refused."""
    fake_jev(monkeypatch, urgent=lambda call: call > 30)  # every later replicate differs
    measured = results_store.measure(inbox, SET, jev(inbox), date=DATE, replicates=3)
    first = standalone(measured, inbox, 1)
    second = standalone(measured, inbox, 2)
    assert measured.inputs[0][0].id != measured.inputs[1][0].id  # the replicates disagree
    assert measured.entry["figures"]["replicates"]["flip_rate"]["k"] == 30
    assert first["figures"]["brier"] != second["figures"]["brier"]
    for key in ("critical", "accuracy", "automation", "brier", "macro_f1"):
        assert measured.entry["figures"][key] == first["figures"][key]
    assert measured.entry["inputs"]["run"]["replicate"] == 1
    assert measured.entry["inputs"]["ledger"] == measured.ledger.id == measured.inputs[0][0].id
    (one, run_one), (two, run_two), (three, run_three) = measured.inputs
    common: dict[str, Any] = {
        "pack": scoreboard.PackScope.of(inbox),
        "arm": arms.CLASSIFIER,
        "configuration": jev(inbox),
        "prices": None,
        "disclosures": None,
    }
    dataset = results_store.resolve_dataset(inbox, SET)
    with pytest.raises(MendariError, match="the headline is replicate 1, as declared"):
        scoreboard.entry(two, dataset, run_two, replicates=[(one, run_one)], **common)
    with pytest.raises(MendariError, match="replicate 2's run record is replicate 3"):
        scoreboard.entry(
            one, dataset, run_one, replicates=[(three, run_three), (two, run_two)], **common
        )
    # The store reads replicate 1 as the result's own ledger and run record.
    found = json.loads(json.dumps(measured.entry))
    found["inputs"]["replicates"][0]["ledger"] = two.id
    found["body_sha256"] = scoreboard.body_sha256(found)
    if two.id != one.id:
        with pytest.raises(MendariError, match="its replicate 1 is not its own ledger"):
            results_store.held(found)
    found["inputs"]["replicates"][0] = {"replicate": 2, "ledger": one.id, "run": "x"}
    found["body_sha256"] = scoreboard.body_sha256(found)
    with pytest.raises(MendariError, match="lacks a field the store reads"):
        results_store.held(found)


def test_a_configuration_not_declared_before_its_run_is_refused(
    store: Store,
    inbox: Pack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = fake_jev(monkeypatch)
    table = prices(tmp_path).read_bytes()

    def run() -> results_store.Outcome:
        return results_store.record_run(
            store, inbox, SET, jev(inbox), date=DATE, replicates=3, prices=table
        )

    with pytest.raises(MendariError, match="is not declared: a remote configuration is run only"):
        run()
    # Declared, but another configuration of the arm.
    other = results_store.configuration_of(inbox, ["safety_screen=rules_and_jev"])
    declare(store, inbox, arms.CLASSIFIER, other.providers)
    commit(store)
    with pytest.raises(MendariError, match="is not the declared remote configuration of classif"):
        run()
    # Declared, but not committed: the run could have chosen it.
    path = declare(store, inbox, arms.CLASSIFIER, jev(inbox).providers)
    with pytest.raises(MendariError, match="is not committed as it is: commit the declaration"):
        run()
    # A declaration that names a configuration the pack does not have is refused.
    data = json.loads(path.read_text())
    data["arms"][arms.CLASSIFIER]["remote"] = {"router": "rules", "safety_screen": "nope"}
    path.write_text(json.dumps(data), encoding="utf-8")
    commit(store)
    with pytest.raises(MendariError, match="is not one of the pack's"):
        run()
    assert calls == [] and not (store.root / "results").exists()
    # Committed before the run: stored, citing the declaration's SHA-256.
    declare(store, inbox, arms.CLASSIFIER, jev(inbox).providers)
    commit(store)
    outcome = run()
    assert outcome.action == results_store.STORED and len(calls) == 90
    result = store.results()[outcome.name]
    declared = store.declaration(INBOX)
    assert declared is not None and result.declared == declared.sha256
    assert result.result["inputs"]["prices"]["date"] == DATE.isoformat()
    assert (store.root / "prices" / f"{DATE.isoformat()}.json").read_bytes() == table
    # Every replicate's ledger and run record is stored; the check recomputes it and re-runs
    # none of them (a remote ledger would call off the machine).
    found = results_store.check(store, base={})
    assert found.problems == [] and found.rerun == 0 and found.recomputed == 1
    assert found.kept == [f"{outcome.name}: its ledger not re-run (a remote configuration)"]
    assert len(calls) == 90
    # A declaration changed after the run: a new result citing the old one is refused.
    data = json.loads(declare(store, inbox, arms.CLASSIFIER, jev(inbox).providers).read_text())
    data["date"] = "2026-10-19"
    store.declaration_path(INBOX).write_text(json.dumps(data), encoding="utf-8")
    assert any(
        "it cites the declaration" in line for line in results_store.check(store, base={}).problems
    )
    held = {name: path.read_bytes() for name, path in store.files("results").items()}
    base = {name.removesuffix(".json"): raw for name, raw in held.items()}
    assert results_store.check(store, base=base).problems == []  # stored before: it stays
    # From the command line, a run of a configuration no declaration names is refused.
    argv = ["scoreboard", "run", INBOX, "--dataset", SET, "--replicates", "3"]
    argv += ["--config", "safety_screen=rules_and_jev", "--prices", str(prices(tmp_path))]
    assert main([*argv, "--store", str(store.root)]) == 1
    assert "is not the declared remote configuration" in capsys.readouterr().err


def test_a_run_refuses_to_start_in_ci(
    store: Store,
    inbox: Pack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = fake_jev(monkeypatch)
    declare(store, inbox, arms.CLASSIFIER, jev(inbox).providers)
    commit(store)
    argv = ["scoreboard", "run", INBOX, "--dataset", SET, "--config", *JEV, "--replicates", "3"]
    argv += ["--prices", str(prices(tmp_path)), "--store", str(store.root)]
    for name in signing.CI_SETTINGS:
        monkeypatch.setenv(name, "true")
        with pytest.raises(MendariError, match=f"{name} is set: a remote configuration .* CI"):
            results_store.measure(inbox, SET, jev(inbox), date=DATE, replicates=3)
        assert main(argv) == 1
        assert f"error: {name} is set: a remote configuration" in capsys.readouterr().err
        monkeypatch.delenv(name)
    assert calls == [] and not (store.root / "results").exists()
    # A local configuration decides on this machine and calls nothing: it runs in CI too.
    monkeypatch.setenv("CI", "true")
    local = results_store.configuration_of(inbox, ["router=naive_bayes"])
    assert results_store.record_run(store, inbox, SET, local, date=DATE).action == "stored"
    # And the workflows never run a remote configuration or a canary.
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        assert "--replicates" not in text and "scoreboard canary" not in text, workflow.name


@pytest.fixture(scope="module")
def test_replicates(inbox: Pack) -> results_store.Measured:
    """Three replicates of the inbox's Jev router on its `test` set (600 cases), which agree on
    every case: made once for the module."""
    with pytest.MonkeyPatch.context() as patch:
        for name in signing.CI_SETTINGS:
            patch.delenv(name, raising=False)
        fake_jev(patch)
        made = declaration(INBOX, arms.CLASSIFIER, jev(inbox).providers)
        declared = runs.declaration(made).sha256
        return results_store.measure(
            inbox, "test", jev(inbox), date=DATE, replicates=3, declared=declared
        )


@pytest.fixture
def replicated(
    store: Store, inbox: Pack, test_replicates: results_store.Measured
) -> results_store.Held:
    """`test_replicates`, stored as a result with its declaration (written past `add`, which
    would recompute it: the store check does that)."""
    declare(store, inbox, arms.CLASSIFIER, jev(inbox).providers)
    for ledger, run in test_replicates.inputs:
        store.put_ledger(ledger)
        store.put_run(run)
    target = store.root / "results" / f"{test_replicates.name}.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(signing.dump(test_replicates.entry), encoding="utf-8")
    return store.results()[test_replicates.name]


def marks_of(store: Store, pack: Pack) -> dict[str, list[str]]:
    """Each current result's marks on the board, by name, with the store's canaries."""
    current = results_store.heads(store.results())
    board = scoreboard.board(
        [found.result for found in current.values()],
        {INBOX: scoreboard.Current.of(pack)},
        list(store.canaries().values()),
    )
    return {
        scoreboard.body_sha256(item["entry"]).removeprefix("sha256:"): item["marks"][2:-1]
        for table in board["tables"]
        for item in table["entries"]
    }


def test_another_reported_model_marks_the_arm_stale(
    store: Store, inbox: Pack, replicated: results_store.Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert replicated.result["figures"]["replicates"]["flip_rate"]["k"] == 0
    assert marks_of(store, inbox) == {replicated.name: []}
    # A canary whose Jev reports another model marks the configuration's results stale.
    fake_jev(monkeypatch, model=lambda call: "jev-10.0.0")
    name, canary = results_store.run_canary(
        store, inbox, jev(inbox), size=30, seed=7, date=DATE + datetime.timedelta(days=3)
    )
    assert canary["another_model"] is True and canary["drift"] is False
    assert canary["models"] == {"jev": ["jev-10.0.0"]}
    assert marks_of(store, inbox) == {replicated.name: [scoreboard.STALE_MODEL]}
    # A replicate after the headline that reports another model: stale from the start.
    (store.root / "canaries" / f"{name}.json").unlink()
    fake_jev(monkeypatch, model=lambda call: JEV_MODEL if call <= 30 else "jev-10.0.0")
    measured = results_store.measure(inbox, SET, jev(inbox), date=DATE, replicates=3)
    assert measured.entry["figures"]["replicates"]["models"] == {"jev": ["jev-10.0.0", JEV_MODEL]}
    (table,) = scoreboard.board([measured.entry], {INBOX: scoreboard.Current.of(inbox)})["tables"]
    assert scoreboard.STALE_MODEL in table["entries"][0]["marks"]


def test_canary_drift_marks_its_arm_stale(
    store: Store, inbox: Pack, replicated: results_store.Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replicates agree on every case (0 of 600 flips): a canary that differs from replicate
    1 on enough of its 30 cases that its lower bound is above their upper bound shows drift."""
    later = DATE + datetime.timedelta(days=5)
    fake_jev(monkeypatch)  # the same answers as the replicates: no drift
    steady_name, steady = results_store.run_canary(
        store, inbox, jev(inbox), size=30, seed=11, date=later
    )
    assert steady["flip_rate"]["k"] == 0 and steady["drift"] is False
    assert marks_of(store, inbox) == {replicated.name: []}
    fake_jev(monkeypatch, urgent=lambda call: True)  # every drawn case answered "urgent"
    name, drifted = results_store.run_canary(store, inbox, jev(inbox), size=30, seed=11, date=later)
    k, n = drifted["flip_rate"]["k"], drifted["flip_rate"]["n"]
    assert n == 30 and k >= 2
    assert stats.clopper_pearson(k, n)[0] > stats.clopper_pearson(0, 600)[1]
    assert drifted["drift"] is True and drifted["another_model"] is False
    assert marks_of(store, inbox) == {replicated.name: [scoreboard.STALE_DRIFT]}
    # Its draw is the seed's, matched by ledger hash; another seed draws other cases.
    drawn = store.ledger(drifted["inputs"]["ledger"])
    assert [case["c"] for case in drawn.cases] == [
        case["c"] for case in store.ledger(steady["inputs"]["ledger"]).cases
    ]
    dataset = results_store.resolve_dataset(inbox, "test")
    hashes = [case["c"] for case in store.ledger(replicated.ledger).cases]
    assert runs.draw(hashes, 30, 11) != runs.draw(hashes, 30, 12)
    # A run of the configuration after its replicates is not marked by the earlier canary.
    newer = json.loads(json.dumps(replicated.result))
    newer["scope"]["measured_on"] = "2026-10-25"
    newer["supersedes"] = f"sha256:{replicated.name}"
    newer["body_sha256"] = scoreboard.body_sha256(newer)
    (table,) = scoreboard.board(
        [newer], {INBOX: scoreboard.Current.of(inbox)}, list(store.canaries().values())
    )["tables"]
    assert scoreboard.STALE_DRIFT not in table["entries"][0]["marks"]
    # The store check recomputes each canary from its inputs; one removed or edited is found.
    base = {
        stem: (store.root / "canaries" / f"{stem}.json").read_bytes()
        for stem in (name, steady_name)
    }
    edited = json.loads(json.dumps(drifted))
    edited["drift"] = False
    edited["body_sha256"] = scoreboard.body_sha256(edited)
    (store.root / "canaries" / f"{name}.json").unlink()
    store.put_canary(edited)
    problems = results_store.check(store, base={}, base_canaries=base).problems
    canaries = [line for line in problems if "canaries/" in line]
    edited_name = scoreboard.body_sha256(edited).removeprefix("sha256:")
    assert canaries == [
        f"canaries/{name}.json: removed or changed: a canary is kept as it was run",
        f"canaries/{edited_name}.json: it is not what its inputs give (recomputed, a byte differs)",
    ]  # and the steady canary is what its inputs give
    assert dataset.sha256 == replicated.dataset


def test_the_canary_never_runs_in_ci(
    store: Store,
    inbox: Pack,
    replicated: results_store.Held,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = fake_jev(monkeypatch)
    argv = ["scoreboard", "canary", INBOX, "--config", *JEV, "--seed", "3"]
    argv += ["--store", str(store.root)]
    for name in signing.CI_SETTINGS:
        monkeypatch.setenv(name, "1")
        with pytest.raises(MendariError, match=f"{name} is set: a canary is never run in CI"):
            results_store.run_canary(store, inbox, jev(inbox), size=30, seed=3, date=DATE)
        assert main(argv) == 1
        assert f"error: {name} is set: a canary is never run in CI" in capsys.readouterr().err
        monkeypatch.delenv(name)
    assert calls == [] and store.canaries() == {}
    # It is the method's 30 cases, of a remote configuration with replicates on `test`.
    with pytest.raises(MendariError, match="is 30 cases"):
        results_store.run_canary(store, inbox, jev(inbox), size=10, seed=3, date=DATE)
    local = results_store.configuration_of(inbox, ["router=naive_bayes"])
    with pytest.raises(MendariError, match="a canary is of a remote one"):
        results_store.run_canary(store, inbox, local, size=30, seed=3, date=DATE)
    other = results_store.configuration_of(inbox, ["safety_screen=rules_and_jev"])
    with pytest.raises(MendariError, match="has no current result with replicates"):
        results_store.run_canary(store, inbox, other, size=30, seed=3, date=DATE)
    # Outside CI, by hand: stored.
    assert main(argv) == 0
    assert capsys.readouterr().out.startswith("stored: canaries/")
    assert len(store.canaries()) == 1 and len(calls) == 30
    # No schedule runs it: no workflow, and no cron of the hosted app.
    vercel = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert "canary" not in json.dumps(vercel.get("crons", []))
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "canary" not in yaml.safe_dump(yaml.safe_load(workflow.read_text())), workflow


def test_an_undisclosed_provider_is_not_published(
    inbox: Pack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_jev(monkeypatch, urgent=lambda call: call == 40)
    measured = results_store.measure(inbox, SET, jev(inbox), date=DATE, replicates=3)
    pending = measured.entry
    assert pending["scope"]["disclosure"] == {
        "status": scoreboard.DISCLOSURE_PENDING,
        "statements": {"jev": None},
    }
    local = results_store.measure(
        inbox, SET, results_store.configuration_of(inbox, ["router=rules"]), date=DATE
    ).entry
    assert local["scope"]["disclosure"]["status"] == scoreboard.NO_MODEL_PROVIDER
    current = {INBOX: scoreboard.Current.of(inbox)}
    (table,) = scoreboard.board([pending, local], current)["tables"]
    by_name = {item["configuration"]: item for item in table["entries"]}
    held = by_name[results_store.written(jev(inbox).providers)]
    assert held["published"] is False and by_name["router=rules,safety_screen=rules"]["published"]
    # The insufficient-evidence banner names only published entries.
    assert {short["configuration"] for short in table["banner"]["short"]} == {
        "router=rules,safety_screen=rules"
    }
    text = scoreboard_markdown(scoreboard.board([pending], current))
    rows = dict(
        line.strip("|").split(" | ", 1)
        for line in text.splitlines()
        if line.startswith("| ") and not line.startswith("| |")
    )
    # Its scope is shown; every figure, the replicates' included, is not.
    assert rows[" Disclosure"].strip() == scoreboard.DISCLOSURE_PENDING
    assert rows[" Models"].strip() == f"jev: {JEV_MODEL}"
    figures = [
        label
        for label in rows
        if label.strip()
        not in ("Marks", "Pack", "Set", "Method", "Measured on", "Models", "Disclosure", "Status")
    ]
    assert "Case flip rate" in [label.strip() for label in figures]
    assert {rows[label].strip(" |") for label in figures} == {scoreboard.DISCLOSURE_PENDING}
    assert " of 30 [" not in text and "Subgroups of" not in text
    # A statement for its type in the disclosures file, and it is published.
    statements = {"jev": "No commercial relationship (a test's statement)."}
    disclosures = runs.disclosures(
        json.dumps(
            {"format": runs.DISCLOSURES_FORMAT, "date": "2026-10-18", "statements": statements}
        ).encode()
    )
    stated = results_store.measure(
        inbox, SET, jev(inbox), date=DATE, replicates=3, disclosures=disclosures
    ).entry
    assert stated["scope"]["disclosure"] == {"status": "stated", "statements": statements}
    shown = scoreboard_markdown(scoreboard.board([stated], current))
    assert "| Case flip rate | " in shown and " of 30 [" in shown
    # The same holds for CQL, which calls Mendari's own CQL engine and no model.
    trial = resolve_pack("mendari/clinical-trial-prescreener")
    cql = results_store.configuration_of(trial, ["structured=cql"])
    assert cql.remote_types == ("cql",) and cql.arm == arms.DETERMINISTIC
    assert scoreboard._disclosure(cql, None)["status"] == scoreboard.DISCLOSURE_PENDING
    assert scoreboard._disclosure(cql, disclosures)["status"] == scoreboard.DISCLOSURE_PENDING


def test_an_llm_replicate_is_priced_from_the_dated_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM arm through the fake gateway (`tests/test_llm.py`'s `install`): three replicates,
    each priced from the same dated table, and the spread of the cost across them."""
    pack = resolve_pack(INBOX)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("LLM_MODEL", "test-llm")
    counter = itertools.count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        number = next(counter)
        body = json.loads(request.content)
        schema = body["response_format"]["json_schema"]["schema"]["properties"]
        content = {}
        for question, spec in schema.items():
            labels = list(spec["properties"])
            top = labels[-1] if number % 2 else labels[0]
            rest = 0.1 / (len(labels) - 1)
            content[question] = {label: (0.9 if label == top else rest) for label in labels}
        found = completion(content, model="test-llm")
        found["usage"] = {"prompt_tokens": 300 + number % 3, "completion_tokens": 40}
        return httpx.Response(200, json=found)

    install(monkeypatch, handler)
    configuration = results_store.configuration_of(pack, ["safety_screen=llm"])
    assert configuration.remote_types == ("llm",)
    path = tmp_path / "prices.json"
    table = {
        "format": runs.PRICES_FORMAT,
        "date": DATE.isoformat(),
        "source": "a test",
        "prices": {"llm": {"test-llm": {"input_per_1000": 0.01, "output_per_1000": 0.03}}},
    }
    path.write_text(json.dumps(table), encoding="utf-8")
    measured = results_store.measure(
        pack, SET, configuration, date=DATE, replicates=3, prices=runs.load_prices(path)
    )
    assert measured.entry["figures"]["cost_per_1000"]["status"] == scoreboard.PRICED
    spread = measured.entry["figures"]["replicates"]["spread"]["Cost per 1,000 decisions"]
    assert spread["replicates"] == 3 and spread["min"] <= spread["max"]
    assert LlmProvider.type == "llm"
