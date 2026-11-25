# SPDX-License-Identifier: Apache-2.0
"""Alert delivery (LP-21 requirement 3, `mendari.alerts`): formats chosen by a setting, an HMAC
signature a receiver checks with the documented recipe, retries, and a record of every alert
that holds neither the webhook's URL nor the secret; and `mendari alerts test`, one signed test
alert for the operator to check the webhook with, never from CI."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari import alerts as alerts_module
from mendari.alerts import (
    AlertSettings,
    alert_settings,
    check_webhook,
    deliver,
    render,
    retry_due,
    signature,
)
from mendari.cli import main
from mendari.errors import MendariError
from mendari.server.app import create_app
from tests.conftest import message

ROOT = Path(__file__).parents[1]
SECRET = "s3cret-" + "x" * 40
HOOK = "https://hooks.example.org/services/T000/B000/WebhookSecretToken"
DECIDE = "/api/agents/mendari-test/triage/decide"
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO
# The triage fixture's providers that automate (its policy's `automation_requires`): its
# screen's canned agent, which automates without sign-in, as a live one does.
SCREENED = {"screen": "agent"}


class Webhook:
    """A webhook that answers each post with the next status (or raises the next error), and
    keeps what it was sent."""

    def __init__(self, *answers: int | Exception):
        self.answers = list(answers)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.sent.append({"url": url, **kwargs})
        answer = self.answers.pop(0) if self.answers else 204
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, request=httpx.Request("POST", url))


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waited: list[float] = []
    monkeypatch.setattr(alerts_module, "_sleep", waited.append)
    return waited


def recipe() -> Any:
    """The receiver's check exactly as docs/deploying-with-phi.md shows it."""
    text = (ROOT / "docs" / "deploying-with-phi.md").read_text()
    block = re.search(r"<!-- receiver: [^>]*-->\s*```python\n(.*?)```", text, re.S)
    assert block is not None, "the documented receiver recipe is gone"
    code = "\n".join(line.removeprefix("  ") for line in block.group(1).splitlines())
    namespace: dict[str, Any] = {}
    exec(compile(code, "deploying-with-phi.md", "exec"), namespace)
    return namespace["is_mendari"]


def one(**overrides: Any) -> dict[str, Any]:
    return {
        "event": "monitor.alerts",
        "org": "clinic-a",
        "alerts": [{"pack": {"id": "mendari/x"}, "alerts": [{"kind": "drift"}]}],
        "lines": ["mendari/x: [medium] drift: PSI 0.31"],
        **overrides,
    }


# -- the signature -----------------------------------------------------------------------------


def test_the_signature_verifies_with_the_documented_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = Webhook(204)
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    assert deliver(AlertSettings(HOOK, secret=SECRET), **one(), attempts=1, timeout=5)
    (sent,) = hook.sent
    headers, body = sent["headers"], sent["content"]
    is_mendari = recipe()
    assert is_mendari(SECRET, headers["mendari-signature"], body)
    assert not is_mendari(SECRET, headers["mendari-signature"], body + b" ")  # tampered
    assert not is_mendari("another-" + "y" * 40, headers["mendari-signature"], body)
    stale = signature(SECRET, body, int(datetime.now(UTC).timestamp()) - 3600)
    assert not is_mendari(SECRET, stale, body)  # replayed an hour later
    assert headers["mendari-delivery"] == json.loads(body)["delivery"]
    assert headers["mendari-event"] == "monitor.alerts"
    assert sent["follow_redirects"] is False


def test_without_a_secret_nothing_is_signed_and_the_server_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    hook = Webhook(204)
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    deliver(AlertSettings(HOOK), **one(), attempts=1, timeout=5)
    assert "mendari-signature" not in hook.sent[0]["headers"]
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", HOOK)
    with caplog.at_level(logging.WARNING, logger="mendari.alerts"):
        create_app(db_path=tmp_path / "a.db")
    assert "without MENDARI_ALERT_SECRET" in caplog.text


