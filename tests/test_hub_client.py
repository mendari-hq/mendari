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
from mendari.core.pack import Pack, PatternsRefused, load_pack
from mendari.core.registry import resolve_pack
from mendari.errors import HubError, PackNotFoundError, PackValidationError
from mendari.evals.share import SiteReport
from mendari.hub import archive, client
from mendari.hub.client import RegistryClient, pull
from mendari.hub.registry import create_registry_app
from mendari.sandbox import example_datasets_dir
from tests.conftest import INBOX_DIR

ADMIN = "admin-token-for-tests"
REFERRAL_DIR = resolve_pack("mendari/referral-router").root
# The packs these tests publish under other ids are copies of the inbox pack, whose patterns
# all run on RE2: a pulled pack that is not a reference pack of this release runs on the engine
# for packs from a registry (ADR-0009), which the referral pack's lookahead cannot run on.
SHIPPED = load_pack(INBOX_DIR).version  # the inbox pack as it ships


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
    """The inbox pack under another id (and version): a pack no search path has."""
    target = tmp_path / "packs" / pack_id.replace("/", "-") / version
    shutil.copytree(INBOX_DIR, target)
    manifest = (target / "pack.yaml").read_text()
    manifest = manifest.replace("id: mendari/patient-inbox-router", f"id: {pack_id}", 1)
    manifest = manifest.replace(f"version: {SHIPPED}", f"version: {version}", 1)
    (target / "pack.yaml").write_text(manifest)
    return load_pack(target)


def test_push_then_pull_verifies_and_caches(hub: Hub, tmp_path: Path) -> None:
    token, key = hub.namespace("site-a")
    pack = copy_pack(tmp_path, "site-a/inbox")
    published = hub.client(token).push(pack, key)
    assert published["digest"] == pack.digest
    pulled = pull("site-a/inbox", hub.client())
    assert (pulled.id, pulled.version, pulled.digest) == (pack.id, pack.version, pack.digest)
    assert pulled.root == client.packs_cache() / "site-a" / "inbox" / SHIPPED
    assert resolve_pack("site-a/inbox").digest == pack.digest  # the cache is on the search path


def test_a_pulled_packs_uploaded_figures_are_not_shown_where_it_is_served(
    hub: Hub, tmp_path: Path
) -> None:
    """The LP-18a review: the registry shows no uploaded benchmark or leaderboard, but the
    engine's pack detail and `mendari card` did, once the pack was pulled. A pack from a registry
    is shown as the registry renders it (ADR-0012); the same pack of one's own keeps its own."""
    from fastapi.testclient import TestClient

    from mendari.core.card import card_of
    from mendari.server.app import create_app

    token, key = hub.namespace("site-a")
    pack = copy_pack(tmp_path, "site-a/inbox")
    assert (pack.root / "evals" / "benchmark.json").is_file()
    hub.client(token).push(pack, key)
    pulled = pull("site-a/inbox", hub.client())
    card = card_of(pulled)
    assert "The registry shows no figure it did not compute itself" in card
    assert "## Provider comparison" not in card and "## Provider comparison" in card_of(pack)
    with TestClient(create_app(db_path=tmp_path / "a.db")) as engine:
        detail = engine.get("/api/packs/site-a/inbox").json()
    assert detail["origin"] == "hub"
    assert detail["benchmark"] is None and detail["leaderboard"] is None
    assert detail["card_markdown"] == card


