# SPDX-License-Identifier: Apache-2.0
"""Dataset checks (LP-20c, `evals/dataset_checks.py`, `mendari data check`): schema and label
problems by case id, each label's count with its exact interval, duplicates under both of
LP-11's forms, overlap with the pack's training data; ids and counts, never an input."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from scipy.stats import beta

from mendari.cli import main
from mendari.core.decision import Decision
from mendari.core.pack import Pack, load_pack
from mendari.core.reference import bundled_dir
from mendari.evals import canonical, dataset_checks
from mendari.evals.dataset_checks import (
    MIN_CASES,
    check,
    duplicates,
    label_counts,
    label_problems,
    schema_problems,
    training_overlap,
)
from mendari.evals.harness import Dataset, load_dataset, resolve_dataset
from tests.conftest import INBOX_DIR, ROOT
from tests.test_canonical import COMBINING_ACUTE, E_ACUTE, ELLIPSIS, EM_DASH, wide

SECRET = "PLANTED-CASE-TEXT-5519"
INBOX = load_pack(INBOX_DIR)
EXAMPLES = {
    "patient-inbox-router": "local-site-example.jsonl",
    "ai-guard": "guard-local-site-example.jsonl",
    "prior-auth-preflight": "prior-auth-local-site-example.jsonl",
    "referral-router": "referral-local-site-example.jsonl",
    "clinical-trial-prescreener": "trial-local-site-example.jsonl",
}


def _case(id: str, body: str, **labels: str) -> dict[str, Any]:
    return {"id": id, "input": {"message": {"body": body}}, "labels": labels}


def _dataset(records: list[dict[str, Any]], name: str = "planted") -> Dataset:
    raw = "".join(json.dumps(record) + "\n" for record in records).encode()
    return Dataset(name=name, path=Path(f"{name}.jsonl"), records=records, sha256=raw.hex()[:64])


def test_a_case_that_breaks_the_schema_is_named_by_id_and_never_by_text() -> None:
    records = [
        _case("ok-1", "I need a refill.", destination="front_desk", urgency="routine"),
        {"id": "bad-1", "input": {"message": SECRET}, "labels": {}},
        {"id": "bad-2", "input": {"message": {"body": "x", "other": SECRET}}, "labels": {}},
    ]
    found = schema_problems(Decision(INBOX), records)
    assert [problem["case"] for problem in found] == ["bad-1", "bad-2"]
    report = check(INBOX, _dataset(records), training={})
    assert not report.passed and report.data["schema_problems"]["count"] == 2
    assert SECRET not in json.dumps(report.data)


def test_a_label_outside_the_declared_answers_is_a_problem() -> None:
    records = [
        _case("c1", "A refill please.", destination="front_desk"),
        _case("c2", "A refill please, again.", destination=SECRET),
        {**_case("c3", "Billing question."), "labels": {"no_such_question": "x"}},
        _case("c1", "Same id as the first."),
    ]
    found = label_problems(INBOX.manifest, records)
    assert [problem["case"] for problem in found] == ["c2", "c3", "c1"]
    assert "destination is not one of its labels" in found[0]["reason"]
    assert "another case has the same id" in found[2]["reason"]
    report = check(INBOX, _dataset(records), training={})
    assert not report.passed and SECRET not in json.dumps(report.data)


def test_each_label_has_its_count_and_exact_interval() -> None:
    """Against scipy's beta quantiles: Clopper-Pearson, as every other figure."""
    records = [_case(f"p{i}", f"refill {i}", destination="front_desk") for i in range(7)] + [
        _case(f"n{i}", f"nurse {i}", destination="nurse") for i in range(13)
    ]
    counts = label_counts(INBOX.manifest, records)["destination"]
    assert counts["labelled"] == 20
    for label, k in (("front_desk", 7), ("nurse", 13), ("billing", 0)):
        share = counts["labels"][label]["share"]
        assert (share["k"], share["n"]) == (k, 20)
        low = 0.0 if k == 0 else beta.ppf(0.025, k, 20 - k + 1)
        high = 1.0 if k == 20 else beta.ppf(0.975, k + 1, 20 - k)
        assert share["low"] == pytest.approx(low, abs=1e-6)
        assert share["high"] == pytest.approx(high, abs=1e-6)
    assert label_counts(INBOX.manifest, records)["urgency"]["labelled"] == 0


def test_a_thin_label_is_marked_too_few_and_is_not_refused() -> None:
    records = [_case(f"c{i}", f"refill {i}", destination="front_desk") for i in range(MIN_CASES)]
    records.append(_case("lone", "a bill", destination="billing"))
    report = check(INBOX, _dataset(records), training={})
    labels = report.data["labels"]["destination"]["labels"]
    assert labels["front_desk"]["too_few"] is False
    assert labels["billing"]["too_few"] is True and labels["billing"]["share"]["k"] == 1
    assert report.passed
    assert "billing: 1 of 101 [" in dataset_checks.text(report)
    assert "too few to estimate" in dataset_checks.text(report)


