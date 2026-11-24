# SPDX-License-Identifier: Apache-2.0
"""An agent's connection, set in the console (ADR-0021: `core/connections.py`,
`server/connection_routes.py`, `providers/reach.py`, `AgentProvider.live`).

Who may set one, what is kept and where it may lead: every refusal (an address that is not
https, one off the deployment's allow-list, one that leads inside, a token where there is no key
to seal it under, another organization's, a role without the right); a recognizable token that
is searched for in every answer, log line and chained payload; the order a live call resolves
in; and the switch that turns the feature off. Nothing here leaves this machine: the client a
connection is called through without an allow-list is replaced (`outside`)."""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from mendari.core.audit import AuditStore
from mendari.core.connections import ALLOWED as ALLOWED_BY_AN_OPERATOR
from mendari.core.connections import (
    KEEP,
    LISTED,
    NOT_LISTED_NOBODY,
    SWITCH_ENV,
    ConnectionRefused,
    ConnectionStore,
    admitted,
    enabled,
)
from mendari.core.connections import PUBLIC as PUBLIC_ONLY
from mendari.core.pack import Pack, load_pack
from mendari.core.phi import PhiPolicy, key_id
from mendari.errors import MendariError, ProviderError
from mendari.providers import reach
from mendari.providers.agent import ENDPOINT_ENV, TOKEN_ENV, Connection, Live
from mendari.providers.http import ALLOWED_ORIGINS_ENV
from mendari.server import connection_routes
from mendari.server.app import create_app
from mendari.standards.fhir_client import origin_of
from tests.conftest import (
    DASHBOARD,
    DEMO_AGENT_DIR,
    INPUT_HASH_KEY,
    encrypt_mode,
    encrypted_store,
    member,
    message,
    security_lines,
    signed_in,
)
from tests.test_agent_provider import agent_response, make_agent_provider
from tests.test_hub_routes import (  # noqa: F401 - `service` is the local agent service fixture
    REFILL,
    Service,
    answered,
    service,
)
from tests.test_memberships import HTTPS
from tests.test_store_uploads import ORG_A, ORG_B, Store, store_world

DEMO = "mendari/portal-message-agent"
INBOX = "mendari/patient-inbox-router"
# A token a search finds anywhere: no other value in these tests holds this text.
TOKEN = "zqTOKEN-5f1c0d6e9a3b47c2-never-shown"
PUBLIC = "93.184.216.34"  # a public address a patched look-up answers; never connected to
ELSEWHERE = "https://agents.example.org/v1/decide"
AT = f"/api/connections/{DEMO}"
# The client itself, before the fixture below puts another in its place.
REAL_PUBLIC_CLIENT = reach.public_client


class Outside:
    """What a connection is called through here in place of `reach.public_client`: nothing
    leaves the test. It answers as an agent's service would at any address, and keeps each
    request (its URL and its headers)."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.status = 200
        self.client = httpx.Client(
            transport=httpx.MockTransport(self._answer), follow_redirects=False
        )

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": Service.SAID})
        return httpx.Response(200, json=answered(json.loads(request.content)))

    def authorizations(self) -> list[str | None]:
        return [request.headers.get("authorization") for request in self.seen]


@pytest.fixture(autouse=True)
def outside(monkeypatch: pytest.MonkeyPatch) -> Outside:
    made = Outside()
    monkeypatch.setattr(reach, "public_client", lambda: made.client)
    return made


@pytest.fixture(autouse=True)
def looked_up(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """The look-up of names, here: every name leads to one public address unless a test says
    otherwise (`looked_up[name] = [...]`; an empty list: the name cannot be looked up). The
    system's resolver is never asked."""
    answers: dict[str, list[str]] = {}

    def resolve(host: str, port: int) -> list[str]:
        found = answers.get(host, [PUBLIC])
        if not found:
            raise OSError("no such name")
        return found

    monkeypatch.setattr(reach, "resolve", resolve)
    return answers


def serving_the_agent(
    target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Store]:
    """A memberships-mode server (its agent store on) that also serves the bundled agent, which
    asks a service of its own."""
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    yield from store_world(target, tmp_path, monkeypatch)


@pytest.fixture
def site(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    yield from serving_the_agent(db_target, tmp_path, monkeypatch)


@pytest.fixture
def lite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """The same server on SQLite only, for what does not depend on the database."""
    yield from serving_the_agent(str(tmp_path / "audit.db"), tmp_path, monkeypatch)


def put(site: Store, org: str = ORG_A, who: str = "admin", at: str = AT, **body: Any) -> Any:
    return site.client.put(at, json={"endpoint": ELSEWHERE, **body}, headers=site.as_(org, who))


def read(site: Store, org: str = ORG_A, who: str = "admin", at: str = AT) -> dict[str, Any]:
    answer = site.client.get(at, headers=site.as_(org, who))
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


def decide(site: Store, org: str = ORG_A) -> dict[str, Any]:
    answer = site.client.post(
        f"/api/agents/{DEMO}/decide", json=REFILL, headers=site.as_(org, "reviewer")
    )
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


def rows(store: AuditStore) -> list[dict[str, Any]]:
    with store._db.session(exclusive=False) as db:
        return [dict(row) for row in db.all("SELECT * FROM agent_connections ORDER BY org")]


def events(store: AuditStore) -> list[tuple[str, dict[str, Any]]]:
    with store._db.session(exclusive=False) as db:
        return [
            (row["event"], json.loads(row["payload"]))
            for row in db.all(
                "SELECT event, payload FROM audit_events WHERE event LIKE ? ORDER BY seq",
                ("agent_connection.%",),
            )
        ]


# -- where an address may lead (`core/connections.py` `admitted`, `providers/reach.py`) -----------


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("http://agents.example.org/decide", "starts with https://"),
        ("ftp://agents.example.org/decide", "starts with https://"),
        ("agents.example.org/decide", "starts with https://"),
        ("https://someone:pw@agents.example.org/decide", "no user or password"),
        ("https://agents.example.org/decide?key=zq", "no query"),
        ("https://agents.example.org/decide#zq", "no query"),
        ("https://agents.example.org/a b", "no space"),
        ("https://agents.example.org/decide?", "no query"),
        # What two parsers could read two ways is not an address here.
        ("https://agents.example.org\\@10.0.0.5/decide", "plain URL characters"),
        ("https://10.0.0.5\\.example.org/decide", "plain URL characters"),
        ("https://ag\u00e9nts.example.org/decide", "plain URL characters"),
        (
            "https://10\u30020\u30020\u30025/decide",
            "plain URL characters",
        ),  # ideographic full stops
        ("https://agents.example.org%2e10.0.0.5/decide", "a name or an IP address"),
        ("https://[fe80::1%25eth0]/decide", "a name or an IP address"),
        ("https://-/decide", ""),
        ("https://agents.example.org:port/decide", "not a valid URL"),
        ("https://" + "a" * 2050, "printable characters"),
        ("", "printable characters"),
        ("https://agents.example.org/\x00", "printable characters"),
    ],
)
def test_an_address_that_is_not_a_plain_https_url_is_refused(address: str, why: str) -> None:
    with pytest.raises(ConnectionRefused) as refused:
        admitted(address)
    assert refused.value.status == 422 and why in str(refused.value)


INSIDE = [
    "http://127.0.0.1:8000/decide",  # this machine: plain http is allowed there, and refused here
    "http://localhost:8000/decide",
    "https://localhost/decide",
    "https://127.0.0.1/decide",
    "https://[::1]/decide",
    "https://0.0.0.0/decide",
    "https://10.0.0.5/decide",
    "https://172.16.9.9/decide",
    "https://192.168.1.10/decide",
    "https://169.254.169.254/latest/meta-data",  # a cloud's metadata address
    "https://100.64.0.1/decide",  # shared address space
    "https://[fd00::1]/decide",  # unique local
    "https://[fe80::1]/decide",  # link-local
    "https://[fec0::1]/decide",  # site-local
    "https://[::ffff:10.0.0.5]/decide",  # a private IPv4 address, wrapped
    "https://[::ffff:127.0.0.1]/decide",
    "https://[64:ff9b::a00:5]/decide",  # 10.0.0.5 through NAT64
    "https://[2002:a00:5::1]/decide",  # 10.0.0.5 through 6to4
    "https://224.0.0.1/decide",  # multicast
    "https://240.0.0.1/decide",  # reserved
    "https://intranet/decide",  # a name on the server's own network
    "https://agents.internal.example.org/decide",  # a name that leads inside (looked up)
    "https://both.example.org/decide",  # a name that leads outside and inside
    "https://nowhere.example.org/decide",  # a name that cannot be looked up
]


@pytest.mark.parametrize("address", INSIDE)
def test_without_an_allow_list_an_address_leads_to_the_public_internet_or_is_refused(
    address: str, looked_up: dict[str, list[str]]
) -> None:
    looked_up["agents.internal.example.org"] = ["10.1.2.3"]
    looked_up["both.example.org"] = [PUBLIC, "192.168.0.7"]
    looked_up["nowhere.example.org"] = []
    with pytest.raises(ConnectionRefused) as refused:
        admitted(address)
    assert refused.value.status == 422
    said = str(refused.value)
    assert "public internet" in said and ALLOWED_ORIGINS_ENV in said
    # Never the address a name led to: a refusal is no way to map the server's network.
    assert "10.1.2.3" not in said and "192.168.0.7" not in said


