# SPDX-License-Identifier: Apache-2.0
"""`mendari improve` (`evals/improve.py`): a pack's classifier learns from new labelled cases, and
what it learned is kept only when it is better on cases it never saw and no worse on everything
the pack already measures. Nothing is written unless asked, and a pack it wrote to is one
`mendari train` rebuilds byte for byte.

The new cases here are the harder inbox draft's train split (436 synthetic messages, a draft that
is not clinically reviewed): they stand in for what a site's reviewers would label."""

from __future__ import annotations

import json
import os
import shutil
import stat
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
    not_writable,
)
from mendari.evals.pack_tests import run_pack_tests
from mendari.hub import client as hub_client
from mendari.sandbox import synthetic_inbox
from tests.conftest import INBOX_DIR, LOCAL_DATASET, ROOT

HARDER = ROOT / "datasets" / "drafts" / "patient-inbox-harder" / "train.jsonl"
# The draft's other split: 436 messages that share no family, paraphrase or sentence with HARDER.
UNSEEN = ROOT / "datasets" / "drafts" / "patient-inbox-harder" / "heldout.jsonl"
MODEL = "models/destination-nb.json"
TRAIN = "data/train.jsonl"
CALIBRATION = "data/calibration.jsonl"
WRITTEN = (MODEL, TRAIN, CALIBRATION)
NOT_KEPT = 3  # the command's exit code when nothing is better


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


def held(root: Path) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in WRITTEN}


def right_on_unseen(root: Path) -> int:
    """How many of the draft's other split the pack at `root` sends to the right team."""
    decision = Decision.from_path(root)
    return sum(
        decision.decide(case["input"]).answers["destination"].label == case["labels"]["destination"]
        for case in load_dataset(UNSEEN).records
    )


def retold(record: dict[str, Any], suffix: str) -> dict[str, Any]:
    """The same message as another record of it: another id and channel, other capitals and
    spacing. The classifier reads it alike."""
    message = record["input"]["message"]
    channel = "email" if message.get("channel") != "email" else "portal"
    again = {**message, "body": f"  {message['body'].upper()} ", "channel": channel}
    return {
        **record,
        "id": f"{record['id']}-{suffix}",
        "input": {**record["input"], "message": again},
    }


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
    assert (cases["learned"], cases["calibration"], cases["held_out"]) == (216, 70, 145)
    assert cases["left_out"]["repeated"] == 5  # five of the draft's messages read like another
    assert 216 + 70 + 145 + sum(cases["left_out"].values()) == 436
    assert cases["held_out"] >= MIN_HELD_OUT

    chosen = report["candidates"][report["chosen"]]
    assert chosen["verdict"] == BETTER and chosen["regressions"] == []
    held_out = chosen["held_out"]
    assert held_out["n"] == cases["held_out"]
    assert held_out["gained"] > held_out["lost"] and held_out["p"] < 0.05
    assert held_out["candidate"] - held_out["current"] == held_out["gained"] - held_out["lost"]
    # The model in the pack gets 102 of the 145 right; with the cases learned, 138 (words alone)
    # or 136 (with character features): both are better, and the one that gains more is chosen.
    assert chosen["features"] is None
    assert (held_out["current"], held_out["candidate"]) == (102, 138)
    assert (held_out["gained"], held_out["lost"]) == (37, 1)
    assert [found["held_out"]["candidate"] for found in report["candidates"]] == [138, 136]
    assert chosen["tests"]["failed"] == 0 and chosen["tests"]["passed"] > 0

    # And it is sure of itself less often when it is wrong: 1 of 116 confident answers on the
    # held-out cases, where the model in the pack is wrong on 10 of 71.
    confident = next(d["confident"] for d in chosen["datasets"] if d["dataset"] == "held out")
    assert confident["current"] == {"n": 71, "wrong": 10}
    assert confident["candidate"] == {"n": 116, "wrong": 1}
    assert (confident["newly_wrong"], confident["no_longer_wrong"]) == (1, 10)


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
            assert compared["paired"]["n"] <= compared["cases"]
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
    assert pack_digest(root) == SHIPPED


