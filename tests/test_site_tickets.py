# SPDX-License-Identifier: Apache-2.0
"""Evaluation tickets (LP-22e, ADR-0017): before a run a verified site asks for a ticket bound
to the site, the pack's digest, the configuration, the dataset's size and a keyed commitment to
the dataset, signed with a live site key. Synthetic sites and data only; every key and secret
is made here."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.cli import _site_configuration, main
from mendari.core.pack import load_pack
from mendari.errors import MendariError
from mendari.hub import site_signing, sites
from mendari.hub.registry import create_registry_app
from mendari.hub.site_signing import Site, init_site
from tests.conftest import FIXTURE_NAMESPACE, TRIAGE_DIR, Registry

# The agent `Registry.publish()` publishes; a site's own labelled cases for it (here its test
# set's file), and how many.
TRIAGE = load_pack(TRIAGE_DIR)
NAMESPACE, NAME = TRIAGE.id.split("/")
DATASET = TRIAGE_DIR / "data" / "test.jsonl"
N = len(DATASET.read_text().splitlines())
# A configuration that decides on the site's machines (its keyword rules, the default), and one
# that asks a service (its screen's agent).
LOCAL = {"router": "rules", "screen": "rules"}
REMOTE = {"router": "rules", "screen": "agent"}


def _site(registry: Registry, tmp_path: Path, *, org: str = "north-health") -> Site:
    """A verified synthetic site with one key, its files in a directory of its own."""
    store = registry.operator
    record = store.register_site(org=org, name=f"{org} (synthetic)", by="user:1")
    where = tmp_path / f"site-{record['id']}"
    made = init_site(record["id"], directory=where)
    store.add_site_key(record["id"], made["public_pem"], made["proof"], org=None, by="o")
    store.decide_site(record["id"], verify=True, by="user:reviewer", note="ok", by_org=None)
    return Site(where)


def _request(site: Site, **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "pack": {"id": TRIAGE.id, "version": TRIAGE.version, "digest": TRIAGE.digest},
        "configuration": LOCAL,
        "remote_consent": False,
        "commitment": site.commitment(DATASET.read_bytes()),
        "n": N,
    }
    fields.update(changes)
    return site.sign(site_signing.TICKET_REQUEST, fields)


def _post(registry: Registry, record: Any, client: TestClient | None = None) -> Any:
    return (client or registry.client).post("/v1/tickets", json={"record": record})


@pytest.fixture
def published(registry: Registry) -> Registry:
    assert registry.publish().status_code == 201
    return registry


def _plant_a_pending_claim(registry: Registry) -> None:
    """A pending claim on the published agent's namespace, written past the registry: what it
    publishes is then unlisted."""
    with registry.operator._db.session(exclusive=True) as db:
        db.all(
            "INSERT INTO hub_claims (namespace, org, claimed_by, created_at, status) "
            "VALUES (?, 'mendari', 'u', '2026-01-01T00:00:00+00:00', 'pending')",
            (FIXTURE_NAMESPACE,),
        )


def test_a_ticket_binds_site_digest_configuration_size_and_commitment(
    published: Registry, tmp_path: Path
) -> None:
    site = _site(published, tmp_path)
    answer = _post(published, _request(site))
    assert answer.status_code == 201, answer.text
    ticket, secret = answer.json()["ticket"], answer.json()["submit_secret"]
    assert (ticket["site"], ticket["key_id"]) == (site.id, site.key_id)
    assert (ticket["version"], ticket["digest"]) == (TRIAGE.version, TRIAGE.digest)
    assert ticket["configuration"] == LOCAL and ticket["n"] == N
    assert ticket["commitment"] == site.commitment(DATASET.read_bytes())
    assert (ticket["status"], ticket["remote_consent"]) == ("open", False)
    assert ticket["expires_at"] > ticket["created_at"]
    assert "submit_sha256" not in ticket and "nonce" not in ticket
    with published.operator._db.session(exclusive=False) as db:
        row = db.one("SELECT submit_sha256 FROM hub_tickets WHERE id = ?", (ticket["id"],))
    assert row is not None and row["submit_sha256"] == hashlib.sha256(secret.encode()).hexdigest()
    assert published.operator.verify()["ok"]


def test_a_ticket_request_with_a_forged_signature_or_an_unregistered_key_is_refused(
    published: Registry, tmp_path: Path
) -> None:
    site = _site(published, tmp_path)
    forged = {**_request(site), "n": N + 1}  # signed over n = N
    answer = _post(published, forged)
    assert answer.status_code == 403 and "signature" in answer.text
    stranger = tmp_path / "stranger"
    init_site(site.id, directory=stranger)  # the same site's id, a key never registered
    unregistered = _post(published, _request(Site(stranger)))
    assert unregistered.status_code == 403
    assert unregistered.json()["detail"] == sites.NOT_A_SITE_KEY
    garbled = {**_request(site), "signature": "not base64"}
    assert _post(published, garbled).status_code == 403
    for broken in ({**_request(site), "nonce": "1"}, {**_request(site), "extra": 1}, "x"):
        assert _post(published, broken).status_code in (403, 422)


def test_a_revoked_key_and_a_pending_site_get_no_ticket(
    published: Registry, tmp_path: Path
) -> None:
    site = _site(published, tmp_path)
    published.operator.revoke_site_key(
        site.key_id, org=None, by="o", reason="rotated", compromised=False
    )
    revoked = _post(published, _request(site))
    assert revoked.status_code == 403 and revoked.json()["detail"] == sites.NOT_A_SITE_KEY
    pending = published.operator.register_site(org="south", name="South (synthetic)", by="u")
    where = tmp_path / "pending"
    made = init_site(pending["id"], directory=where)
    published.operator.add_site_key(
        pending["id"], made["public_pem"], made["proof"], org=None, by="o"
    )
    waiting = _post(published, _request(Site(where)))
    assert waiting.status_code == 403 and waiting.json()["detail"] == sites.NOT_A_SITE_KEY


def test_a_replayed_ticket_request_is_refused(published: Registry, tmp_path: Path) -> None:
    site = _site(published, tmp_path)
    record = _request(site)
    assert _post(published, record).status_code == 201
    again = _post(published, record)
    assert again.status_code == 409 and "nonce" in again.text
    stale = site.sign(
        site_signing.TICKET_REQUEST,
        {k: v for k, v in record.items() if k not in (*site_signing.COMMON, "signature")},
        at="2026-01-01T00:00:00Z",
    )
    assert _post(published, stale).status_code == 422


def test_a_digest_that_is_not_the_published_versions_is_refused(
    published: Registry, tmp_path: Path
) -> None:
    site = _site(published, tmp_path)
    other = {"id": TRIAGE.id, "version": TRIAGE.version, "digest": "sha256:" + "0" * 64}
    answer = _post(published, _request(site, pack=other))
    assert answer.status_code == 409 and "digest" in answer.text
    unknown = {**other, "version": "9.9.9", "digest": TRIAGE.digest}
    assert _post(published, _request(site, pack=unknown)).status_code == 404


def test_a_recalled_deleted_or_unlisted_version_gets_no_ticket(
    published: Registry, tmp_path: Path
) -> None:
    site = _site(published, tmp_path)
    _plant_a_pending_claim(published)
    unlisted = _post(published, _request(site))
    assert unlisted.status_code == 404
    with published.operator._db.session(exclusive=True) as db:
        db.all("DELETE FROM hub_claims")
    published.operator.delete(NAMESPACE, NAME, TRIAGE.version, reason="test", by="operator")
    deleted = _post(published, _request(site))
    assert deleted.status_code in (404, 409)


def test_a_remote_configuration_needs_recorded_consent_and_the_default_is_deterministic(
    published: Registry, tmp_path: Path
) -> None:
    site = _site(published, tmp_path)
    refused = _post(published, _request(site, configuration=REMOTE))
    assert refused.status_code == 409 and "consent" in refused.text
    given = _post(published, _request(site, configuration=REMOTE, remote_consent=True))
    assert given.status_code == 201 and given.json()["ticket"]["remote_consent"] is True
    # The command line: with nothing named, the deterministic arm; a remote one needs consent.
    assert _site_configuration(TRIAGE, None, consent=False) == LOCAL
    named = [f"{slot}={variant}" for slot, variant in REMOTE.items()]
    with pytest.raises(MendariError, match="--consent-remote"):
        _site_configuration(TRIAGE, named, consent=False)
    assert _site_configuration(TRIAGE, named, consent=True) == REMOTE
    unknown = _post(published, _request(site, configuration={**LOCAL, "checklist": "nope"}))
    assert unknown.status_code == 422
    partial = _post(published, _request(site, configuration=dict(list(LOCAL.items())[:1])))
    assert partial.status_code == 422


def test_one_live_ticket_per_site_digest_configuration_and_commitment(
    published: Registry, tmp_path: Path
) -> None:
    site = _site(published, tmp_path)
    first = _post(published, _request(site)).json()["ticket"]
    second = _post(published, _request(site))
    assert second.status_code == 409 and "one live result" in second.text
    other_data = _post(published, _request(site, commitment=site.commitment(b"other cases")))
    assert other_data.status_code == 201  # another dataset is another result
    withdrawal = site.sign(site_signing.TICKET_WITHDRAWAL, {"ticket": first["id"]})
    withdrawn = published.client.post(
        f"/v1/tickets/{first['id']}/withdraw", json={"record": withdrawal}
    )
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    # Withdrawing never frees the dataset for a second run (the adversarial review of 22e).
    again = _post(published, _request(site))
    assert again.status_code == 409 and "one live result" in again.text
    # Another site cannot withdraw this one's ticket.
    other = _site(published, tmp_path, org="south-clinic")
    ticket = _post(published, _request(other)).json()["ticket"]
    theirs = site.sign(site_signing.TICKET_WITHDRAWAL, {"ticket": ticket["id"]})
    answer = published.client.post(f"/v1/tickets/{ticket['id']}/withdraw", json={"record": theirs})
    assert answer.status_code == 404


def test_a_ticket_with_no_result_is_listed_as_unreported_and_an_expired_one_stays_listed(
    published: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(published, tmp_path)
    monkeypatch.setattr(sites, "TICKET_DAYS", 0)  # expires as it is issued
    expired = _post(published, _request(site)).json()["ticket"]
    assert published.operator.ticket(expired["id"])["status"] == "expired"
    monkeypatch.setattr(sites, "TICKET_DAYS", 30)
    assert _post(published, _request(site)).status_code == 201  # an expired one is not live
    listed = published.client.get(f"/v1/agents/{TRIAGE.id}/tickets").json()["tickets"]
    (group,) = listed
    assert group["site"] == site.id and group["site_name"] == "north-health (synthetic)"
    assert (group["reported"], group["unreported"], group["withdrawn"]) == (0, 2, 0)
    text = json.dumps(listed)
    assert "commitment" not in text and "secret" not in text and "hmac" not in text


def test_the_commitment_is_keyed_and_the_plain_hash_is_never_sent(
    published: Registry, tmp_path: Path
) -> None:
    site = _site(published, tmp_path)
    data = DATASET.read_bytes()
    record = _request(site)
    sent = json.dumps(record)
    secret = (tmp_path / f"site-{site.id}" / site_signing.SECRET_FILE).read_bytes()
    assert hashlib.sha256(data).hexdigest() not in sent
    assert secret.decode() not in sent and base64.b64decode(secret).hex() not in sent
    assert site.commitment(data) == site.commitment(data)  # the same file, the same value
    other = tmp_path / "other"
    init_site(site.id, directory=other)
    assert Site(other).commitment(data) != site.commitment(data)  # another secret, another


def test_the_sites_key_and_secret_are_written_private(tmp_path: Path) -> None:
    where = tmp_path / "site"
    made = init_site("0123456789abcdef", directory=where)
    for name in (site_signing.SITE_FILE, site_signing.KEY_FILE, site_signing.SECRET_FILE):
        assert stat.S_IMODE((where / name).stat().st_mode) == 0o600, name
    assert "PRIVATE" not in json.dumps(made)
    with pytest.raises(MendariError, match="already"):
        init_site("0123456789abcdef", directory=where)
    rotated = init_site("0123456789abcdef", directory=where, new_key=True)
    assert rotated["key_id"] != made["key_id"]
    assert (where / site_signing.SECRET_FILE).read_bytes()  # the secret stays


def test_a_new_key_keeps_every_key_it_replaces(tmp_path: Path) -> None:
    # A second `site key --new` before the first new key was registered used to write over the
    # one backup, losing the key the registry still knew.
    where = tmp_path / "site"
    first = init_site("0123456789abcdef", directory=where)
    first_pem = (where / site_signing.KEY_FILE).read_bytes()
    second = init_site("0123456789abcdef", directory=where, new_key=True)
    second_pem = (where / site_signing.KEY_FILE).read_bytes()
    init_site("0123456789abcdef", directory=where, new_key=True)
    for made, pem in ((first, first_pem), (second, second_pem)):
        kept = site_signing.retired_key_path(where, made["key_id"])
        assert kept.read_bytes() == pem
        assert stat.S_IMODE(kept.stat().st_mode) == 0o600


def test_site_key_new_says_where_the_old_key_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MENDARI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    assert main(["site", "init", "--site", "0123456789abcdef", "--out", "a.json"]) == 0
    old = json.loads((tmp_path / "a.json").read_text())["key_id"]
    capsys.readouterr()
    assert main(["site", "key", "--new", "--out", "b.json"]) == 0
    kept = site_signing.retired_key_path(tmp_path / "config", old)
    assert kept.is_file() and f"the old key ({old}) is kept in {kept}" in capsys.readouterr().err


def test_a_recall_or_a_scoreboard_signature_is_not_a_sites(
    published: Registry, tmp_path: Path
) -> None:
    """A site key signing the same fields as a recall would (bare canonical JSON) makes no site
    signature: the prefix and the format are a site's own."""
    site = _site(published, tmp_path)
    record = _request(site)
    fields = {k: v for k, v in record.items() if k != "signature"}
    bare = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    signed = {**record, "signature": base64.b64encode(site.key.sign(bare)).decode()}
    answer = _post(published, signed)
    assert answer.status_code == 403 and "signature" in answer.text
    # And a ticket request is never a withdrawal: the format is signed.
    as_withdrawal = {**record, "format": site_signing.TICKET_WITHDRAWAL}
    assert not site_signing.signature_holds(as_withdrawal, site.key.public_key())


