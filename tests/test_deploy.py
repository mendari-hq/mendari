# SPDX-License-Identifier: Apache-2.0
"""Per-pack images (MENDARI_PACKS) and the on-prem templates in deploy/."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari.errors import MendariError
from mendari.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def test_a_per_pack_server_serves_only_its_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PACKS", "mendari/referral-router")
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert [p["id"] for p in client.get("/api/packs").json()] == ["mendari/referral-router"]
    assert client.get("/api/packs/mendari/patient-inbox-router").status_code == 404


def test_a_pack_that_is_not_installed_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PACKS", "mendari/referal-router")  # a typo
    with pytest.raises(MendariError, match="referal-router"):
        create_app(db_path=tmp_path / "a.db")


def test_the_templates_parse_and_hold_no_secrets() -> None:
    compose = yaml.safe_load((DEPLOY / "onprem" / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {"postgres", "engine", "jobs"}
    manifests = list(yaml.safe_load_all((DEPLOY / "kubernetes" / "mendari.yaml").read_text()))
    kinds = [m["kind"] for m in manifests]
    assert kinds == ["ConfigMap", "Deployment", "Service", "CronJob"]
    config = manifests[0]["data"]
    secret_names = {"MENDARI_PHI_KEY", "MENDARI_INPUT_HASH_KEY", "MENDARI_OIDC_CLIENT_SECRET"}
    assert not {"DATABASE_URL", *secret_names} & set(config)
    example = dict(
        line.split("=", 1)
        for line in (DEPLOY / "onprem" / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    for secret in ("POSTGRES_PASSWORD", *secret_names):
        assert example[secret] == "", secret


def test_compose_trusts_x_forwarded_for_from_the_gateway_a_proxy_on_the_host_comes_from() -> None:
    """Docker's port publishing connects to the engine from the network's gateway, not from
    127.0.0.1: the network's gateway is fixed, and FORWARDED_ALLOW_IPS defaults to it (in the
    compose file and in .env.example), so sign-in limits and the log see each person's address."""
    compose = yaml.safe_load((DEPLOY / "onprem" / "docker-compose.yml").read_text())
    (network,) = compose["networks"]["default"]["ipam"]["config"]
    gateway = ipaddress.ip_address(network["gateway"])
    assert gateway in ipaddress.ip_network(network["subnet"]) and str(gateway) == "172.30.0.1"
    engine = compose["services"]["engine"]
    assert engine["ports"] == ["127.0.0.1:8000:8000"]
    assert engine["environment"]["FORWARDED_ALLOW_IPS"] == "${FORWARDED_ALLOW_IPS:-172.30.0.1}"
    example = dict(
        line.split("=", 1)
        for line in (DEPLOY / "onprem" / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert example["FORWARDED_ALLOW_IPS"] == str(gateway)


def test_every_setting_the_templates_use_is_documented() -> None:
    used = set()
    for path in DEPLOY.rglob("*"):
        if path.is_file():
            used |= set(re.findall(r"MENDARI_[A-Z_]+", path.read_text()))
    documented = (ROOT / "docs" / "deploying-with-phi.md").read_text() + (
        ROOT / "README.md"
    ).read_text()
    assert sorted(s for s in used if s not in documented) == []
