# SPDX-License-Identifier: Apache-2.0
"""An image may be published only when it carries every licence and notice it must, and an SBOM
of the finished image shows no licence that is unknown or not allowed (the owner's decision of
2026-10-07: images wait until they comply). CI checks the image it builds and scans its SBOM
(the `docker` and `image-sbom` jobs); the release checks every image, for both architectures,
before anything is pushed.

    python3 scripts/check_image_licences.py --image mendari-engine:ci
    python3 scripts/check_image_licences.py --image IMG --sbom raw.spdx.json \
        --sbom-out image.spdx.json --sources-out sources.json
    python3 scripts/check_image_licences.py --extract-sbom image.tar --arch arm64 \
        --out raw.spdx.json

**The image** (`--image`): scripts/image_notices.py runs inside it, on its own Python, and fails
when anything is not covered (a Debian package without its copyright file, Debian's common licence
texts gone, a bundled library, crate or package with native code nobody reviewed, a text missing
or not the one fetched from upstream) or the notices are not what the image's contents give.
Here: /licenses/LICENSE and NOTICE are this repository's, SOURCE.md is licenses/image/SOURCE.md,
and the labels say that Apache-2.0 is Mendari's licence only.

**The SBOM** (`--sbom`, SPDX JSON, as BuildKit's SBOM generator writes it for the finished image):
every Debian package, Python package, bundled component and crate the image holds is in it; each
package's licence is permissive, or copyleft with its corresponding source published beside the
image (every Debian package; the components with `corresponding_source`), or a named exception
below with its reason. Anything else, an unknown licence included, fails with the package named.
`--sbom-out` writes the SBOM with each package's source location filled in, as the image's
inventory gives it (the SBOM BuildKit attaches has none; the release publishes this copy in the
image's `-sources` image).

**The SBOM attestation** (`--extract-sbom`): the SPDX document BuildKit attached to one
architecture's image in an OCI archive (`docker buildx build --attest type=sbom`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LABEL_LICENCE = "Apache-2.0"
LABEL_WORDS = ("Mendari application code: Apache-2.0", "/licenses", "THIRD_PARTY_NOTICES")

# Permissive: scripts/check_licences.py's allow-list (a test keeps this a superset of it), and the
# notice-only licences an operating system's packages add.
PERMISSIVE = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BlueOak-1.0.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "ISC",
        "MIT",
        "MIT-0",
        "PSF-2.0",
        "Python-2.0",
        "Unlicense",
        "Zlib",
        "Artistic-2.0",
        "Artistic-dist",
        "Beerware",
        "BSD-4-Clause",
        "BSD-4-Clause-UC",
        "BSD-Attribution-HPND-disclaimer",
        "BSL-1.0",
        "CC-BY-3.0",
        "CC-BY-4.0",
        "curl",
        "FSFAP",
        "FSFUL",
        "FSFULLR",
        "HPND",
        "Latex2e",
        "MIT-CMU",
        "OLDAP-2.8",
        "OpenSSL",
        "PostgreSQL",
        "SunPro",
        "Unicode-3.0",
        "Unicode-DFS-2016",
        "X11",
    }
)
# Copyleft, allowed only where the corresponding source is published beside the image.
RECIPROCAL = re.compile(
    r"^(?:L?GPL-\d\.\d(?:-only|-or-later)?|GFDL-1\.\d(?:-only|-or-later|-invariants.*|-no.*)?|"
    r"MPL-1\.1|MPL-2\.0|MS-PL|Sleepycat|SMAIL-GPL)$"
)
# Debian's licence names, as an SBOM scanner writes them (LicenseRef-<name>), reviewed by name:
# the notice-only ones, and the GNU ones (copyleft, as above).
DEBIAN_PERMISSIVE = frozenset(
    {
        "Artistic",
        "BSLA",
        "BZIP",
        "Carnegie",
        "CORE-MATH",
        "DEC",
        "DONT-CHANGE-THE-GPL",
        "Expat",
        "FSF-manpages",
        "GAP",
        "IBM",
        "Inner-Net",
        "PCRE",
        "pcre",
        "permissive",
        "permissive-nowarranty",
        "REGCOMP",
        "REGCOMP-",
        "SDBM-PUBLIC-DOMAIN",
        "TCL-like",
        "TEXT-TABS",
        "Unicode",
        "Univ-Coimbra",
        "verbatim",
    }
)
DEBIAN_PERMISSIVE_PREFIXES = ("BSD-", "public-domain", "PD", "MIT-", "ISC-")
DEBIAN_RECIPROCAL_PREFIXES = ("GPL", "LGPL", "GFDL")
# Trove classifiers, when a package's metadata says no more (as scripts/check_licences.py reads
# them; every BSD variant is permissive).
CLASSIFIERS = {
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "ISC License (ISCL)": "ISC",
    "MIT License": "MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}
# Reviewed exceptions: (package, the licence the SBOM gives) -> (the licence read, why). A text
# the scanner could not read is named by its hash, so a changed text is reviewed again.
EXCEPTIONS: dict[tuple[str, str], tuple[str, str]] = {
    **{
        (package, "LicenseRef-20390f8a6f3b1e4d7cb45dd8652dabb259bbef688cbad839bcdb0b9ba7252f79"): (
            "GPL-3.0-or-later WITH GCC-exception-3.1",
            "GCC's runtime libraries; Debian's copyright file for gcc-14 is prose, and says so",
        )
        for package in ("gcc-14-base", "libgcc-s1", "libstdc++6")
    },
    ("libcrypt1", "LicenseRef-5a5e7ca0e9f3f9679977e3a3e9ede45ad92885a3297ea78e766979f9866c5a16"): (
        "LGPL-2.1-or-later AND BSD-3-Clause AND BSD-2-Clause AND 0BSD",
        "libxcrypt; its copyright file is upstream's LICENSING inventory, in prose",
    ),
    (
        "debian-archive-keyring",
        "LicenseRef-8f918c0bebd1454a084698a95c09d9a03cec8453dad1fc6eab9e6427da9d33a9",
    ): (
        "GPL-2.0-or-later",
        "Debian's archive keys (no copyright in the keys themselves) and GPL-2+ support files",
    ),
    ("liblzma5", "LicenseRef-noderivs"): (
        "MIT",
        "the notice on the GNU licence documents xz ships (COPYING.GPLv2 and others): verbatim "
        "copies only, which is how they are carried; no restriction on xz itself",
    ),
    ("Simple Launcher", "NOASSERTION"): (
        "PSF-2.0",
        "the Windows launchers distlib ships (t32.exe and others), vendored in the base image's "
        "pip: distlib's own, under its PSF-2.0 licence (pip-vendored-distlib)",
    ),
}
# Packages an SBOM names without a package URL, and where they live.
UNNAMED = {"Simple Launcher": "/pip/_vendor/distlib/"}


# -- the image ---------------------------------------------------------------------------------


def run_in_image(image: str) -> dict[str, Any]:
    """scripts/image_notices.py's check, run inside the image on its own Python."""
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python3",
            "-v",
            f"{ROOT / 'scripts' / 'image_notices.py'}:/tmp/image_notices.py:ro",
            "-v",
            f"{ROOT / 'licenses' / 'image'}:/tmp/licences:ro",
            image,
            "/tmp/image_notices.py",
            "--manifest",
            "/tmp/licences",
            "--check",
            "/licenses",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report: dict[str, Any] = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"{image}: the check did not run:\n{completed.stderr[-2000:]}") from None
    return report


