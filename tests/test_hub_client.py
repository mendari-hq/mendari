# SPDX-License-Identifier: Apache-2.0
"""Push, pull, search and Decision.from_hub (hub/client.py) against a registry served over HTTP."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from mendari.cli import main
from mendari.core.decision import Decision
from mendari.core.pack import Pack, load_pack
from mendari.core.registry import resolve_pack
from mendari.errors import HubError, PackNotFoundError
from mendari.evals.share import SiteReport
from mendari.hub import archive, client
from mendari.hub.client import RegistryClient, pull
from mendari.hub.registry import create_registry_app
from mendari.sandbox import example_datasets_dir

ADMIN = "admin-token-for-tests"
REFERRAL_DIR = resolve_pack("mendari/referral-router").root
SHIPPED = load_pack(REFERRAL_DIR).version  # the referral pack as it ships


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Hub:
    def __init__(self, url: str) -> None:
        self.url = url
        self.clients: list[RegistryClient] = []

    def client(self, token: str | None = None) -> RegistryClient:
        """A client closed when the test ends."""
        opened = RegistryClient(self.url, token)
        self.clients.append(opened)
        return opened

    def namespace(self, name: str) -> tuple[str, archive.Ed25519PrivateKey]:
        """A namespace with a token and a registered key."""
        with httpx.Client(base_url=self.url, headers={"authorization": f"Bearer {ADMIN}"}) as admin:
            admin.post("/v1/namespaces", json={"name": name}).raise_for_status()
            token = admin.post(f"/v1/namespaces/{name}/tokens").json()["token"]
        private, public = archive.generate_key()
        self.client(token).add_key(name, public)
        return token, archive.load_private_key(private)


@pytest.fixture
def hub(tmp_path: Path) -> Iterator[Hub]:
    port = free_port()
    app = create_registry_app(tmp_path / "registry.db", admin_token=ADMIN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with httpx.Client() as probe:
                probe.get(f"{url}/v1/health").raise_for_status()
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    registry = Hub(url)
    yield registry
    for opened in registry.clients:
        opened.close()
    server.should_exit = True
    thread.join(timeout=5)


def copy_pack(tmp_path: Path, pack_id: str, version: str = SHIPPED) -> Pack:
    """The referral pack under another id (and version): a pack no search path has."""
    target = tmp_path / "packs" / pack_id.replace("/", "-") / version
    shutil.copytree(REFERRAL_DIR, target)
    manifest = (target / "pack.yaml").read_text()
    manifest = manifest.replace("id: mendari/referral-router", f"id: {pack_id}", 1)
    manifest = manifest.replace(f"version: {SHIPPED}", f"version: {version}", 1)
    (target / "pack.yaml").write_text(manifest)
    return load_pack(target)


def test_push_then_pull_verifies_and_caches(hub: Hub, tmp_path: Path) -> None:
    token, key = hub.namespace("site-a")
    pack = copy_pack(tmp_path, "site-a/referrals")
    published = hub.client(token).push(pack, key)
    assert published["digest"] == pack.digest
    pulled = pull("site-a/referrals", hub.client())
    assert (pulled.id, pulled.version, pulled.digest) == (pack.id, pack.version, pack.digest)
    assert pulled.root == client.packs_cache() / "site-a" / "referrals" / SHIPPED
    assert resolve_pack("site-a/referrals").digest == pack.digest  # the cache is on the search path


def test_a_cached_pack_is_not_downloaded_again(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, key = hub.namespace("site-a")
    hub.client(token).push(copy_pack(tmp_path, "site-a/referrals"), key)
    pull("site-a/referrals", hub.client())
    monkeypatch.setattr(RegistryClient, "archive", lambda *a: pytest.fail("downloaded again"))
    assert pull(f"site-a/referrals@{SHIPPED}", hub.client()).version == SHIPPED


def test_an_archive_that_is_not_the_recorded_one_is_refused(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, key = hub.namespace("site-a")
    hub.client(token).push(copy_pack(tmp_path, "site-a/referrals"), key)
    original = RegistryClient.archive
    monkeypatch.setattr(RegistryClient, "archive", lambda self, *a: original(self, *a) + b"\0")
    with pytest.raises(HubError, match="not the one the registry recorded"):
        pull("site-a/referrals", hub.client())
    assert not (client.packs_cache() / "site-a" / "referrals").exists()


class HostileRegistry:
    """A registry (compromised, or a wrong MENDARI_REGISTRY_URL) that serves a genuinely signed
    archive of the referral pack under whatever version it is told to name."""

    def __init__(self, tmp_path: Path) -> None:
        self.pack = load_pack(REFERRAL_DIR)
        private, self.public = archive.generate_key()
        key = archive.load_private_key(private)
        self.data = archive.build(self.pack, key)
        self.key_id = archive.key_id(key.public_key())
        self.version = self.pack.version

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/archive"):
            return httpx.Response(200, content=self.data)
        if path.startswith("/v1/namespaces/"):
            keys = [{"key_id": self.key_id, "public_pem": self.public.decode()}]
            return httpx.Response(200, json={"keys": keys})
        meta = {
            "version": self.version,
            "archive_sha256": hashlib.sha256(self.data).hexdigest(),
            "key_id": self.key_id,
            "digest": self.pack.digest,
        }
        return httpx.Response(200, json={"latest": meta, **meta})

    def client(self) -> RegistryClient:
        return RegistryClient(
            "https://registry.example", transport=httpx.MockTransport(self.handle)
        )


@pytest.mark.parametrize("version", ["{victim}", "../../../victim", "0.1.1/../../../../victim"])
def test_a_registry_cannot_choose_where_a_pull_writes(tmp_path: Path, version: str) -> None:
    """T11: the cache path was built from the registry's `version` and replaced with rmtree, so
    a hostile registry deleted any directory and wrote the pack there, the publisher's key
    pinned and the signature good. The version must be one, inside the cache, and the signed
    manifest's."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep me")
    registry = HostileRegistry(tmp_path)
    with registry.client() as honest:
        assert pull("mendari/referral-router", honest).version == registry.pack.version  # pins
    registry.version = version.format(victim=victim)
    with registry.client() as hostile, pytest.raises(HubError, match="not one of"):
        pull("mendari/referral-router", hostile)
    assert (victim / "precious.txt").read_text() == "keep me"
    assert sorted(p.name for p in victim.iterdir()) == ["precious.txt"]


