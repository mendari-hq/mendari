# SPDX-License-Identifier: Apache-2.0
"""Signed CDS client tokens (LP-23b, ADR-0014): the registry of CDS clients, their keys, and the
verifier that makes an EHR's own CDS Hooks token a `cds` principal (`server/auth.py`).

**Off unless a deployment sets `MENDARI_CDS_CLIENTS`**: then no JWT is accepted anywhere, and
sign-in behaves as before. Set, it is a JSON list checked at startup (`cds_clients`), as
`MENDARI_SMART_EHRS` is. Each entry is one CDS client (an EHR, or a sandbox):

- `id` (lower-case letters, digits and hyphens: the principal is `cds:<id>`, the chain's name
  for it `cds <id>`), `title`, the `iss` its tokens carry, the organization (`org`) its calls act
  for, and the `fhirServer` bases (`fhir_servers`) it may send: each a FHIR base on
  `MENDARI_FHIR_ALLOWED_SERVERS`, and, when `MENDARI_SMART_EHRS` registers the same base, to the
  same organization.
- Its keys, one way or the other: `jwks`, a public JWK Set by value (checked as SMART Backend
  Services checks a client's: public keys only, RSA of 2048 to 8192 bits or EC P-384, distinct
  `kid`s), or `jwks_uri`, one URL (https, or plain http for a loopback or single-label host)
  whose origin is on `MENDARI_FHIR_ALLOWED_SERVERS`, so one allow-list governs every call
  Mendari makes to an EHR.
- Optionally `sub` and `tenant`, which its tokens must then carry exactly, `iss_is_fhir_server`
  (the `fhirServer` it sends must then be its `iss`), and `origins` (for CORS, LP-23c).

With sign-in off every organization must be `default`, and `MENDARI_PUBLIC_URL` must be set: a
token's `aud` is that URL plus the path called. A refusal at startup names the setting, the
entry's position and the field, never a value or key material.

**A token** (`verify`), checked in this order, each step before anything that costs more:
its size (8 KiB at most) and shape; its header (only `alg`, `kid`, `typ` and `jku`; `alg` RS384
or ES384, `typ` `JWT`, a `kid`); its `iss`, a registered client (the one lookup an unsigned
token causes, in memory); `jku`, which must equal the registered `jwks_uri` exactly (a client
whose keys are given by value takes none); exactly one of the client's keys for the `kid` and
key type; the signature; `aud` exactly the URL called; `exp`, `iat` and `jti` required, 30 s of
skew, `iat` not ahead, at most 330 s between `iat` and `exp` and from now to `exp`; `sub` and
`tenant` as registered (a `tenant` refused when none is); then, last, the `jti` is claimed once
per client until the token expires, so a refused token claims nothing. Each refusal is one
reason of `REASONS`.

**Keys from a `jwks_uri`** (`JwksCache`) are fetched only through `GuardedHttp`, checked as
keys by value are, and kept an hour. A `kid` the kept keys do not hold fetches them again at
most once per client a minute; an answer that is not a set of public signing keys is refused,
and what was kept for that client is dropped. Each failed fetch is one `cds.jwks.refused` line
in the security log, with the client's id and a fixed reason.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import jwt

from mendari.core.audit import AuditStore
from mendari.errors import ClientError, FhirError, MendariError
from mendari.server import security_log
from mendari.server.auth import DEFAULT_ORG, CdsRefused
from mendari.server.backend_services import ALGORITHMS, MAX_ASSERTION_BYTES, MAX_JTI, check_jwks
from mendari.server.origin import origin_of as browser_origin
from mendari.standards.fhir_client import GuardedHttp, allowed_servers, origin_of
from mendari.standards.smart import Registration, normalize_base

ENV = "MENDARI_CDS_CLIENTS"
SKEW = 30  # seconds of clock skew tolerated on exp, iat and nbf
MAX_LIFETIME = 300  # Inferno's tokens live 300 s; with the skew, 330 s at most
KEYS_SECONDS = 3600  # keys from a jwks_uri are kept this long, as the OIDC discovery is
REFETCH_SECONDS = 60  # an unknown kid fetches a client's keys again at most this often
FETCH_WAIT = 15.0  # how long a request waits for another's fetch of the keys (each step 10 s)
MAX_CLIENTS = 50
MAX_BASES = 20  # fhir_servers per client
MAX_ORIGINS = 20  # origins per client
HEADER_FIELDS = frozenset({"alg", "kid", "typ", "jku"})  # x5u, x5c, jwk, crit, b64, … refused
# Every reason a token is refused for (`CdsRefused.reason`); the security log names each one
# `cds_<reason>`. `server/auth.py` adds `unregistered` (no registry), `headers` (more than one
# Authorization header), `no_organization_record` (memberships mode) and `only` (a `cds`
# principal off `/cds-services`), and the hook route `fhir_server_not_registered`.
REASONS = (
    "size",
    "format",
    "header",
    "issuer",
    "jku",
    "keys_unavailable",
    "key",
    "signature",
    "audience",
    "expired",
    "not_yet_valid",
    "issued_at",
    "lifetime",
    "claims",
    "subject",
    "tenant",
    "jti",
    "replayed",
)
FIELDS = (
    "id",
    "title",
    "iss",
    "org",
    "fhir_servers",
    "origins",
    "jwks",
    "jwks_uri",
    "sub",
    "tenant",
    "iss_is_fhir_server",
)
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_VISIBLE = re.compile(r"[\x21-\x7e]+")  # visible ASCII, no space
_KID = re.compile(r"[\x21-\x7e]{1,256}")
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")  # base64url without padding (RFC 7515)
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]*")  # empty for `alg: none`, refused by the header


@dataclass(frozen=True)
class CdsClient:
    """One registered CDS client: who its tokens say they are (`iss`), the organization its calls
    act for, the FHIR bases it may send (normalized), and its keys, by value (`jwks`, public
    members only) or by one pinned `jwks_uri`."""

    id: str
    title: str
    iss: str
    org: str
    fhir_servers: tuple[str, ...]
    origins: tuple[str, ...] = ()
    jwks_uri: str | None = None
    jwks: tuple[dict[str, Any], ...] | None = field(default=None, hash=False)
    sub: str | None = None
    tenant: str | None = None
    iss_is_fhir_server: bool = False


@dataclass(frozen=True)
class CdsCaller:
    """What a verified token says: the registered client that signed it."""

    client: CdsClient


def fhir_server_allowed(client: CdsClient, server: object) -> bool:
    """Whether a hook request's `fhirServer` is one this client registered (and, with
    `iss_is_fhir_server`, its `iss`). None or empty: nothing can be fetched, so nothing to
    refuse."""
    if not server:
        return True
    if not isinstance(server, str):
        return False
    base = _base(server)
    if base is None or base not in client.fhir_servers:
        return False
    return not client.iss_is_fhir_server or base == _base(client.iss)


def _base(url: str) -> str | None:
    """`smart.normalize_base`, None too for a URL it cannot even split (`http://[`)."""
    try:
        return normalize_base(url)
    except ValueError:
        return None


# -- keys -----------------------------------------------------------------------------------------


class JwksCache:
    """Each client's keys: by value as registered, or fetched from its `jwks_uri` through the
    allow-listed client and kept `ttl` seconds. A `kid` the kept keys do not hold, or kept keys
    that are stale, fetch them again at most once per client every `refetch` seconds, so random
    `kid`s cannot make Mendari call an EHR more often than that. One fetch per client at a time:
    a request that finds one under way while the client's keys are fresh does not wait for it;
    without fresh keys it waits for that fetch, never starting another."""

    def __init__(
        self,
        *,
        allowed: Collection[str],
        ttl: float = KEYS_SECONDS,
        refetch: float = REFETCH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.allowed = frozenset(allowed)
        self.ttl = ttl
        self.refetch = refetch
        self._clock = clock
        self._kept: dict[str, tuple[float, tuple[dict[str, Any], ...]]] = {}
        self._tried: dict[str, float] = {}
        self._running: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def keys(self, client: CdsClient, kid: str) -> Sequence[Mapping[str, Any]]:
        """The keys to look for `kid` in; `CdsRefused("keys_unavailable")` when there are none to
        look in (the first fetch failed, or was refused)."""
        if client.jwks is not None:
            return client.jwks
        with self._lock:
            now = self._clock()
            kept = self._fresh(client.id, now)
            if kept is not None and any(key["kid"] == kid for key in kept):
                return kept
            running = self._running.get(client.id)
            if running is None:
                tried = self._tried.get(client.id)
                if tried is not None and now - tried < self.refetch:
                    if kept is not None:
                        return kept
                    raise CdsRefused("keys_unavailable", client.id)
                self._tried[client.id] = now
                running = self._running[client.id] = threading.Event()
                fetching = True
            elif kept is not None:
                return kept  # another request fetches them: these are fresh, so do not wait
            else:
                fetching = False
        if not fetching:
            running.wait(FETCH_WAIT)
            with self._lock:
                kept = self._fresh(client.id, self._clock())
            if kept is None:
                raise CdsRefused("keys_unavailable", client.id)
            return kept
        try:
            return self._fetch(client)
        finally:
            with self._lock:
                self._running.pop(client.id, None)
            running.set()

    def _fresh(self, client_id: str, now: float) -> tuple[dict[str, Any], ...] | None:
        kept = self._kept.get(client_id)
        return kept[1] if kept is not None and now - kept[0] < self.ttl else None

    def _fetch(self, client: CdsClient) -> tuple[dict[str, Any], ...]:
        assert client.jwks_uri is not None
        actor = f"cds:{client.id}"
        try:
            with GuardedHttp(allowed=self.allowed) as http:
                answer = http.get_json(client.jwks_uri, {"accept": "application/json"})
            fetched = tuple(check_jwks(answer))
        except FhirError:  # unreachable, not JSON, too large, redirected, or off the list
            _jwks_refused("unreachable", actor)
            with self._lock:
                kept = self._fresh(client.id, self._clock())
            if kept is None:
                raise CdsRefused("keys_unavailable", client.id) from None
            return kept  # what was kept still holds until it is stale
        except ClientError:  # a private or symmetric key, a weak one, too many: none is kept
            with self._lock:
                self._kept.pop(client.id, None)
            _jwks_refused("invalid_keys", actor)
            raise CdsRefused("keys_unavailable", client.id) from None
        with self._lock:
            self._kept[client.id] = (self._clock(), fetched)
        return fetched


def _jwks_refused(reason: str, actor: str) -> None:
    """One `cds.jwks.refused` line: `unreachable` (the fetch failed) or `invalid_keys` (the
    answer is not a set of public signing keys), with the client's `cds:<id>`."""
    security_log.event("cds.jwks.refused", outcome="refused", reason=reason, actor=actor)


