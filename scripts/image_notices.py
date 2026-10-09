# SPDX-License-Identifier: Apache-2.0
"""What a Mendari container image holds, with the licence and copyright notices that go with it.
The image build runs it to write /licenses (Dockerfile); scripts/check_image_licences.py runs it
again inside the finished image to check that nothing is missing and the notices are current.

It runs inside the image, on the image's own Python, with the standard library only:

    python3 image_notices.py --manifest DIR --write /licenses    # the build
    python3 image_notices.py --manifest DIR --check /licenses    # the check: a JSON report

DIR is the repository's licenses/image (the build binds it). The inventory reads the filesystem:
Debian's package database and each package's copyright file, the Python packages in /opt/venv
and the base image's, the libraries in psycopg_binary.libs, and the SBOMs packages carry. Each
component (components.toml) and crate (crates.toml) is matched to what is there. Anything not
covered (a package without its copyright file, a bundled library or crate nobody reviewed, a
package with native code at a version nobody reviewed) is listed under `missing`: the build
writes the notices all the same, and the check fails.

What it writes in /licenses: THIRD_PARTY_NOTICES.md (everything, for people), inventory.json (the
same, for the release's checks and its corresponding-source job), third-party.cdx.json
(CycloneDX: what an SBOM scanner cannot see by itself, so the release's SBOM of the finished
image lists it too), and third-party/ (the texts of the bundled components and crates). The
Dockerfile copies LICENSE, NOTICE and SOURCE.md beside them.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import re
import shutil
import sys
import tomllib
import urllib.parse
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT = "mendari-image-inventory/1"
# The common licence texts the notices point to; Debian's base-files carries them.
COMMON_LICENCES = ("Apache-2.0", "GPL-2", "GPL-3", "LGPL-2.1", "LGPL-3", "MPL-2.0")
# An SPDX licence a crate may be taken under, and the common-licenses text that is its text.
COMMON_TEXT = {
    "Apache-2.0": "Apache-2.0",
    "MPL-2.0": "MPL-2.0",
    "LGPL-2.1-or-later": "LGPL-2.1",
    "LGPL-2.1-only": "LGPL-2.1",
}
LICENCE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|COPYRIGHT|NOTICE|AUTHORS|UNLICENSE)", re.I)
ARCHES = ("amd64", "arm64")
MACHINE = {"x86_64": "amd64", "aarch64": "arm64", "amd64": "amd64", "arm64": "arm64"}
# Where Mendari's own source is (licenses/image/SOURCE.md says the same).
REPOSITORY = "https://github.com/mendari-hq/mendari"


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dist_source(name: str, version: str) -> str:
    """Where a Python package's source is: its release on PyPI (its sdist, or the files of its
    wheels), Mendari's repository for Mendari."""
    return REPOSITORY if name == "mendari" else f"https://pypi.org/project/{name}/{version}/"


def crate_source(name: str, version: str) -> str:
    """Where a Rust crate's source is: its release on crates.io."""
    return f"https://crates.io/crates/{name}/{version}"


def debian_source(package: str, version: str) -> str:
    """Where a Debian source package is at a version: snapshot.debian.org keeps every one."""
    quote = urllib.parse.quote
    return f"https://snapshot.debian.org/package/{quote(package)}/{quote(version)}/"


def base_purl(purl: str) -> str:
    """A purl without its qualifiers or subpath, a PyPI name in its canonical form."""
    purl = purl.split("#", 1)[0].split("?", 1)[0]
    match = re.match(r"^pkg:pypi/([^@]+)@(.+)$", purl)
    if match:
        return f"pkg:pypi/{canonical(match.group(1))}@{match.group(2)}"
    return purl


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- reading the filesystem --------------------------------------------------------------------


