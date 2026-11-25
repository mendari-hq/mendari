# SPDX-License-Identifier: Apache-2.0
"""Written output in the browser (`dashboard/written.js`, ADR-0025), over a real memberships-mode
server under PHI mode `encrypt` (tests/browser/access_site.py) and sessions planted as
`/auth/callback` makes them:

- a grader and nothing else, once a round holds work of theirs, sees the grading screen alone:
  their counts round by round, then one text at a time, shown as text (never markup) above what
  the agent was given, and each criterion with its instructions and each level's anchor beside
  its choice; grades saved bring the next text, and the last one the empty state;
- blind: nothing of the round's ledger reaches the page (a planted judge), nor anyone's grade;
- a criterion left ungraded is said beside that criterion, and nothing is recorded;
- a reader of the agent sees its rounds under Evaluations: no report while a round is open, and
  once closed the report, with "withheld" where the judge is not validated; a round left
  closing says so and how to finish it, and a report edited in the database is not shown;
- axe-core finds nothing on either screen.

Every text is synthetic. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from mendari import AuditStore, Decision, load_pack
from mendari.core.pack import Pack
from mendari.core.written import CLOSING_NOTE, REPORT_MISMATCH, WrittenStore
from mendari.evals import written
from mendari.evals.harness import load_dataset
from mendari.evals.written import WrittenLedger, WrittenRun
from mendari.providers.judge import Judge
from mendari.server.auth import person_id
from tests.browser.access_site import Site, memberships, person, running
from tests.browser.test_accessibility import violations
from tests.conftest import ROOT, encrypted_store
from tests.test_auth import IDP


def tamper(store: AuditStore, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Edit the database behind the store's back, as someone with the database could."""
    with store._db.session(exclusive=True) as db:
        db.all(sql, params)


pytestmark = pytest.mark.browser

CLINIC = "clinic-a"
MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini"
RUBRIC = "reply_quality"
MARKUP = '<img src=x onerror="window.pwned=1">'
JUDGE_MARK = "PLANTED-JUDGE-MODEL-3308"
MESSAGES = (f"{MARKUP} Where do I park on Tuesday?", "Could you refill my lisinopril?")


@dataclass
class Grading:
    site: Site
    rounds: WrittenStore
    pack: Pack
    run: WrittenRun

    def grader(self, sub: str, *roles: str) -> dict[str, str]:
        return person(
            self.site, sub=sub, email=f"{sub}@example.org", org=CLINIC,
            roles=list(roles or ("labeller",)),
        )  # fmt: skip

    def round(self, subs: list[str]) -> str:
        made = self.rounds.create_round(
            CLINIC, self.pack, RUBRIC, self.run, sample=2, created_by="o"
        )
        self.rounds.assign(str(made["round"]), [person_id(IDP, sub) for sub in subs], double=1.0)
        return str(made["round"])


@pytest.fixture
def grading(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Grading]:
    memberships(monkeypatch)  # with sign-in and PHI mode `encrypt`, as access_site's
    agents = tmp_path / "agents"
    shutil.copytree(MINI, agents / "written-mini")
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(agents))
    pack = load_pack(agents / "written-mini")
    lines = [
        {"id": f"case-{n}", "input": {"message": {"body": body}}} for n, body in enumerate(MESSAGES)
    ]
    raw = "".join(json.dumps(line) + "\n" for line in lines).encode()
    made = written.run(
        Decision(pack, origin_cap=False),
        load_dataset("written.jsonl", name="written", raw=raw),
        RUBRIC,
        judge=Judge(pack, RUBRIC, mode="canned"),
    )
    header = json.loads(json.dumps(made.ledger.header))
    header["judge"]["model"] = JUDGE_MARK
    header["models"] = [JUDGE_MARK if m == "mock-written-judge" else m for m in header["models"]]
    run = WrittenRun(WrittenLedger(header, made.ledger.cases), made.items)
    db = tmp_path / "a.db"
    with running(browser, db) as site, encrypted_store(db) as store:
        yield Grading(site, WrittenStore(store), pack, run)