def test_the_same_pack_and_cases_give_the_same_report(
    inbox: Path, harder: Dataset, learned: tuple[Path, Improvement]
) -> None:
    _, found = learned
    again = improve(load_pack(inbox), [harder])
    assert again.report == found.report


# -- which cases it uses -------------------------------------------------------------------------


def test_a_message_received_again_is_one_case(
    inbox: Path, harder: Dataset, learned: tuple[Path, Improvement], tmp_path: Path
) -> None:
    """Each message twice more, as another record of it: another id, channel, capitals and
    spacing. Counted as three cases, a repeat would fall on both sides of the split and the
    model would be judged on messages it had learned (given three times, every held-out case
    came out right). It is one case: the run is the run on each message once."""
    _, once = learned
    thrice = [*harder.records]
    thrice += [retold(record, "again") for record in harder.records]
    thrice += [retold(retold(record, "x"), "y") for record in harder.records]
    found = improve(load_pack(inbox), [cases_file(tmp_path / "thrice.jsonl", thrice)])
    cases = found.report["cases"]
    assert cases["given"] == 3 * 436 and cases["left_out"]["repeated"] == 2 * 436 + 5
    assert (cases["learned"], cases["calibration"], cases["held_out"]) == (216, 70, 145)
    assert [c["held_out"] for c in found.report["candidates"]] == [
        c["held_out"] for c in once.report["candidates"]
    ]


def test_a_message_the_pack_holds_is_known_whatever_else_its_record_says(inbox: Path) -> None:
    own = load_dataset(INBOX_DIR / "data" / "test.jsonl")
    assert improve(load_pack(inbox), [own]).report["cases"]["left_out"]["already_in_pack"] == 600

    retyped = [retold(record, "retold") for record in own.records]
    found = improve(load_pack(inbox), [cases_file(inbox.parent / "retold.jsonl", retyped)])
    assert found.verdict == TOO_FEW and found.report["reason"] == "no new case to learn from"
    assert found.report["cases"]["left_out"]["already_in_pack"] == 600


def test_too_few_cases_to_judge_build_no_candidate(
    inbox: Path, harder: Dataset, tmp_path: Path
) -> None:
    few = cases_file(tmp_path / "few.jsonl", harder.records[:40])
    found = improve(load_pack(inbox), [few])
    assert found.verdict == TOO_FEW and found.report["candidates"] == []
    assert found.report["cases"]["held_out"] < MIN_HELD_OUT
    assert f"at least {MIN_HELD_OUT}" in found.report["reason"]


def test_cases_it_cannot_use_are_left_out_and_counted(
    inbox: Path, harder: Dataset, tmp_path: Path
) -> None:
    one, two, three, four, five = (dict(record) for record in harder.records[:5])
    assert two["labels"]["destination"] != "billing"
    given = [
        one,
        {**one, "id": "again"},  # the same message, the same label: once
        retold(one, "retold"),  # and read alike, however its record differs
        two,
        {**two, "id": "disputed", "labels": {"destination": "billing"}},  # two labels: neither
        {**three, "labels": {"urgency": "routine"}},  # not labelled for the question
        {**three, "id": "unknown", "labels": {"destination": "pharmacy"}},  # not one of its labels
        {**four, "input": {"message": {"body": "x" * 20_000}}},  # longer than the schema allows
        {**five, "input": {"message": {"body": "?!"}}},  # no word in it
    ]
    found = improve(load_pack(inbox), [cases_file(tmp_path / "mixed.jsonl", given)])
    cases = found.report["cases"]
    assert cases["given"] == 9
    assert cases["left_out"] == {
        "not_labelled": 2,
        "refused": 1,
        "nothing_to_read": 1,
        "already_in_pack": 0,
        "conflicting": 2,
        "repeated": 2,
    }
    assert cases["learned"] + cases["calibration"] + cases["held_out"] == 1


