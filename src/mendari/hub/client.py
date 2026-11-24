# SPDX-License-Identifier: Apache-2.0
"""The Hub client: log in, push, pull, search.

`pull` never uses a pack it has not verified:
1. the archive's SHA-256 is the one the registry recorded;
2. every file matches the signed manifest;
3. the signature is by a key of the pack's namespace;
4. the extracted pack loads with the signed digest, as a pack from a registry: on the community
   engine (RE2, the caps, the input limits and a CPU budget per decision) unless it is a copy of
   a bundled agent of this release (ADR-0009). A pack that does not load there is not cached.

Nothing the registry answers chooses where a pack is written: the namespace, name and version
must be a pack id and a semantic version before any cache path is built, the path must stay
inside the cache, and the signed manifest must name that id and version. A compromised registry
can refuse service, never delete or write outside the cache.

A recalled version (a signed `mendari.yank/1` record, `hub/yank.py`) is never the latest: a
pull names it by its exact version, and `recall_of` says what its record says and whether its
signature holds with a key this machine pinned (the namespace's, or the registry's own, pinned
on first use like them). A record that does not verify is still a recall: it only stops
automation, the safe direction.

A registry with a signing key counter-signs each version it accepts (a signed
`mendari.registry-acceptance/1` record, `hub/acceptance.py`), and `pull` checks it before the
archive is fetched. The registry's key is trusted when it is one shipped with this package
(`registry_keys.json`) or the one pinned for that registry on first use. Then a version whose
record is missing, does not verify, or names another pack, version, digest, archive or publisher
key is refused, and nothing is cached; so is a registry that published a key before and now
publishes none, or another. A registry that has never published a key (a local one) is pulled
from as before, and the marker beside the pack records `accepted: null`; unless
`MENDARI_REGISTRY_COUNTERSIGNATURE` is `required` (every pull is counter-signed by a shipped or
pinned key) or `shipped` (by a shipped key only: nothing is trusted on first use).

Namespace keys are pinned on first use (per registry). A pack signed by a key that isn't pinned
is refused, unless the user accepts the new key, the way SSH treats a changed host key. A key the
registry lists as revoked (`revoked_at`) is trusted only for what was signed before then: a
version the registry received before (its `published_at`), or a recall whose signed time is
before; anything later, or with no time, is refused. The registry itself does the same: it takes
nothing from a revoked key and still verifies what the key signed before (ADR-0020).

Pulled packs are cached under `~/.cache/mendari/agents/<namespace>/<name>/<version>`, which is
on the pack search path, so `mendari test`, `Decision.from_hub` and the server find them like
any local pack. Until 2026-11 the folder was `packs/`: a version pulled then is still found
there (`caches`), still recalled and removed there, and written under `agents/` when a pull
has to fetch it again. Credentials and pinned keys live in `~/.config/mendari` (files mode 0600,
written whole and moved in). Pins are filed per registry address, scheme and host in lower
case and without the default port; a pin file that is there and cannot be read is refused,
never read as empty.
`MENDARI_CONFIG_DIR` and `MENDARI_CACHE_DIR` move them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mendari.core.https_only import is_https_or_local
from mendari.core.origin import MARKER, ORIGIN_FORMAT, pulled_record
from mendari.core.pack import Pack, load_pack
from mendari.core.spec import is_pack_id, is_version
from mendari.errors import HubError, MendariError
from mendari.hub import archive

TIMEOUT = 60.0
# What a pull asks of the registry's counter-signature (LP-19i): `optional` (unless set) pulls
# from a registry that has never shown this machine a key; `required` refuses one, so every
# pull is counter-signed by a shipped or pinned key; `shipped` also refuses a key that is not
# one shipped with this package, so a first contact trusts nothing it is merely shown.
COUNTERSIGNATURE_ENV = "MENDARI_REGISTRY_COUNTERSIGNATURE"
COUNTERSIGNATURE_MODES = ("optional", "required", "shipped")


def countersignature_mode() -> str:
    mode = os.environ.get(COUNTERSIGNATURE_ENV, "").strip().lower() or "optional"
    if mode not in COUNTERSIGNATURE_MODES:
        raise HubError(f"{COUNTERSIGNATURE_ENV} is one of {', '.join(COUNTERSIGNATURE_MODES)}")
    return mode


def config_dir() -> Path:
    return Path(os.environ.get("MENDARI_CONFIG_DIR") or Path.home() / ".config" / "mendari")


def cache_dir() -> Path:
    return Path(os.environ.get("MENDARI_CACHE_DIR") or Path.home() / ".cache" / "mendari")


def packs_cache() -> Path:
    """The registry cache: where `pull` writes an agent, `<namespace>/<name>/<version>`."""
    return cache_dir() / "agents"


def former_cache() -> Path:
    """The folder the registry cache had until 2026-11. Nothing is written there any more, and
    what a pull left there is read as before (`caches`): an operator's cache outlives a rename."""
    return cache_dir() / "packs"


