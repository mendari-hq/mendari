# SPDX-License-Identifier: Apache-2.0
"""The public page's scores (web/index.html, LP-17g): written between two marker comments by
`scripts/scoreboard_page.py` from the results store, checked by its `--check`, and nowhere typed
by hand. They are the Patient Inbox Router's public `test` set: every arm, each measured entry's
figures as k of n with their exact intervals, the insufficient-evidence banner, and every other
arm "not measured". The page shows no bare percentage, no best row and, until a provider type's
disclosure is stated, no remote model and no configuration that calls one (a variant's name can
name its provider: `rules_and_jev`)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from mendari.core.registry import resolve_pack
from mendari.evals import arms, results_store, runs, scoreboard
from mendari.evals.render import shown
from mendari.evals.results_store import Store
from tests.test_scoreboard_page import page_script

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "web" / "index.html"
# Model and vendor names the shipped page once typed in; none may come back untied to a stated
# disclosure.
REMOTE_NAMES = ("jev", "claude", "haiku", "sonnet", "gpt", "anthropic", "openai")


@pytest.fixture(scope="module")
def scores() -> str:
    return str(page_script.web_scores(Store.at(ROOT / "scoreboard")))


def held(site: str) -> str:
    """What the site holds between its markers, the marker lines included."""
    begin = site.index(page_script.WEB_INDENT + page_script.WEB_BEGIN)
    end = site.index("\n", site.index(page_script.WEB_END)) + 1
    return site[begin:end]


def cells(scores: str) -> dict[str, list[str]]:
    """Each row's cells (tags stripped), by the row's header."""
    found = {}
    for row in re.findall(r"<tr><th scope=\"row\">(.*?)</tr>", scores):
        head, _, rest = row.partition("</th>")
        texts = [re.sub(r"<[^>]+>", "", cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", rest)]
        found[re.sub(r"<[^>]+>", " ", head).strip()] = texts
    return found


def bare_percentages(text: str) -> list[str]:
    """What in `text` is a percentage outside an interval: a bracketed interval ("[98.3-99.8%]"),
    a confidence level ("95% interval", "95% CI") and the interval a level names ("95% CI",
    then its bounds joined by an en dash, as the packs' list writes them) are not."""
    kept = re.sub(r"\[[^\[\]]*%\]", "", text)
    kept = re.sub(r"95% CI [\d.]+\u2013[\d.]+%", "", kept)
    kept = re.sub(r"95% (interval|CI)", "", kept)
    return [line.strip() for line in kept.splitlines() if "%" in line]


def test_the_sites_scores_are_the_stores(scores: str) -> None:
    """The scores between the markers are what the store gives, byte for byte, and each figure
    is its entry's, as `render.shown` prints it."""
    site = SITE.read_text(encoding="utf-8")
    assert site.count(page_script.WEB_BEGIN) == 1 and site.count(page_script.WEB_END) == 1
    assert held(site) == scores
    assert page_script.with_scores(site, scores) == site
    assert page_script.main(["--check"]) == 0

    store = Store.at(ROOT / "scoreboard")
    current = [
        found.result
        for found in results_store.heads(store.results()).values()
        if (found.pack_id, found.set_name, found.is_hidden)
        == (page_script.WEB_PACK, page_script.WEB_SET, False)
    ]
    assert len(current) == 2  # the router's two local configurations, both "Mendari only"
    rows = cells(scores)
    for entry in current:
        figures = entry["figures"]
        configuration = ", ".join(
            f"{slot}={variant}"
            for slot, variant in sorted(entry["configuration"]["providers"].items())
        )
        (key,) = [k for k in rows if configuration in k]
        assert key.startswith(arms.ARMS[entry["arm"]])
        # As the store holds it, so signing (the runbook's step 10) needs no edit here.
        assert (scoreboard.UNSIGNED in key) == ("signature" not in entry), key
        assert (scoreboard.DRAFT in key) == (not entry["scope"]["method"]["in_effect"]), key
        expected = [
            figures["accuracy"],
            figures["automation"],
            figures["false_automation"],
            *(c["detected"] for c in figures["critical"]),
        ]
        assert rows[key] == [shown(f) for f in expected], key
    # Every arm is there: measured, or "not measured" across the row.
    for name in ("+ specialised classifier", "+ general LLM", "+ both"):
        assert rows[name] == [scoreboard.NOT_MEASURED], name
    assert "Insufficient evidence: urgency=urgent has 23 distinct positives here" in scores
    assert "Public set: regression check, not a ranking." in scores
    assert "Engineering evaluation, not clinical validation." in scores


def test_the_site_shows_no_bare_percentage_or_best_row(scores: str) -> None:
    site = SITE.read_text(encoding="utf-8")
    assert bare_percentages(site) == []
    assert 'class="best"' not in site and "strong" not in scores
    assert re.search(r"\b(Rank|ranked|best)\b", scores) is None
    # No remote model, nor a configuration that calls one, is named until its provider type's
    # disclosure is stated (none is yet).
    lowered = scores.lower()
    assert [name for name in REMOTE_NAMES if name in lowered] == []
    # What the typed table said, and is no longer: a size and versions no result is of.
    assert "100 cases" not in site and "pack 0.3\u20130.4" not in site


def test_the_check_fails_when_the_sites_scores_are_edited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    edited = tmp_path / "web" / "index.html"
    edited.parent.mkdir()
    edited.write_text(SITE.read_text(encoding="utf-8").replace("596 of 600", "599 of 600", 1))
    page = tmp_path / "docs" / "grading" / "scoreboard.md"
    page.parent.mkdir(parents=True)
    page.write_text(page_script.PAGE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(page_script, "ROOT", tmp_path)
    monkeypatch.setattr(page_script, "WEB", edited)
    monkeypatch.setattr(page_script, "PAGE", page)
    assert page_script.main(["--check"]) == 1
    assert "web/index.html is not what the results store gives" in capsys.readouterr().err


def test_a_site_without_its_markers_is_refused() -> None:
    site = SITE.read_text(encoding="utf-8")
    for broken in (
        site.replace(page_script.WEB_BEGIN, ""),
        site.replace(page_script.WEB_END, ""),
        site + page_script.WEB_INDENT + page_script.WEB_BEGIN + "\n",
    ):
        with pytest.raises(SystemExit, match="needs its two score markers"):
            page_script.with_scores(broken, "")


def test_the_intro_claims_no_more_than_the_scores_show(scores: str) -> None:
    """The hand-written intro above the markers says what is scored as the table shows it: an
    arm with no stored result reads "not measured", and the intro does not claim every
    configuration is scored."""
    site = SITE.read_text(encoding="utf-8")
    intro = site[site.index('<section id="scores"') : site.index(page_script.WEB_BEGIN)]
    assert scoreboard.NOT_MEASURED in scores
    assert "is scored on the same labelled cases" not in intro
    assert f"“{scoreboard.NOT_MEASURED}”" in intro


def pending(arm: str, configuration: str, models: dict[str, list[str]]) -> dict[str, Any]:
    """A board's item for a remote entry whose provider type has no stated disclosure (17h
    measures them; none is stored yet)."""
    return {
        "entry": {"arm": arm, "scope": {"models": {"by_type": models}}, "figures": {}},
        "arm_name": arms.ARMS[arm],
        "configuration": configuration,
        "marks": [scoreboard.UNSIGNED],
        "published": False,
    }


@pytest.mark.parametrize(
    ("arm", "configuration", "models"),
    [
        ("llm", "router=llm,safety_screen=rules", {"llm": ["vendor/model-x"]}),
        ("classifier", "router=rules,safety_screen=rules_and_jev", {"jev": ["jev-1.13.0"]}),
    ],
)
def test_an_undisclosed_entry_names_no_configuration_model_or_figure(
    arm: str, configuration: str, models: dict[str, list[str]]
) -> None:
    """Such an entry is listed by its arm and marks alone, with "not published: disclosure
    pending" in place of its figures: neither its models nor its configuration is written, as a
    variant's name can name its provider (`rules_and_jev`)."""
    row = page_script._web_entry(pending(arm, configuration, models), [("urgency", "urgent")], 4)
    assert f'<td colspan="4" class="muted">{scoreboard.DISCLOSURE_PENDING}</td>' in row
    assert f"<span>configuration {page_script.WITHHELD}</span>" in row
    assert scoreboard.UNSIGNED in row
    written = row.lower()
    assert [name for name in (*REMOTE_NAMES, "model-x") if name in written] == []
    assert "router=" not in row and "safety_screen" not in row


def test_an_arm_names_its_unmeasured_configurations_only_once_disclosed() -> None:
    """Once an arm has an entry, its configurations not measured yet are listed too. Those that
    call a type with no stated disclosure are not named but counted in one row; once the type's
    disclosure is stated, each is named. A local configuration is always named."""
    pack = resolve_pack(page_script.WEB_PACK)
    pairs = [("urgency", "urgent")]
    missing = ["router=jev,safety_screen=rules", "router=naive_bayes,safety_screen=rules_and_jev"]
    classifier = arms.ARMS[arms.CLASSIFIER]
    table = {
        "entries": [
            pending(
                arms.CLASSIFIER, "router=rules,safety_screen=rules_and_jev", {"jev": ["jev-1.13"]}
            )
        ],
        "not_measured": [
            {"arm": arms.CLASSIFIER, "name": classifier, "configuration": label}
            for label in missing
        ],
    }
    arm = {"arm": arms.CLASSIFIER, "name": classifier, "status": scoreboard.MEASURED}

    undisclosed = page_script.nameable(pack, None)
    assert undisclosed == {
        "router=naive_bayes,safety_screen=rules",
        "router=rules,safety_screen=rules",
    }
    rows = page_script._arm_rows(table, arm, pairs, 4, undisclosed)
    assert len(rows) == 2 and "jev" not in "".join(rows).lower()
    assert f"<span>2 configurations {page_script.WITHHELD}</span>" in rows[1]
    assert f'<td colspan="4" class="muted">{scoreboard.NOT_MEASURED}</td>' in rows[1]

    stated = runs.Disclosures("2026-11-05", {"jev": "A synthetic statement for this test."})
    disclosed = page_script.nameable(pack, stated)
    assert "router=llm,safety_screen=rules" not in disclosed  # the LLM's is still pending
    rows = page_script._arm_rows(table, arm, pairs, 4, disclosed)
    assert len(rows) == 3
    assert "<span>router=jev, safety_screen=rules</span>" in rows[1]
    assert "<span>router=naive_bayes, safety_screen=rules_and_jev</span>" in rows[2]
