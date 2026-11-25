# SPDX-License-Identifier: Apache-2.0
"""A CDS Hooks prefetch template is a read or search relative to the EHR's FHIR server (LP-16e,
conflict 21, `core/spec.py` `PREFETCH`): `Type/<id>`, `Type/{{context.<name>}}` or
`Type?<query>`. Anything else (an absolute URL, `//`, a leading `/`, `..`, an operation, a
fragment, whitespace) refuses an agent the deployment does not own at load and warns of an owned
one, which `mendari validate` refuses; every shipped template passes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, load_agent
from mendari.core.spec import PREFETCH, prefetch_problems
from mendari.errors import AgentValidationError
from mendari.evals.behaviour_tests import validate_agent
from tests.conftest import CHART_DIR, DEMO_AGENT_DIR, TRIAGE_DIR, fixture_agents
from tests.support.chart_fixture import chart_copy
from tests.support.fixture_records import SHOWCASE


def with_prefetch(tmp_path: Path, template: str) -> Path:
    """A copy of the chart fixture under its own id (not a bundled agent: from a registry, it
    is bounded) with its `patient` prefetch template replaced."""

    def replaced(manifest: dict[str, Any]) -> None:
        manifest["cds_hooks"]["prefetch"]["patient"] = template

    return chart_copy(tmp_path, manifest=replaced)


@pytest.mark.parametrize(
    "template",
    [
        "https://x/P",
        "//x/P",
        "/Patient/1",
        "Patient/../x",
        "Patient/$everything",
        "Patient?a#b",
        "Patient/..",
        "Patient/{{ context.patientId }}",
        "Patient/1\n",
        "Patient?_id=1\x07",
    ],
)
def test_a_prefetch_template_must_be_a_relative_read_or_search(
    tmp_path: Path, template: str
) -> None:
    """From a registry, refused at load naming the key; owned, it loads warned and decides as
    before, and `mendari validate` refuses it."""
    root = with_prefetch(tmp_path, template)
    with pytest.raises(AgentValidationError) as refused:
        load_agent(root, origin="hub")
    (problem,) = refused.value.problems
    assert problem.startswith("cds_hooks.prefetch.patient ") and "relative to the EHR" in problem
    owned = load_agent(root)
    assert owned.warnings == (problem,)
    assert Decision(owned).decide_fhir(SHOWCASE.bundle())[0].recommendation is not None
    with pytest.raises(AgentValidationError, match=r"cds_hooks\.prefetch\.patient"):
        validate_agent(owned)


def test_a_relative_read_or_search_loads_from_a_registry(tmp_path: Path) -> None:
    for template in (
        "Patient/{{context.patientId}}",
        "Patient/pat-9",
        "Patient?_id={{context.patientId}}&_count=1",
        "Patient",
    ):
        root = with_prefetch(tmp_path / template.replace("/", "_").replace("?", "_"), template)
        assert load_agent(root, origin="hub").warnings == ()


def test_every_shipped_prefetch_template_is_a_relative_read_or_search() -> None:
    """The bundled demo agent's templates and every test fixture's (the chart fixture's five
    searches and reads, the triage fixture's two reads among them): each a read or a search, so
    none is warned of, and none would be refused from a registry."""
    found = []
    roots = [DEMO_AGENT_DIR, *fixture_agents()]
    assert CHART_DIR in roots and TRIAGE_DIR in roots
    for root in roots:
        hooks = load_agent(root).manifest.cds_hooks
        assert prefetch_problems(hooks) == [], root
        found += [] if hooks is None else list(hooks.prefetch.values())
    assert len(found) >= 9
    assert all(PREFETCH.fullmatch(template) for template in found)
