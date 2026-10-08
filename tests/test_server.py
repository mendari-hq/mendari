# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from mendari.server.app import DASHBOARD_DIR, create_app
from tests.conftest import LOCAL_DATASET
from tests.test_fhir import BUNDLE

PACK = "/api/packs/mendari/patient-inbox-router"


@pytest.fixture
def client(db_target: str) -> TestClient:
    return TestClient(create_app(db_path=db_target, datasets_dir=LOCAL_DATASET.parent))


def test_packs_and_card(client: TestClient) -> None:
    packs = client.get("/api/packs").json()
    assert [p["id"] for p in packs] == [
        "mendari/ai-guard",
        "mendari/clinical-trial-prescreener",
        "mendari/patient-inbox-router",
        "mendari/prior-auth-preflight",
        "mendari/referral-router",
    ]
    detail = client.get(PACK).json()
    assert detail["active_providers"]["router"]["variant"] == "naive_bayes"
    assert detail["without_model"] == ["safety_screen", "router"]  # no model set up here
    assert "local-site-example" in detail["datasets"]
    assert detail["benchmark"]["dataset"]["name"] == "test"
    assert client.get("/api/packs/mendari/missing").status_code == 404


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


def test_datasets_are_listed_with_their_size(client: TestClient) -> None:
    datasets = {d["name"]: d["n"] for d in client.get(f"{PACK}/datasets").json()}
    detail = client.get(PACK).json()
    assert list(datasets) == detail["datasets"]  # the same names, in the same order
    assert datasets["local-site-example"] == len(LOCAL_DATASET.read_text().splitlines())
    assert all(n > 0 for n in datasets.values())
    assert client.get("/api/packs/mendari/missing/datasets").status_code == 404


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
        "mendari.patient-inbox-router",
        "mendari.clinical-trial-prescreener",
        "mendari.referral-router",
        "mendari.prior-auth-preflight",
    }
    inbox = services["mendari.patient-inbox-router"]
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
    assert client.get("/api/evals").json()[0]["run"]["id"] == report["run"]["id"]
    # Evaluation traffic does not enter the operational audit log.
    assert client.get("/api/stats").json()["decisions"] == 0
    assert client.post(f"{PACK}/evals", json={"dataset": "../../etc/passwd"}).status_code == 404
    wrong_pack = client.post(f"{PACK}/evals", json={"dataset": "trial-local-site-example"})
    assert wrong_pack.status_code == 404


def test_reports_and_leaderboards_are_served_with_their_figures(
    client: TestClient, db_target: str
) -> None:
    """Figures (mendari.evals.figures) are worked out on every read and never stored, so a
    served report still hashes to its result_sha256."""
    from mendari import AuditStore
    from mendari.evals import figures
    from mendari.evals.result import result_of, result_sha256

    detail = client.get(PACK).json()
    assert detail["benchmark"]["figures"]["accuracy"] == figures.of(596, 600)
    board = detail["leaderboard"]
    assert board["ranking"]["method"] == "mendari.rank/1" and board["groups"]
    assert all("figures" in row and "standing_text" in row for row in board["rows"])
    report = client.post(f"{PACK}/evals", json={"dataset": "local-site-example"}).json()
    assert report["figures"]["accuracy"] == figures.counted(
        report["questions"]["destination"]["accuracy"], report["questions"]["destination"]["n"]
    )
    assert report["figures"]["comparison"]["findings"]  # the local site degrades
    assert result_sha256(result_of(report)) == report["result_sha256"]
    (listed,) = client.get("/api/evals").json()
    assert listed["figures"] == report["figures"]
    (stored,) = AuditStore(db_target).reports()
    assert "figures" not in stored
    periods = client.get("/api/stats").json()["periods"]
    assert set(periods) == {"7", "30", "all"} and periods["7"]["now"]["decisions"] == 0


def test_a_stored_report_figures_cannot_be_read_from_is_served_with_none(
    client: TestClient, db_target: str
) -> None:
    from mendari import AuditStore

    odd = json.loads((LOCAL_DATASET.parents[1] / "tests/fixtures/eval-v1-inbox.json").read_text())
    odd["decisions"]["critical"][0]["detected"] = 99  # more than its 23 positives
    AuditStore(db_target).save_report(odd)
    (listed,) = client.get("/api/evals").json()
    assert listed["figures"] is None  # n/a on the page, never a failed history tab


def test_examples_are_the_packs_behaviour_tests(client: TestClient) -> None:
    trial = "/api/packs/mendari/clinical-trial-prescreener"
    examples = client.get(f"{trial}/examples").json()
    assert len(examples) == 22
    pregnancy = next(e for e in examples if e["name"].startswith("a pregnancy in the notes"))
    assert pregnancy["expect"]["recommendation"] == "not_eligible"
    # YAML anchors are resolved: every example is a complete input that decides as tested.
    decision = client.post(f"{trial}/decide", json=pregnancy["input"]).json()
    assert decision["recommendation"] == "not_eligible"
    assert "not_eligible: note_pregnancy is yes" in decision["reasons"]
    assert client.get(f"{PACK}/examples").json()[0]["input"]["message"]["body"]
    assert client.get("/api/packs/mendari/missing/examples").status_code == 404