# -- formats -----------------------------------------------------------------------------------


def test_each_format_renders_ids_and_counts_only() -> None:
    fields = one(lines=["mendari/x: [high] <b>accuracy</b> & more"])
    body = json.loads(render("mendari", delivery="d1", **fields))
    assert body == {
        "source": "mendari",
        "delivery": "d1",
        "event": "monitor.alerts",
        "organization": "clinic-a",
        "alerts": fields["alerts"],
    }
    slack = json.loads(render("slack", delivery="d1", **fields))
    assert slack == {
        "text": "Mendari: monitor.alerts for clinic-a\n"
        "mendari/x: [high] &lt;b&gt;accuracy&lt;/b&gt; &amp; more"
    }


@pytest.mark.parametrize(
    ("name", "value", "problem"),
    [
        ("MENDARI_ALERT_FORMAT", "teams", "MENDARI_ALERT_FORMAT must be one of mendari, slack"),
        ("MENDARI_ALERT_SECRET", "short", "at least 32"),
        ("MENDARI_ALERT_SECRET", " " + "z" * 40, "no spaces"),
    ],
)
def test_a_bad_format_or_secret_fails_at_startup_and_before_a_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
    problem: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(MendariError, match=problem):
        create_app(db_path=tmp_path / "a.db")
    AuditStore(tmp_path / "b.db").close()
    assert main(["monitor", "--db", str(tmp_path / "b.db"), "--webhook", HOOK]) == 1
    assert problem in capsys.readouterr().err


def test_the_format_is_chosen_by_its_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_ALERT_FORMAT", "Slack")
    chosen = alert_settings(HOOK)
    assert chosen is not None and chosen.format == "slack"
    monkeypatch.delenv("MENDARI_ALERT_FORMAT")
    assert alert_settings() is None  # no webhook, nothing to post
    assert alert_settings(HOOK) == AlertSettings(HOOK, "mendari", None)


# -- retries and records -----------------------------------------------------------------------