def resolve(root: Path, relative: str) -> Path | None:
    """`relative` under `root`, following symbolic links inside `root` (an absolute link means
    `root`-relative, as it would inside the image). None when it does not exist."""
    parts = list(PurePosixPath(relative.lstrip("/")).parts)
    current = PurePosixPath()
    for _ in range(64):
        if not parts:
            path = root / current
            return path if path.exists() or path.is_symlink() else None
        current = current / parts.pop(0)
        path = root / current
        if path.is_symlink():
            target = PurePosixPath(str(path.readlink()))
            base = PurePosixPath() if target.is_absolute() else current.parent
            joined = (base / target).as_posix().lstrip("/")
            normal: list[str] = []
            for part in PurePosixPath(joined).parts:
                if part == "..":
                    if normal:
                        normal.pop()
                elif part != ".":
                    normal.append(part)
            parts = normal + parts
            current = PurePosixPath()
        elif not path.exists():
            return None
    return None  # a loop of links


def dpkg_packages(root: Path) -> list[dict[str, str]]:
    """The installed packages in Debian's database."""
    status = root / "var/lib/dpkg/status"
    if not status.exists():
        return []
    found = []
    for paragraph in status.read_text(encoding="utf-8").split("\n\n"):
        fields: dict[str, str] = {}
        key = ""
        for line in paragraph.splitlines():
            if line[:1] in (" ", "\t") and key:
                continue  # a continuation line (Description, Conffiles)
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key] = value.strip()
        if "Package" not in fields or fields.get("Status") != "install ok installed":
            continue
        source, version = fields.get("Source", fields["Package"]), fields["Version"]
        match = re.match(r"^(\S+)\s+\((.+)\)$", source)
        if match:
            source, source_version = match.group(1), match.group(2)
        else:
            source_version = version
        found.append(
            {
                "name": fields["Package"],
                "version": version,
                "arch": fields.get("Architecture", "all"),
                "source": source,
                "source_version": source_version,
            }
        )
    return sorted(found, key=lambda p: p["name"])


def copyright_licences(text: str) -> list[str]:
    """The licence short names a machine-readable Debian copyright file names (DEP-5)."""
    if not text.startswith("Format:"):
        return []
    names = set()
    for line in text.splitlines():
        if line.startswith("License:"):
            name = line.split(":", 1)[1].strip()
            if name:
                names.add(name)
    return sorted(names)


def site_packages(root: Path, prefix: str) -> Path | None:
    found = sorted((root / prefix).glob("lib/python3.*/site-packages"))
    return found[0] if found else None


def python_version(root: Path) -> str | None:
    for header in sorted((root / "usr/local/include").glob("python3.*/patchlevel.h")):
        match = re.search(r'#define PY_VERSION\s+"([^"]+)"', header.read_text())
        if match:
            return match.group(1)
    return None


def distributions(root: Path, site: Path | None, env: str) -> list[dict[str, Any]]:
    """The Python distributions installed in a site-packages directory."""
    if site is None:
        return []
    found = []
    for info in sorted(site.glob("*.dist-info")):
        if not (info / "METADATA").exists():
            continue
        metadata = Parser().parsestr((info / "METADATA").read_text(encoding="utf-8"))
        stated = (metadata.get("License") or "").strip()
        classifiers = [
            c.split(" :: ")[-1]
            for c in metadata.get_all("Classifier") or []
            if c.startswith("License :: ")
        ]
        licence = (
            metadata.get("License-Expression")
            or (stated if stated and "\n" not in stated and len(stated) < 80 else "")
            or " AND ".join(classifiers)
        )
        files = sorted(
            "/" + p.relative_to(root).as_posix()
            for p in info.rglob("*")
            if p.is_file() and LICENCE_FILE.match(p.name)
        )
        native = []
        if (info / "RECORD").exists():
            with (info / "RECORD").open(encoding="utf-8", newline="") as record:
                for row in csv.reader(record):
                    if row and re.search(r"\.so(\.|$)", row[0]):
                        native.append(row[0])
        found.append(
            {
                "name": canonical(metadata["Name"]),
                "version": metadata["Version"],
                "env": env,
                "licence": " ".join(licence.split()),
                "licence_files": files,
                "native": bool(native),
                "info": info,
            }
        )
    return found