def test_dashboard_is_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "Mendari" in page.text
    # Asset URLs carry a content hash so a deploy never serves stale JS/CSS: the import map
    # stamps every module main.js reaches (every module but the /docs landing's and the
    # invitation page's), and the entry script and stylesheet are stamped in place.
    assert re.search(r'<script type="module" src="/static/main\.js\?v=[0-9a-f]{12}">', page.text)
    assert re.search(r'/static/style\.css\?v=[0-9a-f]{12}"', page.text)
    found = re.search(r'<script type="importmap">(.*?)</script>', page.text)
    assert found
    stamped = json.loads(found[1])["imports"]
    modules = sorted(
        p.name for p in DASHBOARD_DIR.glob("*.js") if p.name not in ("docs.js", "invite.js")
    )
    assert sorted(stamped) == [f"/static/{name}" for name in modules]
    for url in stamped.values():
        assert re.fullmatch(r"/static/[a-z]+\.js\?v=[0-9a-f]{12}", url)
        assert f'<link rel="modulepreload" href="{url}">' in page.text
        assert "javascript" in client.get(url).headers["content-type"]


def test_dashboard_modules_import_only_files_that_exist() -> None:
    for module in DASHBOARD_DIR.glob("*.js"):
        source = module.read_text()
        imports = re.findall(r'^import (?:\{[^}]*\} from )?"\./([^"]+)";$', source, re.M)
        assert all((DASHBOARD_DIR / name).is_file() for name in imports), module.name


def test_the_favicon_is_served(client: TestClient) -> None:
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/x-icon"
    assert icon.content[:4] == b"\x00\x00\x01\x00"  # an ICO header
    assert client.get("/static/favicon.svg").headers["content-type"] == "image/svg+xml"
    page = client.get("/").text
    assert 'rel="icon" href="/favicon.ico"' in page and 'href="/static/favicon.svg"' in page


def test_health_reports_deployment_state(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["storage"] in {"sqlite", "postgres"}
    assert health["timezone"] == "America/New_York"
    assert health["providers"]["mendari/patient-inbox-router"] == {
        "safety_screen": "rules",
        "router": "naive_bayes",
    }
    # No model set up: every pack runs its offline readers where it offers a model.
    assert health["without_model"] == {
        "mendari/ai-guard": ["screen"],
        "mendari/clinical-trial-prescreener": ["notes"],
        "mendari/patient-inbox-router": ["safety_screen", "router"],
        "mendari/prior-auth-preflight": ["notes"],
        "mendari/referral-router": ["specialty_reader", "triage_screen"],
    }
    assert set(health["jev"]) >= {"credentials", "endpoint_configured", "calls_today"}
    assert set(health["llm"]) >= {"endpoint", "credentials", "default_model", "calls_today"}


def test_remote_evaluations_are_capped_and_budgeted(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mendari.errors import ProviderError
    from mendari.providers.jev import JevProvider

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


def test_the_api_reference_describes_every_operation(client: TestClient) -> None:
    """/docs is the product's face to developers: every operation is grouped, named and
    explained, and "Try it out" starts from a synthetic input that really decides."""
    schema = client.get("/openapi.json").json()
    tags = {tag["name"] for tag in schema["tags"]}
    operations = [
        (method.upper(), path, operation)
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    ]
    for method, path, operation in operations:
        assert len(operation.get("tags", [])) == 1 and operation["tags"][0] in tags, (method, path)
        assert operation.get("summary") and operation.get("description"), (method, path)
    assert "securitySchemes" not in schema.get("components", {})  # sign-in is off here
    assert "Sign-in is off here" in schema["info"]["description"]
    decide = schema["paths"]["/api/packs/{namespace}/{name}/decide"]["post"]
    examples = decide["requestBody"]["content"]["application/json"]["examples"]
    assert next(iter(examples)) == "patient-inbox-router"  # the default pack comes first
    for example in examples.values():
        pack_id = example["summary"].split(":")[0]
        decided = client.post(f"/api/packs/{pack_id}/decide", json=example["value"]).json()
        assert decided["outcome"] != "BLOCK", (pack_id, decided["reasons"])

    def offered(path: str) -> list[str]:
        operation = schema["paths"][path]["post"]
        name = next(p for p in operation["parameters"] if p["name"] == "name")
        return list(name["examples"])

    assert offered("/api/packs/{namespace}/{name}/guard") == ["ai-guard"]  # the guard pack
    fhir_packs = offered("/api/packs/{namespace}/{name}/fhir/decide")
    assert fhir_packs[0] == "patient-inbox-router" and "ai-guard" not in fhir_packs
    smart = schema["paths"]["/api/smart/decide"]["post"]
    assert "Needs an EHR launch" in smart["description"]  # not a session or a key
    fhir = schema["paths"]["/api/packs/{namespace}/{name}/fhir/decide"]["post"]
    bundle = fhir["requestBody"]["content"]["application/json"]["examples"]["message"]["value"]
    decided = client.post(f"{PACK}/fhir/decide", json=bundle).json()
    assert decided["task"]["resourceType"] == "Task"
    assert decided["decision"]["input_issues"] == []  # the example conforms to US Core
