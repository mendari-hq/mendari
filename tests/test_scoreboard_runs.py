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
import re
import subprocess
from collections.abc import Callable, Sequence
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
_LLM_INIT = LlmProvider.__init__  # as the module is imported, before any test replaces it


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
    fails: Callable[[int], bool] = lambda call: False,
) -> list[int]:
    """`calm_jev`'s answers, except that call number `n` (from 1) answers "urgent" and the
    billing team, confidently, when `urgent(n)`, reports the model `model(n)`, and fails (a
    server error, so no answer and no model) when `fails(n)`. Returns the calls made, as they
    are made."""
    original = _JEV_INIT  # never a fake's: a test may set up another fake after one
    calm_jev(monkeypatch)  # its settings
    calls: list[int] = []
    counter = itertools.count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        number = next(counter)
        calls.append(number)
        if fails(number):
            return httpx.Response(500)
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
            disclosures=runs.Disclosures(date=DATE.isoformat(), statements={"jev": "None."}),
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
    # Nor one of 2 replicates (`entry` makes it: `runs.replicates` takes 1 to 5), or of 6 (a
    # result whose inputs list them), by `add` or past it: the store holds 3 to 5.
    for ledger, run in single.inputs:
        store.put_ledger(ledger)
        store.put_run(run)
    two = scoreboard.entry(
        single.ledger,
        results_store.resolve_dataset(inbox, SET),
        single.run,
        pack=scoreboard.PackScope.of(inbox),
        arm=arms.CLASSIFIER,
        configuration=jev(inbox),
        prices=None,
        disclosures=None,
        replicates=single.inputs[1:2],
        declared=store.declaration(INBOX).sha256,  # type: ignore[union-attr]
    )
    assert two["figures"]["replicates"]["count"] == 2
    six = json.loads(json.dumps(single.entry))
    six["inputs"]["replicates"] += [
        {"replicate": number, "ledger": single.ledger.id, "run": runs.run_sha256(single.run)}
        for number in (4, 5, 6)
    ]
    six["body_sha256"] = scoreboard.body_sha256(six)
    for result, count in ((two, 2), (six, 6)):
        with pytest.raises(MendariError, match=f"holds 3 to 5 replicates, not {count}$"):
            store.add(result)
        target = store.root / "results" / f"{results_store.name_of(result)}.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(signing.dump(result), encoding="utf-8")
        problems = results_store.check(store, base={}).problems
        assert any(line.endswith(f"holds 3 to 5 replicates, not {count}") for line in problems)
        target.unlink()


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