def labels(image: str) -> dict[str, str]:
    completed = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image],
        capture_output=True,
        text=True,
        check=True,
    )
    found: dict[str, str] = json.loads(completed.stdout) or {}
    return found


def image_problems(report: dict[str, Any], found_labels: dict[str, str]) -> list[str]:
    """What the image's own check found, and whether its licence files and labels are right."""
    problems = list(report["problems"])
    expected = {
        "LICENSE": ROOT / "LICENSE",
        "NOTICE": ROOT / "NOTICE",
        "SOURCE.md": ROOT / "licenses" / "image" / "SOURCE.md",
    }
    for name, path in expected.items():
        if report["files"].get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
            problems.append(f"/licenses/{name} is not {path.relative_to(ROOT)}")
    if found_labels.get("org.opencontainers.image.licenses") != LABEL_LICENCE:
        problems.append("the org.opencontainers.image.licenses label is not Apache-2.0")
    description = found_labels.get("org.opencontainers.image.description", "")
    for words in LABEL_WORDS:
        if words not in description:
            problems.append(f"the image description does not say {words!r}")
    return problems


# -- the SBOM ------------------------------------------------------------------------------------


def _purl(package: dict[str, Any]) -> str:
    for ref in package.get("externalRefs", []):
        if ref.get("referenceType") == "purl":
            return str(ref["referenceLocator"])
    return ""