# -- the verifier ---------------------------------------------------------------------------------


def verify(
    token: str,
    *,
    audience: str,
    now: float,
    clients: Mapping[str, CdsClient],
    keys: Callable[[CdsClient, str], Sequence[Mapping[str, Any]]],
    claim: Callable[[str, int], bool],
) -> CdsCaller:
    """The registered client a CDS Hooks token is from, or `CdsRefused`. `clients` are keyed by
    `iss`; `keys` gives a client's keys to look for a `kid` in; `claim(value, until)` claims a
    `jti` once (`AuditStore.smart_claim`), and runs last, so nothing a refused token sends is
    written. Nothing here logs: the caller does, with the reason."""
    if not 1 <= len(token) <= MAX_ASSERTION_BYTES:
        raise CdsRefused("size")
    parts = token.split(".")
    if len(parts) != 3 or not all(_SEGMENT.fullmatch(part) for part in parts[:2]):
        raise CdsRefused("format")
    if not _SIGNATURE.fullmatch(parts[2]):
        raise CdsRefused("format")
    header, unverified = _json_segment(parts[0]), _json_segment(parts[1])
    if header is None or unverified is None:
        raise CdsRefused("format")
    alg, kid = header.get("alg"), header.get("kid")
    if (
        not set(header) <= HEADER_FIELDS
        or not isinstance(alg, str)
        or alg not in ALGORITHMS
        or header.get("typ") != "JWT"
        or not isinstance(kid, str)
        or not _KID.fullmatch(kid)
    ):
        raise CdsRefused("header")
    issuer = unverified.get("iss")
    client = clients.get(issuer) if isinstance(issuer, str) else None
    if client is None:
        raise CdsRefused("issuer")
    if "jku" in header and (client.jwks_uri is None or header["jku"] != client.jwks_uri):
        raise CdsRefused("jku", client.id)
    found = keys(client, kid)
    matching = [key for key in found if key.get("kid") == kid and key.get("kty") == ALGORITHMS[alg]]
    if len(matching) != 1:
        raise CdsRefused("key", client.id)
    try:
        public = jwt.PyJWK(dict(matching[0]), algorithm=alg).key
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, binascii.Error):
        raise CdsRefused("key", client.id) from None
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            public,
            algorithms=[alg],
            audience=audience,
            issuer=client.iss,
            leeway=SKEW,
            options={"require": ["iss", "aud", "exp", "iat", "jti"], "strict_aud": True},
        )
    except jwt.InvalidSignatureError:
        raise CdsRefused("signature", client.id) from None
    except jwt.InvalidAudienceError:
        raise CdsRefused("audience", client.id) from None
    except jwt.ExpiredSignatureError:
        raise CdsRefused("expired", client.id) from None
    except jwt.ImmatureSignatureError:
        raise CdsRefused("not_yet_valid", client.id) from None
    except jwt.InvalidIssuedAtError:
        raise CdsRefused("issued_at", client.id) from None
    except (jwt.PyJWTError, ValueError, TypeError):
        raise CdsRefused("claims", client.id) from None
    issued, expires = claims["iat"], claims["exp"]
    if not _number(issued) or not _number(expires):
        raise CdsRefused("claims", client.id)
    if expires <= now - SKEW:
        raise CdsRefused("expired", client.id)
    if issued > now + SKEW:
        raise CdsRefused("issued_at", client.id)
    longest = MAX_LIFETIME + SKEW
    if expires - issued > longest or expires > now + longest:
        raise CdsRefused("lifetime", client.id)
    if client.sub is not None and claims.get("sub") != client.sub:
        raise CdsRefused("subject", client.id)
    if client.tenant is None:
        if "tenant" in claims:  # an EHR shared by several organizations: not supported
            raise CdsRefused("tenant", client.id)
    elif claims.get("tenant") != client.tenant:
        raise CdsRefused("tenant", client.id)
    jti = claims["jti"]
    if not isinstance(jti, str) or not 1 <= len(jti) <= MAX_JTI:
        raise CdsRefused("jti", client.id)
    # Last, the one write a token may cause: claimed until it expires, once per client.
    until = max(int(expires) + SKEW, int(time.time()) + 1)
    if not claim(f"{client.id}\n{jti}", until):
        raise CdsRefused("replayed", client.id)
    return CdsCaller(client)