def test_a_run_whose_provider_is_not_set_up_is_refused_before_it_starts(
    store: Store,
    inbox: Pack,
    replicated: results_store.Held,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without its provider's settings every case would fail and go to a person, and the result
    stored, of those failures, would supersede a good one (the runbook's step 8, run without
    `MENDARI_CQL_URL`, stored 0 of 300 over 298 of 300). The run, and a canary, are refused
    before anything runs, naming the setting, and nothing is stored."""
    trial = results_store.maintained_pack("mendari/clinical-trial-prescreener")
    cql = results_store.configuration_of(trial, ["structured=cql"])
    declare(store, trial, arms.DETERMINISTIC, cql.providers)
    commit(store)
    monkeypatch.delenv("MENDARI_CQL_URL", raising=False)
    unset = (
        "notes=rules,structured=cql: structured=cql cannot run on this machine, so every case "
        "would fail: not_configured: set MENDARI_CQL_URL"
    )
    with pytest.raises(MendariError, match=re.escape(unset)):
        results_store.record_run(store, trial, "test", cql, date=DATE, replicates=3, again=True)
    argv = ["scoreboard", "run", trial.id, "--config", "structured=cql", "--replicates", "3"]
    assert main([*argv, "--again", "--store", str(store.root)]) == 1
    assert capsys.readouterr().err.startswith(f"error: {unset}")
    before = sorted(store.files("results"))
    # Jev without its endpoint: neither the run nor a canary makes a call.
    calls = fake_jev(monkeypatch)
    monkeypatch.delenv("JEV_API_URL")
    table = prices(tmp_path).read_bytes()
    no_endpoint = "router=jev cannot run on this machine, so every case would fail: "
    with pytest.raises(MendariError, match=f"{no_endpoint}not_configured: set JEV_API_URL"):
        results_store.record_run(
            store, inbox, "test", jev(inbox), date=DATE, replicates=3, prices=table, again=True
        )
    with pytest.raises(MendariError, match=f"{no_endpoint}not_configured: set JEV_API_URL"):
        results_store.run_canary(store, inbox, jev(inbox), size=30, seed=3, date=DATE)
    assert calls == [] and store.canaries() == {}
    assert sorted(store.files("results")) == before == [f"{replicated.name}.json"]


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
    shown = scoreboard_markdown(scoreboard.board([stated], current, disclosures=disclosures))
    assert "| Case flip rate | " in shown and " of 30 [" in shown
    # Published only while the disclosures file still holds its statement, word for word: one
    # taken out (no file, or no statement for its type) or rewritten hides its figures, and
    # its old statement is never shown as if it were current.
    rewritten = runs.Disclosures("2026-11-01", {"jev": "Mendari now resells Jev (a test's)."})
    for now in (None, runs.Disclosures("2026-11-01", {}), rewritten):
        (table,) = scoreboard.board([stated], current, disclosures=now)["tables"]
        assert table["entries"][0]["published"] is False
        hidden = scoreboard_markdown(scoreboard.board([stated], current, disclosures=now))
        assert f"| Disclosure | {scoreboard.DISCLOSURE_PENDING} |" in hidden
        assert statements["jev"] not in hidden and " of 30 [" not in hidden
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


# --------------------------------------------------------------------------------------------
# The review of LP-17h: what a paid run must never lose, and freshness by type and by arm
# --------------------------------------------------------------------------------------------


def table_of(tmp_path: Path, name: str, models: dict[str, dict[str, float]]) -> Path:
    """A price table dated `DATE` that prices `models` (each Jev model's two rates)."""
    path = tmp_path / f"{name}.json"
    data = {
        "format": runs.PRICES_FORMAT,
        "date": DATE.isoformat(),
        "source": "a test",
        "prices": {"jev": models},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_run_its_table_cannot_price_is_stored_as_unknown(
    store: Store, inbox: Pack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table that lacks the model Jev reported, or a model that changes during replicate 1,
    gives a cost of "unknown", and the result is still stored: the store recomputes it with no
    table (none is cited) and gets the same bytes, so no paid run is thrown away."""
    calls = fake_jev(monkeypatch)
    declare(store, inbox, arms.CLASSIFIER, jev(inbox).providers)
    commit(store)
    other = table_of(
        tmp_path, "other", {"other-model": {"input_per_1000": 1, "output_per_1000": 1}}
    )
    lacking = results_store.record_run(
        store, inbox, SET, jev(inbox), date=DATE, replicates=3, prices=other.read_bytes()
    )
    assert lacking.action == results_store.STORED and len(calls) == 90
    held = store.results()[lacking.name]
    assert held.result["figures"]["cost_per_1000"] == {
        "status": scoreboard.UNKNOWN,
        "reason": f"jev: {scoreboard.NO_PRICE}",
    }
    assert held.prices is None and not (store.root / "prices").exists()  # cited by none
    spread = held.result["figures"]["replicates"]["spread"]["Cost per 1,000 decisions"]
    assert spread == {"status": runs.NOT_ESTIMABLE}
    # Jev rolls to another model during replicate 1: two models, so the cost is unknown.
    fake_jev(monkeypatch, model=lambda call: JEV_MODEL if call <= 15 else "jev-10.0.0")
    priced = table_of(tmp_path, "priced", {JEV_MODEL: {"input_per_1000": 1, "output_per_1000": 1}})
    rolled = results_store.record_run(
        store,
        inbox,
        SET,
        jev(inbox),
        date=DATE,
        again=True,
        replicates=3,
        prices=priced.read_bytes(),
    )
    assert rolled.action == results_store.STORED and rolled.supersedes == lacking.name
    found = store.results()[rolled.name].result
    assert found["figures"]["cost_per_1000"] == {
        "status": scoreboard.UNKNOWN,
        "reason": "jev: 2 models reported, not one",
    }
    assert found["inputs"]["prices"] is None
    check = results_store.check(store, base={})
    assert check.problems == [] and check.recomputed == 2 and check.rerun == 0


def fake_llm(monkeypatch: pytest.MonkeyPatch, *, model: Callable[[int], str]) -> list[int]:
    """The LLM through a fake gateway (as `tests/test_llm.py`'s `install`): call number `n`
    (from 1) is sure of each question's first label and reports the model `model(n)`. Returns
    the calls made, as they are made."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("LLM_MODEL", "test-llm")
    calls: list[int] = []
    counter = itertools.count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        number = next(counter)
        calls.append(number)
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
        content = {}
        for question, spec in schema["properties"].items():
            labels = list(spec["properties"])
            rest = 0.1 / (len(labels) - 1)
            content[question] = {label: 0.9 if label == labels[0] else rest for label in labels}
        return httpx.Response(200, json=completion(content, model=model(number)))

    def init(self: LlmProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        _LLM_INIT(self, *args, **kwargs)  # never a fake's: a test may set up another after one

    monkeypatch.setattr(LlmProvider, "__init__", init)
    return calls


def test_a_run_of_two_model_types_its_table_cannot_price_is_stored_as_unknown(
    store: Store, inbox: Pack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jev and an LLM in one configuration (the "+ both" arm), with a table that prices Jev's
    model and not the LLM's, or with an LLM that rolls to another model during replicate 1: the
    cost is unknown for a reason no table changes, so the result reads the same recomputed with
    no table (it cites none) and is stored, not refused after every call."""
    both = results_store.configuration_of(inbox, [*JEV, "safety_screen=llm"])
    assert both.arm == arms.CLASSIFIER_AND_LLM and both.remote_types == ("jev", "llm")
    jev_calls = fake_jev(monkeypatch)
    llm_calls = fake_llm(monkeypatch, model=lambda call: "test-llm")
    declare(store, inbox, arms.CLASSIFIER_AND_LLM, both.providers)
    commit(store)
    rate = {"input_per_1000": 1.0, "output_per_1000": 1.0}
    jev_only = table_of(tmp_path, "jev-only", {JEV_MODEL: rate}).read_bytes()
    lacking = results_store.record_run(
        store, inbox, SET, both, date=DATE, replicates=3, prices=jev_only
    )
    assert lacking.action == results_store.STORED
    assert len(jev_calls) == 90 and len(llm_calls) == 90
    held = store.results()[lacking.name]
    assert held.result["scope"]["models"]["by_type"] == {"jev": [JEV_MODEL], "llm": ["test-llm"]}
    assert held.result["figures"]["cost_per_1000"] == {
        "status": scoreboard.UNKNOWN,
        "reason": f"jev, llm: {scoreboard.NO_PRICES}",
    }
    assert held.prices is None and not (store.root / "prices").exists()  # cited by none
    # The LLM rolls to another model during replicate 1 (its first 15 calls one, then another):
    # two models of one type, whichever table is at hand.
    fake_llm(monkeypatch, model=lambda call: "test-llm" if call <= 15 else "test-llm-2")
    rolled = results_store.record_run(
        store, inbox, SET, both, date=DATE, again=True, replicates=3, prices=jev_only
    )
    assert rolled.action == results_store.STORED and rolled.supersedes == lacking.name
    assert store.results()[rolled.name].result["figures"]["cost_per_1000"] == {
        "status": scoreboard.UNKNOWN,
        "reason": "llm: 2 models reported, not one",
    }
    check = results_store.check(store, base={})
    assert check.problems == [] and check.recomputed == 2 and check.rerun == 0


def test_a_price_table_the_store_would_refuse_is_refused_before_any_call(
    store: Store,
    inbox: Pack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = fake_jev(monkeypatch)
    declare(store, inbox, arms.CLASSIFIER, jev(inbox).providers)
    commit(store)
    stored = table_of(tmp_path, "stored", {JEV_MODEL: {"input_per_1000": 1, "output_per_1000": 1}})
    store.put_prices(stored.read_bytes())
    changed = table_of(tmp_path, "new", {JEV_MODEL: {"input_per_1000": 2, "output_per_1000": 2}})
    clash = f"prices/{DATE.isoformat()}.json holds another table of that date"
    with pytest.raises(MendariError, match=clash):
        results_store.refuse_unrunnable(
            store, inbox, SET, jev(inbox), replicates=3, prices=changed.read_bytes()
        )
    with pytest.raises(MendariError, match=clash):
        results_store.record_run(
            store, inbox, SET, jev(inbox), date=DATE, replicates=3, prices=changed.read_bytes()
        )
    argv = ["scoreboard", "run", INBOX, "--dataset", SET, "--config", *JEV, "--replicates", "3"]
    assert main([*argv, "--prices", str(changed), "--store", str(store.root)]) == 1
    assert clash in capsys.readouterr().err
    assert calls == [] and not (store.root / "results").exists()
    # The same table as the stored one is no clash.
    results_store.refuse_unrunnable(
        store, inbox, SET, jev(inbox), replicates=3, prices=stored.read_bytes()
    )


def test_a_model_calling_configuration_needs_its_price_table(
    store: Store,
    inbox: Pack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = fake_jev(monkeypatch)
    declare(store, inbox, arms.CLASSIFIER, jev(inbox).providers)
    commit(store)
    needs = "calls a model: give its dated price table"
    with pytest.raises(MendariError, match=needs):
        results_store.refuse_unrunnable(store, inbox, SET, jev(inbox), replicates=3)
    with pytest.raises(MendariError, match=needs):
        results_store.record_run(store, inbox, SET, jev(inbox), date=DATE, replicates=3)
    argv = ["scoreboard", "run", INBOX, "--dataset", SET, "--store", str(store.root)]
    assert main([*argv, "--config", *JEV, "--replicates", "3"]) == 1
    assert needs in capsys.readouterr().err
    # A local configuration calls no model, so it is never given a table.
    local = results_store.configuration_of(inbox, ["router=naive_bayes"])
    table = prices(tmp_path)
    with pytest.raises(MendariError, match="is local: it calls no model, so it is never priced"):
        results_store.refuse_unrunnable(store, inbox, SET, local, prices=table.read_bytes())
    assert main([*argv, "--every-local", "--prices", str(table)]) == 1
    assert "is local: it calls no model" in capsys.readouterr().err
    assert calls == [] and not (store.root / "results").exists()
    # CQL calls Mendari's own engine and no model: declared, with its engine set, it needs no
    # table.
    trial = results_store.maintained_pack("mendari/clinical-trial-prescreener")
    cql = results_store.configuration_of(trial, ["structured=cql"])
    declare(store, trial, arms.DETERMINISTIC, cql.providers)
    commit(store)
    monkeypatch.setenv("MENDARI_CQL_URL", "http://127.0.0.1:8080/fhir")
    declared = results_store.refuse_unrunnable(store, trial, "test", cql, replicates=3)
    assert declared == store.declaration(trial.id).sha256  # type: ignore[union-attr]


def stated_on(found: dict[str, Any], measured_on: str, **changes: Any) -> dict[str, Any]:
    """`found` as if measured on `measured_on` (with `changes` to its scope), its body's SHA-256
    made again: a board reads entries and never recomputes them."""
    made = json.loads(json.dumps(found))
    made["scope"] = {**made["scope"], "measured_on": measured_on, **changes}
    made["body_sha256"] = scoreboard.body_sha256(made)
    return made


def board_marks(
    entries: list[dict[str, Any]], pack: Pack, canaries: Sequence[dict[str, Any]] = ()
) -> list[list[str]]:
    """Each entry's freshness marks, in the order given (drift and another model only)."""
    shown = scoreboard.board(entries, {INBOX: scoreboard.Current.of(pack)}, canaries)
    by_name = {
        item["entry"]["body_sha256"]: item["marks"]
        for table in shown["tables"]
        for item in table["entries"]
    }
    kept = (scoreboard.STALE_DRIFT, scoreboard.STALE_MODEL)
    return [[m for m in by_name[found["body_sha256"]] if m in kept] for found in entries]


def test_a_canary_or_replicate_that_named_no_model_reports_no_other(
    store: Store, inbox: Pack, replicated: results_store.Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canary whose every Jev call failed names no model: that is not another model, though
    the failures may well show as drift. Nor is a headline whose calls all failed, beside later
    replicates that named one model."""
    fake_jev(monkeypatch, fails=lambda call: True)
    _, failed = results_store.run_canary(
        store, inbox, jev(inbox), size=30, seed=5, date=DATE + datetime.timedelta(days=2)
    )
    assert failed["models"] == {} and failed["another_model"] is False
    (marks,) = board_marks([replicated.result], inbox, [failed])
    assert scoreboard.STALE_MODEL not in marks
    # Replicate 1's calls all fail, and replicates 2 and 3 name one model: not stale.
    fake_jev(monkeypatch, fails=lambda call: call <= 30)
    measured = results_store.measure(inbox, SET, jev(inbox), date=DATE, replicates=3)
    assert measured.entry["scope"]["models"]["by_type"] == {}
    assert measured.entry["figures"]["replicates"]["models"] == {"jev": [JEV_MODEL]}
    assert board_marks([measured.entry], inbox) == [[]]


def test_another_model_marks_every_result_of_its_type(
    store: Store,
    inbox: Pack,
    replicated: results_store.Held,
    test_replicates: results_store.Measured,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another model, reported by a later run of the same model type, marks the results of any
    configuration or pack that reported another one: Jev's model is the deployment's, whatever
    calls it. The same day counts (a day holds no order); a run that reports the same model, or
    an earlier one, marks nothing."""
    fake_jev(monkeypatch, model=lambda call: "jev-10.0.0")
    screen = results_store.configuration_of(inbox, [*JEV, "safety_screen=rules_and_jev"])
    assert screen.arm == arms.CLASSIFIER and screen.providers != jev(inbox).providers
    later = results_store.measure(
        inbox, SET, screen, date=DATE + datetime.timedelta(days=2), replicates=3
    ).entry
    assert later["scope"]["models"]["by_type"] == {"jev": ["jev-10.0.0"]}
    earlier = replicated.result
    assert board_marks([earlier, later], inbox) == [[scoreboard.STALE_MODEL], []]
    # Of another pack (the board groups it apart; the mark is by type, not by pack).
    elsewhere = stated_on(
        later, later["scope"]["measured_on"], pack={**later["scope"]["pack"], "id": "mendari/x"}
    )
    assert board_marks([earlier, elsewhere], inbox) == [[scoreboard.STALE_MODEL], []]
    # The same day: both are marked, as either may be the later.
    same_day = stated_on(later, earlier["scope"]["measured_on"])
    assert board_marks([earlier, same_day], inbox) == [[scoreboard.STALE_MODEL]] * 2
    # A later run of the same model, or an earlier run of another, marks nothing (the earlier
    # run is the one they mark).
    before = stated_on(later, "2026-10-01")
    again = stated_on(earlier, "2026-10-21")
    assert board_marks([earlier, again, before], inbox) == [[], [], [scoreboard.STALE_MODEL]]
    # A canary of another configuration, on another day, that names the new model marks the
    # results measured no later than it that named the old one, whatever their configuration.
    canary = {
        **json.loads(json.dumps(test_replicates.entry)),
        "format": scoreboard.CANARY_FORMAT,
        "pack": {"id": INBOX},
        "arm": arms.CLASSIFIER,
        "configuration": {"providers": screen.providers},
        "measured_on": "2026-10-20",
        "models": {"jev": ["jev-10.0.0"]},
        "drift": False,
        "against": {"measured_on": "2026-10-20"},
    }
    assert board_marks([earlier, again], inbox, [canary]) == [[scoreboard.STALE_MODEL], []]


def test_canary_drift_marks_its_arms_remote_results_up_to_its_day(
    store: Store, inbox: Pack, replicated: results_store.Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canary that drifts marks the remote results of its pack's arm, of whatever declared
    configuration and on whatever set, measured before its day or no later than its replicates;
    never a local configuration's, which is deterministic, and never one run again from the
    canary's day on."""
    canary_on = DATE + datetime.timedelta(days=5)
    fake_jev(monkeypatch, urgent=lambda call: True)
    _, drifted = results_store.run_canary(
        store, inbox, jev(inbox), size=30, seed=11, date=canary_on
    )
    assert drifted["drift"] is True
    earlier = replicated.result
    between = stated_on(earlier, "2026-10-20", set={**earlier["scope"]["set"], "name": SET})
    screen = results_store.configuration_of(inbox, ["safety_screen=rules_and_jev"])
    declared_before = json.loads(json.dumps(between))
    declared_before["configuration"] = screen.to_dict()
    declared_before["body_sha256"] = scoreboard.body_sha256(declared_before)
    on_its_day = stated_on(earlier, canary_on.isoformat())
    after = stated_on(earlier, "2026-10-25")
    entries = [earlier, between, declared_before, on_its_day, after]
    drift = [scoreboard.STALE_DRIFT]
    assert board_marks(entries, inbox, [drifted]) == [drift, drift, drift, [], []]
    # A canary of the prescreener's CQL (the deterministic arm, Mendari's own engine) marks its
    # CQL results, and never the arm's local configurations, which call nothing off the machine.
    trial = results_store.maintained_pack("mendari/clinical-trial-prescreener")
    committed = [
        found.result
        for found in results_store.heads(Store.at(ROOT / "scoreboard").results()).values()
        if found.pack_id == trial.id and not found.is_hidden
    ]
    # Dated the day after the latest of them, whenever they were measured (the runbook's step 8
    # measures them again).
    latest = max(result["scope"]["measured_on"] for result in committed)
    cql_drift = {
        "format": scoreboard.CANARY_FORMAT,
        "pack": {"id": trial.id},
        "arm": arms.DETERMINISTIC,
        "configuration": {"providers": {"notes": "rules", "structured": "cql"}},
        "measured_on": (
            datetime.date.fromisoformat(latest) + datetime.timedelta(days=1)
        ).isoformat(),
        "models": {},
        "drift": True,
        "against": {"measured_on": latest},
    }
    shown = scoreboard.board(committed, {trial.id: scoreboard.Current.of(trial)}, [cql_drift])
    marked = {
        (item["configuration"], item["entry"]["configuration"]["local"]): (
            scoreboard.STALE_DRIFT in item["marks"]
        )
        for table in shown["tables"]
        for item in table["entries"]
    }
    assert {local for (_, local), on in marked.items() if on} == {False}
    assert {local for (_, local), on in marked.items() if not on} == {True}


def test_the_page_publishes_against_the_disclosures_file_as_it_is(
    store: Store, inbox: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page reads `scoreboard/disclosures.json` as it is now: a result stored with a
    statement shows its figures while the file holds that statement, and none of them, nor the
    statement, once it is taken out or rewritten. The site's scores (`web/index.html`, LP-17g)
    are written from the same board, so they publish by the same rule: its figures and its
    models while the file holds the statement, "not published: disclosure pending" in their
    place once it does not."""
    from tests.test_scoreboard_page import page_script

    fake_jev(monkeypatch)
    monkeypatch.setattr(page_script, "WEB_SET", SET)  # the site's set, as this result's
    statement = "No commercial relationship (a test's statement)."
    path = store.root / "disclosures.json"

    def write(statements: dict[str, str]) -> None:
        data = {"format": runs.DISCLOSURES_FORMAT, "date": "2026-10-18", "statements": statements}
        path.write_text(json.dumps(data), encoding="utf-8")

    write({"jev": statement})
    measured = results_store.measure(
        inbox, SET, jev(inbox), date=DATE, replicates=3, disclosures=store.disclosures()
    )
    for ledger, run in measured.inputs:
        store.put_ledger(ledger)
        store.put_run(run)
    target = store.root / "results" / f"{measured.name}.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(signing.dump(measured.entry), encoding="utf-8")
    shown = page_script.page(store)
    assert statement in shown and " of 30 [" in shown
    site = page_script.web_scores(store)
    assert f"jev: {JEV_MODEL}" in site and " of 30" in site
    assert scoreboard.DISCLOSURE_PENDING not in site
    for statements in ({}, {"jev": "Mendari now resells Jev (a test's statement)."}):
        write(statements)
        hidden = page_script.page(store)
        assert statement not in hidden and " of 30 [" not in hidden
        assert f"| Disclosure | {scoreboard.DISCLOSURE_PENDING} |" in hidden
        site = page_script.web_scores(store)
        assert JEV_MODEL not in site and " of 30" not in site
        (row,) = [line for line in site.splitlines() if scoreboard.DISCLOSURE_PENDING in line]
        assert f"configuration {page_script.WITHHELD}" in row and "router=" not in row
    path.unlink()
    assert " of 30 [" not in page_script.page(store)
    assert " of 30" not in page_script.web_scores(store)
