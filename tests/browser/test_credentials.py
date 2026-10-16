# SPDX-License-Identifier: Apache-2.0
"""API keys and Backend Services clients in the dashboard's Access view (LP-12h): keys made from
a preset or by hand and shown once, rotated, revoked and their history; clients registered with
a public key (a PEM one turned into a JWK in the browser, a private one refused before anything
is sent), their key set replaced, revoked and their history; in memberships mode and, for an
administrator by claims, in claims mode (keys and clients only).

Most tests run a real server at `localhost` (tests/browser/access_site.py) with sessions planted
as `/auth/callback` makes them; one mocks a memberships administrator over the open-mode test
server, as tests/browser/test_invitations.py does. Every address is at example.org, and every key
is generated for the test. Run with `uv run pytest -m browser`."""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from playwright.sync_api import Browser, Page, Request, Route, expect

from mendari.core.audit import _thumbprint
from mendari.server.presets import PRESETS
from tests.browser.access_site import (
    Site,
    access,
    dialog,
    lead,
    memberships,
    row,
    running,
)
from tests.browser.test_access import member_of, pins
from tests.browser.test_accessibility import violations
from tests.browser.test_dashboard import INBOX, PHONE, SMALL_TARGETS, open_pack
from tests.conftest import DASHBOARD, encrypt_mode, security_lines, signed_in
from tests.test_auth import CLIENT, IDP
from tests.test_backend_services import EC_KEY, RSA_KEY, Signer

pytestmark = pytest.mark.browser

KEY = re.compile(r"ohk_[A-Za-z0-9_-]{43}")
DOCS_SITE = "https://mendari-hq.github.io/mendari/"  # mkdocs.yml site_url
MARKUP = '<img src=x onerror="window.pwned=1">'
KID_MARKUP = "<img/src/onerror=window.pwned=1>"  # a kid is visible ASCII without spaces
PEOPLE = re.compile(r"^/api/auth/(members|invitations|access-requests)(/|$)")
CREDENTIALS = re.compile(r"^/api/auth/(keys|clients)(/|$)")
LEAD = "lead@example.org"  # Clinic-A's administrator, as the chain records them
# How the dashboard shows a time (ui.js timeFormatFor, the deployment's default zone).
SHOWN = """(iso) => new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", year: "numeric", month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit", timeZoneName: "short",
}).format(new Date(iso))"""


def backend_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """On, addressed at `localhost` (no browser test calls the token endpoint)."""
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "http://localhost")