def embedded_sboms(dists: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(distribution, component) for every component of every CycloneDX SBOM a package carries."""
    found = []
    for dist in dists:
        for path in sorted((dist["info"] / "sboms").glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("bomFormat") != "CycloneDX":
                continue
            pending = list(document.get("components", []))
            while pending:
                component = pending.pop(0)
                pending += component.get("components", [])
                found.append((dist["name"], component))
    return found


def vendored(site: Path | None) -> list[tuple[str, str]]:
    """(name, version) of what pip vendors, from its own list."""
    if site is None or not (site / "pip/_vendor/vendor.txt").exists():
        return []
    found = []
    for line in (site / "pip/_vendor/vendor.txt").read_text().splitlines():
        match = re.match(r"^\s*([A-Za-z0-9._-]+)==(\S+)", line)
        if match:
            found.append((canonical(match.group(1)), match.group(2)))
    return sorted(found)


# -- the inventory ---------------------------------------------------------------------------------


def load_manifest(directory: Path) -> dict[str, Any]:
    components = tomllib.loads((directory / "components.toml").read_text())
    crates = tomllib.loads((directory / "crates.toml").read_text())
    sums = {}
    for line in (directory / "texts/SHA256SUMS").read_text().splitlines():
        digest, path = line.split("  ", 1)
        sums[path] = digest
    return {
        "native": components.get("native", {}),
        "components": components.get("component", []),
        "crates": crates.get("crate", []),
        "sums": sums,
    }


def inventory(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Everything the image holds that has a licence, where each one's notices are in it and
    where its source is, and what is not covered."""
    missing: list[str] = []
    packages = dpkg_packages(root)
    libc = next((p for p in packages if p["name"] == "libc6"), None)
    arch = MACHINE.get(libc["arch"], libc["arch"]) if libc else "unknown"
    release_file = root / "etc/debian_version"
    debian = []
    for package in packages:
        doc = resolve(root, f"usr/share/doc/{package['name']}/copyright")
        if doc is None or not doc.is_file():
            missing.append(
                f"Debian package {package['name']}: no /usr/share/doc/<package>/copyright"
            )
            licences: list[str] = []
        else:
            licences = copyright_licences(doc.read_text(encoding="utf-8", errors="replace"))
        source_url = debian_source(package["source"], package["source_version"])
        debian.append({**package, "licences": licences, "source_url": source_url})
    for name in COMMON_LICENCES:
        if not (root / "usr/share/common-licenses" / name).is_file():
            missing.append(f"/usr/share/common-licenses/{name} is gone")

    venv_site = site_packages(root, "opt/venv")
    base_site = site_packages(root, "usr/local")
    dists = distributions(root, venv_site, "venv") + distributions(root, base_site, "base")
    installed = {d["name"]: d["version"] for d in reversed(dists)}  # the venv's wins
    python = python_version(root)
    if python:
        installed["cpython"] = python

    for dist in dists:
        reviewed = manifest["native"].get(dist["name"])
        if dist["native"] and reviewed != dist["version"]:
            missing.append(
                f"{dist['name']} {dist['version']} has native code, and components.toml's "
                f"[native] reviewed {reviewed or 'no version of it'}: review what it bundles"
            )

    components = []
    for component in manifest["components"]:
        if arch not in component.get("arch", ARCHES):
            continue
        for carrier in component["in"]:
            have = installed.get(carrier["dist"])
            if have is not None and have != carrier["version"]:
                missing.append(
                    f"components.toml: {component['id']} was reviewed in {carrier['dist']} "
                    f"{carrier['version']}, and the image has {have}: review it again"
                )
        if any(installed.get(c["dist"]) == c["version"] for c in component["in"]):
            components.append(component)

    # The libraries auditwheel grafted into packages (<package>.libs).
    libraries = []
    for site in (venv_site, base_site):
        for folder in sorted(site.glob("*.libs")) if site else []:
            dist = canonical(folder.name.removesuffix(".libs"))
            for library in sorted(p.name for p in folder.iterdir()):
                owners = [
                    c["id"]
                    for c in components
                    if any(i["dist"] == dist for i in c["in"])
                    and any(fnmatch.fnmatch(library, g) for g in c.get("libs", []))
                ]
                if not owners:
                    missing.append(f"{folder.name}/{library}: no component in components.toml")
                libraries.append({"file": f"{folder.name}/{library}", "components": owners})

    # What packages' own SBOMs list: crates, grafted RPMs, statically linked libraries.
    crates_by_key = {(c["name"], c["version"]): c for c in manifest["crates"]}
    used_crates: dict[tuple[str, str], set[str]] = {}
    purls = {base_purl(c["purl"]): c for c in components if "purl" in c}
    for dist, entry in embedded_sboms(dists):
        purl = entry.get("purl", "")
        if purl.startswith("pkg:pypi/") and base_purl(purl).split("@")[0] == f"pkg:pypi/{dist}":
            continue  # the package itself
        if purl.startswith("pkg:cargo/"):
            if not entry.get("bom-ref", "").startswith("registry+"):
                continue  # a crate of the package's own workspace: its code, its licence
            key = (entry["name"], entry["version"])
            if key not in crates_by_key:
                missing.append(f"{dist}: crate {key[0]} {key[1]} is not in crates.toml")
            used_crates.setdefault(key, set()).add(dist)
            continue
        owner = purls.get(base_purl(purl)) if purl else None
        if owner is None or not any(i["dist"] == dist for i in owner["in"]):
            label = purl or f"{entry.get('name')} {entry.get('version')}"
            missing.append(f"{dist}'s SBOM lists {label}: no component in components.toml")

    if python and not any(i["dist"] == "cpython" for c in components for i in c["in"]):
        missing.append(f"CPython {python}: no component lists what it incorporates")
    pip = installed.get("pip")
    for name, version in vendored(base_site):
        if f"pkg:pypi/{name}@{version}" not in purls:
            missing.append(f"pip {pip} vendors {name} {version}: no component in components.toml")

    listed_components = []
    for component in sorted(components, key=lambda c: c["id"]):
        if component.get("libs") and not any(
            component["id"] in lib["components"] for lib in libraries
        ):
            missing.append(f"components.toml: {component['id']}'s libraries are not in the image")
        texts = []
        for text in component.get("texts", []):
            relative = f"{component['id']}/{text['file']}"
            if relative not in manifest["sums"]:
                missing.append(f"{relative}: not fetched (scripts/image_licence_texts.py fetch)")
            texts.append({"path": f"/licenses/third-party/{relative}", "from": relative})
        listed_components.append(
            {
                "id": component["id"],
                "name": component["name"],
                "version": component["version"],
                "licence": component["licence"],
                "in": [f"{i['dist']} {i['version']}" for i in component["in"]],
                "libs": [lib["file"] for lib in libraries if component["id"] in lib["components"]],
                "purl": component.get("purl", ""),
                "how": component.get("how", ""),
                "source": component["source"],
                "texts": texts,
            }
        )

    listed_crates = []
    for key in sorted(used_crates):
        crate = crates_by_key.get(key)
        if crate is None:
            continue
        texts = [
            {
                "path": f"/licenses/third-party/crates/{key[0]}-{key[1]}/{t['file']}",
                "from": f"crates/{t['sha256']}",
            }
            for t in crate["texts"]
        ]
        if not texts:
            options = re.findall(r"[A-Za-z0-9.+-]+", crate["licence"])
            common = [COMMON_TEXT[o] for o in options if o in COMMON_TEXT]
            if common:
                texts = [{"path": f"/usr/share/common-licenses/{common[0]}", "from": ""}]
            else:
                missing.append(f"crate {key[0]} {key[1]}: no licence file and no common text")
        listed_crates.append(
            {
                "name": key[0],
                "version": key[1],
                "licence": crate["licence"],
                "in": sorted(used_crates[key]),
                "source": crate_source(key[0], key[1]),
                "texts": texts,
            }
        )

    listed_dists = []
    for dist in sorted(dists, key=lambda d: (d["env"], d["name"])):
        if not dist["licence_files"]:
            missing.append(f"{dist['name']} {dist['version']}: no licence file in its dist-info")
        # A package a component describes (psycopg, certifi, pip) has the component's source.
        owner = purls.get(f"pkg:pypi/{dist['name']}@{dist['version']}")
        source = owner["source"] if owner else dist_source(dist["name"], dist["version"])
        listed_dists.append({**{k: v for k, v in dist.items() if k != "info"}, "source": source})

    sources: list[dict[str, str]] = [
        {"kind": "debian", "package": p["source"], "version": p["source_version"]} for p in packages
    ]
    for component in components:
        for item in component.get("corresponding_source", []):
            sources.append(
                {
                    "kind": "file",
                    "component": component["id"],
                    "file": item["file"],
                    "url": item["url"],
                    "sha256": item["sha256"],
                }
            )
    unique = {json.dumps(s, sort_keys=True): s for s in sources}
    minor = ".".join((python or "0.0").split(".")[:2])
    python_licence = f"/usr/local/lib/python{minor}/LICENSE.txt"
    if python and not (root / python_licence.lstrip("/")).is_file():
        missing.append(f"CPython {python}: {python_licence} is gone")

    return {
        "format": FORMAT,
        "arch": arch,
        "debian": {
            "release": release_file.read_text().strip() if release_file.exists() else "",
            "packages": debian,
        },
        "python": {"version": python or "", "licence": python_licence if python else ""},
        "dists": listed_dists,
        "libraries": libraries,
        "components": listed_components,
        "crates": listed_crates,
        "sources": [unique[k] for k in sorted(unique)],
        "missing": sorted(set(missing)),
    }


# -- what it writes --------------------------------------------------------------------------------


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def notices(found: dict[str, Any]) -> str:
    """THIRD_PARTY_NOTICES.md."""
    lines = [
        "# Third-party notices",
        "",
        "This is a Mendari container image. Mendari's own code is licensed under the Apache",
        "License 2.0 (/licenses/LICENSE; its notices: /licenses/NOTICE). The image also contains",
        "third-party software under its own licences: every component is listed below, with where",
        "its licence and copyright notices are in this image and where its source is. The",
        "corresponding source of the GPL and LGPL software, at these exact versions, is published",
        "beside the image: /licenses/SOURCE.md says where.",
        "",
        f"Architecture: {found['arch']}. Written from the image's own contents when it was built",
        "(scripts/image_notices.py); /licenses/inventory.json holds the same in JSON.",
        "",
    ]
    if found["missing"]:
        lines += ["**Not covered yet** (an image like this one is never published):", ""]
        lines += [f"- {_cell(item)}" for item in found["missing"]] + [""]
    debian = found["debian"]
    lines += [
        f"## Debian packages ({len(debian['packages'])})",
        "",
        f"Debian GNU/Linux {debian['release']}. Each package's licence and copyright notices are",
        "in /usr/share/doc/<package>/copyright, and the common licence texts they refer to in",
        "/usr/share/common-licenses/. The licences column lists the short names a",
        "machine-readable copyright file uses (empty: read the file). Each package's source is",
        "its source package at the version in the source package column: in the image's",
        "`-sources` image under debian/<source package>/ (/licenses/SOURCE.md), and at",
        "https://snapshot.debian.org/package/<source package>/<version>/, which keeps every",
        "version (inventory.json gives each one's address).",
        "",
        "| Package | Version | Source package | Licences named in its copyright file |",
        "|---|---|---|---|",
    ]
    for p in debian["packages"]:
        lines.append(
            f"| {p['name']} | {p['version']} | {p['source']} {p['source_version']} | "
            f"{_cell(', '.join(p['licences']))} |"
        )
    python = found["python"]
    if python["version"]:
        lines += [
            "",
            f"## CPython {python['version']}",
            "",
            f"The base image's Python. Licence: PSF-2.0, {python['licence']}. The software it",
            "incorporates is noticed in the CPython component below (license.rst).",
        ]
    for env, title in (
        ("venv", "in /opt/venv (Mendari and what it runs on)"),
        ("base", "in the base image (/usr/local)"),
    ):
        dists = [d for d in found["dists"] if d["env"] == env]
        if not dists:
            continue
        lines += [
            "",
            f"## Python packages: {title} ({len(dists)})",
            "",
            "| Package | Version | Licence | Licence and notice files | Source |",
            "|---|---|---|---|---|",
        ]
        for d in dists:
            lines.append(
                f"| {d['name']} | {d['version']} | {_cell(d['licence'])} | "
                f"{'<br>'.join(d['licence_files'])} | {d['source']} |"
            )
    lines += [
        "",
        f"## Bundled in or linked into those packages ({len(found['components'])})",
        "",
        "Libraries packages bundle (psycopg-binary's psycopg_binary.libs) or link statically,",
        "software CPython incorporates, and what pip vendors, with their texts from each upstream",
        "project at that version.",
        "",
        "| Component | Version | Licence | In | Notices | Source |",
        "|---|---|---|---|---|---|",
    ]
    for c in found["components"]:
        where = ", ".join(c["in"]) + (f" ({', '.join(c['libs'])})" if c["libs"] else "")
        texts = "<br>".join(t["path"] for t in c["texts"]) or "(the package's own)"
        lines.append(
            f"| {_cell(c['name'])} | {c['version']} | {_cell(c['licence'])} | {_cell(where)} | "
            f"{texts} | {c['source']} |"
        )
    lines += [
        "",
        f"## Rust crates compiled into packages ({len(found['crates'])})",
        "",
        "As each package's own SBOM lists them (its dist-info's sboms/), with the licence files",
        "each crate's package carries and its release on crates.io (its source). A licence with",
        "OR is a choice; Mendari takes it under the first one that is permissive.",
        "",
        "| Crate | Version | Licence | In | Notices | Source |",
        "|---|---|---|---|---|---|",
    ]
    for c in found["crates"]:
        texts = "<br>".join(t["path"] for t in c["texts"])
        lines.append(
            f"| {c['name']} | {c['version']} | {_cell(c['licence'])} | {', '.join(c['in'])} | "
            f"{texts} | {c['source']} |"
        )
    return "\n".join(lines) + "\n"


def cyclonedx(found: dict[str, Any]) -> str:
    """The components an SBOM scanner cannot find by itself, as CycloneDX: an SBOM of the
    finished image reads it, so its bundled libraries and crates are listed with the rest."""
    components = []
    for c in found["components"]:
        if not c["purl"]:
            continue
        components.append(
            {
                "type": "library",
                "bom-ref": f"mendari:{c['id']}",
                "name": c["name"],
                "version": c["version"],
                "licenses": [{"expression": c["licence"]}],
                "purl": c["purl"],
                "externalReferences": [{"type": "distribution", "url": c["source"]}],
                "properties": [{"name": "mendari:in", "value": ", ".join(c["in"])}]
                + [{"name": "mendari:file", "value": lib} for lib in c["libs"]],
            }
        )
    for c in found["crates"]:
        components.append(
            {
                "type": "library",
                "bom-ref": f"mendari:crate:{c['name']}@{c['version']}",
                "name": c["name"],
                "version": c["version"],
                "licenses": [{"expression": c["licence"] or "NOASSERTION"}],
                "purl": f"pkg:cargo/{c['name']}@{c['version']}",
                "externalReferences": [{"type": "distribution", "url": c["source"]}],
                "properties": [{"name": "mendari:in", "value": ", ".join(c["in"])}],
            }
        )
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "file",
                "bom-ref": "mendari:third-party",
                "name": "Mendari image: bundled and statically linked third-party components",
            }
        },
        "components": components,
    }
    return json.dumps(document, indent=1, sort_keys=True) + "\n"