def test_a_ticket_request_is_read_to_8_kb_and_limited_per_address_and_key(
    published: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_target: str
) -> None:
    site = _site(published, tmp_path)
    huge = {"record": {**_request(site), "padding": "x" * 9_000}}
    assert published.client.post("/v1/tickets", json=huge).status_code == 413
    monkeypatch.setenv(
        "MENDARI_REGISTRY_RATE_LIMITS", "on,registry_ticket=4/h:4,registry_ticket_key=2/h:2"
    )
    with TestClient(create_registry_app(db_target)) as limited:
        codes = [_post(published, _request(site, n=N + i), limited).status_code for i in range(3)]
        assert codes[:2] == [201, 409] and codes[2] == 429  # the key's own bucket
        unsigned = [_post(published, {"x": i}, limited).status_code for i in range(3)]
        assert unsigned[-1] == 429  # the address's, before any signature is read


def test_an_old_or_replayed_record_never_counts_against_the_sites_key(
    published: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_target: str
) -> None:
    """The adversarial review of LP-22e: a record anyone saw (old, or sent before) was counted
    against its key's rate before its time and nonce were read, so it could lock the site out."""
    site = _site(published, tmp_path)
    old = site.sign(
        site_signing.TICKET_REQUEST,
        {
            "pack": {"id": TRIAGE.id, "version": TRIAGE.version, "digest": TRIAGE.digest},
            "configuration": LOCAL,
            "remote_consent": False,
            "commitment": site.commitment(b"x"),
            "n": N,
        },
        at="2026-01-01T00:00:00Z",
    )
    used = _request(site)
    assert _post(published, used).status_code == 201
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "on,registry_ticket_key=1/h:1")
    with TestClient(create_registry_app(db_target)) as limited:
        for _ in range(4):
            assert _post(published, old, limited).status_code == 422
            assert _post(published, used, limited).status_code == 409
        fresh = _request(site, commitment=site.commitment(b"y"))
        assert _post(published, fresh, limited).status_code == 201


