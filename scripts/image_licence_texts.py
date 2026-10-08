# SPDX-License-Identifier: Apache-2.0
"""Fetch the licence and copyright texts the container images carry for software that does not
carry its own there (licenses/image/): the libraries psycopg-binary bundles, what other packages
link statically, CPython's incorporated software, pip's vendored packages, and the Rust crates
compiled into packages, as their own SBOMs list them. Each text comes from its upstream project at
the version the image ships; licenses/image/texts/SHA256SUMS records what was fetched, and
`--check` (offline, also a test) verifies the committed texts against it.

    uv run python scripts/image_licence_texts.py fetch            # components.toml's texts
    uv run python scripts/image_licence_texts.py crates IMAGE...  # crates.toml, the crates' texts
    uv run python scripts/image_licence_texts.py --check          # the committed texts, intact

`crates` reads the packages' SBOMs (`*.dist-info/sboms/*.json`) in each image given (build one per
architecture), downloads every crate from crates.io, checks it against the SHA-256 the SBOM
records, and keeps its licence files. Run it after a bump of a package built from Rust; the image
check (scripts/check_image_licences.py) fails until it has run.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LICENCES = ROOT / "licenses" / "image"
TEXTS = LICENCES / "texts"
SUMS = TEXTS / "SHA256SUMS"
COMPONENTS = LICENCES / "components.toml"
CRATES = LICENCES / "crates.toml"
CRATE_URL = "https://static.crates.io/crates/{name}/{name}-{version}.crate"
# A crate's licence files: at its root, or in a LICENSES/ folder (REUSE).
LICENCE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|COPYRIGHT|NOTICE|UNLICENSE)([-._ ].*)?$", re.I)
SBOM_PATH = re.compile(
    r"^opt/venv/lib/python[^/]+/site-packages/([^/]+)\.dist-info/sboms/[^/]+\.json$"
)
USER_AGENT = "mendari-licence-texts (https://github.com/Junebugg1214/mendari)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str, attempts: int = 3) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data: bytes = response.read()
            return data
        except (OSError, urllib.error.URLError) as error:
            if attempt == attempts:
                raise SystemExit(f"{url}: {error}") from None
            time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def components() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = tomllib.loads(COMPONENTS.read_text()).get("component", [])
    return loaded


def crates() -> list[dict[str, Any]]:
    if not CRATES.exists():
        return []
    loaded: list[dict[str, Any]] = tomllib.loads(CRATES.read_text()).get("crate", [])
    return loaded


def text_paths() -> list[str]:
    """Every text the manifests list, relative to texts/."""
    paths = {f"{c['id']}/{t['file']}" for c in components() for t in c.get("texts", [])}
    # A crate's texts are stored once by content: many crates carry the same licence file.
    paths |= {f"crates/{t['sha256']}" for c in crates() for t in c["texts"]}
    return sorted(paths)


def read_sums() -> dict[str, str]:
    if not SUMS.exists():
        return {}
    sums = {}
    for line in SUMS.read_text().splitlines():
        digest, path = line.split("  ", 1)
        sums[path] = digest
    return sums


def write_sums() -> None:
    lines = [f"{sha256((TEXTS / p).read_bytes())}  {p}" for p in text_paths()]
    SUMS.write_text("\n".join(lines) + "\n")


def fetched_text(text: dict[str, Any]) -> bytes:
    """A text from its upstream: a file at a URL, or a member of an archive at a URL, optionally
    only some of its lines (a licence that lives in a source file's header)."""
    data = download(text["url"])
    if "archive_sha256" in text and sha256(data) != text["archive_sha256"]:
        raise SystemExit(f"{text['url']}: not the archive recorded (sha256 differs)")
    if "member" in text:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            member = archive.extractfile(text["member"])
            if member is None:
                raise SystemExit(f"{text['url']}: no file {text['member']}")
            data = member.read()
    if "lines" in text:
        first, last = (int(n) for n in str(text["lines"]).split("-"))
        data = b"".join(data.splitlines(keepends=True)[first - 1 : last])
    return data


def fetch() -> int:
    recorded = read_sums()
    changed = []
    for component in components():
        for text in component.get("texts", []):
            relative = f"{component['id']}/{text['file']}"
            data = fetched_text(text)
            if relative in recorded and recorded[relative] != sha256(data):
                changed.append(relative)
            path = TEXTS / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"ok  {relative}")
    write_sums()
    for relative in changed:
        print(f"changed upstream since it was recorded: {relative}", file=sys.stderr)
    return 0