def key(purl: str) -> str:
    """A package URL without qualifiers or subpath, its version decoded, a PyPI name canonical."""
    purl = purl.split("#", 1)[0].split("?", 1)[0]
    match = re.match(r"^(pkg:[^/]+/(?:[^/]+/)?)([^@/]+)@(.+)$", purl)
    if not match:
        return purl
    kind, name, version = match.groups()
    if kind == "pkg:pypi/":
        name = re.sub(r"[-_.]+", "-", name).lower()
    return f"{kind}{urllib.parse.unquote(name)}@{urllib.parse.unquote(version)}"


def _tokens(expression: str) -> list[str]:
    return re.findall(r"\(|\)|[^\s()]+", expression)


def category(token: str) -> int:
    """2 permissive, 1 copyleft, 0 unknown or not allowed."""
    if token in PERMISSIVE:
        return 2
    if RECIPROCAL.match(token):
        return 1
    if token.startswith("LicenseRef-"):
        name = token.removeprefix("LicenseRef-")
        if name in DEBIAN_PERMISSIVE or name.startswith(DEBIAN_PERMISSIVE_PREFIXES):
            return 2
        if name.startswith(DEBIAN_RECIPROCAL_PREFIXES):
            return 1
    return 0


def evaluate(expression: str, bad: list[str], package: str = "") -> int:
    """An SPDX expression's category: OR takes the best choice, AND the worst part, `X WITH Y`
    is X's. A licence with a reviewed exception for `package` counts as the licence reviewed.
    Unknown or unparseable parts are added to `bad`."""
    tokens = _tokens(expression)
    position = 0

    def factor() -> int:
        nonlocal position
        if position >= len(tokens):
            raise ValueError(expression)
        token = tokens[position]
        position += 1
        if token == "(":
            value = either()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError(expression)
            position += 1
            return value
        if token in {")", "AND", "OR", "WITH"}:
            raise ValueError(expression)
        reviewed = EXCEPTIONS.get((package, token))
        value = evaluate(reviewed[0], bad) if reviewed else category(token)
        if value == 0 and not reviewed:
            bad.append(token)
        if position < len(tokens) and tokens[position] == "WITH":
            position += 2  # an exception only grants more
        return value

    def both() -> int:
        nonlocal position
        value = factor()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            value = min(factor(), value)
        return value

    def either() -> int:
        nonlocal position
        value = both()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            value = max(both(), value)
        return value

    try:
        result = either()
    except ValueError:
        bad.append(expression)
        return 0
    if position != len(tokens):
        bad.append(expression)
        return 0
    return result


def _licences(package: dict[str, Any]) -> list[str]:
    found = []
    for field in ("licenseDeclared", "licenseConcluded"):
        value = package.get(field) or "NOASSERTION"
        if value not in {"NOASSERTION", "NONE"}:
            found.append(value)
    return found


def _name(package: dict[str, Any]) -> str:
    """The name a group of an SBOM's packages goes by: its package URL, else its name."""
    return key(_purl(package)) or f"name:{package['name']}"