def test_a_failed_delivery_is_retried_and_recorded(
    store: AuditStore, monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    flaky = Webhook(503, httpx.ConnectError("down"), 204)
    monkeypatch.setattr(alerts_module.httpx, "post", flaky)
    assert deliver(AlertSettings(HOOK), **one(), attempts=3, timeout=5, store=store)
    assert no_waiting == [2.0, 8.0] and len(flaky.sent) == 3
    (sent,) = store.alert_deliveries()
    assert (sent["status"], sent["attempts"], sent["last_status"]) == ("delivered", 3, 204)
    assert sent["last_error"] is None and sent["next_attempt_at"] is None
    assert json.loads(sent["body"])["organization"] == "clinic-a"


def test_a_final_answer_is_not_retried(
    store: AuditStore, monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    refused = Webhook(404)
    monkeypatch.setattr(alerts_module.httpx, "post", refused)
    assert not deliver(AlertSettings(HOOK), **one(), attempts=3, timeout=5, store=store)
    assert len(refused.sent) == 1 and no_waiting == []
    (sent,) = store.alert_deliveries()
    assert (sent["status"], sent["last_status"], sent["last_error"]) == ("failed", 404, "http_404")


def test_a_pending_alert_is_sent_again_until_it_is_delivered_or_gives_up(
    store: AuditStore, monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    down = Webhook(*[httpx.ConnectError("down")] * 10)
    monkeypatch.setattr(alerts_module.httpx, "post", down)
    settings = AlertSettings(HOOK, secret=SECRET)
    assert not deliver(settings, **one(), attempts=1, timeout=5, store=store)

    def due_now() -> None:
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with store._db.session(exclusive=True) as db:
            db.all("UPDATE alert_deliveries SET next_attempt_at = ?", (past,))

    nothing = {"delivered": 0, "pending": 0, "failed": 0, "deferred": 0}
    assert retry_due(store, settings) == nothing  # not due
    for tries in (2, 3, 4):
        due_now()
        assert retry_due(store, settings) == {**nothing, "pending": 1}
        assert store.alert_deliveries()[0]["attempts"] == tries
    due_now()
    assert retry_due(store, settings) == {**nothing, "failed": 1}
    (sent,) = store.alert_deliveries()
    assert (sent["status"], sent["attempts"]) == ("failed", alerts_module.MAX_ATTEMPTS)
    due_now()
    assert retry_due(store, settings) == nothing
    bodies = {entry["content"] for entry in down.sent}
    assert len(bodies) == 1  # the same alert each time, signed afresh
    signed = [entry["headers"]["mendari-signature"] for entry in down.sent]
    assert all(recipe()(SECRET, header, next(iter(bodies))) for header in signed)


def test_no_url_or_secret_reaches_a_record_or_the_log(
    store: AuditStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    no_waiting: list[float],
) -> None:
    def leaky(url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {url} with {SECRET}")

    monkeypatch.setattr(alerts_module.httpx, "post", leaky)
    with caplog.at_level(logging.DEBUG):
        deliver(AlertSettings(HOOK, secret=SECRET), **one(), attempts=3, timeout=5, store=store)
    recorded = json.dumps(store.alert_deliveries())
    for text in (recorded, caplog.text):
        assert "WebhookSecretToken" not in text and "hooks.example.org" not in text
        assert SECRET not in text
    assert "ConnectError" in recorded


# -- the senders -------------------------------------------------------------------------------


def test_a_quota_alert_is_signed_recorded_and_tried_once_on_the_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_waiting: list[float],
) -> None:
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "decisions=1")
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", HOOK)
    monkeypatch.setenv("MENDARI_ALERT_SECRET", SECRET)
    slow = Webhook(httpx.ReadTimeout("slow"))
    monkeypatch.setattr(alerts_module.httpx, "post", slow)
    client = TestClient(create_app(db_path=tmp_path / "a.db", providers=SCREENED))
    assert client.post(DECIDE, json=message(ROUTINE)).json()["outcome"] == "AUTO"
    (sent,) = slow.sent
    assert sent["timeout"] == 5.0 and no_waiting == []  # one try, never a wait on the decision
    assert recipe()(SECRET, sent["headers"]["mendari-signature"], sent["content"])
    assert ROUTINE not in sent["content"].decode()
    with AuditStore(tmp_path / "a.db") as store:
        (recorded,) = store.alert_deliveries()
    assert (recorded["event"], recorded["org"], recorded["status"]) == (
        "quota.reached",
        "default",
        "pending",
    )


def test_monitor_exit_codes_tell_alerts_from_a_failed_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    triage_agent: Pack,
    no_waiting: list[float],
) -> None:
    db = tmp_path / "a.db"
    args = ["monitor", "--db", str(db), "--agent", triage_agent.id, "--webhook", HOOK]
    with AuditStore(db) as store:
        assert main(args) == 0  # nothing to say
        decided = Decision(triage_agent, providers=SCREENED, store=store)(message(ROUTINE))
        expected = {"question": "destination", "label": "billing"}
        store.record_feedback(decided.id, rating="down", expected=expected)
    down = Webhook(*[httpx.ConnectError("down")] * 3)
    monkeypatch.setattr(alerts_module.httpx, "post", down)
    assert main(args) == 3  # alerts found, and not delivered
    assert len(down.sent) == 3 and no_waiting == [2.0, 8.0]
    assert "the alerts could not be posted to the webhook" in capsys.readouterr().err
    up = Webhook(204)
    monkeypatch.setattr(alerts_module.httpx, "post", up)
    assert main(args) == 1  # alerts found and delivered
    body = json.loads(up.sent[0]["content"])
    assert body["event"] == "monitor.alerts"
    assert body["alerts"][0]["alerts"][0]["kind"] == "automated_wrong"


# -- what the review found (LP-21e) -----------------------------------------------------------


def test_a_webhook_the_client_cannot_parse_fails_at_startup_never_on_a_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: AuditStore
) -> None:
    """A mistyped port passes a look at the scheme and host; the client refuses it. It is
    refused at startup, and an alert that meets one anyway is recorded, never raised."""
    typo = "https://hooks.example.org:8x/services/T000/B000/WebhookSecretToken"
    with pytest.raises(MendariError, match="not a valid URL") as refused:
        check_webhook(typo)
    assert "WebhookSecretToken" not in str(refused.value)
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "decisions=1")
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", typo)
    with pytest.raises(MendariError, match="not a valid URL"):
        create_app(db_path=tmp_path / "a.db")
    assert not deliver(AlertSettings(typo), **one(), attempts=1, timeout=5, store=store)
    (sent,) = store.alert_deliveries()
    assert (sent["status"], sent["last_error"]) == ("failed", "InvalidURL")