@pytest.mark.parametrize(
    "address",
    [
        "https://127.1/decide",  # this machine, written short
        "https://0x7f.0.0.1/decide",  # in hexadecimal
        "https://0177.0.0.1/decide",  # in octal
        "https://0300.0250.0.1/decide",  # 192.168.0.1 in octal
        "https://2130706433/decide",  # as one number
        "https://10.0.0.5.7/decide",  # numbers that are no address at all
        "https://localhost./decide",
    ],
)
def test_this_machine_written_another_way_is_still_this_machine(
    address: str, looked_up: dict[str, list[str]]
) -> None:
    """A resolver reads each of these as an address, and not every system's the same way
    (`0177.0.0.1` is 127.0.0.1 on one and 177.0.0.1 on another): none is looked up, here or
    when a connection is opened. Refused though the look-up, were it asked, would say public."""
    with pytest.raises(ConnectionRefused) as refused:
        admitted(address)
    assert refused.value.status == 422 and "public internet" in str(refused.value)
    host = address.removeprefix("https://").split("/")[0]
    with pytest.raises(httpcore.ConnectError):
        reach.PublicOnly().connect_tcp(host, 443)


def test_a_public_address_is_accepted_and_kept_as_it_was_given() -> None:
    assert admitted(f"  {ELSEWHERE} ") == (ELSEWHERE, "https://agents.example.org", PUBLIC_ONLY)
    assert admitted("https://1.1.1.1:8443/decide") == (
        "https://1.1.1.1:8443/decide",
        "https://1.1.1.1:8443",
        PUBLIC_ONLY,
    )
    # With no list there is nothing to allow: an operator's word changes nothing.
    assert admitted(ELSEWHERE, allow=True)[2] == PUBLIC_ONLY
    assert admitted("https://[2606:4700:4700::1111]/x")[1] == "https://[2606:4700:4700::1111]"


def test_with_an_allow_list_an_address_is_on_it_or_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list is the operator's word: an origin on it may be on the site's own network; one
    off it is refused, public or not; and a list that cannot be read accepts nothing."""
    monkeypatch.setenv(
        ALLOWED_ORIGINS_ENV, "https://agents.internal.example.org,http://127.0.0.1:8000"
    )
    assert admitted("https://agents.internal.example.org/decide")[1] == (
        "https://agents.internal.example.org"
    )
    assert admitted("http://127.0.0.1:8000/decide")[1] == "http://127.0.0.1:8000"
    for off in (
        ELSEWHERE,
        "http://127.0.0.1:8001/decide",
        "https://agents.internal.example.org:8443/x",
    ):
        with pytest.raises(ConnectionRefused) as refused:
            admitted(off)
        assert refused.value.status == 422 and ALLOWED_ORIGINS_ENV in str(refused.value)
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, "not a list of origins")
    with pytest.raises(ConnectionRefused) as unread:
        admitted(ELSEWHERE)
    assert unread.value.status == 409


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("1.1.1.1", True),
        ("2606:4700:4700::1111", True),
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("192.0.2.1", False),  # documentation
        ("::1", False),
        ("::", False),
        ("fc00::1", False),
        ("fec0::1", False),
        ("::ffff:192.168.0.1", False),
        ("::ffff:1.1.1.1", True),
        ("64:ff9b::7f00:1", False),
        ("2002:c0a8:1::1", False),
        ("ff02::1", False),
    ],
)
def test_which_addresses_are_public(address: str, public: bool) -> None:
    assert reach.is_public(ipaddress.ip_address(address)) is public


def test_a_connection_is_opened_to_the_address_that_was_checked_and_to_no_other(
    monkeypatch: pytest.MonkeyPatch, looked_up: dict[str, list[str]]
) -> None:
    """The client's own network layer: it looks the name up itself, refuses unless every address
    is public, and connects to the address it checked. So a name that answered a public address
    when the connection was set and a private one when it is called connects to nothing."""
    opened: list[tuple[str, int]] = []

    def connect(self: Any, host: str, port: int, **options: Any) -> Any:
        opened.append((host, port))
        return object()

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    backend = reach.PublicOnly()
    backend.connect_tcp("agents.example.org", 443)
    assert opened == [(PUBLIC, 443)]  # the address, never the name again
    looked_up["agents.example.org"] = ["127.0.0.1"]  # the name changed its answer
    for name in ("agents.example.org", "localhost", "10.0.0.5", "intranet"):
        with pytest.raises(httpcore.ConnectError):
            backend.connect_tcp(name, 443)
    looked_up["agents.example.org"] = [PUBLIC, "10.0.0.5"]
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("agents.example.org", 443)
    assert opened == [(PUBLIC, 443)]


def test_the_public_client_follows_no_redirect_and_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real client (never used to send anything here): it is built once, follows no
    redirect, reads no proxy from the environment, and opens its connections through
    `PublicOnly`."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(reach, "_CLIENT", None)
    opened: list[str] = []

    def connect(self: Any, host: str, port: int, **options: Any) -> Any:
        opened.append(host)
        raise httpcore.ConnectError("no connection is opened in this test")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    client = REAL_PUBLIC_CLIENT()
    try:
        assert client.follow_redirects is False and client.trust_env is False
        assert REAL_PUBLIC_CLIENT() is client
        with pytest.raises(httpx.ConnectError):
            client.post(ELSEWHERE, json={})
        # Straight to the address the look-up answered: neither the name, nor a proxy.
        assert opened == [PUBLIC]
        with pytest.raises(httpx.ConnectError):
            client.post("https://10.0.0.5/decide", json={})
        assert opened == [PUBLIC]
    finally:
        client.close()


# -- the store (`core/connections.py`) ------------------------------------------------------------


def test_a_connection_is_kept_for_its_organization_sealed_and_chained(db_target: str) -> None:
    """On SQLite and Postgres: the token is sealed in its row, opened only to call, shown as who
    set it and when; each change is chained without it; another organization has nothing."""
    with encrypted_store(db_target) as store:
        connections = ConnectionStore(store)
        assert connections.seals and connections.get(ORG_A, DEMO) is None
        made = connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, token=TOKEN, by="ada")
        assert made == connections.get(ORG_A, DEMO)
        assert (made["endpoint"], made["origin"]) == (ELSEWHERE, "https://agents.example.org")
        assert (made["token"], made["token_set_by"], made["set_by"]) == ("set", "ada", "ada")
        assert made["token_set_at"] == made["set_at"] and TOKEN not in json.dumps(made)
        [row] = rows(store)
        assert row["org"] == ORG_A and TOKEN not in json.dumps(row)
        assert connections.resolve(ORG_A, DEMO) == Connection(ELSEWHERE, TOKEN)
        assert connections.listed(ORG_A) == [made]
        # Another organization: nothing to read, to call or to clear.
        assert connections.get(ORG_B, DEMO) is None and connections.listed(ORG_B) == []
        assert connections.resolve(ORG_B, DEMO) is None
        with pytest.raises(ConnectionRefused) as refused:
            connections.clear(ORG_B, DEMO, by="eve")
        assert refused.value.status == 404
        assert connections.get(ORG_A, DEMO) == made
        # The same again changes nothing and chains nothing.
        assert connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, by="bob") == made
        assert events(store) == [
            (
                "agent_connection.set",
                {
                    "org": ORG_A,
                    "pack_id": DEMO,
                    "origin": "https://agents.example.org",
                    "token": True,
                    "changed_by": "ada",
                },
            )
        ]
        connections.clear(ORG_A, DEMO, by="bob")
        assert connections.get(ORG_A, DEMO) is None and connections.resolve(ORG_A, DEMO) is None
        [kept] = rows(store)  # the row stays, emptied: its events name it
        assert (kept["id"], kept["endpoint"], kept["token"]) == (row["id"], None, None)
        assert events(store)[-1] == (
            "agent_connection.cleared",
            {"org": ORG_A, "pack_id": DEMO, "changed_by": "bob"},
        )
        with pytest.raises(ConnectionRefused):
            connections.clear(ORG_A, DEMO, by="bob")  # none to clear
        again = connections.set(ORG_A, DEMO, endpoint="https://1.1.1.1/decide", by="ada")
        assert again["token"] == "none" and rows(store)[0]["id"] == row["id"]
        assert store.verify()["ok"]
        listed = store.iter_administrative_events(ORG_A)
        assert [item["event"] for item in listed if item["event"].startswith("agent_conn")] == [
            "agent_connection.set",
            "agent_connection.cleared",
            "agent_connection.set",
        ]
        assert list(store.iter_administrative_events(ORG_B)) == []


def test_a_token_stays_with_the_address_it_was_set_for(tmp_path: Path) -> None:
    """Changing the address drops the token unless a new one comes with it: nobody can have the
    server send a token they cannot read to an address of theirs."""
    with encrypted_store(tmp_path / "a.db") as store:
        connections = ConnectionStore(store)
        connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, token=TOKEN, by="ada")
        kept = connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, token=KEEP, by="bob")
        assert kept["token"] == "set" and kept["token_set_by"] == "ada"
        moved = connections.set(ORG_A, DEMO, endpoint="https://1.1.1.1/decide", by="mallory")
        assert moved["token"] == "none" and moved["token_set_by"] is None
        assert connections.resolve(ORG_A, DEMO) == Connection("https://1.1.1.1/decide", None)
        assert rows(store)[0]["token"] is None
        # Back at the first address: the token does not come back either.
        back = connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, by="mallory")
        assert back["token"] == "none"
        both = connections.set(ORG_A, DEMO, endpoint="https://1.1.1.1/x", token="other", by="ada")
        assert both["token"] == "set"
        assert connections.resolve(ORG_A, DEMO) == Connection("https://1.1.1.1/x", "other")
        removed = connections.set(ORG_A, DEMO, endpoint="https://1.1.1.1/x", token=None, by="ada")
        assert removed["token"] == "none" and connections.resolve(ORG_A, DEMO) == Connection(
            "https://1.1.1.1/x", None
        )
        payloads = [payload for _, payload in events(store)]
        assert [p["token"] for p in payloads] == [True, False, False, True, False]
        assert [p["changed_by"] for p in payloads] == ["ada", "mallory", "mallory", "ada", "ada"]


@pytest.mark.parametrize("mode", ["off", "hash"])
def test_a_token_is_refused_where_there_is_no_key_to_seal_it_under(
    tmp_path: Path, mode: str
) -> None:
    policy = PhiPolicy(mode=mode, hash_key=INPUT_HASH_KEY if mode == "hash" else None)  # type: ignore[arg-type]
    with AuditStore(tmp_path / "a.db", phi=policy) as store:
        connections = ConnectionStore(store)
        assert connections.seals is False
        with pytest.raises(ConnectionRefused) as refused:
            connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, token=TOKEN, by="ada")
        assert refused.value.status == 409 and "MENDARI_PHI_MODE=encrypt" in str(refused.value)
        assert TOKEN not in str(refused.value)
        assert rows(store) == [] and events(store) == []
        made = connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, by="ada")  # an address alone
        assert made["token"] == "none"
        assert connections.resolve(ORG_A, DEMO) == Connection(ELSEWHERE, None)


@pytest.mark.parametrize(
    "token", ["with space", "line\nbreak", "tab\there", "", "naïve", "x" * 4097, 7, ["x"]]
)
def test_a_token_is_one_line_of_visible_ascii(tmp_path: Path, token: Any) -> None:
    with encrypted_store(tmp_path / "a.db") as store:
        with pytest.raises(ConnectionRefused) as refused:
            ConnectionStore(store).set(ORG_A, DEMO, endpoint=ELSEWHERE, token=token, by="ada")
        assert refused.value.status == 422 and rows(store) == []


def test_a_token_opens_only_for_its_own_row_organization_and_agent(tmp_path: Path) -> None:
    """A sealed token moved to another organization's row, or another agent's, opens nothing:
    the call fails, and never goes out with another's token."""
    with encrypted_store(tmp_path / "a.db") as store:
        connections = ConnectionStore(store)
        connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, token=TOKEN, by="ada")
        connections.set(ORG_B, DEMO, endpoint="https://1.1.1.1/b", token="theirs", by="eve")
        connections.set(ORG_A, INBOX, endpoint="https://1.1.1.1/c", token="inbox", by="ada")
        sealed = {(row["org"], row["pack_id"]): row["token"] for row in rows(store)}
        with store._db.session(exclusive=True) as db:
            db.all(
                "UPDATE agent_connections SET token = ? WHERE org = ? AND pack_id = ?",
                (sealed[(ORG_A, DEMO)], ORG_B, DEMO),
            )
            db.all(
                "UPDATE agent_connections SET token = ? WHERE org = ? AND pack_id = ?",
                (sealed[(ORG_A, DEMO)], ORG_A, INBOX),
            )
        for org, agent in ((ORG_B, DEMO), (ORG_A, INBOX)):
            with pytest.raises(MendariError):
                connections.resolve(org, agent)
        assert connections.resolve(ORG_A, DEMO) == Connection(ELSEWHERE, TOKEN)


