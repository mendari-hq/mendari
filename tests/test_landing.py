# SPDX-License-Identifier: Apache-2.0
"""The developer landing at /docs (`server/landing.py`, `dashboard/docs.html`): public and static,
its examples the snippet scripts CI runs, its table of what each call needs built from the route
rules, and with sign-in on nothing about the deployment beyond its modes."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mendari.server import api_docs, landing, scopes, snippets
from mendari.server.app import DASHBOARD_DIR, create_app, mounted_routes
from mendari.server.auth import permission_for
from mendari.settings import default_providers
from tests.conftest import (
    calm_jev,
    encrypt_mode,
    encrypted_store,
    evalset_on,
    evalsets_are_on,
    member,
    signed_in,
)
from tests.test_backend_services import PUBLIC_URL
from tests.test_backend_services import on as backend_services_on

SECTIONS = ("what", "quickstart", "fhir", "guard", "auth", "more")
_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _sign_in_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "a.db"))


def test_the_landing_is_a_static_page_with_every_section_and_example(client: TestClient) -> None:
    page = client.get("/docs", follow_redirects=False)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-cache"
    # Stricter than the dashboard's policy: no style attribute anywhere, so no inline style.
    policy = page.headers["content-security-policy"]
    assert "style-src 'self';" in policy and "unsafe" not in policy
    text = page.text
    assert "<!-- landing:" not in text  # every part filled
    assert len(re.findall(r"<h1[ >]", text)) == 1
    for section in SECTIONS:
        assert f'id="{section}"' in text and f'href="#{section}"' in text, section
    assert api_docs.PRODUCT.replace("'", "&#x27;") in text
    for code, words, _ in api_docs.OUTCOMES:  # the dashboard's outcome chips
        assert f'<span class="badge o-{code}" title="{code}">{words}</span>' in text
    # Every step of every snippet file, verbatim and in order, each with a named copy button.
    at = 0
    for step in snippets.every():
        found = text.find(html.escape(step.text), at)
        assert found > at, f"{step.file}.sh {step.name} is missing or out of order"
        at = found
        button = (
            f'data-copy="code-{step.file}-{step.name}" aria-label="Copy: {html.escape(step.title)}"'
        )
        assert button in text
    assert "Sign-in is off here" in text and 'href="/auth/login"' not in text


def test_every_link_on_the_landing_goes_somewhere(client: TestClient) -> None:
    text = client.get("/docs").text
    ids = set(re.findall(r'\bid="([^"]+)"', text))
    links = set(re.findall(r'\bhref="([^"]+)"', text))
    assert links
    for link in sorted(links):
        if link.startswith("#"):
            assert link[1:] in ids, link
            continue
        assert link.startswith("/") and not link.startswith("//"), link  # this origin only
        status = client.get(html.unescape(link).split("#")[0], follow_redirects=False).status_code
        assert status < 400, (link, status)


def test_open_mode_links_every_packs_decision_card(client: TestClient) -> None:
    text = client.get("/docs").text
    packs = client.get("/api/packs").json()
    assert packs
    for pack in packs:
        assert f'href="/?pack={pack["id"]}#card"' in text, pack["id"]


def test_with_sign_in_on_the_landing_is_public_and_tells_nothing_of_the_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signed out, the page is the same whatever packs run here: no pack list, no provider, no
    count, nothing from the store. Only the mode shows, which /api/auth/me says anyway."""
    _sign_in_on(monkeypatch)
    every = TestClient(create_app(db_path=tmp_path / "a.db")).get("/docs", follow_redirects=False)
    # Nor whether Jev is set up: the quickstart shows both of the inbox's answers.
    calm_jev(monkeypatch)
    app = create_app(db_path=tmp_path / "c.db", default_providers=default_providers())
    with_jev = TestClient(app).get("/docs", follow_redirects=False)
    monkeypatch.setenv("MENDARI_PACKS", "mendari/ai-guard")
    one = TestClient(create_app(db_path=tmp_path / "b.db")).get("/docs", follow_redirects=False)
    assert every.status_code == one.status_code == with_jev.status_code == 200
    assert every.text == one.text == with_jev.text
    for printed in (landing.REVIEWED, landing.AUTOMATED):
        assert html.escape(json.dumps(printed, indent=2, ensure_ascii=False)) in every.text
    assert "Sign-in is on here" in every.text and 'href="/auth/login"' in every.text
    assert "/?pack=" not in every.text and 'href="/#card"' in every.text
    assert "Backend Services (signed client assertions" in every.text  # off here


