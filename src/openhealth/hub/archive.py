"""Pack archives (`.ohpack`): a pack, byte for byte, with a signed list of its files.

An archive is a deterministic gzip tar (sorted entries, zero mtimes, owner and mode fixed, gzip
mtime 0), so the same pack always gives the same bytes and the same archive digest. It holds:

- `openhealth-pack.json`: the format, the pack's id, version and content digest, and every file
  with its SHA-256 and size;
- `openhealth-pack.sig`: an Ed25519 signature over those exact manifest bytes, with the key id;
- `pack/...`: the pack's files (everything but hidden files and caches; evaluation reports and
  the rendered card included, so they are signed too, though the pack digest leaves them out).

`verify` checks the whole archive before anything is written to disk: only regular files under
`pack/`, no traversal, size and count caps, every file hash, and the signature by a key the
caller trusts. `extract` then writes the files and checks that the pack loads with the digest the
manifest names.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from openhealth.core.pack import Pack, load_pack
from openhealth.errors import HubError

FORMAT = "openhealth-pack/1"
MANIFEST = "openhealth-pack.json"
SIGNATURE = "openhealth-pack.sig"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES = 2000


@dataclass(frozen=True)
class Verified:
    manifest: dict[str, Any]
    files: dict[str, bytes]  # path under the pack root -> content
    key_id: str
    sha256: str  # of the archive


# -- keys ---------------------------------------------------------------------------------------


def generate_key() -> tuple[bytes, bytes]:
    """A new publisher key pair as PEM (private, public)."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return private_pem, public_pem(private.public_key())


def public_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        raise HubError("not a PEM private key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise HubError("publisher keys are Ed25519")
    return key


def load_public_key(pem: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(pem)
    except (ValueError, TypeError):
        raise HubError("not a PEM public key") from None
    if not isinstance(key, Ed25519PublicKey):
        raise HubError("publisher keys are Ed25519")
    return key


def key_id(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed25519:" + hashlib.sha256(raw).hexdigest()[:16]


# -- build --------------------------------------------------------------------------------------


def pack_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and not any(
            part.startswith(".") or part == "__pycache__" for part in p.relative_to(root).parts
        )
    )


def archive_name(pack: Pack) -> str:
    namespace, name = pack.id.split("/")
    return f"{namespace}-{name}-{pack.version}.ohpack"


def build(pack: Pack, key: Ed25519PrivateKey) -> bytes:
    """The archive's bytes (deterministic: the same pack and key give the same archive)."""
    files = {f.relative_to(pack.root).as_posix(): f.read_bytes() for f in pack_files(pack.root)}
    manifest = {
        "format": FORMAT,
        "id": pack.id,
        "version": pack.version,
        "digest": pack.digest,
        "files": [
            {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for path, data in files.items()
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    signature = {
        "algorithm": "Ed25519",
        "key_id": key_id(key.public_key()),
        "signature": base64.b64encode(key.sign(manifest_bytes)).decode(),
    }
    entries = [
        (MANIFEST, manifest_bytes),
        (SIGNATURE, json.dumps(signature, sort_keys=True).encode()),
    ]
    entries += [(f"pack/{path}", data) for path, data in files.items()]
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


# -- verify and extract -------------------------------------------------------------------------


def verify(archive: bytes, trusted: Mapping[str, Ed25519PublicKey]) -> Verified:
    """Check an archive completely, in memory; `trusted` maps key ids to public keys."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise HubError(f"the archive is larger than {MAX_ARCHIVE_BYTES} bytes")
    members = _read(archive)
    if MANIFEST not in members or SIGNATURE not in members:
        raise HubError("the archive has no manifest or no signature")
    manifest_bytes = members.pop(MANIFEST)
    try:
        signature = json.loads(members.pop(SIGNATURE))
        manifest = json.loads(manifest_bytes)
    except ValueError:
        raise HubError("the archive's manifest or signature is not JSON") from None
    if manifest.get("format") != FORMAT:
        raise HubError(f"unknown archive format {manifest.get('format')!r}")
    key = trusted.get(str(signature.get("key_id")))
    if key is None or signature.get("algorithm") != "Ed25519":
        raise HubError(
            f"the archive is signed by a key that is not trusted ({signature.get('key_id')})"
        )
    try:
        key.verify(base64.b64decode(str(signature.get("signature", ""))), manifest_bytes)
    except (InvalidSignature, ValueError):
        raise HubError("the archive's signature does not match its manifest") from None
    files = {name.removeprefix("pack/"): data for name, data in members.items()}
    listed = {entry["path"]: entry for entry in manifest.get("files", [])}
    if set(files) != set(listed):
        raise HubError("the archive's files are not the files its manifest lists")
    for path, data in files.items():
        entry = listed[path]
        if (
            entry.get("size") != len(data)
            or entry.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise HubError(f"{path} does not match the manifest")
    digest = hashlib.sha256(archive).hexdigest()
    return Verified(manifest=manifest, files=files, key_id=str(signature["key_id"]), sha256=digest)


def extract(verified: Verified, dest: Path) -> Pack:
    """Write a verified archive's pack to `dest` and load it; its digest, id and version must be
    the ones the signed manifest names."""
    dest.mkdir(parents=True, exist_ok=True)
    for path, data in verified.files.items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    pack = load_pack(dest)
    expected = (
        verified.manifest.get("id"),
        verified.manifest.get("version"),
        verified.manifest.get("digest"),
    )
    if (pack.id, pack.version, pack.digest) != expected:
        raise HubError("the extracted pack is not the pack the manifest names")
    return pack


def _read(archive: bytes) -> dict[str, bytes]:
    try:
        data = gzip.decompress(archive)
    except (OSError, EOFError):
        raise HubError("the archive is not gzip") from None
    if len(data) > MAX_ARCHIVE_BYTES * 4:
        raise HubError("the archive expands beyond the size cap")
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for info in tar:
                if len(members) >= MAX_FILES:
                    raise HubError(f"the archive has more than {MAX_FILES} files")
                if not info.isreg():
                    raise HubError(f"{info.name}: only regular files are allowed")
                _check_name(info.name)
                if info.size > MAX_FILE_BYTES:
                    raise HubError(f"{info.name} is larger than {MAX_FILE_BYTES} bytes")
                if info.name in members:
                    raise HubError(f"{info.name} appears twice")
                handle = tar.extractfile(info)
                members[info.name] = handle.read() if handle else b""
    except tarfile.TarError:
        raise HubError("the archive is not a valid tar") from None
    return members


def _check_name(name: str) -> None:
    path = PurePosixPath(name)
    if name in (MANIFEST, SIGNATURE):
        return
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.parts[:1] != ("pack",)
        or len(path.parts) < 2
    ):
        raise HubError(f"{name}: not a path inside the pack")
    if any(part.startswith(".") for part in path.parts) or "\\" in name:
        raise HubError(f"{name}: not a path inside the pack")