def test_a_phi_key_rotation_reseals_the_token_and_the_check_counts_it(db_target: str) -> None:
    old, new, hashed = (secrets.token_bytes(32) for _ in range(3))
    with AuditStore(db_target, phi=PhiPolicy("encrypt", old, hashed)) as store:
        ConnectionStore(store).set(ORG_A, DEMO, endpoint=ELSEWHERE, token=TOKEN, by="ada")
        ConnectionStore(store).set(ORG_B, DEMO, endpoint="https://1.1.1.1/b", by="eve")  # no token
        before = rows(store)[0]["token"]
    with AuditStore(db_target, phi=PhiPolicy("encrypt", new, hashed, (old,))) as store:
        summary = store.rotate_phi(to=key_id(new))
        assert summary["connections"] == 1 and summary["unopenable"] == []
        after = rows(store)[0]["token"]
        assert after != before and after.startswith(f"v2:{key_id(new)}:")
        assert store.phi_check()["by_key"]["connections"] == {key_id(new): 1}
        assert store.rotate_phi(to=key_id(new)).get("connections", 0) == 0  # done stays done
        assert store.verify()["ok"]
    with AuditStore(db_target, phi=PhiPolicy("encrypt", new, hashed)) as store:  # the new key alone
        assert ConnectionStore(store).resolve(ORG_A, DEMO) == Connection(ELSEWHERE, TOKEN)
        assert TOKEN not in json.dumps(rows(store))


def test_the_switch_is_on_unless_it_says_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH_ENV, raising=False)
    assert enabled() is True
    for value, expected in (("on", True), (" ON ", True), ("off", False), ("Off", False)):
        monkeypatch.setenv(SWITCH_ENV, value)
        assert enabled() is expected
    monkeypatch.setenv(SWITCH_ENV, "maybe")
    with pytest.raises(MendariError, match=SWITCH_ENV):
        enabled()


# -- where a live call goes (`AgentProvider.live`) ------------------------------------------------


@pytest.fixture
def agent() -> Pack:
    return load_pack(DEMO_AGENT_DIR)


def test_a_live_call_goes_to_the_organizations_connection_then_the_deployments_then_none(
    agent: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order, as `AgentProvider.live` states it once; and a token goes only to the address
    it was set with."""
    sent: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json=agent_response())

    provider = make_agent_provider(agent, handler)
    # Nothing set: its canned examples.
    assert provider.live() is None and provider.has_canned_examples
    assert provider.answer(message("Please refill my pills")).canned is not None and sent == []

    monkeypatch.setenv(ENDPOINT_ENV, "https://deployment.test/decide")
    monkeypatch.setenv(TOKEN_ENV, "deployment-token")
    assert provider.live() == Live(
        "https://deployment.test/decide", "deployment", "deployment-token"
    )
    provider.answer(message("Please refill my pills"))

    theirs: dict[str, Connection | None] = {DEMO: Connection(ELSEWHERE, TOKEN)}
    provider.use_connection(theirs.get)
    assert provider.live() == Live(ELSEWHERE, "organization", TOKEN)
    assert provider.answer(message("Please refill my pills")).canned is None
    theirs[DEMO] = Connection(ELSEWHERE, None)  # no token of its own: none is sent, never another's
    provider.answer(message("Please refill my pills"))
    theirs[DEMO] = None  # cleared: the deployment's again
    provider.answer(message("Please refill my pills"))
    assert sent == [
        ("https://deployment.test/decide", "Bearer deployment-token"),
        (ELSEWHERE, f"Bearer {TOKEN}"),
        (ELSEWHERE, None),
        ("https://deployment.test/decide", "Bearer deployment-token"),
    ]
    assert TOKEN not in repr(provider.live()) and TOKEN not in repr(Connection(ELSEWHERE, TOKEN))
    assert TOKEN not in repr(Live(ELSEWHERE, "organization", TOKEN))


def test_a_connection_that_cannot_be_read_fails_the_call_and_never_falls_through(
    agent: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json=agent_response())

    monkeypatch.setenv(ENDPOINT_ENV, "https://deployment.test/decide")
    provider = make_agent_provider(agent, handler)

    def unreadable(pack_id: str) -> Connection | None:
        raise MendariError(f"the value does not open: {TOKEN}")

    provider.use_connection(unreadable)
    for call in (provider.live, provider.preflight, lambda: provider.answer(message("refill"))):
        with pytest.raises(ProviderError) as failed:
            call()
        assert failed.value.code == "credential_unavailable" and TOKEN not in str(failed.value)
    assert sent == []  # neither the deployment's service nor the canned answers


def test_an_organizations_connection_is_called_through_the_public_client_or_the_allow_list(
    agent: Pack, monkeypatch: pytest.MonkeyPatch, outside: Outside, looked_up: dict[str, list[str]]
) -> None:
    """Without an allow-list a connection goes out through the client that opens public
    addresses only, and its host is checked before each call; with one, through the
    deployment's own client, to a listed origin only."""
    provider = make_agent_provider(agent)  # no client of its own: as a server builds it
    theirs = {DEMO: Connection(ELSEWHERE, TOKEN)}
    provider.use_connection(theirs.get)
    assert provider.answer(message("refill")).canned is None
    assert [str(request.url) for request in outside.seen] == [ELSEWHERE]
    assert outside.authorizations() == [f"Bearer {TOKEN}"]
    for inside in ("https://10.0.0.5/decide", "https://localhost/decide", "https://intranet/x"):
        theirs[DEMO] = Connection(inside, TOKEN)  # as if a row had been changed past the checks
        with pytest.raises(ProviderError) as refused:
            provider.answer(message("refill"))
        assert refused.value.code == "invalid_endpoint"
        with pytest.raises(ProviderError):
            provider.preflight()
    assert len(outside.seen) == 1
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, "https://listed.example.org")
    theirs[DEMO] = Connection(ELSEWHERE, TOKEN)
    with pytest.raises(ProviderError) as off_list:
        provider.answer(message("refill"))
    assert off_list.value.code == "invalid_endpoint" and len(outside.seen) == 1


