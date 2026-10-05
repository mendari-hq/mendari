"""`scripts/synthetic_cases.py`: synthetic data from templates, for pack authors who do not write
Python (docs/guides/contributing-a-pack.md)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from mendari import Decision, load_pack
from mendari.cli import COMMUNITY_GUIDE, main
from mendari.evals.harness import evaluate, resolve_dataset
from tests.conftest import ROOT

TEMPLATES = """
- input: {message: {body: "Please refill my {drug}, I have {left} left. {not-a-placeholder}"}}
  labels: {request: refill, urgent_symptoms: "no"}
  fill:
    drug: [metformin, lisinopril]
    left: [two pills, "none"]
- input: {message: {body: "Chest pain since this morning."}}
  labels: {request: other, urgent_symptoms: "yes"}
"""


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "synthetic_cases", ROOT / "scripts" / "synthetic_cases.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_templates_expand_in_the_order_written(tmp_path: Path) -> None:
    source = tmp_path / "cases.yaml"
    source.write_text(TEMPLATES)
    out = tmp_path / "examples.jsonl"
    assert _script().main([str(source), "--out", str(out), "--prefix", "ex"]) == 0
    cases = [json.loads(line) for line in out.read_text().splitlines()]
    assert [case["id"] for case in cases] == ["ex-0001", "ex-0002", "ex-0003", "ex-0004", "ex-0005"]
    assert [case["input"]["message"]["body"] for case in cases[:2]] == [
        "Please refill my metformin, I have two pills left. {not-a-placeholder}",
        "Please refill my metformin, I have none left. {not-a-placeholder}",
    ]
    assert cases[4] == {
        "id": "ex-0005",
        "input": {"message": {"body": "Chest pain since this morning."}},
        "labels": {"request": "other", "urgent_symptoms": "yes"},
    }


@pytest.mark.parametrize(
    ("templates", "problem"),
    [
        ("{input: {}}", "must be a list of templates"),
        ("- labels: {a: b}", "needs an `input` mapping"),
        ("- {input: {m: x}, label: {a: b}}", "unknown keys ['label']"),
        ("- {input: {m: x}, fill: {drug: [a]}}", "the input never uses {drug}"),
        ("- {input: {m: '{d}'}, fill: {d: []}}", "`fill` must map each placeholder"),
    ],
)
def test_a_template_that_cannot_be_expanded_says_what_to_fix(
    tmp_path: Path, templates: str, problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "cases.yaml"
    source.write_text(templates)
    assert _script().main([str(source), "--out", str(tmp_path / "x.jsonl")]) == 1
    assert problem in capsys.readouterr().err


def test_too_many_cases_are_refused() -> None:
    script = _script()
    many = [{"input": {"m": "{a}{b}"}, "fill": {"a": list(range(100)), "b": list(range(100))}}]
    with pytest.raises(script.TemplateError, match="more than 5000 cases"):
        list(script.expand(many))


def test_the_output_is_a_dataset_the_scaffold_evaluates_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guide's flow: `mendari new --community`, templates in, the pack's dataset out."""
    pack_dir = tmp_path / "refill-router"
    assert main(["new", str(pack_dir), "--community", "--publisher", "Test Clinic"]) == 0
    assert f"Contributing it: {COMMUNITY_GUIDE}" in capsys.readouterr().out
    source = pack_dir / "data" / "cases.yaml"
    source.write_text(TEMPLATES)
    out = pack_dir / "data" / "examples.jsonl"
    assert _script().main([str(source), "--out", str(out), "--prefix", "ex"]) == 0
    pack = load_pack(pack_dir)
    dataset = resolve_dataset(pack, "examples")
    assert len(dataset.records) == 5
    report = evaluate(Decision(pack), dataset)
    assert report["dataset"]["n"] == 5