def test_where_a_case_falls_does_not_depend_on_what_else_is_given(
    inbox: Path, harder: Dataset, learned: tuple[Path, Improvement], tmp_path: Path
) -> None:
    """A case held out in one run is held out in every run, so it is never learned from later:
    not with fewer cases beside it, and not as another record of the same message."""
    _, whole = learned
    half = cases_file(tmp_path / "half.jsonl", harder.records[::2])
    part = improve(load_pack(inbox), [half])
    assert set(part.held_out_ids) <= set(whole.held_out_ids)
    assert len(part.held_out_ids) >= part.report["cases"]["held_out"] >= MIN_HELD_OUT

    retyped = [retold(record, "retold") for record in harder.records]
    other = improve(load_pack(inbox), [cases_file(tmp_path / "retold.jsonl", retyped)])
    assert {held_out.removesuffix("-retold") for held_out in other.held_out_ids} == set(
        whole.held_out_ids
    )


# -- keeping what was learned ------------------------------------------------------------------


def test_a_pack_it_wrote_to_is_one_train_rebuilds_and_a_second_run_learns_nothing_new(
    inbox: Path, harder: Dataset, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reviewed = [
        {**record, "attributes": {"source": "review", "decision_id": f"decision-{number}"}}
        for number, record in enumerate(harder.records)
    ]
    cases_given = cases_file(tmp_path / "reviewed.jsonl", reviewed)
    before = held(inbox)
    for name in (TRAIN, MODEL):
        (inbox / name).chmod(0o600)  # as a site may keep a pack that holds its cases
    assert right_on_unseen(inbox) == 322  # of 436, as shipped
    found = improve(load_pack(inbox), [cases_given])
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
        assert (inbox / name).read_bytes().startswith(before[name])  # added to, nothing changed
        added = lines(inbox / name)[len(before[name].splitlines()) :]
        assert len(added) == count
        assert all(record["id"].startswith("learned-") for record in added)
        assert all(set(record) == {"id", "input", "labels", "attributes"} for record in added)
        # Each says a run added it: what keeps the pack out of an archive (`core/learned.py`).
        assert all(record["attributes"]["added_by"] == "mendari improve" for record in added)
        # Where each came from, and the decision it was, to find it again (an erasure).
        assert all(record["attributes"]["source"] == "review" for record in added)
        assert all(record["attributes"]["decision_id"].startswith("decision-") for record in added)
    for name, mode in ((TRAIN, 0o600), (MODEL, 0o600), (CALIBRATION, 0o644)):
        assert stat.S_IMODE((inbox / name).stat().st_mode) == mode  # each file's own, kept

    # The run is kept once: the pack it judged is not the pack on disk any more.
    improved = held(inbox)
    with pytest.raises(MendariError, match="the pack has changed since this run judged it"):
        apply(load_pack(inbox), found)
    assert held(inbox) == improved
    assert not list(inbox.parent.glob(".mendari-improve-*"))  # nothing left beside the pack

    # The pack is whole again: its own data gives the model it holds.
    assert main(["train", str(inbox)]) == 0
    capsys.readouterr()
    assert held(inbox) == improved
    pack = load_pack(inbox)
    assert all(outcome.passed for outcome in run_pack_tests(Decision(pack)))

    # What it learned carries to messages unlike any it learned from: 354 of the draft's other
    # split, where the pack as shipped gets 322. (A draft, not clinically reviewed: this
    # measures the learning, and nothing about the pack.)
    assert held_out["candidate"] > held_out["current"]
    assert right_on_unseen(inbox) == 354

    # A second run with the same cases: what it learned from is in the pack now, and the cases
    # it held out are held out again, so there is nothing new to learn.
    second = improve(pack, [cases_given])
    assert second.verdict == TOO_FEW and second.report["candidates"] == []
    again = second.report["cases"]
    assert (again["learned"], again["calibration"], again["held_out"]) == (0, 0, 145)
    # Every record of a case it learned from or calibrated on, a repeat of one too.
    assert again["left_out"]["already_in_pack"] >= cases["learned"] + cases["calibration"]
    assert again["left_out"]["already_in_pack"] + again["left_out"]["repeated"] + 145 == 436
    with pytest.raises(MendariError, match="nothing to keep"):
        apply(pack, second)
    assert held(inbox) == improved


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="needs a directory it cannot write"
)
def test_a_run_that_cannot_be_kept_whole_is_not_kept_at_all(
    learned: tuple[Path, Improvement], inbox: Path
) -> None:
    """The model cannot be written. The two datasets, moved into place before it, are put back."""
    _, found = learned
    before = held(inbox)
    models = inbox / "models"
    models.chmod(0o555)
    try:
        with pytest.raises(MendariError, match=r"destination-nb\.json could not be written"):
            apply(load_pack(inbox), found)
    finally:
        models.chmod(0o755)
    assert held(inbox) == before and pack_digest(inbox) == SHIPPED
    assert not list(inbox.parent.glob(".mendari-improve-*"))
    apply(load_pack(inbox), found)  # as it was, so the same run can still be kept
    assert held(inbox) != before


