# SPDX-License-Identifier: Apache-2.0
"""The committee packet (core/packet.py): everything an AI governance committee reads about one
agent, as one structured document and one page that prints.

Held here: every section, in order, against the packet's own schema; a fresh deployment's packet
says what is missing; no case text reaches the JSON or the page; the route is refused to a
signed-out caller, to a role without access and to another organization; the page is
self-contained and holds no script; the crosswalk is data, every row names a section and a part
that exist, and what was not confirmed is shown as "to verify".

Every test runs on the bundled demo agent (`agents/portal-message-agent`) or a copy of it;
the one of a calibration and a second signal, which the demo agent has not, on what
`mendari new` writes."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_pack
from mendari.cli import main
from mendari.core import packet
from mendari.core.pack import Pack
from mendari.server.app import create_app
from mendari.server.scopes import DECIDE
from tests.conftest import (
    DASHBOARD,
    DEMO_AGENT_DIR,
    ROOT,
    encrypt_mode,
    encrypted_store,
    member,
)
from tests.support.confidence_agents import calibration_file, live_member, scaffold, variant

AGENT_ID = "mendari/portal-message-agent"
AGENT = f"/api/agents/{AGENT_ID}"
PACKET = f"{AGENT}/committee-packet"
SECTIONS = [
    "identity",
    "intended_use",
    "risk",
    "oversight",
    "publisher_evidence",
    "site_evidence",
    "limits",
    "monitoring",
    "change_control",
    "data_handling",
    "approvals",
]
# Text no agent, manifest or framework holds: if it is in a packet, a case leaked into it.
SENTINEL = "zebraquartz7731"
REFILL = {"message": {"body": "Please send a refill of my metformin to the pharmacy."}}
URGENT = {"message": {"body": "I have chest pain and trouble breathing since last night."}}
FRONT_DESK = {"message": {"body": "Where do I park when I come in?"}}


def demo(origin: str = "maintained") -> Pack:
    return load_pack(DEMO_AGENT_DIR, origin=origin)  # type: ignore[arg-type]


def local_deployment(**changes: Any) -> packet.Deployment:
    """A deployment as a laptop runs one: no sign-in, synthetic data, nothing scheduled."""
    said: dict[str, Any] = {
        "phi_mode": "off",
        "input_hash": "sha256",
        "retention_days": 30,
        "scheduled": False,
        "sign_in": False,
        "agent_endpoint": False,
        "providers": {"reader": "agent"},
        "models": {"reader": "mock-portal-agent"},
    }
    return packet.Deployment(**{**said, **changes})


def section(built: dict[str, Any], key: str) -> dict[str, Any]:
    (found,) = [s["content"] for s in built["sections"] if s["key"] == key]
    return dict(found)


@pytest.fixture
def open_client(db_target: str) -> Iterator[TestClient]:
    """A release as it starts: open mode, serving the bundled agent."""
    with TestClient(create_app(db_path=db_target, packs_paths=[ROOT / "agents"])) as client:
        yield client


def decide(client: TestClient, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    answer = client.post(f"{AGENT}/decide", json=body, **kwargs)
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


# -- the document ----------------------------------------------------------------------------------


def test_the_demo_agents_packet_has_every_section_in_order_and_fits_its_schema(
    open_client: TestClient,
) -> None:
    for body in (REFILL, REFILL, URGENT, FRONT_DESK):
        decide(open_client, body)
    answer = open_client.get(PACKET)
    assert answer.status_code == 200, answer.text
    built = answer.json()
    assert packet.problems(built) == []
    assert built["format"] == "mendari.committee-packet/1"
    assert [s["key"] for s in built["sections"]] == SECTIONS == [key for key, _ in packet.SECTIONS]
    assert [s["number"] for s in built["sections"]] == list(range(1, 12))
    assert built["agent"]["id"] == AGENT_ID and built["agent"]["digest"] == demo().digest
    # What it is, from the manifest; what happened here, from this deployment's own decisions.
    assert section(built, "identity")["publisher"]["name"] == "Mendari"
    assert section(built, "risk")["level"] == 0
    site = section(built, "site_evidence")
    assert site["empty"] is None and site["decisions"]["all_time"] == 4
    outcomes = site["decisions"]["outcomes"]
    assert sum(share["k"] for share in outcomes.values()) == 4
    assert all(share["n"] == 4 for share in outcomes.values())  # each with what it is out of
    assert outcomes["AUTO"]["k"] == 2 and outcomes["AUTO"]["low"] < 0.5 < outcomes["AUTO"]["high"]
    # The appendices: what nobody stated, and the crosswalk.
    assert built["not_stated"] and built["crosswalk"]["rows"]
    # A shape that is not the packet's is told apart, and so is a changed figure.
    assert packet.problems({**built, "sections": built["sections"][::-1]})
    changed = json.loads(json.dumps(built))
    changed["sections"][5]["content"]["decisions"]["all_time"] = 400
    assert packet.problems(changed) == ["sha256: it is not the hash of this packet"]


def test_a_fresh_deployments_packet_says_what_is_missing(open_client: TestClient) -> None:
    """No decision, no evaluation, no approval: every section is there and says why it is empty,
    and the gates have no data. Nothing fails, and nothing reads as good news."""
    answer = open_client.get(PACKET)
    assert answer.status_code == 200, answer.text
    built = answer.json()
    assert packet.problems(built) == []
    site = section(built, "site_evidence")
    assert site["empty"].startswith("This deployment has made no decision with this agent")
    assert site["decisions"]["all_time"] == 0
    nothing = {"k": 0, "n": 0, "low": None, "high": None}  # 0 of 0: no share, and no interval
    assert all(share == nothing for share in site["decisions"]["outcomes"].values())
    states = {gate["key"]: gate["status"] for gate in site["gates"]}
    assert states == {
        "volume": "no_data",
        "safety": "no_data",
        "agreement": "no_data",
        "governance": "pass",
        "drift": "no_data",
    }
    assert site["gates_line"] == "1 of 5 gates pass; 4 without enough data"
    publisher = section(built, "publisher_evidence")
    assert publisher["evaluation"] is None and "no benchmark" in publisher["reason"]
    assert section(built, "approvals")["governance"] == []
    assert section(built, "monitoring")["alerts"] == []
    missing = {item["key"] for item in built["not_stated"]}
    assert {"local_evidence", "publisher_evaluation", "approval_record"} <= missing
    page = open_client.get(f"{PACKET}?format=html").text
    assert "This deployment has made no decision with this agent" in page
    assert page.count("Not enough data") == 4 and ">Pass<" in page
    assert "None recorded for this agent in this organization." in page
    assert "All gates" not in page and "ALL GATES" not in page


def test_what_was_stated_leaves_the_not_stated_appendix(store: AuditStore) -> None:
    """An item is listed only while nobody has stated it; an item no field can hold yet is
    always listed, with who could state it and which framework asks."""
    pack = demo()
    fresh = packet.build(store, pack, deployment=local_deployment())
    before = {item["key"]: item for item in fresh["not_stated"]}
    # The manifest states its failure modes and out-of-scope uses: neither is listed.
    assert "local_evidence" in before
    assert not {"known_failure_modes", "out_of_scope"} & set(before)
    contact = before["developer_contact"]
    assert contact["who"] == "publisher" and contact["note"].startswith("No field or record")
    assert {asked["framework"] for asked in contact["asked_by"]} == {
        "CHAI model card",
        "HTI-1 source attributes",
    }
    assert before["staff_training"]["who"] == "site"
    Decision(pack, store=store).decide(REFILL)
    decided = packet.build(store, pack, deployment=local_deployment())
    after = {item["key"] for item in decided["not_stated"]}
    assert before.keys() - after == {"local_evidence"}


def test_an_evaluation_run_here_is_shown_as_counts_with_its_configuration(
    open_client: TestClient,
) -> None:
    """An evaluation saved for the organization: its test set, the configuration it ran on, each
    share with what it is out of, and the critical cases. Never a case of the test set."""
    ran = open_client.post(f"{AGENT}/evals", json={"dataset": "examples"})
    assert ran.status_code == 200, ran.text
    answer = open_client.get(PACKET)
    built = answer.json()
    assert packet.problems(built) == []
    site = section(built, "site_evidence")
    assert site["empty"] is None and site["evaluations_saved"] == 1
    (found,) = site["evaluations"]
    assert found["dataset"]["name"] == "examples" and found["dataset"]["n"] == 12
    assert found["measured_on"] == {
        "version": demo().version,
        "digest": demo().digest,
        "this_version": True,
    }
    assert found["configuration"]["reader"]["variant"] == "agent"
    assert found["accuracy"]["n"] == 12 and 0 <= found["accuracy"]["k"] <= 12
    assert found["accuracy"]["low"] <= found["accuracy"]["k"] / 12 <= found["accuracy"]["high"]
    (critical,) = found["critical"]
    assert (critical["question"], critical["label"]) == ("urgent_symptoms", "yes")
    assert critical["detected"]["n"] == 3 and critical["insufficient_evidence"] is True
    assert "local_evidence" not in {item["key"] for item in built["not_stated"]}
    page = open_client.get(f"{PACKET}?format=html").text
    assert "too few cases to judge" in page and "12 cases" in page
    cases = [
        json.loads(line)["input"]["message"]["body"]
        for line in (DEMO_AGENT_DIR / "data" / "examples.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(cases) == 12
    for text in (answer.text, page):
        assert not [case for case in cases if case in text]


def test_the_packet_is_the_same_for_the_same_evidence(store: AuditStore) -> None:
    pack = demo()
    Decision(pack, store=store).decide(REFILL)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    first = packet.build(store, pack, deployment=local_deployment(), now=now)
    second = packet.build(store, pack, deployment=local_deployment(), now=now)
    assert first["sha256"] == second["sha256"] and first == second
    assert first["generated_at"] == "2026-11-23T12:00:00+00:00"
    with pytest.raises(Exception, match="14 to 180 days"):
        packet.build(store, pack, deployment=local_deployment(), days=7)


# -- what it never holds ---------------------------------------------------------------------------


def test_no_case_text_and_no_reviewers_name_reaches_the_packet(open_client: TestClient) -> None:
    """Decisions, a review and a rating are seeded with recognizable text, in open mode with PHI
    mode off, where the audit record keeps inputs as written. None of it is in the JSON or the
    page: the packet is counts, ids, label codes and dates."""
    stated = f"I ran out of my pills {SENTINEL}. Also chest pain {SENTINEL} since last night."
    held = decide(open_client, {"message": {"subject": f"refill {SENTINEL}", "body": stated}})
    assert held["outcome"] == "HUMAN_REVIEW" and held["recommendation"]
    kept = open_client.get(f"/api/audit/{held['id']}").json()
    assert SENTINEL in json.dumps(kept)  # the deployment does hold it
    reviewed = open_client.post(
        f"/api/review/{held['id']}",
        json={"final_label": "other", "reviewer": f"rn-{SENTINEL}", "note": f"called {SENTINEL}"},
    )
    assert reviewed.status_code == 200 and reviewed.json()["human_override"] is True
    automated = decide(open_client, {"message": {"body": f"Please refill my inhaler {SENTINEL}"}})
    assert automated["outcome"] == "AUTO"
    rated = open_client.post(
        f"/api/audit/{automated['id']}/feedback",
        json={"rating": "down", "reporter": f"rep-{SENTINEL}", "note": f"wrong {SENTINEL}"},
    )
    assert rated.status_code in (200, 201), rated.text
    built = open_client.get(PACKET)
    page = open_client.get(f"{PACKET}?format=html")
    assert built.status_code == page.status_code == 200
    for text in (built.text, page.text):
        assert SENTINEL not in text
        assert "ran out of my pills" not in text and "since last night" not in text
    site = section(built.json(), "site_evidence")
    # What it does hold of them: the counts, and the override as two label codes.
    assert site["review"]["overrides"]["k"] == 1 and site["review"]["overrides"]["n"] == 1
    assert site["review"]["patterns"] == [
        {"recommended": held["recommendation"], "chosen": "other", "count": 1, "of": 1}
    ]
    assert site["ratings"] == {"automated": 1, "rated": 1, "rated_wrong": 1, "with_outcome": 0}
    gates = {gate["key"]: gate["status"] for gate in site["gates"]}
    assert gates["safety"] == "fail"


def risky_copy(tmp_path: Path, **changes: Any) -> Path:
    """The demo agent with its manifest changed, in a folder of its own."""
    target = tmp_path / "agents" / "portal-message-agent"
    shutil.copytree(DEMO_AGENT_DIR, target)
    manifest = yaml.safe_load((target / "agent.yaml").read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        *path, last = dotted.split(".")
        where = manifest
        for part in path:
            where = where[part]
        where[last] = value
    (target / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return target.parent


def test_an_approval_is_shown_with_who_recorded_it(db_target: str, tmp_path: Path) -> None:
    """A risk level 2 agent: no approval, then one. The approver is the one person the packet
    names; the approval is for this exact version, and has its reference and its expiry."""
    agents = risky_copy(tmp_path, **{"risk.level": 2, "risk.category": "clinical support"})
    with TestClient(create_app(db_path=db_target, packs_paths=[agents])) as client:
        before = client.get(PACKET).json()
        assert packet.problems(before) == []
        assert section(before, "risk")["level"] == 2
        assert "a person reviews every decision" in " ".join(section(before, "risk")["enforced"])
        assert section(before, "approvals")["governance"] == []
        assert "approval_record" in {item["key"] for item in before["not_stated"]}
        approved = client.post(
            "/api/governance",
            json={
                "pack_id": AGENT_ID,
                "reference": "AI committee minutes 2026-11",
                "days": 90,
                "approver": "cmio-okafor",  # open mode: who approves says so
            },
        )
        assert approved.status_code == 201, approved.text
        after = client.get(PACKET).json()
        page = client.get(f"{PACKET}?format=html").text
    (shown,) = section(after, "approvals")["governance"]
    assert shown["approver"] == approved.json()["approver"] == "cmio-okafor"
    assert shown["reference"] == "AI committee minutes 2026-11"
    assert shown["this_version"] is True and shown["in_force"] is True
    assert shown["expires_at"] == approved.json()["expires_at"]
    assert section(after, "approvals")["in_force"] == {"governance": True, "registry": None}
    assert "approval_record" not in {item["key"] for item in after["not_stated"]}
    assert "AI committee minutes 2026-11" in page and "cmio-okafor" in page
    assert ">in force<" in page


# -- who may read it -------------------------------------------------------------------------------


@pytest.fixture
def two_organizations(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, dict[str, str]]]]:
    """A deployment with sign-in that serves two organizations (memberships mode), and people
    of each: a reviewer and a viewer of `acme`, a viewer of `other`, and a labeller of `acme`,
    who holds no permission to read an agent's evidence."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-tests")
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    app = create_app(db_path=db_target, packs_paths=[ROOT / "agents"])
    with encrypted_store(db_target) as store:
        people = {
            "reviewer": member(store, org="acme", roles=["reviewer"], sub="acme-reviewer"),
            "governance": member(store, org="acme", roles=["governance"], sub="acme-governance"),
            "viewer": member(store, org="acme", roles=["viewer"], sub="acme-viewer"),
            "labeller": member(store, org="acme", roles=["labeller"], sub="acme-labeller"),
            "other": member(store, org="other", roles=["viewer"], sub="other-viewer"),
            "nobody": member(store, org=None, sub="no-organization"),
        }
        # Machines: a viewer key reads; a key scoped to deciding reaches only that.
        for name, scopes in (("key", None), ("scoped key", DECIDE)):
            made = store.create_api_key(
                name=name, role="service", org="acme", created_by="test", scopes=scopes
            )
            people[name] = {"authorization": f"Bearer {made['key']}"}
    with TestClient(app, base_url="https://mendari.test", headers=DASHBOARD) as client:
        yield client, people


