# SPDX-License-Identifier: Apache-2.0
"""The EHR scorecard (notes/ehr-scorecard.md, rubric v1; LP-23a, graded again by LP-23m): every
criterion is graded twice, before LP-23 (as LP-23a merged it) and at the close-out; every
criterion cites a test that exists or says "missing", every grade states its rubric version,
commits and scope, a test that was not at the before commit is cited only by a row that names
the LP-23 step whose test it is, a row whose evidence is a CI run that has not happened yet is not
met, no figure is a percentage or a fraction, and each grade is what its column's criteria give,
the overall grade the lowest dimension's."""

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
COMMIT = "06d5332f"  # before: main at that commit, and what LP-23a adds
AFTER = "d2ef0201"  # after: the close-out commit, LP-23m's, on the lane's head
MERGED = "75ccf3bf"  # LP-23a merged (#256): the before column's statuses
GRADED = f"before graded at `{COMMIT}` with LP-23a, after at `{AFTER}` with LP-23m"
SCOPE = (
    f"rubric v1; {GRADED} (the close-out commit); synthetic data and test keys only, not a "
    "vendor certification"
)
# What a criterion not yet shown in CI says it waits on: a workflow, or some of its jobs, that has
# not run on GitHub (`conformance.yml` job `crd-server` has not run on GitHub yet).
NOT_RUN = re.compile(
    r"`([\w.-]+)`(?: jobs? ((?:`[\w-]+`(?:, | and )?)+))? (?:has|have) not run on GitHub yet"
)
WORKFLOWS = ("sandbox-e2e", "fhir-conformance", "conformance.yml", "terminology.yml")
# `conformance.yml` already runs on main (its `dtr-payer` job, weekly): only the jobs the LP-23
# lane adds to it have not run there.
NEW_JOBS = {"conformance.yml": {"crd-server", "smart-client", "bulk-client"}}
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
    before: str
    status: str  # after: at the close-out commit
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
            assert len(cells) == 8, line
            rows.append(Criterion(cells[0], int(cells[1]), *cells[2:]))
    return rows


def paragraphs(body: str) -> list[str]:
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", body) if p.strip()]


def level_of(rows: list[Criterion], column: str = "status") -> int:
    """The rubric's rule: the highest level whose criteria, and every lower level's, are met, in
    one column (`before`, or `status`: after)."""
    reached = 1
    for level in LEVELS[1:]:
        if any(getattr(row, column) != "met" for row in rows if row.level == level):
            break
        reached = level
    return reached


def grade(paragraph: str, label: str) -> tuple[int, int]:
    """A paragraph's `**<label>:** level <before> before, level <after> after`."""
    found = re.search(rf"\*\*{label}:\*\* level (\d) before, level (\d) after\b", paragraph)
    assert found, paragraph
    return int(found.group(1)), int(found.group(2))


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
        assert row.before in ("met", "partial", "missing"), row.id
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
    """Each dimension's grade, the summary's rows and the overall grade carry the scope line (the
    rubric, both commits), and the scorecard says why a level has no interval, how the after
    column is graded, and that the launch plan's grade is not reproduced."""
    whole = " ".join(text().split())
    assert f"**The scope of every grade:** {SCOPE}." in whole
    assert "not an estimate from a sample, so it has no interval" in whole
    assert "not in the repository or anywhere in its history" in whole and "A12.goal" in whole
    assert "graded only on evidence in the repository: a test, a golden, or a run" in whole
    assert "CI run that has not happened yet is not met" in whole
    graded = 0
    for title, body in sections().items():
        stated = [p for p in paragraphs(body) if p.startswith("**Grade:**")]
        assert len(stated) == 1, title
        assert f"Rubric v1; {GRADED} (the close-out commit); synthetic data" in stated[0]
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
    """The before grade is at `COMMIT` with LP-23a. A test a row names (as its evidence, or in
    its last column) is a test function at `COMMIT`, or the row's last column says it rests on
    LP-23a's work, or, for a criterion met after, on the work of the LP-23 step that met it
    (`23d: ...`); and a row that says so names such a test. A partly met criterion may name the
    tests of the step that met part of it, which its last column then names first (`23e: ...`).
    Skipped in a shallow clone, which holds no history."""
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


