# SPDX-License-Identifier: Apache-2.0
"""The early scoreboard's page (LP-17f, `scripts/scoreboard_page.py`, docs/grading/scoreboard.md;
the grading method's section 14): it is what the results store gives, byte for byte; every local
configuration of the five reference packs is on each of their public scoring sets; each table
is a regression check with its scope statement, intervals and the insufficient-evidence banner
where evidence is short; and the page says "unsigned" and "method draft, not in effect", with no
Rank column, no "ranked" and no bare percentage."""

from __future__ import annotations

import datetime
import importlib.util
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from mendari.core.registry import resolve_pack
from mendari.evals import method, results_store, scoreboard
from mendari.evals.results_store import Store
from tests.test_scoreboard import bare_percentages

ROOT = Path(__file__).resolve().parents[1]
PACKS = (
    "mendari/ai-guard",
    "mendari/clinical-trial-prescreener",
    "mendari/patient-inbox-router",
    "mendari/prior-auth-preflight",
    "mendari/referral-router",
)
SCOPE_ROWS = ("Marks", "Pack", "Set", "Method", "Measured on", "Models", "Disclosure", "Status")


def _script() -> Any:
    path = ROOT / "scripts" / "scoreboard_page.py"
    spec = importlib.util.spec_from_file_location("scoreboard_page", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


page_script = _script()


@pytest.fixture(scope="module")
def text() -> str:
    return str(page_script.page(Store.at(ROOT / "scoreboard")))


def tables(text: str) -> dict[tuple[str, str], str]:
    """Each pack and set's section of the page, by its heading."""
    parts = re.split(r"^## (\S+): `(\w+)`$", text.split("\n## Superseded results")[0], flags=re.M)
    return {(pack, name): body for pack, name, body in zip(*[iter(parts[1:])] * 3, strict=True)}


def test_the_page_is_regenerated_from_the_store(text: str) -> None:
    assert page_script.PAGE.read_text(encoding="utf-8") == text
    assert page_script.main(["--check"]) == 0


def test_the_page_reads_the_repositorys_packs_whatever_the_search_path(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer copy of a reference pack on `MENDARI_PACKS_PATH` (or pulled from a registry)
    comes first on the search path; the page and its check never read it."""
    copy = tmp_path / "ai-guard"
    shutil.copytree(ROOT / "packs" / "ai-guard", copy)
    manifest = (copy / "pack.yaml").read_text(encoding="utf-8")
    newer = re.sub(r"^version: .*$", "version: 99.0.0", manifest, count=1, flags=re.M)
    assert newer != manifest
    (copy / "pack.yaml").write_text(newer, encoding="utf-8")
    monkeypatch.setenv("MENDARI_PACKS_PATH", str(tmp_path))
    assert resolve_pack("mendari/ai-guard").root == copy.resolve()
    assert page_script.page(Store.at(ROOT / "scoreboard")) == text
    assert page_script.main(["--check"]) == 0


def test_the_page_check_fails_on_an_edited_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    edited = tmp_path / "scoreboard.md"
    edited.write_text(page_script.PAGE.read_text(encoding="utf-8").replace("0 of", "1 of", 1))
    monkeypatch.setattr(page_script, "PAGE", edited)
    monkeypatch.setattr(page_script, "ROOT", tmp_path)
    assert page_script.main(["--check"]) == 1
    assert "is not what the results store gives" in capsys.readouterr().err


def test_all_five_packs_appear_on_every_public_scoring_set(text: str) -> None:
    shown = tables(text)
    expected = {
        (pack, name) for pack in PACKS for name in results_store.scoring_sets(resolve_pack(pack))
    }
    assert set(shown) == expected and len(expected) == 15
    for (pack, name), body in shown.items():
        for configuration in results_store.local_configurations(resolve_pack(pack)):
            written = results_store.written(configuration.providers)
            assert f"| Mendari only: `{written}`" in body, (pack, name, written)
    # The prescreener's CQL configuration is deterministic, but remote: measured by a build
    # session on Mendari's own CQL engine, in 3 replicates (LP-17h), and not published until
    # `cql` has a disclosure statement: its scope is shown, and none of its figures.
    for name in ("test", "exclusions", "exclusions_holdout"):
        body = shown[("mendari/clinical-trial-prescreener", name)]
        assert "Not measured yet" not in body
        assert "| Mendari only: `notes=rules,structured=cql` |" in body
        assert f"| Disclosure | {scoreboard.DISCLOSURE_PENDING} | no model provider |" in body
        assert f"| Accuracy | {scoreboard.DISCLOSURE_PENDING} | " in body
        assert f"| Case flip rate | {scoreboard.DISCLOSURE_PENDING} | none: one replicate |" in body
    store = Store.at(ROOT / "scoreboard")
    public = {name: found for name, found in store.results().items() if not found.is_hidden}
    heads = results_store.heads(public)
    assert len(heads) == 21
    remote = [found for found in heads.values() if not found.local]
    assert sorted(found.set_name for found in remote) == sorted(
        ("test", "exclusions", "exclusions_holdout")
    )
    for found in remote:
        assert found.providers == {"notes": "rules", "structured": "cql"}
        assert len(found.replicates) == 3 and found.declared is not None
        assert found.result["figures"]["replicates"]["flip_rate"]["k"] == 0  # deterministic
    # No remote arm (a model's) is measured: each reads "not measured".
    for body in shown.values():
        assert "+ specialised classifier: not measured; + general LLM: not measured" in body


def test_the_early_scoreboard_is_unranked_with_intervals_banner_and_scope(text: str) -> None:
    shown = tables(text)
    for (pack, name), body in shown.items():
        assert "Public set: regression check, not a ranking." in body
        assert "Engineering evaluation, not clinical validation." in body
        for row in SCOPE_ROWS:
            assert re.search(rf"^\| {row} \|", body, flags=re.M), (pack, name, row)
        # A figure in each table: a remote entry whose disclosure is pending shows none.
        assert re.search(
            r"^\| Accuracy \|(?: [^|]+ \|)*? \d+ of \d+ \[[^\]]+%\] \|", body, flags=re.M
        )
        assert re.search(
            r"^\| Macro-F1 \|(?: [^|]+ \|)*? [\d.]+ \[[\d.]+-[\d.]+\] \|", body, flags=re.M
        )
        assert "lower bound " in body and "simultaneous across" in body
        assert "Arms: Mendari only: measured;" in body
    # Only the Referral Router's test set has enough distinct positives on every critical label.
    banners = {key for key, body in shown.items() if "**Insufficient evidence.**" in body}
    assert set(shown) - banners == {("mendari/referral-router", "test")}
    referral = shown[("mendari/referral-router", "test")]
    assert re.search(r"113 distinct positives, \d+ detected", referral)
    assert re.search(r"124 distinct positives, \d+ detected", referral)
    guard = shown[("mendari/ai-guard", "test")]
    for label, positives in (("verdict=block", 30), ("question_emergency=yes", 39)):
        assert f"`dosing=dosing,screen=rules`: {label}, {positives} distinct positives" in guard


def test_the_page_has_no_bare_percentage_rank_or_ranked(text: str) -> None:
    assert bare_percentages(text) == []
    assert re.search(r"\bRank\b|\branked\b|unranked|\brank\b", text) is None
    assert "| Rank" not in text


def test_unsigned_and_draft_are_said_on_the_page(text: str) -> None:
    """Each current result's marks open with "unsigned" while it has no signature, then the
    method's draft mark while it was not in effect on the day measured, and end with the day it
    goes stale; each superseded result is listed with the one that superseded it. All of it is
    worked out from the store, so it holds as the owner re-measures and signs the results (the
    runbook's steps 8 and 10)."""
    public = {
        name: found
        for name, found in Store.at(ROOT / "scoreboard").results().items()
        if not found.is_hidden
    }
    current = results_store.heads(public)
    rows = re.findall(r"^\| Marks \| (.+) \|$", text, flags=re.M)
    cells = [cell.strip() for row in rows for cell in row.split(" | ")]
    assert (len(rows), len(cells)) == (15, len(current))

    def opening(unsigned: bool, draft: bool) -> list[str]:
        return [scoreboard.UNSIGNED] * unsigned + [scoreboard.DRAFT] * draft

    said = []
    for cell in cells:
        marks = cell.split("; ")
        unsigned, draft = scoreboard.UNSIGNED in marks, scoreboard.DRAFT in marks
        assert marks[: unsigned + draft] == opening(unsigned, draft), cell
        assert re.fullmatch(r"stale after \d{4}-\d{2}-\d{2}", marks[-1]), cell
        said.append((unsigned, draft))
    held = [
        ("signature" not in found.result, not found.result["scope"]["method"]["in_effect"])
        for found in current.values()
    ]
    assert sorted(said) == sorted(held)
    assert '"unsigned" until the owner signs it' in text
    assert f'**"{scoreboard.DRAFT}":**' in text
    legend = "the method has no effective date yet."
    assert (legend in text) == (method.EFFECTIVE is None)
    later = results_store.superseded(public)
    if not later:
        assert "None yet: no stored result has been superseded." in text
    for name, by in later.items():
        assert f"| `{name[:16]}` | `{by[:16]}` |" in text


def test_the_draft_legend_follows_the_effective_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the owner sets the effective date (the runbook's step 4), the legend says the mark is
    on the results measured before that day, not that the method has none."""
    monkeypatch.setattr(method, "EFFECTIVE", datetime.date(2026, 12, 15))
    text = page_script.page(Store.at(ROOT / "scoreboard"))
    legend = "measured before the method's effective date, 2026-12-15."
    assert f'- **"{scoreboard.DRAFT}":** {legend}' in text
    assert "the method has no effective date yet." not in text


def test_the_page_points_to_site_evidence_and_ranks_none_of_it(text: str) -> None:
    """LP-22h: sites' own evaluations are on the Hub, beside each pack; the page says so in one
    sentence (it is made from the repository alone, so it names no registry's address) and
    carries none of their figures."""
    assert page_script.SITE_EVIDENCE in text
    assert text.count("Site evidence") == 1
    assert "site-evidence" not in text and "signed counts, are shown on the Hub" in text
    assert "http" not in page_script.SITE_EVIDENCE
