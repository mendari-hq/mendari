"""Push, pull, search and Decision.from_hub (hub/client.py) against a registry served over HTTP."""

from __future__ import annotations

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

from openhealth.cli import main
from openhealth.core.decision import Decision
from openhealth.core.pack import Pack, load_pack
from openhealth.core.registry import resolve_pack
from openhealth.errors import HubError, PackNotFoundError
from openhealth.hub import archive, client
from openhealth.hub.client import RegistryClient, pull
from openhealth.hub.registry import create_registry_app
from openhealth.sandbox import example_datasets_dir

ADMIN = "admin-token-for-tests"
REFERRAL_DIR = resolve_pack("openhealth/referral-router").root


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


def copy_pack(tmp_path: Path, pack_id: str, version: str = "0.1.1") -> Pack:
    """The referral pack under another id (and version): a pack no search path has."""
    target = tmp_path / "packs" / pack_id.replace("/", "-") / version
    shutil.copytree(REFERRAL_DIR, target)
    manifest = (target / "pack.yaml").read_text()
    manifest = manifest.replace("id: openhealth/referral-router", f"id: {pack_id}", 1)
    manifest = manifest.replace("version: 0.1.1", f"version: {version}", 1)
    (target / "pack.yaml").write_text(manifest)
    return load_pack(target)


def test_push_then_pull_verifies_and_caches(hub: Hub, tmp_path: Path) -> None:
    token, key = hub.namespace("site-a")
    pack = copy_pack(tmp_path, "site-a/referrals")
    published = hub.client(token).push(pack, key)
    assert published["digest"] == pack.digest
    pulled = pull("site-a/referrals", hub.client())
    assert (pulled.id, pulled.version, pulled.digest) == (pack.id, pack.version, pack.digest)
    assert pulled.root == client.packs_cache() / "site-a" / "referrals" / "0.1.1"
    assert resolve_pack("site-a/referrals").digest == pack.digest  # the cache is on the search path


def test_a_cached_pack_is_not_downloaded_again(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, key = hub.namespace("site-a")
    hub.client(token).push(copy_pack(tmp_path, "site-a/referrals"), key)
    pull("site-a/referrals", hub.client())
    monkeypatch.setattr(RegistryClient, "archive", lambda *a: pytest.fail("downloaded again"))
    assert pull("site-a/referrals@0.1.1", hub.client()).version == "0.1.1"


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
    monkeypatch.setenv("OPENHEALTH_REGISTRY_URL", hub.url)
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
    assert "site-a/referrals@0.1.1" in capsys.readouterr().out
    assert main(["pull", "site-a/referrals"]) == 0
    assert "verified and cached" in capsys.readouterr().out
    assert main(["search", "--json", "--risk", "4"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_without_a_registry_the_client_says_how_to_set_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["search", "x"]) == 1
    assert "openhealth login --registry URL" in capsys.readouterr().err


def test_an_unreachable_registry_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["login", "--registry", f"http://127.0.0.1:{free_port()}"]) == 1
    assert "could not reach the registry" in capsys.readouterr().err
