# SPDX-License-Identifier: Apache-2.0
"""The EHR scorecard (notes/ehr-scorecard.md, rubric v1; LP-23a): every criterion cites a test
that exists or says "missing", every grade states its rubric version, commit and scope, a test
that was not at the graded commit is cited only by a row that says it rests on LP-23a, no figure
is a percentage or a fraction, and each grade is what its criteria give, the overall grade the
lowest dimension's."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCORECARD = ROOT / "notes" / "ehr-scorecard.md"
COMMIT = "06d5332f"
GRADED = f"graded at `{COMMIT}` with LP-23a"  # main at that commit, and what LP-23a adds
SCOPE = f"rubric v1, {GRADED}, synthetic data and test keys only, not a vendor certification"
DIMENSIONS = (
    "Client trust",
    "Who may call from where",
    "Failure behaviour",
    "Data shapes",
    "Cards and workflow",
    "Conformance evidence",
    "Vendor fit",
)
LEVELS = (1, 2, 3, 4, 5)
NODE = re.compile(r"tests/[\w/]+\.py::\w+")
CRITERION = re.compile(r"^\| (?P<id>[A-Z]{2}-(?P<level>\d)[a-z]) \| ")


class Criterion(NamedTuple):
    id: str
    level: int
    text: str
    status: str
    evidence: str
    source: str
    waits_on: str


def text() -> str:
    return SCORECARD.read_text(encoding="utf-8")


def sections() -> dict[str, str]:
    """Each dimension's section, by its title, in order."""
    parts = re.split(r"^## (\d)\. (.+)$", text(), flags=re.MULTILINE)
    return {
        title: body for _, title, body in zip(parts[1::3], parts[2::3], parts[3::3], strict=True)
    }


def criteria(body: str) -> list[Criterion]:
    rows = []
    for line in body.splitlines():
        if CRITERION.match(line):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            assert len(cells) == 7, line
            rows.append(Criterion(cells[0], int(cells[1]), *cells[2:]))
    return rows


def paragraphs(body: str) -> list[str]:
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", body) if p.strip()]


def level_of(rows: list[Criterion]) -> int:
    """The rubric's rule: the highest level whose criteria, and every lower level's, are met."""
    reached = 1
    for level in LEVELS[1:]:
        if any(row.status != "met" for row in rows if row.level == level):
            break
        reached = level
    return reached


def grade(paragraph: str, label: str) -> int:
    found = re.search(rf"\*\*{label}:\*\* level (\d)\b", paragraph)
    assert found, paragraph
    return int(found.group(1))


