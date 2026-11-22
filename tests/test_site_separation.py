# SPDX-License-Identifier: Apache-2.0
"""Site evidence is shown apart (LP-22h, ADR-0017's decision 10): sites choose their own cases,
so nothing of the site network enters a hidden-set result, a grade, a band or the scoreboard's
tables. The modules that rank import none of it, and a registry full of site reports changes
nothing they compute."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mendari.evals.results_store import Store
from tests.conftest import ROOT, Registry, share_signed, shared_benchmark, verified_site
from tests.test_scoreboard_page import page_script

RANKING = (
    "mendari.evals.ranking",
    "mendari.evals.scoreboard",
    "mendari.evals.hidden",
    "mendari.evals.grade",
    "mendari.evals.bands",
)
SITE_CODE = ("mendari.hub.site_evidence", "mendari.hub.site_reports", "mendari.evals.pooling")


def test_the_ranking_modules_import_no_site_code() -> None:
    """In a fresh interpreter: importing every module that ranks or grades loads none of the
    site network's (directly or through anything they import)."""
    code = (
        "import importlib, json, sys\n"
        f"for name in {list(RANKING)!r}:\n"
        "    importlib.import_module(name)\n"
        f"print(json.dumps(sorted(m for m in sys.modules if m in {list(SITE_CODE)!r})))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert json.loads(done.stdout) == []


def test_site_evidence_never_enters_the_hidden_set_rank(registry: Registry, tmp_path: Path) -> None:
    """The scoreboard's page and its board, made before and after sites share signed reports
    of a reference pack, are the same bytes: they read the repository's results store only."""
    store = Store.at(ROOT / "tests" / "fixtures" / "scoreboard")
    before = page_script.page(store)
    assert registry.publish().status_code == 201
    for org in ("north-health", "south-clinic"):
        site = verified_site(registry, tmp_path, org=org)
        assert share_signed(registry, site, shared_benchmark()).status_code == 201
    evidence = registry.client.get("/v1/packs/mendari/referral-router/site-evidence").json()
    assert evidence["groups"][0]["sites"] == 2
    assert page_script.page(store) == before
