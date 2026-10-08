# SPDX-License-Identifier: Apache-2.0
"""Invitations and an organization's people in the browser (LP-12f-ii, ADR-0008): the invitation
page (`/invite`) and the administrators' Access view (`#admin/<tab>`), in memberships mode.

Most tests run a real memberships-mode server at `localhost` with sessions planted as
`/auth/callback` makes them (`tests.conftest.member`), and one signs in for real, through a local
authorization endpoint on another origin. The rest mock a memberships administrator over the
open-mode test server, as tests/browser/test_access.py does. Every address is at example.org; all
data is synthetic. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Request, Route, expect

from mendari.core.audit import AuditStore
from mendari.server import auth as auth_module
from mendari.server.app import create_app
from mendari.server.auth import person_id
from mendari.standards.fhir_client import GuardedHttp
from tests.browser.conftest import serving
from tests.browser.test_access import member_of, pins
from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import INBOX, PHONE, SMALL_TARGETS, open_pack, signed_in_as
from tests.conftest import member, security_lines
from tests.test_auth import CLIENT, IDP, FakeIdp

pytestmark = pytest.mark.browser

TOKEN = re.compile(r"ohi_[A-Za-z0-9_-]{43}")
MARKUP = '<img src=x onerror="window.pwned=1">'
PEOPLE = re.compile(r"^/api/auth/(members|invitations|access-requests)(/|$)")  # a path
PEOPLE_URL = re.compile(r"/api/auth/(members|invitations|access-requests)(/|\?|$)")


# -- a real memberships-mode server --------------------------------------------------------------


@dataclass
class Site:
    """Mendari in memberships mode at `localhost` (a secure context: its Secure cookies stay), its
    store, and the browser windows a test opens (closed with it)."""

    base: str
    store: AuditStore
    browser: Browser
    contexts: list[BrowserContext] = field(default_factory=list)

    def window(self, headers: dict[str, str] | None = None, **options: Any) -> Page:
        """A new browser (context) holding the session in `headers`, if any."""
        context = self.browser.new_context(base_url=self.base, **options)
        self.contexts.append(context)
        if headers:
            name, _, value = headers["cookie"].partition("=")
            context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "url": self.base,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ]
            )
        return context.new_page()


@contextmanager
def running(browser: Browser, db: Path) -> Iterator[Site]:
    """The server, with the environment the caller set (memberships mode and sign-in)."""
    app = create_app(db_path=db)
    with AuditStore(db) as store, serving(app) as port:
        site = Site(f"http://localhost:{port}", store, browser)
        try:
            yield site
        finally:
            for context in site.contexts:
                context.close()


def memberships(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)  # reached only by the real sign-in's test
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)


@pytest.fixture
def site(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    memberships(monkeypatch)
    with running(browser, tmp_path / "a.db") as here:
        yield here


def lead(site: Site) -> dict[str, str]:
    """Clinic-A's administrator (Clinic-A's record too, made by the first call)."""
    return member(
        site.store, org="clinic-a", roles=["admin"], sub="lead", email="lead@example.org",
        host="localhost",
    )  # fmt: skip


def person(
    site: Site,
    sub: str = "ada",
    email: str | None = "ada@example.org",
    org: str | None = None,
    roles: list[str] | None = None,
) -> dict[str, str]:
    """Someone signed in (of no organization unless `org`), their address verified unless None."""
    return member(site.store, org=org, roles=roles, sub=sub, email=email, host="localhost")


def invited(
    site: Site, email: str = "ada@example.org", roles: tuple[str, ...] = ("viewer",)
) -> Any:
    """An invitation to Clinic-A made by its administrator (`lead` first), with its token."""
    return site.store.create_invitation(
        org="clinic-a", email=email, roles=list(roles), created_by=person_id(IDP, "lead")
    )


def access(page: Page, tab: str) -> None:
    """The Access view's `tab`, freshly loaded."""
    page.goto("about:blank")
    page.goto(f"/#admin/{tab}")
    expect(page.locator("main h1")).to_have_text("Access")
    expect(page.locator(f'.tabs a[data-tab="{tab}"]')).to_have_attribute("aria-current", "page")


def row(page: Page, table: str, text: str) -> Locator:
    return page.locator(f'[data-table="{table}"] tbody tr', has_text=text)


def dialog(page: Page) -> Locator:
    return page.locator("dialog[open]")


def title(page: Page) -> Locator:
    return page.locator("#invite-title")


def kept(page: Page) -> Any:
    return page.evaluate("sessionStorage.getItem('oh.invite')")


def make_link(page: Page, email: str) -> str:
    """Invite `email` (a viewer) from the Access view: the link its dialog shows."""
    access(page, "invitations")
    page.fill("#invite-email", email)
    page.get_by_role("button", name="Create link").click()
    expect(dialog(page).locator("#link-title")).to_have_text(f"Invitation link for {email}")
    return dialog(page).locator("#invite-link").input_value()


def accept(page: Page, token: str) -> None:
    """Open the link and press Accept."""
    page.goto(f"/invite#{token}")
    expect(title(page)).to_have_text("Accept your invitation")
    page.get_by_role("button", name="Accept invitation").click()