def caches() -> tuple[Path, Path]:
    """The registry cache as it is read: its folder, then the one it had before. A version in
    both is the first's (the search path lists them in this order, and the newest of an id is
    the first found of its version)."""
    return packs_cache(), former_cache()


def _read(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError):
        return {}


def _write_private(path: Path, data: dict[str, Any]) -> None:
    """Written whole beside the file (mode 0600), then moved in: a reader never finds half a
    file, and a write that is cut short leaves the old one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # 0600
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def _pins_path() -> Path:
    return config_dir() / "trusted_keys.json"


def _read_pins() -> dict[str, Any]:
    """The pinned keys (`trusted_keys.json`): nothing when the file is not there, and refused
    when it is there and cannot be read as one. A pin file read as empty would make this
    machine trust, as on first use, whatever a registry shows next."""
    path = _pins_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError):
        text = ""
    try:
        pins = json.loads(text)
    except (ValueError, RecursionError):
        pins = None
    if not isinstance(pins, dict) or not all(isinstance(held, dict) for held in pins.values()):
        raise HubError(
            f"the pinned keys in {path} cannot be read, so no registry key is trusted: restore "
            "the file, or remove it to pin every registry's keys again on first use"
        )
    return pins


def _pin_url(url: str) -> str:
    """A registry's address as its pins are filed: scheme and host in lower case, without the
    scheme's default port or a trailing slash, so one registry has one set of pins however its
    address is typed."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        return url
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != {"https": 443, "http": 80}.get(scheme):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path.rstrip("/"), "", ""))


def _pins_of(pins: dict[str, Any], url: str) -> dict[str, Any]:
    """This registry's pins, filed under its address (`_pin_url`). Pins filed before under the
    address as it was typed (another case, a default port) are read with them."""
    filed = _pin_url(url)
    found: dict[str, Any] = {}
    for address in sorted(pins, key=lambda a: a != filed):  # the filed one first: it wins
        if _pin_url(address) != filed:
            continue
        for name, held in pins[address].items():
            if isinstance(held, list):
                found[name] = sorted({*found.get(name, []), *held})
            else:
                found.setdefault(name, held)
    return found


def login(url: str, token: str | None) -> dict[str, Any]:
    """Check the registry answers, then remember it (and the token) as the default."""
    url = url.rstrip("/")
    with RegistryClient(url) as registry:
        health = registry.health()
    credentials = _read(config_dir() / "credentials.json")
    credentials.setdefault("registries", {})[url] = {"token": token} if token else {}
    credentials["default"] = url
    _write_private(config_dir() / "credentials.json", credentials)
    return health


def default_registry() -> tuple[str, str | None]:
    """The registry to use (MENDARI_REGISTRY_URL, else the one logged into) and its token."""
    credentials = _read(config_dir() / "credentials.json")
    configured = os.environ.get("MENDARI_REGISTRY_URL", "").strip().rstrip("/")
    url = configured or credentials.get("default")
    if not url:
        raise HubError("no registry: `mendari login --registry URL` or MENDARI_REGISTRY_URL")
    saved = credentials.get("registries", {}).get(url, {})
    token = os.environ.get("MENDARI_REGISTRY_TOKEN") or saved.get("token")
    return str(url), token