def scan(sbom: dict[str, Any], found: dict[str, Any]) -> list[str]:
    """The packages in the SBOM whose licence is unknown or not allowed, and what the image holds
    that the SBOM does not list."""
    packages = [p for p in sbom["packages"] if not p["SPDXID"].startswith("SPDXRef-DocumentRoot")]
    groups: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        groups.setdefault(_name(package), []).append(package)

    dists = {f"pkg:pypi/{d['name']}@{d['version']}": d for d in found["dists"]}
    sourced_ids = {s["component"] for s in found["sources"] if s["kind"] == "file"}
    sourced = set()
    for component in found["components"]:
        if component["id"] in sourced_ids:
            sourced.add(key(component["purl"]))
            for carrier in component["in"]:
                name, version = carrier.split(" ", 1)
                sourced.add(
                    "pkg:generic/python@" + version
                    if name == "cpython"
                    else f"pkg:pypi/{name}@{version}"
                )

    problems = []
    for name, group in sorted(groups.items()):
        label = group[0]["name"]
        expressions = sorted({e for p in group for e in _licences(p)})
        if not expressions and name in dists and dists[name]["licence"]:
            parts = dists[name]["licence"].split(" AND ")
            expressions = [" AND ".join(CLASSIFIERS.get(c.strip(), c.strip()) for c in parts)]
        if not expressions:
            expressions = ["NOASSERTION"]
        reviewer = label
        # A package with no package URL is the one reviewed only where it was found.
        unnamed = label in UNNAMED and name.startswith("name:")
        if unnamed and any(UNNAMED[label] not in p.get("sourceInfo", "") for p in group):
            reviewer = ""
        for expression in expressions:
            bad: list[str] = []
            value = evaluate(expression, bad, reviewer)
            if value == 2:
                continue
            if value == 1 and (name.startswith("pkg:deb/") or name in sourced):
                continue
            if value == 1:
                problems.append(
                    f"{label} ({name}): {expression} is copyleft, and its corresponding source "
                    "is not published with the image (components.toml corresponding_source)"
                )
            else:
                problems.append(f"{label} ({name}): {expression}: {', '.join(bad)} not allowed")

    listed = set(groups)
    expected = {
        f"pkg:deb/debian/{p['name']}@{p['version']}": f"Debian package {p['name']}"
        for p in found["debian"]["packages"]
    }
    expected |= {name: f"Python package {d['name']}" for name, d in dists.items()}
    expected |= {key(c["purl"]): c["name"] for c in found["components"] if c["purl"]}
    expected |= {
        f"pkg:cargo/{c['name']}@{c['version']}": f"crate {c['name']}" for c in found["crates"]
    }
    for name, what in sorted(expected.items()):
        if name not in listed:
            problems.append(f"the SBOM does not list {what} ({name})")
    return problems


def source_location(package: dict[str, Any], found: dict[str, Any]) -> str:
    """Where a package's source is, as the image's inventory says (THIRD_PARTY_NOTICES.md lists
    the same), and for the two an SBOM names that have no entry of their own there: distlib's
    launchers in pip, and the Swagger UI the dashboard vendors (in Mendari's package)."""
    name = _name(package)
    for component in found["components"]:
        if component["purl"] and key(component["purl"]) == name:
            return str(component["source"])
    for dist in found["dists"]:
        if name == f"pkg:pypi/{dist['name']}@{dist['version']}":
            return str(dist["source"])
    for crate in found["crates"]:
        if name == f"pkg:cargo/{crate['name']}@{crate['version']}":
            return str(crate["source"])
    if name.startswith("pkg:deb/"):
        binary, version = name.split("/", 2)[2].split("@", 1)
        for p in found["debian"]["packages"]:
            if p["name"] == binary and p["version"] == version:
                return str(p["source_url"])
    if name == "name:Simple Launcher":
        for component in found["components"]:
            if component["id"] == "pip-vendored-distlib":
                return str(component["source"])
    if name.startswith("pkg:npm/mendari-vendored-swagger-ui"):
        return "https://www.npmjs.com/package/swagger-ui-dist"
    return ""


def enrich(sbom: dict[str, Any], found: dict[str, Any]) -> list[str]:
    """Fill in every package's source location; the packages that have none."""
    problems = []
    for package in sbom["packages"]:
        if package["SPDXID"].startswith("SPDXRef-DocumentRoot"):
            continue
        location = source_location(package, found)
        if location:
            package["downloadLocation"] = location
        elif package.get("downloadLocation", "NOASSERTION") in {"NOASSERTION", "NONE"}:
            problems.append(f"{package['name']} ({_name(package)}): no source location")
    creators = sbom.setdefault("creationInfo", {}).setdefault("creators", [])
    tool = "Tool: mendari-check_image_licences (source locations)"
    if tool not in creators:
        creators.append(tool)
    return problems