def accepts(page: Page) -> list[str]:
    """The token each accept request from `page` sends, in order."""
    sent: list[str] = []

    def record(request: Request) -> None:
        if request.url.endswith("/api/auth/invitations/accept"):
            sent.append(json.loads(request.post_data or "{}").get("token", ""))

    page.on("request", record)
    return sent


# -- the whole way, as people do it --------------------------------------------------------------


def test_an_administrator_invites_and_the_invitee_joins(site: Site) -> None:
    admin = site.window(lead(site), permissions=["clipboard-read", "clipboard-write"])
    link = make_link(admin, "ada@example.org")
    assert re.fullmatch(rf"{re.escape(site.base)}/invite#{TOKEN.pattern}", link), link
    shown = dialog(admin)
    expect(shown).to_contain_text("Shown once.")
    expect(shown).to_contain_text("Mendari sends no e-mail")
    shown.get_by_role("button", name="Copy link").click()
    expect(shown.get_by_role("button", name="Copied")).to_be_visible()
    assert admin.evaluate("navigator.clipboard.readText()") == link
    shown.get_by_role("button", name="Done").click()
    expect(admin.locator("dialog")).to_have_count(0)
    assert "ohi_" not in admin.evaluate("document.documentElement.outerHTML")
    expect(row(admin, "invitations", "ada@example.org")).to_contain_text("Open")

    ada = site.window(person(site))
    ada.goto(link)
    expect(title(ada)).to_have_text("Accept your invitation")
    assert ada.url == f"{site.base}/invite"  # the token left the address
    expect(ada.locator("#invite-body")).to_contain_text("Signed in as ada (ada@example.org)")
    ada.get_by_role("button", name="Accept invitation").click()
    expect(title(ada)).to_have_text("You've joined Clinic-A")
    expect(ada.locator("#invite-body")).to_contain_text("Your roles: Viewer.")
    assert kept(ada) is None
    ada.get_by_role("link", name="Open Mendari").click()
    expect(ada.locator("#signed-in-name")).to_have_text("ada · Clinic-A · viewer")
    expect(ada.locator('.nav a[data-view="overview"]')).to_be_visible()
    expect(ada.locator('.nav a[data-view="admin"]')).to_be_hidden()  # a viewer

    access(admin, "members")
    expect(row(admin, "members", "ada@example.org")).to_contain_text("Viewer")
    access(admin, "invitations")
    accepted = row(admin, "invitations", "ada@example.org")
    expect(accepted).to_contain_text("Accepted")
    expect(accepted).to_contain_text("by ada")


class LocalIdp(FakeIdp):
    """The fake identity provider, its authorization endpoint a page a browser can open."""

    def __init__(self, authorize: str) -> None:
        super().__init__()
        self.authorize_url = authorize

    def __call__(self, request: httpx.Request) -> httpx.Response:
        answer = super().__call__(request)
        if str(request.url) == f"{IDP}/.well-known/openid-configuration":
            return httpx.Response(
                200, json={**answer.json(), "authorization_endpoint": self.authorize_url}
            )
        return answer


