# SPDX-License-Identifier: Apache-2.0
"""An organization's agent store runs a version's tests as `mendari test` does: the probe's
regression tests (`tests/probe.yaml`) with the agent's own, all counted against the most a
version may hold here."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari.core.pack import load_pack
from mendari.evals.pack_tests import PROBE_TESTS
from mendari.hub import archive
from mendari.server import store_routes
from mendari.server.app import create_app
from tests.conftest import DASHBOARD, encrypt_mode, encrypted_store, evalset_on, member
from tests.probe_agents import MANIFEST, build_agent
from tests.test_probe import found_test

ORG = "clinic-a"
HTTPS = "https://mendari.test"


def test_a_stored_version_runs_and_counts_the_probes_tests(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    encrypt_mode(monkeypatch)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    monkeypatch.setenv("MENDARI_AGENT_STORE", "on")
    evalset_on(monkeypatch)
    root = build_agent(tmp_path / "agent", manifest={"id": f"{ORG}/probe-triage"})
    assert MANIFEST["id"] != f"{ORG}/probe-triage"
    (root / PROBE_TESTS).write_text(yaml.safe_dump([found_test()]), encoding="utf-8")
    private, public = archive.generate_key()

    app = create_app(db_path=db_target)
    with (
        TestClient(app, base_url=HTTPS, headers=DASHBOARD) as client,
        encrypted_store(db_target) as db,
    ):
        admin = member(db, org=ORG, roles=["admin"], sub="an-admin")
        author = member(db, org=ORG, roles=["pack_author"], sub="an-author")
        added = client.post("/api/store/keys", json={"public_pem": public.decode()}, headers=admin)
        assert added.status_code == 201, added.text
        data = archive.build(load_pack(root), archive.load_private_key(private))
        stored = client.post("/api/store/versions", content=data, headers=author)
        assert stored.status_code == 201, stored.text
        tests = f"/api/store/versions/{stored.json()['digest']}/tests"

        tested = client.post(tests, headers=author)
        assert tested.status_code == 200, tested.text
        # The agent's own two pass; the probe's fails until the agent is fixed.
        assert [case["passed"] for case in tested.json()["cases"]] == [True, True, False]
        assert (tested.json()["passed"], tested.json()["failed"]) == (2, 1)

        monkeypatch.setattr(store_routes, "MAX_TEST_CASES", 2)
        refused = client.post(tests, headers=author)
        assert refused.status_code == 422
        assert refused.json()["detail"] == "a version has at most 2 tests here"