class RegistryClient:
    def __init__(
        self, url: str, token: str | None = None, transport: httpx.BaseTransport | None = None
    ):
        self.url = url.rstrip("/")
        if not is_https_or_local(self.url):
            # Before any request: a token is never sent in clear, nor to an address that
            # carries credentials of its own.
            raise HubError(
                "a registry is reached over https (http only on this machine, and no user or "
                "password in the address): `mendari login --registry https://...` or "
                "MENDARI_REGISTRY_URL"
            )
        headers = {"authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(
            base_url=self.url,
            headers=headers,
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> RegistryClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, **params: Any) -> Any:
        response = self._http.get(
            path, params={k: v for k, v in params.items() if v not in (None, "")}
        )
        return self._json(response)

    def _json(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            # A proxy's or a hostile registry's error page need not be an object.
            detail = body.get("detail") if isinstance(body, dict) else None
            status = response.status_code
            message = f"the registry answered HTTP {status}" + (f": {detail}" if detail else "")
            raise HubError(message, status if status in (404, 409, 410) else 422)
        try:
            return response.json()
        except ValueError:  # a captive portal's or a proxy's page: said so, never a traceback
            raise HubError(
                f"the registry at {_shown_url(self.url)} answered with something that is not "
                "JSON: check the registry's address"
            ) from None

    def health(self) -> dict[str, Any]:
        try:
            return dict(self._get("/v1/health"))
        except httpx.HTTPError as error:
            raise HubError(
                f"could not reach the registry at {self.url} ({type(error).__name__})"
            ) from None

    def namespace(self, namespace: str) -> dict[str, Any]:
        return dict(self._get(f"/v1/namespaces/{namespace}"))

    def version(self, namespace: str, name: str, version: str | None = None) -> dict[str, Any]:
        if version:
            return dict(self._get(f"/v1/agents/{namespace}/{name}/{version}"))
        latest = self._get(f"/v1/agents/{namespace}/{name}").get("latest")
        if not isinstance(latest, dict):
            raise HubError(
                f"every version of {namespace}/{name} is recalled: pull an exact version to "
                "read why",
                404,
            )
        return dict(latest)

    def registry_key(self) -> dict[str, Any] | None:
        """The registry's own public key (which signs the recalls it makes), or None."""
        response = self._http.get("/v1/registry-key")
        if response.status_code == 404:
            return None
        return dict(self._json(response))

    def yank(
        self,
        namespace: str,
        name: str,
        version: str,
        *,
        reason: str,
        severity: str,
        key: Ed25519PrivateKey,
    ) -> dict[str, Any]:
        """Recall a version this namespace published, signed with one of its keys."""
        from mendari.hub import yank

        meta = self.version(namespace, name, version)
        record = yank.sign(
            pack=f"{namespace}/{name}",
            version=version,
            digest=str(meta["digest"]),
            archive_sha256=str(meta["archive_sha256"]),
            reason=reason,
            severity=severity,
            signer="publisher",
            key=key,
            key_id=archive.key_id(key.public_key()),
        )
        response = self._http.post(
            f"/v1/agents/{namespace}/{name}/{version}/yank", json={"record": record}
        )
        return dict(self._json(response))

    def archive(self, namespace: str, name: str, version: str) -> bytes:
        """The archive's bytes, read as they come and refused past `archive.MAX_ARCHIVE_BYTES`
        (what `archive.verify` takes): a registry cannot make a pull hold more in memory."""
        path = f"/v1/agents/{namespace}/{name}/{version}/archive"
        with self._http.stream("GET", path) as response:
            if response.status_code >= 400:
                response.read()
                self._json(response)
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > archive.MAX_ARCHIVE_BYTES:
                    raise HubError(
                        f"the registry's archive is larger than {archive.MAX_ARCHIVE_BYTES} bytes"
                    )
        return bytes(data)

    def search(self, **filters: Any) -> list[dict[str, Any]]:
        return list(self._get("/v1/search", **filters))

    def submit(self, submission: Mapping[str, Any], secret: str) -> dict[str, Any]:
        """Send a signed `mendari.site-submission/1` (LP-22f) with its ticket's one-time secret
        as the bearer credential (never a publisher token)."""
        from pydantic import ValidationError

        from mendari.hub.site_reports import Submission

        try:  # the path is built from it: only a submission's own pack id and version
            pack = Submission.model_validate(dict(submission)).report.pack
        except ValidationError:
            raise MendariError("not a signed site submission (mendari eval --share-out)") from None
        namespace, name = pack.id.split("/")
        path = f"/v1/agents/{namespace}/{name}/{pack.version}/reports"
        response = self._http.post(
            path, json=dict(submission), headers={"authorization": f"Bearer {secret}"}
        )
        return dict(self._json(response))

    def request_ticket(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Send a signed `mendari.site-ticket-request/1` (LP-22e): the ticket and its one-time
        submission secret come back."""
        return dict(self._json(self._http.post("/v1/tickets", json={"record": dict(record)})))

    def withdraw_ticket(self, ticket_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        response = self._http.post(f"/v1/tickets/{ticket_id}/withdraw", json={"record": record})
        return dict(self._json(response))

    def pack_tickets(self, pack_id: str) -> list[dict[str, Any]]:
        return list(self._get(f"/v1/agents/{pack_id}/tickets")["tickets"])

    def add_key(self, namespace: str, public_pem: bytes) -> str:
        body = {"public_pem": public_pem.decode()}
        response = self._http.post(f"/v1/namespaces/{namespace}/keys", json=body)
        return str(self._json(response)["key_id"])

    def publish_evaluation(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Send a signed `mendari.publisher-evaluation/1` (ADR-0020, `mendari eval --publish`)
        with this namespace's publisher token: the registry's answer names its id and whether
        it re-ran it."""
        from mendari.hub.evaluations import parse

        pack = parse(record).pack  # the path is built from it: only the record's own pack
        namespace, name = pack.id.split("/")
        response = self._http.post(
            f"/v1/agents/{namespace}/{name}/{pack.version}/evaluations", json=dict(record)
        )
        return dict(self._json(response))

    def evaluations(self, namespace: str, name: str, version: str) -> dict[str, Any]:
        """A version's live publisher evaluations, with the registry's figures."""
        return dict(self._get(f"/v1/agents/{namespace}/{name}/{version}/evaluations"))

    def push(self, pack: Pack, key: Ed25519PrivateKey) -> dict[str, Any]:
        namespace, name = pack.id.split("/")
        data = archive.build(pack, key)
        response = self._http.put(
            f"/v1/agents/{namespace}/{name}/{pack.version}",
            content=data,
            headers={"content-type": "application/octet-stream"},
        )
        return dict(self._json(response))


def pull(
    ref: str,
    client: RegistryClient,
    *,
    accept_new_key: bool = False,
    registry_keys: Path | None = None,
) -> Pack:
    """Download, verify and cache a pack (`namespace/name[@version]`); a verified copy in the
    cache is used as is. `registry_keys` is the file of registry keys trusted without first use
    (the one shipped with this package unless given)."""
    pack_id, _, wanted = ref.partition("@")
    if not is_pack_id(pack_id) or (wanted and not is_version(wanted)):
        raise HubError(f"{ref!r} is not namespace/name[@version]")
    namespace, name = pack_id.split("/")
    meta = client.version(namespace, name, wanted or None)
    version = meta.get("version")
    if not is_version(version) or (wanted and version != wanted):
        raise HubError(f"the registry answered a version that is not one of {pack_id}")
    target, former = (_cached_at(cache, pack_id, version) for cache in caches())
    # The copy in use: this version in the cache, or, with none there, the one a pull left in
    # the folder the cache had before (`former_cache`). It is the copy that is used as is,
    # recalled or removed; a version fetched again is written to the cache, and replaces it.
    held = former if former.is_dir() and not target.exists() else target
    deleted = meta.get("deleted")
    if isinstance(deleted, Mapping):
        # Deleted by the registry: never served, and every copy cached before is removed.
        for copy in (target, former):
            if copy.exists():
                shutil.rmtree(copy)
        raise HubError(
            f"{pack_id}@{version} was deleted from the registry: {deleted.get('reason')}", 410
        )
    # The registry's key is pinned on first contact, before any recall it signs is read; and the
    # version's counter-signature is checked before the archive is fetched or a cached copy used.
    accepted = _accepted(client, pack_id, meta, registry_keys)
    if pulled_record(held).get("archive_sha256") == meta["archive_sha256"]:
        pack = load_pack(held, origin="hub")
        if pack.digest == meta["digest"]:
            # Cached: what the registry says of it now (a recall since) is written beside it.
            _write_origin(held, client, namespace, meta, accepted)
            return pack
    # Signed when the registry received it (it takes nothing from a revoked key): a key revoked
    # since still verifies it, one revoked before does not.
    keys = _trusted_keys(
        client,
        namespace,
        meta["key_id"],
        signed_at=meta.get("published_at"),
        accept_new_key=accept_new_key,
    )
    data = client.archive(namespace, name, version)
    if hashlib.sha256(data).hexdigest() != meta["archive_sha256"]:
        raise HubError("the downloaded archive is not the one the registry recorded")
    verified = archive.verify(data, keys)
    if (verified.manifest.get("id"), verified.manifest.get("version")) != (pack_id, version):
        raise HubError(f"the archive is not {pack_id}@{version}, which the registry named")
    if accepted and verified.key_id != meta["key_id"]:
        raise HubError("the archive is not signed by the key the registry counter-signed")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
        staged = Path(scratch) / "pack"
        # On the engine it will run on: a pack it refuses is never cached, nor marked verified.
        pack = archive.extract(verified, staged, origin="hub")
        if accepted and pack.digest != meta["digest"]:
            raise HubError("the agent is not the build the registry counter-signed")
        marked = {**meta, "archive_sha256": verified.sha256}
        _write_origin(staged, client, namespace, marked, accepted)
        if target.exists():
            shutil.rmtree(target)
        staged.rename(target)
    if held != target:
        shutil.rmtree(held)  # the copy in the former folder, which this one replaces
    return load_pack(target, origin="hub")


def _cached_at(cache: Path, pack_id: str, version: str) -> Path:
    """Where `cache` keeps this version of an agent, `<namespace>/<name>/<version>`: refused
    unless it stays inside the agent's own folder there, whatever the registry answered."""
    namespace, name = pack_id.split("/")
    root = cache.resolve() / namespace / name
    target = root / version
    if not target.resolve().is_relative_to(root):
        raise HubError(f"the registry answered a version that is not one of {pack_id}")
    return target


def _write_origin(
    root: Path,
    client: RegistryClient,
    namespace: str,
    meta: Mapping[str, Any],
    accepted: bool | None = None,
) -> None:
    """The verified marker (`core/origin.py` `pulled_record`): where the pack came from, the
    recall the registry served for it at this pull, if any, and whether the registry's
    counter-signature was checked (`accepted`: true, or null for a registry with no key).
    Written whole, then moved in."""
    record = {
        "format": ORIGIN_FORMAT,
        "registry": _shown_url(client.url),
        "namespace": namespace,
        "publisher_type": meta.get("publisher_type"),
        "key_id": meta.get("key_id"),
        "archive_sha256": meta.get("archive_sha256"),
        "digest": meta.get("digest"),
        "pulled_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "yank": meta.get("yank") if isinstance(meta.get("yank"), Mapping) else None,
        "accepted": accepted,
    }
    staged = root / f"{MARKER}.new"
    staged.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    staged.replace(root / MARKER)


def _shown_url(url: str) -> str:
    """A registry's URL as the marker records it: scheme, host, port and path, never a user,
    password, query or fragment."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def recall_of(meta: Mapping[str, Any], client: RegistryClient, pack: Pack) -> dict[str, Any] | None:
    """What the registry says recalls `pack` (None: nothing): its yank's severity, reason, signer
    and time, and whether the record's signature holds with a key this machine pinned and names
    exactly this pack, version and digest (`verified`, and `problem` when it does not); or that
    the version was deleted. A record that does not verify is still a recall: it only stops
    automation."""
    from mendari.hub import yank

    record = meta.get("yank")
    deleted = meta.get("deleted")
    if not isinstance(record, Mapping):
        if isinstance(deleted, Mapping):
            reason = str(deleted.get("reason", ""))
            return {
                "severity": "critical",
                "reason": reason,
                "signer": "registry",
                "at": deleted.get("at"),
                "verified": False,
                "problem": "the version was deleted (no signed record)",
                "deleted": True,
            }
        return None
    shown = {name: record.get(name) for name in ("severity", "reason", "signer", "at")}
    problem = yank.shape_problem(record)
    if problem is None and (record["pack"], record["version"], record["digest"]) != (
        pack.id,
        pack.version,
        pack.digest,
    ):
        problem = "it is about another agent, version or build"
    if problem is None:
        try:
            if record["signer"] == "publisher":
                namespace = pack.id.split("/")[0]
                keys = _trusted_keys(
                    client,
                    namespace,
                    str(record["key_id"]),
                    signed_at=str(record["at"]),
                    accept_new_key=False,
                )
                key = keys.get(str(record["key_id"]))
            else:
                key = _registry_key(client)
        except HubError as error:  # a key this machine has not pinned, say
            key, problem = None, str(error)
        if problem is None:
            problem = (
                "it is signed by a key this machine does not trust"
                if key is None
                else yank.problem(record, key, str(record["key_id"]))
            )
    return {**shown, "verified": problem is None, "problem": problem,
            "deleted": isinstance(deleted, Mapping)}  # fmt: skip


def _registry_key(client: RegistryClient) -> Ed25519PublicKey | None:
    """The registry's own key, pinned on first use (as namespace keys are); None when it has
    none or it changed since it was pinned."""
    return _published_key(client)[2]


def _published_key(
    client: RegistryClient,
) -> tuple[dict[str, Any] | None, Ed25519PublicKey | None, Ed25519PublicKey | None]:
    """What the registry publishes as its key (None: nothing); that key when it is the key its
    id names; and that key again when this machine pinned it (now, on first use, or before)."""
    from mendari.core.checkpoint import key_id as raw_key_id
    from mendari.core.checkpoint import load_public

    published = client.registry_key()
    if published is None:
        return None, None, None
    try:
        key = load_public(str(published.get("public_key", "")))
    except MendariError:
        return published, None, None
    if raw_key_id(key) != published.get("key_id"):
        return published, None, None
    pins = _read_pins()
    mine = _pins_of(pins, client.url)
    pinned = mine.get(_REGISTRY_PIN)
    if pinned is None:
        pins[_pin_url(client.url)] = {**mine, _REGISTRY_PIN: published["key_id"]}
        _write_private(_pins_path(), pins)
    elif pinned != published["key_id"]:
        return published, key, None
    return published, key, key


def _accepted(
    client: RegistryClient, pack_id: str, meta: Mapping[str, Any], registry_keys: Path | None
) -> bool | None:
    """Check the registry's counter-signature on the version `meta` describes (LP-19i,
    `hub/acceptance.py`). True: the registry's key is trusted (shipped with this package, or
    pinned for this registry) and its record verifies and names exactly this pack, version,
    digest, archive and publisher key. None: the registry publishes no key and none was ever
    pinned for it, so nothing is counter-signed. Anything else is refused: a record that is
    missing, does not verify or names something else, and a registry whose key is gone, is
    not a key, or changed since it was pinned (as a changed host key is)."""
    from mendari.core.checkpoint import public_text
    from mendari.hub import acceptance

    version = f"{pack_id}@{meta.get('version')}"
    mode = countersignature_mode()  # a value that is not one fails here, before any trust
    published, presented, key = _published_key(client)
    if published is None:
        if _pins_of(_read_pins(), client.url).get(_REGISTRY_PIN) is None:
            if mode != "optional":
                raise HubError(
                    f"{version} is refused: this registry publishes no signing key, and "
                    f"{COUNTERSIGNATURE_ENV}={mode} pulls only what a registry counter-signed"
                )
            return None
        raise HubError(
            f"{version} is refused: this registry published a signing key before and publishes "
            "none now, so its versions cannot be checked"
        )
    if presented is None:
        raise HubError(f"{version} is refused: the registry's signing key is not a key")
    shipped = acceptance.shipped_keys(registry_keys).get(str(published["key_id"]))
    if shipped is not None and public_text(shipped) == public_text(presented):
        key = presented  # a shipped key is trusted without first use, whatever was pinned
    elif mode == "shipped":
        raise HubError(
            f"{version} is refused: the registry's signing key is not one shipped with this "
            f"release of Mendari, and {COUNTERSIGNATURE_ENV}=shipped trusts no other"
        )
    if key is None:
        raise HubError(
            f"{version} is refused: the registry's signing key is not the one this machine "
            "pinned for it. If its operator confirms the new key, remove the registry's "
            f"'{_REGISTRY_PIN}' entry from {_pins_path()} and pull again"
        )
    checked = meta.get("checked")
    record = checked.get("accepted") if isinstance(checked, Mapping) else None
    if record is None:
        raise HubError(
            f"{version} is not counter-signed by the registry, which has a signing key: "
            "refused (its operator counter-signs with `mendari registry countersign`)"
        )
    problem = acceptance.problem(record, key, str(published["key_id"]))
    if problem is None:
        problem = acceptance.names_problem(
            record,
            pack=pack_id,
            version=meta.get("version"),
            digest=meta.get("digest"),
            archive_sha256=meta.get("archive_sha256"),
            publisher_key_id=meta.get("key_id"),
        )
    if problem:
        raise HubError(
            f"{version} is refused: the registry's counter-signature is not one: {problem}"
        )
    return True


_REGISTRY_PIN = "@registry"  # never a namespace (a namespace is letters, digits and dashes)


def _revoked_at(listed: Mapping[str, Any]) -> datetime | None:
    """When the registry says a listed key was revoked (None: it is live, or the registry is
    one from before `revoked_at`). A value that is not a UTC time is refused, never read as
    live."""
    from mendari.hub import yank

    value = listed.get("revoked_at")
    if value is None:
        return None
    when = yank.at({"at": value}) if isinstance(value, str) else None
    if when is None:
        raise HubError("the registry listed a key whose revoked_at is not a UTC time")
    return when


def _trusted_keys(
    client: RegistryClient,
    namespace: str,
    signer: str,
    *,
    signed_at: str | None,
    accept_new_key: bool,
) -> dict[str, Ed25519PublicKey]:
    """The namespace's keys this machine trusts for what was signed at `signed_at`, pinning
    them the first time it sees them. A key the registry lists as revoked (`revoked_at`) is
    trusted only for what was signed strictly before it was revoked, as the registry itself
    keeps verifying what such a key signed before (`mendari registry revoke-key`, ADR-0020);
    for a `signed_at` that is missing or not a UTC time, never. Refused outright when `signer`
    is such a key."""
    from mendari.hub import yank

    listed = client.namespace(namespace)["keys"]
    found = {k["key_id"]: archive.load_public_key(k["public_pem"].encode()) for k in listed}
    for key_id, key in found.items():
        if archive.key_id(key) != key_id:
            raise HubError("the registry listed a key under the wrong id")
    revoked = {k["key_id"]: _revoked_at(k) for k in listed}
    when = yank.at({"at": signed_at}) if isinstance(signed_at, str) else None
    untrusted = {
        key_id: at
        for key_id, at in revoked.items()
        if at is not None and (when is None or when >= at)
    }
    if signer in untrusted:
        raise HubError(
            f"{namespace} is signed by {signer}, which the registry lists as revoked since "
            f"{untrusted[signer].isoformat()}: nothing it signed after that is trusted"
            + ("" if when is not None else ", and when this was signed is not known")
        )
    pins = _read_pins()
    mine = _pins_of(pins, client.url)
    pinned = set(mine.get(namespace, []))
    if not pinned or accept_new_key:
        pinned |= set(found)
        pins[_pin_url(client.url)] = {**mine, namespace: sorted(pinned)}
        _write_private(_pins_path(), pins)
    elif signer not in pinned:
        raise HubError(
            f"{namespace} is signed by {signer}, which this machine has not seen before; "
            "if the publisher rotated their key, pull again with --accept-new-key"
        )
    return {
        key_id: key for key_id, key in found.items() if key_id in pinned and key_id not in untrusted
    }