@contextmanager
def authorizing(approve: Callable[[str], str]) -> Iterator[str]:
    """An authorization endpoint on 127.0.0.1, another origin than `localhost`: each request's
    path to `approve`, which answers where to send the browser back to. Yields its origin."""

    class Approve(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the standard library names it
            self.send_response(302)
            self.send_header("location", approve(self.path))
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Approve)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_a_signed_out_invitee_signs_in_and_the_token_never_leaves_the_page(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real sign-in, through a redirect to another origin and back (COOP keeps nothing across
    it but the tab's sessionStorage): the token is in no URL, header or redirect after the link's
    own, and the authorization endpoint never sees it."""
    memberships(monkeypatch)
    seen: list[str] = []
    back: list[str] = []
    idps: list[LocalIdp] = []

    def approve(path: str) -> str:
        seen.append(path)
        claims = {"sub": "ada", "name": "Ada", "email": "ada@example.org", "email_verified": True}
        code = idps[0].authorize(path, **claims)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["state"][0]
        return f"{back[0]}/auth/callback?{urllib.parse.urlencode({'code': code, 'state': state})}"

    with authorizing(approve) as endpoint:
        idps.append(LocalIdp(f"{endpoint}/authorize"))
        monkeypatch.setenv("MENDARI_OIDC_ENDPOINT_ORIGINS", endpoint)
        transport = httpx.MockTransport(idps[0])
        monkeypatch.setattr(
            auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
        )
        with running(browser, tmp_path / "a.db") as site:
            back.append(site.base)
            lead(site)
            token = invited(site)["token"]
            ada = site.window()
            sent: list[Request] = []
            ada.context.on("request", lambda request: sent.append(request))
            navigated: list[str] = []
            ada.on("framenavigated", lambda frame: navigated.append(frame.url))
            ada.goto(f"/invite#{token}")
            expect(title(ada)).to_have_text("Sign in to accept your invitation")
            assert ada.url == f"{site.base}/invite" and kept(ada) == token
            ada.get_by_role("button", name="Sign in").click()
            expect(title(ada)).to_have_text("Accept your invitation")  # back, signed in
            assert ada.url == f"{site.base}/invite"
            # Back from signing in, before Accept: in no cookie, whoever set it.
            assert not [c for c in ada.context.cookies() if "ohi_" in json.dumps(c)]
            assert "ohi_" not in ada.evaluate("document.cookie")
            expect(ada.locator("#invite-body")).to_contain_text("Signed in as Ada")
            ada.get_by_role("button", name="Accept invitation").click()
            expect(title(ada)).to_have_text("You've joined Clinic-A")
            assert kept(ada) is None
            # Every header each request really sent (cookies and the referrer included), read
            # while the browser is open.
            later = sent[1:]  # after the link's own request
            headed = [r.url for r in later if "ohi_" in json.dumps(r.all_headers())]
    assert len(seen) == 1 and "ohi_" not in seen[0]  # the identity provider saw no token
    assert any(r.url.startswith(endpoint) for r in sent)  # it really went there
    assert not [r.url for r in later if "ohi_" in r.url]
    assert headed == []
    assert not [url for url in navigated[1:] if "ohi_" in url]
    accepted = [r for r in sent if r.url.endswith("/api/auth/invitations/accept")]
    assert [json.loads(r.post_data or "{}") for r in accepted] == [{"token": token}]


def test_an_expired_link_says_ask_for_a_new_one(site: Site) -> None:
    admin_session = lead(site)
    made = invited(site)
    with site.store._db.session(exclusive=True) as db:
        db.all(
            "UPDATE invitations SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", made["id"]),
        )
    ada = site.window(person(site))
    accept(ada, made["token"])
    expect(title(ada)).to_have_text("This invitation isn't valid")
    expect(ada.locator("#invite-body")).to_contain_text("Ask whoever invited you for a new link")
    assert kept(ada) is None
    admin = site.window(admin_session)
    access(admin, "invitations")
    expired = row(admin, "invitations", "ada@example.org")
    expect(expired).to_contain_text("Expired")
    expired.get_by_role("button", name="Revoke the invitation for ada@example.org").click()
    expect(dialog(admin).locator("h2")).to_have_text("Revoke the invitation for ada@example.org?")
    dialog(admin).get_by_role("button", name="Revoke").click()
    expect(expired).to_contain_text("Revoked")
    expect(expired).to_contain_text("by You")
    expect(expired.get_by_role("button")).to_have_count(0)


def test_a_wrong_address_offers_another_account_and_keeps_the_link_open(site: Site) -> None:
    lead(site)
    made = invited(site)
    bo = site.window(person(site, sub="bo", email="bo@example.org"))
    asked: list[tuple[str, str]] = []
    bo.on("request", lambda r: asked.append((r.method, urllib.parse.urlsplit(r.url).path)))
    bo.route("**/auth/login*", lambda route: route.fulfill(status=200, body="signing in"))
    accept(bo, made["token"])
    expect(title(bo)).to_have_text("This invitation is for another e-mail address")
    expect(bo.locator("#invite-body")).to_contain_text("You're signed in as bo@example.org")
    assert kept(bo) == made["token"]
    bo.get_by_role("button", name="Use another account").click()
    bo.wait_for_url("**/auth/login?account=choose&next=%2Finvite")
    assert ("POST", "/auth/logout") in asked
    assert kept(bo) == made["token"]  # waits in the tab for the right account
    assert [i["status"] for i in site.store.invitations("clinic-a")] == ["open"]


@pytest.mark.parametrize("before", ["damaged", "no invitation", "another address"])
def test_a_second_link_in_the_same_tab_starts_over_with_it(site: Site, before: str) -> None:
    """A link opened in a tab already on /invite changes only the fragment, which loads no new
    page: it leaves the address and its history entry all the same, it replaces the token held,
    it is the one sent, and the page shows where it stands."""
    lead(site)
    sub, email = (
        ("bo", "bo@example.org") if before == "another address" else ("ada", "ada@example.org")
    )
    page = site.window(person(site, sub=sub, email=email))
    sent = accepts(page)
    if before == "damaged":
        page.goto("/invite#ohi_short")
        expect(title(page)).to_have_text("This link is incomplete")
    elif before == "no invitation":
        page.goto("/invite")
        expect(title(page)).to_have_text("No invitation in this tab")
    else:
        accept(page, invited(site)["token"])  # made out to ada; bo is signed in
        expect(title(page)).to_have_text("This invitation is for another e-mail address")
    second = invited(site, email=email)
    page.evaluate("window.sameDocument = true")
    page.goto(f"/invite#{second['token']}")
    expect(title(page)).to_have_text("Accept your invitation")
    assert page.evaluate("window.sameDocument") is True  # no new page loaded
    assert page.url == f"{site.base}/invite"
    assert kept(page) == second["token"]
    page.get_by_role("button", name="Accept invitation").click()
    expect(title(page)).to_have_text("You've joined Clinic-A")
    assert sent[-1] == second["token"]
    assert kept(page) is None
    page.go_back()  # the entry before holds no token either
    assert page.evaluate("window.sameDocument") is True and "ohi_" not in page.url


@pytest.mark.parametrize("how", ["a new page", "the same page"])
def test_a_malformed_link_clears_the_token_and_sends_nothing(site: Site, how: str) -> None:
    lead(site)
    made = invited(site)
    page = site.window(person(site))
    sent = accepts(page)
    page.goto(f"/invite#{made['token']}")
    expect(title(page)).to_have_text("Accept your invitation")
    assert kept(page) == made["token"]
    if how == "a new page":
        page.goto("about:blank")  # the tab keeps its sessionStorage
    page.goto("/invite#ohi_short")
    expect(title(page)).to_have_text("This link is incomplete")
    assert kept(page) is None
    assert page.url == f"{site.base}/invite"
    expect(page.locator("#invite-actions button")).to_have_count(0)
    assert sent == []
    assert [i["status"] for i in site.store.invitations("clinic-a")] == ["open"]


def test_an_unverified_address_is_told_to_sign_in_with_a_verified_one(site: Site) -> None:
    lead(site)
    made = invited(site)
    cy = site.window(person(site, sub="cy", email=None))
    accept(cy, made["token"])
    expect(title(cy)).to_have_text("Your address isn't verified")
    expect(cy.get_by_role("button", name="Use another account")).to_be_visible()
    assert kept(cy) == made["token"]
    assert [i["status"] for i in site.store.invitations("clinic-a")] == ["open"]


def test_a_used_link_is_not_valid_the_second_time(site: Site) -> None:
    lead(site)
    made = invited(site)
    first = site.window(person(site))
    accept(first, made["token"])
    expect(title(first)).to_have_text("You've joined Clinic-A")
    again = site.window(person(site))  # the same person, in another browser
    accept(again, made["token"])
    expect(title(again)).to_have_text("This invitation isn't valid")
    assert kept(again) is None


def test_a_revoked_link_is_not_valid(site: Site) -> None:
    admin = site.window(lead(site))
    made = invited(site)
    access(admin, "invitations")
    open_one = row(admin, "invitations", "ada@example.org")
    revoke = open_one.get_by_role("button", name="Revoke the invitation for ada@example.org")
    revoke.click()
    dialog(admin).get_by_role("button", name="Cancel").click()
    expect(admin.locator("dialog")).to_have_count(0)
    expect(open_one).to_contain_text("Open")
    assert [i["status"] for i in site.store.invitations("clinic-a")] == ["open"]
    revoke.click()
    dialog(admin).get_by_role("button", name="Revoke").click()
    expect(open_one).to_contain_text("Revoked")
    ada = site.window(person(site))
    accept(ada, made["token"])
    expect(title(ada)).to_have_text("This invitation isn't valid")


def test_a_member_accepting_again_is_told_so(site: Site) -> None:
    lead(site)
    ada = site.window(person(site, org="clinic-a", roles=["viewer"]))
    made = invited(site, roles=("reviewer",))
    accept(ada, made["token"])
    expect(title(ada)).to_have_text("You're already a member")
    expect(ada.get_by_role("link", name="Open Mendari")).to_be_visible()
    assert kept(ada) is None
    assert [i["status"] for i in site.store.invitations("clinic-a")] == ["open"]
    [held] = site.store.memberships(org="clinic-a", user_id=person_id(IDP, "ada"))
    assert held["roles"] == ["viewer"]  # roles change only by an administrator


def test_an_invitee_in_another_organization_may_switch(site: Site) -> None:
    lead(site)
    made = invited(site)
    ada = site.window(person(site, org="clinic-b", roles=["viewer"]))
    accept(ada, made["token"])
    expect(title(ada)).to_have_text("You've joined Clinic-A")
    expect(ada.locator("#invite-body")).to_contain_text("This browser still works in Clinic-B.")
    expect(ada.get_by_role("link", name="Stay in Clinic-B")).to_have_attribute("href", "/")
    ada.get_by_role("button", name="Switch to Clinic-A").click()
    expect(ada.locator("#org-select")).to_have_value("clinic-a")  # the dashboard, in Clinic-A
    me = ada.request.get("/api/auth/me").json()
    assert me["principal"]["org"] == "clinic-a"
    assert ada.evaluate("localStorage.getItem('oh.org')") == "clinic-a"


def test_an_administrator_changes_a_members_roles(site: Site) -> None:
    admin = site.window(lead(site))
    person(site, sub="bo", email="bo@example.org", org="clinic-a", roles=["viewer"])
    access(admin, "members")
    bo = row(admin, "members", "bo@example.org")
    expect(bo).to_contain_text("Viewer")
    bo.get_by_role("button", name="Change roles for bo").click()
    asking = dialog(admin)
    expect(asking.locator("h2")).to_have_text("Roles for bo")
    asking.locator('input[value="viewer"]').uncheck()
    asking.get_by_role("button", name="Save roles").click()
    expect(asking.get_by_role("alert")).to_have_text("Choose at least one role.")  # none: asked
    asking.locator('input[value="reviewer"]').check()
    asking.get_by_role("button", name="Save roles").click()
    expect(admin.locator("dialog")).to_have_count(0)
    expect(bo).to_contain_text("Reviewer")
    expect(bo).not_to_contain_text("Viewer")
    [held] = site.store.memberships(org="clinic-a", user_id=person_id(IDP, "bo"))
    assert held["roles"] == ["reviewer"]


def test_removing_a_member_asks_first(site: Site) -> None:
    admin = site.window(lead(site))
    bo_session = person(site, sub="bo", email="bo@example.org", org="clinic-a", roles=["viewer"])
    access(admin, "members")
    bo = row(admin, "members", "bo@example.org")
    bo.get_by_role("button", name="Remove bo").click()
    asking = dialog(admin)
    expect(asking.locator("h2")).to_have_text("Remove bo from Clinic-A?")
    expect(asking).to_contain_text("Open invitations to bo@example.org are revoked with it.")
    asking.get_by_role("button", name="Cancel").click()
    expect(bo).to_have_count(1)
    assert site.store.membership_in_force(person_id(IDP, "bo"), "clinic-a") is not None
    bo.get_by_role("button", name="Remove bo").click()
    dialog(admin).get_by_role("button", name="Remove").click()
    expect(bo).to_have_count(0)
    removed = site.window(bo_session)
    assert removed.request.get("/api/auth/me").json()["principal"]["org"] is None


def test_the_last_administrator_is_told_plainly(site: Site) -> None:
    admin = site.window(lead(site))
    person(site, sub="bo", email="bo@example.org", org="clinic-a", roles=["viewer"])
    access(admin, "members")
    me = row(admin, "members", "lead@example.org")
    expect(me).to_contain_text("You")
    plainly = (
        "Clinic-A needs an administrator who has signed in. "
        "Make someone else an administrator first."
    )
    me.get_by_role("button", name="Change roles for lead").click()
    expect(dialog(admin)).to_contain_text("These are your own roles")
    dialog(admin).locator('input[value="admin"]').uncheck()
    dialog(admin).locator('input[value="viewer"]').check()
    dialog(admin).get_by_role("button", name="Save roles").click()
    expect(dialog(admin).get_by_role("alert")).to_have_text(plainly)
    dialog(admin).get_by_role("button", name="Cancel").click()
    me.get_by_role("button", name="Remove lead").click()
    expect(dialog(admin)).to_contain_text("You lose access to Clinic-A at once.")
    dialog(admin).get_by_role("button", name="Remove").click()
    expect(dialog(admin).get_by_role("alert")).to_have_text(plainly)
    dialog(admin).get_by_role("button", name="Cancel").click()
    [held] = site.store.memberships(org="clinic-a", user_id=person_id(IDP, "lead"))
    assert held["roles"] == ["admin"]


def test_leaving_while_your_own_roles_change_still_starts_over(site: Site) -> None:
    """Demoting yourself, then Back to the overview before the answer comes: the dialog waits for
    it, and the page still starts over (what this window may do changed)."""
    admin = site.window(lead(site))
    person(site, sub="bo", email="bo@example.org", org="clinic-a", roles=["admin"])  # signed in
    admin.goto("/#overview")
    expect(admin.locator('.nav a[data-view="overview"]')).to_have_class(re.compile("active"))
    admin.locator('.nav a[data-view="admin"]').click()
    expect(admin.locator("main h1")).to_have_text("Access")
    held: list[Route] = []
    admin.route("**/api/auth/members/*/roles", lambda route: held.append(route))
    mine = row(admin, "members", "lead@example.org")
    mine.get_by_role("button", name="Change roles for lead").click()
    dialog(admin).locator('input[value="admin"]').uncheck()
    dialog(admin).locator('input[value="viewer"]').check()
    dialog(admin).get_by_role("button", name="Save roles").click()
    for _ in range(100):
        if held:
            break
        admin.wait_for_timeout(50)
    assert held, "the PUT was never sent"
    admin.go_back()
    expect(admin.locator('.nav a[data-view="overview"]')).to_have_class(re.compile("active"))
    expect(dialog(admin)).to_be_visible()  # waiting for its answer
    with admin.expect_navigation():
        held[0].continue_()
    expect(admin.locator('.nav a[data-view="admin"]')).to_be_hidden()  # a viewer now
    [membership] = site.store.memberships(org="clinic-a", user_id=person_id(IDP, "lead"))
    assert membership["roles"] == ["viewer"]


def test_a_request_to_join_is_granted_with_chosen_roles(site: Site) -> None:
    admin = site.window(lead(site))
    dee = site.window(person(site, sub="dee", email="dee@example.org"))
    dee.goto("/")
    expect(dee.locator("main h1")).to_have_text("You don't have access yet")
    dee.fill("#ask-org", "clinic-a")
    dee.get_by_role("button", name="Ask to join").click()
    expect(dee.locator("#toast")).to_have_text(
        "If that organization exists, your request is recorded."
    )
    access(admin, "requests")
    expect(admin.locator('.tabs a[data-tab="requests"] .count')).to_have_text("1")
    asked = row(admin, "requests", "dee@example.org")
    asked.get_by_role("button", name="Grant dee's request").click()
    granting = dialog(admin)
    expect(granting.locator("h2")).to_have_text("Let dee join Clinic-A")
    granting.locator('input[value="viewer"]').uncheck()
    granting.locator('input[value="reviewer"]').check()
    granting.get_by_role("button", name="Grant").click()
    expect(asked).to_have_count(0)
    expect(admin.locator("#admin-body")).to_contain_text("No requests to join")
    expect(admin.locator('.tabs a[data-tab="requests"] .count')).to_have_text("")
    admin.locator('.tabs a[data-tab="members"]').click()
    expect(row(admin, "members", "dee@example.org")).to_contain_text("Reviewer")
    held = site.store.membership_in_force(person_id(IDP, "dee"), "clinic-a")
    assert held is not None and held["roles"] == ["reviewer"]


def test_a_request_to_join_is_refused(site: Site) -> None:
    admin = site.window(lead(site))
    person(site, sub="dee", email="dee@example.org")
    site.store.request_access(user_id=person_id(IDP, "dee"), org="clinic-a")
    access(admin, "requests")
    asked = row(admin, "requests", "dee@example.org")
    asked.get_by_role("button", name="Refuse dee's request").click()
    expect(dialog(admin).locator("h2")).to_have_text("Refuse dee's request?")
    expect(dialog(admin)).to_contain_text("Mendari doesn't tell them; they can ask again.")
    dialog(admin).get_by_role("button", name="Refuse").click()
    expect(asked).to_have_count(0)
    assert site.store.membership_in_force(person_id(IDP, "dee"), "clinic-a") is None
    assert site.store.open_access_requests("clinic-a") == []


def test_names_and_addresses_render_as_text(site: Site) -> None:
    """A person's name (their identity provider's claim), an organization's name and an address
    can hold markup: shown as text everywhere, in a browser that would run it (no CSP)."""
    with site.store._db.session(exclusive=True) as db:
        site.store._insert_organization(
            db, org="clinic-a", name=f"Clinic {MARKUP}", created_by="test"
        )
    admin = site.window(lead(site), bypass_csp=True)
    person(site, sub=f"Bo {MARKUP}", email="bo@example.org", org="clinic-a", roles=["viewer"])
    person(site, sub=f"Dee {MARKUP}", email="dee@example.org")
    site.store.request_access(user_id=person_id(IDP, f"Dee {MARKUP}"), org="clinic-a")
    marked = "<b>x</b>@example.org"
    invited(site, email=marked)
    made = invited(site)

    def harmless(page: Page) -> None:
        """Nothing became markup, and nothing ran (an image that fails runs its handler late)."""
        expect(page.locator("main img, dialog img")).to_have_count(0)
        page.wait_for_timeout(200)
        assert page.evaluate("window.pwned") is None

    access(admin, "members")  # one document from here: every tab, each dialog
    expect(admin.locator("main")).to_contain_text(f"Who can use Clinic {MARKUP}")
    for tab, shown in (("members", f"Bo {MARKUP}"), ("requests", MARKUP), ("invitations", marked)):
        admin.locator(f'.tabs a[data-tab="{tab}"]').click()
        expect(admin.locator("#admin-body")).to_contain_text(shown)
        harmless(admin)
    row(admin, "invitations", "<b>x</b>").get_by_role("button").click()  # the invitations tab
    expect(dialog(admin).locator("h2")).to_have_text(f"Revoke the invitation for {marked}?")
    harmless(admin)
    dialog(admin).get_by_role("button", name="Cancel").click()
    admin.locator('.tabs a[data-tab="members"]').click()
    row(admin, "members", "bo@example.org").get_by_role("button", name="Change roles").click()
    expect(dialog(admin).locator("h2")).to_have_text(f"Roles for Bo {MARKUP}")
    harmless(admin)
    dialog(admin).get_by_role("button", name="Cancel").click()
    admin.locator('.tabs a[data-tab="requests"]').click()
    row(admin, "requests", "dee@example.org").get_by_role("button", name="Grant").click()
    expect(dialog(admin).locator("h2")).to_have_text(f"Let Dee {MARKUP} join Clinic {MARKUP}")
    harmless(admin)

    ada = site.window(person(site, sub=f"Ada {MARKUP}"), bypass_csp=True)
    ada.goto(f"/invite#{made['token']}")
    expect(ada.locator("#invite-body")).to_contain_text(f"Signed in as Ada {MARKUP}")
    harmless(ada)
    ada.get_by_role("button", name="Accept invitation").click()
    expect(title(ada)).to_have_text(f"You've joined Clinic {MARKUP}")
    harmless(ada)


def test_a_link_is_never_kept_or_logged(site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    admin = site.window(lead(site))
    link = make_link(admin, "ada@example.org")
    dialog(admin).get_by_role("button", name="Done").click()
    ada = site.window(person(site))
    ada.goto(link)
    expect(title(ada)).to_have_text("Accept your invitation")
    ada.get_by_role("button", name="Accept invitation").click()
    expect(title(ada)).to_have_text("You've joined Clinic-A")
    out, err = capsys.readouterr()
    lines = security_lines(out)
    assert lines and not [line for line in lines if "ohi_" in json.dumps(line)]
    assert "ohi_" not in out and "ohi_" not in err
    for page in (admin, ada):
        kept_here = page.evaluate(
            "JSON.stringify([{...localStorage}, {...sessionStorage}, document.cookie])"
        )
        assert "ohi_" not in kept_here
        assert not [c for c in page.context.cookies() if "ohi_" in json.dumps(c)]
    assert ada.evaluate("sessionStorage.length") == 0


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_invite_page_and_the_access_view_pass_axe(site: Site, scheme: str) -> None:
    look = {"bypass_csp": True, "color_scheme": scheme}  # the test injects axe
    admin_session = lead(site)
    person(site, sub="bo", email="bo@example.org", org="clinic-a", roles=["viewer"])
    person(site, sub="dee", email="dee@example.org")
    site.store.request_access(user_id=person_id(IDP, "dee"), org="clinic-a")
    made = invited(site)
    found = {}
    out = site.window(None, **look)
    out.goto(f"/invite#{made['token']}")
    expect(title(out)).to_have_text("Sign in to accept your invitation")
    found["signed out"] = violations(out)
    ada = site.window(person(site), **look)
    ada.goto(f"/invite#{made['token']}")
    expect(title(ada)).to_have_text("Accept your invitation")
    found["ready"] = violations(ada)
    cy = site.window(person(site, sub="cy", email="cy@example.org"), **look)
    accept(cy, made["token"])
    expect(title(cy)).to_have_text("This invitation is for another e-mail address")
    found["refused"] = violations(cy)
    admin = site.window(admin_session, **look)
    for tab in ("members", "requests"):
        access(admin, tab)
        found[tab] = violations(admin)
    make_link(admin, "eve@example.org")
    found["the link"] = violations(admin)
    assert {where: v for where, v in found.items() if v} == {}


def test_the_access_view_and_the_invite_page_fit_a_phone(site: Site) -> None:
    phone = {"viewport": PHONE, "has_touch": True, "is_mobile": True}
    admin_session = lead(site)
    person(site, sub="dee", email="dee@example.org")
    site.store.request_access(user_id=person_id(IDP, "dee"), org="clinic-a")
    made = invited(site)
    admin = site.window(admin_session, **phone)
    wide = "document.documentElement.scrollWidth <= window.innerWidth"
    for tab in ("members", "invitations", "requests"):
        access(admin, tab)
        assert admin.evaluate(wide), tab
        assert admin.evaluate(SMALL_TARGETS) == [], tab
    ada = site.window(person(site), **phone)
    ada.goto(f"/invite#{made['token']}")
    expect(title(ada)).to_have_text("Accept your invitation")
    assert ada.evaluate(wide) and ada.evaluate(SMALL_TARGETS) == []
    ada.get_by_role("button", name="Accept invitation").click()
    expect(title(ada)).to_have_text("You've joined Clinic-A")
    assert ada.evaluate(wide) and ada.evaluate(SMALL_TARGETS) == []


# -- mocked: memberships mode over the open server (tests/browser/test_access.py) ---------------

CLINIC_A = [{"org": "clinic-a", "name": "Clinic A", "roles": ["admin"]}]
MEMBERS = [
    {
        "id": "m1", "user_id": "user:0000000000000001", "name": "Nurse Kim",
        "email": "kim@clinic.example.org", "pending": False, "roles": ["admin"],
        "created_by": "test", "created_at": "2026-10-01T12:00:00+00:00", "you": True,
    },
    {
        "id": "m2", "user_id": "user:0000000000000002", "name": "Bo",
        "email": "bo@clinic.example.org", "pending": False, "roles": ["viewer"],
        "created_by": "user:0000000000000001", "created_at": "2026-10-02T12:00:00+00:00",
        "you": False,
    },
]  # fmt: skip
INVITATION = {
    "id": "i1", "org": "clinic-a", "email": "ada@example.org", "roles": ["viewer"],
    "created_by": "user:0000000000000001", "created_at": "2026-10-03T12:00:00+00:00",
    "expires_at": "2026-10-10T12:00:00+00:00", "accepted_at": None, "accepted_by": None,
    "revoked_at": None, "revoked_by": None, "status": "open",
}  # fmt: skip
MADE = {**INVITATION, "link": "/invite#ohi_" + "A" * 43}


def administering(page: Page) -> dict[str, list[Any]]:
    """Clinic A's administrator in memberships mode: its people's lists answered from the
    returned lists (writes answered as the server would), later routes per path first."""
    member_of(page, CLINIC_A, "clinic-a", ["admin"])
    lists: dict[str, list[Any]] = {"members": MEMBERS, "invitations": [INVITATION], "requests": []}

    def people(route: Route) -> None:
        path = urllib.parse.urlsplit(route.request.url).path
        kind = {"members": "members", "invitations": "invitations"}.get(
            path.split("/")[3], "requests"
        )
        if route.request.method == "GET":
            route.fulfill(json=lists[kind])
        elif route.request.method == "DELETE":
            route.fulfill(status=204, body="")
        else:
            route.fulfill(status=201, json=MADE)

    page.route(PEOPLE_URL, people)
    return lists


def test_the_access_view_is_hidden_from_everyone_but_administrators(page: Page) -> None:
    seen = pins(page)
    reviewer = [{"org": "clinic-a", "name": "Clinic A", "roles": ["reviewer"]}]
    member_of(page, reviewer, "clinic-a", ["reviewer"])
    open_pack(page, INBOX)
    expect(page.locator('.nav a[data-view="admin"]')).to_be_hidden()
    page.goto(f"/?pack={INBOX}#admin/members")
    expect(page.locator('.nav a[data-view="overview"]')).to_have_class(re.compile("active"))
    expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")
    assert not [path for _, path, _ in seen if PEOPLE.match(path)]


@pytest.mark.parametrize("mode", ["claims", "open"])
def test_claims_and_open_mode_have_no_access_view(page: Page, mode: str) -> None:
    """An administrator by claims holds `keys` too, and open mode may do anything: neither
    reaches an organization's people (no memberships mode, no such routes)."""
    seen = pins(page)
    if mode == "claims":
        signed_in_as(page, "admin")
    open_pack(page, INBOX)
    expect(page.locator('.nav a[data-view="admin"]')).to_be_hidden()
    page.goto(f"/?pack={INBOX}#admin")
    expect(page.locator('.nav a[data-view="overview"]')).to_have_class(re.compile("active"))
    expect(page.locator("#view")).not_to_have_attribute("aria-busy", "true")
    assert not [path for _, path, _ in seen if PEOPLE.match(path)]


def test_an_administrator_sees_the_access_view(page: Page) -> None:
    administering(page)
    open_pack(page, INBOX)
    link = page.locator('.nav a[data-view="admin"]')
    expect(link).to_be_visible()
    link.click()
    expect(page.locator("main h1")).to_have_text("Access")
    expect(page.locator("main")).to_contain_text("Who can use Clinic A and with which roles.")
    expect(page.locator(".tabs a")).to_have_text(["Members", "Invitations", "Requests to join"])
    expect(row(page, "members", "Bo")).to_contain_text("You")  # who added Bo


def test_inviting_waits_for_the_notice(page: Page) -> None:
    administering(page)
    required = {"now": False}
    posted: list[str] = []

    def notice(route: Route) -> None:
        route.fulfill(
            json={
                "version": "1", "text": "Synthetic data only.", "acknowledgement": "I understand.",
                "required": required["now"], "acknowledged": False,
            }
        )  # fmt: skip

    def acknowledge(route: Route) -> None:
        required["now"] = False
        route.fulfill(json={"acknowledged": True})

    def invitations(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        posted.append(route.request.post_data or "")
        if len(posted) == 1:  # a new notice version since the dashboard opened
            required["now"] = True
            refused = {"error": "AuthError", "detail": "acknowledge the notice first"}
            route.fulfill(status=403, headers={"Mendari-Notice-Required": "1"}, json=refused)
        else:
            route.fulfill(status=201, json=MADE)

    page.route("**/api/notice", notice)
    page.route("**/api/notice/acknowledge", acknowledge)
    page.route("**/api/auth/invitations", invitations)
    open_pack(page, INBOX)
    page.goto(f"/?pack={INBOX}#admin/invitations")
    page.fill("#invite-email", "ada@example.org")
    page.get_by_role("button", name="Create link").click()
    page.get_by_role("button", name="I understand: synthetic data only").click()
    expect(page.locator("#invite-link")).to_have_value(re.compile(r"/invite#ohi_A{43}$"))
    assert len(posted) == 2 and json.loads(posted[1])["email"] == "ada@example.org"


def test_a_refused_invitation_says_why_in_words(page: Page) -> None:
    administering(page)
    problem = {
        "type": "value_error", "loc": ["body", "email"], "input": "x",
        "msg": "Value error, an e-mail address here is ASCII (an internationalised one is refused)",
    }  # fmt: skip

    def refused(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        route.fulfill(status=422, json={"detail": [problem]})

    page.route("**/api/auth/invitations", refused)
    open_pack(page, INBOX)
    page.goto(f"/?pack={INBOX}#admin/invitations")
    page.fill("#invite-email", "ada@example.org")
    page.get_by_role("button", name="Create link").click()
    expect(page.locator("#toast")).to_have_text(
        "an e-mail address here is ASCII (an internationalised one is refused)"
    )
    expect(page.locator("dialog")).to_have_count(0)


def test_every_people_request_names_the_organization(page: Page) -> None:
    seen = pins(page)
    administering(page)
    open_pack(page, INBOX)
    for tab in ("members", "invitations", "requests"):
        page.goto(f"/?pack={INBOX}#admin/{tab}")
        expect(page.locator(f'.tabs a[data-tab="{tab}"]')).to_have_attribute("aria-current", "page")
    page.goto(f"/?pack={INBOX}#admin/invitations")
    page.get_by_role("button", name="Revoke the invitation for ada@example.org").click()
    page.locator("dialog[open]").get_by_role("button", name="Revoke").click()
    expect(page.locator("dialog")).to_have_count(0)
    people = [(method, path, org) for method, path, org in seen if PEOPLE.match(path)]
    assert {"GET", "DELETE"} <= {method for method, _, _ in people}
    assert {org for _, _, org in people} == {"clinic-a"}, people
    moved = {"error": "OrganizationChanged", "detail": "this window shows another organization"}
    page.route("**/api/auth/members/**", lambda route: route.fulfill(status=409, json=moved))
    page.goto(f"/?pack={INBOX}#admin/members")
    row(page, "members", "Bo").get_by_role("button", name="Remove Bo").click()
    page.locator("dialog[open]").get_by_role("button", name="Remove").click()
    expect(page.locator("dialog[open] .error")).to_contain_text("another organization")
    expect(page.locator("#org-changed")).to_be_visible()