def test_the_route_is_refused_without_sign_in_and_to_a_role_without_access(
    two_organizations: tuple[TestClient, dict[str, dict[str, str]]],
) -> None:
    client, people = two_organizations
    for query in ("", "?format=html"):
        signed_out = client.get(f"{PACKET}{query}")
        assert signed_out.status_code == 401, signed_out.text
        assert "Committee packet" not in signed_out.text
        # A labeller reads hidden cases to label them, and nothing of an agent's evidence.
        assert client.get(f"{PACKET}{query}", headers=people["labeller"]).status_code == 403
        # A person of no organization reads no organization's packet.
        assert client.get(f"{PACKET}{query}", headers=people["nobody"]).status_code in (401, 403)
        # Every role that reads an agent's evidence reads its packet: people and a machine's key.
        for role in ("viewer", "reviewer", "governance", "key"):
            assert client.get(f"{PACKET}{query}", headers=people[role]).status_code == 200, role
        # A key whose scopes cover deciding only does not reach it.
        scoped = client.get(f"{PACKET}{query}", headers=people["scoped key"])
        assert scoped.status_code == 403, scoped.text


def test_another_organization_sees_none_of_this_ones_evidence(
    two_organizations: tuple[TestClient, dict[str, dict[str, str]]],
) -> None:
    client, people = two_organizations
    for body in (REFILL, URGENT, FRONT_DESK):
        decide(client, body, headers=people["reviewer"])
    held = client.post(
        "/api/holds",
        json={"pack_id": AGENT_ID, "reason": "shadow pilot: acme compares with its nurses"},
        headers=people["governance"],
    )
    assert held.status_code == 201, held.text
    own = client.get(PACKET, headers=people["viewer"]).json()
    theirs = client.get(PACKET, headers=people["other"]).json()
    assert packet.problems(own) == [] and packet.problems(theirs) == []
    assert (own["organization"], theirs["organization"]) == ("acme", "other")
    assert section(own, "site_evidence")["decisions"]["all_time"] == 3
    assert section(own, "site_evidence")["shadow_pilot"]["active"] is True
    assert [hold["reason"] for hold in section(own, "oversight")["holds"]] == [
        "shadow pilot: acme compares with its nurses"
    ]
    # The other organization: the same agent, and nothing of what the first one did with it.
    other = section(theirs, "site_evidence")
    assert other["decisions"]["all_time"] == 0 and other["empty"] is not None
    assert other["shadow_pilot"]["active"] is False
    assert section(theirs, "oversight")["holds"] == []
    assert "acme" not in json.dumps(theirs)
    assert "acme" not in client.get(f"{PACKET}?format=html", headers=people["other"]).text
    # What is the same for both is the agent, and the sections that describe it.
    for key in ("identity", "intended_use", "risk", "limits"):
        assert section(own, key) == section(theirs, key)
    # In memberships mode which version ran where is the deployment operator's to read.
    assert section(own, "change_control")["records"] is None


