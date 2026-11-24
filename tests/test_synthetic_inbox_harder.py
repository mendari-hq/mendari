# SPDX-License-Identifier: Apache-2.0
"""The harder inbox benchmark's draft (`tests/support/synthetic_inbox_harder.py`), draft, not
clinically reviewed: its committed files are the generator's byte for byte, whatever the hash
seed; its leakage check passes and finds every kind of leak planted in a copy; every label,
critical label, red-flag sign and frame is covered in both splits; every case says it is a draft;
the shipped pack evaluates every case; and the hidden-set store refuses the draft's cases as
public."""

from __future__ import annotations

import ast
import copy
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from mendari import Decision, load_pack
from mendari.core import evalsets
from mendari.evals.canonical import canonical_input
from mendari.evals.harness import evaluate, record_problems, resolve_dataset
from tests.conftest import ROOT
from tests.support import harder_inbox_draft
from tests.support import synthetic_inbox_harder as harder

PACK_DIR = ROOT / "tests" / "fixtures" / "reference-packs" / harder.PACK_NAME
DRAFT_DIR = ROOT / harder.DRAFT_DIR
Splits = dict[str, list[dict[str, Any]]]


@pytest.fixture(scope="module")
def draft() -> harder.Draft:
    return harder.build(PACK_DIR)


@pytest.fixture(scope="module")
def corpus() -> harder.Corpus:
    return harder.shipped_corpus(PACK_DIR)


def _script() -> ModuleType:
    """The command that writes or checks the draft: `python -m tests.support.harder_inbox_draft`."""
    return harder_inbox_draft


# -- byte-reproducibility ---------------------------------------------------------------------


def test_the_committed_draft_is_the_generators_byte_for_byte(draft: harder.Draft) -> None:
    assert sorted(draft.files) == sorted(harder.FILES)
    for name, content in draft.files.items():
        assert (DRAFT_DIR / name).read_bytes() == content, name
    assert harder.check_files(DRAFT_DIR, draft) == []


def test_the_same_seed_writes_the_same_bytes(draft: harder.Draft) -> None:
    again = harder.build(PACK_DIR)
    assert again.files == draft.files
    assert harder.generate(harder.DEFAULT_SEED) == draft.splits


