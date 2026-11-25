# SPDX-License-Identifier: Apache-2.0
"""Signed site reports, checked on receipt (LP-22f, ADR-0017): a verified site answers one
ticket with a `mendari.site-submission/1` it signed, sent with the ticket's one-time secret.
Synthetic sites and the shipped benchmark's counts only; every key and secret is made here."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mendari.cli import main
from mendari.core.pack import load_pack
from mendari.evals.share import SiteReportV2
from mendari.hub import archive, site_signing
from mendari.hub.site_signing import Site, init_site
from tests.conftest import (
    TRIAGE_DIR,
    Registry,
    share_signed,
    shared_benchmark,
    site_ticket,
    verified_site,
)

# The agent `Registry.publish()` publishes, whose benchmark set's counts `shared_benchmark()`
# shares.
TRIAGE = load_pack(TRIAGE_DIR)
NAMESPACE, NAME = TRIAGE.id.split("/")
PATH = f"/v1/agents/{TRIAGE.id}/{TRIAGE.version}/reports"
LISTED = f"/v1/agents/{TRIAGE.id}/reports"


@pytest.fixture
def published(registry: Registry) -> Registry:
    assert registry.publish().status_code == 201
    return registry


def _send(registry: Registry, record: Any, secret: str) -> Any:
    return registry.client.post(PATH, json=record, headers={"authorization": f"Bearer {secret}"})


def _submission(site: Site, issued: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    return site.sign(site_signing.SUBMISSION, {"ticket": issued["ticket"]["id"], "report": report})


def _stored(registry: Registry) -> list[dict[str, Any]]:
    with registry.operator._db.session(exclusive=False) as db:
        return db.all("SELECT * FROM hub_site_reports")


def test_a_forged_signature_is_rejected(published: Registry, tmp_path: Path) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    record = _submission(site, issued, report)
    forged = {**record, "signature": base64.b64encode(b"\0" * 64).decode()}
    answer = _send(published, forged, issued["submit_secret"])
    assert answer.status_code == 403 and "signature" in answer.text
    assert _stored(published) == []


def test_an_unregistered_or_revoked_key_is_rejected(published: Registry, tmp_path: Path) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    stranger = tmp_path / "stranger"
    init_site(site.id, directory=stranger)  # the site's id, a key never registered
    answer = _send(published, _submission(Site(stranger), issued, report), issued["submit_secret"])
    assert answer.status_code == 403
    published.operator.revoke_site_key(
        site.key_id, org=None, by="o", reason="rotated", compromised=False
    )
    answer = _send(published, _submission(site, issued, report), issued["submit_secret"])
    assert answer.status_code == 403
    # Another verified site's live key, on this site's ticket: refused too.
    other = verified_site(published, tmp_path, org="south-clinic")
    answer = _send(published, _submission(other, issued, report), issued["submit_secret"])
    assert answer.status_code == 403
    assert _stored(published) == []


def test_a_valid_signature_over_a_different_body_is_rejected(
    published: Registry, tmp_path: Path
) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    record = _submission(site, issued, report)
    changed = copy.deepcopy(record)
    changed["report"]["decisions"]["false_automation"] += 1
    answer = _send(published, changed, issued["submit_secret"])
    assert answer.status_code == 403 and "signature" in answer.text
    other_ticket = {**record, "ticket": "0" * 16}
    assert _send(published, other_ticket, issued["submit_secret"]).status_code == 403


def test_a_replay_is_rejected(published: Registry, tmp_path: Path) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    record = _submission(site, issued, report)
    assert _send(published, record, issued["submit_secret"]).status_code == 201
    again = _send(published, record, issued["submit_secret"])  # the same bytes
    assert again.status_code == 409
    renewed = _submission(site, issued, report)  # a new nonce on a used ticket
    answer = _send(published, renewed, issued["submit_secret"])
    assert answer.status_code == 409 and "reported" in answer.text
    assert len(_stored(published)) == 1


@pytest.mark.parametrize("field", ["digest", "configuration", "size", "site", "consent"])
def test_a_report_for_another_digest_configuration_size_or_site_than_its_ticket_is_rejected(
    published: Registry, tmp_path: Path, field: str
) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    if field == "digest":
        issued = site_ticket(published, site, report)
        report = {**report, "pack": {**report["pack"], "digest": "sha256:" + "1" * 64}}
    elif field == "configuration":
        slot = next(s for s, p in TRIAGE.manifest.providers.items() if len(p.variants) > 1)
        variants = list(TRIAGE.manifest.providers[slot].variants)
        other = next(v for v in variants if v != report["providers"][slot]["variant"])
        configuration = {s: p["variant"] for s, p in report["providers"].items()}
        issued = site_ticket(
            published, site, report, configuration={**configuration, slot: other},
            remote_consent=True,
        )  # fmt: skip
        report = {**report, "remote_consent": True}
    elif field == "size":
        issued = site_ticket(published, site, report, n=report["dataset"]["n"] + 1)
    elif field == "site":
        other = verified_site(published, tmp_path, org="south-clinic")
        issued = site_ticket(published, other, report)  # the other site's ticket and secret
    else:
        issued = site_ticket(published, site, report)
        report = {**report, "remote_consent": True}
    answer = _send(published, _submission(site, issued, report), issued["submit_secret"])
    assert answer.status_code in (403, 409), answer.text
    assert _stored(published) == []


def test_a_payload_with_any_case_text_fails_on_both_sides(
    published: Registry, tmp_path: Path
) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    text = "Jane Doe, DOB 1960-02-01, called about chest pain"
    for place in ("primary", "providers"):
        bad = copy.deepcopy(report)
        if place == "primary":
            bad["primary"] = text
        else:
            slot = next(iter(bad["providers"]))
            bad["providers"][slot]["model"] = text
        with pytest.raises(ValidationError):  # the sender's model refuses it
            SiteReportV2.model_validate(bad)
        answer = _send(published, _submission(site, issued, bad), issued["submit_secret"])
        assert answer.status_code == 422 and "Jane" not in answer.text
    assert _stored(published) == []


def test_a_publisher_token_no_longer_shares_a_report(published: Registry) -> None:
    answer = published.client.post(
        PATH,
        json={"site": "Site A", "report": shared_benchmark()},
        headers={"authorization": f"Bearer {published.fixture_publisher()[0]}"},
    )
    assert answer.status_code == 401 and "mendari site init" in answer.text


def test_nothing_is_read_past_the_limit_and_nothing_before_the_tickets_secret(
    published: Registry, tmp_path: Path
) -> None:
    huge = b'{"x": "' + b"a" * 2_100_000 + b'"}'
    assert published.client.post(PATH, content=huge).status_code == 401
    wrong = {"authorization": "Bearer not-a-secret"}
    assert published.client.post(PATH, content=huge, headers=wrong).status_code == 401
    site = verified_site(published, tmp_path)
    issued = site_ticket(published, site, shared_benchmark())
    secret = {"authorization": f"Bearer {issued['submit_secret']}"}
    assert published.client.post(PATH, content=huge, headers=secret).status_code == 413
    nan = b'{"report": {"dataset": {"n": NaN}}}'
    assert published.client.post(PATH, content=nan, headers=secret).status_code == 422


def test_a_verified_report_names_its_site_key_and_ticket_and_its_signature_checks(
    published: Registry, tmp_path: Path
) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    added = _send(published, _submission(site, issued, report), issued["submit_secret"])
    assert added.status_code == 201, added.text
    (served,) = published.client.get(LISTED).json()["reports"]
    assert (served["site"], served["key_id"], served["ticket"], served["verified"]) == (
        site.id,
        site.key_id,
        issued["ticket"]["id"],
        True,
    )
    assert served["site_name"] == "north-health (synthetic)"
    record = published.client.get(f"{LISTED}/{served['id']}").json()["record"]
    keys = published.client.get(f"/v1/sites/{site.id}").json()["keys"]
    assert [key["key_id"] for key in keys] == [record["key_id"]]
    # Anyone checks it against the site's public key, as the site's own machine signed it.
    public = archive.load_public_key(
        archive.public_pem(site.key.public_key())  # the public half of the published key id
    )
    assert archive.key_id(public) == record["key_id"]
    assert site_signing.signature_holds(record, public)
    assert published.client.get(f"{LISTED}/0123456789abcdef").status_code == 404


def test_a_correction_supersedes_and_both_stay_listed(published: Registry, tmp_path: Path) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    assert share_signed(published, site, report).status_code == 201
    (first,) = published.client.get(LISTED).json()["reports"]
    # A second report of the same data is refused; a correction names the report it replaces.
    with pytest.raises(AssertionError):
        site_ticket(published, site, report)
    corrected = site_ticket(published, site, report, supersedes=first["id"])
    assert share_signed(published, site, report, corrected).status_code == 201
    listed = published.client.get(LISTED).json()
    assert listed["superseded"] == 1
    by_id = {report["id"]: report for report in listed["reports"]}
    old = by_id.pop(first["id"])
    (new,) = by_id.values()
    assert (old["id"], old["superseded_by"]) == (first["id"], new["id"])
    assert (new["supersedes"], new["superseded_by"]) == (first["id"], None)
    # The superseded one cannot be corrected again, nor another site's.
    with pytest.raises(AssertionError):
        site_ticket(published, site, report, supersedes=first["id"])
    other = verified_site(published, tmp_path, org="south-clinic")
    with pytest.raises(AssertionError):
        site_ticket(published, other, report, supersedes=new["id"])


def test_reports_under_a_compromised_key_leave_the_pool(
    published: Registry, tmp_path: Path
) -> None:
    site = verified_site(published, tmp_path)
    assert share_signed(published, site, shared_benchmark()).status_code == 201
    published.operator.revoke_site_key(
        site.key_id, org=None, by="o", reason="laptop lost", compromised=True
    )
    (served,) = published.client.get(LISTED).json()["reports"]
    assert served["compromised_key"] is True  # listed as such; the pool (22g) leaves it out


def test_share_out_writes_exactly_what_is_sent_and_sends_nothing(
    published: Registry, tmp_path: Path
) -> None:
    """The command line's own path is in tests/test_hub_client.py (a running registry); here
    the bytes: what a site writes to read first is what the registry stores and serves."""
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    written = json.dumps(_submission(site, issued, report), indent=2)
    assert _stored(published) == []
    assert _send(published, json.loads(written), issued["submit_secret"]).status_code == 201
    (served,) = published.client.get(LISTED).json()["reports"]
    assert published.client.get(f"{LISTED}/{served['id']}").json()["record"] == json.loads(written)


def test_an_offline_ticket_and_submission_are_accepted_through_the_same_checks(
    published: Registry,
    tmp_path: Path,
    db_target: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A submission carried by hand reaches the registry through the operator's command, which
    runs every check but the ticket's secret."""
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    carried = tmp_path / "submission.json"
    tampered = tmp_path / "tampered.json"
    record = _submission(site, issued, report)
    carried.write_text(json.dumps(record))
    bad = copy.deepcopy(record)
    bad["report"]["decisions"]["false_automation"] += 1
    tampered.write_text(json.dumps(bad))
    assert main(["registry", "accept-report", str(tampered), "--db", db_target]) == 1
    assert "signature" in capsys.readouterr().err
    assert main(["registry", "accept-report", str(carried), "--db", db_target]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["ticket"] == issued["ticket"]["id"] and accepted["verified"] is True
    assert main(["registry", "accept-report", str(carried), "--db", db_target]) == 1  # once


def test_the_registry_chain_verifies_after_a_ticket_a_report_and_a_supersession(
    published: Registry, tmp_path: Path
) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    assert share_signed(published, site, report).status_code == 201
    (first,) = published.client.get(LISTED).json()["reports"]
    corrected = site_ticket(published, site, report, supersedes=first["id"])
    assert share_signed(published, site, report, corrected).status_code == 201
    result = published.operator.verify()
    assert result["ok"], result
    with published.operator._db.session(exclusive=False) as db:
        events = [row["event"] for row in db.all("SELECT event FROM hub_events ORDER BY seq")]
    assert events[-7:] == [
        "site.ticket_issued",
        "report.shared",
        "site.ticket_reported",
        "site.ticket_issued",
        "site.correction_opened",
        "report.shared",
        "site.ticket_reported",
    ]


def test_counts_that_cannot_add_up_are_refused_without_holding_the_registry(
    published: Registry, tmp_path: Path
) -> None:
    """The adversarial review of LP-22f: a signed payload whose counts could not add up kept
    the suppression check running inside the registry's write lock. It is refused, at once,
    before the lock, and the ticket stays open for a report that adds up."""
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    broken = copy.deepcopy(report)
    broken["questions"][TRIAGE.manifest.policy.primary]["calibration"][9]["wrong"] = 200
    answer = _send(published, _submission(site, issued, broken), issued["submit_secret"])
    assert answer.status_code == 422
    assert (
        _send(published, _submission(site, issued, report), issued["submit_secret"]).status_code
        == 201
    )


def test_a_recalled_version_takes_no_report_on_a_ticket_issued_before(
    published: Registry, tmp_path: Path
) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    issued = site_ticket(published, site, report)
    published.operator.delete(NAMESPACE, NAME, TRIAGE.version, reason="test", by="operator")
    answer = _send(published, _submission(site, issued, report), issued["submit_secret"])
    assert answer.status_code in (404, 409)
    assert _stored(published) == []


def test_a_correction_can_itself_be_corrected(published: Registry, tmp_path: Path) -> None:
    site = verified_site(published, tmp_path)
    report = shared_benchmark()
    assert share_signed(published, site, report).status_code == 201
    first = published.client.get(LISTED).json()["reports"][0]["id"]
    second_ticket = site_ticket(published, site, report, supersedes=first)
    assert share_signed(published, site, report, second_ticket).status_code == 201
    second = next(
        r["id"] for r in published.client.get(LISTED).json()["reports"] if r["id"] != first
    )
    third = site_ticket(published, site, report, supersedes=second)
    assert share_signed(published, site, report, third).status_code == 201
    listed = published.client.get(LISTED).json()
    assert listed["superseded"] == 2


def test_the_client_sends_only_a_submission_it_can_read(tmp_path: Path) -> None:
    from mendari.errors import MendariError
    from mendari.hub.client import RegistryClient

    with RegistryClient("http://127.0.0.1:9") as client, pytest.raises(MendariError):
        client.submit({"report": {"pack": {"id": "../../v1/tickets", "version": "x"}}}, "s")