# -- the page --------------------------------------------------------------------------------------

_VOID = {"meta", "br"}
ALLOWED_TAGS = {
    *("html", "head", "meta", "title", "style", "body", "header", "nav", "section", "div"),
    *("h1", "h2", "h3", "p", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td"),
    *("a", "b", "br", "code", "span"),
}


class Page(HTMLParser):
    """The page's tags and attributes, and the text inside its style element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.styles: list[str] = []
        self.links: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._in_style = tag == "style"
        if tag == "a":
            self.links.append(dict(attrs).get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.styles.append(data)


def parsed(text: str) -> Page:
    page = Page()
    page.feed(text)
    return page


class _Words(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.words: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in_style = tag == "style"

    def handle_endtag(self, tag: str) -> None:
        self._in_style = False

    def handle_data(self, data: str) -> None:
        if not self._in_style:
            self.words.append(data)


def html_text(text: str) -> str:
    """A page's words as a reader sees them: each element's text on its own line, with no tag
    and no style."""
    words = _Words()
    words.feed(text)
    words.close()
    return "\n".join(words.words)


def reads(text: str) -> str:
    """A page's words as one line, as a sentence reads: an element inside a sentence (a name
    set as code) does not break it."""
    line = " ".join(html_text(text).split())
    return re.sub(r"\s+([:;,.)])", r"\1", line).replace("( ", "(")


def test_the_page_is_self_contained_and_holds_no_script(open_client: TestClient) -> None:
    decide(open_client, REFILL)
    answer = open_client.get(f"{PACKET}?format=html&days=30")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "text/html; charset=utf-8"
    assert answer.headers["content-disposition"] == (
        'attachment; filename="portal-message-agent-committee-packet.html"'
    )
    text = answer.text
    page = parsed(text)
    names = {tag for tag, _ in page.tags}
    assert names <= ALLOWED_TAGS, names - ALLOWED_TAGS  # no script, link, img, iframe, form ...
    assert "<script" not in text.lower()
    # Nothing is loaded: no attribute that fetches, no handler, no inline style, no CSS fetch.
    for tag, attrs in page.tags:
        assert not {"src", "srcset", "data", "action", "style", "background"} & set(attrs), tag
        assert not [name for name in attrs if name.startswith("on")], tag
        assert tag == "a" or "href" not in attrs, tag
    style = "".join(page.styles)
    assert "@import" not in style and "url(" not in style and "<" not in style
    assert 'http-equiv="Content-Security-Policy" content="default-src \'none\'' in text
    # The only addresses are plain links: this page's own sections, and the frameworks' sources.
    inside = [link for link in page.links if link.startswith("#")]
    outside = [link for link in page.links if not link.startswith("#")]
    assert len(inside) == 13 and len(outside) >= 8
    assert all(link.startswith("https://") for link in outside)
    sources = {
        source["url"] for f in packet.crosswalk_data()["frameworks"] for source in f["sources"]
    }
    assert set(outside) == sources
    # It prints: a page break before each section, and the agent, its version and digest and
    # the time it was generated at the foot of every page.
    assert "section { break-before: page;" in style
    foot = re.search(r"@bottom-left \{ content: (\"[^\"]*\")", style)
    assert foot is not None
    said = re.sub(r"\\([0-9A-F]+) ", lambda m: chr(int(m.group(1), 16)), foot.group(1))
    built = open_client.get(f"{PACKET}?days=30").json()
    agent = built["agent"]
    assert agent["name"] in said and f"version {agent['version']}" in said
    assert agent["digest"].removeprefix("sha256:")[:16] in said and "generated 20" in said
    assert "counter(page)" in style
    # Black on white, whatever the reader's theme.
    assert "color: #000; background: #fff" in style


def test_what_a_publisher_wrote_is_plain_text_on_the_page(
    store: AuditStore, tmp_path: Path
) -> None:
    """A manifest is a publisher's words. Markup in them is text on the page, and a name cannot
    end the style element its foot is written in."""
    hostile = '</style><script>alert("x")</script><img src=x onerror=alert(1)>'
    agents = risky_copy(
        tmp_path,
        name=f"Portal {hostile}",
        summary=f"Routes messages. {hostile}",
        **{"card.intended_use": f"Routing. {hostile}"},
    )
    pack = load_pack(agents / "portal-message-agent")
    text = packet.render_html(packet.build(store, pack, deployment=local_deployment()))
    page = parsed(text)
    assert {tag for tag, _ in page.tags} <= ALLOWED_TAGS
    assert "<script" not in text and "<img" not in text
    assert text.count("<style>") == 1 and text.count("</style>") == 1
    assert "&lt;/style&gt;&lt;script&gt;" in text  # shown, as the text it is
    assert "<" not in "".join(page.styles)


def test_an_agent_from_a_registry_shows_no_figure_it_carries_and_what_it_needs(
    store: AuditStore,
) -> None:
    """Pulled from a registry: the packet copies no evaluation the agent carries (the engine
    shows no figure it did not compute), says where the publisher's are, and that nothing is
    automated until this organization approves the exact version. The approval, once recorded,
    is shown with who recorded it."""
    pack = demo("hub")
    why = "its registry has not been checked for recalls yet"
    before = packet.build(store, pack, deployment=local_deployment(), recall=why)
    assert packet.problems(before) == []
    publisher = section(before, "publisher_evidence")
    assert (publisher["source"], publisher["evaluation"]) == ("registry", None)
    assert "this deployment shows no figure it did not compute itself" in publisher["reason"]
    missing = {item["key"]: item for item in before["not_stated"]}
    assert missing["publisher_evaluation"]["note"].startswith("Held by the registry")
    assert "approval_record" in missing
    approvals = section(before, "approvals")
    assert approvals["in_force"] == {"governance": False, "registry": False}
    needs = " ".join(approvals["required"])
    assert "An agent from a registry needs the organization's approval" in needs
    gates = {gate["key"]: gate for gate in section(before, "site_evidence")["gates"]}
    assert gates["governance"]["status"] == "warn"
    signed = section(before, "change_control")["signed"]
    assert signed["held_now"] == why and signed["recall_checked"] is True
    assert f"its registry stops it automating now: {why}" in packet.render_html(before)
    # A command asks no registry: it says so, and never that no recall stands.
    told = packet.build(store, pack, deployment=local_deployment(seen_from="command line"))
    assert "not checked from the command line" in packet.render_html(told)
    assert "none standing" not in packet.render_html(told)
    store.approve_registry_pack(
        pack_id=pack.id,
        pack_digest=pack.digest,
        registry="https://registry.example.org",
        approver="cmio-okafor",
        reference="AI committee minutes 2026-11",
    )
    after = packet.build(store, pack, deployment=local_deployment())
    (shown,) = section(after, "approvals")["registry"]
    assert shown["approver"] == "cmio-okafor"
    assert shown["in_force"] is True and shown["this_version"] is True
    assert shown["expires_at"] is None  # it lasts until revoked
    assert "approval_record" not in {item["key"] for item in after["not_stated"]}
    assert "none standing at the server's last check" in packet.render_html(after)


def test_every_share_on_the_page_says_what_it_is_out_of() -> None:
    assert packet._figure({"k": 3, "n": 12, "low": 0.0549, "high": 0.5719}) == (
        "3 of 12 (25.0%; 95% interval 5.5% to 57.2%)"
    )
    assert packet._figure({"k": 0, "n": 0, "low": None, "high": None}) == "no cases (0 of 0)"
    assert packet._figure(None) == "not known"


# -- the crosswalk ---------------------------------------------------------------------------------


def test_the_crosswalk_is_data_and_every_row_names_what_exists() -> None:
    data = packet.crosswalk_data()
    sections = {key for key, _ in packet.SECTIONS}
    parts = {part["id"]: part for f in data["frameworks"] for part in f["parts"]}
    assert [f["id"] for f in data["frameworks"]] == ["chai-amc", "hti-1", "nist-ai-rmf", "ruaih"]
    assert data["rows"] and data["items"]
    for row in data["rows"]:
        assert row["section"] in sections, row
        assert row["part"] in parts, row
        assert row["holds"].strip().endswith("."), row
    # Every section answers at least one part, and every framework is answered somewhere.
    assert {row["section"] for row in data["rows"]} == sections
    answered = {row["part"].split(".")[0] for row in data["rows"]}
    assert answered == {"chai-amc", "hti-1", "nist", "ruaih"}
    # Every entry carries its source's title, an address and the day it was checked.
    day = re.compile(r"^2\d{3}-\d{2}-\d{2}$")
    for framework in data["frameworks"]:
        sources = {source["id"]: source for source in framework["sources"]}
        for source in sources.values():
            assert source["title"].strip() and source["url"].startswith("https://"), source
            assert day.match(source["checked_on"]), source
        for part in framework["parts"]:
            assert part["source"] in sources and part["status"] in packet.STATUS_WORDS, part
        for note in framework["notes"]:
            assert note["status"] in packet.STATUS_WORDS
    # What a framework asks for and nobody may have stated: each names a part, and who.
    for item in data["items"]:
        assert item["who"] in ("publisher", "site") and set(item["asked_by"]) <= set(parts), item
    assert len({item["key"] for item in data["items"]}) == len(data["items"])
    # The frameworks' own structure, as their sources give it.
    assert (
        sum(part.startswith("nist.") for part in parts) == 19
    )  # GOVERN 6, MAP 5, MEASURE 4, MANAGE 4
    assert sum(part.startswith("hti-1.b") for part in parts) == 9  # (b)(11)(iv)(B)(1) to (9)
    assert [parts[f"ruaih.{n}"]["title"] for n in range(1, 8)] == [
        "AI Policies and Governance Structures",
        "Patient Privacy and Transparency",
        "Data Security and Data Use Protections",
        "Ongoing Quality Monitoring",
        "Voluntary, Blinded Reporting of AI Safety-Related Events",
        "Risk and Bias Assessment",
        "Education and Training",
    ]


def test_the_packet_carries_the_crosswalk_whole_and_shows_what_is_to_verify(
    store: AuditStore,
) -> None:
    built = packet.build(store, demo(), deployment=local_deployment())
    crosswalk = built["crosswalk"]
    assert len(crosswalk["rows"]) == len(packet.crosswalk_data()["rows"])
    numbers = {s["key"]: s["number"] for s in built["sections"]}
    for row in crosswalk["rows"]:
        assert row["section_number"] == numbers[row["section"]]
        assert row["source"]["url"].startswith("https://") and row["source"]["checked_on"]
        assert row["status"] in ("confirmed", "to_verify")
    unconfirmed = [
        note["text"]
        for framework in crosswalk["frameworks"]
        for note in framework["notes"]
        if note["status"] == "to_verify"
    ]
    assert unconfirmed  # what no primary source confirmed is in the data, as that
    text = packet.render_html(built)
    assert text.count("To verify") == len(unconfirmed)
    # Above the tables, the page says what the crosswalk is and is not.
    appendix = text[text.index('<section id="s-crosswalk"') :]
    said = appendix.index("It is not a certification, an attestation of compliance or legal")
    assert "reading aid prepared by the software" in appendix[:said]
    assert said < appendix.index("<table")
    # A rule that is leaving the regulation is said to be, with the rule that removes it.
    assert "removes paragraphs (b)(11)(iv) through (vi)" in appendix
    assert "91 FR 67916" in appendix


def test_a_part_that_does_not_exist_is_refused_when_the_data_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whole = packet.crosswalk_data()
    broken = {**whole, "rows": [*whole["rows"], {"section": "identity", "part": "nist.map-9"}]}
    monkeypatch.setattr(packet.yaml, "safe_load", lambda text: broken)
    packet.crosswalk_data.cache_clear()
    try:
        with pytest.raises(Exception, match=r"a row names identity / nist\.map-9"):
            packet.crosswalk_data()
    finally:
        monkeypatch.undo()
        packet.crosswalk_data.cache_clear()
    assert packet.crosswalk_data()["rows"]


# -- the command -----------------------------------------------------------------------------------


def test_the_command_writes_the_page_or_the_json_and_records_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "audit.db"
    with AuditStore(db) as store:
        Decision(demo(), store=store).decide(REFILL)
        events = len(list(store.iter_events()))
    out = tmp_path / "packet.html"
    assert main(["packet", str(DEMO_AGENT_DIR), "--db", str(db), "--out", str(out)]) == 0
    printed = capsys.readouterr()
    assert printed.out == "" and "11 sections" in printed.err
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>") and "<script" not in text
    assert "Made from the command line" in text
    agent = str(DEMO_AGENT_DIR)  # by its folder: the tests' search path holds other agents
    assert main(["packet", agent, "--db", str(db), "--json", "--days", "30"]) == 0
    built = json.loads(capsys.readouterr().out)
    assert packet.problems(built) == [] and built["window_days"] == 30
    assert section(built, "site_evidence")["decisions"]["all_time"] == 1
    assert section(built, "data_handling")["seen_from"] == "command line"
    assert section(built, "data_handling")["access"]["sign_in"] is None  # not known from here
    with AuditStore(db) as store:
        assert len(list(store.iter_events())) == events  # read-only: nothing recorded
    # A window the monitor does not cover is refused as a usage error, in one line.
    assert main(["packet", agent, "--db", str(db), "--days", "3"]) == 1
    assert "give 14 to 180" in capsys.readouterr().err


class _Noon(datetime):
    """The clock the packet reads, stopped: a packet made twice is then the same bytes."""

    @classmethod
    def now(cls, tz: Any = None) -> _Noon:
        return cls(2026, 11, 23, 12, 0, tzinfo=tz)


def test_with_a_database_the_command_writes_what_the_deployment_holds_as_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--db` is the packet it always was: `packet.build` over that database, with this
    environment's settings, 90 days unless told, byte for byte, as JSON and as a page. Nothing
    of the packet made without a deployment is in it."""
    from mendari.core.phi import phi_policy
    from mendari.evals.monitoring import recorded_providers
    from mendari.providers import provider_allow_list
    from mendari.settings import retention_days

    monkeypatch.setattr(packet, "datetime", _Noon)
    db = tmp_path / "audit.db"
    pack = load_pack(DEMO_AGENT_DIR)
    with AuditStore(db) as store:
        for body in (REFILL, URGENT, FRONT_DESK):
            Decision(pack, store=store).decide(body)
    phi = phi_policy()
    with AuditStore(db, read_only=True) as store:
        expected = packet.build(
            store,
            pack,
            deployment=packet.Deployment(
                phi_mode=phi.mode,
                input_hash=phi.hash_kind,
                retention_days=retention_days(),
                provider_allowed_origins=provider_allow_list(),
                agent_endpoint=False,
                providers=recorded_providers(store, pack),
                seen_from="command line",
            ),
            days=90,
            holds=store.holds(None),
            records=store.deployments(pack.id, limit=20),
        )
    agent = str(DEMO_AGENT_DIR)
    assert main(["packet", agent, "--db", str(db), "--json"]) == 0
    printed = capsys.readouterr()
    assert printed.out == json.dumps(expected, indent=2) + "\n"
    assert "no --db" not in printed.err
    assert main(["packet", agent, "--db", str(db)]) == 0
    assert capsys.readouterr().out == packet.render_html(expected)
    # A deployment's packet says nothing of "no deployment", in its document or on its page.
    assert set(expected) == set(packet.SCHEMA["required"])
    assert expected["window_days"] == 90
    assert not any("no_deployment" in s["content"] for s in expected["sections"])
    page = packet.render_html(expected)
    assert "without a deployment" not in page and "Deployment: none" not in page
    # The two kinds are told apart by the schema: neither passes as the other.
    assert packet.problems({**expected, "no_deployment": packet.NO_DEPLOYMENT})
    assert packet.problems({**expected, "window_days": None})


