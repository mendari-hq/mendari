# SPDX-License-Identifier: Apache-2.0
"""Collect the corresponding source of a release's container images: what the release publishes
beside each image as `<tag>-sources` (licenses/image/SOURCE.md, which every image carries, says
where and how to get it).

    python3 scripts/image_sources.py --out /tmp/sources [--sboms DIR] SOURCES.json ...

Each SOURCES.json is what scripts/check_image_licences.py --sources-out wrote for one image and
architecture; the source of all of them is collected once:

- every Debian source package, at the version installed: its .dsc from Debian's archive
  (deb.debian.org, then its security archive, then snapshot.debian.org, which keeps every
  version), and each file the .dsc names, checked against the SHA-256 the .dsc records;
- every other file components.toml lists as `corresponding_source`, checked against its pinned
  SHA-256.

It writes debian/<package>/, components/, sbom/ (DIR's *.spdx.json) and README.md, which lists every
file with its SHA-256. Anything that cannot be fetched or does not match fails the run, so a
release never publishes images whose source is not beside them.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

DEBIAN = (
    "https://deb.debian.org/debian/pool/main",
    "https://deb.debian.org/debian-security/pool/updates/main",
)
SNAPSHOT = "https://snapshot.debian.org"
USER_AGENT = "mendari-image-sources (https://github.com/mendari-hq/mendari)"

Fetch = Callable[[str], bytes | None]


def fetch(url: str, attempts: int = 4) -> bytes | None:
    """A URL's content, or None when it does not exist (404 or 410). Other errors are retried,
    then fail the run."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                data: bytes = response.read()
            return data
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                return None
            if attempt == attempts:
                raise SystemExit(f"{url}: HTTP {error.code}") from None
        except OSError as error:
            if attempt == attempts:
                raise SystemExit(f"{url}: {error}") from None
        time.sleep(5 * attempt)
    return None


def source_name(value: str) -> str:
    """Names in Debian metadata and the component manifest are single filename components."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_~:-]*", value):
        raise SystemExit("invalid source filename")
    return value


def pool_prefix(package: str) -> str:
    return package[:4] if package.startswith("lib") else package[0]


def without_epoch(version: str) -> str:
    return version.split(":", 1)[1] if ":" in version else version


def dsc_files(dsc: str) -> list[tuple[str, str]]:
    """(SHA-256, file name) of each file a .dsc names (its Checksums-Sha256 field)."""
    found = []
    inside = False
    for line in dsc.splitlines():
        if line.startswith("Checksums-Sha256:"):
            inside = True
            continue
        if inside and line.startswith(" "):
            digest, _size, name = line.split()
            found.append((digest, source_name(name)))
        elif inside:
            break
    if not found:
        raise SystemExit("a .dsc with no Checksums-Sha256 field")
    return found


def _snapshot_files(package: str, version: str, get: Fetch) -> dict[str, tuple[str, str]]:
    """name -> (SHA-1, URL) of a source package's files on snapshot.debian.org, which serves
    each file by its SHA-1."""
    quoted = f"{urllib.parse.quote(package)}/{urllib.parse.quote(version)}"
    listing = get(f"{SNAPSHOT}/mr/package/{quoted}/srcfiles?fileinfo=1")
    if listing is None:
        raise SystemExit(f"Debian source {package} {version}: not on snapshot.debian.org either")
    info = json.loads(listing)["fileinfo"]
    return {
        entries[0]["name"]: (digest, f"{SNAPSHOT}/file/{digest}")
        for digest, entries in info.items()
        if entries
    }


def debian_source(package: str, version: str, out: Path, get: Fetch = fetch) -> list[Path]:
    """Fetch one Debian source package into out/<package>/, every file checked."""
    source_name(package)
    source_name(version)
    dsc_name = f"{package}_{without_epoch(version)}.dsc"
    base = None
    dsc = None
    for archive in DEBIAN:
        candidate = f"{archive}/{pool_prefix(package)}/{package}"
        dsc = get(f"{candidate}/{dsc_name}")
        if dsc is not None:
            base = candidate
            break
    snapshot: dict[str, tuple[str, str]] = {}
    if dsc is None:
        snapshot = _snapshot_files(package, version, get)
        if dsc_name not in snapshot:
            raise SystemExit(f"Debian source {package} {version}: no {dsc_name}")
        digest, url = snapshot[dsc_name]
        dsc = get(url)
        if dsc is None or hashlib.sha1(dsc).hexdigest() != digest:
            raise SystemExit(f"{dsc_name}: not the file snapshot.debian.org lists")
    target = out / package
    target.mkdir(parents=True, exist_ok=True)
    (target / dsc_name).write_bytes(dsc)
    written = [target / dsc_name]
    for digest, name in dsc_files(dsc.decode("utf-8", errors="replace")):
        url = f"{base}/{name}" if base else snapshot.get(name, ("", ""))[1]
        data = get(url) if url else None
        if data is None and base:  # the pool kept the .dsc but not this file: try the snapshot
            snapshot = snapshot or _snapshot_files(package, version, get)
            data = get(snapshot[name][1]) if name in snapshot else None
        if data is None or hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit(f"{package} {version}: {name} is missing or not what its .dsc says")
        (target / name).write_bytes(data)
        written.append(target / name)
    return written


def component_file(item: dict[str, str], out: Path, get: Fetch = fetch) -> Path:
    path = out / source_name(item["file"])
    data = get(item["url"])
    if data is None or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise SystemExit(f"{item['file']}: missing at {item['url']}, or not its pinned SHA-256")
    path.write_bytes(data)
    return path


def merged(lists: list[dict[str, Any]]) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Every image's sources, once: Debian (package, version) pairs, and component files (one per
    file name; the same name with another SHA-256 is an error)."""
    debian = set()
    files: dict[str, dict[str, str]] = {}
    for listing in lists:
        for item in listing["sources"]:
            if item["kind"] == "debian":
                debian.add((item["package"], item["version"]))
            elif item["kind"] == "file":
                seen = files.setdefault(item["file"], item)
                if seen["sha256"] != item["sha256"]:
                    raise SystemExit(f"{item['file']}: two different files with that name")
            else:
                raise SystemExit(f"an unknown kind of source: {item['kind']}")
    return sorted(debian), [files[name] for name in sorted(files)]