# -- the routes (`server/connection_routes.py`) ---------------------------------------------------


def test_an_administrator_sets_reads_changes_and_clears_their_organizations_connection(
    site: Store,
) -> None:
    """On SQLite and Postgres, through the API: what each read shows, and what is chained."""
    before = read(site)
    assert (before["applies"], before["source"], before["canned"]) == (True, "none", True)
    assert before["connection"] is None and before["may_set"] is True and before["seals"] is True
    assert before["allowed_origins"] is None and before["deployment_endpoint"] is False
    made = put(site, token=TOKEN)
    assert made.status_code == 200, made.text
    shown = made.json()["connection"]
    assert (shown["endpoint"], shown["origin"]) == (ELSEWHERE, "https://agents.example.org")
    assert shown["token"] == "set" and shown["token_set_by"] == shown["set_by"]
    assert shown["set_by"].endswith("admin-clinic-a")
    after = read(site)
    assert (after["source"], after["canned"], after["connection"]) == ("organization", False, shown)
    assert after["slots"] == [
        {"slot": "reader", "variant": "agent", "source": "organization", "canned_examples": True}
    ]
    # A reader who may not set it sees where it leads, never the path.
    seen = read(site, who="viewer")
    assert seen["may_set"] is False and "endpoint" not in seen["connection"]
    assert seen["connection"]["origin"] == "https://agents.example.org"
    assert seen["connection"]["token"] == "set"
    listed = site.client.get("/api/connections", headers=site.as_(ORG_A, "viewer")).json()
    [mine] = [item for item in listed["agents"] if item["pack_id"] == DEMO]
    assert mine == {key: seen[key] for key in mine}
    assert {item["pack_id"]: item["applies"] for item in listed["agents"]}[INBOX] is False

    changed = put(site, endpoint="https://1.1.1.1/decide")
    assert changed.status_code == 200 and changed.json()["connection"]["token"] == "none"
    cleared = site.client.delete(AT, headers=site.as_(ORG_A, "admin"))
    assert cleared.status_code == 204 and cleared.content == b""
    assert read(site)["connection"] is None and read(site)["source"] == "none"
    assert site.client.delete(AT, headers=site.as_(ORG_A, "admin")).status_code == 404
    assert [
        (kind, payload["token"]) for kind, payload in events(site.db) if "token" in payload
    ] == [
        ("agent_connection.set", True),
        ("agent_connection.set", False),
    ]
    assert events(site.db)[-1][0] == "agent_connection.cleared"
    assert site.db.verify()["ok"]
    listed = site.client.get("/api/audit/administrative", headers=site.as_(ORG_A, "admin")).json()[
        "items"
    ]
    assert "agent_connection.cleared" in {item["event"] for item in listed}


@pytest.mark.parametrize("role", ["viewer", "reviewer", "governance", "pack_author"])
def test_a_role_without_the_right_changes_nothing(lite: Store, role: str) -> None:
    headers = lite.as_(ORG_A, role)
    for refused in (
        lite.client.put(AT, json={"endpoint": ELSEWHERE, "token": TOKEN}, headers=headers),
        lite.client.delete(AT, headers=headers),
        lite.client.post(f"{AT}/check", headers=headers),
    ):
        assert refused.status_code == 403, refused.text
    assert rows(lite.db) == [] and events(lite.db) == []
    assert lite.client.get(AT, headers=headers).status_code == (
        403 if role == "pack_author" else 200
    )


def test_no_machines_credential_sets_a_connection(lite: Store, outside: Outside) -> None:
    """An API key (whatever its role), and a request with no credential at all."""
    admin = lite.as_(ORG_A, "admin")
    for role in ("service", "viewer"):
        key = lite.client.post(
            "/api/auth/keys", json={"role": role, "name": role}, headers=admin
        ).json()["key"]
        machine = {"authorization": f"Bearer {key}"}
        assert lite.client.put(AT, json={"endpoint": ELSEWHERE}, headers=machine).status_code == 403
        assert lite.client.delete(AT, headers=machine).status_code == 403
        assert lite.client.post(f"{AT}/check", headers=machine).status_code == 403
        assert lite.client.get(AT, headers=machine).status_code == 200  # a reader's
    for method, url in (("PUT", AT), ("DELETE", AT), ("POST", f"{AT}/check"), ("GET", AT)):
        bare = lite.client.request(method, url, json={"endpoint": ELSEWHERE})
        assert bare.status_code == 401, (method, url)
    assert lite.client.get("/api/connections").status_code == 401
    assert rows(lite.db) == [] and outside.seen == []


@pytest.mark.parametrize(
    "address",
    [
        "http://agents.example.org/decide",
        "https://10.0.0.5/decide",
        "https://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8000/decide",
        "https://localhost/decide",
        "https://intranet/decide",
        "https://agents.internal.example.org/decide",
        "https://someone:pw@agents.example.org/decide",
        "https://agents.example.org/decide?key=zq",
    ],
)
def test_an_address_that_may_not_be_called_is_refused_and_nothing_is_saved(
    lite: Store, looked_up: dict[str, list[str]], capsys: pytest.CaptureFixture[str], address: str
) -> None:
    looked_up["agents.internal.example.org"] = ["10.1.2.3"]
    capsys.readouterr()
    refused = put(lite, endpoint=address, token=TOKEN)
    assert refused.status_code == 422, refused.text
    assert TOKEN not in refused.text and "10.1.2.3" not in refused.text
    assert rows(lite.db) == [] and events(lite.db) == []
    [said] = [
        line
        for line in security_lines(capsys.readouterr().out)
        if line["event"] == "admin.connection.refused"
    ]
    assert (said["outcome"], said["reason"], said["ids"]) == ("refused", "checks", [DEMO])
    assert read(lite)["connection"] is None


def test_an_origin_off_the_deployments_allow_list_is_refused(
    lite: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, "https://listed.example.org")
    refused = put(lite, token=TOKEN)
    assert refused.status_code == 422 and ALLOWED_ORIGINS_ENV in refused.json()["detail"]
    assert rows(lite.db) == []
    assert read(lite)["allowed_origins"] == ["https://listed.example.org"]
    listed = put(lite, endpoint="https://listed.example.org/decide", token=TOKEN)
    assert listed.status_code == 200, listed.text


def test_without_a_sealing_key_a_token_is_refused_and_an_address_alone_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: Outside
) -> None:
    """A server with no PHI key (open mode on synthetic data, PHI mode off): the form is told it
    cannot keep a token, a token is refused before anything is written, and never echoed. An
    address alone is kept, and called with no token."""
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    target = tmp_path / "open.db"
    with TestClient(create_app(db_path=target)) as client, AuditStore(target) as store:
        assert client.get(AT).json()["seals"] is False
        refused = client.put(AT, json={"endpoint": ELSEWHERE, "token": TOKEN})
        assert refused.status_code == 409, refused.text
        assert "MENDARI_PHI_MODE=encrypt" in refused.json()["detail"]
        assert TOKEN not in refused.text and rows(store) == [] and events(store) == []
        made = client.put(AT, json={"endpoint": ELSEWHERE})
        assert made.status_code == 200 and made.json()["connection"]["token"] == "none"
        assert rows(store)[0]["org"] == "default"
        decided = client.post(f"/api/agents/{DEMO}/decide", json=REFILL)
        assert decided.status_code == 200 and decided.json()["outcome"] == "AUTO"
        assert [str(request.url) for request in outside.seen] == [ELSEWHERE]
        assert outside.authorizations() == [None]


def test_another_organization_neither_sees_nor_changes_a_connection(
    lite: Store, outside: Outside
) -> None:
    assert put(lite, token=TOKEN).status_code == 200
    theirs = read(lite, org=ORG_B)
    assert (theirs["source"], theirs["connection"]) == ("none", None)
    listed = lite.client.get("/api/connections", headers=lite.as_(ORG_B, "admin")).json()
    assert all(item["connection"] is None for item in listed["agents"])
    assert "agents.example.org" not in json.dumps(listed) + json.dumps(theirs)
    other = lite.as_(ORG_B, "admin")
    # A query or a body that names the first organization is not honoured.
    assert lite.client.delete(AT, params={"org": ORG_A}, headers=other).status_code == 404
    own = lite.client.put(
        AT,
        params={"org": ORG_A},
        json={"endpoint": "https://1.1.1.1/theirs", "token": "theirs", "org": ORG_A},
        headers=other,
    )
    assert own.status_code == 200, own.text
    assert {row["org"]: row["endpoint"] for row in rows(lite.db)} == {
        ORG_A: ELSEWHERE,
        ORG_B: "https://1.1.1.1/theirs",
    }
    assert read(lite)["connection"]["endpoint"] == ELSEWHERE
    # Each organization's decisions call its own service with its own token.
    decide(lite, ORG_A)
    decide(lite, ORG_B)
    assert [(str(r.url), r.headers["authorization"]) for r in outside.seen] == [
        (ELSEWHERE, f"Bearer {TOKEN}"),
        ("https://1.1.1.1/theirs", "Bearer theirs"),
    ]
    assert lite.client.delete(AT, headers=other).status_code == 204
    assert read(lite)["connection"]["endpoint"] == ELSEWHERE  # the first's stays
    assert [payload["org"] for _, payload in events(lite.db)] == [ORG_A, ORG_B, ORG_B]
    # An agent in one organization's store is no agent at all to another.
    copied = lite.client.post(
        "/api/store/copies",
        json={"pack": INBOX, "name": "inbox-copy"},
        headers=lite.as_(ORG_A, "pack_author"),
    )
    assert copied.status_code == 201, copied.text
    held = f"/api/connections/{ORG_A}/inbox-copy"
    assert lite.client.get(held, headers=lite.as_(ORG_A, "admin")).json()["applies"] is False
    nothing = "/api/connections/clinic-a/no-such-agent"
    for url in (held, nothing):
        assert lite.client.get(url, headers=other).status_code == 404
        assert lite.client.put(url, json={"endpoint": ELSEWHERE}, headers=other).status_code == 404
        assert lite.client.post(f"{url}/check", headers=other).status_code == 404
    same = [lite.client.get(url, headers=other).json()["detail"] for url in (held, nothing)]
    assert same[0] == same[1]


