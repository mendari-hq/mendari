# SPDX-License-Identifier: Apache-2.0
"""A live dashboard for browser tests: the real app on a free port, seeded through its own API.

It serves the test fixtures' agents (`mendari-test/triage`, `clinical-review` and
`chart-summary`), each a copy with what a maintained agent publishes beside it: a benchmark (its
ledger too) and a leaderboard, measured here on the agent's own synthetic sets. The server runs
the agents' default providers only (keyword rules and canned answers), as do those measurements,
so the browser suite never calls a model or a service and costs nothing.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
import uvicorn
import yaml
from fastapi.dependencies import models as fastapi_models
from playwright.sync_api import Browser, Page

from mendari import Decision, load_pack
from mendari.core.registry import SearchPath
from mendari.evals import leaderboard
from mendari.evals.harness import evaluation, resolve_dataset
from mendari.server.app import create_app
from tests.conftest import CHART_DIR, CLINICAL_DIR, TRIAGE_DIR

TRIAGE = "mendari-test/triage"
CLINICAL = "mendari-test/clinical-review"
CHART = "mendari-test/chart-summary"
BENCHMARK = "evals/benchmark.json"  # where a served copy's benchmark is (`evaluation.benchmark`)
# The configurations each served agent's leaderboard compares, by dataset: every pairing of its
# variants that answers offline (the `llm` ones need a model, so none). Fewer on the triage
# fixture's critical set, so a slot the same in every row folds into its table's caption.
COMPARED: dict[Path, dict[str, list[dict[str, str]]]] = {
    TRIAGE_DIR: {
        "test": [
            {"router": router, "screen": screen}
            for router in ("rules", "agent")
            for screen in ("rules", "agent", "checked", "repeated")
        ],
        "critical": [
            {"router": "rules", "screen": screen}
            for screen in ("rules", "agent", "checked", "repeated")
        ],
    },
    CLINICAL_DIR: {"test": [{"reader": "rules"}, {"reader": "agent"}]},
    CHART_DIR: {"test": [{"reader": "agent"}, {"reader": "rules"}]},
}
# Messages the seed decides on the triage fixture: a possible emergency (a nurse, at priority
# stat, waits for review), a refill (the pharmacy, reviewed while the screen is the keyword rules)
# and one the rules cannot place (no suggestion).
SEEDED = [
    "Having tight chest pressure and I'm sweating a lot right now.",
    "Hi, I need a refill on my lisinopril. I have about three pills left.",
    "hello?",
]


def _forget_endpoints() -> None:
    """Lets stopped servers' apps go. FastAPI caches what each endpoint is in module-level LRU
    caches (4,096 entries) keyed by the endpoint, and every Mendari endpoint is a closure over its
    app: about 47 apps (their stores, packs and models) stayed alive at once, and the suite's
    memory and full garbage collections grew with every test that serves one. Production builds
    one app, so only tests need this."""
    for cached in vars(fastapi_models).values():
        if callable(getattr(cached, "cache_clear", None)):
            cached.cache_clear()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def published(source: Path, target: Path, compared: Mapping[str, list[dict[str, str]]]) -> None:
    """A copy of the fixture's agent at `source`, at `target`, publishing what a maintained agent
    does: its benchmark (its benchmark set on its default providers, the run's ledger beside it)
    and a leaderboard of the `compared` configurations on each named set. Both are attestations,
    outside the agent's digest; naming the benchmark in the manifest (`evaluation.benchmark`) is
    the copy's one change, so each report is measured on the copy as it is served."""
    shutil.copytree(source, target)
    manifest = target / "agent.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["evaluation"]["benchmark"] = BENCHMARK
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    agent = load_pack(target)
    benchmark_dataset = agent.manifest.evaluation.benchmark_dataset
    assert benchmark_dataset is not None, agent.id
    run = evaluation(Decision(agent), resolve_dataset(agent, benchmark_dataset))
    ledgers = target / "evals" / "ledgers"
    ledgers.mkdir(parents=True)
    (ledgers / run.ledger.file_name).write_bytes(run.ledger.dumps())
    (target / BENCHMARK).write_text(json.dumps(run.report, indent=2) + "\n", encoding="utf-8")
    reports = [
        evaluation(Decision(agent, providers=configuration), resolve_dataset(agent, name)).report
        for name, configurations in compared.items()
        for configuration in configurations
    ]
    board = leaderboard.build(agent, reports)
    (target / leaderboard.PATH).write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")


def _seed(base: str) -> None:
    """At least one decision per agent (a behaviour-test example), plus triage decisions that land
    in the review queue and a thumbs-down, so every view has something to render."""
    with httpx.Client(base_url=base, timeout=30) as client:
        for agent in client.get("/api/agents").json():
            examples = client.get(f"/api/agents/{agent['id']}/examples").json()
            for example in examples[:2]:
                client.post(f"/api/agents/{agent['id']}/decide", json=example["input"])
        decided = [
            client.post(
                f"/api/agents/{TRIAGE}/decide",
                json={"message": {"body": body, "channel": "portal"}},
            ).json()
            for body in SEEDED
        ]
        client.post(
            f"/api/audit/{decided[1]['id']}/feedback",
            json={"rating": "down", "reporter": "browser-test", "note": "Should be front desk"},
        )


@contextmanager
def serving(app: Any, port: int | None = None) -> Iterator[int]:
    """`app` on a free port of 127.0.0.1 (or on `port`, chosen before the app is built, when
    its settings name it) until the block ends; yields the port."""
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the dashboard server did not start")
        time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        _forget_endpoints()


@pytest.fixture(scope="session")
def served_agents(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The folder of the agents the live server serves: a published copy of each test fixture's
    agent (`published`), by its folder's name."""
    root = Path(tmp_path_factory.mktemp("agents"))
    for source, compared in COMPARED.items():
        published(source, root / source.name, compared)
    return root


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory, served_agents: Path) -> Iterator[str]:
    """The live server: the served agents as maintained ones, and a site's own labelled cases for
    the triage fixture (`local-site-example`, its calibration set) to run an evaluation on."""
    db = Path(tmp_path_factory.mktemp("dashboard")) / "mendari.db"
    datasets = Path(tmp_path_factory.mktemp("datasets"))
    shutil.copyfile(
        TRIAGE_DIR / "data" / "calibration.jsonl", datasets / "local-site-example.jsonl"
    )
    app = create_app(
        db_path=db,
        datasets_dir=datasets,
        packs_paths=[SearchPath(served_agents, "maintained")],
    )
    with serving(app) as port:
        base = f"http://127.0.0.1:{port}"
        _seed(base)
        yield base


@pytest.fixture(scope="session")
def base_url(live_server: str) -> str:
    """pytest-playwright resolves relative `page.goto` URLs against this."""
    return live_server


@pytest.fixture(scope="session")
def pack_ids(live_server: str) -> list[str]:
    return [pack["id"] for pack in httpx.get(f"{live_server}/api/agents").json()]


@pytest.fixture
def axe_page(browser: Browser, base_url: str, request: pytest.FixtureRequest) -> Page:
    """A page in a context that lets a test inject axe (tests/browser/test_accessibility.py; the
    CSP stays on for everything else the page loads), in the colour scheme asked for."""
    scheme: Literal["light", "dark"] = (
        "dark" if getattr(request, "param", "light") == "dark" else "light"
    )
    context = browser.new_context(base_url=base_url, bypass_csp=True, color_scheme=scheme)
    request.addfinalizer(context.close)
    return context.new_page()
