# SPDX-License-Identifier: Apache-2.0
"""The Hub client: log in, push, pull, search.

`pull` never uses a pack it has not verified:
1. the archive's SHA-256 is the one the registry recorded;
2. every file matches the signed manifest;
3. the signature is by a key of the pack's namespace;
4. the extracted pack loads with the signed digest, as a pack from a registry: on the community
   engine (RE2, the caps, the input limits and a CPU budget per decision) unless it is a copy of
   a reference pack of this release (ADR-0009). A pack that does not load there is not cached.

Nothing the registry answers chooses where a pack is written: the namespace, name and version
must be a pack id and a semantic version before any cache path is built, the path must stay
inside the cache, and the signed manifest must name that id and version. A compromised registry
can refuse service, never delete or write outside the cache.

A recalled version (a signed `mendari.yank/1` record, `hub/yank.py`) is never the latest: a
pull names it by its exact version, and `recall_of` says what its record says and whether its
signature holds with a key this machine pinned (the namespace's, or the registry's own, pinned
on first use like them). A record that does not verify is still a recall: it only stops
automation, the safe direction.

Namespace keys are pinned on first use (per registry). A pack signed by a key that isn't pinned
is refused, unless the user accepts the new key, the way SSH treats a changed host key.

Pulled packs are cached under `~/.cache/mendari/packs/<namespace>/<name>/<version>`, which is
on the pack search path, so `mendari test`, `Decision.from_hub` and the server find them like
any local pack. Credentials and pinned keys live in `~/.config/mendari` (files mode 0600).
`MENDARI_CONFIG_DIR` and `MENDARI_CACHE_DIR` move them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mendari.core.pack import Pack, load_pack
from mendari.core.spec import is_pack_id, is_version
from mendari.errors import HubError, MendariError
from mendari.hub import archive

TIMEOUT = 60.0
_MARKER = ".mendari-verified"


def config_dir() -> Path:
    return Path(os.environ.get("MENDARI_CONFIG_DIR") or Path.home() / ".config" / "mendari")


def cache_dir() -> Path:
    return Path(os.environ.get("MENDARI_CACHE_DIR") or Path.home() / ".cache" / "mendari")


def packs_cache() -> Path:
    return cache_dir() / "packs"


def _read(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError):
        return {}


def _write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.chmod(path, 0o600)


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
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            status = response.status_code
            message = f"the registry answered HTTP {status}" + (f": {detail}" if detail else "")
            raise HubError(message, status if status in (404, 409, 410) else 422)
        return response.json()

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
            return dict(self._get(f"/v1/packs/{namespace}/{name}/{version}"))
        latest = self._get(f"/v1/packs/{namespace}/{name}").get("latest")
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
            f"/v1/packs/{namespace}/{name}/{version}/yank", json={"record": record}
        )
        return dict(self._json(response))

    def archive(self, namespace: str, name: str, version: str) -> bytes:
        response = self._http.get(f"/v1/packs/{namespace}/{name}/{version}/archive")
        if response.status_code >= 400:
            self._json(response)
        return response.content

    def search(self, **filters: Any) -> list[dict[str, Any]]:
        return list(self._get("/v1/search", **filters))

    def share_report(self, report: Mapping[str, Any], site: str) -> dict[str, Any]:
        """Share an evaluation of a published pack (the registry keeps only its metrics)."""
        from mendari.evals.share import shareable

        pack = report["pack"]
        namespace, name = str(pack["id"]).split("/")
        path = f"/v1/packs/{namespace}/{name}/{pack['version']}/reports"
        response = self._http.post(path, json={"site": site, "report": shareable(report)})
        return dict(self._json(response))

    def add_key(self, namespace: str, public_pem: bytes) -> str:
        body = {"public_pem": public_pem.decode()}
        response = self._http.post(f"/v1/namespaces/{namespace}/keys", json=body)
        return str(self._json(response)["key_id"])

    def push(self, pack: Pack, key: Ed25519PrivateKey) -> dict[str, Any]:
        namespace, name = pack.id.split("/")
        data = archive.build(pack, key)
        response = self._http.put(
            f"/v1/packs/{namespace}/{name}/{pack.version}",
            content=data,
            headers={"content-type": "application/octet-stream"},
        )
        return dict(self._json(response))


def pull(ref: str, client: RegistryClient, *, accept_new_key: bool = False) -> Pack:
    """Download, verify and cache a pack (`namespace/name[@version]`); a verified copy in the
    cache is used as is."""
    pack_id, _, wanted = ref.partition("@")
    if not is_pack_id(pack_id) or (wanted and not is_version(wanted)):
        raise HubError(f"{ref!r} is not namespace/name[@version]")
    namespace, name = pack_id.split("/")
    meta = client.version(namespace, name, wanted or None)
    version = meta.get("version")
    if not is_version(version) or (wanted and version != wanted):
        raise HubError(f"the registry answered a version that is not one of {pack_id}")
    cache = packs_cache().resolve()
    target = cache / namespace / name / version
    if not target.resolve().is_relative_to(cache / namespace / name):
        raise HubError(f"the registry answered a version that is not one of {pack_id}")
    if (target / _MARKER).is_file() and (target / _MARKER).read_text().strip() == meta[
        "archive_sha256"
    ]:
        pack = load_pack(target, origin="hub")
        if pack.digest == meta["digest"]:
            return pack
    keys = _trusted_keys(client, namespace, meta["key_id"], accept_new_key=accept_new_key)
    data = client.archive(namespace, name, version)
    if hashlib.sha256(data).hexdigest() != meta["archive_sha256"]:
        raise HubError("the downloaded archive is not the one the registry recorded")
    verified = archive.verify(data, keys)
    if (verified.manifest.get("id"), verified.manifest.get("version")) != (pack_id, version):
        raise HubError(f"the archive is not {pack_id}@{version}, which the registry named")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
        staged = Path(scratch) / "pack"
        # On the engine it will run on: a pack it refuses is never cached, nor marked verified.
        pack = archive.extract(verified, staged, origin="hub")
        (staged / _MARKER).write_text(verified.sha256 + "\n")
        if target.exists():
            shutil.rmtree(target)
        staged.rename(target)
    return load_pack(target, origin="hub")


def recall_of(meta: Mapping[str, Any], client: RegistryClient) -> dict[str, Any] | None:
    """What a version's recall says (None: it is not recalled): its severity, reason, signer and
    time, and whether its signature holds with a key this machine pinned (`verified`, and
    `problem` when it does not)."""
    from mendari.hub import yank

    record = meta.get("yank")
    if not isinstance(record, Mapping):
        return None
    shown = {name: record.get(name) for name in ("severity", "reason", "signer", "at")}
    problem = yank.shape_problem(record)
    if problem is None:
        namespace = str(record["pack"]).partition("/")[0]
        if record["signer"] == "publisher":
            keys = _trusted_keys(client, namespace, str(record["key_id"]), accept_new_key=False)
            key = keys.get(str(record["key_id"]))
        else:
            key = _registry_key(client)
        problem = (
            "it is signed by a key this machine does not trust"
            if key is None
            else yank.problem(record, key, str(record["key_id"]))
        )
    if problem is None and (record["digest"], record["archive_sha256"]) != (
        meta.get("digest"),
        meta.get("archive_sha256"),
    ):
        problem = "it is about another build of this version"
    return {**shown, "verified": problem is None, "problem": problem}


def _registry_key(client: RegistryClient) -> Ed25519PublicKey | None:
    """The registry's own key, pinned on first use (as namespace keys are); None when it has
    none or it changed since it was pinned."""
    from mendari.core.checkpoint import key_id as raw_key_id
    from mendari.core.checkpoint import load_public

    published = client.registry_key()
    if published is None:
        return None
    try:
        key = load_public(str(published.get("public_key", "")))
    except MendariError:
        return None
    if raw_key_id(key) != published.get("key_id"):
        return None
    path = config_dir() / "trusted_keys.json"
    pins = _read(path)
    pinned = pins.setdefault(client.url, {}).get(_REGISTRY_PIN)
    if pinned is None:
        pins[client.url][_REGISTRY_PIN] = published["key_id"]
        _write_private(path, pins)
    elif pinned != published["key_id"]:
        return None
    return key


_REGISTRY_PIN = "@registry"  # never a namespace (a namespace is letters, digits and dashes)


def _trusted_keys(
    client: RegistryClient, namespace: str, signer: str, *, accept_new_key: bool
) -> dict[str, Ed25519PublicKey]:
    """The namespace's keys this machine trusts, pinning them the first time it sees them."""
    listed = client.namespace(namespace)["keys"]
    found = {k["key_id"]: archive.load_public_key(k["public_pem"].encode()) for k in listed}
    for key_id, key in found.items():
        if archive.key_id(key) != key_id:
            raise HubError("the registry listed a key under the wrong id")
    path = config_dir() / "trusted_keys.json"
    pins = _read(path)
    pinned = set(pins.get(client.url, {}).get(namespace, []))
    if not pinned or accept_new_key:
        pinned |= set(found)
        pins.setdefault(client.url, {})[namespace] = sorted(pinned)
        _write_private(path, pins)
    elif signer not in pinned:
        raise HubError(
            f"{namespace} is signed by {signer}, which this machine has not seen before; "
            "if the publisher rotated their key, pull again with --accept-new-key"
        )
    return {key_id: key for key_id, key in found.items() if key_id in pinned}
