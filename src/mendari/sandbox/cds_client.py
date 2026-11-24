# SPDX-License-Identifier: Apache-2.0
"""A signed CDS Hooks client for the sandbox (LP-23e): `mendari sandbox cds-client` plays the EHR
that calls Mendari's CDS services over HTTP with its own signed tokens, as CDS Hooks 2.0
describes ("Trusting CDS Clients") and an EHR such as Epic does.

- At start it makes an EC P-384 key in memory. The private key is never written anywhere and
  dies with the process. It serves the public JWK Set at `<iss>/jwks.json` (`--listen`), for
  the engine to fetch as it would an EHR's.
- It prints the registry entry the engine needs for it (`MENDARI_CDS_CLIENTS`).
- It finds what the FHIR server it names holds for the agent to decide (`sandbox_calls`): the
  synthetic cases `mendari sandbox seed` loaded, by the sandbox tag and the agent's FHIR focus.
  The agent is the bundled demo agent unless another is named.
- It reads discovery, then calls the agent's service once for each case found (`HookCall`).
  Each call carries a new ES384 token: `iss` its own URL, `aud` the engine's public URL and the
  path, five minutes, a new `jti`, and `jku` its JWK Set's URL in the header. A request names
  the FHIR server (`fhirServer`) and sends no prefetch, so the engine reads the record there
  through its allow-list. Its context names the patient and what the service's prefetch asks
  for by id; an order hook's `draftOrders` are the patient's orders, in draft.
- It exits 1 on any answer other than 200, a hook answer that is not CDS Hooks' (a `cards`
  list, each card with a summary, an indicator and a source), or a card saying part of the
  record could not be read (LP-23d's ABSTAIN) for anything but what the call says may be left
  unread (`HookCall.unread`: nothing, for the sandbox's own cases). Any other key or reason
  (`not_fetched`, `http_status`, `not_r4`, ...) is a read of the FHIR server that failed or was
  not made.

A new key each run: an engine fetches a client's keys again at most once a minute, so a second
run within the minute is refused (`cds_key`) until that minute has passed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from mendari.core.pack import Pack
from mendari.errors import MendariError
from mendari.sandbox.code_systems import SANDBOX_TAG
from mendari.sandbox.smart_launch import focus_search
from mendari.standards.cds_hooks import ALSO_NOTED, ORDER_HOOKS, UNREADABLE_SUMMARY, service_id

ALGORITHM = "ES384"
LIFETIME = 300  # seconds a token lives, as CDS Hooks 2.0 advises at most
TITLE = "Mendari sandbox CDS client"
INDICATORS = ("info", "warning", "critical")
USER = "Practitioner/sandbox-clinician"  # who the sandbox's calls say is at the EHR
_JWKS_PATH = "/jwks.json"
_TAG = f"{SANDBOX_TAG['system']}|{SANDBOX_TAG['code']}"
# A prefetch template that reads one resource by an id the hook's context gives:
# `Communication/{{context.communicationId}}`.
_READ_BY_ID = re.compile(r"^(\w+)/\{\{\s*context\.(\w+)\s*\}\}$")


class Answer(Protocol):
    """An HTTP answer, as the client reads one."""

    @property
    def status_code(self) -> int: ...

    def json(self) -> Any: ...


class Http(Protocol):
    """What the client calls the engine and reads the FHIR server with: an `httpx.Client`, or any
    client with its `get` and `post` (a test's in-process engine)."""

    def get(self, url: str, *, headers: Mapping[str, str]) -> Answer: ...

    def post(self, url: str, *, json: Any, headers: Mapping[str, str]) -> Answer: ...


@dataclass(frozen=True)
class HookCall:
    """One call the client makes: a CDS service's hook, for one patient."""

    service: str  # the CDS service's id (`<namespace>.<name>`)
    patient: str  # the patient's id on the FHIR server
    # The hook's context beside the patient: the user, what its prefetch reads by id, the drafts.
    context: Mapping[str, Any] = field(default_factory=dict)
    hooks: tuple[str, ...] = ()  # the hooks discovery may list for the service; empty: any
    # What a card may say it could not read, as (key, reason), and how a problem says so.
    unread: frozenset[tuple[str, str]] = frozenset()
    unread_rule: str = "nothing of a sandbox case may be unread"


class SandboxClient:
    """The client: its key, its JWK Set server, and the calls it makes. Use it as a context
    manager, so the server stops when the client is done."""

    def __init__(self, *, listen: tuple[str, int] = ("127.0.0.1", 8300), iss: str | None = None):
        self._key = ec.generate_private_key(ec.SECP384R1())
        public: dict[str, Any] = json.loads(
            jwt.algorithms.ECAlgorithm.to_jwk(self._key.public_key())
        )
        members = {name: public[name] for name in ("crv", "kty", "x", "y")}
        self.kid = _thumbprint(members)
        self.jwk = {**members, "kid": self.kid, "use": "sig", "alg": ALGORITHM}
        self.fetches = 0  # how often the engine has fetched the JWK Set
        body = json.dumps({"keys": [self.jwk]}).encode()
        host = str(listen[0])
        server = _IPv6Server if ":" in host else ThreadingHTTPServer
        self._server = server((host, listen[1]), _handler(body, self._fetched))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        port = self._server.server_address[1]
        named = f"[{host}]" if ":" in host else host
        self.iss = (iss or f"http://{named}:{port}").rstrip("/")
        self.jwks_uri = f"{self.iss}{_JWKS_PATH}"

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _fetched(self) -> None:
        self.fetches += 1

    def entry(self, *, client_id: str, org: str, fhir_server: str) -> dict[str, Any]:
        """Its `MENDARI_CDS_CLIENTS` entry: no key material, only where its keys are."""
        return {
            "id": client_id,
            "title": TITLE,
            "iss": self.iss,
            "org": org,
            "fhir_servers": [fhir_server],
            "jwks_uri": self.jwks_uri,
        }

    def token(self, audience: str) -> str:
        """A token for one call to `audience` (the engine's public URL and the path)."""
        now = int(time.time())
        claims = {
            "iss": self.iss,
            "aud": audience,
            "exp": now + LIFETIME,
            "iat": now,
            "jti": uuid.uuid4().hex,
        }
        headers = {"kid": self.kid, "typ": "JWT", "jku": self.jwks_uri}
        return jwt.encode(claims, self._key, algorithm=ALGORITHM, headers=headers)

    def run(
        self,
        *,
        engine: str,
        fhir: str,
        calls: Sequence[HookCall],
        audience: str | None = None,
        http: Http | None = None,
    ) -> dict[str, Any]:
        """Discovery, then each of `calls` at `engine`, with `fhir` as the FHIR server; what
        each answered, and the problems found (none: it passed). `audience` is the engine's
        `MENDARI_PUBLIC_URL` (default: `engine`)."""
        engine, fhir = engine.rstrip("/"), fhir.rstrip("/")
        public = (audience or engine).rstrip("/")
        result: dict[str, Any] = {"discovery": None, "calls": [], "jwks_fetches": 0, "problems": []}
        problems: list[str] = result["problems"]
        with _client(http) as caller:
            path = "/cds-services"
            listed = caller.get(f"{engine}{path}", headers=self._bearer(f"{public}{path}"))
            services = _services(listed) if listed.status_code == 200 else {}
            result["discovery"] = {"status": listed.status_code, "services": sorted(services)}
            if listed.status_code != 200:
                problems.append(_refused(f"discovery answered {listed.status_code}", listed))
            unlisted: set[str] = set()
            for call in calls if listed.status_code == 200 else ():
                hook = services.get(call.service)
                if hook is None or (call.hooks and hook not in call.hooks):
                    if call.service not in unlisted:
                        wanted = " or ".join(call.hooks) if call.hooks else "a"
                        problems.append(f"discovery lists no {wanted} hook for {call.service}")
                    unlisted.add(call.service)
                    continue
                route = f"{path}/{call.service}"
                body = hook_request(hook, call, fhir)
                answer = caller.post(
                    f"{engine}{route}", json=body, headers=self._bearer(f"{public}{route}")
                )
                result["calls"].append(_called(call, hook, answer, problems))
        result["jwks_fetches"] = self.fetches
        return result

    def _bearer(self, audience: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token(audience)}"}


def sandbox_calls(
    agent: Pack, fhir: str, *, limit: int = 5, http: Http | None = None
) -> list[HookCall]:
    """One call of the agent's CDS service for each of up to `limit` of the sandbox's cases
    for it on the FHIR server: the tagged resources its FHIR mapping focuses on, as `mendari
    sandbox seed` loads them. The context names the patient, the user, and the case's resource
    under whichever name the service's prefetch reads it by id; an order hook also gets it as
    the draft order."""
    hooks = agent.manifest.cds_hooks
    if hooks is None:
        raise MendariError(f"{agent.id} declares no CDS Hooks service (`cds_hooks`)")
    kind, narrowed = focus_search(agent)
    query = urlencode({"_tag": _TAG, **narrowed, "_count": str(limit)})
    with _client(http) as reader:
        found = reader.get(
            f"{fhir.rstrip('/')}/{kind}?{query}",
            headers={"accept": "application/fhir+json", "cache-control": "no-cache"},
        )
    if found.status_code != 200:
        raise MendariError(f"the FHIR server answered {found.status_code} for its {kind}s")
    entries = found.json().get("entry") or []
    cases = [entry["resource"] for entry in entries if isinstance(entry.get("resource"), dict)]
    cases = [case for case in cases if case.get("resourceType") == kind][:limit]
    if not cases:
        raise MendariError(
            f"no synthetic {kind} for {agent.id} in {fhir}: run `mendari sandbox seed` first"
        )
    # The context names under which the service's prefetch reads the case's resource by id.
    reads = [_READ_BY_ID.match(template) for template in hooks.prefetch.values()]
    names = [read[2] for read in reads if read is not None and read[1] == kind]
    calls = []
    for case in cases:
        context: dict[str, Any] = {"userId": USER, **dict.fromkeys(names, case.get("id"))}
        if hooks.hook in ORDER_HOOKS:
            draft = {"resource": {**case, "status": "draft"}}
            context["draftOrders"] = {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [draft],
            }
        subject = str(case.get("subject", {}).get("reference", ""))
        patient = str(case.get("id")) if kind == "Patient" else subject.rsplit("/", 1)[-1]
        calls.append(HookCall(service_id(agent), patient, context))
    return calls


def hook_request(hook: str, call: HookCall, fhir: str) -> dict[str, Any]:
    """A CDS Hooks request for one call, as an EHR sends it with no prefetch: the context (the
    patient and what the call adds) and the FHIR server to read the record from."""
    return {
        "hook": hook,
        "hookInstance": str(uuid.uuid4()),
        "fhirServer": fhir,
        "context": {"patientId": call.patient, **call.context},
    }


def unread_of(card: Mapping[str, Any]) -> set[tuple[str, str]] | None:
    """What an unreadable ABSTAIN's card says could not be read: each `- <key>: <reason>` line of
    its detail (the agent's key names and a closed set of reasons, nothing of the record), up to
    what it also notes (`ALSO_NOTED`). None for any other card."""
    if card.get("summary") != UNREADABLE_SUMMARY:
        return None
    detail = card.get("detail")
    unread = set()
    for line in detail.splitlines() if isinstance(detail, str) else ():
        if line == ALSO_NOTED:
            break  # a list read without a source the EHR does not offer: not what was unread
        key, colon, reason = line.removeprefix("- ").partition(": ")
        if line.startswith("- ") and colon:
            unread.add((key.strip(), reason.strip()))
    return unread


def problems_of(answer: Any) -> list[str]:
    """What is wrong with a hook's answer, as CDS Hooks 2.0 has one: an object with a `cards`
    list, each card with a summary, one of the three indicators, and a source with a label."""
    if not isinstance(answer, dict) or not isinstance(answer.get("cards"), list):
        return ["the answer has no cards list"]
    found = []
    for number, card in enumerate(answer["cards"], start=1):
        if not isinstance(card, dict):
            found.append(f"card {number} is not an object")
            continue
        if not isinstance(card.get("summary"), str) or not card["summary"]:
            found.append(f"card {number} has no summary")
        if card.get("indicator") not in INDICATORS:
            found.append(f"card {number} has no indicator")
        source = card.get("source")
        if not isinstance(source, dict) or not source.get("label"):
            found.append(f"card {number} has no source label")
    return found


def _called(call: HookCall, hook: str, answer: Answer, problems: list[str]) -> dict[str, Any]:
    """One hook call as the result shows it, its problems added to `problems`."""
    called: dict[str, Any] = {
        "service": call.service,
        "hook": hook,
        "patient": call.patient,
        "status": answer.status_code,
    }
    where = f"{call.service} for {call.patient}"
    if answer.status_code != 200:
        problems.append(_refused(f"{where} answered {answer.status_code}", answer))
        return called
    try:
        body = answer.json()
    except ValueError:
        problems.append(f"{where}: the answer is not JSON")
        return called
    problems += [f"{where}: {problem}" for problem in problems_of(body)]
    listed = body.get("cards") if isinstance(body, dict) else None
    cards = [card for card in listed or [] if isinstance(card, dict)]
    called["cards"] = [
        {"indicator": card.get("indicator"), "summary": card.get("summary")} for card in cards
    ]
    for number, card in enumerate(cards, start=1):
        unread = unread_of(card)
        if unread is not None and not unread:
            problems.append(
                f"{where}: card {number} says part of the record was not read, not which"
            )
        for key, reason in sorted((unread or set()) - call.unread):
            problems.append(f"{where}: could not read {key}: {reason} ({call.unread_rule})")
    return called


def _refused(problem: str, answer: Answer) -> str:
    if answer.status_code == 401:
        return (
            f"{problem}: the engine did not take the token (its security log says why; a client "
            "run again within a minute has a key the engine has not fetched yet)"
        )
    return problem


def _services(listed: Answer) -> dict[str, str]:
    try:
        services = listed.json().get("services")
    except (ValueError, AttributeError):
        return {}
    if not isinstance(services, list):
        return {}
    return {
        str(s["id"]): str(s.get("hook"))
        for s in services
        if isinstance(s, dict) and isinstance(s.get("id"), str)
    }


class _client:
    """The given httpx client, or a new one closed after use."""

    def __init__(self, given: Http | None):
        self.given = given
        self.made: httpx.Client | None = None

    def __enter__(self) -> Http:
        if self.given is not None:
            return self.given
        self.made = httpx.Client(timeout=60)
        return self.made

    def __exit__(self, *_: object) -> None:
        if self.made is not None:
            self.made.close()


class _IPv6Server(ThreadingHTTPServer):
    """The JWK Set server on an IPv6 address (`--listen [::1]:8300`)."""

    address_family = socket.AF_INET6


def _handler(body: bytes, fetched: Callable[[], None]) -> type[BaseHTTPRequestHandler]:
    """The JWK Set server's requests: `GET /jwks.json`, its public key; anything else 404."""

    class JwksHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the name http.server calls
            if self.path != _JWKS_PATH:
                self.send_error(404)
                return
            fetched()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # the result counts the fetches (`jwks_fetches`)

    return JwksHandler


def _thumbprint(members: dict[str, str]) -> str:
    """RFC 7638's JWK thumbprint of an EC public key: the key's own id."""
    canonical = json.dumps(
        {name: members[name] for name in ("crv", "kty", "x", "y")}, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
