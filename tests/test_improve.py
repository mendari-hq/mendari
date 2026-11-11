# SPDX-License-Identifier: Apache-2.0
"""`mendari improve` (`evals/improve.py`): a pack's classifier learns from new labelled cases, and
what it learned is kept only when it is better on cases it never saw and no worse on everything
the pack already measures. Nothing is written unless asked, and a pack it wrote to is one
`mendari train` rebuilds byte for byte.

The new cases here are the harder inbox draft's train split (436 synthetic messages, a draft that
is not clinically reviewed): they stand in for what a site's reviewers would label."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, load_pack
from mendari.cli import main
from mendari.core import registry
from mendari.core.pack import pack_digest
from mendari.errors import MendariError
from mendari.evals.harness import Dataset, load_dataset
from mendari.evals.improve import (
    BETTER,
    FORMAT,
    MIN_HELD_OUT,
    NOT_BETTER,
    REGRESSED,
    TOO_FEW,
    Improvement,
    apply,
    improve,
)
from mendari.evals.pack_tests import run_pack_tests
from mendari.sandbox import synthetic_inbox
from tests.conftest import INBOX_DIR, LOCAL_DATASET, ROOT

HARDER = ROOT / "datasets" / "drafts" / "patient-inbox-harder" / "train.jsonl"
# The draft's other split: 436 messages that share no family, paraphrase or sentence with HARDER.
UNSEEN = ROOT / "datasets" / "drafts" / "patient-inbox-harder" / "heldout.jsonl"
MODEL = "models/destination-nb.json"
TRAIN = "data/train.jsonl"
CALIBRATION = "data/calibration.jsonl"


SHIPPED = pack_digest(INBOX_DIR)


@pytest.fixture(autouse=True)
def _the_repositorys_own_pack_is_never_written_to() -> Iterator[None]:
    """Every test here works on a copy. Once, a test of this file applied a run to the
    repository's own pack; this fails the test that does."""
    yield
    assert pack_digest(INBOX_DIR) == SHIPPED, "a test changed packs/patient-inbox-router"


def copy_of_the_inbox(root: Path) -> Path:
    shutil.copytree(INBOX_DIR, root)
    return root


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    return copy_of_the_inbox(tmp_path / "patient-inbox-router")


@pytest.fixture(scope="module")
def harder() -> Dataset:
    return load_dataset(HARDER)


@pytest.fixture(scope="module")
def learned(tmp_path_factory: pytest.TempPathFactory, harder: Dataset) -> tuple[Path, Improvement]:
    """One run on the harder cases, shared by the tests that only read it."""
    root = copy_of_the_inbox(tmp_path_factory.mktemp("learned") / "patient-inbox-router")
    return root, improve(load_pack(root), [harder])


def cases_file(path: Path, records: list[dict[str, Any]]) -> Dataset:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return load_dataset(path)


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def right_on_unseen(root: Path) -> int:
    """How many of the draft's other split the pack at `root` sends to the right team."""
    decision = Decision.from_path(root)
    return sum(
        decision.decide(case["input"]).answers["destination"].label == case["labels"]["destination"]
        for case in load_dataset(UNSEEN).records
    )


# -- what a run finds ----------------------------------------------------------------------------


def test_new_cases_make_a_better_model(learned: tuple[Path, Improvement]) -> None:
    _, found = learned
    report = found.report
    assert report["format"] == FORMAT == "mendari.improvement/1"
    assert report["verdict"] == BETTER and found.verdict == BETTER
    assert (report["slot"], report["question"], report["model_file"]) == (
        "router",
        "destination",
        MODEL,
    )
    assert report["providers"] == {"safety_screen": "rules", "router": "naive_bayes"}

    cases = report["cases"]
    assert cases["given"] == 436
    assert (
        cases["learned"]
        + cases["calibration"]
        + cases["held_out"]
        + sum(cases["left_out"].values())
        == 436
    )
    assert (cases["learned"], cases["calibration"], cases["held_out"]) == (227, 76, 133)
    assert cases["held_out"] >= MIN_HELD_OUT

    chosen = report["candidates"][report["chosen"]]
    assert chosen["verdict"] == BETTER and chosen["regressions"] == []
    held_out = chosen["held_out"]
    assert held_out["n"] == cases["held_out"]
    assert held_out["gained"] > held_out["lost"] and held_out["p"] < 0.05
    assert held_out["candidate"] - held_out["current"] == held_out["gained"] - held_out["lost"]
    # The model in the pack gets 90 of the 133 right; with the cases learned and character
    # features, 124. Words alone, 121: both are better, and the one that gains more is chosen.
    assert chosen["features"] == [3, 5]
    assert (held_out["current"], held_out["candidate"]) == (90, 124)
    assert (held_out["gained"], held_out["lost"]) == (37, 3)
    assert [found["held_out"]["candidate"] for found in report["candidates"]] == [121, 124]
    assert chosen["tests"]["failed"] == 0 and chosen["tests"]["passed"] > 0