def test_cases_that_teach_the_wrong_thing_are_not_kept(inbox: Path, tmp_path: Path) -> None:
    wrong = [
        {**record, "labels": {"destination": "billing"}}
        for record in load_dataset(LOCAL_DATASET).records
    ]
    found = improve(load_pack(inbox), [cases_file(tmp_path / "wrong.jsonl", wrong)])
    assert found.verdict == REGRESSED and found.report["chosen"] is None
    for candidate in found.report["candidates"]:
        assert candidate["verdict"] == REGRESSED
        assert candidate["held_out"]["gained"] > candidate["held_out"]["lost"]  # by those labels
        assert any(
            problem.startswith("test: got fewer right:") for problem in candidate["regressions"]
        )
    # The pack's own test: 6 of its 600 cases lost and none gained. That is within the 3% the
    # eval gate tolerates, and more than chance.
    assert (
        "test: got fewer right: 6 lost, 0 gained (p = 0.0312)"
        in (found.report["candidates"][0]["regressions"])
    )
    with pytest.raises(MendariError, match="nothing to keep"):
        apply(load_pack(inbox), found)
    assert pack_digest(inbox) == SHIPPED


def test_a_candidate_surer_of_wrong_answers_about_emergencies_is_not_kept(inbox: Path) -> None:
    """The local-site example, with its true labels. Words alone: on the pack's hand-written
    emergency messages it would be sure of 23 answers where the model in the pack is sure of 7,
    and wrong on 4 of them. And neither candidate's gain (3 of 99 held-out cases, none lost) is
    more than chance. Nothing is kept."""
    found = improve(load_pack(inbox), [load_dataset(LOCAL_DATASET)])
    assert found.verdict == NOT_BETTER and found.report["chosen"] is None
    cases = found.report["cases"]
    # 45 of its 400 messages read like one the pack's own data holds, and 10 like another of it.
    assert (cases["left_out"]["already_in_pack"], cases["left_out"]["repeated"]) == (45, 10)
    words, characters = found.report["candidates"]

    assert (words["held_out"]["current"], words["held_out"]["candidate"]) == (95, 98)
    assert words["verdict"] == REGRESSED and words["regressions"] == [
        "red_flags: confident and wrong more often: 4 of 23 confident answers (the model in "
        "the pack: 0 of 7)"
    ]
    red_flags = next(found for found in words["datasets"] if found["dataset"] == "red_flags")
    assert red_flags["confident"]["current"] == {"n": 7, "wrong": 0}
    assert red_flags["confident"]["candidate"] == {"n": 23, "wrong": 4}

    held_out = characters["held_out"]
    assert (held_out["gained"], held_out["lost"]) == (3, 0) and held_out["p"] == 0.25
    assert characters["verdict"] == NOT_BETTER and characters["regressions"] == []