def test_an_agent_that_decides_on_this_server_has_no_connection_to_set(lite: Store) -> None:
    at = f"/api/connections/{INBOX}"
    shown = read(lite, at=at)
    assert (shown["applies"], shown["source"], shown["slots"]) == (False, None, [])
    refused = put(lite, at=at)
    assert refused.status_code == 409
    assert refused.json()["detail"] == connection_routes.NO_SERVICE
    assert lite.client.post(f"{at}/check", headers=lite.as_(ORG_A, "admin")).status_code == 409
    for bad in ("/api/connections/Mendari/x", "/api/connections/a/b%20c"):
        assert lite.client.get(bad, headers=lite.as_(ORG_A, "admin")).status_code == 404
    assert rows(lite.db) == []


def test_the_token_is_in_no_answer_no_log_line_and_no_chained_payload(
    site: Store,
    service: Service,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recognizable token is set, used, kept through a change, refused, checked, replaced and
    cleared; then it is searched for in every answer of every route called, every line logged or
    printed, and every text the database holds. It reaches one place: the service it was set
    for, as its bearer token."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, service.origin)
    admin, viewer = site.as_(ORG_A, "admin"), site.as_(ORG_A, "viewer")
    answers: list[Any] = []

    def call(method: str, url: str, headers: dict[str, str] = admin, **options: Any) -> Any:
        answer = site.client.request(method, url, headers=headers, **options)
        answers.append(answer)
        return answer

    made = call("PUT", AT, json={"endpoint": service.url, "token": TOKEN})
    assert made.status_code == 200, made.text
    call("GET", AT)
    call("GET", AT, viewer)
    call("GET", "/api/connections")
    call("GET", "/api/connections", viewer)
    checked = call("POST", f"{AT}/check")
    assert checked.json()["ok"] is True, checked.text
    decided = decide(site)
    assert decided["outcome"] == "AUTO", decided["reasons"]
    call("GET", f"/api/audit/{decided['id']}", site.as_(ORG_A, "reviewer"))
    # A refusal that carries the token: an address that may not be called, a token that is not one.
    assert (
        call("PUT", AT, json={"endpoint": "https://10.0.0.5/x", "token": TOKEN}).status_code == 422
    )
    assert call("PUT", AT, json={"endpoint": service.url, "token": TOKEN + " x"}).status_code == 422
    assert (
        call("PUT", AT, json={"endpoint": service.url, "token": TOKEN, "x": [TOKEN]}).status_code
        == 200
    )
    # The service refuses, in words of its own; then answers what is not an answer.
    service.status = 401
    refused = call("POST", f"{AT}/check").json()
    assert (refused["ok"], refused["checked"][0]["problem"]) == (False, "credential_unavailable")
    service.status, service.answer = 200, {"answers": TOKEN}
    assert call("POST", f"{AT}/check").json()["checked"][0]["problem"] == "invalid_response"
    service.answer = None
    kept = call("PUT", AT, json={"endpoint": service.url})  # the token left out: it stays
    assert kept.json()["connection"]["token"] == "set"
    assert decide(site)["outcome"] == "AUTO"
    call("PUT", AT, json={"endpoint": service.url, "token": "another-token"})
    assert call("DELETE", AT).status_code == 204
    for url in (
        "/api/audit/administrative",
        "/api/audit/administrative/export/fhir",
        "/api/audit/export/fhir",
        "/api/health",
        "/api/usage",
        "/api/auth/me",
        "/openapi.json",
        "/api/agents",
        f"/api/agents/{DEMO}",
    ):
        assert call("GET", url).status_code == 200, url

    assert set(service.authorizations()) == {f"Bearer {TOKEN}"}  # the one place it went
    assert len(service.seen) == 5
    printed = capsys.readouterr()
    for name, text in (
        ("an answer", "\n".join(a.text + json.dumps(dict(a.headers)) for a in answers)),
        ("the log", caplog.text + "\n".join(str(record.__dict__) for record in caplog.records)),
        ("what was printed", printed.out + printed.err),
    ):
        assert TOKEN not in text, name
        assert Service.SAID not in text, name  # nor a word of what the service answered
    with site.db._db.session(exclusive=False) as db:
        tables = [
            "audit_events", "agent_connections", "decisions", "decision_inputs", "notes",
            "usage_events", "security_marks",
        ]  # fmt: skip
        held = {
            table: json.dumps(db.all(f"SELECT * FROM {table}"), default=str)
            for table in tables
            if db.columns(table)
        }
    assert set(held) >= {"audit_events", "agent_connections", "decisions"}
    for table, text in held.items():
        assert TOKEN not in text and "another-token" not in text, table
    kinds = [kind for kind, _ in events(site.db)]
    assert kinds.count("agent_connection.set") == 3 and kinds[-1] == "agent_connection.cleared"
    for _, payload in events(site.db):
        assert set(payload) <= {"org", "pack_id", "origin", "token", "changed_by"}
        assert payload.get("origin") in (None, service.origin)  # never the path
    assert site.db.verify()["ok"]


def test_where_a_decisions_call_goes_for_each_organization(
    lite: Store,
    service: Service,  # noqa: F811
    outside: Outside,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolution order through a running server: canned examples (a person decides); the
    deployment's own service for every organization; an organization's own connection before
    it, for that organization alone; and a connection that cannot be read fails the decision
    without going anywhere else."""
    canned = decide(lite)
    assert canned["outcome"] == "HUMAN_REVIEW"
    assert any("canned answers" in reason for reason in canned["reasons"])
    assert read(lite)["source"] == "none"

    monkeypatch.setenv(ENDPOINT_ENV, service.url)  # the deployment's own, on this machine
    monkeypatch.setenv(TOKEN_ENV, "deployment-token")
    assert decide(lite)["outcome"] == "AUTO"
    assert (read(lite)["source"], read(lite)["deployment_endpoint"]) == ("deployment", True)
    assert service.authorizations() == ["Bearer deployment-token"] and outside.seen == []

    assert put(lite, token=TOKEN).status_code == 200  # the organization's own, set in the console
    assert read(lite)["source"] == "organization"
    assert decide(lite)["outcome"] == "AUTO"
    assert outside.authorizations() == [f"Bearer {TOKEN}"]
    assert [str(request.url) for request in outside.seen] == [ELSEWHERE]
    assert len(service.seen) == 1  # the deployment's was not called
    assert decide(lite, ORG_B)["outcome"] == "AUTO"  # another organization: the deployment's
    assert read(lite, org=ORG_B)["source"] == "deployment"
    assert len(service.seen) == 2 and len(outside.seen) == 1

    # Its token no longer opens (its row changed past the store): the decision fails closed.
    assert put(lite, token=TOKEN + "-2").status_code == 200
    with lite.db._db.session(exclusive=True) as db:
        row = db.one("SELECT id, org, pack_id FROM agent_connections WHERE org = ?", (ORG_A,))
        forged = lite.db.phi.seal("connection", TOKEN, "another-row", row["org"], row["pack_id"])
        db.all("UPDATE agent_connections SET token = ? WHERE id = ?", (forged, row["id"]))
    failed = decide(lite)
    assert failed["outcome"] != "AUTO"
    assert "credential_unavailable" in json.dumps(failed["errors"])
    assert len(service.seen) == 2 and len(outside.seen) == 1  # nowhere else
    unread = read(lite)
    assert unread["source"] == "organization"
    assert unread["slots"][0]["problem"] == "credential_unavailable"
    check = lite.client.post(f"{AT}/check", headers=lite.as_(ORG_A, "admin")).json()
    assert (check["ok"], check["checked"][0]["problem"]) == (False, "unreadable")

    assert lite.client.delete(AT, headers=lite.as_(ORG_A, "admin")).status_code == 204
    assert decide(lite)["outcome"] == "AUTO" and len(service.seen) == 3  # the deployment's again


def test_the_check_sends_the_agents_own_example_and_says_what_came_back_in_fixed_words(
    lite: Store, outside: Outside
) -> None:
    admin = lite.as_(ORG_A, "admin")

    def check() -> dict[str, Any]:
        answer = lite.client.post(f"{AT}/check", headers=admin)
        assert answer.status_code == 200, answer.text
        return dict(answer.json())

    nothing = check()  # no connection: it says so, and calls nothing
    assert nothing["ok"] is False and outside.seen == []
    [slot] = nothing["checked"]
    assert (slot["slot"], slot["source"], slot["problem"]) == ("reader", "none", "canned")
    assert slot["said"] == connection_routes.SAID["canned"]

    assert put(lite, token=TOKEN).status_code == 200
    passed = check()
    assert passed["ok"] is True and passed["pack_id"] == DEMO
    assert passed["example"] == "a refill request goes to the refill queue"
    [slot] = passed["checked"]
    assert (slot["ok"], slot["source"], slot["said"]) == (
        True,
        "organization",
        connection_routes.SAID["ok"],
    )
    assert (slot["model"], slot["agent_version"]) == ("local-test-agent", "9.9.9")
    assert isinstance(slot["latency_ms"], int)
    # What was sent: the agent's first behaviour test, a synthetic example it carries.
    [request] = outside.seen
    sent = json.loads(request.content)
    assert sent["input"] == {
        "message": {"body": "Could you refill my lisinopril? I am running low."}
    }
    assert request.headers["authorization"] == f"Bearer {TOKEN}"

    for status, problem in (
        (401, "credential_unavailable"),
        (403, "credential_unavailable"),
        (500, "provider_unavailable"),
        (429, "rate_limited"),
    ):
        outside.status = status
        found = check()
        [slot] = found["checked"]
        assert (found["ok"], slot["problem"]) == (False, problem), status
        assert slot["said"] == connection_routes.SAID[problem]
        assert Service.SAID not in json.dumps(found) and TOKEN not in json.dumps(found)
    # Nothing is recorded: no decision, no evaluation, no event but the two the settings made.
    with lite.db._db.session(exclusive=False) as db:
        assert db.one("SELECT COUNT(*) AS n FROM decisions")["n"] == 0
    assert [kind for kind, _ in events(lite.db)] == ["agent_connection.set"]


def test_a_check_is_counted_against_each_persons_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: Outside
) -> None:
    """A check makes this server send a request: it counts per person against the `label`
    class (`server/limits.py`). Setting a connection is a person's pace, chained, with no
    class."""
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,label=1/min:1")
    for site in serving_the_agent(str(tmp_path / "audit.db"), tmp_path, monkeypatch):
        admin = site.as_(ORG_A, "admin")
        assert put(site, token=TOKEN).status_code == 200
        assert site.client.post(f"{AT}/check", headers=admin).status_code == 200
        assert site.client.post(f"{AT}/check", headers=admin).status_code == 429
        assert len(outside.seen) == 1
        assert put(site, token=TOKEN + "-2").status_code == 200
        # Another administrator has an allowance of their own.
        second = member(site.db, org=ORG_A, roles=["admin"], sub="second-admin")
        assert site.client.post(f"{AT}/check", headers=second).status_code == 200
        assert len(outside.seen) == 2


def test_with_the_switch_off_nothing_is_set_and_what_was_set_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: Outside
) -> None:
    """`MENDARI_AGENT_CONNECTIONS=off`: the reads still say where each agent's answers come
    from; every write is refused; and a connection set before the switch is neither shown nor
    called."""
    target = str(tmp_path / "audit.db")
    with encrypted_store(target) as store:
        ConnectionStore(store).set(ORG_A, DEMO, endpoint=ELSEWHERE, token=TOKEN, by="ada")
    monkeypatch.setenv(SWITCH_ENV, "off")
    for site in serving_the_agent(target, tmp_path, monkeypatch):
        shown = read(site)
        assert (shown["enabled"], shown["may_set"]) == (False, False)
        assert (shown["source"], shown["connection"]) == ("none", None)
        assert "agents.example.org" not in json.dumps(shown)
        admin = site.as_(ORG_A, "admin")
        for refused in (put(site, token=TOKEN), site.client.delete(AT, headers=admin)):
            assert refused.status_code == 409
            assert refused.json()["detail"] == connection_routes.OFF
        canned = decide(site)
        assert canned["outcome"] == "HUMAN_REVIEW" and outside.seen == []
        check = site.client.post(f"{AT}/check", headers=admin).json()
        assert check["checked"][0]["problem"] == "canned" and outside.seen == []
        assert [row["endpoint"] for row in rows(site.db)] == [ELSEWHERE]  # kept, unused
    monkeypatch.setenv(SWITCH_ENV, "sometimes")
    with pytest.raises(MendariError, match=SWITCH_ENV):
        create_app(db_path=tmp_path / "bad.db")


def test_a_connection_set_on_one_instance_is_used_by_another_on_the_same_database(
    site: Store, db_target: str, outside: Outside
) -> None:
    """On SQLite and Postgres: a second instance, told nothing but the database, calls the
    connection the first one saved."""
    assert put(site, token=TOKEN).status_code == 200
    with TestClient(create_app(db_path=db_target), base_url=HTTPS, headers=DASHBOARD) as another:
        shown = another.get(AT, headers=site.as_(ORG_A, "admin")).json()
        assert shown["source"] == "organization"
        assert shown["connection"]["endpoint"] == ELSEWHERE
        decided = another.post(
            f"/api/agents/{DEMO}/decide", json=REFILL, headers=site.as_(ORG_A, "reviewer")
        )
        assert decided.status_code == 200 and decided.json()["outcome"] == "AUTO"
    assert outside.authorizations() == [f"Bearer {TOKEN}"]


# -- an origin the deployment's list does not name, allowed by an operator ------------------------

LIST = "https://listed.example.org"
ORIGIN = "https://agents.example.org"
NOT_PUBLIC = (
    "https://10.0.0.5/decide",
    "https://169.254.169.254/latest/meta-data",
    "https://localhost/decide",
    "https://intranet/decide",
    "https://agents.internal.example.org/decide",  # a name that leads to a private address
    "http://127.0.0.1:8000/decide",
)


@pytest.fixture
def operated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """The server of `lite`, operated by clinic-a (its administrators hold `operate`), with an
    allow-list that names one origin and not the agent's service: a site install's state."""
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", ORG_A)
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, LIST)
    yield from serving_the_agent(str(tmp_path / "audit.db"), tmp_path, monkeypatch)


def test_an_origin_off_the_list_is_admitted_only_when_an_operator_allows_it(
    monkeypatch: pytest.MonkeyPatch, looked_up: dict[str, list[str]]
) -> None:
    """`admitted`, every branch with a list: on it (the deployment's word, inside or not); off
    it and refused; off it and allowed now, or before for this same origin: public only."""
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, f"{LIST},https://10.0.0.9")
    looked_up["agents.internal.example.org"] = ["10.1.2.3"]
    assert admitted(f"{LIST}/decide")[2] == LISTED
    assert admitted("https://10.0.0.9/decide", allow=True)[2] == LISTED  # listed: not "allowed"
    with pytest.raises(ConnectionRefused) as refused:
        admitted(ELSEWHERE)
    assert (refused.value.status, refused.value.code) == (422, "origin_not_listed")
    assert ORIGIN in str(refused.value) and "operates this deployment" in str(refused.value)
    assert admitted(ELSEWHERE, allow=True) == (ELSEWHERE, ORIGIN, ALLOWED_BY_AN_OPERATOR)
    assert admitted(ELSEWHERE, allowed_origin=ORIGIN)[2] == ALLOWED_BY_AN_OPERATOR
    # What was allowed is one origin: another is as unlisted as before.
    with pytest.raises(ConnectionRefused) as other:
        admitted("https://other.example.org/decide", allowed_origin=ORIGIN)
    assert other.value.code == "origin_not_listed"
    with pytest.raises(ConnectionRefused):
        admitted("https://agents.example.org:8443/decide", allowed_origin=ORIGIN)  # another port
    # An operator's word never reaches inside: the same refusals as with no list at all.
    for inside in NOT_PUBLIC:
        with pytest.raises(ConnectionRefused) as now:
            admitted(inside, allow=True)
        with pytest.raises(ConnectionRefused) as before:
            admitted(inside, allowed_origin=origin_of(inside))
        for kept_out in (now.value, before.value):
            assert kept_out.code is None and "10.1.2.3" not in str(kept_out)
    # A list that cannot be read accepts nothing, an operator's word or not.
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, "not a list of origins")
    with pytest.raises(ConnectionRefused) as unread:
        admitted(ELSEWHERE, allow=True)
    assert unread.value.status == 409


def test_what_an_operator_allowed_is_kept_with_its_origin_and_chained(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On SQLite and Postgres, the store: the allowance is on the row and in the chain; it
    holds while the connection stays at that origin, for whoever changes it; another origin,
    or a cleared connection, has lost it; an origin the list names needs none."""
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, LIST)
    with encrypted_store(db_target) as store:
        connections = ConnectionStore(store)
        with pytest.raises(ConnectionRefused) as refused:
            connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, token=TOKEN, by="bob")
        assert refused.value.code == "origin_not_listed"
        assert rows(store) == [] and events(store) == []
        made = connections.set(
            ORG_A, DEMO, endpoint=ELSEWHERE, token=TOKEN, by="ada", allow_origin=True
        )
        assert made["origin_allowed_by"] == "ada" and made["origin_allowed_at"] == made["set_at"]
        [row] = rows(store)
        assert (row["allowed_origin"], row["allowed_by"]) == (ORIGIN, "ada")
        assert events(store) == [
            (
                "agent_connection.origin_allowed",
                {"org": ORG_A, "pack_id": DEMO, "origin": ORIGIN, "allowed_by": "ada"},
            ),
            (
                "agent_connection.set",
                {
                    "org": ORG_A,
                    "pack_id": DEMO,
                    "origin": ORIGIN,
                    "token": True,
                    "changed_by": "ada",
                },
            ),
        ]
        assert connections.resolve(ORG_A, DEMO) == Connection(ELSEWHERE, TOKEN, allowed=True)
        # Another organization's connection to the same origin was allowed by nobody.
        with pytest.raises(ConnectionRefused):
            connections.set(ORG_B, DEMO, endpoint=ELSEWHERE, by="eve")
        # Someone who is not an operator changes the path and the token: the same origin, so
        # what was allowed still holds, as who allowed it and when, and is not chained again.
        moved = connections.set(ORG_A, DEMO, endpoint=f"{ORIGIN}/v2/decide", token=TOKEN, by="bob")
        assert (moved["origin_allowed_by"], moved["origin_allowed_at"]) == (
            "ada",
            made["origin_allowed_at"],
        )
        assert [kind for kind, _ in events(store)].count("agent_connection.origin_allowed") == 1
        # Another origin: refused to them, the row as it was; an operator allows that one anew.
        with pytest.raises(ConnectionRefused) as elsewhere:
            connections.set(ORG_A, DEMO, endpoint="https://other.example.org/decide", by="bob")
        assert elsewhere.value.code == "origin_not_listed"
        assert connections.get(ORG_A, DEMO) == moved
        other = connections.set(
            ORG_A, DEMO, endpoint="https://other.example.org/decide", by="cy", allow_origin=True
        )
        assert other["origin_allowed_by"] == "cy"
        assert events(store)[-2] == (
            "agent_connection.origin_allowed",
            {
                "org": ORG_A,
                "pack_id": DEMO,
                "origin": "https://other.example.org",
                "allowed_by": "cy",
            },
        )
        # The first origin is no longer allowed: going back needs an operator again.
        with pytest.raises(ConnectionRefused):
            connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, by="bob")
        # Cleared: the allowance goes with the connection.
        connections.clear(ORG_A, DEMO, by="bob")
        [emptied] = rows(store)
        assert (emptied["allowed_origin"], emptied["allowed_by"], emptied["allowed_at"]) == (
            None,
            None,
            None,
        )
        with pytest.raises(ConnectionRefused) as after:
            connections.set(ORG_A, DEMO, endpoint="https://other.example.org/decide", by="bob")
        assert after.value.code == "origin_not_listed"
        # An origin the list names needs nobody's allowance, and none is recorded for it.
        listed = connections.set(
            ORG_A, DEMO, endpoint=f"{LIST}/decide", by="bob", allow_origin=True
        )
        assert listed["origin_allowed_by"] is None and rows(store)[0]["allowed_origin"] is None
        assert connections.resolve(ORG_A, DEMO) == Connection(f"{LIST}/decide", None)
        assert [kind for kind, _ in events(store)].count("agent_connection.origin_allowed") == 2
        assert store.verify()["ok"]
        said = [item["event"] for item in store.iter_administrative_events(ORG_A)]
        assert said.count("agent_connection.origin_allowed") == 2


