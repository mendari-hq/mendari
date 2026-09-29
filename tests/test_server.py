from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from openhealth.server.app import create_app
from tests.conftest import LOCAL_DATASET
from tests.test_fhir import BUNDLE

PACK = "/api/packs/openhealth/patient-inbox-router"


@pytest.fixture
def client(db_target: str) -> TestClient:
    return TestClient(create_app(db_path=db_target, datasets_dir=LOCAL_DATASET.parent))


def test_packs_and_card(client: TestClient) -> None:
    packs = client.get("/api/packs").json()
    assert [p["id"] for p in packs] == [
        "openhealth/clinical-trial-prescreener",
        "openhealth/patient-inbox-router",
    ]
    detail = client.get(PACK).json()
    assert detail["active_providers"]["router"]["variant"] == "naive_bayes"
    assert "local-site-example" in detail["datasets"]
    assert detail["benchmark"]["dataset"]["name"] == "test"
    assert client.get("/api/packs/openhealth/missing").status_code == 404


def test_decide_review_and_audit_flow(client: TestClient) -> None:
    decision = client.post(
        f"{PACK}/decide", json={"message": {"body": "Crushing chest pain"}}
    ).json()
    assert decision["outcome"] == "HUMAN_REVIEW" and decision["priority"] == "stat"

    queue = client.get("/api/review").json()
    assert [item["id"] for item in queue] == [decision["id"]]

    resolved = client.post(
        f"/api/review/{decision['id']}", json={"final_label": "nurse", "reviewer": "rn-1"}
    )
    assert resolved.status_code == 200 and resolved.json()["human_override"] is False
    again = client.post(
        f"/api/review/{decision['id']}", json={"final_label": "nurse", "reviewer": "rn-1"}
    )
    assert again.status_code == 409

    record = client.get(f"/api/audit/{decision['id']}").json()
    assert [e["event"] for e in record["events"]] == ["decision.created", "review.resolved"]
    assert client.get("/api/audit/verify").json()["ok"] is True
    assert client.get("/api/stats").json()["reviews_resolved"] == 1


def test_invalid_input_is_a_recorded_block_not_an_http_error(client: TestClient) -> None:
    response = client.post(f"{PACK}/decide", json={"message": {"body": ""}})
    assert response.status_code == 200 and response.json()["outcome"] == "BLOCK"


def test_fhir_endpoint_returns_task(client: TestClient) -> None:
    body = client.post(f"{PACK}/fhir/decide", json=BUNDLE).json()
    assert body["task"]["resourceType"] == "Task"
    missing = client.post(f"{PACK}/fhir/decide", json={"resourceType": "Bundle", "entry": []})
    assert missing.status_code == 422


def test_cds_hooks_endpoints(client: TestClient) -> None:
    services = {s["id"]: s for s in client.get("/cds-services").json()["services"]}
    assert set(services) == {
        "openhealth.patient-inbox-router",
        "openhealth.clinical-trial-prescreener",
    }
    inbox = services["openhealth.patient-inbox-router"]
    response = client.post(
        f"/cds-services/{inbox['id']}",
        json={
            "hook": inbox["hook"],
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {},
            "prefetch": {
                e["resource"]["resourceType"].lower(): e["resource"] for e in BUNDLE["entry"]
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["cards"][0]["indicator"] == "critical"


def test_local_validation_via_api(client: TestClient) -> None:
    # Each pack is offered only the local datasets that label its questions.
    assert "trial-local-site-example" not in client.get(PACK).json()["datasets"]
    report = client.post(f"{PACK}/evals", json={"dataset": "local-site-example"}).json()
    assert report["comparison"]["status"] == "degraded"
    assert client.get("/api/evals").json()[0]["id"] == report["id"]
    # Evaluation traffic does not enter the operational audit log.
    assert client.get("/api/stats").json()["decisions"] == 0
    assert client.post(f"{PACK}/evals", json={"dataset": "../../etc/passwd"}).status_code == 404
    wrong_pack = client.post(f"{PACK}/evals", json={"dataset": "trial-local-site-example"})
    assert wrong_pack.status_code == 404


def test_dashboard_is_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "OpenHealth Hub" in page.text
    # Asset URLs carry a content hash so a deploy never serves stale JS/CSS.
    assert re.search(r'/static/app\.js\?v=[0-9a-f]{12}"', page.text)
    assert re.search(r'/static/style\.css\?v=[0-9a-f]{12}"', page.text)
    assert client.get("/static/app.js").status_code == 200


def test_health_reports_deployment_state(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["storage"] in {"sqlite", "postgres"}
    assert health["providers"]["openhealth/patient-inbox-router"] == {
        "safety_screen": "rules",
        "router": "naive_bayes",
    }
    assert set(health["jev"]) >= {"credentials", "endpoint_configured", "calls_today"}
    assert set(health["llm"]) >= {"endpoint", "credentials", "default_model", "calls_today"}


def test_remote_evaluations_are_capped_and_budgeted(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openhealth.errors import ProviderError
    from openhealth.providers.jev import JevProvider

    def unreachable(*_: object) -> None:
        raise ProviderError("timeout")

    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.delenv("JEV_CONNECTOR", raising=False)
    monkeypatch.setattr(JevProvider, "_send", unreachable)
    app = create_app(
        db_path=db_target,
        providers={"router": "jev"},
        datasets_dir=LOCAL_DATASET.parent,
        budget_limits={"jev": 5},
    )
    report = TestClient(app).post(f"{PACK}/evals", json={"dataset": "test"}).json()
    assert report["dataset"]["n"] == 200 and report["dataset"]["limit"] == 200
    assert report["providers"]["router"]["model"] == "jev-test-model"
    # Five calls fit the budget (and time out); the rest are refused by the budget.
    assert report["errors"] == {"timeout": 5, "budget_exhausted": 195}
    assert report["decisions"]["automation"]["n"] == 0  # nothing automated without Jev


def test_calls_that_cannot_authenticate_use_no_budget(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("JEV_CONNECTOR", raising=False)
    client = TestClient(
        create_app(db_path=db_target, providers={"router": "jev"}, budget_limits={"jev": 5})
    )
    report = client.post(f"{PACK}/evals", json={"dataset": "red_flags"}).json()
    assert report["errors"] == {"credential_unavailable": 38}
    assert client.get("/api/health").json()["jev"]["calls_today"] == 0
