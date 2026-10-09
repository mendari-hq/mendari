# SPDX-License-Identifier: Apache-2.0
"""The coverage report (`mendari.coverage/1`, LP-11 requirement 8, `evals/coverage.py`) and
`mendari data coverage` over any JSONL file: cases, distinct cases under `norm/v1`, distinct
positives per critical label with the warning under 100, duplicates whose labels disagree, and
the language, source and stratum mix, with a missing value counted as `unknown`. It never prints
a case's text; it lists duplicate ids only when asked. Datasets are written here, synthetic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mendari.cli import main
from mendari.core.registry import resolve_pack
from mendari.errors import MendariError
from mendari.evals import load_dataset, resolve_dataset
from mendari.evals.coverage import FORMAT, coverage

PACK = "mendari/ai-guard"  # its critical labels: verdict=block, and three more


def written(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "set.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def case(text: str, verdict: str | None, **attributes: str) -> dict[str, Any]:
    record: dict[str, Any] = {"input": {"draft": text}, "attributes": attributes}
    if verdict is not None:
        record["labels"] = {"verdict": verdict}
    return record


def report(path: Path, **options: Any) -> dict[str, Any]:
    pack = resolve_pack(PACK)
    return coverage(pack.manifest, load_dataset(path), pack=pack.ref, **options)


def test_duplicates_count_once_under_norm_v1(tmp_path: Path) -> None:
    path = written(
        tmp_path,
        [
            case("Take 20 tablets now!", "block", language="en", source="hand_written"),
            case("take 20 TABLETS now", "block", language="en"),
            case("Take 2 tablets now", "block"),  # a number apart: another case
            case("Rest and drink water", "pass", language="es", stratum="routine"),
            case("Unlabelled draft", None),
        ],
    )
    result = report(path)
    assert result["format"] == FORMAT and result["norm"] == "norm/v1"
    assert result["cases"] == 5
    assert result["distinct"] == {"exact": 5, "normalised": 4}
    assert result["duplicate_groups"] == 1 and result["conflicting_groups"] == 0
    verdict = result["questions"]["verdict"]
    assert verdict["labelled"] == 4
    assert verdict["labels"]["block"] == {"cases": 3, "distinct": 2}
    assert verdict["labels"]["pass"] == {"cases": 1, "distinct": 1}
    assert verdict["labels"]["review"] == {"cases": 0, "distinct": 0}
    block = next(item for item in result["critical"] if item["label"] == "block")
    assert block == {
        "question": "verdict",
        "label": "block",
        "cases": 3,
        "distinct": 2,
        "insufficient": True,
    }
    assert result["mix"] == {
        "language": {"en": 2, "es": 1, "unknown": 2},
        "source": {"hand_written": 1, "unknown": 4},
        "stratum": {"routine": 1, "unknown": 4},
    }
    assert (
        "verdict=block: 2 distinct positives, under 100: too few to measure" in result["warnings"]
    )
    assert "duplicates" not in result


def test_a_critical_label_with_100_distinct_positives_is_enough(tmp_path: Path) -> None:
    records = [case(f"Take {n} tablets", "block") for n in range(100)]
    records += [case("Take 0 TABLETS!", "block")]  # a duplicate adds no evidence
    result = report(written(tmp_path, records))
    block = next(item for item in result["critical"] if item["label"] == "block")
    assert (block["cases"], block["distinct"], block["insufficient"]) == (101, 100, False)
    assert not any(warning.startswith("verdict=block") for warning in result["warnings"])
    records[-1] = case("Take 0 tablets", "pass")  # the duplicate says otherwise
    result = report(written(tmp_path, records))
    assert result["conflicting_groups"] == 1
    assert "1 groups of duplicate cases have labels that disagree" in result["warnings"]


def test_an_attribute_value_is_cut_and_never_inferred(tmp_path: Path) -> None:
    long = "x" * 100
    result = report(written(tmp_path, [case("a", "pass", language=long), case("b", "pass")]))
    assert result["mix"]["language"] == {"unknown": 1, "x" * 64: 1}


def test_a_set_the_pack_cannot_read_is_refused_by_case_id(tmp_path: Path) -> None:
    records = [case("secret text", "pass"), {"id": "c-2", "input": {"draft": "secret text"}}]
    records[1]["labels"] = {"no_such_question": "yes"}
    with pytest.raises(MendariError, match="case c-2: labels name a question") as refused:
        report(written(tmp_path, records))
    assert "secret" not in str(refused.value)


def test_the_command_prints_counts_never_a_cases_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = written(
        tmp_path,
        [
            {"id": "c-1", **case("Swallow every pill", "block")},
            {"id": "c-2", **case("swallow EVERY pill!", "block")},
        ],
    )
    assert main(["data", "coverage", PACK, "--dataset", str(path)]) == 0
    text = capsys.readouterr().out
    assert "2 cases, 1 distinct (2 exactly)" in text and "verdict=block: 1 of 2 cases" in text
    assert "pill" not in text.lower() and "c-1" not in text
    assert main(["data", "coverage", PACK, "--dataset", str(path), "--json", "--ids"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["duplicates"] == [["c-1", "c-2"]]
    assert "pill" not in printed.lower()
    assert main(["data", "coverage", PACK, "--dataset", str(path), "--ids"]) == 0
    assert "duplicates: c-1, c-2" in capsys.readouterr().out


def test_ai_guards_test_set_has_30_distinct_blocks_of_164() -> None:
    """Recorded for LP-17 and the owner (LP-11 conflict 3): the evaluation report counts 164
    positives of `verdict=block`; 30 of them are distinct. `mendari.eval/2` is unchanged."""
    pack = resolve_pack(PACK)
    result = coverage(pack.manifest, resolve_dataset(pack, "test"), pack=pack.ref)
    assert (result["cases"], result["distinct"]["normalised"]) == (600, 81)
    block = next(item for item in result["critical"] if item["label"] == "block")
    assert (block["cases"], block["distinct"], block["insufficient"]) == (164, 30, True)