def test_a_registry_cannot_serve_one_version_as_another(tmp_path: Path) -> None:
    registry = HostileRegistry(tmp_path)
    registry.version = "9.9.9"  # a valid version, but not the signed archive's
    with registry.client() as hostile, pytest.raises(HubError, match="is not mendari/referral"):
        pull("mendari/referral-router", hostile)
    assert not (client.packs_cache() / "mendari" / "referral-router" / "9.9.9").exists()
    with registry.client() as hostile, pytest.raises(HubError, match="not namespace/name"):
        pull("../x", hostile)


def test_a_rotated_key_needs_consent(hub: Hub, tmp_path: Path) -> None:
    token, key = hub.namespace("site-a")
    publisher = hub.client(token)
    publisher.push(copy_pack(tmp_path, "site-a/referrals"), key)
    pull("site-a/referrals", hub.client())  # pins the first key
    private, public = archive.generate_key()
    publisher.add_key("site-a", public)
    publisher.push(
        copy_pack(tmp_path, "site-a/referrals", "0.2.0"), archive.load_private_key(private)
    )
    with pytest.raises(HubError, match="has not seen before"):
        pull("site-a/referrals@0.2.0", hub.client())
    assert pull("site-a/referrals@0.2.0", hub.client(), accept_new_key=True).version == "0.2.0"


def test_from_hub_pulls_a_pack_that_is_not_local(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, key = hub.namespace("site-a")
    hub.client(token).push(copy_pack(tmp_path, "site-a/referrals"), key)
    with pytest.raises(PackNotFoundError, match="no registry"):
        Decision.from_hub("site-a/referrals")
    monkeypatch.setenv("MENDARI_REGISTRY_URL", hub.url)
    decision = Decision.from_hub("site-a/referrals")
    dataset = (example_datasets_dir() or Path("datasets")) / "referral-local-site-example.jsonl"
    case = json.loads(dataset.read_text().splitlines()[0])
    assert decision(case["input"]).recommendation


def test_the_cli_logs_in_pushes_searches_and_pulls(
    hub: Hub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token, key = hub.namespace("site-a")
    key_file = tmp_path / "publisher.key"
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    key_file.write_bytes(pem)
    assert main(["login", "--registry", hub.url, "--token", token]) == 0
    credentials = client.config_dir() / "credentials.json"
    assert credentials.stat().st_mode & 0o777 == 0o600
    pack = copy_pack(tmp_path, "site-a/referrals")
    assert main(["push", str(pack.root), "--key", str(key_file)]) == 0
    assert main(["search", "specialty"]) == 0
    assert f"site-a/referrals@{SHIPPED}" in capsys.readouterr().out
    assert main(["pull", "site-a/referrals"]) == 0
    assert "verified and cached" in capsys.readouterr().out
    assert main(["search", "--json", "--risk", "4"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_without_a_registry_the_client_says_how_to_set_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["search", "x"]) == 1
    assert "mendari login --registry URL" in capsys.readouterr().err


def test_an_unreachable_registry_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["login", "--registry", f"http://127.0.0.1:{free_port()}"]) == 1
    assert "could not reach the registry" in capsys.readouterr().err


def test_eval_share_out_writes_what_would_be_sent_and_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--share-out` lets a site read the payload first: no registry, no login, no case id."""
    out = tmp_path / "share.json"
    args = ["eval", "mendari/patient-inbox-router", "--dataset", "red_flags_holdout"]
    assert main([*args, "--baseline", "none", "--share-out", str(out)]) == 0
    assert f"wrote the report --share sends to {out}" in capsys.readouterr().err
    payload = json.loads(out.read_text())
    assert SiteReport.model_validate(payload).dataset.n == 30
    pack = resolve_pack("mendari/patient-inbox-router")
    ids = [
        json.loads(line)["id"]
        for line in pack.read_bytes("data/red_flags_holdout.jsonl").decode().splitlines()
        if line.strip()
    ]
    assert ids and not any(case in out.read_text() for case in ids)


def test_eval_share_sends_the_metrics(
    hub: Hub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token, key = hub.namespace("mendari")
    hub.client(token).push(resolve_pack("mendari/referral-router"), key)
    assert main(["login", "--registry", hub.url, "--token", token]) == 0
    dataset = (example_datasets_dir() or Path("datasets")) / "referral-local-site-example.jsonl"
    args = ["eval", "mendari/referral-router", "--dataset", str(dataset), "--limit", "20"]
    assert main([*args, "--baseline", "none", "--share", "Site B"]) == 0
    assert "shared with the registry as Site B" in capsys.readouterr().err
    with httpx.Client() as http:
        sites = http.get(f"{hub.url}/v1/packs/mendari/referral-router/reports").json()["sites"]
    assert [(s["site"], s["n"]) for s in sites] == [("Site B", 20)]