# -- the SBOM attestation ------------------------------------------------------------------------


def _descriptors(archive: tarfile.TarFile, index: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for descriptor in index.get("manifests", []):
        if (
            descriptor["mediaType"].endswith("index.v1+json")
            or "manifest.list" in descriptor["mediaType"]
        ):
            yield from _descriptors(archive, _blob(archive, descriptor["digest"]))
        else:
            yield descriptor


def _blob(archive: tarfile.TarFile, digest: str) -> dict[str, Any]:
    handle = archive.extractfile("blobs/" + digest.replace(":", "/"))
    if handle is None:
        raise SystemExit(f"no blob {digest}")
    loaded: dict[str, Any] = json.loads(handle.read())
    return loaded


def extract_sbom(path: Path, arch: str) -> dict[str, Any]:
    """The SPDX document BuildKit attached to the linux/<arch> image in an OCI archive."""
    with tarfile.open(path) as archive:
        index = json.loads(archive.extractfile("index.json").read())  # type: ignore[union-attr]
        descriptors = list(_descriptors(archive, index))
        images = [
            d
            for d in descriptors
            if d.get("platform", {}).get("os") == "linux"
            and d.get("platform", {}).get("architecture") == arch
        ]
        if len(images) != 1:
            raise SystemExit(f"{path}: {len(images)} linux/{arch} images, not one")
        image = images[0]["digest"]
        attestations = [
            d
            for d in descriptors
            if d.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest"
            and d["annotations"].get("vnd.docker.reference.digest") == image
        ]
        for attestation in attestations:
            for layer in _blob(archive, attestation["digest"]).get("layers", []):
                kind = layer.get("annotations", {}).get("in-toto.io/predicate-type", "")
                if kind.startswith("https://spdx.dev/Document"):
                    statement = _blob(archive, layer["digest"])
                    document: dict[str, Any] = statement["predicate"]
                    return document
    raise SystemExit(
        f"{path}: no SBOM attestation for linux/{arch} (build with --attest type=sbom)"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", help="a local image to check")
    parser.add_argument("--sbom", type=Path, help="the SBOM of that image (SPDX JSON)")
    parser.add_argument("--sbom-out", type=Path, help="write the SBOM with source locations")
    parser.add_argument("--sources-out", type=Path, help="write what the image's source needs")
    parser.add_argument("--extract-sbom", type=Path, metavar="ARCHIVE", help="an OCI archive")
    parser.add_argument("--arch", help="with --extract-sbom: amd64 or arm64")
    parser.add_argument("--out", type=Path, help="with --extract-sbom: where to write it")
    args = parser.parse_args(argv)
    if args.extract_sbom:
        if not (args.arch and args.out):
            parser.error("--extract-sbom needs --arch and --out")
        args.out.write_text(json.dumps(extract_sbom(args.extract_sbom, args.arch), indent=1) + "\n")
        print(f"ok  {args.out}: the SBOM of {args.extract_sbom.name}'s linux/{args.arch} image")
        return 0
    if not args.image:
        parser.error("name an image (--image) or an archive (--extract-sbom)")
    report = run_in_image(args.image)
    found = report["inventory"]
    problems = image_problems(report, labels(args.image))
    if args.sbom:
        sbom = json.loads(args.sbom.read_text())
        problems += scan(sbom, found)
        problems += enrich(sbom, found)
        if args.sbom_out:
            args.sbom_out.write_text(json.dumps(sbom, indent=1) + "\n")
    if args.sources_out:
        args.sources_out.write_text(
            json.dumps(
                {"image": args.image, "arch": found["arch"], "sources": found["sources"]},
                indent=1,
                sort_keys=True,
            )
            + "\n"
        )
    for problem in problems:
        print(f"{args.image}: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(
        f"ok  {args.image} ({found['arch']}): LICENSE, NOTICE, SOURCE.md and THIRD_PARTY_NOTICES; "
        f"{len(found['debian']['packages'])} Debian packages with their copyright files, "
        f"{len(found['dists'])} Python packages, {len(found['components'])} bundled components, "
        f"{len(found['crates'])} crates"
        + (f"; the SBOM's {len(sbom['packages'])} packages allowed" if args.sbom else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