def case(page: Page) -> Locator:
    return page.locator("#label-case")


def criterion(page: Page, name: str) -> Locator:
    return page.locator(".label-question", has=page.locator("legend", has_text=name))


def grade(page: Page, safe: str = "yes", next_: str = "fully", plain: str = "yes") -> None:
    criterion(page, "Safe to send").get_by_role("radio", name=safe).first.check()
    criterion(page, "Says what happens next").get_by_role("radio", name=next_).first.check()
    criterion(page, "Plain language").get_by_role("radio", name=plain).first.check()


def test_a_grader_grades_text_after_text_blind_until_nothing_waits(grading: Grading) -> None:
    ada = grading.grader("ada")
    grading.grader("bo")
    round_id = grading.round(["ada", "bo"])
    page = grading.site.window(ada, bypass_csp=True)  # axe is injected
    page.goto("/")
    expect(page.locator("main h1")).to_have_text("Written output")
    # A grader and nothing else: the grading screen alone.
    expect(page.locator(".pack-picker")).to_be_hidden()
    expect(page.locator(".nav a[data-view]:visible")).to_have_text(["Written output"])
    sets = page.locator("#label-sets")
    expect(sets.locator("h2")).to_have_text("Your rounds")
    expect(sets.locator("tbody tr")).to_have_count(1)
    expect(sets.locator("tbody tr td").first).to_have_text(round_id)
    expect(sets).to_contain_text("2 texts wait for you in all.")
    expect(case(page).locator("h2")).to_have_text("Text to grade")
    # The text the agent wrote, as text, above what it was given; markup in either never runs.
    expect(page.locator("#written-text")).to_contain_text("Thank you for your message.")
    expect(case(page)).to_contain_text("What the agent was given")
    assert page.evaluate("window.pwned") is None
    assert page.locator("#label-case img").count() == 0
    # The rubric's own words: each criterion's instructions, each level's anchor.
    rubric = grading.pack.manifest.rubrics[RUBRIC]
    safe = criterion(page, "Safe to send")
    expect(safe).to_contain_text(rubric.criteria["safe_to_send"].instructions)
    expect(safe).to_contain_text(rubric.criteria["safe_to_send"].criteria["no"])
    following = criterion(page, "Says what happens next")
    expect(following.get_by_role("radio")).to_have_count(3)
    expect(following).to_contain_text(rubric.criteria["addresses_request"].criteria["partly"])
    # Blind: nothing of the round's ledger, and no one's grade.
    said = page.content()
    for hidden in (JUDGE_MARK, "pass_from", "must_pass", "kappa", "confidence"):
        assert hidden not in said, hidden
    assert not page.locator("#label-case input:checked").count()  # nothing is chosen for them
    assert violations(page) == []
    # A criterion left ungraded is said beside it, and nothing is saved.
    criterion(page, "Safe to send").get_by_role("radio", name="yes").first.check()
    case(page).get_by_role("button", name="Save grades").click()
    expect(criterion(page, "Plain language").locator(".field-error")).to_have_text(
        "Choose a level for this criterion."
    )
    assert grading.rounds.progress(round_id)["assignments"]["graded"] == 0
    grade(page)
    case(page).get_by_role("button", name="Save grades").click()
    expect(sets).to_contain_text("1 text waits for you in all.")
    grade(page, safe="no", next_="partly")
    case(page).get_by_role("button", name="Save grades").click()
    expect(case(page)).to_contain_text("Nothing waits for you")
    expect(case(page)).to_contain_text("Every text assigned to you is graded.")
    assert grading.rounds.progress(round_id)["assignments"]["graded"] == 2
    assert violations(page) == []


def test_a_labeller_with_no_round_sees_what_they_saw_before(grading: Grading) -> None:
    """A grader's routes need no key of their own, so the screen shows only once a round holds
    work of theirs: before, a labeller and nothing else is told there is nothing to do."""
    page = grading.site.window(grading.grader("ada"))
    page.goto("/")
    expect(page.locator("main h1")).to_have_text("Nothing to do here yet")
    expect(page.locator('.nav a[data-view="written"]')).to_be_hidden()


