from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openhealth.server.app import create_app
from tests.conftest import LOCAL_DATASET
from tests.test_fhir import BUNDLE

PACK = "/api/packs/openhealth/patient-inbox-router"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "server.db", datasets_dir=LOCAL_DATASET.parent))


def test_packs_and_card(client: TestClient) -> None:
    packs = client.get("/api/packs").json()
    assert [p["id"] for p in packs] == ["openhealth/patient-inbox-router"]
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
    services = client.get("/cds-services").json()["services"]
    response = client.post(
        f"/cds-services/{services[0]['id']}",
        json={
            "hook": services[0]["hook"],
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
    report = client.post(f"{PACK}/evals", json={"dataset": "local-site-example"}).json()
    assert report["comparison"]["status"] == "degraded"
    assert client.get("/api/evals").json()[0]["id"] == report["id"]
    # Evaluation traffic does not enter the operational audit log.
    assert client.get("/api/stats").json()["decisions"] == 0
    assert client.post(f"{PACK}/evals", json={"dataset": "../../etc/passwd"}).status_code == 404


def test_dashboard_is_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "OpenHealth Hub" in page.text
    assert client.get("/static/app.js").status_code == 200
