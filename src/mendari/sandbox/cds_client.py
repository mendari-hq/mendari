# SPDX-License-Identifier: Apache-2.0
"""A signed CDS Hooks client for the sandbox (LP-23e): `mendari sandbox cds-client` plays the EHR
that calls Mendari's CDS services over HTTP with its own signed tokens, as CDS Hooks 2.0
describes ("Trusting CDS Clients") and an EHR such as Epic does.

- At start it makes an EC P-384 key in memory. The private key is never written anywhere and
  dies with the process. It serves the public JWK Set at `<iss>/jwks.json` (`--listen`), for
  the engine to fetch as it would an EHR's.
- It prints the registry entry the engine needs for it (`MENDARI_CDS_CLIENTS`).
- It checks that the FHIR server it names holds the EHR profile's patients
  (`sandbox/ehr_profile.py`, `mendari sandbox seed --pack ehr-profile`).
- It reads discovery, then calls each standard hook (`patient-view` and `order-sign`) of each
  service the profile has patients for, once per patient. Each call carries a new ES384 token:
  `iss` its own URL, `aud` the engine's public URL and the path, five minutes, a new `jti`, and
  `jku` its JWK Set's URL in the header. A request names the FHIR server (`fhirServer`) and
  sends no prefetch, so the engine reads the record there through its allow-list. An order
  hook's `draftOrders` are the patient's orders, in draft.
- It exits 1 on any answer other than 200, a hook answer that is not CDS Hooks' (a `cards`
  list, each card with a summary, an indicator and a source), or a card saying part of the
  record could not be read (LP-23d's ABSTAIN) for anything but what the profile leaves unread:
  a patient's note whose Binary is RTF, which no pack reads as text (`notes: unsupported_type`,
  `expected_unread`; since LP-23k the packs read a text/plain or text/html note's Binary). Any
  other key or reason (`not_fetched`, `http_status`, `not_r4`, ...) is a read of the FHIR server
  that failed or was not made, and a patient with no note, or a readable one, has nothing to
  leave unread.

A new key each run: an engine fetches a client's keys again at most once a minute, so a second
run within the minute is refused (`cds_key`) until that minute has passed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Protocol

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from mendari.sandbox.ehr_profile import PACKS, PRESCRIBER_ID, ProfilePatient, patients
from mendari.standards.cds_hooks import ALSO_NOTED, ORDER_HOOKS, UNREADABLE_SUMMARY

STANDARD_HOOKS = ("patient-view", "order-sign")
ALGORITHM = "ES384"
LIFETIME = 300  # seconds a token lives, as CDS Hooks 2.0 advises at most
TITLE = "Mendari sandbox CDS client"
INDICATORS = ("info", "warning", "critical")
_JWKS_PATH = "/jwks.json"
# What a hook may say it could not read on the profile: a note whose linked Binary is RTF, which
# no pack reads as text (the hooks read text/plain and text/html Binaries since LP-23h, and the
# shipped packs read their notes so since LP-23k).
RTF_NOTE = ("notes", "unsupported_type")
UNREADABLE_NOTE_TYPES = ("text/rtf",)


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
        audience: str | None = None,
        today: date | None = None,
        http: Http | None = None,
        fhir_http: Http | None = None,
    ) -> dict[str, Any]:
        """Discovery and every standard hook of the profile's packs at `engine`, with `fhir`
        as the FHIR server; what each answered, and the problems found (none: it passed).
        `audience` is the engine's `MENDARI_PUBLIC_URL` (default: `engine`)."""
        engine, fhir = engine.rstrip("/"), fhir.rstrip("/")
        public = (audience or engine).rstrip("/")
        profile = patients(today)
        result: dict[str, Any] = {"discovery": None, "calls": [], "jwks_fetches": 0, "problems": []}
        problems: list[str] = result["problems"]
        with _client(http) as caller, _client(fhir_http) as reader:
            missing = [p.id for p in profile if not _held(reader, fhir, p)]
            if missing:
                problems.append(
                    f"{fhir} does not hold the EHR profile's patients ({', '.join(missing)}): "
                    "seed it first (mendari sandbox seed --pack ehr-profile)"
                )
                return result
            path = "/cds-services"
            listed = caller.get(f"{engine}{path}", headers=self._bearer(f"{public}{path}"))
            services = _services(listed) if listed.status_code == 200 else {}
            result["discovery"] = {"status": listed.status_code, "services": sorted(services)}
            if listed.status_code != 200:
                problems.append(_refused(f"discovery answered {listed.status_code}", listed))
            for spec in PACKS if listed.status_code == 200 else ():
                service = spec.pack_id.replace("/", ".")
                hook = services.get(service)
                if hook not in STANDARD_HOOKS:
                    problems.append(f"discovery lists no standard hook for {spec.pack_id}")
                    continue
                for patient in (p for p in profile if p.pack_id == spec.pack_id):
                    call = f"{path}/{service}"
                    body = hook_request(hook, patient, fhir)
                    answer = caller.post(
                        f"{engine}{call}", json=body, headers=self._bearer(f"{public}{call}")
                    )
                    result["calls"].append(_called(service, hook, patient, answer, problems))
        result["jwks_fetches"] = self.fetches
        return result

    def _bearer(self, audience: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token(audience)}"}


