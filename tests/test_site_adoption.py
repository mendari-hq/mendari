# SPDX-License-Identifier: Apache-2.0
"""The one-machine install can adopt an agent without a terminal (`deploy/site`,
`mendari.install`): a server on the settings an install wrote, a registry the installer was
told, and the first administrator in the console from there on.

What the install's settings must already hold for that: the organization's agent store on, the
registry's address, and its administrators as the deployment's operators, so that the list of
addresses the deployment may call, which names none as installed, does not send them back to a
file to connect an agent's service at a public address. What they must not shorten: a version
is added by one person and started, and approved, by another.

Nothing leaves this machine: the registry is a real one on a local port, the identity provider
is the tests' own, and the public-only client a connection is called through is replaced."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, install
from mendari.core.phi import phi_policy
from mendari.hub import yank
from mendari.providers import reach
from tests.conftest import member
from tests.test_agent_connections import Outside
from tests.test_auth import FakeIdp, sign_in
from tests.test_hub_client import Hub, hub  # noqa: F401 - the live registry fixture
from tests.test_hub_routes import ACME, REFILL, as_published
from tests.test_site_install import ADA, ANSWERS, run, serving

SERVICE = "https://agents.example.org/v1/decide"
ORIGIN = "https://agents.example.org"
TOKEN = "zqTOKEN-site-3b7d-never-shown"


def checks(client: Any) -> dict[str, dict[str, Any]]:
    answer = client.get("/api/system/checks")
    assert answer.status_code == 200, answer.text
    return {check["key"]: check for check in answer.json()["checks"]}


def test_the_first_administrator_adopts_an_agent_on_an_installs_settings_without_a_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub: Hub,  # noqa: F811
) -> None:
    # The registry counter-signs what it lists, which the install's settings require.
    monkeypatch.setenv(yank.KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    outside = Outside()
    monkeypatch.setattr(reach, "resolve", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(reach, "public_client", lambda: outside.client)
    token, key = hub.namespace("acme")
    pack = as_published(tmp_path, ACME)
    hub.client(token).push(pack, key)

    site = tmp_path / "site"
    made = run(site, {**ANSWERS, "REGISTRY_URL": hub.url}).values
    # As installed: the store on, the registry named, and no address the deployment may call.
    assert (made["MENDARI_AGENT_STORE"], made["MENDARI_REGISTRY_URL"]) == ("on", hub.url)
    assert made["MENDARI_PROVIDER_ALLOWED_ORIGINS"] == install.NONE_LISTED
    assert made["MENDARI_REGISTRY_COUNTERSIGNATURE"] == "required"
    idp = FakeIdp()
    database = tmp_path / "site.db"
    with serving(site, monkeypatch, idp, database) as client:
        assert sign_in(client, idp, sub="ada", email=ADA, email_verified=True).status_code == 302
        me = client.get("/api/auth/me").json()["principal"]
        assert me["roles"] == ["admin"] and "operate" in me["permissions"]
        first = checks(client)
        # The registry answers and agents can be added; only the demo agent is served so far,
        # and "Finish setting up" ends there.
        assert first["registry"]["state"] == "ok", first["registry"]
        assert "can be found and added in the Agents view" in first["registry"]["detail"]
        assert first["agents"]["state"] == "not_set"
        assert first["agents"]["action"] == "Adopt an agent from the Agents view."
        told = client.get("/api/hub").json()
        assert told == {"registry": hub.url, "add": {"available": True, "reason": None}}
        assert "author" not in me["permissions"]  # the administrator is not an author yet
        assert [found["id"] for found in client.get("/api/hub/search").json()["agents"]] == [ACME]

        # The administrator gives themself the author's role, in Access, and adds the agent.
        [mine] = [m for m in client.get("/api/auth/members").json() if m["you"]]
        given = client.put(
            f"/api/auth/members/{mine['id']}/roles", json={"roles": ["admin", "pack_author"]}
        )
        assert given.status_code == 200, given.text
        assert "author" in client.get("/api/auth/me").json()["principal"]["permissions"]
        added = client.post("/api/store/pulls", json={"ref": ACME})
        assert added.status_code == 201, added.text
        job = client.post(
            "/api/store/jobs", json={"version": pack.digest, "dataset": {"name": "examples"}}
        )
        assert job.status_code == 201, job.text
        for _ in range(200):
            step = client.post(f"/api/store/jobs/{job.json()['id']}/advance").json()
            if step["state"] in ("done", "failed"):
                break
        assert step["state"] == "done", step
        promotion = {
            "version": pack.digest,
            "reports": [step["report_id"]],
            "reference": "AI committee, item 2",
            "automate": True,
        }
        approval = {"pack_id": ACME, "pack_digest": pack.digest, "reference": "minutes 9"}
        # The two-person rule: alone, they can neither start serving what they added nor
        # approve it. An organization of one person stops here, by design.
        alone = client.post("/api/store/promotions", json=promotion)
        assert alone.status_code == 403, alone.text
        assert client.post(f"/api/agents/{ACME}/decide", json=REFILL).status_code == 404

        # A second administrator, invited from Access, starts serving it.
        with AuditStore(database, phi=phi_policy()) as store:
            bo = member(
                store,
                org="default",
                roles=["admin"],
                sub="bo",
                email="bo@stmarys.example",
                host="mendari.stmarys.example",
            )
        ada = dict(client.cookies)
        client.cookies.clear()
        started = client.post("/api/store/promotions", json=promotion, headers=bo)
        assert started.status_code == 201, started.text
        for name, value in ada.items():
            client.cookies.set(name, value)

        # The first administrator connects its service. The deployment's list names nothing;
        # as one of its operators they allow this one address for this agent, and nothing is
        # edited on the machine.
        at = f"/api/connections/{ACME}"
        before = client.get(at).json()
        assert before["allowed_origins"] == [] and before["may_allow_origin"] is True
        refused = client.put(at, json={"endpoint": SERVICE, "token": TOKEN})
        assert refused.status_code == 422 and ORIGIN in refused.json()["detail"]
        inside = client.put(at, json={"endpoint": "https://10.0.0.5/decide", "allow_origin": True})
        assert inside.status_code == 422, inside.text
        connected = client.put(at, json={"endpoint": SERVICE, "token": TOKEN, "allow_origin": True})
        assert connected.status_code == 200, connected.text
        assert connected.json()["connection"]["origin_allowed_by"] == ADA
        checked = client.post(f"{at}/check")
        assert checked.status_code == 200 and checked.json()["ok"] is True, checked.text

        # Served and connected, a person still decides until someone other than whoever added
        # it approves this exact version.
        waiting = client.post(f"/api/agents/{ACME}/decide", json=REFILL).json()
        assert waiting["outcome"] == "HUMAN_REVIEW"
        assert client.post("/api/registry-approvals", json=approval).status_code == 403
        client.cookies.clear()
        approved = client.post("/api/registry-approvals", json=approval, headers=bo)
        assert approved.status_code == 201, approved.text
        for name, value in ada.items():
            client.cookies.set(name, value)
        decided = client.post(f"/api/agents/{ACME}/decide", json=REFILL).json()
        assert decided["outcome"] == "AUTO", decided["reasons"]
        assert {str(request.url) for request in outside.seen} == {SERVICE}
        assert set(outside.authorizations()) == {f"Bearer {TOKEN}"}

        after = checks(client)
        assert after["agents"]["state"] == "ok" and "from a registry" in after["agents"]["detail"]
        assert after["registry"]["state"] == "ok"
        assert client.get("/api/system/checks").json()["setup"]["fresh"] is False
        said = client.get("/api/audit/administrative").json()["items"]
        kinds = [item["event"] for item in said]
        for kind in (
            "pack_version.created",
            "pack_version.promoted",
            "agent_connection.origin_allowed",
            "agent_connection.set",
            "registry_pack.approved",
        ):
            assert kind in kinds, kind
        assert TOKEN not in client.get("/api/audit/administrative").text
    with AuditStore(database, phi=phi_policy()) as store:
        assert store.verify()["ok"]