def test_a_run_judges_the_model_as_it_is_built_and_with_the_other_features(
    learned: tuple[Path, Improvement],
) -> None:
    _, found = learned
    features = [candidate["features"] for candidate in found.report["candidates"]]
    assert features == [None, [3, 5]]  # as the shipped model is built, then with characters


def test_every_dataset_the_pack_declares_is_compared_but_what_it_trains_on(
    learned: tuple[Path, Improvement],
) -> None:
    _, found = learned
    for candidate in found.report["candidates"]:
        names = [compared["dataset"] for compared in candidate["datasets"]]
        assert names == ["test", "red_flags", "red_flags_holdout", "held out"]
        for compared in candidate["datasets"]:
            assert compared["confident"]["candidate"]["n"] <= compared["cases"]
            metrics = [row["metric"] for row in compared["gate"]]
            assert "destination accuracy" in metrics


def test_the_report_holds_no_case(learned: tuple[Path, Improvement], harder: Dataset) -> None:
    _, found = learned
    written = json.dumps(found.report)
    for record in harder.records:
        assert record["id"] not in written
        assert record["input"]["message"]["body"] not in written


def test_nothing_is_written_until_asked(learned: tuple[Path, Improvement]) -> None:
    root, _ = learned
    assert pack_digest(root) == pack_digest(INBOX_DIR)


def test_a_pack_pulled_from_a_registry_is_never_written_to(
    learned: tuple[Path, Improvement],
) -> None:
    root, found = learned
    with pytest.raises(MendariError, match="pulled from a registry"):
        apply(load_pack(root, origin="hub"), found)
    assert pack_digest(root) == pack_digest(INBOX_DIR)