def test_a_closed_sites_unreported_tickets_stay_listed(published: Registry, tmp_path: Path) -> None:
    site = _site(published, tmp_path)
    assert _post(published, _request(site)).status_code == 201
    published.operator.close_site(site.id, org=None, by="o", reason="merged")
    (group,) = published.client.get(f"/v1/agents/{TRIAGE.id}/tickets").json()["tickets"]
    assert (group["site"], group["site_status"], group["unreported"]) == (site.id, "closed", 1)


def test_the_tickets_of_a_pack_nobody_may_read_are_a_404(
    published: Registry, tmp_path: Path
) -> None:
    site = _site(published, tmp_path)
    assert _post(published, _request(site)).status_code == 201
    assert published.client.get("/v1/agents/mendari/no-such-pack/tickets").status_code == 404
    _plant_a_pending_claim(published)
    hidden = published.client.get(f"/v1/agents/{TRIAGE.id}/tickets")
    assert hidden.status_code == 404 and site.id not in hidden.text


def test_an_expired_ticket_is_not_withdrawn_and_a_withdrawal_is_one_field(
    published: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(published, tmp_path)
    monkeypatch.setattr(sites, "TICKET_DAYS", 0)
    ticket = _post(published, _request(site)).json()["ticket"]
    late = site.sign(site_signing.TICKET_WITHDRAWAL, {"ticket": ticket["id"]})
    answer = published.client.post(f"/v1/tickets/{ticket['id']}/withdraw", json={"record": late})
    assert answer.status_code == 409 and "expired" in answer.text
    padded = site.sign(site_signing.TICKET_WITHDRAWAL, {"ticket": ticket["id"], "note": "x"})
    answer = published.client.post(f"/v1/tickets/{ticket['id']}/withdraw", json={"record": padded})
    assert answer.status_code == 422
