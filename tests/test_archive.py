# SPDX-License-Identifier: Apache-2.0
"""Signed pack archives (hub/archive.py): reproducible, tamper-evident, safe to open."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import shutil
import tarfile
import tracemalloc
import zlib
from pathlib import Path

import pytest

from mendari.cli import main
from mendari.core.pack import PatternsRefused, load_pack
from mendari.core.registry import resolve_pack
from mendari.errors import HubError
from mendari.hub import archive

REFERRAL = resolve_pack("mendari/referral-router")


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
    assert verified.manifest["id"] == "mendari/referral-router"
    assert "DECISION_CARD.md" in verified.files  # attestations are signed too
    pack = archive.extract(verified, tmp_path / "pack", origin="hub")
    assert (pack.id, pack.version, pack.digest) == (REFERRAL.id, REFERRAL.version, REFERRAL.digest)
    assert pack.origin == "hub" and not pack.bounded  # the reference pack of this release


def test_extract_needs_an_origin(key: archive.Ed25519PrivateKey, tmp_path: Path) -> None:
    """No default: every caller names the engine an archive is checked on (LP-16, ADR-0009)."""
    verified = archive.verify(archive.build(REFERRAL, key), trusted(key))
    with pytest.raises(TypeError, match="origin"):
        archive.extract(verified, tmp_path / "pack")  # type: ignore[call-arg]
    assert not (tmp_path / "pack").exists()


def changed_referral(tmp_path: Path, version: str | None = None) -> Path:
    """The referral pack with one more line in its rules, at `version` (its own unless given)."""
    root = tmp_path / "changed"
    shutil.copytree(REFERRAL.root, root)
    rules = root / "logic" / "triage_rules.yaml"
    rules.write_text(rules.read_text() + "# one more line\n")
    if version is not None:
        manifest = root / "pack.yaml"
        manifest.write_text(
            manifest.read_text().replace(f"version: {REFERRAL.version}", f"version: {version}", 1)
        )
    return root


def test_extract_on_the_hub_engine_refuses_a_changed_reference_pack(
    key: archive.Ed25519PrivateKey, tmp_path: Path
) -> None:
    """One changed byte makes the referral pack a pack the deployment does not own: on RE2 its
    lookahead cannot run, so the archive (at a new version: at its own it is a shadow, refused
    first) does not extract as a pack from a registry."""
    root = changed_referral(tmp_path, "9.9.9")
    changed = load_pack(root)
    verified = archive.verify(archive.build(changed, key), trusted(key))
    with pytest.raises(PatternsRefused) as refused:
        archive.extract(verified, tmp_path / "pack", origin="hub")
    assert any("lookahead" in problem for problem in refused.value.problems)
    pack = archive.extract(verified, tmp_path / "local", origin="local")  # an operator's own
    assert pack.digest == changed.digest and not pack.bounded


def test_pack_verify_checks_an_archive_as_a_puller_loads_it(
    key: archive.Ed25519PrivateKey, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari pack verify` loads an archive on the engine a puller runs it on: a signed copy of
    the referral pack with one changed byte (at a new version) fails, naming the lookahead and
    why; the reference pack itself passes."""
    root = changed_referral(tmp_path, "9.9.9")
    public = tmp_path / "publisher.pub"
    public.write_bytes(archive.public_pem(key.public_key()))
    changed = tmp_path / "changed.ohpack"
    changed.write_bytes(archive.build(load_pack(root), key))
    assert main(["pack", "verify", str(changed), "--public-key", str(public)]) != 0
    err = capsys.readouterr().err
    assert "lookahead" in err and "not the copy of it in this release" in err
    assert "Traceback" not in err
    reference = tmp_path / "reference.ohpack"
    reference.write_bytes(archive.build(REFERRAL, key))
    assert main(["pack", "verify", str(reference), "--public-key", str(public)]) == 0
    assert f"OK {REFERRAL.id}@{REFERRAL.version} {REFERRAL.digest}" in capsys.readouterr().out