def test_a_pulled_packs_uploaded_benchmark_is_never_compared_or_monitored_against(
    hub: Hub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """LP-18h: the engine still read a pulled pack's uploaded benchmark in two places. The
    evaluation route and `mendari eval` attached a comparison with it, and the monitor took it
    as its reference. Now the evaluation says it is not compared with the benchmark the pack
    carries, as it says when a benchmark cannot be compared, and the monitor has no benchmark
    (`from_registry`). The same pack of one's own is compared as before."""
    from fastapi.testclient import TestClient

    from mendari.core.card import benchmark
    from mendari.evals.compare import FROM_REGISTRY, not_compared
    from mendari.server.app import create_app

    token, key = hub.namespace("site-a")
    pack = copy_pack(tmp_path, "site-a/inbox")
    hub.client(token).push(pack, key)
    pulled = pull("site-a/inbox", hub.client())
    assert pulled.origin == "hub" and benchmark(pulled) is not None  # it carries one
    run = {"dataset": "red_flags", "limit": 20}
    with TestClient(create_app(db_path=tmp_path / "a.db")) as engine:
        answer = engine.post("/api/packs/site-a/inbox/evals", json=run).json()
        monitored = engine.get("/api/monitoring", params={"pack_id": "site-a/inbox"}).json()
    capsys.readouterr()  # the server's security log
    assert answer["comparison"] == not_compared(FROM_REGISTRY)
    assert answer["figures"]["comparison"] is None
    assert (monitored["benchmark"]["comparable"], monitored["benchmark"]["reason"]) == (
        False,
        "from_registry",
    )
    assert monitored["benchmark"]["figures"]["accuracy"] is None
    eval_args = ["--dataset", "red_flags", "--limit", "20"]
    assert main(["eval", "site-a/inbox", *eval_args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["comparison"] == not_compared(FROM_REGISTRY)
    assert main(["eval", "site-a/inbox", *eval_args]) == 0
    assert f"Comparison unavailable: {FROM_REGISTRY}" in capsys.readouterr().out
    # The pack it was published from is one's own: compared with its benchmark, as before.
    assert main(["eval", str(pack.root), *eval_args, "--json"]) == 0
    own = json.loads(capsys.readouterr().out)["comparison"]
    assert own["status"] in ("ok", "warning", "degraded") and "baseline" in own


def test_the_demo_never_compares_a_pulled_copy_with_the_benchmark_it_carries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The LP-18h review: `mendari demo` takes its pack from the search path, the pack cache
    first, and compared a pulled copy of the reference pack with the benchmark the copy carries,
    whatever its publisher put there. Now it says the copy is not compared; the bundled pack is
    compared as before."""
    from mendari.evals.compare import FROM_REGISTRY
    from mendari.sandbox.demo import PACK_ID, run_demo

    run_demo(example_datasets_dir())
    assert "Public benchmark (test, n=600):" in capsys.readouterr().out
    # The reference pack's own bytes, in the cache: not refused as a shadow, and from a registry.
    shutil.copytree(INBOX_DIR, client.packs_cache() / PACK_ID / SHIPPED)
    assert resolve_pack(PACK_ID).origin == "hub"
    run_demo(example_datasets_dir())
    shown = capsys.readouterr().out
    assert f"Comparison unavailable: {FROM_REGISTRY}" in shown
    assert "Public benchmark" not in shown and "degradation" not in shown


def test_a_cached_pack_is_not_downloaded_again(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, key = hub.namespace("site-a")
    hub.client(token).push(copy_pack(tmp_path, "site-a/inbox"), key)
    pull("site-a/inbox", hub.client())
    monkeypatch.setattr(RegistryClient, "archive", lambda *a: pytest.fail("downloaded again"))
    assert pull(f"site-a/inbox@{SHIPPED}", hub.client()).version == SHIPPED


def test_an_archive_that_is_not_the_recorded_one_is_refused(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, key = hub.namespace("site-a")
    hub.client(token).push(copy_pack(tmp_path, "site-a/inbox"), key)
    original = RegistryClient.archive
    monkeypatch.setattr(RegistryClient, "archive", lambda self, *a: original(self, *a) + b"\0")
    with pytest.raises(HubError, match="not the one the registry recorded"):
        pull("site-a/inbox", hub.client())
    assert not (client.packs_cache() / "site-a" / "inbox").exists()


class HostileRegistry:
    """A registry (compromised, or a wrong MENDARI_REGISTRY_URL) that serves a genuinely signed
    archive of the referral pack (or `root`) under whatever version it is told to name."""

    def __init__(self, tmp_path: Path, root: Path = REFERRAL_DIR) -> None:
        self.pack = load_pack(root)
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


def changed_referral(tmp_path: Path, version: str | None = None) -> Path:
    """A copy of the referral pack with one more line in its rules, at `version` (its own unless
    given)."""
    changed = tmp_path / "changed"
    shutil.copytree(REFERRAL_DIR, changed)
    rules = changed / "logic" / "triage_rules.yaml"
    rules.write_text(rules.read_text() + "# one more line\n")
    if version is not None:
        manifest = changed / "pack.yaml"
        shipped = load_pack(REFERRAL_DIR).version
        manifest.write_text(
            manifest.read_text().replace(f"version: {shipped}", f"version: {version}", 1)
        )
    return changed


def test_a_pull_the_engine_refuses_leaves_nothing_in_the_cache(tmp_path: Path) -> None:
    """H3: a signed copy of the referral pack with one changed byte (at a new version: at its own
    it is a shadow, below) is not the reference pack, so it runs on the engine for packs from a
    registry, which cannot run its lookahead. The pull refuses it before the rename: no version
    directory, no verified marker, nothing on the search path."""
    registry = HostileRegistry(tmp_path, changed_referral(tmp_path, "9.9.9"))
    assert registry.version == "9.9.9"
    with registry.client() as signed, pytest.raises(PatternsRefused) as refused:
        pull("mendari/referral-router", signed)
    assert any("lookahead" in problem for problem in refused.value.problems)
    assert any("not the copy of it in this release" in p for p in refused.value.problems)
    cache = client.packs_cache()
    assert not (cache / "mendari" / "referral-router" / registry.version).exists()
    assert list(cache.rglob(".mendari-verified")) == []
    assert resolve_pack("mendari/referral-router").origin == "maintained"


def test_a_pull_of_a_shadow_is_refused_and_nothing_is_cached(tmp_path: Path) -> None:
    """F: a signed copy of the referral pack with one changed byte at the reference pack's own
    version would be served in its place. The pull refuses it before the rename, and the
    reference pack is still the one served."""
    registry = HostileRegistry(tmp_path, changed_referral(tmp_path))
    reference = load_pack(REFERRAL_DIR)
    assert registry.version == reference.version
    with registry.client() as signed, pytest.raises(PackValidationError) as refused:
        pull("mendari/referral-router", signed)
    assert not isinstance(refused.value, PatternsRefused)
    (problem,) = refused.value.problems
    assert "never takes a reference pack's place" in problem and "remove" not in problem
    cache = client.packs_cache()
    assert not (cache / "mendari" / "referral-router" / reference.version).exists()
    assert list(cache.rglob(".mendari-verified")) == []
    served = resolve_pack("mendari/referral-router")
    assert (served.origin, served.digest) == ("maintained", reference.digest)


def test_a_shadow_cached_before_this_release_is_refused_on_the_cache_hit_path(
    tmp_path: Path,
) -> None:
    """One pulled before this release refused it is in the cache, marked verified: the pull's
    cache hit loads it and refuses it, naming the directory to remove, and the reference pack is
    served."""
    registry = HostileRegistry(tmp_path, changed_referral(tmp_path))
    reference = load_pack(REFERRAL_DIR)
    cached = client.packs_cache() / "mendari" / "referral-router" / reference.version
    shutil.copytree(registry.pack.root, cached)
    (cached / ".mendari-verified").write_text(hashlib.sha256(registry.data).hexdigest() + "\n")
    with registry.client() as signed, pytest.raises(PackValidationError) as refused:
        pull("mendari/referral-router", signed)
    (problem,) = refused.value.problems
    assert problem.endswith(f"; remove {cached.resolve()} from the cache")
    served = resolve_pack("mendari/referral-router")
    assert (served.origin, served.digest) == ("maintained", reference.digest)


def test_pulling_a_reference_pack_keeps_the_maintained_engine(
    hub: Hub, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reference pack, published unchanged (`scripts/hub_seed.py`, `hub-e2e`), is a copy
    of the one this release bundles: pulled, it keeps the maintained engine."""
    token, key = hub.namespace("mendari")
    reference = load_pack(REFERRAL_DIR)
    hub.client(token).push(reference, key)
    assert main(["login", "--registry", hub.url, "--token", token]) == 0
    capsys.readouterr()
    assert main(["pull", "mendari/referral-router"]) == 0
    out = capsys.readouterr().out
    assert "runs on the maintained engine: a copy of the reference pack in this release" in out
    cached = client.packs_cache() / "mendari" / "referral-router" / reference.version
    pack = Decision.from_path(cached).pack
    assert (pack.origin, pack.bounded, pack.digest) == ("hub", False, reference.digest)


def test_a_rotated_key_needs_consent(hub: Hub, tmp_path: Path) -> None:
    token, key = hub.namespace("site-a")
    publisher = hub.client(token)
    publisher.push(copy_pack(tmp_path, "site-a/inbox"), key)
    pull("site-a/inbox", hub.client())  # pins the first key
    private, public = archive.generate_key()
    publisher.add_key("site-a", public)
    publisher.push(copy_pack(tmp_path, "site-a/inbox", "0.2.0"), archive.load_private_key(private))
    with pytest.raises(HubError, match="has not seen before"):
        pull("site-a/inbox@0.2.0", hub.client())
    assert pull("site-a/inbox@0.2.0", hub.client(), accept_new_key=True).version == "0.2.0"


def test_from_hub_pulls_a_pack_that_is_not_local(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, key = hub.namespace("site-a")
    hub.client(token).push(copy_pack(tmp_path, "site-a/inbox"), key)
    with pytest.raises(PackNotFoundError, match="no registry"):
        Decision.from_hub("site-a/inbox")
    monkeypatch.setenv("MENDARI_REGISTRY_URL", hub.url)
    decision = Decision.from_hub("site-a/inbox")
    dataset = (example_datasets_dir() or Path("datasets")) / "local-site-example.jsonl"
    case = json.loads(dataset.read_text().splitlines()[0])
    assert decision(case["input"]).recommendation
    assert decision.pack.origin == "hub" and decision.pack.bounded  # not a reference pack


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
    pack = copy_pack(tmp_path, "site-a/inbox")
    assert main(["push", str(pack.root), "--key", str(key_file)]) == 0
    assert main(["search", "inbox"]) == 0
    assert f"site-a/inbox@{SHIPPED}" in capsys.readouterr().out
    assert main(["pull", "site-a/inbox"]) == 0
    out = capsys.readouterr().out
    assert "verified and cached" in out
    assert "runs on the engine for packs from a registry (RE2, a 1 s CPU budget" in out
    assert main(["search", "--json", "--risk", "4"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_search_shows_no_figure_a_registry_sends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A registry before LP-18 served the publisher's own benchmark beside each entry: the
    listing never shows an uploaded number (ADR-0012), and says the risk level is declared."""
    import contextlib

    from mendari import cli

    found: list[dict[str, object]] = [
        {
            "id": "site-a/fine", "version": "1.0.0", "risk": {"level": 1},
            "publisher_type": "community", "summary": "fine",
            "benchmark": {"dataset": "test", "accuracy": 0.9, "n": 10},
        }
    ]  # fmt: skip

    class Registry:
        def search(self, **_: object) -> list[dict[str, object]]:
            return found

    monkeypatch.setattr(cli, "_registry_client", lambda _: contextlib.nullcontext(Registry()))
    assert main(["search"]) == 0
    out = capsys.readouterr().out
    assert "site-a/fine@1.0.0  risk 1 (declared)  community\n" in out
    assert "benchmark" not in out and "9 of 10" not in out


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


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"a request left for {request.url}")


@pytest.mark.parametrize(
    "url",
    [
        "http://registry.example",
        "http://10.0.0.7:8100",
        "http://192.168.1.20",
        "http://localhost.example.org",
        "ftp://registry.example",
        "registry.example",
        "https://user:secret@registry.example",
        "http://user:secret@127.0.0.1:8100",
        "https://",
    ],
)
def test_a_registry_is_reached_over_https_only(url: str) -> None:
    with pytest.raises(HubError, match="reached over https"):
        RegistryClient(url, "ohr_token", transport=httpx.MockTransport(_never_called))


@pytest.mark.parametrize(
    "url",
    [
        "https://registry.example",
        "https://registry.example/registry/",
        "http://localhost:8100",
        "http://dev.localhost:8100",
        "http://127.0.0.1:8100",
        "http://[::1]:8100",
    ],
)
def test_https_and_this_machine_are_reached(url: str) -> None:
    def healthy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    with RegistryClient(url, transport=httpx.MockTransport(healthy)) as registry:
        assert registry.health() == {"status": "ok"}


def test_no_token_is_sent_to_a_refused_address(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal comes before any connection: with every send failing the test, logging in to
    a plain-http registry, and pulling from one named in the environment, send nothing."""

    def no_send(self: httpx.Client, *args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("a request was sent")

    monkeypatch.setattr(httpx.Client, "send", no_send)
    assert main(["login", "--registry", "http://registry.example", "--token", "ohr_token"]) == 1
    assert "reached over https" in capsys.readouterr().err
    assert not (client.config_dir() / "credentials.json").exists()  # and nothing is remembered

    monkeypatch.setenv("MENDARI_REGISTRY_URL", "http://registry.example")
    monkeypatch.setenv("MENDARI_REGISTRY_TOKEN", "ohr_token")
    with pytest.raises(HubError, match="reached over https"):
        Decision.from_hub("site-a/inbox")


def test_a_saved_http_registry_is_refused_with_what_to_do(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A registry saved before this rule (plain http, not this machine) is refused when it is
    next used, and the message says how to replace it."""
    saved = {"default": "http://registry.example", "registries": {"http://registry.example": {}}}
    client._write_private(client.config_dir() / "credentials.json", saved)
    assert main(["search", "inbox"]) == 1
    error = capsys.readouterr().err
    assert "reached over https" in error and "mendari login --registry https://" in error
