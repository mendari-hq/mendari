# SPDX-License-Identifier: Apache-2.0
"""CORS for the CDS Hooks services in a real browser (LP-23c, `server/cors.py`): a page on an
origin a CDS client registered reads a hook's answer, though every Mendari answer says
`Cross-Origin-Resource-Policy: same-origin` (it governs `no-cors` loads, not a CORS read); a
page on an origin nobody registered cannot read. Run with `uv run pytest -m browser`."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from mendari.core.audit import AuditStore
from mendari.server.app import create_app
from mendari.server.cds_clients import SANDBOX_ENV
from tests.browser.conftest import free_port, serving
from tests.jwt_signers import cds_token
from tests.test_cds_clients import DISCOVERY, EHR, HOOK, RSA_EHR, entry, register
from tests.test_limits import order_sign

pytestmark = pytest.mark.browser

PAGE = "<!doctype html><html lang=en><title>EHR</title><p>A CDS client's page</p></html>"
# The page's call: a hook with the EHR's own token and a JSON body, so the browser sends a
# preflight first; what the page could read of the answer, or the error it got.
CALL = """async ([url, token, body]) => {
  try {
    const answer = await fetch(url, {
      method: "POST",
      headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
      body: JSON.stringify(body),
    });
    return {status: answer.status, cards: (await answer.json()).cards.length};
  } catch (error) {
    return {error: String(error)};
  }
}"""
LIST = """async (url) => {
  try {
    const answer = await fetch(url);
    return {status: answer.status};
  } catch (error) {
    return {error: String(error)};
  }
}"""


@pytest.fixture(autouse=True)
def _cds_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overrides the suite's (`tests/conftest.py`): no flag, the real rule, which a browser on
    this machine calling 127.0.0.1 meets."""
    monkeypatch.delenv(SANDBOX_ENV, raising=False)


def test_a_page_on_a_registered_origin_reads_a_hook_answer(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mendari in open mode on 127.0.0.1, with no flag; the EHR's page at `localhost`, another
    origin, registered for `epic`. The page calls a hook with `epic`'s token and reads its cards,
    and the decision is chained as asked for by `cds epic`. The same page at 127.0.0.1's own
    origin on the page server's port, which nobody registered, can read neither the hook nor
    discovery."""
    port, page_port = free_port(), free_port()
    here = f"http://127.0.0.1:{port}"
    origin = f"http://localhost:{page_port}"
    register(monkeypatch, entry(origins=[origin]), public_url=here)
    db = tmp_path / "a.db"
    mendari = create_app(db_path=db)

    async def index(request: Request) -> HTMLResponse:
        return HTMLResponse(PAGE)

    pages = Starlette(routes=[Route("/", index)])
    with serving(mendari, port), serving(pages, page_port):
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{origin}/")
        sent = [f"{here}{HOOK}", cds_token(RSA_EHR, EHR, f"{here}{HOOK}"), order_sign(1)]
        read = page.evaluate(CALL, sent)
        assert read == {"status": 200, "cards": 1}, read
        # Every answer said `Cross-Origin-Resource-Policy: same-origin`, and the read worked.
        corp = httpx.get(f"{here}{DISCOVERY}").headers["cross-origin-resource-policy"]
        assert corp == "same-origin"

        page.goto(f"http://127.0.0.1:{page_port}/")  # another origin, registered for no one
        again = [f"{here}{HOOK}", cds_token(RSA_EHR, EHR, f"{here}{HOOK}"), order_sign(1)]
        assert "error" in page.evaluate(CALL, again)
        assert "error" in page.evaluate(LIST, f"{here}{DISCOVERY}")
        context.close()
    with AuditStore(db) as store:
        created = [e["payload"] for e in store.events() if e["event"] == "decision.created"]
    assert [payload.get("requested_by") for payload in created] == ["cds epic"]
