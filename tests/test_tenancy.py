# SPDX-License-Identifier: Apache-2.0
"""Tenancy fails closed (LP-12): for a signed-in principal acting for no organization, every
read and write that resolves its organization through `tenancy.current()` is refused, never
widened to all organizations (None, which is open mode and the command line) or to the default
one. And an empty organization is an organization, never "any"."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.core import tenancy
from mendari.errors import AuthError, MendariError, NoOrganization
from tests.conftest import message
from tests.test_backend_services import RSA_KEY

REFILL = "Hi, I need a refill on my metoprolol."


def test_none_is_every_organization_and_nobody_is_none() -> None:
    assert tenancy.current() is None  # open mode, the command line
    with tenancy.acting_for("clinic-a", "Ada"):
        assert (tenancy.current(), tenancy.requester()) == ("clinic-a", "Ada")
    with tenancy.acting_for_nobody("Bo"):
        with pytest.raises(NoOrganization):
            tenancy.current()
        assert tenancy.requester() == "Bo"
        with tenancy.acting_for("clinic-a"):  # an operator acting inside, say
            assert tenancy.current() == "clinic-a"
        with pytest.raises(NoOrganization):
            tenancy.current()
    assert tenancy.current() is None and tenancy.requester() is None


def test_nobody_reads_any_organization_data(store: AuditStore) -> None:
    reads: list[Callable[[], Any]] = [
        lambda: store.query(),
        lambda: store.events(),
        lambda: store.stats(),
        lambda: store.feedback(),
        lambda: store.reports(),
        lambda: store.approvals(),
        lambda: store.decision_rows("mendari/patient-inbox-router", "2026-01-01"),
    ]
    with tenancy.acting_for_nobody("Bo"):
        for read in reads:
            with pytest.raises(NoOrganization):
                read()


def test_nobody_writes_any_organization_data(store: AuditStore, inbox_pack: Pack) -> None:
    inputs = message(REFILL)
    result = Decision(inbox_pack)(inputs)
    before = store.verify()
    with tenancy.acting_for_nobody("Bo"):
        with pytest.raises(NoOrganization):
            store.record(result, inputs)
        with pytest.raises(NoOrganization):
            store.acknowledge_notice("person:bo", version="1", actor="Bo")
        with pytest.raises(NoOrganization):
            store.approve_automation(
                pack_id=inbox_pack.id,
                pack_digest=inbox_pack.digest,
                risk_level=2,
                approver="Bo",
                reference="ref",
                expires_at="2099-01-01T00:00:00+00:00",
            )
    after = store.verify()
    assert after["ok"] and after["events"] == before["events"]  # nothing chained
    assert store.query() == []  # and nothing recorded


def test_an_empty_organization_is_one_not_every(store: AuditStore) -> None:
    key = store.create_api_key(name="k", role="viewer", org="clinic-a", created_by="Ada")
    assert store.api_keys("") == [] and len(store.api_keys(None)) == 1
    assert store.revoke_api_key(key["id"], "", revoked_by="Mallory") is False
    with pytest.raises(MendariError):
        store.rotate_api_key(key["id"], "", rotated_by="Mallory")
    assert store.api_key(key["key"]) is not None  # still in force, and only once
    assert [k["id"] for k in store.api_keys("clinic-a")] == [key["id"]]
    client = store.register_client(
        name="c", role="viewer", org="clinic-a", created_by="Ada", scopes="system/*.rs",
        jwks=[RSA_KEY.jwk],
    )  # fmt: skip
    assert store.clients("") == [] and len(store.clients(None)) == 1
    assert store.revoke_client(client["client_id"], "", revoked_by="Mallory") is False
    assert store.client(client["client_id"]) is not None  # still in force
    # A credential's history (LP-12h): another organization's, "" included, is none.
    assert store.subject_history("key", key["id"], "") is None
    assert store.subject_history("client", client["client_id"], "") is None
    assert store.subject_history("key", key["id"], "clinic-a") is not None
    with pytest.raises(AuthError) as refused:
        store.replace_client_keys(
            client["client_id"], "", replaced_by="Mallory", jwks=[RSA_KEY.jwk]
        )
    assert refused.value.status == 404
    assert store.client(client["client_id"])["keys_version"] == 0  # type: ignore[index]