def test_without_a_database_the_command_makes_the_packet_from_the_agents_files_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publisher's packet, before any deployment exists: made in a folder with no database,
    it creates none, reads no setting of the machine (a broken one does not stop it), and says
    beside the document that it holds no deployment's data."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("MENDARI_PHI_MODE", "not-a-mode")  # the server would refuse to start
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:9/none")  # never opened
    agent = str(DEMO_AGENT_DIR)
    assert main(["packet", agent, "--out", "packet.html"]) == 0
    printed = capsys.readouterr()
    assert printed.out == "" and "11 sections" in printed.err
    assert "note: no --db: made from the agent's files alone" in printed.err
    assert sorted(path.name for path in work.iterdir()) == ["packet.html"]  # no var/, no database
    text = (work / "packet.html").read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>") and "<script" not in text
    assert "Deployment: none, made from the agent's files alone" in text
    assert main(["packet", agent, "--json"]) == 0
    printed = capsys.readouterr()
    built = json.loads(printed.out)
    assert packet.problems(built) == [] and built["no_deployment"] == packet.NO_DEPLOYMENT
    assert "note: no --db" in printed.err
    assert sorted(path.name for path in work.iterdir()) == ["packet.html"]
    # --org and --days count a deployment's decisions: without one they are refused, in a line.
    for extra in (["--org", "st-marys"], ["--days", "30"]):
        assert main(["packet", agent, *extra]) == 1
        assert "give --db, the server's database, with them" in capsys.readouterr().err