def hook_request(hook: str, patient: ProfilePatient, fhir: str) -> dict[str, Any]:
    """A CDS Hooks request for one patient, as an EHR sends it with no prefetch: the context
    (the user, the patient and, for an order hook, the patient's orders in draft) and the FHIR
    server to read the record from."""
    context: dict[str, Any] = {"userId": f"Practitioner/{PRESCRIBER_ID}", "patientId": patient.id}
    if hook in ORDER_HOOKS:
        drafts = [{"resource": {**order, "status": "draft"}} for order in patient.orders()]
        context["draftOrders"] = {"resourceType": "Bundle", "type": "collection", "entry": drafts}
    return {
        "hook": hook,
        "hookInstance": str(uuid.uuid4()),
        "fhirServer": fhir,
        "context": context,
    }


def expected_unread(patient: ProfilePatient) -> set[tuple[str, str]]:
    """The keys and reasons a hook may say it could not read for the patient (an unreadable
    ABSTAIN's card): its note, when the Binary it links to is RTF; nothing for a patient with no
    note or a readable one."""
    binaries = {f"Binary/{r['id']}": r for r in patient.resources if r["resourceType"] == "Binary"}
    for document in (r for r in patient.resources if r["resourceType"] == "DocumentReference"):
        for content in document.get("content", []):
            binary = binaries.get(content.get("attachment", {}).get("url"), {})
            if binary.get("contentType") in UNREADABLE_NOTE_TYPES:
                return {RTF_NOTE}
    return set()


def unread_of(card: Mapping[str, Any]) -> set[tuple[str, str]] | None:
    """What an unreadable ABSTAIN's card says could not be read: each `- <key>: <reason>` line of
    its detail (the pack's key names and a closed set of reasons, nothing of the record), up to
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


def _called(
    service: str, hook: str, patient: ProfilePatient, answer: Answer, problems: list[str]
) -> dict[str, Any]:
    """One hook call as the result shows it, its problems added to `problems`."""
    called: dict[str, Any] = {
        "service": service,
        "hook": hook,
        "patient": patient.id,
        "status": answer.status_code,
    }
    where = f"{service} for {patient.id}"
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
    expected = expected_unread(patient)
    for number, card in enumerate(cards, start=1):
        unread = unread_of(card)
        if unread is not None and not unread:
            problems.append(
                f"{where}: card {number} says part of the record was not read, not which"
            )
        for key, reason in sorted((unread or set()) - expected):
            problems.append(
                f"{where}: could not read {key}: {reason} (only an RTF note may be unread)"
            )
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


def _held(reader: Http, fhir: str, patient: ProfilePatient) -> bool:
    """Whether the FHIR server holds the patient (a server that cannot be reached raises)."""
    found = reader.get(
        f"{fhir}/Patient/{patient.id}",
        headers={"accept": "application/fhir+json", "cache-control": "no-cache"},
    )
    return found.status_code == 200


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
