# SPDX-License-Identifier: Apache-2.0
"""The System view and the first-run panel, in a real browser, on a real server set up as the
one-machine install leaves one: memberships mode, an operator organization, patient data mode,
and the bundled demo agent alone.

- A new deployment's operator opens on "Finish setting up": what is left, in order, ending
  with adopting an agent. It goes when the deployment is no longer new.
- The System view shows every check with its state in words and what to do, and holds the
  operator's actions: verify the audit trail, send a test alert, check an EHR's connection,
  hold all automation and lift the hold, erase a decision's kept input. What takes something
  away is asked about first; each is recorded as the engine records it.
- A served version its registry recalled is said at the top.
- Anyone who does not operate the deployment has no System view and no panel, and asks the
  server for neither.
- Both pass axe in both themes, and work from the keyboard and at a phone's width."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, Route, expect

from mendari import Decision, load_agent
from mendari import alerts as alerts_module
from mendari.core import tenancy
from mendari.server import smart_check
from mendari.server.app import create_app
from mendari.server.smart_check import Finding
from tests.browser.access_site import Site, dialog, lead, memberships, person
from tests.browser.conftest import serving
from tests.browser.test_accessibility import SCHEMES, violations
from tests.browser.test_dashboard import PHONE
from tests.conftest import DEMO_AGENT_DIR, ROOT, encrypted_store, message
from tests.test_monitor_cron import Webhook
from tests.test_system_checks import KEYS

pytestmark = pytest.mark.browser

DEMO = "mendari/portal-message-agent"
STATES = {"In order", "Needs attention", "Not set up", "Does not apply"}
EHR = "https://ehr.stmarys.example/fhir"


@contextmanager
def deployment(
    browser: Browser, db: Path, monkeypatch: pytest.MonkeyPatch, **settings: str
) -> Iterator[Site]:
    """A server as the one-machine install leaves one, serving the product's own agents (the
    bundled demo agent alone), with Clinic-A as the organization that operates it."""
    memberships(monkeypatch)
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", "clinic-a")
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    app = create_app(db_path=db, agents_paths=[ROOT / "agents"])
    with encrypted_store(db) as store, serving(app) as port:
        site = Site(f"http://localhost:{port}", store, browser)
        try:
            yield site
        finally:
            for context in site.contexts:
                context.close()


def decide(site: Site) -> str:
    """One decision by the demo agent, its input kept sealed: the deployment is new no more."""
    with tenancy.acting_for("clinic-a"):
        decided = Decision(load_agent(DEMO_AGENT_DIR), store=site.store)(
            message("Please refill my usual prescription.")
        )
    return str(decided.id)


def chained(site: Site, kind: str) -> list[dict[str, Any]]:
    with site.store._db.session(exclusive=False) as db:
        rows = db.all("SELECT payload FROM audit_events WHERE event = ? ORDER BY seq", (kind,))
    return [json.loads(row["payload"]) for row in rows]


def system(page: Page, check: str | None = None) -> None:
    page.goto("about:blank")
    page.goto(f"/#system{'/' + check if check else ''}")
    expect(page.locator("main h1")).to_have_text("System")
    expect(page.locator("main")).not_to_have_attribute("aria-busy", "true")


def row(page: Page, check: str) -> Any:
    return page.locator(f'.check-row[data-check="{check}"]')


# -- the first run --------------------------------------------------------------------------------


def test_a_new_deployments_operator_opens_on_finish_setting_up(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        page = site.window(lead(site))
        page.goto("/")
        panel = page.locator("#setup")
        expect(panel.get_by_role("heading", name="Finish setting up")).to_be_visible()
        # It is the first thing on the page the console opens on.
        assert page.evaluate("document.querySelector('main').firstElementChild.id") == "setup"
        checks = page.request.get("/api/system/checks", headers={"x-mendari-request": "1"}).json()
        assert checks["setup"] == {"fresh": True, "finished": False}
        to_do = [c for c in checks["checks"] if c["state"] in ("attention", "not_set")]
        steps = panel.locator(".adoption-step")
        # What is left, in the checks' own order, ending with adopting an agent.
        expected = [c["key"] for c in to_do if c["key"] != "agents"] + ["agents"]
        assert steps.evaluate_all("(steps) => steps.map((s) => s.dataset.step)") == expected
        expect(panel).to_contain_text(f"{len(KEYS) - len(to_do)} of {len(KEYS)} checks in order")
        first = steps.first
        expect(first).to_have_attribute("aria-current", "step")
        expect(first).to_contain_text("Who runs this deployment")
        expect(first).to_contain_text("Invite a second administrator in the Access view.")
        expect(first.get_by_role("link", name="Open Access")).to_have_attribute(
            "href", "#admin/members"
        )
        last = steps.last
        expect(last).to_contain_text("Only the bundled demo agent is served")
        expect(last).to_contain_text("Adopt an agent from the Agents view.")
        # Each step says what is so and what to do, in words, and leads to where it is done.
        for step, check in zip(steps.all(), [*to_do[:-1], to_do[-1]], strict=False):
            expect(step.get_by_role("link")).to_have_count(1)
            assert check["detail"]
        # Followed from the keyboard: a step's link lands on its check in the System view.
        verify = panel.locator('[data-step="audit_chain"]').get_by_role("link")
        verify.focus()
        page.keyboard.press("Enter")
        expect(page.locator("main h1")).to_have_text("System")
        expect(row(page, "audit_chain")).to_have_class("check-row target")
        expect(row(page, "audit_chain").locator("h3")).to_be_focused()

        # It goes once the deployment is no longer new: a decision was made here.
        decide(site)
        page.goto("about:blank")
        page.goto("/")
        expect(page.locator("main h1")).to_be_visible()
        expect(page.locator("#setup")).to_have_count(0)
        # And nothing more is asked about it in this window.
        asked: list[str] = []
        page.on("request", lambda request: asked.append(request.url))
        page.locator('.nav a[data-view="audit"]').click()
        expect(page.locator("#audit-table")).to_be_visible()
        page.locator('.nav a[data-view="overview"]').click()
        expect(page.locator(".agent-hero h1")).to_be_visible()
        assert [url for url in asked if "/api/stats" in url]  # the overview was read again
        assert not [url for url in asked if "/api/system/checks" in url]


def test_the_panel_ends_at_the_agents_view_where_the_console_has_one(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view where agents are found and adopted registers itself as `agents`; the last step
    then leads there, and so do the checks about agents and the registry."""
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        page = site.window(lead(site))
        # A stand-in for that view, registered as the console's views register themselves.
        page.add_init_script(
            """window.addEventListener("DOMContentLoaded", async () => {
              const { views } = await import("/static/router.js");
              views.agents = async (main) => { main.innerHTML = "<h1>Agents</h1>"; };
              views.agents.agentless = true;
            });"""
        )
        page.goto("/")
        last = page.locator("#setup .adoption-step").last
        expect(last.get_by_role("link", name="Open Agents")).to_have_attribute("href", "#agents")
        system(page)
        for check in ("agents", "registry"):
            expect(row(page, check).get_by_role("link", name="Open Agents")).to_have_attribute(
                "href", "#agents"
            )