def test_once_the_list_names_an_allowed_origin_the_list_speaks_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, LIST)
    with encrypted_store(str(tmp_path / "audit.db")) as store:
        connections = ConnectionStore(store)
        connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, by="ada", allow_origin=True)
        monkeypatch.setenv(ALLOWED_ORIGINS_ENV, f"{LIST},{ORIGIN}")
        again = connections.set(ORG_A, DEMO, endpoint=ELSEWHERE, by="bob")
        assert again["origin_allowed_by"] is None and rows(store)[0]["allowed_origin"] is None
        # And with no list at all, nothing is allowed because nothing needs to be.
        monkeypatch.delenv(ALLOWED_ORIGINS_ENV)
        free = connections.set(
            ORG_A, DEMO, endpoint="https://1.1.1.1/decide", by="ada", allow_origin=True
        )
        assert free["origin_allowed_by"] is None
        assert [kind for kind, _ in events(store)].count("agent_connection.origin_allowed") == 1


def test_an_allowed_origin_is_called_at_public_addresses_only(
    agent: Pack, monkeypatch: pytest.MonkeyPatch, outside: Outside
) -> None:
    """The provider, with a list that does not name the connection's origin: allowed, it goes
    out through the client that opens public addresses only, its host checked before each
    call; not allowed, it is refused; a listed origin is the deployment's own."""
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, LIST)
    provider = make_agent_provider(agent)  # no client of its own: as a server builds it
    theirs = {DEMO: Connection(ELSEWHERE, TOKEN, allowed=True)}
    provider.use_connection(theirs.get)
    assert provider.live() == Live(ELSEWHERE, "organization", TOKEN, True)
    provider.preflight()
    assert provider.answer(message("refill")).canned is None
    assert [str(request.url) for request in outside.seen] == [ELSEWHERE]
    assert outside.authorizations() == [f"Bearer {TOKEN}"]
    for inside in ("https://10.0.0.5/decide", "https://localhost/decide", "https://intranet/x"):
        theirs[DEMO] = Connection(inside, TOKEN, allowed=True)  # a row changed past the checks
        with pytest.raises(ProviderError) as refused:
            provider.answer(message("refill"))
        assert refused.value.code == "invalid_endpoint"
        with pytest.raises(ProviderError):
            provider.preflight()
    theirs[DEMO] = Connection(ELSEWHERE, TOKEN)  # the same address, allowed by nobody
    with pytest.raises(ProviderError) as off_list:
        provider.answer(message("refill"))
    assert off_list.value.code == "invalid_endpoint" and "allow-list" in str(off_list.value.detail)
    assert len(outside.seen) == 1
    # A list that cannot be read refuses every call, allowed or not.
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, "not a list of origins")
    theirs[DEMO] = Connection(ELSEWHERE, TOKEN, allowed=True)
    with pytest.raises(ProviderError) as unread:
        provider.answer(message("refill"))
    assert unread.value.code == "invalid_endpoint" and len(outside.seen) == 1