def test_a_database_that_is_there_is_not_read_unless_it_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`var/mendari.db` in the folder the command runs in is a deployment's only when --db
    names it: the packet never takes on a database's decisions by where it was run."""
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "var" / "mendari.db"
    db.parent.mkdir()
    with AuditStore(db) as store:
        Decision(demo(), store=store).decide(REFILL)
    before = db.read_bytes()
    agent = str(DEMO_AGENT_DIR)
    assert main(["packet", agent, "--json"]) == 0
    alone = json.loads(capsys.readouterr().out)
    assert alone["no_deployment"] and section(alone, "site_evidence")["decisions"] is None
    assert db.read_bytes() == before
    assert main(["packet", agent, "--db", "var/mendari.db", "--json"]) == 0
    held = json.loads(capsys.readouterr().out)
    assert "no_deployment" not in held
    assert section(held, "site_evidence")["decisions"]["all_time"] == 1


# -- without a deployment --------------------------------------------------------------------------

# What only a deployment holds, by section: null in a packet made without one.
DEPLOYMENT_ONLY = {
    "oversight": ["review_queue", "holds"],
    "site_evidence": [
        "window_days",
        "decisions",
        "shadow_pilot",
        "review",
        "ratings",
        "gates",
        "gates_line",
        "evaluations",
        "evaluations_saved",
    ],
    "monitoring": [
        "days",
        "benchmark",
        "last_7_days",
        "weeks",
        "alerts",
        "other_versions",
        "alert_destination",
        "scheduled",
    ],
    "change_control": ["records"],
    "data_handling": [
        "phi_mode",
        "phi_mode_meaning",
        "input_hash",
        "retention",
        "allowed_origins",
        "access",
        "audit",
    ],
    "approvals": ["governance", "registry", "in_force"],
}