def write(target: Path, manifest_dir: Path, found: dict[str, Any]) -> None:
    third_party = target / "third-party"
    if third_party.exists():
        shutil.rmtree(third_party)
    for entry in found["components"] + found["crates"]:
        for text in entry["texts"]:
            if not text["from"]:
                continue  # a common licence text, already in the image
            destination = Path(str(target) + text["path"].removeprefix("/licenses"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = manifest_dir / "texts" / text["from"]
            if source.exists():
                shutil.copyfile(source, destination)
    (target / "inventory.json").write_text(json.dumps(found, indent=1, sort_keys=True) + "\n")
    (target / "THIRD_PARTY_NOTICES.md").write_text(notices(found))
    (target / "third-party.cdx.json").write_text(cyclonedx(found))


def check(target: Path, root: Path, manifest: dict[str, Any], found: dict[str, Any]) -> list[str]:
    """What is wrong with what the image's /licenses holds."""
    problems = list(found["missing"])
    for name in ("LICENSE", "NOTICE", "SOURCE.md"):
        if not (target / name).is_file():
            problems.append(f"/licenses/{name} is missing")
    written = {
        "inventory.json": json.dumps(found, indent=1, sort_keys=True) + "\n",
        "THIRD_PARTY_NOTICES.md": notices(found),
        "third-party.cdx.json": cyclonedx(found),
    }
    for name, expected in written.items():
        path = target / name
        if not path.is_file():
            problems.append(f"/licenses/{name} is missing")
        elif path.read_text() != expected:
            problems.append(f"/licenses/{name} is not what this image's contents give")
    expected_texts = {}
    for entry in found["components"] + found["crates"]:
        for text in entry["texts"]:
            if text["from"]:
                expected_texts[text["path"]] = manifest["sums"].get(text["from"], "")
            elif not (root / text["path"].lstrip("/")).is_file():
                problems.append(f"{text['path']} is missing")
    for path, digest in sorted(expected_texts.items()):
        local = Path(str(target) + path.removeprefix("/licenses"))
        if not local.is_file():
            problems.append(f"{path} is missing")
        elif sha256(local) != digest:
            problems.append(f"{path} is not the text fetched from its upstream")
    if (target / "third-party").exists():
        present = {
            "/licenses/" + p.relative_to(target).as_posix()
            for p in (target / "third-party").rglob("*")
            if p.is_file()
        }
        problems += [f"{p}: listed nowhere" for p in sorted(present - set(expected_texts))]
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, required=True, help="licenses/image")
    parser.add_argument("--root", type=Path, default=Path("/"), help="the image's filesystem")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", type=Path, metavar="DIR", help="write the notices into DIR")
    action.add_argument("--check", type=Path, metavar="DIR", help="check DIR; a JSON report")
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    found = inventory(args.root, manifest)
    if args.write:
        args.write.mkdir(parents=True, exist_ok=True)
        write(args.write, args.manifest, found)
        for item in found["missing"]:
            print(f"warning: not covered: {item}", file=sys.stderr)
        print(
            f"{args.write}: {len(found['debian']['packages'])} Debian packages, "
            f"{len(found['dists'])} Python packages, {len(found['components'])} bundled "
            f"components, {len(found['crates'])} crates; {len(found['missing'])} not covered"
        )
        return 0
    problems = check(args.check, args.root, manifest, found)
    files = {
        name: sha256(args.check / name) if (args.check / name).is_file() else ""
        for name in ("LICENSE", "NOTICE", "SOURCE.md")
    }
    print(json.dumps({"problems": problems, "files": files, "inventory": found}, sort_keys=True))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