@pytest.fixture
def site(browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    """Memberships mode with Backend Services on: every tab."""
    memberships(monkeypatch)
    backend_services(monkeypatch)
    with running(browser, tmp_path / "a.db") as here:
        yield here


@pytest.fixture
def without_clients(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Site]:
    """Memberships mode, Backend Services off."""
    memberships(monkeypatch)
    with running(browser, tmp_path / "a.db") as here:
        yield here


@pytest.fixture
def claims_site(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Site]:
    """Claims mode (sign-in, no records) with Backend Services on."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    backend_services(monkeypatch)
    with running(browser, tmp_path / "a.db") as here:
        yield here


def admin(site: Site, **options: Any) -> Page:
    return site.window(lead(site), **options)


def make_key(site: Site, name: str = "ehr", **options: Any) -> dict[str, Any]:
    """A key of Clinic-A's, made by its administrator (`lead` first)."""
    made: dict[str, Any] = site.store.create_api_key(
        name=name, role="service", org="clinic-a", created_by=LEAD, **options
    )
    return made


def make_client(site: Site, name: str = "bulk", keys: tuple[dict[str, Any], ...] = ()) -> str:
    made = site.store.register_client(
        name=name, role="viewer", org="clinic-a", created_by=LEAD, scopes="system/*.rs",
        jwks=list(keys) or [RSA_KEY.jwk],
    )  # fmt: skip
    return str(made["client_id"])


def stored_key(site: Site, key_id: str) -> dict[str, Any]:
    [found] = [k for k in site.store.api_keys("clinic-a") if k["id"] == key_id]
    return found


def doc_url(doc: str) -> str:
    """A preset's documentation page on the docs site, as mkdocs serves it."""
    page, _, anchor = doc.partition("#")
    return f"{DOCS_SITE}{page.removesuffix('.md')}/#{anchor}"


def public_pem(signer: Signer) -> str:
    return str(
        signer.private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def private_pem(signer: Signer) -> str:
    return signer.pem()


def private_der(signer: Signer, layout: serialization.PrivateFormat) -> str:
    """A private key's DER in base64, as a tool that prints it bare would."""
    der = signer.private.private_bytes(
        serialization.Encoding.DER, layout, serialization.NoEncryption()
    )
    return base64.b64encode(der).decode()


def private_jwk(signer: Signer) -> dict[str, Any]:
    family = jwt.algorithms.RSAAlgorithm if signer.alg == "RS384" else jwt.algorithms.ECAlgorithm
    return {**json.loads(family.to_jwk(signer.private)), "kid": signer.kid}


def jwk_without_kid(signer: Signer) -> dict[str, Any]:
    return {k: v for k, v in signer.jwk.items() if k != "kid"}


def create_key(page: Page, name: str, preset: str | None = "rest") -> str:
    """Fill and send the create form; the key its dialog shows."""
    page.fill("#key-name", name)
    if preset is None:
        page.select_option("#key-preset", label="Custom")
    else:
        page.select_option("#key-preset", preset)
    page.get_by_role("button", name="Create key").click()
    expect(dialog(page).locator("#key-title")).to_have_text(f"API key {name}")
    return dialog(page).locator("#key-secret").input_value()


def posts(page: Page, prefix: str = "/api/auth/clients") -> list[str]:
    """Every POST `page` sends under `prefix`, by path."""
    sent: list[str] = []

    def record(request: Request) -> None:
        path = urllib.parse.urlsplit(request.url).path
        if request.method == "POST" and path.startswith(prefix):
            sent.append(path)

    page.on("request", record)
    return sent


def harmless(page: Page) -> None:
    """Nothing became markup, and nothing ran (an image that fails runs its handler late)."""
    expect(page.locator("main img, dialog img")).to_have_count(0)
    page.wait_for_timeout(200)
    assert page.evaluate("window.pwned") is None


# -- keys ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("preset", ["rest", "fhir", "fhir-reader", "custom"])
def test_a_key_is_made_from_each_kind_of_preset(site: Site, preset: str) -> None:
    """The whole role (`rest`), a scoped service key, a viewer's, and one made by hand."""
    page = admin(site)
    access(page, "keys")
    presets = page.request.get("/api/auth/keys/presets", headers=DASHBOARD)  # as its pages ask
    served = {p["id"]: p for p in presets.json()}
    expected: tuple[str, str | None, str | None]
    if preset == "custom":
        page.select_option("#key-preset", label="Custom")
        page.locator('input[name="key-role"][value="viewer"]').check()
        page.fill("#key-scope", "system/Task.rs")
        expected = ("viewer", "system/Task.rs", None)
    else:
        page.select_option("#key-preset", preset)
        chosen = PRESETS[preset]
        expected = (chosen.role, chosen.scope, preset)
        detail = page.locator("#key-preset-detail")
        expect(detail.locator("summary")).to_have_text(
            f"Reaches {len(served[preset]['reaches'])} routes"
        )
        link = detail.get_by_role("link", name="How to use it")
        expect(link).to_have_attribute("href", doc_url(chosen.doc))
        expect(link).to_have_attribute("target", "_blank")
        expect(link).to_have_attribute("rel", "noopener noreferrer")
    page.fill("#key-name", f"ehr {preset}")
    page.get_by_role("button", name="Create key").click()
    secret = dialog(page).locator("#key-secret").input_value()
    assert KEY.fullmatch(secret), secret
    found = site.store.api_key(secret)
    assert found is not None
    assert (found["role"], found["scope"]) == expected[:2]
    assert stored_key(site, str(found["id"]))["preset"] == expected[2]
    dialog(page).get_by_role("button", name="Done").click()
    listed = row(page, "keys", f"ehr {preset}")
    expect(listed).to_contain_text("Active")
    expect(listed).to_contain_text("You")
    if preset == "custom":
        expect(listed).to_contain_text("system/Task.rs")
    else:
        expect(listed).to_contain_text(PRESETS[preset].title)


def test_a_key_is_shown_once_and_kept_nowhere(
    site: Site, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    page = admin(site)
    console: list[str] = []
    page.on("console", lambda message: console.append(message.text))
    access(page, "keys")
    secret = create_key(page, "ehr")
    shown = dialog(page)
    expect(shown).to_contain_text("Shown once.")
    expect(shown).to_contain_text("Mendari keeps only a digest")
    assert secret not in page.evaluate("document.documentElement.outerHTML")  # no attribute
    assert "ohk_" not in page.evaluate("document.documentElement.outerHTML")
    shown.get_by_role("button", name="Done").click()
    expect(page.locator("dialog")).to_have_count(0)
    assert "ohk_" not in page.content()
    kept = page.evaluate(
        "JSON.stringify([{...localStorage}, {...sessionStorage}, document.cookie])"
    )
    assert "ohk_" not in kept
    assert not [c for c in page.context.cookies() if "ohk_" in json.dumps(c)]
    # Leaving the view takes a key shown off the page too.
    create_key(page, "second")
    page.evaluate("location.hash = '#overview'")
    expect(page.locator("dialog")).to_have_count(0)
    assert "ohk_" not in page.content()
    access(page, "keys")  # loaded again: listed, never shown
    expect(row(page, "keys", "second")).to_contain_text("Active")
    assert "ohk_" not in page.content()
    assert not [text for text in console if "ohk_" in text]
    out, err = capsys.readouterr()
    lines = security_lines(out)
    assert [line["event"] for line in lines if line["event"].startswith("admin.")] == [
        "admin.key.created",
        "admin.key.created",
    ]
    assert not [line for line in lines if "ohk_" in json.dumps(line)]
    assert "ohk_" not in out and "ohk_" not in err


def test_a_refused_scope_reads_as_its_message(site: Site) -> None:
    page = admin(site)
    access(page, "keys")
    page.select_option("#key-preset", label="Custom")
    page.fill("#key-scope", "system/Patient.rs")
    page.fill("#key-name", "ehr")
    page.get_by_role("button", name="Create key").click()
    expect(page.locator("#toast")).to_have_text(
        "scope 'system/Patient.rs' is not granted here: Mendari serves no Patient resources"
    )
    expect(page.locator("dialog")).to_have_count(0)
    assert site.store.api_keys("clinic-a") == []


def test_rotating_shows_the_new_key_once_and_when_the_old_one_stops(site: Site) -> None:
    page = admin(site)
    old = make_key(site)
    access(page, "keys")
    row(page, "keys", "ehr").get_by_role("button", name="Rotate ehr").click()
    asking = dialog(page)
    expect(asking.locator("h2")).to_have_text("Rotate ehr")
    asking.locator("#rotate-overlap").fill("")
    asking.get_by_role("button", name="Rotate").click()
    expect(asking.get_by_role("alert")).to_have_text("The overlap is a whole number from 0 to 168.")
    asking.locator("#rotate-overlap").fill("2")
    asking.locator("#rotate-days").fill("1")
    asking.locator("#rotate-overlap").fill("48")
    asking.get_by_role("button", name="Rotate").click()
    expect(asking.get_by_role("alert")).to_have_text(
        "The overlap is longer than the new key lives."
    )
    assert stored_key(site, old["id"])["status"] == "active"  # refused in the browser
    asking.locator("#rotate-days").fill("90")
    asking.locator("#rotate-overlap").fill("2")
    asking.get_by_role("button", name="Rotate").click()
    secret = dialog(page).locator("#key-secret").input_value()
    assert KEY.fullmatch(secret) and secret != old["key"]
    new = site.store.api_key(secret)
    assert new is not None
    before = stored_key(site, old["id"])
    assert before["replaced_by"] == new["id"] and before["status"] == "replaced"
    stops = datetime.fromisoformat(before["expires_at"]) - datetime.now(UTC)
    assert timedelta(hours=1, minutes=55) < stops <= timedelta(hours=2)
    until = page.evaluate(SHOWN, before["expires_at"])
    expect(dialog(page)).to_contain_text(f"ehr (the old key) keeps working until {until}.")
    dialog(page).get_by_role("button", name="Done").click()
    replaced = row(page, "keys", "Replaced")
    expect(replaced).to_contain_text(f"Stops {until}")
    expect(replaced.get_by_role("button", name="Rotate ehr")).to_have_count(0)
    expect(row(page, "keys", "Active").get_by_role("button", name="Rotate ehr")).to_have_count(1)


def test_revoking_a_key_asks_first_and_it_stops(site: Site) -> None:
    page = admin(site)
    made = make_key(site)
    access(page, "keys")
    listed = row(page, "keys", "ehr")
    listed.get_by_role("button", name="Revoke ehr").click()
    expect(dialog(page).locator("h2")).to_have_text("Revoke ehr?")
    expect(dialog(page)).to_contain_text("This can't be undone")
    dialog(page).get_by_role("button", name="Cancel").click()
    expect(page.locator("dialog")).to_have_count(0)
    assert site.store.api_key(made["key"]) is not None
    listed.get_by_role("button", name="Revoke ehr").click()
    dialog(page).get_by_role("button", name="Revoke").click()
    expect(listed).to_contain_text("Revoked")
    assert site.store.api_key(made["key"]) is None
    expect(listed.get_by_role("button")).to_have_text(["History"])


def test_a_keys_history_follows_its_rotation(site: Site) -> None:
    page = admin(site)
    old = make_key(site, preset="fhir", scopes=PRESETS["fhir"].scope)
    new = site.store.rotate_api_key(old["id"], rotated_by=LEAD, overlap_hours=2)
    access(page, "keys")
    row(page, "keys", "Replaced").get_by_role("button", name="History of ehr").click()
    shown = dialog(page)
    expect(shown.locator("h2")).to_have_text("History of ehr")
    lines = shown.locator("ol.history > li")
    expect(lines).to_have_count(2)
    expect(lines.nth(0)).to_contain_text("Created by You (Service, preset FHIR in, FHIR out)")
    expect(lines.nth(1)).to_contain_text(f"Rotated by You: replaced by key {new['id']}")
    expect(lines.nth(1)).to_contain_text("(overlap 2 h;")
    shown.get_by_role("button", name=new["id"]).click()
    expect(lines).to_have_count(1)
    expect(lines.nth(0)).to_contain_text(f"replacing key {old['id']}")
    expect(shown.locator("h2")).to_be_focused()
    shown.get_by_role("button", name="Close").click()
    expect(page.locator("dialog")).to_have_count(0)


def held(page: Page, path: str) -> list[Route]:
    """POSTs to `path` held until the test lets them go (`route.continue_()`)."""
    waiting: list[Route] = []

    def hold(route: Route) -> None:
        if route.request.method == "POST":
            waiting.append(route)
        else:
            route.fallback()

    page.route(f"**{path}", hold)
    return waiting


def released(page: Page, waiting: list[Route]) -> None:
    for _ in range(100):
        if waiting:
            break
        page.wait_for_timeout(50)
    assert waiting, "the POST was never sent"
    waiting[0].continue_()


@pytest.mark.parametrize("where", ["the clients tab", "another view"])
def test_a_key_made_while_leaving_its_tab_is_still_shown_once(site: Site, where: str) -> None:
    """The administrator moves on before the key's answer comes: the key exists, so it is shown
    once all the same (a key made and never shown is lost), and nothing fails unseen."""
    page = admin(site)
    failures: list[str] = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    access(page, "keys")
    waiting = held(page, "/api/auth/keys")
    page.fill("#key-name", "ehr")
    page.get_by_role("button", name="Create key").click()
    if where == "the clients tab":
        page.locator('.tabs a[data-tab="clients"]').click()
        expect(page.locator("#client-form")).to_be_visible()
    else:
        page.locator('.nav a[data-view="overview"]').click()
        expect(page.locator('.nav a[data-view="overview"]')).to_have_class(re.compile("active"))
    released(page, waiting)
    expect(dialog(page).locator("#key-title")).to_have_text("API key ehr")
    secret = dialog(page).locator("#key-secret").input_value()
    assert KEY.fullmatch(secret), secret
    found = site.store.api_key(secret)
    assert found is not None and found["name"] == "ehr"
    dialog(page).get_by_role("button", name="Done").click()
    expect(page.locator("dialog")).to_have_count(0)
    assert "ohk_" not in page.content()
    if where == "the clients tab":
        expect(page.locator('.tabs a[data-tab="clients"]')).to_have_attribute(
            "aria-current", "page"
        )
    assert failures == []


def test_a_client_registered_while_leaving_its_tab_is_said(site: Site) -> None:
    page = admin(site)
    failures: list[str] = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    access(page, "clients")
    waiting = held(page, "/api/auth/clients")
    page.fill("#client-name", "bulk")
    page.fill("#client-keys", json.dumps({"keys": [RSA_KEY.jwk]}))
    page.get_by_role("button", name="Register client").click()
    page.locator('.tabs a[data-tab="keys"]').click()
    expect(page.locator("#key-form")).to_be_visible()
    released(page, waiting)
    expect(page.locator("#toast")).to_have_text("Client bulk registered.")
    assert [c["name"] for c in site.store.clients("clinic-a")] == ["bulk"]
    expect(page.locator('.tabs a[data-tab="keys"]')).to_have_attribute("aria-current", "page")
    assert failures == []


# -- the notice, mocked: memberships mode over the open-mode test server -------------------------

CLINIC_A = [{"org": "clinic-a", "name": "Clinic A", "roles": ["admin"]}]
LATER = "2099-01-01T12:00:00+00:00"
KEY_ROW = {
    "id": "k1", "name": "ehr", "role": "service", "org": "clinic-a", "scope": None,
    "created_by": "kim@clinic.example.org", "created_at": "2026-10-01T12:00:00+00:00",
    "expires_at": LATER, "revoked_at": None, "revoked_by": None, "replaced_by": None,
    "preset": "rest", "status": "active",
}  # fmt: skip
MADE_SECRET = "ohk_" + "B" * 43
KEY_MADE = {**KEY_ROW, "key": MADE_SECRET}
ROTATED_SECRET = "ohk_" + "C" * 43
ROTATED = {
    **KEY_ROW,
    "id": "k2",
    "key": ROTATED_SECRET,
    "replaces": {"id": "k1", "expires_at": LATER},
}
CLIENT_ID = "c" * 24
CLIENT_ROW = {
    "client_id": CLIENT_ID, "name": "bulk", "role": "viewer", "org": "clinic-a",
    "scope": "system/*.rs", "keys": [{"kid": "ec-1", "kty": "EC", "thumbprint": "t"}],
    "created_by": "kim@clinic.example.org", "created_at": "2026-10-01T12:00:00+00:00",
    "revoked_at": None, "revoked_by": None, "keys_replaced_at": None,
}  # fmt: skip


def test_creating_rotating_and_replacing_wait_for_the_notice(page: Page) -> None:
    """A new notice version since the dashboard opened: each POST that mints is refused once,
    the acknowledgement asked (over the dialog that sent it, for rotating and replacing), and
    the POST sent again, once."""
    member_of(page, CLINIC_A, "clinic-a", ["admin"])
    required = {"now": False}
    posted: dict[str, list[str]] = {}

    def health(route: Route) -> None:
        answer = route.fetch()
        route.fulfill(
            json={
                **answer.json(),
                "backend_services": {"token_endpoint": "https://m.example.org/auth/token"},
            }
        )

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

    def minting(made: dict[str, Any], status: int, listed: list[Any]) -> Any:
        def handle(route: Route) -> None:
            request = route.request
            path = urllib.parse.urlsplit(request.url).path
            if request.method == "GET":
                route.fulfill(json=listed)
                return
            sent = posted.setdefault(path, [])
            sent.append(request.post_data or "")
            if len(sent) == 1:  # a new notice version since the dashboard opened
                required["now"] = True
                refused = {"error": "AuthError", "detail": "acknowledge the notice first"}
                route.fulfill(status=403, headers={"Mendari-Notice-Required": "1"}, json=refused)
            else:
                route.fulfill(status=status, json=made)

        return handle

    people = re.compile(r"/api/auth/(members|invitations|access-requests)(\?|$)")
    page.route(people, lambda route: route.fulfill(json=[]))  # its people's lists: empty
    page.route("**/api/health", health)
    page.route("**/api/notice", notice)
    page.route("**/api/notice/acknowledge", acknowledge)
    page.route("**/api/auth/keys", minting(KEY_MADE, 201, [KEY_ROW]))
    page.route("**/api/auth/keys/k1/rotate", minting(ROTATED, 201, []))
    page.route("**/api/auth/clients", minting(CLIENT_ROW, 201, [CLIENT_ROW]))
    replaced = {"client_id": CLIENT_ID, "tokens_dropped": 1}
    page.route(f"**/api/auth/clients/{CLIENT_ID}/keys", minting(replaced, 200, []))
    acknowledging = page.get_by_role("button", name="I understand: synthetic data only")

    open_pack(page, INBOX)
    page.goto(f"/?pack={INBOX}#admin/keys")
    page.fill("#key-name", "ehr")
    page.get_by_role("button", name="Create key").click()
    acknowledging.click()
    expect(page.locator("#key-secret")).to_have_value(MADE_SECRET)
    dialog(page).get_by_role("button", name="Done").click()

    row(page, "keys", "ehr").get_by_role("button", name="Rotate ehr").click()
    dialog(page).get_by_role("button", name="Rotate").click()
    acknowledging.click()  # over the rotate dialog
    expect(page.locator("#key-secret")).to_have_value(ROTATED_SECRET)
    dialog(page).get_by_role("button", name="Done").click()

    page.locator('.tabs a[data-tab="clients"]').click()
    row(page, "clients", "bulk").get_by_role("button", name="Replace keys of bulk").click()
    dialog(page).locator("#replace-keys").fill(json.dumps({"keys": [EC_KEY.jwk]}))
    dialog(page).get_by_role("button", name="Replace keys").click()
    acknowledging.click()
    expect(page.locator("#toast")).to_have_text("Keys of bulk replaced; 1 token dropped.")
    expect(page.locator("dialog")).to_have_count(0)

    assert {path: len(bodies) for path, bodies in posted.items()} == {
        "/api/auth/keys": 2,
        "/api/auth/keys/k1/rotate": 2,
        f"/api/auth/clients/{CLIENT_ID}/keys": 2,
    }
    assert json.loads(posted["/api/auth/keys"][1]) == {
        "name": "ehr", "role": "service", "preset": "rest", "days": 90,
    }  # fmt: skip
    assert json.loads(posted["/api/auth/keys/k1/rotate"][1]) == {"overlap_hours": 24, "days": 90}
    assert json.loads(posted[f"/api/auth/clients/{CLIENT_ID}/keys"][1]) == {
        "jwks": {"keys": [EC_KEY.jwk]}
    }


# -- clients ------------------------------------------------------------------------------------


def test_a_client_is_registered_its_keys_replaced_and_revoked(site: Site) -> None:
    page = admin(site)
    access(page, "clients")
    expect(page.locator("#admin-body")).to_contain_text(
        "Token endpoint: http://localhost/auth/token"
    )
    page.fill("#client-name", "bulk")
    page.select_option("#client-preset", "fhir")
    expect(page.locator('#client-preset option[value="rest"]')).to_have_count(0)  # keys only
    page.fill("#client-keys", public_pem(RSA_KEY))
    page.get_by_role("button", name="Register client").click()
    expect(page.locator("#toast")).to_have_text("Client bulk registered.")
    [client] = site.store.clients("clinic-a")
    thumbprint = _thumbprint(jwk_without_kid(RSA_KEY))
    assert [(k["kid"], k["kty"]) for k in client["keys"]] == [(thumbprint, "RSA")]
    assert (client["role"], client["scope"]) == ("service", PRESETS["fhir"].scope)
    listed = row(page, "clients", "bulk")
    expect(listed).to_contain_text(client["client_id"])
    expect(listed).to_contain_text(f"{thumbprint} (RSA)")
    expect(listed).to_contain_text("FHIR in, FHIR out")
    found = site.store.client(client["client_id"])
    assert found is not None
    token, _ = site.store.issue_token(
        client["client_id"], "system/Task.rs", 300, keys_version=found["keys_version"]
    )
    assert site.store.access_token(token) is not None

    listed.get_by_role("button", name="Replace keys of bulk").click()
    asking = dialog(page)
    expect(asking).to_contain_text(f"Its keys now: {thumbprint} (RSA).")
    expect(asking).to_contain_text("The tokens it holds stop at once")
    asking.locator("#replace-keys").fill(json.dumps({"keys": [EC_KEY.jwk]}))
    asking.get_by_role("button", name="Replace keys").click()
    expect(page.locator("#toast")).to_have_text("Keys of bulk replaced; 1 token dropped.")
    assert site.store.access_token(token) is None
    expect(listed).to_contain_text("ec-1 (EC)")
    expect(listed).to_contain_text("replaced ")
    listed.get_by_role("button", name="History of bulk").click()
    lines = dialog(page).locator("ol.history > li")
    expect(lines).to_have_count(2)
    expect(lines.nth(0)).to_contain_text(f"Registered by You with keys {thumbprint}")
    expect(lines.nth(1)).to_contain_text(
        f"Keys replaced by You: {thumbprint} → ec-1; 1 token dropped"
    )
    dialog(page).get_by_role("button", name="Close").click()

    listed.get_by_role("button", name="Revoke bulk").click()
    expect(dialog(page)).to_contain_text("Its tokens stop at once and it gets no new one.")
    dialog(page).get_by_role("button", name="Revoke").click()
    expect(listed).to_contain_text("Revoked")
    expect(listed.get_by_role("button", name="History of bulk")).to_be_visible()
    expect(listed.get_by_role("button", name="Replace keys of bulk")).to_have_count(0)
    assert site.store.client(client["client_id"]) is None


PRIVATE_PASTES = {
    "a PEM private key": lambda: private_pem(EC_KEY),
    "a JWK with d": lambda: json.dumps(private_jwk(EC_KEY)),
    "a set with one private": lambda: json.dumps(
        {"keys": [RSA_KEY.jwk, {"kty": "oct", "kid": "h", "k": "c2VjcmV0"}]}
    ),
    "a JSON array holding a private JWK": lambda: json.dumps([RSA_KEY.jwk, private_jwk(RSA_KEY)]),
    "a JWK with d and no kty": lambda: json.dumps(
        {k: v for k, v in private_jwk(EC_KEY).items() if k != "kty"}
    ),
    "a bare PKCS#8 DER": lambda: private_der(RSA_KEY, serialization.PrivateFormat.PKCS8),
    "a bare SEC 1 DER": lambda: private_der(EC_KEY, serialization.PrivateFormat.TraditionalOpenSSL),
    "a private DER under a PUBLIC KEY header": lambda: (
        "-----BEGIN PUBLIC KEY-----\n"
        f"{private_der(RSA_KEY, serialization.PrivateFormat.TraditionalOpenSSL)}\n"
        "-----END PUBLIC KEY-----"
    ),
}


@pytest.mark.parametrize("pasted", list(PRIVATE_PASTES))
def test_a_private_key_is_refused_before_it_leaves_the_browser(site: Site, pasted: str) -> None:
    text = PRIVATE_PASTES[pasted]()
    said = "That is a private key: paste the public key only, and treat this one as exposed."
    client_id = make_client(site)
    page = admin(site)
    sent = posts(page)
    access(page, "clients")
    page.fill("#client-name", "bulk")
    page.fill("#client-keys", text)
    page.get_by_role("button", name="Register client").click()
    expect(page.locator("#toast")).to_have_text(said)
    expect(page.locator("#client-keys")).to_have_value("")  # off the page
    row(page, "clients", client_id).get_by_role("button", name="Replace keys of bulk").click()
    dialog(page).locator("#replace-keys").fill(text)
    dialog(page).get_by_role("button", name="Replace keys").click()
    expect(dialog(page).get_by_role("alert")).to_have_text(said)
    expect(dialog(page).locator("#replace-keys")).to_have_value("")
    dialog(page).get_by_role("button", name="Cancel").click()
    assert sent == []  # nothing was sent
    assert len(site.store.clients("clinic-a")) == 1


def test_what_is_not_a_key_is_said_and_never_sent(site: Site) -> None:
    page = admin(site)
    sent = posts(page)
    access(page, "clients")
    page.fill("#client-name", "bulk")
    for text, said in (
        ("ssh-rsa AAAA", "Paste a JWK Set, a JWK or a PEM public key."),
        ("[1, 2]", "Paste a JWK Set, a JWK or a PEM public key."),
        ("-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----", "That PEM is not"),
    ):
        page.fill("#client-keys", text)
        page.get_by_role("button", name="Register client").click()
        expect(page.locator("#toast")).to_contain_text(said)
    assert sent == []


def test_the_clients_tab_is_absent_without_backend_services(without_clients: Site) -> None:
    page = admin(without_clients)
    asked: list[str] = []
    page.on("request", lambda request: asked.append(urllib.parse.urlsplit(request.url).path))
    access(page, "keys")
    expect(page.locator(".tabs a")).to_have_text(
        ["Members", "Invitations", "Requests to join", "API keys", "Usage", "Activity"]
    )
    page.goto("about:blank")
    page.goto("/#admin/clients")  # the first tab instead
    expect(page.locator('.tabs a[data-tab="members"]')).to_have_attribute("aria-current", "page")
    assert not [path for path in asked if path.startswith("/api/auth/clients")]


def test_claims_mode_shows_keys_and_clients_but_not_people(claims_site: Site) -> None:
    page = claims_site.window(signed_in(claims_site.store, role="admin", host="localhost"))
    seen = pins(page)
    page.goto("/")
    link = page.locator('.nav a[data-view="admin"]')
    expect(link).to_be_visible()
    link.click()
    expect(page.locator("main h1")).to_have_text("Access")
    expect(page.locator(".tabs a")).to_have_text(["API keys", "Clients", "Usage", "Activity"])
    expect(page.locator("main")).to_contain_text(
        "API keys and Backend Services clients: the credentials machines use here."
    )
    secret = create_key(page, "ehr")
    assert KEY.fullmatch(secret)
    dialog(page).get_by_role("button", name="Done").click()
    expect(row(page, "keys", "ehr")).to_contain_text("You")
    expect(page.locator('[data-table="keys"] caption')).to_have_text("API keys, oldest first")
    page.locator('.tabs a[data-tab="clients"]').click()
    expect(page.locator("#client-form")).to_be_visible()
    page.goto("about:blank")
    page.goto("/#admin/members")  # not a tab here: the first one
    expect(page.locator('.tabs a[data-tab="keys"]')).to_have_attribute("aria-current", "page")
    assert not [path for _, path, _ in seen if PEOPLE.match(path)]
    assert [path for _, path, _ in seen if CREDENTIALS.match(path)]
    assert {org for _, _, org in seen} == {None}  # claims mode names no organization
    made = claims_site.store.api_keys("default")
    assert [(k["name"], k["created_by"]) for k in made] == [("ehr", "admin@default.example.org")]


def test_a_viewer_by_claims_sees_no_access_view(claims_site: Site) -> None:
    page = claims_site.window(signed_in(claims_site.store, role="viewer", host="localhost"))
    seen = pins(page)
    page.goto("/#admin/keys")
    expect(page.locator('.nav a[data-view="overview"]')).to_have_class(re.compile("active"))
    expect(page.locator('.nav a[data-view="admin"]')).to_be_hidden()
    assert not [path for _, path, _ in seen if CREDENTIALS.match(path)]


# -- markup, the organization each request names, accessibility, phones --------------------------


def test_names_are_text_never_markup(site: Site) -> None:
    """A key's and a client's name, a scope, a key id and who made them can hold markup: shown as
    text in the lists, every dialog and both histories, in a browser that would run it."""
    old = make_key(site, name=f"Key {MARKUP}")
    with site.store._db.session(exclusive=True) as db:
        db.all("UPDATE api_keys SET created_by = ? WHERE id = ?", (f"Mallory {MARKUP}", old["id"]))
    site.store.rotate_api_key(old["id"], rotated_by=f"Mallory {MARKUP}", overlap_hours=2)
    make_key(site, name="scoped", scopes=f"system/Task.rs {MARKUP}")  # stored as it is
    marked = {**EC_KEY.jwk, "kid": KID_MARKUP}
    client_id = make_client(site, name=f"Client {MARKUP}", keys=(marked,))
    page = admin(site, bypass_csp=True)
    access(page, "keys")
    expect(row(page, "keys", "Replaced")).to_contain_text(f"Key {MARKUP}")
    expect(row(page, "keys", "Replaced")).to_contain_text(f"Mallory {MARKUP}")
    expect(row(page, "keys", "scoped")).to_contain_text(f"system/Task.rs {MARKUP}")
    harmless(page)
    replaced = row(page, "keys", "Replaced")
    replaced.get_by_role("button", name=f"History of Key {MARKUP}").click()
    expect(dialog(page).locator("h2")).to_have_text(f"History of Key {MARKUP}")
    expect(dialog(page)).to_contain_text(f"Rotated by Mallory {MARKUP}")
    harmless(page)
    dialog(page).get_by_role("button", name="Close").click()
    row(page, "keys", "scoped").get_by_role("button", name="History of scoped").click()
    expect(dialog(page)).to_contain_text(f"scope system/Task.rs {MARKUP}")
    harmless(page)
    dialog(page).get_by_role("button", name="Close").click()
    replaced.get_by_role("button", name=f"Revoke Key {MARKUP}").click()
    expect(dialog(page).locator("h2")).to_have_text(f"Revoke Key {MARKUP}?")
    harmless(page)
    dialog(page).get_by_role("button", name="Cancel").click()
    active = row(page, "keys", "Active")
    active.get_by_role("button", name=f"Rotate Key {MARKUP}").click()
    expect(dialog(page).locator("h2")).to_have_text(f"Rotate Key {MARKUP}")
    dialog(page).get_by_role("button", name="Rotate").click()
    expect(dialog(page).locator("#key-title")).to_have_text(f"API key Key {MARKUP}")
    expect(dialog(page)).to_contain_text(f"Key {MARKUP} (the old key) keeps working until")
    harmless(page)
    dialog(page).get_by_role("button", name="Done").click()
    create_key(page, f"New {MARKUP}")
    harmless(page)
    dialog(page).get_by_role("button", name="Done").click()

    page.locator('.tabs a[data-tab="clients"]').click()
    listed = row(page, "clients", client_id)
    expect(listed).to_contain_text(f"Client {MARKUP}")
    expect(listed).to_contain_text(f"{KID_MARKUP} (EC)")
    harmless(page)
    listed.get_by_role("button", name=f"Replace keys of Client {MARKUP}").click()
    expect(dialog(page)).to_contain_text(f"Its keys now: {KID_MARKUP} (EC).")
    harmless(page)
    dialog(page).get_by_role("button", name="Cancel").click()
    listed.get_by_role("button", name=f"History of Client {MARKUP}").click()
    expect(dialog(page)).to_contain_text(f"with keys {KID_MARKUP}")
    harmless(page)


def test_every_credential_request_names_the_organization(site: Site) -> None:
    make_key(site)
    page = admin(site)
    seen = pins(page)
    access(page, "keys")
    create_key(page, "made here")
    dialog(page).get_by_role("button", name="Done").click()
    row(page, "keys", "made here").get_by_role("button", name="History of made here").click()
    dialog(page).get_by_role("button", name="Close").click()
    row(page, "keys", "made here").get_by_role("button", name="Rotate made here").click()
    dialog(page).get_by_role("button", name="Rotate").click()
    dialog(page).get_by_role("button", name="Done").click()
    row(page, "keys", "Replaced").get_by_role("button", name="Revoke made here").click()
    dialog(page).get_by_role("button", name="Revoke").click()
    expect(row(page, "keys", "Revoked")).to_have_count(1)
    page.locator('.tabs a[data-tab="clients"]').click()
    page.fill("#client-name", "bulk")
    page.fill("#client-keys", json.dumps({"keys": [RSA_KEY.jwk]}))
    page.get_by_role("button", name="Register client").click()
    listed = row(page, "clients", "bulk")
    listed.get_by_role("button", name="Replace keys of bulk").click()
    dialog(page).locator("#replace-keys").fill(json.dumps(EC_KEY.jwk))
    dialog(page).get_by_role("button", name="Replace keys").click()
    expect(page.locator("#toast")).to_contain_text("Keys of bulk replaced")
    listed.get_by_role("button", name="History of bulk").click()
    dialog(page).get_by_role("button", name="Close").click()
    listed.get_by_role("button", name="Revoke bulk").click()
    dialog(page).get_by_role("button", name="Revoke").click()
    expect(listed).to_contain_text("Revoked")
    credentials = [(method, path, org) for method, path, org in seen if CREDENTIALS.match(path)]
    asked = {
        (method, re.sub(r"/[0-9a-f]{12,24}(?=/|$)", "/{id}", path))
        for method, path, _ in credentials
    }
    assert asked >= {
        ("GET", "/api/auth/keys"),
        ("GET", "/api/auth/keys/presets"),
        ("POST", "/api/auth/keys"),
        ("GET", "/api/auth/keys/{id}/history"),
        ("POST", "/api/auth/keys/{id}/rotate"),
        ("DELETE", "/api/auth/keys/{id}"),
        ("GET", "/api/auth/clients"),
        ("POST", "/api/auth/clients"),
        ("POST", "/api/auth/clients/{id}/keys"),
        ("GET", "/api/auth/clients/{id}/history"),
        ("DELETE", "/api/auth/clients/{id}"),
    }, asked
    assert {org for _, _, org in credentials} == {"clinic-a"}, credentials


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_credential_tabs_pass_axe_in_both_themes(site: Site, scheme: str) -> None:
    make_key(site)
    make_client(site)
    page = admin(site, bypass_csp=True, color_scheme=scheme)  # the test injects axe
    found = {}
    access(page, "keys")
    page.locator("#key-preset-detail summary").click()
    expect(page.locator("#key-preset-detail details")).to_have_attribute("open", "")
    found["keys"] = violations(page)
    page.select_option("#key-preset", label="Custom")
    found["a key by hand"] = violations(page)
    row(page, "keys", "ehr").get_by_role("button", name="History of ehr").click()
    found["a key's history"] = violations(page)
    dialog(page).get_by_role("button", name="Close").click()
    create_key(page, "shown")
    found["a key shown once"] = violations(page)
    dialog(page).get_by_role("button", name="Done").click()
    access(page, "clients")
    found["clients"] = violations(page)
    row(page, "clients", "bulk").get_by_role("button", name="Replace keys of bulk").click()
    found["replacing keys"] = violations(page)
    dialog(page).get_by_role("button", name="Cancel").click()
    row(page, "clients", "bulk").get_by_role("button", name="History of bulk").click()
    found["a client's history"] = violations(page)
    assert {where: v for where, v in found.items() if v} == {}


def test_the_credential_tabs_fit_a_phone(site: Site) -> None:
    make_key(site)
    make_client(site)
    page = admin(site, viewport=PHONE, has_touch=True, is_mobile=True)
    wide = "document.documentElement.scrollWidth <= window.innerWidth"
    for tab in ("keys", "clients", "usage", "activity"):
        access(page, tab)
        expect(page.locator(".tabs a")).to_have_count(7)
        if tab == "activity":
            expect(row(page, "activity", "API key created")).to_have_count(1)
        assert page.evaluate(wide), tab
        assert page.evaluate(SMALL_TARGETS) == [], tab
    access(page, "keys")
    page.select_option("#key-preset", label="Custom")
    expect(page.locator("#key-scope")).to_be_visible()
    assert page.evaluate(wide) and page.evaluate(SMALL_TARGETS) == []