def test_a_packet_made_without_a_deployment_says_so_wherever_one_would_be_read(
    store: AuditStore,
) -> None:
    """From the agent's files alone: the publisher's sections are those of any packet, and
    every section that reads a deployment says in words that none was read. What it did not
    read is null, never a zero: a zero would say that nothing happened."""
    pack = demo()
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    alone = packet.build_alone(pack, now=now)
    assert packet.problems(alone) == []
    assert alone == packet.build_alone(pack, now=now)  # the same files, the same packet
    assert alone["no_deployment"] == packet.NO_DEPLOYMENT and alone["about"] == packet.ABOUT_ALONE
    assert alone["organization"] is None and alone["window_days"] is None
    assert [s["key"] for s in alone["sections"]] == SECTIONS
    assert set(DEPLOYMENT_ONLY) == set(packet.WITHOUT_DEPLOYMENT)
    for key in SECTIONS:
        content = section(alone, key)
        if key not in DEPLOYMENT_ONLY:
            assert "no_deployment" not in content
            continue
        assert content["no_deployment"] == packet.WITHOUT_DEPLOYMENT[key]
        assert "without a deployment" in content["no_deployment"]
        assert {name: content[name] for name in DEPLOYMENT_ONLY[key]} == dict.fromkeys(
            DEPLOYMENT_ONLY[key]
        )
    assert section(alone, "site_evidence")["empty"] == packet.WITHOUT_DEPLOYMENT["site_evidence"]
    # What the agent's files say is what a deployment's packet says of the same agent.
    Decision(pack, store=store).decide(REFILL)
    served = packet.build(store, pack, deployment=local_deployment(), now=now)
    for key in ("identity", "intended_use", "publisher_evidence"):
        assert section(alone, key) == section(served, key)
    for name in ("outcomes", "primary", "thresholds", "never_automated", "rules", "fallback"):
        assert section(alone, "oversight")[name] == section(served, "oversight")[name]
    assert section(alone, "monitoring")["checks"] == section(served, "monitoring")["checks"]
    assert section(alone, "risk")["level"] == section(served, "risk")["level"]
    assert section(alone, "data_handling")["inputs"] == section(served, "data_handling")["inputs"]
    # What runs is the agent's own default, known to no deployment; nothing is said to be canned.
    control = section(alone, "change_control")
    assert control["running_known"] is False and control["digest"] == pack.digest
    assert [(slot["slot"], slot["variant"], slot["known"]) for slot in control["running"]] == [
        ("reader", "agent", False)
    ]
    assert not any(slot["canned"] for slot in control["running"])
    assert section(alone, "limits")["of_the_evidence"][0].startswith(
        "This packet was made without a deployment"
    )
    assert "a deployment records none" in section(alone, "approvals")["required"][0]
    # Appendix A: what a site's records could state was not read, and says so.
    missing = {item["key"]: item for item in alone["not_stated"]}
    for key in ("local_evidence", "local_subgroups", "approval_record"):
        assert missing[key]["who"] == "site"
        assert missing[key]["note"] == (
            "Not read: this packet was made without a deployment's database."
        )
    assert {item["key"] for item in served["not_stated"]} <= set(missing)
    # A section that lost its sentence, or a window of days, is not this kind of packet.
    cut = json.loads(json.dumps(alone))
    del cut["sections"][5]["content"]["no_deployment"]
    assert packet.problems({**cut, "sha256": packet._digest_of(cut)})
    assert packet.problems({**alone, "window_days": 90})


