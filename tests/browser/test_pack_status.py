# SPDX-License-Identifier: Apache-2.0
"""The reference packs' label (mendari.core.status, LP-04) in a real browser: every view of
every reference pack says what it is."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from mendari.core.status import SYNTHETIC_EXAMPLE
from tests.browser.test_dashboard import PACKS, open_pack, show

pytestmark = pytest.mark.browser

# The views that show the pack's status (src/mendari/dashboard: overview, card, try, review,
# audit).
LABELLED = ["overview", "card", "try", "review", "audit"]


@pytest.mark.parametrize("pack_id", PACKS)
def test_every_view_shows_the_label(page: Page, pack_id: str) -> None:
    open_pack(page, pack_id, LABELLED[0])
    for view in LABELLED:
        show(page, view)
        expect(page.locator(".status-notice").first).to_have_text(SYNTHETIC_EXAMPLE)