def test_duplicates_are_found_exactly_and_under_norm_v1() -> None:
    """The vectors `tests/test_canonical.py` pins: the same message in other case, spacing,
    punctuation, full-width letters and Unicode form is one case under `norm/v1`, and only the
    byte-identical copy is one under the strict form."""
    records = [
        _case("a", "Chest pain, since Monday!"),
        _case("b", "chest   PAIN since monday"),
        _case("c", f"{wide('Chest pain')} {EM_DASH} since Monday{ELLIPSIS}"),
        _case("d", "Chest pain, since Monday!"),
        _case("e", "caf" + E_ACUTE),
        _case("f", "cafe" + COMBINING_ACUTE),
        _case("g", "take 5 mg"),
        _case("h", "take 50 mg"),
    ]
    found = duplicates(records)
    assert found["exact"] == [["a", "d"]]
    assert found["norm/v1"] == [["a", "b", "c", "d"], ["e", "f"]]
    report = check(INBOX, _dataset(records), training={})
    assert report.passed and report.data["duplicates"]["norm/v1"]["count"] == 2


def test_the_checks_use_lp11s_canonical_functions() -> None:
    """The module imports `canonical_input` and `normalised_input` and defines no form of its
    own: one rule for what makes two cases the same, as LP-11 asks."""
    assert dataset_checks.canonical_input is canonical.canonical_input
    assert dataset_checks.normalised_input is canonical.normalised_input
    tree = ast.parse(Path(dataset_checks.__file__).read_text())
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not defined & {"canonical_input", "normalised_input", "fold", "canonical"}
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"canonical_input", "normalised_input"} <= calls | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert "unicodedata" not in Path(dataset_checks.__file__).read_text()


def test_overlap_with_the_packs_training_data_is_found() -> None:
    train = resolve_dataset(INBOX, "train").records
    copied = {**train[0], "id": "copy"}
    reworded = {
        **train[1],
        "id": "reworded",
        "input": _shouted(train[1]["input"]),
    }
    fresh = _case("fresh", f"{SECRET} is nowhere in the training data")
    records = [copied, reworded, fresh]
    found = training_overlap(records, train)
    assert found == {"exact": ["copy"], "norm/v1": ["copy", "reworded"]}
    report = check(INBOX, _dataset(records))  # the pack's own `train` and `calibration`
    assert report.data["training"] == ["calibration", "train"]
    assert report.data["training_overlap"]["norm/v1"]["listed"] == ["copy", "reworded"]
    assert report.passed  # overlap is reported, never a failure
    # A set is not compared with itself.
    assert "train" not in check(INBOX, resolve_dataset(INBOX, "train")).data["training"]


def _shouted(value: dict[str, Any]) -> dict[str, Any]:
    """An input with its message's body in capitals and a full stop added: the same case
    under norm/v1, another under the strict form."""
    copied = json.loads(json.dumps(value))
    copied["message"]["body"] = copied["message"]["body"].upper() + "."
    return copied


def _shipped() -> list[tuple[Pack, Dataset]]:
    root = bundled_dir()
    assert root is not None
    found = []
    for folder in sorted(root.iterdir()):
        if not (folder / "pack.yaml").is_file():
            continue
        pack = load_pack(folder)
        for name in pack.manifest.evaluation.datasets:
            found.append((pack, resolve_dataset(pack, name)))
        example = ROOT / "tests" / "fixtures" / "datasets" / EXAMPLES[folder.name]
        found.append((pack, load_dataset(example)))
    return found


def test_every_shipped_dataset_is_checked_and_the_report_holds_no_input() -> None:
    shipped = _shipped()
    assert len({pack.id for pack, _ in shipped}) == 5
    for pack, dataset in shipped:
        report = check(pack, dataset)
        assert report.passed, (pack.id, dataset.name, report.data["schema_problems"])
        assert report.data["cases"] == len(dataset.records)
        served = json.dumps(report.data) + dataset_checks.text(report)
        texts = {
            value
            for record in dataset.records
            for value in _strings(record["input"])
            if len(value) > 12
        }
        assert not [value for value in texts if value in served], (pack.id, dataset.name)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def test_the_command_exits_1_on_a_schema_problem_and_0_on_duplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    duplicated = tmp_path / "duplicated.jsonl"
    case = _case("a", "A refill please.", destination="front_desk", urgency="routine")
    duplicated.write_text(json.dumps(case) + "\n" + json.dumps({**case, "id": "b"}) + "\n")
    assert main(["data", "check", str(INBOX_DIR), str(duplicated)]) == 0
    out = capsys.readouterr().out
    assert "duplicates (exact): 1 group(s)" in out and "  a, b" in out and out.endswith("passed\n")

    broken = tmp_path / "broken.jsonl"
    broken.write_text(json.dumps({"id": "x", "input": {"message": SECRET}, "labels": {}}) + "\n")
    assert main(["data", "check", str(INBOX_DIR), str(broken), "--json"]) == 1
    printed = capsys.readouterr().out
    reports = json.loads(printed)
    assert [r["dataset"] for r in reports] == ["broken"]
    assert reports[0]["schema_problems"]["listed"][0]["case"] == "x"
    assert SECRET not in printed

    # Every declared dataset when no file is named.
    assert main(["data", "check", str(INBOX_DIR), "--json"]) == 0
    names = [r["dataset"] for r in json.loads(capsys.readouterr().out)]
    assert names == list(INBOX.manifest.evaluation.datasets)