def gates_script() -> ModuleType:
    path = ROOT / "scripts" / "check_gates.py"
    spec = importlib.util.spec_from_file_location("check_gates_for_the_scorecard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_row_cites_a_test_that_exists_or_says_missing() -> None:
    """A met criterion cites its tests, a missing one says "missing", and every test the
    scorecard names anywhere is collected (collect-only, as tests/test_gates.py checks the
    gates' tests: none is run)."""
    rows = [row for body in sections().values() for row in criteria(body)]
    assert len(rows) >= 7 * 4
    for row in rows:
        assert row.status in ("met", "partial", "missing"), row.id
        cited = NODE.findall(row.evidence)
        if row.evidence == "missing":
            assert row.status != "met", row.id
        else:
            assert cited and row.evidence == ", ".join(f"`{node}`" for node in cited), row.id
        if row.status == "missing":
            assert row.evidence == "missing", row.id
    named = sorted(set(NODE.findall(text())))
    assert named
    check = gates_script()
    files = sorted({node.split("::")[0] for node in named})
    for file in files:
        assert (ROOT / file).is_file(), file
    collected = check._pytest(ROOT, ["--collect-only", *files])
    assert [node for node in named if not check._matches(node, collected)] == []


def test_every_grade_states_its_rubric_version_commit_and_scope() -> None:
    """Each dimension's grade, the summary's rows and the overall grade carry the scope line,
    and the scorecard says why a level has no interval and that the launch plan's grade is not
    reproduced."""
    whole = " ".join(text().split())
    assert f"**The scope of every grade:** {SCOPE}." in whole
    assert "not an estimate from a sample, so it has no interval" in whole
    assert "not in the repository or anywhere in its history" in whole and "A12.goal" in whole
    graded = 0
    for title, body in sections().items():
        stated = [p for p in paragraphs(body) if p.startswith("**Grade:**")]
        assert len(stated) == 1, title
        assert f"Rubric v1, {GRADED}, synthetic data and test keys only" in stated[0]
        assert stated[0].endswith("not a vendor certification."), title
        graded += 1
    assert graded == len(DIMENSIONS)
    summary = [line for line in text().splitlines() if re.match(r"^\| \d\. ", line)]
    assert len(summary) == len(DIMENSIONS)
    for line in summary:
        assert line.rstrip().endswith(f"| {SCOPE} |"), line
    (overall,) = [p for p in paragraphs(text()) if p.startswith("**Overall:**")]
    assert overall.endswith(f"R{SCOPE[1:]}."), overall


def _shallow() -> bool:
    asked = ["git", "rev-parse", "--is-shallow-repository"]
    return subprocess.run(asked, cwd=ROOT, capture_output=True, text=True).stdout.strip() == "true"


def test_a_test_not_at_the_graded_commit_is_cited_only_where_the_row_names_23a() -> None:
    """The scope line says `COMMIT` with LP-23a. A test a row names (as its evidence, or in its
    last column) is a test function at `COMMIT`, or the row's last column says it rests on
    LP-23a's work, or, for a met criterion, on the work of the LP-23 step that met it (`23d: ...`,
    as each step updates the rows it meets); and a row that says so names such a test. A partly
    met criterion may name the tests of the step that met part of it, which its last column then
    names first (`23e: ...`). Skipped in a shallow clone, which holds no history."""
    if _shallow():
        pytest.skip("a shallow clone holds no history")
    files: dict[str, str] = {}
    for body in sections().values():
        for row in criteria(body):
            new = []
            for node in NODE.findall(f"{row.evidence} {row.waits_on}"):
                file, name = node.split("::")
                if file not in files:
                    shown = ["git", "show", f"{COMMIT}:{file}"]
                    found = subprocess.run(shown, cwd=ROOT, capture_output=True, text=True)
                    files[file] = found.stdout if found.returncode == 0 else ""
                if not re.search(rf"^def {name}\(", files[file], flags=re.MULTILINE):
                    new.append(node)
            if row.status == "partial":
                assert not new or re.match(r"23[a-m]: ", row.waits_on), (row.id, new)
                continue
            step = r"23[a-m]: " if row.status == "met" else r"23a: "
            assert bool(new) is bool(re.match(step, row.waits_on)), (row.id, new)


def test_the_scorecard_shows_no_percentage_or_fraction() -> None:
    """A rubric level is a level, never a share: no percentage sign, no number of percent, no
    fraction written with a slash, "of" or "out of", and no fraction character."""
    for number, line in enumerate(text().splitlines(), start=1):
        assert "%" not in line, number
        assert not re.search(r"\d\s*percent", line, re.IGNORECASE), number
        assert not re.search(r"\d(?:\.\d+)?\s*(?:/|of|out of)\s*\d", line), number
        assert not re.search(r"[¼-¾⅐-⅞]", line), number


def test_each_grade_is_what_its_criteria_give_and_the_overall_is_the_lowest() -> None:
    """The seven dimensions, each with criteria at every level from 2 to 5, graded by the
    rubric's rule; the overall grade is the lowest, beside every dimension; each criterion names
    an A12 row that exists (or an LP-23 requirement, or the Definition of shipped); and each one
    not met says what builds it or what it waits on, a vendor sandbox's always the owner."""
    found = sections()
    assert list(found) == list(DIMENSIONS)
    audit = (ROOT / "notes" / "vision-audit.md").read_text(encoding="utf-8")
    rows_of_a12 = set(re.findall(r"^\| (A12\.[\w-]+) \|", audit, flags=re.MULTILINE))
    assert "A12.goal" in rows_of_a12 and "A12.acc-3" in rows_of_a12
    levels: dict[str, int] = {}
    prefixes: set[str] = set()
    for title, body in found.items():
        rows = criteria(body)
        assert {row.level for row in rows} == set(LEVELS[1:]), title
        prefix = {row.id[:2] for row in rows}
        assert len(prefix) == 1 and not prefix & prefixes, title
        prefixes |= prefix
        for row in rows:
            assert int(row.id[3]) == row.level, row.id
            for source in row.source.split(", "):
                assert (
                    source in rows_of_a12
                    or re.fullmatch(r"LP-23 requirement \d+", source)
                    or re.fullmatch(r"Definition of shipped §\d+", source)
                ), (row.id, source)
            if row.status != "met":
                assert row.waits_on, row.id
                assert re.match(r"23[b-m]\b|the owner: \S+( \S+){2}", row.waits_on), row.id
            else:  # the LP-23 step whose work a met row rests on, or nothing: `COMMIT` alone
                assert not row.waits_on or re.match(r"23[a-m]: \S+", row.waits_on), row.id
            if row.level == 5:
                assert row.waits_on.startswith("the owner: "), row.id
        levels[title] = level_of(rows)
        (stated,) = [p for p in paragraphs(body) if p.startswith("**Grade:**")]
        assert grade(stated, "Grade") == levels[title], title
    lowest = min(levels.values())
    for title, body in found.items():
        (stated,) = [p for p in paragraphs(body) if p.startswith("**Grade:**")]
        assert grade(stated, "Overall") == lowest, title
    summary = {
        cells[0]: (int(cells[1]), int(cells[2]))
        for line in text().splitlines()
        if re.match(r"^\| \d\. ", line)
        for cells in [[cell.strip() for cell in line.strip().strip("|").split("|")]]
    }
    assert summary == {
        f"{n}. {title}": (levels[title], lowest) for n, title in enumerate(DIMENSIONS, start=1)
    }
    (overall,) = [p for p in paragraphs(text()) if p.startswith("**Overall:**")]
    assert grade(overall, "Overall") == lowest