def test_cases_the_pack_already_reads_well_change_nothing(inbox: Path, tmp_path: Path) -> None:
    fresh = synthetic_inbox.generate(600, site="public", seed=20261110, id_prefix="fresh")
    found = improve(load_pack(inbox), [cases_file(tmp_path / "fresh.jsonl", fresh)])
    assert found.report["cases"]["held_out"] >= MIN_HELD_OUT
    assert found.verdict == NOT_BETTER and found.report["chosen"] is None
    for candidate in found.report["candidates"]:
        assert candidate["verdict"] == NOT_BETTER and candidate["regressions"] == []


def test_a_pack_with_a_second_classifier_still_trains_after_a_run_is_kept(
    inbox: Path, harder: Dataset, capsys: pytest.CaptureFixture[str]
) -> None:
    """What a run learned labels one question. A classifier for another question, trained on the
    same dataset, does not count those cases among its examples."""
    manifest = inbox / "pack.yaml"
    after = "        config: logic/red_flags.yaml\n      rules_and_jev:\n"
    text = manifest.read_text()
    assert text.count(after) == 1
    second = (
        "        config: logic/red_flags.yaml\n"
        "      classifier:\n"
        "        type: naive_bayes\n"
        "        model_file: models/urgency-nb.json\n"
        "        text_fields: [message.subject, message.body]\n"
        "      rules_and_jev:\n"
    )
    manifest.write_text(text.replace(after, second))
    assert main(["train", str(inbox)]) == 0
    urgency = (inbox / "models" / "urgency-nb.json").read_bytes()

    with pytest.raises(MendariError, match="several classifiers"):
        improve(load_pack(inbox), [harder])
    apply(load_pack(inbox), improve(load_pack(inbox), [harder], slot="router"))
    router = (inbox / MODEL).read_bytes()

    assert main(["train", str(inbox)]) == 0
    capsys.readouterr()
    assert (inbox / MODEL).read_bytes() == router
    assert (inbox / "models" / "urgency-nb.json").read_bytes() == urgency


# -- where it never writes -----------------------------------------------------------------------