def test_a_reference_pack_of_this_release_is_never_written_to(
    inbox: Path, harder: Dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mendari improve mendari/patient-inbox-router --write` reports and keeps nothing: the
    bundled packs' data comes from their generators. Here the copy stands where they are."""
    found = improve(load_pack(inbox), [harder])
    monkeypatch.setattr(registry, "_BUNDLED_DIRS", [inbox.parent])
    assert registry.origin_of(inbox) == "maintained"
    with pytest.raises(MendariError, match="a reference pack of this release is not changed"):
        apply(load_pack(inbox), found)
    assert pack_digest(inbox) == SHIPPED


def test_the_same_pack_and_cases_give_the_same_report(
    inbox: Path, harder: Dataset, learned: tuple[Path, Improvement]
) -> None:
    _, found = learned
    again = improve(load_pack(inbox), [harder])
    assert again.report == found.report


# -- keeping what was learned ------------------------------------------------------------------


def test_a_pack_it_wrote_to_is_one_train_rebuilds_and_a_second_run_learns_nothing_new(
    inbox: Path, harder: Dataset, capsys: pytest.CaptureFixture[str]
) -> None:
    before = {name: (inbox / name).read_bytes() for name in (MODEL, TRAIN, CALIBRATION)}
    assert right_on_unseen(inbox) == 322  # of 436, as shipped
    found = improve(load_pack(inbox), [harder])
    cases = found.report["cases"]
    held_out = found.report["candidates"][found.report["chosen"]]["held_out"]

    wrote = apply(load_pack(inbox), found)
    assert sorted(path.relative_to(inbox).as_posix() for path in wrote) == [
        CALIBRATION,
        TRAIN,
        MODEL,
    ]
    assert (inbox / MODEL).read_bytes() != before[MODEL]
    for name, count in ((TRAIN, cases["learned"]), (CALIBRATION, cases["calibration"])):
        assert (inbox / name).read_bytes().startswith(before[name])  # appended, nothing changed
        added = lines(inbox / name)[len(before[name].splitlines()) :]
        assert len(added) == count
        assert all(record["id"].startswith("learned-") for record in added)
        assert all(set(record) == {"id", "input", "labels", "attributes"} for record in added)

    # The run is kept once: the pack it judged is not the pack on disk any more.
    stale = {name: (inbox / name).read_bytes() for name in (MODEL, TRAIN, CALIBRATION)}
    with pytest.raises(MendariError, match="the pack has changed since this run judged it"):
        apply(load_pack(inbox), found)
    assert stale == {name: (inbox / name).read_bytes() for name in (MODEL, TRAIN, CALIBRATION)}
    assert not list(inbox.parent.glob(".mendari-improve-*"))  # nothing left beside the pack

    # The pack is whole again: its own data gives the model it holds.
    improved = (inbox / MODEL).read_bytes()
    assert main(["train", str(inbox)]) == 0
    capsys.readouterr()
    assert (inbox / MODEL).read_bytes() == improved
    pack = load_pack(inbox)
    assert all(outcome.passed for outcome in run_pack_tests(Decision(pack)))

    # What it learned carries to messages unlike any it learned from: 369 of the draft's other
    # split, where the pack as shipped gets 322. (A draft, not clinically reviewed: this
    # measures the learning, and nothing about the pack.)
    assert held_out["candidate"] > held_out["current"]
    assert right_on_unseen(inbox) == 369

    # A second run with the same cases: what it learned from is in the pack now, and the cases
    # it held out are held out again, so there is nothing new to learn.
    second = improve(pack, [harder])
    assert second.verdict == TOO_FEW and second.report["candidates"] == []
    left_out = second.report["cases"]["left_out"]
    assert left_out["already_in_pack"] == cases["learned"] + cases["calibration"]
    assert second.report["cases"]["held_out"] == cases["held_out"]
    with pytest.raises(MendariError, match="nothing to keep"):
        apply(pack, second)
    assert (inbox / MODEL).read_bytes() == improved


def test_cases_that_teach_the_wrong_thing_are_not_kept(inbox: Path, tmp_path: Path) -> None:
    wrong = [
        {**record, "labels": {"destination": "billing"}}
        for record in load_dataset(LOCAL_DATASET).records
    ]
    found = improve(load_pack(inbox), [cases_file(tmp_path / "wrong.jsonl", wrong)])
    assert found.verdict == REGRESSED and found.report["chosen"] is None
    for candidate in found.report["candidates"]:
        assert candidate["verdict"] == REGRESSED
        # The pack's own test: 7 or more of its 600 cases lost and none gained. That is within
        # the 3% the eval gate tolerates, and more than chance.
        assert any(
            problem.startswith("test: got fewer right:") for problem in candidate["regressions"]
        )
    assert (
        "test: got fewer right: 7 lost, 0 gained (p = 0.0156)"
        in (found.report["candidates"][0]["regressions"])
    )
    with pytest.raises(MendariError, match="nothing to keep"):
        apply(load_pack(inbox), found)
    assert pack_digest(inbox) == pack_digest(INBOX_DIR)


def test_a_gain_is_not_kept_when_it_comes_with_confident_mistakes_on_emergencies(
    inbox: Path,
) -> None:
    """The local-site example, with its true labels. Words alone: every held-out case right, and
    refused, for on the pack's hand-written emergency messages it would be sure of 23 answers
    where the model in the pack is sure of 7, and wrong on 4 of them. With character features:
    7 cases gained and 2 lost, which is no more than chance."""
    found = improve(load_pack(inbox), [load_dataset(LOCAL_DATASET)])
    assert found.verdict == NOT_BETTER and found.report["chosen"] is None
    words, characters = found.report["candidates"]

    assert (words["held_out"]["current"], words["held_out"]["candidate"]) == (98, 105)
    assert words["verdict"] == REGRESSED and words["regressions"] == [
        "red_flags: confident and wrong rose by more than 3% of confident answers"
    ]
    red_flags = next(found for found in words["datasets"] if found["dataset"] == "red_flags")
    assert red_flags["confident"] == {
        "current": {"n": 7, "wrong": 0},
        "candidate": {"n": 23, "wrong": 4},
    }

    held_out = characters["held_out"]
    assert (held_out["gained"], held_out["lost"]) == (7, 2) and held_out["p"] > 0.05
    assert characters["verdict"] == NOT_BETTER and characters["regressions"] == []


def test_cases_the_pack_already_reads_well_change_nothing(inbox: Path, tmp_path: Path) -> None:
    fresh = synthetic_inbox.generate(600, site="public", seed=20261110, id_prefix="fresh")
    found = improve(load_pack(inbox), [cases_file(tmp_path / "fresh.jsonl", fresh)])
    assert found.verdict == NOT_BETTER and found.report["chosen"] is None
    for candidate in found.report["candidates"]:
        assert candidate["verdict"] == NOT_BETTER and candidate["regressions"] == []


# -- which cases it uses -------------------------------------------------------------------------


def test_too_few_cases_to_judge_build_no_candidate(
    inbox: Path, harder: Dataset, tmp_path: Path
) -> None:
    few = cases_file(tmp_path / "few.jsonl", harder.records[:40])
    found = improve(load_pack(inbox), [few])
    assert found.verdict == TOO_FEW and found.report["candidates"] == []
    assert found.report["cases"]["held_out"] < MIN_HELD_OUT
    assert f"at least {MIN_HELD_OUT}" in found.report["reason"]


def test_a_case_of_the_packs_own_data_is_never_learned_from(inbox: Path) -> None:
    own = load_dataset(INBOX_DIR / "data" / "test.jsonl")
    found = improve(load_pack(inbox), [own])
    assert found.verdict == TOO_FEW
    assert found.report["cases"]["left_out"]["already_in_pack"] == len(own.records) == 600


def test_repeated_conflicting_unlabelled_and_refused_cases_are_left_out(
    inbox: Path, harder: Dataset, tmp_path: Path
) -> None:
    one, two, three, four = (dict(record) for record in harder.records[:4])
    given = [
        one,
        {**one, "id": "again"},  # the same message, the same label: once
        two,
        {**two, "id": "disputed", "labels": {"destination": "billing"}},  # two labels: neither
        {**three, "labels": {"urgency": "routine"}},  # not labelled for the question
        {**three, "id": "unknown", "labels": {"destination": "pharmacy"}},  # not one of its labels
        {**four, "input": {"message": {"body": "x" * 20_000}}},  # longer than the schema allows
    ]
    found = improve(load_pack(inbox), [cases_file(tmp_path / "mixed.jsonl", given)])
    cases = found.report["cases"]
    assert cases["given"] == 7
    assert cases["left_out"] == {
        "not_labelled": 2,
        "refused": 1,
        "already_in_pack": 0,
        "conflicting": 2,
        "repeated": 1,
    }
    assert cases["learned"] + cases["calibration"] + cases["held_out"] == 1


def test_where_a_case_falls_does_not_depend_on_what_else_is_given(
    inbox: Path, harder: Dataset, tmp_path: Path
) -> None:
    """A case held out in one run is held out in every run, so it is never learned from later."""
    half = cases_file(tmp_path / "half.jsonl", harder.records[::2])
    whole = improve(load_pack(inbox), [harder])
    part = improve(load_pack(inbox), [half])
    assert set(part.held_out_ids) <= set(whole.held_out_ids)
    assert len(part.held_out_ids) == part.report["cases"]["held_out"]


# -- what it refuses -----------------------------------------------------------------------------


def test_a_pack_with_no_classifier_has_nothing_to_improve(harder: Dataset) -> None:
    with pytest.raises(MendariError, match="no naive_bayes"):
        improve(load_pack(ROOT / "packs" / "ai-guard"), [harder])


def test_a_slot_that_is_not_the_classifiers_is_refused(inbox: Path, harder: Dataset) -> None:
    with pytest.raises(MendariError, match="no naive_bayes variant in slot 'safety_screen'"):
        improve(load_pack(inbox), [harder], slot="safety_screen")


def test_a_pack_whose_other_slots_call_a_model_is_not_judged(inbox: Path, harder: Dataset) -> None:
    manifest = inbox / "pack.yaml"
    text = manifest.read_text()
    assert text.count("    use: rules\n") == 1
    manifest.write_text(text.replace("    use: rules\n", "    use: llm\n"))
    with pytest.raises(MendariError, match="safety_screen=llm calls a model"):
        improve(load_pack(inbox), [harder])


# -- the command ---------------------------------------------------------------------------------


def test_the_command_reports_and_writes_only_when_asked(
    inbox: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.json"
    argv = ["improve", str(inbox), "--with", str(HARDER), "--out", str(report)]
    assert main(argv) == 0
    printed = capsys.readouterr().out
    assert "Better" in printed and "held out" in printed
    assert "Nothing was written: run it again with --write to keep it." in printed
    assert json.loads(report.read_text())["verdict"] == BETTER
    assert pack_digest(inbox) == pack_digest(INBOX_DIR)

    assert main([*argv, "--write"]) == 0
    printed = capsys.readouterr().out
    assert f"wrote {MODEL}" in printed and f"wrote {TRAIN}" in printed
    assert "bump the pack's version" in printed
    assert pack_digest(inbox) != pack_digest(INBOX_DIR)

    assert main([*argv, "--write"]) == 2  # nothing new the second time: nothing kept
    printed = capsys.readouterr().out
    assert "Not kept" in printed and "wrote" not in printed


def test_the_command_prints_the_report_as_json(
    inbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    own = str(INBOX_DIR / "data" / "test.jsonl")
    assert main(["improve", str(inbox), "--with", own, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == TOO_FEW