def _json_segment(segment: str) -> dict[str, Any] | None:
    """A JWS segment as a JSON object (unverified), or None."""
    try:
        value = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, binascii.Error, UnicodeDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _number(value: object) -> bool:
    """A NumericDate: a finite JSON number, not a boolean or a string."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value)


# -- the registry ---------------------------------------------------------------------------------


class CdsRegistry:
    """The registered CDS clients, by `iss` and by id, the public URL their tokens are addressed
    to, and their keys."""

    def __init__(
        self,
        *,
        public_url: str,
        clients: Sequence[CdsClient],
        allowed: Collection[str] = (),
        keys: JwksCache | None = None,
    ):
        self.public_url = public_url.rstrip("/")
        self.clients = tuple(clients)
        self.by_iss = {client.iss: client for client in self.clients}
        self.by_id = {client.id: client for client in self.clients}
        self.keys = keys or JwksCache(allowed=allowed)

    def audience(self, path: str) -> str:
        """What a token for `path` must carry as `aud`: the public URL and the path, never the
        `Host` header, which a caller chooses."""
        return f"{self.public_url}{path}"

    def client(self, client_id: str) -> CdsClient | None:
        return self.by_id.get(client_id)

    def ids(self, org: str | None) -> list[str]:
        """The clients' ids, of `org` only (None: every organization's, open mode)."""
        return sorted(c.id for c in self.clients if org is None or c.org == org)

    def verify(self, token: str, *, path: str, store: AuditStore) -> CdsCaller:
        """`verify` against this registry, for a request to `path`, its `jti` claimed in
        `store` (the session table's `cds-jti` claims, which expire and are never ended)."""
        return verify(
            token,
            audience=self.audience(path),
            now=time.time(),
            clients=self.by_iss,
            keys=self.keys.keys,
            claim=lambda value, until: store.smart_claim("cds-jti", value, until),
        )


def cds_clients(
    *, signed_in: bool, ehrs: Mapping[str, Registration], allowed: Collection[str] | None = None
) -> CdsRegistry | None:
    """`MENDARI_CDS_CLIENTS`, checked: None when it is unset (no JWT is accepted anywhere).
    Anything wrong in it stops the server here, at startup, with a message that names the entry
    and the field, never a value."""
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return None
    servers = allowed_servers() if allowed is None else frozenset(allowed)
    try:
        entries = json.loads(raw)
    except (ValueError, RecursionError):
        raise MendariError(f"{ENV} is not JSON: a list of clients") from None
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_CLIENTS:
        raise MendariError(f"{ENV} must be a JSON list of 1 to {MAX_CLIENTS} clients")
    public_url = _public_url()
    clients: list[CdsClient] = []
    for number, entry in enumerate(entries, start=1):
        client = _client(number, entry, servers=servers, signed_in=signed_in, ehrs=ehrs)
        if any(other.id == client.id for other in clients):
            raise MendariError(f"{ENV} entry {number}: its id is another entry's too")
        if any(other.iss == client.iss for other in clients):
            raise MendariError(f"{ENV} entry {number}: its iss is another entry's too")
        clients.append(client)
    return CdsRegistry(public_url=public_url, clients=clients, allowed=servers)


def _public_url() -> str:
    public = os.environ.get("MENDARI_PUBLIC_URL", "").strip().rstrip("/")
    if not public:
        raise MendariError(
            f"{ENV} needs MENDARI_PUBLIC_URL: a CDS client's token is addressed (aud) to it and "
            "the path it calls"
        )
    parts = urlsplit(public)
    local = (parts.hostname or "") in ("localhost", "127.0.0.1")
    if (
        not parts.netloc
        or parts.query
        or parts.fragment
        or not (parts.scheme == "https" or (parts.scheme == "http" and local))
    ):
        raise MendariError("MENDARI_PUBLIC_URL must be an https URL without a query or fragment")
    return public


def _client(
    number: int,
    entry: object,
    *,
    servers: frozenset[str],
    signed_in: bool,
    ehrs: Mapping[str, Registration],
) -> CdsClient:
    where = f"{ENV} entry {number}"

    def refused(problem: str) -> MendariError:
        return MendariError(f"{where}: {problem}")

    if not isinstance(entry, Mapping):
        raise MendariError(f"{where} is not an object")
    if not set(entry) <= set(FIELDS):
        raise refused(f"a field is not one of {', '.join(FIELDS)}")
    client_id = entry.get("id")
    if not isinstance(client_id, str) or not _ID.fullmatch(client_id):
        raise refused("id must be 1 to 63 lower-case letters, digits and hyphens")
    title = entry.get("title")
    if not isinstance(title, str) or not 1 <= len(title) <= 100 or not title.isprintable():
        raise refused("title must be 1 to 100 printable characters")
    iss = _visible(entry, "iss", 1024, refused)
    if iss is None:
        raise refused("iss is required: what the client's tokens carry as iss")
    org = _visible(entry, "org", 100, refused)
    if org is None:
        raise refused("org is required: the organization its calls act for")
    if not signed_in and org != DEFAULT_ORG:
        raise refused(f"with sign-in off, org must be {DEFAULT_ORG}")
    bases = _list(entry, "fhir_servers", MAX_BASES, refused, required=True)
    fhir_servers: list[str] = []
    for position, given in enumerate(bases, start=1):
        base = _base(given)
        if base is None:
            raise refused(f"fhir_servers {position} is not a FHIR base (http or https, no query)")
        if origin_of(base) not in servers:
            raise refused(f"fhir_servers {position} is not on MENDARI_FHIR_ALLOWED_SERVERS")
        if base in fhir_servers:
            raise refused(f"fhir_servers {position} is given twice")
        registered = ehrs.get(base)
        if registered is not None and registered.org != org:
            raise refused(
                f"fhir_servers {position} is registered in MENDARI_SMART_EHRS to another "
                "organization"
            )
        fhir_servers.append(base)
    origins: list[str] = []
    for position, given in enumerate(_list(entry, "origins", MAX_ORIGINS, refused), start=1):
        origin = browser_origin(given)  # None for anything urlsplit refuses, too
        extra = "?" in given or "#" in given
        if origin is None or extra or urlsplit(given.strip()).path not in ("", "/"):
            raise refused(f"origins {position} is not an origin (https://host[:port])")
        if origin in origins:
            raise refused(f"origins {position} is given twice")
        origins.append(origin)
    jwks_uri, jwks = entry.get("jwks_uri"), entry.get("jwks")
    if (jwks_uri is None) == (jwks is None):
        raise refused("give its keys as jwks (by value) or as jwks_uri, one of the two")
    keys: tuple[dict[str, Any], ...] | None = None
    if jwks is not None:
        try:
            keys = tuple(check_jwks(jwks))
        except ClientError as problem:  # names the key by its position, never its material
            raise refused(f"jwks: {problem}") from None
    else:
        jwks_uri = _jwks_uri(jwks_uri, servers, refused)
    sub = _visible(entry, "sub", 256, refused)
    tenant = _visible(entry, "tenant", 256, refused)
    pinned = entry.get("iss_is_fhir_server", False)
    if not isinstance(pinned, bool):
        raise refused("iss_is_fhir_server must be true or false")
    if pinned and _base(iss) not in fhir_servers:
        raise refused("iss_is_fhir_server is set, but iss is not one of its fhir_servers")
    return CdsClient(
        id=client_id,
        title=title,
        iss=iss,
        org=org,
        fhir_servers=tuple(fhir_servers),
        origins=tuple(origins),
        jwks_uri=jwks_uri,
        jwks=keys,
        sub=sub,
        tenant=tenant,
        iss_is_fhir_server=pinned,
    )


def _visible(
    entry: Mapping[str, Any], name: str, most: int, refused: Callable[[str], MendariError]
) -> str | None:
    value = entry.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > most or not _VISIBLE.fullmatch(value):
        raise refused(f"{name} must be 1 to {most} visible ASCII characters, without spaces")
    return value


def _list(
    entry: Mapping[str, Any],
    name: str,
    most: int,
    refused: Callable[[str], MendariError],
    *,
    required: bool = False,
) -> list[str]:
    value = entry.get(name)
    if value is None and not required:
        return []
    least = 1 if required else 0
    if (
        not isinstance(value, list)
        or not least <= len(value) <= most
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise refused(f"{name} must be a list of {least} to {most} URLs")
    return value


def _jwks_uri(
    value: object, servers: frozenset[str], refused: Callable[[str], MendariError]
) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or not _VISIBLE.fullmatch(value):
        raise refused("jwks_uri must be a URL")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
    except ValueError:
        raise refused("jwks_uri must be a URL") from None
    if (
        origin_of(value) is None
        or parts.fragment
        or not (parts.scheme == "https" or (parts.scheme == "http" and _local(host)))
    ):
        raise refused(
            "jwks_uri must be https (plain http only for a loopback or single-label host), "
            "without a fragment"
        )
    if origin_of(value) not in servers:
        raise refused("the jwks_uri's origin is not on MENDARI_FHIR_ALLOWED_SERVERS")
    return value


def _local(host: str) -> bool:
    """Plain http is for this machine or a single-label name (a compose service) only, as the
    FHIR allow-list has it (`fhir_client.allowed_servers`)."""
    if host == "localhost" or ("." not in host and ":" not in host):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