def test_the_check_passes_under_another_hash_seed() -> None:
    """Nothing the generator writes depends on the order of a set or a dict built from one:
    the script's check of the committed files passes with other string hashes."""
    for hash_seed in ("0", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        run = subprocess.run(
            [sys.executable, "-m", "tests.support.harder_inbox_draft", "--check"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        assert run.returncode == 0, run.stderr
        assert run.stdout.rstrip().endswith(": 0 problems")


def test_a_committed_file_that_differs_fails_the_check(tmp_path: Path, draft: harder.Draft) -> None:
    for name, content in draft.files.items():
        (tmp_path / name).write_bytes(content)
    assert harder.check_files(tmp_path, draft) == []
    (tmp_path / "heldout.jsonl").write_bytes(
        draft.files["heldout.jsonl"].replace(b"urgent", b"x", 1)
    )
    (tmp_path / "REPORT.md").unlink()
    problems = harder.check_files(tmp_path, draft)
    assert any("heldout.jsonl is not the generator's output" in p for p in problems)
    assert any("REPORT.md is missing" in p for p in problems)
    assert _script().main(["--check", "--out", str(tmp_path)]) == 1


def test_another_seed_draws_other_cases_from_the_same_families(corpus: harder.Corpus) -> None:
    """The separation is the construction's, not the seed's: another seed draws other cases
    from the same families, and the leakage check passes on them too."""
    base, other = harder.generate(harder.DEFAULT_SEED), harder.generate(harder.DEFAULT_SEED + 1)
    assert base != other
    for split in harder.SPLITS:
        assert len(base[split]) == len(other[split])
        assert [r["attributes"]["family"] for r in base[split]] == [
            r["attributes"]["family"] for r in other[split]
        ]
    assert harder.leakage_problems(other["train"], other["heldout"], corpus) == []


# -- the leakage check --------------------------------------------------------------------------


def test_the_leakage_check_passes_on_the_draft(draft: harder.Draft) -> None:
    assert [check.name for check in draft.leakage] == [
        "families in both splits",
        "paraphrase sources in both splits",
        "cases filed under the other split",
        "held-out text found in the train split",
        "held-out text found in the shipped pack's training and tuning data or templates",
        "train text found in the shipped pack's held-out red-flag set",
    ]
    for check in draft.leakage:
        assert check.compared > 0 and check.problems == (), check.name
    report = draft.files["REPORT.md"].decode()
    assert "Result: **passed** (0 problems)" in report


def test_the_banks_of_the_two_splits_share_nothing() -> None:
    """By construction: each slot's values, each other source's items and each family's frames
    differ between the splits, once folded."""
    for name in set(harder.SLOTS["train"]) & set(harder.SLOTS["heldout"]):
        train = {harder.fold(v) for v in harder.SLOTS["train"][name]}
        assert not train & {harder.fold(v) for v in harder.SLOTS["heldout"][name]}, name
    for source in harder.BANKS:
        if source.split == "train":
            other = harder.bank(source.kind, source.language, "heldout")
            assert not {repr(i) for i in source.items} & {repr(i) for i in other.items}, source.id
    frames = {
        s: {harder.fold(f) for x in harder.families(s) for f in x.frames} for s in harder.SPLITS
    }
    assert not frames["train"] & frames["heldout"]
    ids = [family.id for family in harder.FAMILIES]
    assert len(ids) == len(set(ids)) == len(harder.FAMILY_BY_ID)


def _copy(draft: harder.Draft) -> Splits:
    return copy.deepcopy({split: list(records) for split, records in draft.splits.items()})


def _first(splits: Splits, split: str, family: str) -> dict[str, Any]:
    return next(r for r in splits[split] if r["attributes"]["family"] == family)


def _body(record: dict[str, Any], body: str) -> None:
    record["input"]["message"]["body"] = body


def _shipped(name: str) -> str:
    """The body of the first case of the shipped pack's `data/<name>.jsonl`."""
    return str(
        harder._read_jsonl(PACK_DIR / "data" / f"{name}.jsonl")[0]["input"]["message"]["body"]
    )


def _plant_family(splits: Splits) -> str:
    """A held-out case copied into train, as it is."""
    record = copy.deepcopy(_first(splits, "heldout", "heldout.en.stroke"))
    splits["train"].append({**record, "id": "planted"})
    return "planted"


def _plant_source(splits: Splits) -> str:
    """A train case that names a held-out bank among its sources."""
    record = _first(splits, "train", "train.en.refill")
    record["attributes"]["sources"] = [*record["attributes"]["sources"], "hedge.en.heldout"]
    return str(record["id"])


def _plant_misfiled(splits: Splits) -> str:
    """A train case that says it belongs to the held-out split."""
    record = _first(splits, "train", "train.en.bill")
    record["attributes"]["split"] = "heldout"
    return str(record["id"])


def _plant_sentence(splits: Splits) -> str:
    """A held-out sentence, shouted and punctuated otherwise, inside a train case."""
    held = _first(splits, "heldout", "heldout.en.chest_pain")
    sentence = held["input"]["message"]["body"].split(".")[0]
    _body(_first(splits, "train", "train.en.symptoms"), f"Hello!! {sentence.upper()}!!! ok")
    return str(held["id"])


def _plant_run(splits: Splits) -> str:
    """Six words of a held-out sentence, and nothing else of it, inside a train case."""
    held = _first(splits, "heldout", "heldout.en.self_harm")
    words = harder.fold(held["input"]["message"]["body"]).split()[:6]
    _body(_first(splits, "train", "train.en.logistics"), f"So {' '.join(words)} and more here")
    return str(held["id"])


def _plant_shipped_training(splits: Splits) -> str:
    """A held-out case that is a case of the shipped pack's training data."""
    record = _first(splits, "heldout", "heldout.en.refill")
    _body(record, _shipped("train"))
    return str(record["id"])


def _plant_shipped_tuning(splits: Splits) -> str:
    """A held-out case that is a case of the set the pack's rules were tuned on."""
    record = _first(splits, "heldout", "heldout.en.breathing")
    _body(record, _shipped("red_flags"))
    return str(record["id"])


def _plant_template(splits: Splits) -> str:
    """A held-out case that copies a shipped template's text between its slots."""
    record = _first(splits, "heldout", "heldout.en.logistics")
    _body(record, "Hey, Is the clinic open the day after Thanksgiving? thanks")
    return str(record["id"])


def _plant_shipped_held_out(splits: Splits) -> str:
    """A train case that is a case of the shipped pack's held-out red-flag set."""
    record = _first(splits, "train", "train.en.bleeding")
    _body(record, _shipped("red_flags_holdout"))
    return str(record["id"])


PLANTED: dict[str, tuple[Callable[[Splits], str], str]] = {
    "a held-out case in train": (_plant_family, "families in both splits"),
    "a held-out source in train": (_plant_source, "paraphrase sources in both splits"),
    "a case under the other split": (_plant_misfiled, "cases filed under the other split"),
    "a held-out sentence in train": (_plant_sentence, "held-out text found in the train split"),
    "a held-out 6-word run in train": (_plant_run, "held-out text found in the train split"),
    "shipped training text in held-out": (
        _plant_shipped_training,
        "held-out text found in the shipped pack's training and tuning data or templates",
    ),
    "shipped tuning text in held-out": (
        _plant_shipped_tuning,
        "held-out text found in the shipped pack's training and tuning data or templates",
    ),
    "shipped template text in held-out": (
        _plant_template,
        "held-out text found in the shipped pack's training and tuning data or templates",
    ),
    "shipped held-out text in train": (
        _plant_shipped_held_out,
        "train text found in the shipped pack's held-out red-flag set",
    ),
}


@pytest.mark.parametrize("planted", PLANTED)
def test_the_leakage_check_finds_a_planted_leak(
    planted: str, draft: harder.Draft, corpus: harder.Corpus
) -> None:
    """The positive controls: each leak, planted in a copy of the draft, is found by the check
    meant for it, which names the case."""
    plant, name = PLANTED[planted]
    splits = _copy(draft)
    case = plant(splits)
    checks = {
        check.name: check
        for check in harder.leakage_check(splits["train"], splits["heldout"], corpus)
    }
    found = checks[name].problems
    assert found, f"{planted}: not found by {name!r}"
    if name == "families in both splits":
        assert found == ("family heldout.en.stroke has cases in both splits",)
    elif name == "paraphrase sources in both splits":
        assert found == ("source hedge.en.heldout is used by both splits",)
    else:
        assert any(problem.startswith(f"{case} ") for problem in found), found
    assert harder.leakage_problems(splits["train"], splits["heldout"], corpus)


# -- coverage -----------------------------------------------------------------------------------


def test_every_label_critical_label_sign_and_frame_is_covered(draft: harder.Draft) -> None:
    pack = load_pack(PACK_DIR)
    labels, critical = harder.pack_labels(pack.manifest)
    assert critical == [("urgency", "urgent")]
    assert draft.coverage == []
    for split in harder.SPLITS:
        records = draft.splits[split]
        for question, names in labels.items():
            assert {r["labels"][question] for r in records} == set(names), (split, question)
        urgent = [r for r in records if r["labels"]["urgency"] == "urgent"]
        assert {r["attributes"]["red_flag"] for r in urgent} == set(harder.RED_FLAGS)
        assert {r["attributes"]["language"] for r in urgent} == set(harder.LANGUAGES)
        assert all(r["labels"]["destination"] == "nurse" for r in urgent)
    for family in harder.FAMILIES:
        assert family.kind != "look-alike" or family.urgency == "routine"
        assert set(family.mimics) <= set(harder.RED_FLAGS), family.id


def _drop(splits: Splits, split: str, keep: Callable[[dict[str, Any]], bool]) -> Splits:
    return {**splits, split: [r for r in splits[split] if keep(r)]}


COVERAGE_GAPS: dict[str, tuple[Callable[[Splits], Splits], str]] = {
    "no billing in train": (
        lambda s: _drop(s, "train", lambda r: r["labels"]["destination"] != "billing"),
        "train: destination=billing has 0 cases",
    ),
    "no stroke in held-out": (
        lambda s: _drop(s, "heldout", lambda r: r["attributes"]["red_flag"] != "stroke"),
        "heldout: the sign stroke has 0 urgent cases",
    ),
    "no Spanish emergency in held-out": (
        lambda s: _drop(
            s,
            "heldout",
            lambda r: (
                not (r["labels"]["urgency"] == "urgent" and r["attributes"]["language"] == "es")
            ),
        ),
        "heldout: critical urgency=urgent has no case in es",
    ),
    "a frame never drawn": (
        lambda s: _drop(
            s,
            "train",
            lambda r: (r["attributes"]["family"], r["attributes"]["frame"]) != ("train.en.bill", 2),
        ),
        "train: family train.en.bill has no case from frames [2]",
    ),
}


@pytest.mark.parametrize("gap", COVERAGE_GAPS)
def test_the_coverage_check_finds_a_gap(gap: str, draft: harder.Draft) -> None:
    labels, critical = harder.pack_labels(load_pack(PACK_DIR).manifest)
    make, expected = COVERAGE_GAPS[gap]
    splits = make(_copy(draft))
    problems = harder.coverage_problems(splits["train"], splits["heldout"], labels, critical)
    assert any(problem.startswith(expected) for problem in problems), problems


def test_the_coverage_check_finds_a_label_the_pack_does_not_have(draft: harder.Draft) -> None:
    labels, critical = harder.pack_labels(load_pack(PACK_DIR).manifest)
    splits = _copy(draft)
    splits["heldout"][0]["labels"]["destination"] = "pharmacy"
    problems = harder.coverage_problems(splits["train"], splits["heldout"], labels, critical)
    assert "heldout: destination=pharmacy is not a label of the pack" in problems


# -- what the draft is ------------------------------------------------------------------------


def test_every_case_and_file_says_it_is_a_draft(draft: harder.Draft) -> None:
    assert harder.REVIEW_STATUS == "Draft, not clinically reviewed"
    for split in harder.SPLITS:
        assert {r["attributes"]["review"] for r in draft.splits[split]} == {harder.REVIEW_STATUS}
    for name in ("REPORT.md", "FAMILIES.md"):
        head = draft.files[name].decode().split("\n")[:4]
        assert f"**{harder.REVIEW_STATUS}.**" in "\n".join(head), name
    assert harder.REVIEW_STATUS in (harder.__doc__ or "").split("\n")[0]
    assert "draft, not clinically reviewed" in (_script().__doc__ or "").split("\n")[0]
    brief = (ROOT / "notes" / "clinician-review-harder-inbox-benchmark.md").read_text()
    assert brief.startswith("Drafted by a build session for the owner to review. Not clinically")


def test_the_shipped_pack_evaluates_every_case() -> None:
    """Each split is a dataset the pack takes as it is (`mendari eval --dataset <file>`), and
    the pack's offline variants decide every case."""
    pack = load_pack(PACK_DIR)
    for split in harder.SPLITS:
        dataset = resolve_dataset(pack, str(DRAFT_DIR / f"{split}.jsonl"))
        assert record_problems(pack.manifest, dataset) == []
        assert len(dataset.records) == len(harder.generate()[split])
    report = evaluate(Decision(pack), resolve_dataset(pack, str(DRAFT_DIR / "heldout.jsonl")))
    assert report["dataset"]["n"] == len(dataset.records)
    assert report["errors"] == {}


def test_the_hidden_set_store_refuses_the_draft_as_public(draft: harder.Draft) -> None:
    """The draft is public, under datasets/: none of its cases may become a hidden case."""
    evalsets._public_forms.cache_clear()
    try:
        exact, _folded = evalsets._public_forms()
        for split in harder.SPLITS:
            for record in draft.splits[split][::50]:
                assert evalsets._digest(canonical_input(record["input"])) in exact, record["id"]
    finally:
        evalsets._public_forms.cache_clear()


def test_the_generator_is_local() -> None:
    """It imports the standard library's text, random and data modules and Mendari only: no
    network client, no model provider."""
    path = Path(harder.__file__)
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    allowed = {
        "__future__", "hashlib", "json", "random", "re", "unicodedata", "collections",
        "collections.abc", "dataclasses", "pathlib", "typing", "mendari", "tests.support",
    }  # fmt: skip
    assert imported <= allowed, imported - allowed