def test_the_page_made_without_a_deployment_shows_no_figure_of_one(store: AuditStore) -> None:
    pack = demo()
    alone = packet.build_alone(pack)
    text = packet.render_html(alone)
    page = parsed(text)
    assert not [tag for tag, _ in page.tags if tag in ("script", "img", "link", "iframe")]
    assert "Deployment: none, made from the agent's files alone" in text
    assert "covering the last" not in text and "Organization:" not in text
    said = html_text(text)
    assert packet.NO_DEPLOYMENT in said and packet.ABOUT_ALONE in said
    for why in packet.WITHOUT_DEPLOYMENT.values():
        assert why in said
    # Nothing a deployment's packet shows of its own data: no count, no empty list, no setting.
    for words in (
        "Waiting for review now",
        "No hold covers this agent now",
        "Decisions made here",
        "The validation brief's gates",
        "Alerts now",
        "Compared with a benchmark",
        "no alert destination is set",
        "No decision of this version in these weeks",
        "How an input is recognized again",
        "How long a kept input stays",
        "Browser sessions",
        "Daily signed checkpoint",
        "None recorded for this agent in this organization",
        "Made from the command line",
        "This site has not stated",
    ):
        assert words not in said, words
    # What the agent's files and the software say is all there.
    for words in (
        "Every decision ends in one of four outcomes",
        "What is checked, and what raises an alert",
        "What happens when the agent or its model changes",
        "Fields the agent reads",
        "What this agent needs before it automates",
        "For the committee",
        "A site that adopts it has yet to state",
        "The publisher has not stated",
    ):
        assert words in said, words
    # A deployment's page keeps its own words.
    Decision(pack, store=store).decide(REFILL)
    served = html_text(packet.render_html(packet.build(store, pack, deployment=local_deployment())))
    assert "This site has not stated" in served and "Waiting for review now" in served
    assert "without a deployment" not in served


@pytest.mark.parametrize("origin", ["community", "hub"])
def test_a_packet_without_a_deployment_keeps_what_the_runtime_enforces(origin: str) -> None:
    """Where an agent came from still decides what the runtime enforces, with no deployment
    read: a community agent never decides alone, and one from a registry needs approval."""
    alone = packet.build_alone(demo(origin))
    assert packet.problems(alone) == []
    enforced = " ".join(section(alone, "risk")["enforced"])
    needs = " ".join(section(alone, "approvals")["required"])
    if origin == "community":
        assert "A community agent never decides alone" in enforced
        assert "A community agent never automates" in needs
    else:
        assert "It came from a registry" in enforced
        assert "An agent from a registry needs the organization's approval" in needs
        signed = section(alone, "change_control")["signed"]
        assert signed["held_now"] is None and signed["recall_checked"] is False
        assert "none standing" not in packet.render_html(alone)  # no registry was asked


# -- actions, a checked confidence and a second signal (spec 0.4) ----------------------------------


def test_the_packet_says_what_the_agent_may_ask_to_have_done_and_how_each_is_gated() -> None:
    """From the manifest: each declared action with its effect and the runtime's gate, in the
    Decision Card's words. The demo agent declares two, is not calibrated and has no second
    signal, and the page says each of those."""
    alone = packet.build_alone(demo())
    assert packet.problems(alone) == []
    oversight = section(alone, "oversight")
    assert [
        (a["name"], a["effect"], a["gate"], a["tightened"], a["arguments"])
        for a in oversight["actions"]
    ] == [
        ("assign_queue", "write", "auto", False, ["queue"]),
        ("send_acknowledgement", "contact", "person", True, ["template"]),
    ]
    assert oversight["actions"][0]["released"] == (
        "Only with an automated decision; otherwise when a reviewer confirms it."
    )
    assert oversight["confidence"] == [
        {"slot": "reader", "variant": "agent", "calibrated": False, "why_not": "not calibrated"}
    ]
    assert oversight["second_signal"] == []
    said = reads(packet.render_html(alone))
    for words in (
        "What the agent may ask to have done",
        "This software authorizes each one and runs none",
        "Never without a person: when a reviewer confirms the decision. tightened by the agent",
        "checked: queue",
        "checked: template",
        "Whether the agent's confidence was checked",
        "reader / agent: not calibrated. Its confidence is the agent's own report",
        "Not stated: no part of this agent is asked a case more than once, and none is checked "
        "by a second model.",
    ):
        assert words in said, words
    # A community agent's proposals are never released without a person, a read's included.
    community = section(packet.build_alone(demo("community")), "oversight")["actions"]
    assert [a["gate"] for a in community] == ["auto", "person"]