@pytest.mark.parametrize("sign_in", [False, True], ids=["open mode", "sign-in on"])
def test_every_example_that_takes_focus_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sign_in: bool
) -> None:
    """A screen reader tabbing to an example hears what it is: its kind and its step, so the two
    answers shown for one step with sign-in on are told apart."""
    if sign_in:
        _sign_in_on(monkeypatch)
    text = TestClient(create_app(db_path=tmp_path / "a.db")).get("/docs").text
    focusable = re.findall(r"<pre tabindex=\"0\"([^>]*)>", text)
    names = [re.search(r'role="group" aria-label="([^"]+)"', attrs) for attrs in focusable]
    assert focusable and all(names), focusable
    prints = [n[1] for n in names if n and n[1].startswith("Prints")]
    assert len(prints) == (2 if sign_in else 1) and len(set(prints)) == len(prints)


def test_the_landing_describes_phase_4b_without_this_deployments_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions that end, rate limits with a 429, and requests from other sites are described,
    but a deployment's own session lengths and rates stay behind `read` (in /api/health and the
    API reference): the page is the same whatever they are set to."""
    _sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on")
    usual = TestClient(create_app(db_path=tmp_path / "a.db")).get("/docs").text
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,decide=7/min")
    monkeypatch.setenv("MENDARI_SESSION_IDLE_MINUTES", "11")
    monkeypatch.setenv("MENDARI_SESSION_MAX_HOURS", "3")
    tuned = TestClient(create_app(db_path=tmp_path / "b.db")).get("/docs").text
    assert usual == tuned
    assert "30 minutes and 8 hours unless the deployment sets otherwise" in usual
    assert "<code>429</code> with <code>Retry-After</code>" in usual
    assert "7/min" not in tuned and "11 minutes" not in tuned
    monkeypatch.delenv("MENDARI_OIDC_ISSUER")
    monkeypatch.delenv("MENDARI_OIDC_CLIENT_ID")
    for text in (usual, TestClient(create_app(db_path=tmp_path / "c.db")).get("/docs").text):
        assert "A browser request that another site starts reaches only public routes" in text
        assert "whether or not the browser sends the session cookie" in text  # logout CSRF


def _table(text: str) -> dict[tuple[str, str], str]:
    """(method, path) -> permission, as the landing's table shows them."""
    shown: dict[tuple[str, str], str] = {}
    for permission, row in re.findall(r'<tr data-permission="([a-z_]+)">(.*?)</tr>', text, re.S):
        for method, path in re.findall(r"<li><code>([A-Z]+) ([^<]+)</code>", row):
            assert (method, html.unescape(path)) not in shown
            shown[(method, html.unescape(path))] = permission
    return shown


