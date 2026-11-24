# SPDX-License-Identifier: Apache-2.0
"""The Agent Hub's tests show an agent's evaluation page in every state it has words for from
`tests/fixtures/evaluation_pages.json` (`hub/tests/stub-registry.mjs` serves it as a registry
would, `hub/tests/evaluation-states.spec.ts` opens each case). This holds that file to what a
registry answers now, so the page is tested against real answers and not remembered ones."""

from __future__ import annotations

import json

from mendari.hub import badge, evaluation_page
from tests.test_evaluation_page import parsed


def test_the_hub_s_fixture_is_what_a_registry_answers_now() -> None:
    """`tests/fixtures/evaluation_pages.json`, which the Agent Hub's tests show the evaluation
    page from, holds a registry's own answers in every state the page has words for: the same
    answers, by shape, as the registry gives today for the same cases
    (`tests/support/evaluation_pages.py` writes it)."""
    from tests.support import evaluation_pages as fixture

    committed = json.loads(fixture.FIXTURE.read_text(encoding="utf-8"))
    assert fixture.shape(committed) == fixture.shape(fixture.generate()), (
        "the registry answers otherwise now: uv run python -m tests.support.evaluation_pages"
    )
    answers = [case["evaluation"] for case in committed["agents"].values()]
    assert {answer["state"] for answer in answers} == {
        "evaluated",
        "none",
        "not_verified",
        "recalled",
    }
    compared = {d["comparison"]["status"] for answer in answers for d in answer["datasets"]}
    assert compared == {
        evaluation_page.ORDERED,
        evaluation_page.TIED,
        evaluation_page.ONE,
        evaluation_page.NO_CRITICAL_LABEL,
        evaluation_page.INSUFFICIENT,
    }
    assert {n["reason"] for answer in answers for n in answer["not_shown"]} >= {
        "record",
        "key_revoked",
    }
    sizes = {len(d["configurations"]) for answer in answers for d in answer["datasets"]}
    assert sizes >= {1, 2, 3}  # one, two and several configurations
    # Each badge is its agent's, as the registry draws it: the newest version's, so an earlier
    # version's answer comes with the badge of the newest.
    cases = committed["agents"]
    for key, case in cases.items():
        said_of = cases[key.split("@")[0]]["evaluation"]["badge"]
        assert parsed(case["badge"]) == ("Mendari", said_of["message"])
        assert case["badge"] == badge.svg(said_of["message"], said_of["tone"])
    assert cases["states/older@1.0.0"]["evaluation"]["state"] == "evaluated"
    assert cases["states/older"]["evaluation"]["badge"]["message"] == "no evaluation published"
    # The Hub's tests open every case.
    spec = (fixture.ROOT / "hub" / "tests" / "evaluation-states.spec.ts").read_text(
        encoding="utf-8"
    )
    for key in committed["agents"]:
        name = key.split("/", 1)[1].split("@")[0]
        assert f'"{name}"' in spec, f"hub/tests/evaluation-states.spec.ts does not open {name}"
    # What a publisher wrote is in it as written: the page, not the fixture, makes it text.
    hostile = committed["agents"]["states/hostile"]["evaluation"]
    assert hostile["agent"]["name"] == fixture.HOSTILE_NAME
    assert hostile["datasets"][0]["labelled_by"] == [fixture.HOSTILE_LABELLER]
