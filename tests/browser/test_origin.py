# SPDX-License-Identifier: Apache-2.0
"""Requests other sites start, in a real browser (Chromium's own Fetch Metadata and SameSite
rules), and Mendari's own pages in a browser without Fetch Metadata: run with
`uv run pytest -m browser`."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Route, expect

from mendari.core.audit import AuditStore
from mendari.server.app import create_app
from mendari.server.origin import OWN_REQUEST
from tests.browser.conftest import INBOX, serving
from tests.browser.test_dashboard import open_pack, show
from tests.conftest import encrypt_mode, security_lines, signed_in

pytestmark = pytest.mark.browser

ATTACKER = "http://attacker.test/"  # another site, answered by the test itself


def site_signed_in_as(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, str, str]]:
    """Mendari with sign-in on, at `localhost` (a secure context, so its Secure cookies stay),
    and a session of `role`: the base URL, the cookie's name and its value."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")  # never reached
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    db = tmp_path / "a.db"
    app = create_app(db_path=db)
    with AuditStore(db) as store:
        name, _, value = signed_in(store, role=role, host="localhost")["cookie"].partition("=")
    with serving(app) as port:
        yield f"http://localhost:{port}", name, value


@pytest.fixture
def signed_in_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, str, str]]:
    """A reviewer's session (`site_signed_in_as`)."""
    yield from site_signed_in_as("reviewer", tmp_path, monkeypatch)


@pytest.fixture
def admin_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str, str]]:
    """An administrator's session (`site_signed_in_as`): every export is theirs."""
    yield from site_signed_in_as("admin", tmp_path, monkeypatch)


def with_session(browser: Browser, base: str, name: str, value: str) -> BrowserContext:
    context = browser.new_context(base_url=base, accept_downloads=True)
    context.add_cookies(
        [
            {
                "name": name,
                "value": value,
                "url": base,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ]
    )
    return context


def test_a_form_on_another_site_cannot_sign_you_out(
    browser: Browser, signed_in_site: tuple[str, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The browser leaves the SameSite=Lax session cookie off the cross-site POST; the answer
    must still not clear it."""
    base, name, value = signed_in_site
    context = browser.new_context()
    context.add_cookies(
        [
            {
                "name": name,
                "value": value,
                "url": base,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ]
    )
    page = context.new_page()
    form = f'<form id=f method=post action="{base}/auth/logout"></form><script>f.submit()</script>'
    page.route(ATTACKER, lambda route: route.fulfill(content_type="text/html", body=form))
    with page.expect_response(f"{base}/auth/logout") as answer:
        page.goto(ATTACKER)
    assert answer.value.status == 403
    refusals = [e for e in security_lines(capsys.readouterr().out) if e["event"] != "request"]
    assert [(e["event"], e["reason"]) for e in refusals] == [("auth.origin.refused", "cross_site")]
    assert [c["name"] for c in context.cookies(base)] == [name]
    page.goto(f"{base}/api/auth/me")
    assert '"signed_in":true' in page.inner_text("body")
    context.close()


def test_the_dashboard_works_in_a_browser_without_fetch_metadata(
    browser: Browser, admin_site: tuple[str, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Safari before 16.4 sends no `Sec-Fetch-Site`, and no `Origin` on a read. Played here by
    sending each of Chromium's requests again without its Fetch Metadata: every request
    Mendari's pages make carries `X-Mendari-Request` and is answered (the dashboard's views,
    its three downloads, the landing's OpenAPI document, and the API reference's, which it
    reads and links twice), while another site's link to an API route, which cannot carry it,
    is refused."""
    base, name, value = admin_site
    context = with_session(browser, base, name, value)
    sent: list[tuple[str, str, str | None, int]] = []

    def without_fetch_metadata(route: Route) -> None:
        request = route.request
        headers = {k: v for k, v in request.headers.items() if not k.startswith("sec-fetch-")}
        answer = route.fetch(headers=headers)
        path = urllib.parse.urlsplit(request.url).path
        sent.append((request.method, path, headers.get(OWN_REQUEST), answer.status))
        route.fulfill(response=answer)

    context.route(f"{base}/**", without_fetch_metadata)
    page = context.new_page()
    open_pack(page, INBOX)
    expect(page.locator("#signed-in")).to_be_visible()
    show(page, "review")
    show(page, "audit")
    with page.expect_download() as audit:
        page.get_by_role("button", name="Export as FHIR").click()
    assert audit.value.suggested_filename.startswith("audit-")
    show(page, "feedback")
    with page.expect_download() as wrong:
        page.locator("#feedback-export").click()
    assert wrong.value.suggested_filename.endswith("-feedback-down.jsonl")
    page.goto("/#admin/activity")
    expect(page.locator("#activity")).not_to_have_attribute("aria-busy", "true")
    expect(page.locator("#activity .error")).to_have_count(0)
    with page.expect_download() as activity:
        page.get_by_role("button", name="Export as FHIR").click()
    assert activity.value.suggested_filename.startswith("activity-")
    page.goto("/docs")
    with page.expect_download() as document:
        page.get_by_role("link", name="The OpenAPI document").click()
    assert document.value.suggested_filename == "openapi.json"
    page.goto("/docs/reference")
    expect(page.locator(".swagger-ui .info .title")).to_contain_text("Mendari")
    links = page.locator('a[href="/openapi.json"]')  # under the title, and in the introduction
    expect(links).to_have_count(2)
    for n in range(2):
        with page.expect_download() as linked:
            links.nth(n).click()
        assert linked.value.suggested_filename == "openapi.json"

    reads = [(m, p, own, status) for m, p, own, status in sent if p.startswith("/api/")]
    reads += [(m, p, own, status) for m, p, own, status in sent if p == "/openapi.json"]
    called = {p for _, p, _, _ in reads}
    assert {"/api/audit/export/fhir", "/api/audit/administrative", "/openapi.json"} <= called
    assert f"/api/packs/{INBOX}/feedback/export" in called
    assert [r for r in reads if r[2] != "1" or r[3] == 403] == []
    logged = security_lines(capsys.readouterr().out)
    assert {"export.audit", "export.audit.administrative"} <= {e["event"] for e in logged}
    assert [e for e in logged if e["event"] == "auth.origin.refused"] == []

    # Another site's link to the review queue, in the same browser: no header, so refused.
    refused = page.goto(f"{base}/api/review")
    assert refused is not None and refused.status == 403
    events = [e for e in security_lines(capsys.readouterr().out) if e["event"] != "request"]
    assert [(e["event"], e["reason"]) for e in events] == [
        ("auth.origin.refused", "no_fetch_metadata")
    ]
    context.close()