def test_a_listed_origin_is_called_as_the_deployments_own(
    agent: Pack, monkeypatch: pytest.MonkeyPatch, outside: Outside
) -> None:
    """An origin on the list goes through the deployment's shared client, not the public-only
    one, whether or not anything was allowed for it: the list is the operator's word."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json=agent_response())

    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, LIST)
    provider = make_agent_provider(agent, handler)
    provider.use_connection({DEMO: Connection(f"{LIST}/decide", TOKEN, allowed=True)}.get)
    assert provider.answer(message("refill")).canned is None
    assert sent == [f"{LIST}/decide"] and outside.seen == []


def test_an_operator_allows_an_unlisted_origin_from_the_console(
    operated: Store, outside: Outside, capsys: pytest.CaptureFixture[str]
) -> None:
    """The site install's path, through the API: the list names nothing useful, and an
    administrator of the operating organization connects the agent's service without a file."""
    before = read(operated)
    assert before["allowed_origins"] == [LIST] and before["may_allow_origin"] is True
    capsys.readouterr()
    plain = put(operated, token=TOKEN)  # not asked for: refused, and told what would allow it
    assert plain.status_code == 422, plain.text
    assert ORIGIN in plain.json()["detail"] and "operates this deployment" in plain.json()["detail"]
    assert rows(operated.db) == [] and events(operated.db) == []
    made = put(operated, token=TOKEN, allow_origin=True)
    assert made.status_code == 200, made.text
    shown = made.json()["connection"]
    assert shown["origin_allowed_by"] == shown["set_by"] and shown["set_by"].endswith(
        "admin-clinic-a"
    )
    assert shown["origin_allowed_at"] == shown["set_at"]
    assert [kind for kind, _ in events(operated.db)] == [
        "agent_connection.origin_allowed",
        "agent_connection.set",
    ]
    lines = [
        (line["event"], line.get("reason"))
        for line in security_lines(capsys.readouterr().out)
        if line["event"].startswith("admin.connection")
    ]
    assert lines == [
        ("admin.connection.refused", "origin_not_listed"),
        ("admin.connection.origin_allowed", None),
        ("admin.connection.set", None),
    ]
    # Every reader sees who allowed it and when; the list itself is as it was.
    seen = read(operated, who="viewer")
    assert seen["connection"]["origin_allowed_by"] == shown["origin_allowed_by"]
    assert seen["allowed_origins"] == [LIST] and seen["may_allow_origin"] is False
    # A decision calls the service, with its token, through the public-only client.
    assert decide(operated)["outcome"] == "AUTO"
    assert [str(request.url) for request in outside.seen] == [ELSEWHERE]
    assert outside.authorizations() == [f"Bearer {TOKEN}"]
    checked = operated.client.post(f"{AT}/check", headers=operated.as_(ORG_A, "admin"))
    assert checked.status_code == 200 and checked.json()["ok"] is True
    # Changed within the origin, with no word about allowing: still allowed, chained once.
    moved = put(operated, endpoint=f"{ORIGIN}/v2/decide")
    assert moved.status_code == 200, moved.text
    assert moved.json()["connection"]["origin_allowed_at"] == shown["origin_allowed_at"]
    kinds = [kind for kind, _ in events(operated.db)]
    assert kinds.count("agent_connection.origin_allowed") == 1
    assert operated.db.verify()["ok"]
    listed = operated.client.get(
        "/api/audit/administrative", headers=operated.as_(ORG_A, "admin")
    ).json()["items"]
    assert "agent_connection.origin_allowed" in {item["event"] for item in listed}