def test_the_format_setting_reaches_what_is_posted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    triage_agent: Pack,
    no_waiting: list[float],
) -> None:
    db = tmp_path / "a.db"
    with AuditStore(db) as store:
        decided = Decision(triage_agent, providers=SCREENED, store=store)(message(ROUTINE))
        expected = {"question": "destination", "label": "billing"}
        store.record_feedback(decided.id, rating="down", expected=expected)
    hook = Webhook(204)
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    monkeypatch.setenv("MENDARI_ALERT_FORMAT", "slack")
    args = ["monitor", "--db", str(db), "--agent", triage_agent.id, "--webhook", HOOK]
    assert main(args) == 1
    posted = json.loads(hook.sent[0]["content"])
    assert set(posted) == {"text"} and "automated_wrong" in posted["text"]


def test_a_pending_alert_sent_again_is_delivered_with_its_id_and_a_fresh_signature(
    store: AuditStore, monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    settings = AlertSettings(HOOK, secret=SECRET)
    monkeypatch.setattr(alerts_module.httpx, "post", Webhook(503))
    assert not deliver(settings, **one(), attempts=1, timeout=5, store=store)
    (pending,) = store.alert_deliveries()
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE alert_deliveries SET next_attempt_at = ?", (past,))
    up = Webhook(204)
    monkeypatch.setattr(alerts_module.httpx, "post", up)
    assert retry_due(store, settings) == {"delivered": 1, "pending": 0, "failed": 0, "deferred": 0}
    (sent,) = up.sent
    assert sent["headers"]["mendari-delivery"] == pending["id"]
    assert sent["headers"]["mendari-event"] == "monitor.alerts"
    assert recipe()(SECRET, sent["headers"]["mendari-signature"], sent["content"])
    (delivered,) = store.alert_deliveries()
    assert (delivered["status"], delivered["attempts"], delivered["last_status"]) == (
        "delivered",
        2,
        204,
    )
    assert delivered["next_attempt_at"] is None and delivered["last_error"] is None


# -- the test alert (`mendari alerts test`) -------------------------------------------------------


@pytest.fixture
def operator_machine(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """The operator's own machine, not CI, whatever runs these tests."""
    for name in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def receiver() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A webhook on this machine that answers 204 and keeps each post's path, headers and raw
    body; its address carries a token in its path, as most webhooks' do."""
    heard: list[dict[str, Any]] = []

    class Receive(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["content-length"]))
            headers = {name.lower(): value for name, value in self.headers.items()}
            heard.append({"path": self.path, "headers": headers, "body": body})
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Receive)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/services/T000/B000/WebhookSecretToken", heard
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_alerts_test_posts_one_signed_test_alert_and_prints_neither_the_url_nor_the_secret(
    operator_machine: pytest.MonkeyPatch,
    receiver: tuple[str, list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, heard = receiver
    operator_machine.setenv("MENDARI_ALERT_WEBHOOK", url)
    operator_machine.setenv("MENDARI_ALERT_SECRET", SECRET)
    assert main(["alerts", "test"]) == 0
    (post,) = heard
    assert post["path"] == "/services/T000/B000/WebhookSecretToken"
    assert recipe()(SECRET, post["headers"]["mendari-signature"], post["body"])
    delivery = post["headers"]["mendari-delivery"]
    assert post["headers"]["mendari-event"] == "alerts.test"
    assert json.loads(post["body"]) == {
        "source": "mendari",
        "delivery": delivery,
        "event": "alerts.test",
        "organization": None,
        "alerts": [],
    }
    out, err = capsys.readouterr()
    assert out == (
        "delivered: one alerts.test in the mendari format, signed "
        f"(the webhook answered 204, delivery {delivery})\n"
    )
    for text in (out, err):
        assert "WebhookSecretToken" not in text and "127.0.0.1" not in text
        assert SECRET not in text


def test_alerts_test_posts_in_the_format_set_and_says_when_it_is_not_signed(
    operator_machine: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_waiting: list[float],
) -> None:
    hook = Webhook(200)
    operator_machine.setattr(alerts_module.httpx, "post", hook)
    operator_machine.setenv("MENDARI_ALERT_WEBHOOK", HOOK)
    operator_machine.setenv("MENDARI_ALERT_FORMAT", "slack")
    assert main(["alerts", "test"]) == 0
    (sent,) = hook.sent
    assert json.loads(sent["content"]) == {
        "text": "Mendari: alerts.test\na test alert from `mendari alerts test`: nothing to do"
    }
    assert "mendari-signature" not in sent["headers"]
    out = capsys.readouterr().out
    assert out.startswith("delivered: one alerts.test in the slack format, not signed: ")
    assert "MENDARI_ALERT_SECRET is not set" in out


def test_alerts_test_tries_once_and_says_why_it_failed_never_the_url(
    operator_machine: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_waiting: list[float],
) -> None:
    operator_machine.setenv("MENDARI_ALERT_WEBHOOK", HOOK)
    operator_machine.setenv("MENDARI_ALERT_SECRET", SECRET)
    busy = Webhook(503)
    operator_machine.setattr(alerts_module.httpx, "post", busy)
    assert main(["alerts", "test"]) == 1
    assert len(busy.sent) == 1 and no_waiting == []  # one try: it says what happens now
    first = capsys.readouterr().err
    assert first.startswith("error: the test alert was not delivered: the webhook answered 503")
    down = Webhook(httpx.ConnectError(f"cannot reach {HOOK} with {SECRET}"))
    operator_machine.setattr(alerts_module.httpx, "post", down)
    assert main(["alerts", "test"]) == 1
    second = capsys.readouterr().err
    assert "the test alert was not delivered: no answer (ConnectError)" in second
    for text in (first, second):
        assert "WebhookSecretToken" not in text and "hooks.example.org" not in text
        assert SECRET not in text


def test_alerts_test_is_refused_in_ci_without_a_webhook_and_for_a_webhook_alerts_never_use(
    operator_machine: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = Webhook()
    operator_machine.setattr(alerts_module.httpx, "post", hook)
    assert main(["alerts", "test"]) == 1
    assert "error: MENDARI_ALERT_WEBHOOK is not set" in capsys.readouterr().err
    operator_machine.setenv("MENDARI_ALERT_WEBHOOK", HOOK)
    for name in ("CI", "GITHUB_ACTIONS"):
        operator_machine.setenv(name, "true")
        assert main(["alerts", "test"]) == 1
        assert capsys.readouterr().err == (
            f"error: {name} is set: a test alert is never sent from CI; run it on your own "
            "machine\n"
        )
        operator_machine.delenv(name)
    operator_machine.setenv("MENDARI_ALERT_WEBHOOK", HOOK.replace("https:", "http:"))
    assert main(["alerts", "test"]) == 1
    assert capsys.readouterr().err == "error: the alert webhook must use https\n"
    assert hook.sent == []