def readme(out: Path, debian: list[tuple[str, str]], lists: list[dict[str, Any]]) -> str:
    images = sorted({f"{x['image']} ({x['arch']})" for x in lists})
    lines = [
        "# Corresponding source",
        "",
        "The source of the GPL and LGPL software (and the other software whose licence asks for",
        "its source) in the Mendari container images this release published, at the exact",
        "versions they hold. Every image carries /licenses/SOURCE.md and",
        "/licenses/THIRD_PARTY_NOTICES.md, which say what is what.",
        "",
        "Collected for: " + ", ".join(images) + ".",
        "",
        f"- debian/: {len(debian)} Debian source packages, each with its .dsc",
        "- components/: the other sources (components.toml's corresponding_source)",
        "- sbom/: the images' SBOMs (SPDX)",
        "",
        "| File | SHA-256 |",
        "|---|---|",
    ]
    for path in sorted(p for p in out.rglob("*") if p.is_file() and p.name != "README.md"):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        lines.append(f"| {path.relative_to(out).as_posix()} | {digest} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out", type=Path, required=True, help="a new or empty directory for the verified sources"
    )
    parser.add_argument("--sboms", type=Path, help="a directory whose *.spdx.json to add")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("lists", type=Path, nargs="+", help="check_image_licences --sources-out")
    args = parser.parse_args(argv)
    lists = [json.loads(path.read_text()) for path in args.lists]
    debian, files = merged(lists)
    if args.out.resolve() == Path.cwd().resolve():
        raise SystemExit("--out must be a directory other than the working directory")
    if args.out.is_symlink() or (
        args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir()))
    ):
        raise SystemExit("--out needs a new or empty directory")
    args.out.mkdir(parents=True, exist_ok=True)
    sboms = sorted(args.sboms.glob("*.spdx.json")) if args.sboms else []
    stage = args.out / ".mendari-sources"
    try:
        stage.mkdir(mode=0o700)
    except FileExistsError:
        raise SystemExit("--out needs a new or empty directory") from None
    with ExitStack() as cleanup:
        cleanup.callback(shutil.rmtree, stage)
        if any(path != stage for path in args.out.iterdir()):
            raise SystemExit("--out needs a new or empty directory")
        for folder in ("debian", "components", "sbom"):
            (stage / folder).mkdir(parents=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            jobs: list[concurrent.futures.Future[Any]] = [
                pool.submit(debian_source, p, v, stage / "debian") for p, v in debian
            ]
            jobs += [pool.submit(component_file, item, stage / "components") for item in files]
            for job in concurrent.futures.as_completed(jobs):
                job.result()
        for sbom in sboms:
            shutil.copyfile(sbom, stage / "sbom" / sbom.name)
        (stage / "README.md").write_text(readme(stage, debian, lists))
        # Keep an existing output directory and publish its inventory after every file.
        with ExitStack() as rollback:
            for entry in sorted(stage.iterdir(), key=lambda path: path.name == "README.md"):
                final = args.out / entry.name
                if os.path.lexists(final):
                    raise SystemExit(f"--out already contains {entry.name}")
                entry.rename(final)
                rollback.callback(final.rename, entry)
            rollback.pop_all()
    size = sum(p.stat().st_size for p in args.out.rglob("*") if p.is_file())
    print(
        f"ok  {args.out}: {len(debian)} Debian source packages, {len(files)} other files, "
        f"{len(sboms)} SBOMs; {size / 1e6:.0f} MB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