def test_the_before_column_is_what_lp_23a_merged() -> None:
    """Each criterion's Before is its status as LP-23a merged the scorecard (#256, `MERGED`), the
    same criteria in the same order, so the after grade is set beside the grade LP-23 started
    from. Skipped in a shallow clone, which holds no history."""
    if _shallow():
        pytest.skip("a shallow clone holds no history")
    shown = ["git", "show", f"{MERGED}:notes/ehr-scorecard.md"]
    merged = subprocess.run(shown, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    then = {}
    for line in merged.splitlines():
        if CRITERION.match(line):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            assert len(cells) == 7, line  # LP-23a's layout: one status
            then[cells[0]] = cells[3]
    now = {row.id: row.before for body in sections().values() for row in criteria(body)}
    assert list(now) == list(then)
    assert now == then


def test_a_row_waiting_on_a_ci_run_is_not_met_after() -> None:
    """A criterion whose last column says a workflow has not run on GitHub yet is not met after,
    and names the workflow (a job of ci.yml, which the lane's pull request runs, or a scheduled
    workflow, which runs on main after the lane merges): the after grade counts no CI run that
    has not happened. A scheduled workflow that already runs on main (`conformance.yml`) is
    named by the lane's new jobs in it, never as a whole. Each rests on its LP-23 step's work,
    named first."""
    waiting = 0
    for body in sections().values():
        for row in criteria(body):
            named = NOT_RUN.findall(row.waits_on)
            if not named:
                continue
            waiting += 1
            assert row.status != "met", row.id
            for workflow, jobs in named:
                assert workflow in WORKFLOWS, (row.id, workflow)
                listed = set(re.findall(r"`([\w-]+)`", jobs))
                if workflow in NEW_JOBS:
                    assert listed and listed <= NEW_JOBS[workflow], (row.id, workflow, jobs)
                else:
                    assert not listed, (row.id, workflow, jobs)
            assert re.match(r"23[e-l]: ", row.waits_on), row.id
    assert waiting >= 8


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
    rubric's rule before and after; the overall grade is the lowest of each column, beside every
    dimension; each criterion names an A12 row that exists (or an LP-23 requirement, or the
    Definition of shipped); and each one not met after says what builds it or what it waits on,
    a vendor sandbox's always the owner."""
    found = sections()
    assert list(found) == list(DIMENSIONS)
    audit = (ROOT / "notes" / "vision-audit.md").read_text(encoding="utf-8")
    rows_of_a12 = set(re.findall(r"^\| (A12\.[\w-]+) \|", audit, flags=re.MULTILINE))
    assert "A12.goal" in rows_of_a12 and "A12.acc-3" in rows_of_a12
    levels: dict[str, tuple[int, int]] = {}
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
        levels[title] = (level_of(rows, "before"), level_of(rows))
        (stated,) = [p for p in paragraphs(body) if p.startswith("**Grade:**")]
        assert grade(stated, "Grade") == levels[title], title
    lowest = (min(b for b, _ in levels.values()), min(a for _, a in levels.values()))
    for title, body in found.items():
        (stated,) = [p for p in paragraphs(body) if p.startswith("**Grade:**")]
        assert grade(stated, "Overall") == lowest, title
    summary = {
        cells[0]: tuple(int(cell) for cell in cells[1:5])
        for line in text().splitlines()
        if re.match(r"^\| \d\. ", line)
        for cells in [[cell.strip() for cell in line.strip().strip("|").split("|")]]
    }
    assert summary == {
        f"{n}. {title}": (*levels[title], *lowest) for n, title in enumerate(DIMENSIONS, start=1)
    }
    (overall,) = [p for p in paragraphs(text()) if p.startswith("**Overall:**")]
    assert grade(overall, "Overall") == lowest