def test_pack_verify_refuses_a_shadow_without_a_traceback(
    key: archive.Ed25519PrivateKey, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F: a signed copy of the referral pack with one changed byte at the reference pack's own
    version would take its place where it is pulled: `mendari pack verify` fails, saying why."""
    public = tmp_path / "publisher.pub"
    public.write_bytes(archive.public_pem(key.public_key()))
    shadow = tmp_path / "shadow.ohpack"
    shadow.write_bytes(archive.build(load_pack(changed_referral(tmp_path)), key))
    assert main(["pack", "verify", str(shadow), "--public-key", str(public)]) != 0
    err = capsys.readouterr().err
    assert f"{REFERRAL.id}@{REFERRAL.version} has the id and version of a reference pack" in err
    assert "never takes a reference pack's place" in err and "remove" not in err
    assert "Traceback" not in err


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


def signed_with(key: archive.Ed25519PrivateKey, name: str, data: bytes) -> bytes:
    """The referral pack's archive plus one more entry `name`, listed in the manifest (as
    `verify` would read it) and signed: only the name check stands in the way."""
    return signed_with_all(key, [(name, data)])


def signed_with_all(key: archive.Ed25519PrivateKey, extra: list[tuple[str, bytes]]) -> bytes:
    entries = members(archive.build(REFERRAL, key))
    manifest = json.loads(entries[0][1])
    manifest["files"] += [
        {
            "path": name.removeprefix("pack/"),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        for name, data in extra
    ]
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    signature = {
        "algorithm": "Ed25519",
        "key_id": archive.key_id(key.public_key()),
        "signature": base64.b64encode(key.sign(manifest_bytes)).decode(),
    }
    return repack(
        [
            (archive.MANIFEST, manifest_bytes),
            (archive.SIGNATURE, json.dumps(signature).encode()),
            *entries[2:],
            *extra,
        ]
    )


@pytest.mark.parametrize(
    "names",
    [
        ["pack/PACK.yaml"],
        ["pack/LOGIC"],
        ["pack/caf\u00e9.md", "pack/cafe\u0301.md"],
        ["pack/A", "pack/a/x.yaml"],
        ["pack/Stra\u00dfe.md", "pack/STRASSE.md"],
    ],
    ids=["case", "file-and-directory", "nfc-nfd", "file-then-directory", "casefold"],
)
def test_names_that_are_one_file_where_case_is_not_significant_are_refused(
    key: archive.Ed25519PrivateKey,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    names: list[str],
) -> None:
    """`A` and `a/x.yaml` (or `pack.yaml` and `PACK.yaml`, NFC and NFD spellings) are two files
    on Linux and one on macOS or Windows: `mendari pull` printed a FileExistsError traceback,
    or the last write won. Refused by `verify`, on every platform."""
    data = signed_with_all(key, [(name, b"x: 1\n") for name in names])
    with pytest.raises(HubError, match="where case and Unicode normalization"):
        archive.verify(data, trusted(key))
    built, public = tmp_path / "odd.ohpack", tmp_path / "publisher.pub"
    built.write_bytes(data)
    public.write_bytes(archive.public_pem(key.public_key()))
    assert main(["pack", "verify", str(built), "--public-key", str(public)]) == 1
    err = capsys.readouterr().err
    assert "where case and Unicode normalization" in err and "Traceback" not in err


def test_a_file_extract_cannot_write_is_a_hub_error(
    key: archive.Ed25519PrivateKey, tmp_path: Path
) -> None:
    """An OSError while writing (here a file where a directory goes) is a HubError naming the
    file, which `mendari pull` and `pack verify` report, never a traceback."""
    good = archive.verify(archive.build(REFERRAL, key), trusted(key))
    files = {**good.files, "A": b"x", "A/x.yaml": b"y"}
    odd = archive.Verified(good.manifest, files, good.key_id, good.sha256)
    with pytest.raises(HubError, match=r"A/x\.yaml: cannot be written \("):
        archive.extract(odd, tmp_path / "pack", origin="hub")


@pytest.mark.parametrize("name", ["pack/{outside}", "pack/a//b", "pack/./x", "pack/x/"])
def test_names_with_empty_or_dot_segments_are_refused(
    key: archive.Ed25519PrivateKey, tmp_path: Path, name: str
) -> None:
    # `pack//<absolute path>` once passed (a path library reads it as `pack/<path>`) and was
    # written at the absolute path: a file anywhere on the registry host or a puller's machine.
    outside = tmp_path / "outside" / "pwned.txt"
    name = name.format(outside=outside.as_posix())
    with pytest.raises(HubError, match="not a path inside the pack"):
        archive.verify(signed_with(key, name, b"x"), trusted(key))
    assert not outside.exists()


@pytest.mark.parametrize("path", ["{outside}", "../escape.txt", "logic/../../escape.txt"])
def test_extract_writes_nothing_outside_the_destination(
    key: archive.Ed25519PrivateKey, tmp_path: Path, path: str
) -> None:
    outside = tmp_path / "outside" / "pwned.txt"
    good = archive.verify(archive.build(REFERRAL, key), trusted(key))
    files = {**good.files, path.format(outside=outside.as_posix()): b"x"}
    evil = archive.Verified(good.manifest, files, good.key_id, good.sha256)
    with pytest.raises(HubError, match="not a path inside the pack"):
        archive.extract(evil, tmp_path / "dest" / "pack", origin="hub")
    assert not outside.exists() and not (tmp_path / "dest" / "escape.txt").exists()
    assert not (tmp_path / "dest" / "pack" / "pack.yaml").exists()  # checked before any write


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
    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", 100 * 1024 * 1024)
    monkeypatch.setattr(archive, "MAX_FILE_BYTES", 50 * 1024 * 1024)
    monkeypatch.setattr(archive, "MAX_FILES", 5)
    with pytest.raises(HubError, match="more than 5 files"):
        archive.verify(data, trusted(key))


@pytest.mark.parametrize(
    "kind",
    [tarfile.LNKTYPE, tarfile.SYMTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE],
    ids=["hard-link", "symlink", "character-device", "block-device", "fifo"],
)
def test_only_regular_files_are_read(key: archive.Ed25519PrivateKey, kind: bytes) -> None:
    entries = [*members(archive.build(REFERRAL, key)), ("pack/special", b"")]
    with pytest.raises(HubError, match="pack/special: only regular files"):
        archive.verify(repack(entries, kind=kind), trusted(key))


def _sparse_archive(count: int, style: str) -> bytes:
    """`count` sparse members, each declaring 50 MiB of zeros in a few bytes."""
    raw = io.BytesIO()
    real = str(50 * 1024 * 1024)
    tar_format = tarfile.GNU_FORMAT if style == "gnu" else tarfile.PAX_FORMAT
    with tarfile.open(fileobj=raw, mode="w", format=tar_format) as tar:
        for i in range(count):
            info = tarfile.TarInfo(f"pack/f{i}")
            data = b""
            if style == "pax-1.0":
                info.pax_headers = {
                    "GNU.sparse.major": "1",
                    "GNU.sparse.minor": "0",
                    "GNU.sparse.name": f"pack/f{i}",
                    "GNU.sparse.realsize": real,
                }
                data = b"0\n".ljust(512, b"\0")  # a sparse map with no data blocks
            elif style == "pax-0.1":
                info.pax_headers = {"GNU.sparse.size": real, "GNU.sparse.map": f"0,0,{real},0"}
            else:
                info.type = tarfile.GNUTYPE_SPARSE
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


@pytest.mark.parametrize("style", ["pax-1.0", "pax-0.1", "gnu"])
def test_sparse_members_are_refused_before_anything_is_read(style: str) -> None:
    """R4: tarfile reads a PAX 1.0 sparse member as a regular file of its declared size, so
    2,000 of them in a 46 KB archive made the reader allocate about 100 GB (before any key or
    signature was checked)."""
    tracemalloc.start()
    try:
        with pytest.raises(HubError, match="pack/f0: only regular files"):
            archive.verify(_sparse_archive(20, style), {})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 5 * 1024 * 1024


def _zeros_archive(count: int, mib: int) -> bytes:
    """A gzip tar of `count` regular files of `mib` MiB of zeros each, built as a stream."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    out: list[bytes] = []

    class Sink:
        def write(self, data: bytes) -> int:
            out.append(compressor.compress(data))
            return len(data)

    class Zeros:
        def __init__(self, size: int):
            self.left = size

        def read(self, size: int) -> bytes:
            size = min(size, self.left)
            self.left -= size
            return b"\0" * size

    with tarfile.open(fileobj=Sink(), mode="w|", format=tarfile.USTAR_FORMAT) as tar:  # type: ignore[call-overload]
        for i in range(count):
            info = tarfile.TarInfo(f"pack/zeros{i}")
            info.size = mib * 1024 * 1024
            tar.addfile(info, Zeros(info.size))
    return b"".join(out) + compressor.flush()


