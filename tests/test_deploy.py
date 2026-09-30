"""Per-pack images (OPENHEALTH_PACKS) and the on-prem templates in deploy/."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from openhealth.errors import OpenHealthError
from openhealth.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def test_a_per_pack_server_serves_only_its_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENHEALTH_PACKS", "openhealth/referral-router")
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert [p["id"] for p in client.get("/api/packs").json()] == ["openhealth/referral-router"]
    assert client.get("/api/packs/openhealth/patient-inbox-router").status_code == 404


def test_a_pack_that_is_not_installed_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENHEALTH_PACKS", "openhealth/referal-router")  # a typo
    with pytest.raises(OpenHealthError, match="referal-router"):
        create_app(db_path=tmp_path / "a.db")


def test_the_templates_parse_and_hold_no_secrets() -> None:
    compose = yaml.safe_load((DEPLOY / "onprem" / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {"postgres", "engine", "jobs"}
    manifests = list(yaml.safe_load_all((DEPLOY / "kubernetes" / "openhealth.yaml").read_text()))
    kinds = [m["kind"] for m in manifests]
    assert kinds == ["ConfigMap", "Deployment", "Service", "CronJob"]
    config = manifests[0]["data"]
    assert not {"DATABASE_URL", "OPENHEALTH_PHI_KEY", "OPENHEALTH_OIDC_CLIENT_SECRET"} & set(config)
    example = dict(
        line.split("=", 1)
        for line in (DEPLOY / "onprem" / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    for secret in ("POSTGRES_PASSWORD", "OPENHEALTH_PHI_KEY", "OPENHEALTH_OIDC_CLIENT_SECRET"):
        assert example[secret] == "", secret


def test_every_setting_the_templates_use_is_documented() -> None:
    used = set()
    for path in DEPLOY.rglob("*"):
        if path.is_file():
            used |= set(re.findall(r"OPENHEALTH_[A-Z_]+", path.read_text()))
    documented = (ROOT / "docs" / "deploying-with-phi.md").read_text() + (
        ROOT / "README.md"
    ).read_text()
    assert sorted(s for s in used if s not in documented) == []
