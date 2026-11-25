# SPDX-License-Identifier: Apache-2.0
"""The reference agents' label (mendari.core.status, LP-04) in a real browser: every view of
every reference agent says what it is."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from mendari.core.status import SYNTHETIC_EXAMPLE
from tests.browser.test_dashboard import AGENTS, open_agent, show

pytestmark = pytest.mark.browser

# The views that show the agent's status (src/mendari/dashboard: overview, card, try, review,
# audit).
LABELLED = ["overview", "card", "try", "review", "audit"]


@pytest.mark.parametrize("agent_id", AGENTS)
def test_every_view_shows_the_label(page: Page, agent_id: str) -> None:
    open_agent(page, agent_id, LABELLED[0])
    for view in LABELLED:
        show(page, view)
        expect(page.locator(".status-notice").first).to_have_text(SYNTHETIC_EXAMPLE)