@pytest.mark.parametrize("mode", ["open", "sign-in", "backend services", "memberships"])
def test_the_table_of_what_each_call_needs_is_the_route_rules(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With sign-in on, hidden evaluation sets are on too, so the routes they mount are in the
    table (LP-11g, conflict 37); open mode cannot have them. In memberships mode a labeller's
    routes are listed under `label`, a person's only (LP-11j), and an organization's curators'
    under `curate`, an administrator's who signed in (LP-11k)."""
    if mode in ("sign-in", "memberships"):
        _sign_in_on(monkeypatch)
    if mode == "backend services":
        backend_services_on(monkeypatch)
    if mode == "memberships":
        monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    if mode != "open":
        evalset_on(monkeypatch)
    app = create_app(db_path=tmp_path / "a.db")
    client = TestClient(app, base_url=PUBLIC_URL)
    if mode != "open":
        with encrypted_store(tmp_path / "a.db") as served:
            reader = (
                member(served, org="default", roles=["viewer"], sub="evalsets-reader")
                if mode == "memberships"
                else signed_in(served, role="viewer")
            )
        evalsets_are_on(client, reader)
    text = client.get("/docs").text
    expected = {(m, p): permission_for(m, p) for m, p in mounted_routes(app)}
    assert _table(text) == expected
    decide = re.search(r"<li><code>POST /fhir/\$decide</code>(.*?)</li>", text)
    assert decide and scopes.DECIDE in decide[1]
    # An EHR launch's own routes: who holds them, and no scoped key's note (ADR-0006).
    launch = re.search(r'<tr data-permission="launch_decide">(.*?)</tr>', text, re.S)
    assert launch and "registered here to decide" in launch[1]
    assert "<code>POST /api/smart/decide</code></li>" in launch[1]
    labelling = re.search(r'<tr data-permission="label">(.*?)</tr>', text, re.S)
    if mode == "memberships":
        assert labelling and "labeller role" in labelling[1] and "never a key" in labelling[1]
        assert {route for route, permission in expected.items() if permission == "label"} == {
            ("GET", "/api/labelling"),
            ("GET", "/api/labelling/next"),
            ("POST", "/api/labelling/{assignment_id}"),
        }
        assert "docs-scope" not in labelling[1]  # no scope reaches them
        curating = re.search(r'<tr data-permission="curate">(.*?)</tr>', text, re.S)
        assert curating and "an administrator of the organization" in curating[1]
        assert "never a key, a token or a labeller" in curating[1]
        assert {route for route, permission in expected.items() if permission == "curate"} == {
            ("GET", "/api/evalsets"),
            ("GET", "/api/evalsets/{set_id}"),
            ("POST", "/api/evalsets/{set_id}/sample"),
            ("POST", "/api/evalsets/{set_id}/assignments"),
            ("POST", "/api/evalsets/{set_id}/adjudication"),
        }
        assert "docs-scope" not in curating[1]
    else:
        assert labelling is None
        assert '<tr data-permission="curate">' not in text
    if mode == "backend services":
        assert ("POST", "/auth/token") in expected
        assert f"<code>POST {PUBLIC_URL}/auth/token</code>" in text
        assert 'href="/fhir/.well-known/smart-configuration"' in text


def test_the_smart_launch_is_described_as_it_is(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "The SMART launch from an EHR is off here" in client.get("/docs").text
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-smart")
    on = TestClient(create_app(db_path=tmp_path / "b.db")).get("/docs").text
    assert "register <code>/smart/launch</code>" in on
    assert "The SMART launch from an EHR is off here" not in on
    assert "MENDARI_SMART_EHRS" not in on  # open mode: any allow-listed EHR launches
    assert "opens the dashboard on that patient's work" in on
    _sign_in_on(monkeypatch)
    signed = TestClient(create_app(db_path=tmp_path / "c.db")).get("/docs").text
    assert "Only an EHR the deployment registers (<code>MENDARI_SMART_EHRS</code>)" in signed
    assert "It opens the dashboard on that patient's work alone." in signed


@pytest.mark.parametrize("jev", [False, True], ids=["without a model", "with jev set up"])
def test_the_quickstart_prints_what_the_server_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jev: bool
) -> None:
    """The abridged answer shown under the decide step is the server's answer to that step: a
    person reviews it without a model in the inbox's screen, and it is automated with Jev."""
    if jev:
        calm_jev(monkeypatch)
    app = create_app(db_path=tmp_path / "a.db", default_providers=default_providers())
    client = TestClient(app)
    printed = landing.AUTOMATED if jev else landing.REVIEWED
    (step,) = [s for s in snippets.load("quickstart") if s.name == "decide"]
    body = re.search(r"-d '(.*?)'", step.text, re.S)
    assert body
    ((method, path),) = step.calls()
    assert method == "POST"
    decided = client.post(path, json=json.loads(body[1])).json()
    shown = {key: value for key, value in printed.items() if key != "id"}
    assert {key: decided[key] for key in shown} == shown
    page = client.get("/docs").text
    assert html.escape(json.dumps(printed, indent=2, ensure_ascii=False)) in page


def test_the_landing_loads_only_its_own_modules(client: TestClient) -> None:
    """Its import map holds what docs.js reaches, not the dashboard's whole graph."""
    text = client.get("/docs").text
    found = re.search(r'<script type="importmap">(.*?)</script>', text)
    assert found
    stamped = json.loads(found[1])["imports"]
    assert sorted(stamped) == ["/static/docs.js", "/static/theme.js", "/static/ui.js"]
    for url in stamped.values():
        assert f'<link rel="modulepreload" href="{url}">' in text
        assert client.get(url).status_code == 200
    assert f'<script type="module" src="{stamped["/static/docs.js"]}">' in text


def test_its_theme_script_is_the_dashboards() -> None:
    """Both pages resolve the theme before first paint the same way (theme.js keeps it after)."""
    dashboard = _INLINE.findall((DASHBOARD_DIR / "index.html").read_text())[0]
    assert _INLINE.findall((DASHBOARD_DIR / "docs.html").read_text())[0] == dashboard


def test_a_marker_and_its_part_go_together() -> None:
    assert landing.fill("<p><!-- landing:a --></p>", {"a": "x"}) == "<p>x</p>"
    with pytest.raises(RuntimeError, match="markers"):
        landing.fill("<!-- landing:a -->", {})
    with pytest.raises(RuntimeError, match="markers"):
        landing.fill("<!-- landing:a -->", {"a": "x", "b": "y"})
    with pytest.raises(RuntimeError, match="markers"):
        landing.fill("<!-- landing:a --><!-- landing:a -->", {"a": "x"})


def test_the_api_reference_sends_newcomers_to_the_landing() -> None:
    text = api_docs.description(sign_in=False, default_pack=None)
    assert text.startswith("**New here?** [Start at `/docs`](/docs)")
    assert all(f"**`{code}`**" in text for code, _, _ in api_docs.OUTCOMES)
    # Like the landing, it says another site's form is refused with or without the cookie.
    assert "whether or not the browser sends the session cookie" in " ".join(text.split())