@pytest.mark.parametrize("inside", NOT_PUBLIC)
def test_an_operator_cannot_allow_an_address_inside(
    operated: Store, looked_up: dict[str, list[str]], inside: str
) -> None:
    looked_up["agents.internal.example.org"] = ["10.1.2.3"]
    refused = put(operated, endpoint=inside, token=TOKEN, allow_origin=True)
    assert refused.status_code == 422, refused.text
    assert "10.1.2.3" not in refused.text and TOKEN not in refused.text
    assert rows(operated.db) == [] and events(operated.db) == []


def test_an_administrator_who_does_not_operate_the_deployment_stays_bound_by_the_list(
    operated: Store, outside: Outside, capsys: pytest.CaptureFixture[str]
) -> None:
    """Another organization's administrator: `allow_origin` is refused whole, and without it
    the list refuses the origin as before. What the operating organization allowed for its own
    connection allows nothing for theirs."""
    assert put(operated, token=TOKEN, allow_origin=True).status_code == 200
    theirs = read(operated, org=ORG_B)
    assert theirs["may_set"] is True and theirs["may_allow_origin"] is False
    capsys.readouterr()
    asked = put(operated, org=ORG_B, token=TOKEN, allow_origin=True)
    assert asked.status_code == 403, asked.text
    assert "operates this deployment" in asked.json()["detail"]
    plain = put(operated, org=ORG_B, token=TOKEN)
    assert plain.status_code == 422 and ALLOWED_ORIGINS_ENV in plain.json()["detail"]
    assert [row["org"] for row in rows(operated.db)] == [ORG_A]
    reasons = [
        line["reason"]
        for line in security_lines(capsys.readouterr().out)
        if line["event"] == "admin.connection.refused"
    ]
    assert reasons == ["not_operator", "origin_not_listed"]
    # An origin the list names is theirs to set, as before.
    assert put(operated, org=ORG_B, endpoint=f"{LIST}/decide").status_code == 200
    assert read(operated, org=ORG_B)["connection"]["origin_allowed_by"] is None
    assert decide(operated, org=ORG_A)["outcome"] == "AUTO" and len(outside.seen) == 1


def test_where_nobody_operates_the_deployment_through_the_web_nobody_allows_an_origin(
    lite: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memberships mode with no operator organization named: no administrator holds `operate`,
    so the list binds every one of them."""
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, LIST)
    seen = read(lite)
    assert seen["may_allow_origin"] is False and seen["operator_named"] is False
    asked = put(lite, token=TOKEN, allow_origin=True)
    assert asked.status_code == 403
    assert asked.json()["detail"] == connection_routes.NO_OPERATOR_NAMED  # what to set
    plain = put(lite, token=TOKEN)
    assert plain.status_code == 422
    assert plain.json()["detail"] == NOT_LISTED_NOBODY.format(origin=origin_of(ELSEWHERE))
    assert rows(lite.db) == [] and events(lite.db) == []


def test_without_sign_in_nobody_allows_an_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: Outside
) -> None:
    """Open mode holds `operate` as it holds everything, but nobody there is a person who
    signed in: an origin off the list stays refused, and whoever runs the server lists it."""
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, LIST)
    target = tmp_path / "open.db"
    with TestClient(create_app(db_path=target)) as client, AuditStore(target) as store:
        assert client.get(AT).json()["may_allow_origin"] is False
        asked = client.put(AT, json={"endpoint": ELSEWHERE, "allow_origin": True})
        assert asked.status_code == 403, asked.text
        assert asked.json()["detail"] == connection_routes.NOT_AN_OPERATOR  # nobody signs in
        assert client.put(AT, json={"endpoint": ELSEWHERE}).status_code == 422
        assert rows(store) == [] and events(store) == []
        assert client.put(AT, json={"endpoint": f"{LIST}/decide"}).status_code == 200


def test_with_no_list_an_operators_word_allows_nothing_because_nothing_needs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", ORG_A)
    for site in serving_the_agent(str(tmp_path / "audit.db"), tmp_path, monkeypatch):
        assert read(site)["allowed_origins"] is None and read(site)["may_allow_origin"] is False
        made = put(site, token=TOKEN, allow_origin=True)
        assert made.status_code == 200, made.text
        assert made.json()["connection"]["origin_allowed_by"] is None
        assert [kind for kind, _ in events(site.db)] == ["agent_connection.set"]


def test_a_database_from_before_allowances_gains_their_columns_and_keeps_its_rows(
    db_target: str,
) -> None:
    """On SQLite and Postgres: a connection kept before an origin could be allowed is read as
    one nobody allowed, after the start that adds the columns; a second start changes nothing."""
    with encrypted_store(db_target) as store:
        made = ConnectionStore(store).set(ORG_A, DEMO, endpoint=ELSEWHERE, token=TOKEN, by="ada")
        with store._db.session(exclusive=True) as db:
            for column in ("allowed_origin", "allowed_by", "allowed_at"):
                db.all(f"ALTER TABLE agent_connections DROP COLUMN {column}")
            assert "allowed_origin" not in db.columns("agent_connections")
    for _ in range(2):
        with encrypted_store(db_target) as store:
            with store._db.session(exclusive=False) as db:
                assert {"allowed_origin", "allowed_by", "allowed_at"} <= db.columns(
                    "agent_connections"
                )
            assert ConnectionStore(store).get(ORG_A, DEMO) == made
            assert ConnectionStore(store).resolve(ORG_A, DEMO) == Connection(ELSEWHERE, TOKEN)
            assert store.verify()["ok"]


def test_no_machines_credential_allows_an_origin_even_in_the_operating_organization(
    operated: Store,
) -> None:
    """An API key made by an operator holds neither `keys` nor `operate`: the guard refuses it
    before the route, with the word about allowing or without."""
    admin = operated.as_(ORG_A, "admin")
    for role in ("service", "viewer"):
        key = operated.client.post(
            "/api/auth/keys", json={"role": role, "name": role}, headers=admin
        ).json()["key"]
        machine = {"authorization": f"Bearer {key}"}
        asked = operated.client.put(
            AT, json={"endpoint": ELSEWHERE, "allow_origin": True}, headers=machine
        )
        assert asked.status_code == 403, asked.text
        assert operated.client.get(AT, headers=machine).json()["may_allow_origin"] is False
    assert rows(operated.db) == [] and events(operated.db) == []


def test_in_claims_mode_nobody_allows_an_origin_until_the_deployment_names_its_operator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outside: Outside,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sign-in by claims: every administrator holds `operate` while no organization is named
    (ADR-0022), and none of them may allow an origin. It fails closed: the refusal says what to
    set, nothing is saved or chained, and once the deployment names the organization that
    operates it, that one's administrators may and no other's."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, LIST)
    monkeypatch.delenv("MENDARI_OPERATOR_ORG", raising=False)
    encrypt_mode(monkeypatch)
    target = str(tmp_path / "claims.db")
    body = {"endpoint": ELSEWHERE, "allow_origin": True}
    with (
        TestClient(create_app(db_path=target), base_url=HTTPS, headers=DASHBOARD) as client,
        encrypted_store(target) as store,
    ):
        north = signed_in(store, role="admin", org="north")
        # An administrator here operates the deployment: its System view answers them.
        assert client.get("/api/system/checks", headers=north).status_code == 200
        seen = client.get(AT, headers=north).json()
        assert seen["may_set"] is True and seen["may_allow_origin"] is False
        assert seen["operator_named"] is False
        capsys.readouterr()
        asked = client.put(AT, json=body, headers=north)
        assert asked.status_code == 403, asked.text
        assert asked.json()["detail"] == connection_routes.NO_OPERATOR_NAMED
        assert "MENDARI_OPERATOR_ORG" in connection_routes.NO_OPERATOR_NAMED  # it says what to set
        assert "MENDARI_PROVIDER_ALLOWED_ORIGINS" in connection_routes.NO_OPERATOR_NAMED
        assert client.put(AT, json={"endpoint": ELSEWHERE}, headers=north).status_code == 422
        assert rows(store) == [] and events(store) == [] and outside.seen == []
        # An address on the list is set as it always was.
        listed = client.put(AT, json={"endpoint": f"{LIST}/decide"}, headers=north)
        assert listed.status_code == 200, listed.text
        assert client.delete(AT, headers=north).status_code == 204
    reasons = [
        line["reason"]
        for line in security_lines(capsys.readouterr().out)
        if line["event"] == "admin.connection.refused"
    ]
    assert reasons == ["no_operator_named", "origin_not_listed"]
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", "north")
    with (
        TestClient(create_app(db_path=target), base_url=HTTPS, headers=DASHBOARD) as client,
        encrypted_store(target) as store,
    ):
        north = signed_in(store, role="admin", org="north")
        seen = client.get(AT, headers=north).json()
        assert seen["may_allow_origin"] is True and seen["operator_named"] is True
        before = len(events(store))
        assert client.put(AT, json=body, headers=north).status_code == 200
        assert [kind for kind, _ in events(store)][before:] == [
            "agent_connection.origin_allowed",
            "agent_connection.set",
        ]
        south = signed_in(store, role="admin", org="south")
        assert client.get(AT, headers=south).json()["may_allow_origin"] is False
        refused = client.put(AT, json=body, headers=south)
        assert (
            refused.status_code == 403
            and refused.json()["detail"] == connection_routes.NOT_AN_OPERATOR
        )
        assert client.put(AT, json={"endpoint": ELSEWHERE}, headers=south).status_code == 422
        assert [row["org"] for row in rows(store)] == ["north"]