def test_a_reader_sees_the_rounds_and_a_closed_ones_report(grading: Grading) -> None:
    grading.grader("ada")
    grading.grader("bo")
    round_id = grading.round(["ada", "bo"])
    reader = grading.grader("vera", "viewer")
    page = grading.site.window(reader, bypass_csp=True)  # axe is injected
    page.goto("/?agent=mendari-test/written-mini#evals/written")
    panel = page.locator(f'[data-round="{round_id}"]')
    expect(panel).to_contain_text("Open: being graded")
    expect(panel).to_contain_text(
        "No report yet: nothing is compared, for anyone, until the round is closed."
    )
    assert JUDGE_MARK not in page.content() and "kappa" not in panel.inner_text()
    # A reader holds no `label`: no grading screen for them.
    expect(page.locator('.nav a[data-view="written"]')).to_be_hidden()
    grading.rounds.close(round_id, by="ops")
    page.reload()
    panel = page.locator(f'[data-round="{round_id}"]')
    expect(panel).to_contain_text("Closed")
    rows = panel.locator('[data-table="written-report"] tbody tr')
    expect(rows).to_have_count(3)
    expect(rows.first).to_contain_text("insufficient evidence")
    expect(rows.first).to_contain_text(
        "withheld"
    )  # never a figure where the judge is not validated
    expect(panel).to_contain_text(JUDGE_MARK)  # the report names its judge, once closed
    # What a state was decided on is on the page: agreement on pass or fail, what the judge
    # could not grade, and the seed and resamples nobody chose afterwards.
    expect(panel.locator('[data-table="written-report"] thead th')).to_contain_text(
        ["Passing, on the outputs both graded", "The judge could not grade"]
    )
    expect(rows.first).to_contain_text("on pass or fail:")
    expect(panel).to_contain_text("Bootstrap seed 0, 2000 resamples.")
    assert "Thank you for your message" not in page.content()
    assert violations(page) == []


def test_a_round_left_closing_says_so_and_how_to_finish(
    grading: Grading, monkeypatch: pytest.MonkeyPatch
) -> None:
    grading.grader("ada")
    grading.grader("bo")
    round_id = grading.round(["ada", "bo"])

    def interrupted(*args: object, **kwargs: object) -> object:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(written, "report", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        grading.rounds.close(round_id, by="ops")
    monkeypatch.undo()
    page = grading.site.window(grading.grader("vera", "viewer"))
    page.goto("/?agent=mendari-test/written-mini#evals/written")
    panel = page.locator(f'[data-round="{round_id}"]')
    expect(panel).to_contain_text("Closing: not finished")
    expect(panel.locator("[data-round-note]")).to_have_text(CLOSING_NOTE)
    expect(panel).to_contain_text("round close` on it again to finish")
    expect(panel).to_contain_text("No report is shown.")
    expect(panel.locator('[data-table="written-report"]')).to_have_count(0)


def test_a_report_edited_in_the_database_is_not_shown(grading: Grading) -> None:
    grading.grader("ada")
    grading.grader("bo")
    round_id = grading.round(["ada", "bo"])
    report = grading.rounds.close(round_id, by="ops")["report"]
    forged = json.loads(json.dumps(report))
    for measured in forged["criteria"].values():
        measured["validation"] = {"state": "validated", "reasons": []}
    tamper(
        grading.rounds.store,
        "UPDATE written_rounds SET report = ? WHERE id = ?",
        (json.dumps(forged, sort_keys=True, separators=(",", ":")), round_id),
    )
    page = grading.site.window(grading.grader("vera", "viewer"))
    page.goto("/?agent=mendari-test/written-mini#evals/written")
    panel = page.locator(f'[data-round="{round_id}"]')
    expect(panel.locator("[data-round-note]")).to_have_text(REPORT_MISMATCH)
    expect(panel).to_contain_text("does not match the audit trail")
    expect(panel).to_contain_text("No report is shown.")
    expect(panel.locator('[data-table="written-report"]')).to_have_count(0)
    assert "validated" not in panel.inner_text()