def _image_sboms(image: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """(distribution, SBOM) for every SBOM a package installed in the image carries."""
    container = subprocess.run(
        ["docker", "create", image], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        with subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE) as export:
            assert export.stdout is not None
            with tarfile.open(fileobj=export.stdout, mode="r|") as archive:
                for member in archive:
                    match = SBOM_PATH.match(member.name.lstrip("/"))
                    if match and member.isfile():
                        handle = archive.extractfile(member)
                        assert handle is not None
                        dist = match.group(1).rsplit("-", 1)[0]
                        yield dist, json.loads(handle.read())
    finally:
        subprocess.run(["docker", "rm", container], capture_output=True, check=False)


def registry_crates(sbom: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The crates.io crates an SBOM lists (a crate of the package's own workspace is the
    package's code, under its licence)."""
    for component in sbom.get("components", []):
        if not component.get("purl", "").startswith("pkg:cargo/"):
            continue
        if not component.get("bom-ref", "").startswith("registry+"):
            continue
        hashes = [h["content"] for h in component.get("hashes", []) if h["alg"] == "SHA-256"]
        licences = [
            entry.get("expression") or entry.get("license", {}).get("id")
            for entry in component.get("licenses", [])
        ]
        yield {
            "name": component["name"],
            "version": component["version"],
            "sha256": hashes[0] if hashes else "",
            "licence": _joined([x for x in licences if x]),
        }


def _joined(expressions: list[str]) -> str:
    """One SPDX expression from an SBOM's licence entries (a compound one in parentheses only
    when there is more than one)."""
    if len(expressions) == 1:
        return expressions[0]
    return " AND ".join(f"({x})" if " " in x else x for x in expressions)


def crate_texts(name: str, version: str, data: bytes) -> dict[str, bytes]:
    """A crate's licence files, from its .crate archive."""
    found = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            if not member.isfile() or parts[0] != f"{name}-{version}":
                continue
            inner = parts[1:]
            licence_dir = len(inner) == 2 and inner[0].upper() in {"LICENSES", "LICENSE"}
            if (len(inner) == 1 or licence_dir) and LICENCE_FILE.match(inner[-1]):
                handle = archive.extractfile(member)
                assert handle is not None
                found["/".join(inner)] = handle.read()
    return found


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def update_crates(images: list[str]) -> int:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for image in images:
        for dist, sbom in _image_sboms(image):
            for crate in registry_crates(sbom):
                entry = found.setdefault((crate["name"], crate["version"]), {**crate, "in": set()})
                entry["in"].add(dist.replace("_", "-"))
                if not entry["sha256"]:
                    entry["sha256"] = crate["sha256"]
    if not found:
        raise SystemExit("no crates found: are these Mendari images?")
    # Every crate is fetched and checked before anything is replaced.
    fetched = {}
    for (name, version), crate in sorted(found.items()):
        if not crate["sha256"]:
            raise SystemExit(f"{name} {version}: its SBOM records no SHA-256 to check it against")
        data = download(CRATE_URL.format(name=name, version=version))
        if sha256(data) != crate["sha256"]:
            raise SystemExit(f"{name} {version}: the crate is not the one the SBOM records")
        fetched[(name, version)] = crate_texts(name, version, data)
    if (TEXTS / "crates").exists():
        shutil.rmtree(TEXTS / "crates")
    lines = [
        "# The Rust crates compiled into the images' Python packages, as each package's own SBOM",
        "# lists them (`*.dist-info/sboms/`), with the licence files each crate's package carries.",
        "# Generated by scripts/image_licence_texts.py crates: do not edit by hand.",
        "",
    ]
    for (name, version), crate in sorted(found.items()):
        texts = fetched[(name, version)]
        stored = []
        for file, content in sorted(texts.items()):
            digest = sha256(content)
            path = TEXTS / "crates" / digest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            stored.append(f"{{ file = {_toml_string(file)}, sha256 = {_toml_string(digest)} }}")
        lines += [
            "[[crate]]",
            f"name = {_toml_string(name)}",
            f"version = {_toml_string(version)}",
            f"licence = {_toml_string(crate['licence'])}",
            f"sha256 = {_toml_string(crate['sha256'])}",
            f"in = [{', '.join(_toml_string(d) for d in sorted(crate['in']))}]",
            "texts = [" + "".join(f"\n  {entry}," for entry in stored) + ("\n]" if stored else "]"),
            "",
        ]
        print(f"ok  {name} {version}: {len(texts)} file(s)")
    CRATES.write_text("\n".join(lines))
    write_sums()
    return 0


def check() -> list[str]:
    """What is wrong with the committed texts: a listed text missing or changed, or a file in
    texts/ that nothing lists."""
    problems = []
    recorded = read_sums()
    listed = text_paths()
    for relative in listed:
        path = TEXTS / relative
        if not path.is_file():
            problems.append(f"missing: licenses/image/texts/{relative}")
        elif recorded.get(relative) != sha256(path.read_bytes()):
            problems.append(f"not what SHA256SUMS records: licenses/image/texts/{relative}")
    present = {
        p.relative_to(TEXTS).as_posix() for p in TEXTS.rglob("*") if p.is_file() and p != SUMS
    }
    problems += [f"listed nowhere: licenses/image/texts/{p}" for p in sorted(present - set(listed))]
    problems += [
        f"in SHA256SUMS but listed nowhere: {p}" for p in sorted(set(recorded) - set(listed))
    ]
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="verify the committed texts")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("fetch", help="fetch every text components.toml lists")
    crate = sub.add_parser("crates", help="rebuild crates.toml from the images' packages' SBOMs")
    crate.add_argument("images", nargs="+")
    args = parser.parse_args(argv)
    if args.check:
        problems = check()
        for problem in problems:
            print(problem, file=sys.stderr)
        if not problems:
            print(f"ok  {len(text_paths())} licence texts, as SHA256SUMS records them")
        return 1 if problems else 0
    if args.command == "fetch":
        return fetch()
    if args.command == "crates":
        return update_crates(args.images)
    parser.error("say fetch, crates or --check")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