def test_a_gzip_bomb_is_refused_while_it_expands() -> None:
    """R4: the whole gzip was decompressed in memory before its size was checked, so a 1 MB
    archive took about 2 GB (and a 100 MB one, the most the registry accepts, about 100 GB).
    It is read as a stream that stops at the cap: here about 500 KB declaring 500 MiB."""
    data = _zeros_archive(100, 5)
    assert len(data) < 1024 * 1024
    tracemalloc.start()
    try:
        with pytest.raises(HubError, match="expands beyond the size cap"):
            archive.verify(data, {})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < archive.EXPANSION_FLOOR + 16 * 1024 * 1024


def test_expansion_is_capped_by_ratio_and_in_all(
    key: archive.Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = archive.build(REFERRAL, key)  # expands about 15 times
    monkeypatch.setattr(archive, "EXPANSION_FLOOR", 1024)
    assert archive.verify(data, trusted(key)).manifest["id"] == REFERRAL.id
    monkeypatch.setattr(archive, "MAX_EXPANSION", 5)
    with pytest.raises(HubError, match="expands beyond the size cap"):
        archive.verify(data, trusted(key))
    monkeypatch.setattr(archive, "MAX_EXPANSION", 100)
    monkeypatch.setattr(archive, "MAX_UNPACKED_BYTES", 1024 * 1024)
    with pytest.raises(HubError, match="expands beyond the size cap"):
        archive.verify(data, trusted(key))


def test_a_truncated_archive_is_not_gzip(key: archive.Ed25519PrivateKey) -> None:
    data = archive.build(REFERRAL, key)
    with pytest.raises(HubError, match="not gzip"):
        archive.verify(data[: len(data) // 2], trusted(key))


def _signed_manifest(key: archive.Ed25519PrivateKey, manifest_bytes: bytes) -> bytes:
    signature = {
        "algorithm": "Ed25519",
        "key_id": archive.key_id(key.public_key()),
        "signature": base64.b64encode(key.sign(manifest_bytes)).decode(),
    }
    entries = members(archive.build(REFERRAL, key))
    return repack(
        [
            (archive.MANIFEST, manifest_bytes),
            (archive.SIGNATURE, json.dumps(signature).encode()),
            *entries[2:],
        ]
    )


@pytest.mark.parametrize(
    ("manifest", "problem"),
    [
        (b"[" * 100_000 + b"]" * 100_000, "not JSON"),
        (b"[]", "not a JSON object"),
        (b'{"format": "mendari-pack/1", "files": {"a": 1}}', "does not list its files"),
        (b'{"format": "mendari-pack/1", "files": [{"path": 1}]}', "does not list its files"),
    ],
    ids=["too-deep", "a-list", "files-not-a-list", "path-not-a-string"],
)
def test_a_malformed_manifest_is_refused_not_a_server_error(
    key: archive.Ed25519PrivateKey, manifest: bytes, problem: str
) -> None:
    """A manifest nested too deep for the JSON reader, or of the wrong shape, raised a raw
    RecursionError, AttributeError or TypeError: a 500 from the registry's publish."""
    with pytest.raises(HubError, match=problem):
        archive.verify(_signed_manifest(key, manifest), trusted(key))


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
        main(["pack", "build", "mendari/referral-router", "--key", key, "--out", str(tmp_path)])
        == 0
    )
    built = tmp_path / f"mendari-referral-router-{REFERRAL.version}.ohpack"
    assert (
        main(["pack", "verify", str(built), "--public-key", str(tmp_path / "publisher.pub")]) == 0
    )
    shown = f"OK mendari/referral-router@{REFERRAL.version} {REFERRAL.digest}"
    assert shown in capsys.readouterr().out