def test_a_run_is_kept_only_in_a_pack_of_ones_own(
    learned: tuple[Path, Improvement], inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not among the packs this release bundles, not in the repository's community packs, not in
    the registry's cache: by where the pack is on disk, whatever it was loaded as."""
    _, found = learned
    assert not_writable(load_pack(inbox)) is None
    places = {
        "a reference pack of this release": lambda: monkeypatch.setattr(
            registry, "_BUNDLED_DIRS", [inbox.parent]
        ),
        "a community pack of this repository": lambda: monkeypatch.setattr(
            registry, "_COMMUNITY_DIR", inbox.parent
        ),
        "a pack pulled from a registry": lambda: monkeypatch.setattr(
            hub_client, "packs_cache", lambda: inbox.parent
        ),
    }
    for what, stand_there in places.items():
        with monkeypatch.context() as monkeypatch:
            stand_there()
            assert registry.origin_of(inbox) != "local"
            with pytest.raises(MendariError, match=f"nothing to keep: {what} is not changed"):
                apply(load_pack(inbox), found)  # loaded as a local pack, and still refused
    with pytest.raises(MendariError, match="a pack pulled from a registry is not changed"):
        apply(load_pack(inbox, origin="hub"), found)  # and as it was loaded, wherever it is
    assert pack_digest(inbox) == SHIPPED


def test_a_pack_is_known_by_the_directory_it_is_in_however_its_path_is_spelt(
    learned: tuple[Path, Improvement], inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a volume that ignores case, PACKS/ is packs/. A path spelt another way is not a way
    around: the directory is compared by the file it is."""
    _, found = learned
    respelt = Path(str(inbox.parent).swapcase()) / inbox.name
    if not respelt.is_dir():
        pytest.skip("this volume tells the two spellings apart")
    monkeypatch.setattr(registry, "_BUNDLED_DIRS", [inbox.parent])
    assert registry.origin_of(respelt) == "maintained"
    with pytest.raises(MendariError, match="a reference pack of this release is not changed"):
        apply(load_pack(respelt), found)
    assert pack_digest(inbox) == SHIPPED


def test_the_command_says_so_before_it_runs(
    inbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(registry, "_BUNDLED_DIRS", [inbox.parent])
    assert main(["improve", str(inbox), "--with", str(HARDER), "--write"]) == 1
    printed = capsys.readouterr()
    assert "--write: a reference pack of this release is not changed in place" in printed.err
    assert printed.out == ""  # no run was made
    assert pack_digest(inbox) == SHIPPED


# -- what it refuses, and what does not stop it --------------------------------------------------


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


def test_a_model_is_calibrated_on_cases_it_did_not_learn_from(inbox: Path, harder: Dataset) -> None:
    with pytest.raises(MendariError, match="the training and calibration datasets are two"):
        improve(load_pack(inbox), [harder], train="train", calibration="train")


def test_a_model_file_named_by_a_path_out_of_the_pack_is_never_written(
    inbox: Path, harder: Dataset
) -> None:
    """A report-only run writes nothing, whatever a manifest names: not through a path that
    leaves the pack and comes back to its model."""
    manifest = inbox / "pack.yaml"
    absolute = str((inbox / MODEL).resolve())
    manifest.write_text(
        manifest.read_text().replace(f"model_file: {MODEL}", f"model_file: {absolute}")
    )
    before = held(inbox)
    with pytest.raises(MendariError):
        improve(load_pack(inbox), [harder])
    assert held(inbox) == before


@pytest.mark.skipif(os.name == "nt", reason="needs a symbolic link and a read-only file")
def test_what_else_is_in_the_packs_directory_does_not_stop_a_run(
    inbox: Path, harder: Dataset, learned: tuple[Path, Improvement]
) -> None:
    """A virtual environment with a link that leads nowhere, an editor's folder, a model file
    that is read-only: a run copies the files that make up the pack, and nothing else."""
    _, found = learned
    (inbox / ".venv" / "bin").mkdir(parents=True)
    (inbox / ".venv" / "bin" / "python").symlink_to(inbox / "nowhere")
    (inbox / "models" / "__pycache__").mkdir()
    (inbox / MODEL).chmod(0o444)
    assert improve(load_pack(inbox), [harder]).report == found.report


# -- the command ---------------------------------------------------------------------------------


def test_the_command_reports_and_writes_only_when_asked(
    inbox: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.json"
    argv = ["improve", str(inbox), "--with", str(HARDER), "--out", str(report)]
    assert main(argv) == 0
    printed = capsys.readouterr().out
    assert "Better: words alone." in printed
    assert "138 of 145 held out right (now 102): 37 gained, 1 lost" in printed
    assert "Nothing was written: run it again with --write to keep it." in printed
    assert json.loads(report.read_text())["verdict"] == BETTER
    assert pack_digest(inbox) == SHIPPED

    assert main([*argv, "--write"]) == 0
    printed = capsys.readouterr().out
    assert f"wrote {MODEL}" in printed and f"wrote {TRAIN}" in printed
    assert "bump the pack's version" in printed
    assert pack_digest(inbox) != SHIPPED

    assert main([*argv, "--write"]) == NOT_KEPT  # nothing new the second time: nothing kept
    printed = capsys.readouterr().out
    assert "Not kept: no new case to learn from." in printed and "wrote" not in printed


def test_the_command_prints_the_report_as_json(
    inbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    own = str(INBOX_DIR / "data" / "test.jsonl")
    assert main(["improve", str(inbox), "--with", own, "--json"]) == NOT_KEPT
    assert json.loads(capsys.readouterr().out)["verdict"] == TOO_FEW
