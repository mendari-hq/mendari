"""Signed pack archives (hub/archive.py): reproducible, tamper-evident, safe to open."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from openhealth.cli import main
from openhealth.core.registry import resolve_pack
from openhealth.errors import HubError
from openhealth.hub import archive

REFERRAL = resolve_pack("openhealth/referral-router")


@pytest.fixture(scope="module")
def key() -> archive.Ed25519PrivateKey:
    return archive.load_private_key(archive.generate_key()[0])


def trusted(key: archive.Ed25519PrivateKey) -> dict[str, archive.Ed25519PublicKey]:
    return {archive.key_id(key.public_key()): key.public_key()}


def members(data: bytes) -> list[tuple[str, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
        return [(m.name, tar.extractfile(m).read()) for m in tar if m.isreg()]  # type: ignore[union-attr]


def repack(entries: list[tuple[str, bytes]], *, kind: bytes = tarfile.REGTYPE) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.type = kind if name == entries[-1][0] else tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def test_the_same_pack_gives_the_same_archive(key: archive.Ed25519PrivateKey) -> None:
    assert archive.build(REFERRAL, key) == archive.build(REFERRAL, key)


def test_an_archive_verifies_and_extracts_to_the_same_pack(
    key: archive.Ed25519PrivateKey, tmp_path: Path
) -> None:
    verified = archive.verify(archive.build(REFERRAL, key), trusted(key))
    assert verified.manifest["id"] == "openhealth/referral-router"
    assert "DECISION_CARD.md" in verified.files  # attestations are signed too
    pack = archive.extract(verified, tmp_path / "pack")
    assert (pack.id, pack.version, pack.digest) == (REFERRAL.id, REFERRAL.version, REFERRAL.digest)


def test_a_changed_file_is_caught(key: archive.Ed25519PrivateKey) -> None:
    entries = members(archive.build(REFERRAL, key))
    entries = [(n, d + b"#" if n == "pack/pack.yaml" else d) for n, d in entries]
    with pytest.raises(HubError, match=r"pack\.yaml does not match the manifest"):
        archive.verify(repack(entries), trusted(key))


def test_a_changed_manifest_breaks_the_signature(key: archive.Ed25519PrivateKey) -> None:
    entries = members(archive.build(REFERRAL, key))
    manifest = json.loads(entries[0][1])
    manifest["version"] = "9.9.9"
    entries[0] = (
        archive.MANIFEST,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
    )
    with pytest.raises(HubError, match="signature does not match"):
        archive.verify(repack(entries), trusted(key))


def test_an_untrusted_key_is_refused(key: archive.Ed25519PrivateKey) -> None:
    other = archive.load_private_key(archive.generate_key()[0])
    with pytest.raises(HubError, match="not trusted"):
        archive.verify(archive.build(REFERRAL, other), trusted(key))


def test_a_file_the_manifest_does_not_list_is_refused(key: archive.Ed25519PrivateKey) -> None:
    entries = [*members(archive.build(REFERRAL, key)), ("pack/extra.py", b"print('hi')")]
    with pytest.raises(HubError, match="not the files its manifest lists"):
        archive.verify(repack(entries), trusted(key))


@pytest.mark.parametrize(
    "name", ["/etc/passwd", "pack/../../evil", "other/file", "pack/.hidden", "pack\\\\x"]
)
def test_paths_outside_the_pack_are_refused(key: archive.Ed25519PrivateKey, name: str) -> None:
    entries = [*members(archive.build(REFERRAL, key)), (name, b"x")]
    with pytest.raises(HubError, match="not a path inside the pack"):
        archive.verify(repack(entries), trusted(key))


def test_links_are_refused(key: archive.Ed25519PrivateKey) -> None:
    entries = [*members(archive.build(REFERRAL, key)), ("pack/link", b"")]
    with pytest.raises(HubError, match="only regular files"):
        archive.verify(repack(entries, kind=tarfile.SYMTYPE), trusted(key))


def test_size_caps(key: archive.Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch) -> None:
    data = archive.build(REFERRAL, key)
    monkeypatch.setattr(archive, "MAX_FILE_BYTES", 1000)
    with pytest.raises(HubError, match="larger than 1000 bytes"):
        archive.verify(data, trusted(key))
    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", 1000)
    with pytest.raises(HubError, match="archive is larger"):
        archive.verify(data, trusted(key))


def test_garbage_is_not_an_archive(key: archive.Ed25519PrivateKey) -> None:
    with pytest.raises(HubError, match="not gzip"):
        archive.verify(b"not an archive", trusted(key))


def test_the_cli_generates_keys_builds_and_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["keys", "generate", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "publisher.key").stat().st_mode & 0o777 == 0o600
    assert main(["keys", "generate", "--out", str(tmp_path)]) != 0  # never overwrites a key
    assert "not overwriting" in capsys.readouterr().err
    key = str(tmp_path / "publisher.key")
    assert (
        main(["pack", "build", "openhealth/referral-router", "--key", key, "--out", str(tmp_path)])
        == 0
    )
    built = tmp_path / "openhealth-referral-router-0.1.1.ohpack"
    assert (
        main(["pack", "verify", str(built), "--public-key", str(tmp_path / "publisher.pub")]) == 0
    )
    assert f"OK openhealth/referral-router@0.1.1 {REFERRAL.digest}" in capsys.readouterr().out