# -- the System view ------------------------------------------------------------------------------


def test_the_system_view_shows_every_check_with_its_state_and_what_to_do(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        page = site.window(lead(site))
        page.goto("/")
        link = page.locator('.nav a[data-view="system"]')
        expect(link).to_be_visible()
        link.click()
        expect(page.locator("main h1")).to_have_text("System")
        expect(link).to_have_attribute("aria-current", "page")
        checks = page.request.get("/api/system/checks", headers={"x-mendari-request": "1"}).json()
        rows = page.locator(".check-row")
        assert rows.evaluate_all("(rows) => rows.map((r) => r.dataset.check)") == KEYS
        for check in checks["checks"]:
            shown = row(page, check["key"])
            expect(shown.locator("h3")).to_have_text(check["label"])
            expect(shown).to_contain_text(check["detail"])
            assert shown.locator(".check-state").inner_text().strip() in STATES
            if check["action"]:
                expect(shown).to_contain_text(f"What to do: {check['action']}")
        # The state is said in words, never by colour alone.
        said = rows.locator(".check-state").all_inner_texts()
        assert {text.strip() for text in said} <= STATES and len(said) == len(KEYS)
        # Grouped under the areas a person thinks in, each a section with its heading.
        areas = page.locator("main section h2").all_inner_texts()
        assert areas[:2] == ["Automation", "Sign-in and people"]
        assert "Database and audit trail" in areas and areas[-1] == "Erase a decision's kept input"
        # The counts at the top are the checks'.
        summary = checks["summary"]
        counted = page.locator(".stats-primary .stat-value").all_inner_texts()
        assert [int(n) for n in counted] == [
            summary["ok"],
            summary["attention"],
            summary["not_set"],
        ]
        # Nothing here names a button without words, and every control is reachable by Tab.
        unnamed = page.evaluate(
            """() => [...document.querySelectorAll('main button, main a, main input, main select')]
              .filter((el) => !(el.textContent.trim() || el.getAttribute('aria-label')
                || (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`))))
              .map((el) => el.outerHTML.slice(0, 80))"""
        )
        assert unnamed == []
        assert (
            page.evaluate(
                "document.querySelectorAll('main [tabindex]:not([tabindex=\"-1\"])').length"
            )
            == 0
        )


def test_verifying_the_audit_trail_from_the_system_view(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        decide(site)
        page = site.window(lead(site))
        system(page, "audit_chain")
        check = row(page, "audit_chain")
        expect(check.locator(".check-state")).to_have_text("Needs attention")
        expect(check).to_contain_text("has not been verified on this deployment yet")
        before = site.store.verify()["events"]
        button = check.get_by_role("button", name="Verify now")
        button.focus()
        page.keyboard.press("Enter")  # from the keyboard
        expect(page.locator("#toast")).to_contain_text(f"The audit trail verifies: {before} events")
        expect(row(page, "audit_chain").locator(".check-state")).to_have_text("In order")
        expect(row(page, "audit_chain")).to_contain_text("As of")
        assert site.store.verify()["events"] == before  # verifying chains nothing
        assert site.store.runs()["audit.verify"]["outcome"] == "ok"


def test_holding_all_automation_asks_first_takes_a_reason_and_is_lifted_the_same_way(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        page = site.window(lead(site))
        system(page)
        panel = page.locator("#system-automation")
        expect(panel).to_contain_text("Agents automate what their policies allow")
        panel.get_by_role("button", name="Hold all automation…").click()
        asked = dialog(page)
        expect(asked.get_by_role("heading", name="Hold all automation?")).to_be_visible()
        expect(asked).to_contain_text("Every agent of Clinic-A stops deciding on its own, at once")
        expect(asked).to_contain_text("kept in the audit trail")
        expect(asked.get_by_role("button", name="Cancel")).to_be_focused()  # the safe choice
        # Without a reason nothing is held, and the dialog says why.
        asked.get_by_role("button", name="Hold all automation").click()
        expect(asked.get_by_role("alert")).to_have_text(
            "Say why, in a few words: it is kept with the hold."
        )
        assert site.store.holds("clinic-a") == []
        asked.get_by_label("Why, in a few words").fill("investigating a missed emergency")
        asked.get_by_role("button", name="Hold all automation").click()
        expect(page.locator("#toast")).to_contain_text("All automation is held")
        held = page.locator("#system-automation .callout.danger")
        expect(held).to_contain_text(
            "Automation is on hold for every agent: investigating a missed emergency."
        )
        expect(held).to_contain_text("Set by lead@example.org")
        expect(page.locator("#system-automation")).not_to_contain_text("Hold all automation…")
        (hold,) = site.store.holds("clinic-a")
        assert (hold["org"], hold["agent_id"], hold["reason"]) == (
            "clinic-a",
            None,
            "investigating a missed emergency",
        )
        (event,) = chained(site, "automation.held")
        assert event["set_by"] == "lead@example.org"

        # Cancelled, the hold stays; lifted, it is gone, and that is chained too.
        held.get_by_role("button", name="Lift…").click()
        expect(dialog(page)).to_contain_text("Every agent decides on its own again")
        page.keyboard.press("Escape")
        expect(dialog(page)).to_have_count(0)
        assert len(site.store.holds("clinic-a")) == 1
        held.get_by_role("button", name="Lift…").click()
        dialog(page).get_by_role("button", name="Lift").click()
        expect(page.locator("#toast")).to_contain_text("Hold lifted")
        expect(page.locator("#system-automation .callout.danger")).to_have_count(0)
        assert site.store.holds("clinic-a") == []
        assert len(chained(site, "automation.lifted")) == 1


def test_a_hold_set_for_the_whole_deployment_says_where_it_is_lifted(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hold set from the machine's command line (a restore sets one) covers every
    organization: the console shows it and how it is lifted, and offers no button that would
    be refused."""
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        hold = site.store.hold_automation(
            org=None,
            agent_id=None,
            agent_digest=None,
            reason="restored from a backup",
            set_by="ops",
        )
        page = site.window(lead(site))
        system(page)
        held = page.locator("#system-automation .callout.danger")
        expect(held).to_contain_text(
            "Automation is on hold for every agent, for the whole deployment"
        )
        expect(held).to_contain_text(f"mendari automation lift {hold['id']}")
        expect(held).to_contain_text(f"./mendari-site lift {hold['id']}")
        expect(held.get_by_role("button")).to_have_count(0)
        expect(page.locator("#system-automation")).not_to_contain_text("Hold all automation…")


def test_a_test_alert_from_the_system_view(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = Webhook(204, 500)
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    webhook = "https://hooks.stmarys.example/services/zq-webhook-token"
    settings = {"MENDARI_ALERT_WEBHOOK": webhook, "MENDARI_ALERT_SECRET": "zq-alert-secret-" * 3}
    with deployment(browser, tmp_path / "mendari.db", monkeypatch, **settings) as site:
        page = site.window(lead(site))
        system(page, "alerts")
        check = row(page, "alerts")
        expect(check).to_contain_text("nothing has been delivered to it yet")
        assert "zq-webhook-token" not in page.locator("main").inner_text()  # never the address
        check.get_by_role("button", name="Send a test alert").click()
        expect(page.locator("#toast")).to_contain_text("The test alert was delivered")
        expect(row(page, "alerts").locator(".check-state")).to_have_text("In order")
        (sent,) = hook.sent
        assert sent["url"] == webhook and sent["body"]["event"] == "alerts.test"
        # A receiver that refuses it is said, with its answer and not its address.
        row(page, "alerts").get_by_role("button", name="Send a test alert").click()
        expect(page.locator("#toast")).to_contain_text(
            "The test alert was not delivered (the receiver answered 500)."
        )
        expect(row(page, "alerts").locator(".check-state")).to_have_text("Needs attention")
    # Without a destination there is nothing to press.
    monkeypatch.delenv("MENDARI_ALERT_WEBHOOK")
    with deployment(browser, tmp_path / "other.db", monkeypatch) as site:
        page = site.window(lead(site))
        system(page, "alerts")
        expect(row(page, "alerts").locator(".check-state")).to_have_text("Not set up")
        expect(row(page, "alerts").get_by_role("button")).to_have_count(0)


def test_checking_an_ehrs_connection_from_the_system_view(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = [
        Finding("ok", "registration", "registered to clinic-a, level view"),
        Finding("warn", "scopes", "the EHR does not publish the scopes it supports"),
        Finding("fail", "allow-list", "add https://ehr.stmarys.example to the allow-list"),
    ]
    monkeypatch.setattr(smart_check, "check", lambda base: findings)
    settings = {
        "MENDARI_FHIR_ALLOWED_SERVERS": "https://ehr.stmarys.example",
        "MENDARI_SMART_CLIENT_ID": "mendari-smart",
        "MENDARI_SMART_EHRS": f"{EHR}=clinic-a:view",
    }
    with deployment(browser, tmp_path / "mendari.db", monkeypatch, **settings) as site:
        page = site.window(lead(site))
        system(page, "ehr")
        check = row(page, "ehr")
        expect(check).to_contain_text(EHR)
        check.get_by_role("button", name="Check the connection").click()
        result = check.locator(".ehr-result")
        expect(result).to_contain_text("A launch from this EHR would be refused.")
        shown = result.locator("li").all_inner_texts()
        assert [" ".join(text.split()) for text in shown] == [
            "In order registration: registered to clinic-a, level view",
            "Check this scopes: the EHR does not publish the scopes it supports",
            "Would be refused allow-list: add https://ehr.stmarys.example to the allow-list",
        ]
        assert site.store.runs()["ehr.check"]["outcome"] == "failed"


def test_erasing_a_decisions_kept_input_asks_first_and_is_chained(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        decision = decide(site)
        page = site.window(lead(site))
        # From the decision's own record in the audit trail: a link to the form, with its id.
        page.goto(f"/#audit/{decision}")
        link = page.locator(".drawer").get_by_role(
            "link", name="Erase what this decision keeps of its input…"
        )
        link.click()
        expect(page.locator("main h1")).to_have_text("System")
        form = page.locator("#erase-form")
        expect(form.get_by_label("Decision id")).to_have_value(decision)
        expect(page.locator("#system-erase-title")).to_be_focused()
        form.get_by_label("Reason").select_option("request")
        form.get_by_role("button", name="Erase…").click()
        asked = dialog(page)
        expect(
            asked.get_by_role("heading", name="Erase this decision's kept input?")
        ).to_be_visible()
        expect(asked).to_contain_text(decision)
        expect(asked).to_contain_text("deleted for good")
        expect(asked).to_contain_text("Backups made before now still hold it")
        asked.get_by_role("button", name="Cancel").click()
        assert chained(site, "input.erased") == []  # cancelled: nothing happened
        form.get_by_role("button", name="Erase…").click()
        dialog(page).get_by_role("button", name="Erase").click()
        # The demo agent proposed an action with its answer (spec 0.4): its arguments are kept
        # as the input is, as a note, and erased with it.
        expect(page.locator("#erase-result")).to_contain_text(
            "Erased: its sealed input and evidence and 1 note."
        )
        (event,) = chained(site, "input.erased")
        assert (event["reason"], event["erased_by"]) == ("request", "lead@example.org")
        assert site.store.verify()["ok"]
        # A decision that is not there is refused inside the dialog, in the server's words.
        form.get_by_label("Decision id").fill("no-such-decision")
        form.get_by_role("button", name="Erase…").click()
        dialog(page).get_by_role("button", name="Erase").click()
        expect(dialog(page).get_by_role("alert")).to_contain_text(
            "unknown decision no-such-decision"
        )


def recalled(route: Route) -> None:
    """The checks as the server gives them, with a served version its registry recalled."""
    answer = route.fetch()
    body = answer.json()
    body["recalls"] = [
        {"agent": "acme/intake-agent", "version": "1.2.0", "reason": "it misses chest pain"}
    ]
    route.fulfill(response=answer, json=body)


def test_a_recalled_version_is_said_at_the_top(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        page = site.window(lead(site))
        system(page)
        expect(page.locator("#system-recalls")).to_have_count(0)
        page.route("**/api/system/checks", recalled)
        system(page)
        banner = page.locator("#system-recalls")
        expect(banner).to_contain_text(
            "A version this deployment serves was recalled by its registry."
        )
        expect(banner).to_contain_text("acme/intake-agent 1.2.0: it misses chest pain")
        expect(banner).to_contain_text(
            "no longer automates: every decision it makes goes to a person"
        )
        # It comes before everything but the heading and its line.
        placed = "[...document.querySelector('#system-view').children].map((el) => el.id)"
        assert page.evaluate(placed).index("system-recalls") == 2


# -- who has no System view -----------------------------------------------------------------------


def requests_of(page: Page) -> list[str]:
    """The address of every request `page` makes from now on."""
    asked: list[str] = []
    page.on("request", lambda request: asked.append(request.url))
    return asked


def test_only_the_deployments_operators_have_the_system_view_and_the_panel(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        lead(site)  # Clinic-A, which operates the deployment, has its administrator
        others = {
            "a reviewer of the operator organization": person(
                site, sub="rev", email="rev@example.org", org="clinic-a", roles=["reviewer"]
            ),
            "governance of the operator organization": person(
                site, sub="gov", email="gov@example.org", org="clinic-a", roles=["governance"]
            ),
            "another organization's administrator": person(
                site, sub="bee", email="bee@example.org", org="clinic-b", roles=["admin"]
            ),
        }
        for who, headers in others.items():
            page = site.window(headers)
            asked = requests_of(page)
            page.goto("/")
            expect(page.locator(".agent-hero h1")).to_be_visible()
            expect(page.locator("#setup")).to_have_count(0)
            expect(page.locator('.nav a[data-view="system"]')).to_be_hidden()
            page.goto("/#system")  # typed by hand: the overview, and nothing asked
            expect(page.locator(".agent-hero h1")).to_be_visible()
            expect(page.locator("main h1")).not_to_have_text("System")
            assert not [url for url in asked if "/api/system/" in url], who
            # The whole chain's verification stays the operator's too.
            page.goto("/#audit")
            expect(page.locator("#audit-table")).to_be_visible()
            expect(page.locator("#verify")).to_have_count(0)
        # The operator has both: the System view, and the chain's verification.
        page = site.window(lead(site))
        page.goto("/#audit")
        expect(page.locator("#audit-table")).to_be_visible()
        expect(page.get_by_role("button", name="Verify chain")).to_be_visible()


# -- for everyone who uses it ---------------------------------------------------------------------


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_system_view_and_the_first_run_panel_pass_axe(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        site.store.hold_automation(
            org="clinic-a", agent_id=None, agent_digest=None, reason="a drill", set_by="lead"
        )
        page = site.window(lead(site), bypass_csp=True, color_scheme=scheme)
        page.route("**/api/system/checks", recalled)
        page.goto("/")
        expect(page.locator("#setup")).to_be_visible()
        assert violations(page) == []
        system(page, "alerts")
        assert violations(page) == []
        page.locator("#system-automation").get_by_role("button", name="Lift…").click()
        expect(dialog(page)).to_be_visible()
        assert violations(page) == []


def test_the_system_view_fits_a_phone(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with deployment(browser, tmp_path / "mendari.db", monkeypatch) as site:
        page = site.window(lead(site), viewport=PHONE)
        for address in ("/", "/#system"):
            page.goto("about:blank")
            page.goto(address)
            expect(page.locator("main h1").first).to_be_visible()
            expect(page.locator("main")).not_to_have_attribute("aria-busy", "true")
            # Nothing scrolls sideways but the section list, and nothing is cut off.
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            ), address
        expect(page.locator('.nav a[data-view="system"]')).to_be_attached()
        # A check's parts stack: its state, then what it says, then what can be done.
        box = row(page, "audit_chain").bounding_box()
        button = row(page, "audit_chain").get_by_role("button", name="Verify now").bounding_box()
        assert box is not None and button is not None
        assert button["x"] >= box["x"] and button["x"] + button["width"] <= box["x"] + box["width"]
        assert button["height"] >= 24  # a target a thumb can hit (WCAG 2.5.8)