def test_an_agent_that_declares_no_action_has_it_said_as_not_stated(tmp_path: Path) -> None:
    """Nothing is filled in: no action, no agent's own service and no second signal are each
    said to be not stated, and the document holds an empty list for each."""
    folder = tmp_path / "plain"
    shutil.copytree(DEMO_AGENT_DIR, folder)
    manifest = yaml.safe_load((folder / "agent.yaml").read_text())
    del manifest["actions"]
    manifest["providers"]["reader"]["use"] = "rules"
    del manifest["providers"]["reader"]["variants"]["agent"]
    (folder / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    alone = packet.build_alone(load_pack(folder))
    assert packet.problems(alone) == []
    oversight = section(alone, "oversight")
    assert oversight["actions"] == oversight["confidence"] == oversight["second_signal"] == []
    said = reads(packet.render_html(alone))
    assert "Not stated: the agent declares no action." in said
    assert "Not stated: no part of this agent is an agent's own service" in said
    assert "Not stated: no part of this agent is asked a case more than once" in said


def test_a_calibration_and_a_second_signal_are_read_from_the_manifest(tmp_path: Path) -> None:
    def add(manifest: dict[str, Any]) -> None:
        manifest["providers"]["reader"]["variants"]["checked"] = {
            "type": "check",
            "members": {
                "agent": live_member(manifest, agent_id="first-agent"),
                "second": live_member(manifest, agent_id="second-agent"),
            },
        }
        variant(manifest).update({"repeat": 3, "calibration": "models/c.json"})

    root = scaffold(tmp_path, edit=add)
    calibration_file(root, {"request": 1.3}, relative="models/c.json")
    alone = packet.build_alone(load_pack(root))
    assert packet.problems(alone) == []
    oversight = section(alone, "oversight")
    calibrated, first, second = oversight["confidence"]
    assert calibrated == {
        "slot": "reader",
        "variant": "agent",
        "calibrated": True,
        "cases": 400,
        "dataset": "calibration",
        "date": "2026-11-24",
        "agent_id": "starter-portal-agent",
        "agent_version": "2.1.0",
        "questions": ["request"],
        "not_fitted": {},
    }
    assert [(e["variant"], e["calibrated"]) for e in (first, second)] == [
        ("checked.agent", False),
        ("checked.second", False),
    ]
    assert oversight["second_signal"] == [
        {"slot": "reader", "variant": "agent", "kind": "repeat", "runs": 3},
        {
            "slot": "reader",
            "variant": "checked",
            "kind": "check",
            "answers": "agent",
            "checks": "second",
        },
    ]
    assert oversight["actions"] == []  # the scaffold declares none: its page says "Not stated"
    said = reads(packet.render_html(alone))
    for words in (
        "reader / agent: calibrated on 400 cases of calibration on 2026-11-24, for "
        "starter-portal-agent version 2.1.0 (questions: request).",
        "When the agent's service reports another version, no decision is automated until it "
        "is calibrated again.",
        "reader / agent: each case is asked 3 times.",
        "reader / checked: agent answers and second checks. Where they disagree, a person decides.",
        "A second signal only lowers automation: agreement raises no confidence.",
        "Not stated: the agent declares no action.",
    ):
        assert words in said, words


def test_an_evaluation_says_what_it_found_of_confidence_and_of_a_second_signal(
    open_client: TestClient,
) -> None:
    """From the latest evaluation: the demo agent's says its variant is not calibrated, and that
    no second signal ran."""
    ran = open_client.post(f"{AGENT}/evals", json={"dataset": "examples"})
    assert ran.status_code == 200, ran.text
    built = open_client.get(PACKET).json()
    assert packet.problems(built) == []
    (found,) = section(built, "site_evidence")["evaluations"]
    (variant_found,) = found["calibration"]
    questions = variant_found.pop("questions")
    assert variant_found == {
        "slot": "reader",
        "variant": "agent",
        "calibrated": False,
        "cases": None,
        "dataset": None,
        "agent_version": None,
        "same_cases": False,
    }
    assert {q["question"] for q in questions} == {"request", "urgent_symptoms"}
    assert all(q["ece_before"] is None and q["n"] == 12 for q in questions)
    assert found["second_signal"] is None
    page = reads(open_client.get(f"{PACKET}?format=html").text)
    assert "Confidence, in this evaluation" in page
    assert "reader / agent: not calibrated; calibration error of" in page
    assert "Not stated: no second signal ran in this evaluation." in page


def test_a_reports_confidence_blocks_are_read_as_names_and_counts_and_nothing_else() -> None:
    """A report that holds neither block says "Not stated" for both. One that holds them is
    read field by field: what is not a name or a count is left out, and no line of text a
    report carries is passed on to the page."""
    assert packet._evaluated_calibration({}) is None
    assert packet._evaluated_second_signal({}) is None
    assert packet._evaluated_confidence_words(None) == (
        "Not stated: this evaluation's report says nothing of calibration."
    )
    assert packet._evaluated_second_signal_words(None) == (
        "Not stated: no second signal ran in this evaluation."
    )
    run = {
        "calibration": {
            "variants": [
                {
                    "slot": "reader",
                    "variant": "agent",
                    "status": "calibrated",
                    "same_cases": True,
                    "fitted": {
                        "cases": 400,
                        "dataset": "calibration",
                        "agent": {"id": "portal-agent", "version": "2.1.0"},
                        "note": f"<script>{SENTINEL}</script>",
                    },
                    "questions": {
                        "request": {"n": 300, "before": {"ece": 0.142}, "after": {"ece": 0.031}},
                        "broken": "not figures",
                    },
                    "lines": [SENTINEL],
                },
                "not an entry",
                {"slot": 7, "variant": "agent"},
            ],
            "lines": [SENTINEL],
        },
        "second_signal": {
            "cases": 300,
            "signals": [
                {
                    "slot": "reader",
                    "variant": "agent",
                    "kind": "repeat",
                    "runs": 3,
                    "cases": 300,
                    "canned": 0,
                    "disagreed": 41,
                    "caught": 30,
                },
                {"slot": "reader", "variant": "agent", "kind": "anything", "cases": 1},
            ],
            "kept_from_automation": {"k": 25},
            "lines": [SENTINEL],
        },
    }
    calibration = packet._evaluated_calibration(run)
    signal = packet._evaluated_second_signal(run)
    assert calibration == [
        {
            "slot": "reader",
            "variant": "agent",
            "calibrated": True,
            "cases": 400,
            "dataset": "calibration",
            "agent_version": "2.1.0",
            "same_cases": True,
            "questions": [
                {"question": "request", "n": 300, "ece_before": 0.142, "ece_after": 0.031}
            ],
        }
    ]
    assert signal is not None and signal["kept_from_automation"] == 25
    assert [(s["kind"], s["disagreed"], s["checked"]) for s in signal["signals"]] == [
        ("repeat", 41, 300)
    ]
    words = packet._evaluated_confidence_words(calibration)
    assert "calibrated on 400 cases of <code>calibration</code>, for version" in words
    assert "these are the cases it was fitted on" in words
    assert "0.142 before it, 0.031 after" in words
    said = packet._evaluated_second_signal_words(signal)
    assert "each case asked 3 times; disagreed on 41 of 300 cases, 30 of them wrong" in said
    assert "kept 25 of 300 cases from automation" in said
    assert SENTINEL not in json.dumps([calibration, signal]) + words + said


def test_what_a_part_calls_is_said_in_words_that_read(store: AuditStore) -> None:
    """An agent's own service, never "a agent service"."""
    pack = demo()
    Decision(pack, store=store).decide(REFILL)
    built = packet.build(store, pack, deployment=local_deployment(agent_endpoint=True))
    page = packet.render_html(built)
    said = reads(page)
    assert "a agent" not in said and "a agent" not in page
    assert "the agent's own service, model mock-portal-agent" in said
    assert packet._service("llm") == "a service of type <code>llm</code>"
    assert packet._service("check") == "the services of its members (<code>check</code>)"
    canned = reads(packet.render_html(packet.build(store, pack, deployment=local_deployment())))
    assert "nothing here: it answers from canned examples" in canned
