# SPDX-License-Identifier: Apache-2.0
"""The container images carry every licence and notice they must, and a release pushes none that
does not (the owner's decision of 2026-10-07, after a licensing analysis the owner received in
October 2026): Mendari's LICENSE and NOTICE in /licenses, THIRD_PARTY_NOTICES.md written from the
image's own contents, the texts of what psycopg-binary bundles and what packages link statically
(licenses/image), Debian's copyright files and common licence texts kept, an SBOM of the finished
image whose licences are allowed, and the corresponding source published beside each image.

The scripts are checked here on synthetic filesystems, SBOMs and archives; CI's docker job and the
release run them on the real images (scripts/check_image_licences.py)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
import tarfile
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "licenses" / "image"


def _script(name: str) -> Any:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notices = _script("image_notices")
check = _script("check_image_licences")
texts = _script("image_licence_texts")
sources = _script("image_sources")
licences = _script("check_licences")

COMPONENTS = tomllib.loads((MANIFEST / "components.toml").read_text())
CRATES = tomllib.loads((MANIFEST / "crates.toml").read_text())["crate"]
LOCKED = {
    re.sub(r"[-_.]+", "-", p["name"]).lower(): p
    for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
}


# -- the committed manifests and texts ---------------------------------------------------------


def test_the_committed_texts_are_the_ones_fetched() -> None:
    assert texts.check() == [], "run: uv run python scripts/image_licence_texts.py fetch"


def test_every_component_says_what_it_is_where_it_comes_from_and_its_texts() -> None:
    ids = [c["id"] for c in COMPONENTS["component"]]
    assert len(ids) == len(set(ids))
    for component in COMPONENTS["component"]:
        for field in ("id", "name", "version", "licence", "in", "source"):
            assert component.get(field), (component["id"], field)
        assert component["source"].startswith("https://"), component["id"]
        assert check.evaluate(component["licence"], []) > 0, component["id"]
        for text in component.get("texts", []):
            assert text["url"].startswith("https://"), (component["id"], text)
        for item in component.get("corresponding_source", []):
            assert item["url"].startswith("https://") and re.fullmatch(
                r"[0-9a-f]{64}", item["sha256"]
            )
        for arch in component.get("arch", []):
            assert arch in notices.ARCHES


def test_copyleft_components_publish_their_source() -> None:
    """Every component under a copyleft licence names its exact source, pinned by SHA-256."""
    for component in COMPONENTS["component"]:
        if check.evaluate(component["licence"], []) == 1:
            assert component.get("corresponding_source"), component["id"]
    for name in ("libxcrypt-4.5.2", "keyutils-1.5.8-3.el7", "keyutils-1.5.10-9.el8"):
        assert any(c["id"] == name and c["corresponding_source"] for c in COMPONENTS["component"])


def test_what_psycopg_binary_bundles_is_inventoried_for_both_architectures() -> None:
    """The libraries in psycopg_binary.libs, as the wheels for each architecture in uv.lock hold
    them (each component names its files)."""
    libraries = {
        "amd64": [
            "libcom_err-2abe824b.so.2.1",
            "libcrypt-bc2db45e.so.1",
            "libcrypto-2d1629ee.so.3",
            "libgssapi_krb5-497db0c6.so.2.2",
            "libk5crypto-b1f99d5c.so.3.1",
            "libkeyutils-dfe70bd6.so.1.5",
            "libkrb5-fcafa220.so.3.3",
            "libkrb5support-d0bcff84.so.0.1",
            "liblber-a44a5afb.so.2.0.200",
            "libldap-7c3a4b12.so.2.0.200",
            "libpcre-9513aab5.so.1.2.0",
            "libpq-e9b5c9ac.so.5.18",
            "libsasl2-84219a89.so.3.0.0",
            "libselinux-0922c95c.so.1",
            "libssl-8bd944e8.so.3",
        ],
        "arm64": [
            "libcom_err-6d8d18aa.so.2.1",
            "libcrypt-258f54d5.so.1.1.0",
            "libcrypto-02bae65c.so.1.1.1k",
            "libcrypto-cff3d539.so.3",
            "libgssapi_krb5-42536122.so.2.2",
            "libk5crypto-f5362a5f.so.3.1",
            "libkeyutils-fe6e95a9.so.1.6",
            "libkrb5-98c41d04.so.3.3",
            "libkrb5support-beb3078f.so.0.1",
            "liblber-e2ad9298.so.2.0.200",
            "libldap-1357b183.so.2.0.200",
            "libpcre2-8-8701a61e.so.0.7.1",
            "libpq-53e4e5f8.so.5.18",
            "libsasl2-076b3c1f.so.3.0.0",
            "libselinux-be7a4dc7.so.1",
            "libssl-c64c0f6b.so.3",
        ],
    }
    version = COMPONENTS["native"]["psycopg-binary"]
    assert LOCKED["psycopg-binary"]["version"] == version
    for arch, files in libraries.items():
        bundled = [
            c
            for c in COMPONENTS["component"]
            if arch in c.get("arch", notices.ARCHES)
            and c.get("libs")
            and {"dist": "psycopg-binary", "version": version} in c["in"]
        ]
        for library in files:
            owners = [
                c["id"]
                for c in bundled
                if any(
                    re.fullmatch(g.replace(".", r"\.").replace("*", ".*"), library)
                    for g in c["libs"]
                )
            ]
            assert len(owners) == 1, (arch, library, owners)
    names = {c["name"].split(" (")[0] for c in COMPONENTS["component"]}
    for expected in (
        "PostgreSQL libpq",
        "OpenSSL",
        "OpenSSL 1.1.1",
        "libxcrypt",
        "keyutils",
        "Cyrus SASL",
        "MIT Kerberos",
        "OpenLDAP",
        "PCRE2",
        "libselinux",
    ):
        assert expected in names, expected


def test_every_native_package_in_the_lock_was_reviewed_at_its_locked_version() -> None:
    """A package whose Linux wheels hold native code may bundle or link anything: each is in
    components.toml's [native] at the version uv.lock installs, and so is every package a
    component says it is in. A bump fails here, before any image is built."""
    closure = {p.name for p in licences.python_closure()}
    native = {
        name
        for name in closure
        if any("manylinux" in w["url"] for w in LOCKED[name].get("wheels", []))
    }
    assert native == set(COMPONENTS["native"]), native ^ set(COMPONENTS["native"])
    for name, version in COMPONENTS["native"].items():
        assert LOCKED[name]["version"] == version, name
    for component in COMPONENTS["component"]:
        for carrier in component["in"]:
            if carrier["dist"] in closure:  # not the base image's CPython and pip
                assert LOCKED[carrier["dist"]]["version"] == carrier["version"], component["id"]
    for crate in CRATES:
        assert set(crate["in"]) <= set(COMPONENTS["native"]), crate["name"]


def test_crates_are_checked_against_their_sboms_and_carry_their_texts() -> None:
    for crate in CRATES:
        assert re.fullmatch(r"[0-9a-f]{64}", crate["sha256"]), crate["name"]
        assert check.evaluate(crate["licence"], []) == 2, crate["name"]  # a permissive choice
        if not crate["texts"]:  # no licence file in its package: a common text stands in
            options = re.findall(r"[A-Za-z0-9.+-]+", crate["licence"])
            assert any(o in notices.COMMON_TEXT for o in options), crate["name"]


# -- the inventory, on a synthetic image ---------------------------------------------------------

STATUS = """Package: libc6
Status: install ok installed
Architecture: arm64
Version: 2.41-12
Description: GNU C Library
 continuation line

Package: bash
Status: install ok installed
Architecture: arm64
Source: bash (5.2.37-2)
Version: 5.2.37-2+b10

Package: removed
Status: deinstall ok config-files
Version: 1.0
"""


def _write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return path


def _dist(site: Path, name: str, version: str, licence: str, so: bool = False) -> Path:
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    _write(
        info / "METADATA",
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nLicense-Expression: {licence}\n",
    )
    _write(info / "licenses" / "LICENSE", f"{name} licence\n")
    record = f"{name.replace('-', '_')}/__init__.py,,\n"
    if so:
        record += f"{name.replace('-', '_')}/_native.cpython-312-aarch64-linux-gnu.so,,\n"
    _write(info / "RECORD", record)
    return info


@pytest.fixture
def image(tmp_path: Path) -> tuple[Path, Path]:
    """A synthetic image filesystem and a manifest that covers it."""
    root = tmp_path / "root"
    _write(root / "var/lib/dpkg/status", STATUS)
    _write(root / "etc/debian_version", "13.7\n")
    _write(root / "usr/share/doc/libc6/copyright", "Format: https://dep5\n\nLicense: LGPL-2.1+\n")
    _write(root / "usr/share/doc/bash-docs/copyright", "Format: x\n\nLicense: GPL-3+\n")
    (root / "usr/share/doc/bash").symlink_to("bash-docs")  # a doc folder shared by link
    for name in notices.COMMON_LICENCES:
        _write(root / "usr/share/common-licenses" / name, name)
    _write(root / "usr/local/include/python3.12/patchlevel.h", '#define PY_VERSION "3.12.15"\n')
    _write(root / "usr/local/lib/python3.12/LICENSE.txt", "PSF\n")
    site = root / "opt/venv/lib/python3.12/site-packages"
    _dist(site, "pure", "1.0", "MIT")
    info = _dist(site, "carrier", "2.0", "LGPL-3.0-only", so=True)
    _write(site / "carrier.libs/libz-1234abcd.so.1", b"\x7fELF")
    sbom = {
        "bomFormat": "CycloneDX",
        "components": [
            {"name": "carrier", "version": "2.0", "purl": "pkg:pypi/carrier@2.0?file_name=x.whl"},
            {"name": "zlib", "version": "1.2-3.el8", "purl": "pkg:rpm/almalinux/zlib@1.2-3.el8"},
            {
                "name": "ahash",
                "version": "0.8.12",
                "purl": "pkg:cargo/ahash@0.8.12",
                "bom-ref": "registry+https://github.com/rust-lang/crates.io-index#ahash@0.8.12",
            },
            {
                "name": "own",
                "version": "2.0",
                "purl": "pkg:cargo/own@2.0",
                "bom-ref": "path+file:///x",
            },
        ],
    }
    _write(info / "sboms/carrier.cdx.json", json.dumps(sbom))

    manifest = tmp_path / "manifest"
    licence = _write(manifest / "texts/zlib/LICENSE", "zlib licence\n")
    crate = _write(manifest / "texts/crates" / ("a" * 64), "MIT, ahash\n")
    _write(
        manifest / "texts/SHA256SUMS",
        f"{hashlib.sha256(licence.read_bytes()).hexdigest()}  zlib/LICENSE\n"
        f"{hashlib.sha256(crate.read_bytes()).hexdigest()}  crates/{'a' * 64}\n",
    )
    _write(
        manifest / "components.toml",
        """
[native]
carrier = "2.0"

[[component]]
id = "zlib"
name = "zlib"
version = "1.2-3.el8"
licence = "Zlib"
in = [{ dist = "carrier", version = "2.0" }]
libs = ["libz-*.so.1"]
purl = "pkg:rpm/almalinux/zlib@1.2-3.el8"
source = "https://example.org/zlib.src.rpm"
texts = [{ file = "LICENSE", url = "https://example.org/LICENSE" }]

[[component]]
id = "carrier-source"
name = "carrier"
version = "2.0"
licence = "LGPL-3.0-only"
in = [{ dist = "carrier", version = "2.0" }]
purl = "pkg:pypi/carrier@2.0"
source = "https://example.org/carrier"
corresponding_source = [
  { file = "carrier-2.0.tar.gz", url = "https://example.org/c.tgz", sha256 = "%s" },
]

[[component]]
id = "cpython"
name = "CPython"
version = "3.12.15"
licence = "PSF-2.0"
in = [{ dist = "cpython", version = "3.12.15" }]
purl = "pkg:generic/python@3.12.15"
source = "https://www.python.org/"
"""
        % ("b" * 64),
    )
    _write(
        manifest / "crates.toml",
        f"""
[[crate]]
name = "ahash"
version = "0.8.12"
licence = "MIT OR Apache-2.0"
sha256 = "{"c" * 64}"
in = ["carrier"]
texts = [{{ file = "LICENSE-MIT", sha256 = "{"a" * 64}" }}]
""",
    )
    return root, manifest


def _inventory(image: tuple[Path, Path]) -> dict[str, Any]:
    root, manifest = image
    found: dict[str, Any] = notices.inventory(root, notices.load_manifest(manifest))
    return found


def test_a_covered_image_has_nothing_missing_and_lists_everything(image: tuple[Path, Path]) -> None:
    found = _inventory(image)
    assert found["missing"] == []
    assert found["arch"] == "arm64"
    assert [p["name"] for p in found["debian"]["packages"]] == ["bash", "libc6"]
    bash = found["debian"]["packages"][0]
    assert (bash["source"], bash["source_version"], bash["licences"]) == (
        "bash",
        "5.2.37-2",
        ["GPL-3+"],
    )
    assert [c["id"] for c in found["components"]] == ["carrier-source", "cpython", "zlib"]
    assert found["libraries"] == [
        {"file": "carrier.libs/libz-1234abcd.so.1", "components": ["zlib"]}
    ]
    assert [(c["name"], c["in"]) for c in found["crates"]] == [("ahash", ["carrier"])]
    assert {"kind": "debian", "package": "bash", "version": "5.2.37-2"} in found["sources"]
    assert any(s.get("file") == "carrier-2.0.tar.gz" for s in found["sources"])


@pytest.mark.parametrize(
    ("change", "missing"),
    [
        (lambda r, m: (r / "usr/share/doc/bash-docs/copyright").unlink(), "Debian package bash"),
        (lambda r, m: (r / "usr/share/common-licenses/GPL-3").unlink(), "common-licenses/GPL-3"),
        (
            lambda r, m: _write(
                r / "opt/venv/lib/python3.12/site-packages/carrier.libs/libnew-1.so", "x"
            ),
            "carrier.libs/libnew-1.so: no component",
        ),
        (lambda r, m: _write(m / "crates.toml", ""), "crate ahash 0.8.12 is not in crates.toml"),
        (
            lambda r, m: _write(
                m / "components.toml",
                (m / "components.toml").read_text().replace('carrier = "2.0"', 'carrier = "1.9"'),
            ),
            "has native code",
        ),
        (
            lambda r, m: (
                r / "opt/venv/lib/python3.12/site-packages/pure-1.0.dist-info/licenses/LICENSE"
            ).unlink(),
            "pure 1.0: no licence file",
        ),
        (
            lambda r, m: _write(
                r / "usr/local/include/python3.12/patchlevel.h", '#define PY_VERSION "3.12.16"\n'
            ),
            "CPython 3.12.16",
        ),
    ],
)
def test_anything_not_covered_is_missing(
    image: tuple[Path, Path], change: Any, missing: str
) -> None:
    change(*image)
    found = _inventory(image)
    assert any(missing in item for item in found["missing"]), found["missing"]


def test_a_component_reviewed_for_another_version_is_missing(image: tuple[Path, Path]) -> None:
    _, manifest = image
    text = (manifest / "components.toml").read_text()
    _write(
        manifest / "components.toml",
        text.replace('id = "carrier-source"', 'id = "carrier-source"').replace(
            'in = [{ dist = "carrier", version = "2.0" }]\npurl = "pkg:pypi',
            'in = [{ dist = "carrier", version = "1.0" }]\npurl = "pkg:pypi',
        ),
    )
    assert any("reviewed in carrier 1.0" in m for m in _inventory(image)["missing"])


def test_written_notices_pass_the_check_until_something_changes(
    image: tuple[Path, Path], tmp_path: Path
) -> None:
    root, manifest = image
    target = tmp_path / "licenses"
    for name in ("LICENSE", "NOTICE", "SOURCE.md"):
        _write(target / name, name)
    loaded = notices.load_manifest(manifest)
    found = notices.inventory(root, loaded)
    notices.write(target, manifest, found)
    assert (target / "third-party/zlib/LICENSE").read_text() == "zlib licence\n"
    assert (target / "third-party/crates/ahash-0.8.12/LICENSE-MIT").read_text() == "MIT, ahash\n"
    written = (target / "THIRD_PARTY_NOTICES.md").read_text()
    assert "/licenses/third-party/zlib/LICENSE" in written and "| bash | 5.2.37-2+b10 |" in written
    # Every component says where its source is: Debian's by its source package (and the
    # inventory's address for it), the bundled ones, each Python package and each crate.
    assert "| bash 5.2.37-2 |" in written and "https://snapshot.debian.org/package/<" in written
    assert found["debian"]["packages"][0]["source_url"] == (
        "https://snapshot.debian.org/package/bash/5.2.37-2/"
    )
    python = written[written.index("## Python packages") : written.index("## Bundled")]
    site = "/opt/venv/lib/python3.12/site-packages"
    assert "| Package | Version | Licence | Licence and notice files | Source |" in python
    assert (
        f"| pure | 1.0 | MIT | {site}/pure-1.0.dist-info/licenses/LICENSE | "
        "https://pypi.org/project/pure/1.0/ |"
    ) in python
    # A package a component describes has the component's source (as the SBOM gets it).
    assert (
        f"| carrier | 2.0 | LGPL-3.0-only | {site}/carrier-2.0.dist-info/licenses/LICENSE | "
        "https://example.org/carrier |"
    ) in python
    crates = written[written.index("## Rust crates") :]
    assert "| Crate | Version | Licence | In | Notices | Source |" in crates
    assert (
        "| ahash | 0.8.12 | MIT OR Apache-2.0 | carrier | "
        "/licenses/third-party/crates/ahash-0.8.12/LICENSE-MIT | "
        "https://crates.io/crates/ahash/0.8.12 |"
    ) in crates
    assert {d["name"]: d["source"] for d in found["dists"]} == {
        "carrier": "https://example.org/carrier",
        "pure": "https://pypi.org/project/pure/1.0/",
    }
    assert notices.dist_source("mendari", "0.1.0") == notices.REPOSITORY
    cdx = json.loads((target / "third-party.cdx.json").read_text())
    assert {c["purl"] for c in cdx["components"]} >= {
        "pkg:rpm/almalinux/zlib@1.2-3.el8",
        "pkg:cargo/ahash@0.8.12",
    }
    assert notices.check(target, root, loaded, found) == []

    _write(target / "third-party/zlib/LICENSE", "edited\n")
    _write(target / "third-party/stray.txt", "x")
    (target / "NOTICE").unlink()
    _write(
        root / "usr/share/doc/libc6/copyright", "Format: x\n\nLicense: LGPL-2.1+\nLicense: MIT\n"
    )
    problems = notices.check(target, root, loaded, notices.inventory(root, loaded))
    assert (
        "/licenses/third-party/zlib/LICENSE is not the text fetched from its upstream" in problems
    )
    assert "/licenses/third-party/stray.txt: listed nowhere" in problems
    assert "/licenses/NOTICE is missing" in problems
    assert "/licenses/inventory.json is not what this image's contents give" in problems


def test_a_link_out_of_the_root_resolves_inside_it(tmp_path: Path) -> None:
    _write(tmp_path / "usr/share/doc/real/copyright", "x")
    (tmp_path / "usr/share/doc/abs").symlink_to("/usr/share/doc/real")
    (tmp_path / "usr/share/doc/loop").symlink_to("loop")
    assert (
        notices.resolve(tmp_path, "usr/share/doc/abs/copyright")
        == tmp_path / "usr/share/doc/real/copyright"
    )
    assert notices.resolve(tmp_path, "usr/share/doc/loop/copyright") is None
    assert notices.resolve(tmp_path, "usr/share/doc/none/copyright") is None


# -- the licence scan of an SBOM -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "value"),
    [
        ("MIT", 2),
        ("Apache-2.0 OR GPL-2.0-only", 2),
        ("GPL-2.0-or-later AND MIT", 1),
        ("LGPL-2.1-or-later WITH GCC-exception-3.1", 1),
        ("(MIT OR Apache-2.0) AND Unicode-3.0", 2),
        ("LicenseRef-Expat AND LicenseRef-BSD-3-clause-Regents", 2),
        ("LicenseRef-GPL-3--WITH-BISON-EXCEPTION", 1),
        ("AGPL-3.0-only", 0),
        ("SSPL-1.0 OR AGPL-3.0-or-later", 0),
        ("LicenseRef-AGPL-3", 0),
        ("LicenseRef-noderivs", 0),
        ("NOASSERTION", 0),
        ("MIT AND", 0),
        ("(MIT", 0),
    ],
)
def test_a_licence_is_permissive_copyleft_or_refused(expression: str, value: int) -> None:
    assert check.evaluate(expression, []) == value


def test_the_image_allow_list_includes_the_python_allow_list() -> None:
    assert licences.PERMISSIVE <= check.PERMISSIVE


def test_an_exception_counts_only_for_its_package() -> None:
    token = "LicenseRef-20390f8a6f3b1e4d7cb45dd8652dabb259bbef688cbad839bcdb0b9ba7252f79"
    assert check.evaluate(token, [], "libgcc-s1") == 1  # GCC's runtime, reviewed
    bad: list[str] = []
    assert check.evaluate(token, bad, "another") == 0 and bad == [token]
    assert all(reason for _, reason in check.EXCEPTIONS.values())


def _package(name: str, purl: str, licence: str = "NOASSERTION", **more: Any) -> dict[str, Any]:
    return {
        "name": name,
        "SPDXID": f"SPDXRef-{name}",
        "licenseDeclared": licence,
        "licenseConcluded": "NOASSERTION",
        "downloadLocation": "NOASSERTION",
        "externalRefs": [{"referenceType": "purl", "referenceLocator": purl}] if purl else [],
        **more,
    }


def _found() -> dict[str, Any]:
    return {
        "debian": {
            "packages": [
                {
                    "name": "bash",
                    "version": "5.2-1+b1",
                    "source": "bash",
                    "source_version": "1:5.2-1",
                    "source_url": notices.debian_source("bash", "1:5.2-1"),
                }
            ]
        },
        "dists": [
            {
                "name": "carrier",
                "version": "2.0",
                "licence": "LGPL-3.0-only",
                "source": "https://example.org/carrier",
            },
            {
                "name": "re2like",
                "version": "1.0",
                "licence": "BSD License",
                "source": notices.dist_source("re2like", "1.0"),
            },
        ],
        "components": [
            {
                "id": "carrier-source",
                "name": "carrier",
                "purl": "pkg:pypi/carrier@2.0",
                "in": ["carrier 2.0"],
                "source": "https://example.org/carrier",
            },
        ],
        "crates": [
            {
                "name": "ahash",
                "version": "0.8.12",
                "licence": "MIT OR Apache-2.0",
                "source": notices.crate_source("ahash", "0.8.12"),
            }
        ],
        "sources": [{"kind": "file", "component": "carrier-source", "file": "c.tgz"}],
    }


def _sbom(*packages: dict[str, Any]) -> dict[str, Any]:
    root = {"name": "sbom", "SPDXID": "SPDXRef-DocumentRoot-Directory-sbom"}
    return {"packages": [root, *packages], "creationInfo": {"creators": []}}


def test_a_scanned_sbom_passes_when_every_licence_is_allowed_and_everything_is_listed() -> None:
    sbom = _sbom(
        _package("bash", "pkg:deb/debian/bash@5.2-1%2Bb1?arch=arm64", "GPL-3.0-or-later"),
        _package("carrier", "pkg:pypi/carrier@2.0", "LGPL-3.0-only"),
        _package("carrier", "pkg:pypi/Carrier@2.0?file_name=x.whl"),  # merged: same package
        _package("re2like", "pkg:pypi/re2like@1.0"),  # read from its classifier: BSD
        _package("ahash", "pkg:cargo/ahash@0.8.12", "MIT OR Apache-2.0"),
    )
    found = _found()
    assert check.scan(sbom, found) == []
    assert check.enrich(sbom, found) == []
    # Each source location is the one the image's inventory (and THIRD_PARTY_NOTICES.md) gives.
    locations = {p["name"]: p["downloadLocation"] for p in sbom["packages"][1:]}
    assert locations == {
        "bash": "https://snapshot.debian.org/package/bash/1%3A5.2-1/",
        "carrier": "https://example.org/carrier",
        "re2like": "https://pypi.org/project/re2like/1.0/",
        "ahash": "https://crates.io/crates/ahash/0.8.12",
    }


def test_a_scanned_sbom_names_each_package_it_refuses() -> None:
    found = _found()
    found["sources"] = []  # carrier's source is not published
    sbom = _sbom(
        _package("bash", "pkg:deb/debian/bash@5.2-1%2Bb1", "GPL-3.0-or-later AND AGPL-3.0-only"),
        _package("carrier", "pkg:pypi/carrier@2.0", "LGPL-3.0-only"),
        _package("mystery", "pkg:generic/mystery@1"),
        _package("Simple Launcher", "", sourceInfo="from /elsewhere/t32.exe"),
        _package("stray", "pkg:pypi/stray@1.0", "MIT"),  # allowed, and not in the inventory
    )
    problems = check.scan(sbom, found)
    assert any(p.startswith("bash ") and "AGPL-3.0-only not allowed" in p for p in problems)
    assert any(p.startswith("carrier ") and "corresponding source" in p for p in problems)
    assert any(p.startswith("mystery ") and "NOASSERTION" in p for p in problems)
    assert any(p.startswith("Simple Launcher") for p in problems)  # reviewed only where it was
    assert "the SBOM does not list Python package re2like (pkg:pypi/re2like@1.0)" in problems
    assert check.enrich(sbom, found) == [
        "mystery (pkg:generic/mystery@1): no source location",
        "Simple Launcher (name:Simple Launcher): no source location",  # pip's distlib is not here
        "stray (pkg:pypi/stray@1.0): no source location",  # what the image's notices do not list
    ]


# -- the SBOM attestation in an OCI archive ------------------------------------------------------


def _oci(path: Path, arches: list[str]) -> Path:
    blobs: dict[str, bytes] = {}

    def blob(document: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(document).encode()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        blobs[digest] = data
        return {"digest": digest, "size": len(data)}

    manifests = []
    for arch in arches:
        image = blob({"layers": [], "arch": arch})
        manifests.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {"os": "linux", "architecture": arch},
                **image,
            }
        )
        statement = blob(
            {"predicateType": "https://spdx.dev/Document", "predicate": {"name": arch}}
        )
        attestation = blob(
            {
                "layers": [
                    {
                        "mediaType": "application/vnd.in-toto+json",
                        "annotations": {"in-toto.io/predicate-type": "https://spdx.dev/Document"},
                        **statement,
                    }
                ]
            }
        )
        manifests.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": image["digest"],
                },
                **attestation,
            }
        )
    inner = blob({"manifests": manifests})
    index = {"manifests": [{"mediaType": "application/vnd.oci.image.index.v1+json", **inner}]}
    with tarfile.open(path, "w") as archive:
        for name, data in [("index.json", json.dumps(index).encode())] + [
            ("blobs/" + d.replace(":", "/"), b) for d, b in blobs.items()
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_each_architectures_sbom_is_read_from_the_archive(tmp_path: Path) -> None:
    archive = _oci(tmp_path / "image.tar", ["amd64", "arm64"])
    assert check.extract_sbom(archive, "arm64") == {"name": "arm64"}
    assert check.extract_sbom(archive, "amd64") == {"name": "amd64"}
    with pytest.raises(SystemExit, match="0 linux/riscv64 images"):
        check.extract_sbom(archive, "riscv64")


# -- the image's own files and labels ------------------------------------------------------------


def _report(**files: str) -> dict[str, Any]:
    real = {
        "LICENSE": hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest(),
        "NOTICE": hashlib.sha256((ROOT / "NOTICE").read_bytes()).hexdigest(),
        "SOURCE.md": hashlib.sha256((MANIFEST / "SOURCE.md").read_bytes()).hexdigest(),
    }
    return {"problems": [], "files": {**real, **files}}


def _labels() -> dict[str, str]:
    dockerfile = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    return dict(re.findall(r'(org\.opencontainers\.image\.\w+)="([^"]*)"', dockerfile))


def test_the_dockerfiles_labels_and_licence_files_pass() -> None:
    assert check.image_problems(_report(), _labels()) == []
    wrong = check.image_problems(
        _report(NOTICE="0" * 64), {**_labels(), "org.opencontainers.image.description": "x"}
    )
    assert "/licenses/NOTICE is not NOTICE" in wrong
    assert any("does not say '/licenses'" in p for p in wrong)


def test_the_label_names_mendaris_licence_only_and_points_at_the_rest() -> None:
    labels = _labels()
    assert labels["org.opencontainers.image.licenses"] == "Apache-2.0"
    description = labels["org.opencontainers.image.description"]
    assert "Mendari application code: Apache-2.0." in description
    assert "Container includes third-party software under additional licenses." in description
    assert "See /licenses and /licenses/THIRD_PARTY_NOTICES.md." in description


def test_the_dockerfile_writes_licences_and_keeps_debians() -> None:
    """LICENSE, NOTICE and SOURCE.md go to /licenses and the notices are written from the image;
    nothing removes Debian's copyright files or common licence texts (the base image's slim
    configuration keeps /usr/share/doc/*/copyright), and the image check asserts they exist."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY LICENSE NOTICE licenses/image/SOURCE.md /licenses/" in dockerfile
    assert "python3 /tmp/image_notices.py --manifest /tmp/licences --write /licenses" in dockerfile
    assert dockerfile.index("--write /licenses") < dockerfile.index("USER mendari")
    for line in dockerfile.splitlines():
        if line.lstrip().startswith("#"):
            continue
        # A pinned Debian security update discards just its downloaded package lists and source
        # selection. Any other deletion or attempt to exclude licence paths still fails.
        # The superseded ensurepip wheel is replaced by a hash-verified, noticed wheel.
        line = line.replace('rm "$ensure_dir/_bundled/pip-${old_version}-py3-none-any.whl"', "")
        if line.strip() == "&& rm -rf /var/lib/apt/lists/* /tmp/mendari-security.list":
            continue
        assert not re.search(r"\brm\b|path-exclude|/usr/share/doc|common-licenses", line), line
    assert (
        "usr/share/doc/{package['name']}/copyright"
        in (ROOT / "scripts/image_notices.py").read_text()
    )


def test_the_dockerfile_builds_without_buildkit() -> None:
    """Sites build the image themselves with `docker build .` (README, deploy/README.md), and
    Docker's classic builder is still the default where buildx is not installed: the notices are
    written in a stage of their own, and nothing in the file needs BuildKit."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    for line in dockerfile.splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert not re.search(
            r"--(mount|link|chmod|parents|exclude|network|security|checksum)\b|<<", line
        ), line
    assert "FROM runtime AS notices" in dockerfile
    final = dockerfile[dockerfile.rindex("\nFROM ") :]
    assert final.startswith("\nFROM runtime\n")
    assert "COPY --from=notices /licenses /licenses" in final
    # The image takes /licenses from that stage, neither the script nor the manifest.
    instructions = [s for s in final.splitlines() if s.strip() and not s.lstrip().startswith("#")]
    assert not any("image_notices" in s or "licenses/image " in s for s in instructions)


def test_cpythons_build_recipe_is_the_base_images() -> None:
    """CPython's readline and _gdbm modules link GPL-3.0 libraries: its corresponding source is
    the Python release and the official image's Dockerfile that compiled it, for the very base
    image the Dockerfile pins (a new base image is reviewed again, CONTRIBUTING.md)."""
    (cpython,) = [c for c in COMPONENTS["component"] if c["id"].startswith("cpython-")]
    froms = re.findall(r"^FROM (python:\S+)", (ROOT / "Dockerfile").read_text(), re.M)
    assert froms and set(froms) == {cpython["base_image"]}
    files = {item["file"]: item["url"] for item in cpython["corresponding_source"]}
    assert files[f"Python-{cpython['version']}.tar.xz"].startswith("https://www.python.org/ftp/")
    (recipe,) = [url for name, url in files.items() if name.endswith(".Dockerfile")]
    minor = ".".join(cpython["version"].split(".")[:2])
    assert re.fullmatch(
        rf"https://raw\.githubusercontent\.com/docker-library/python/[0-9a-f]{{40}}/{minor}/"
        r"slim-[a-z]+/Dockerfile",
        recipe,
    )


def test_runtime_pip_and_its_vendored_components_share_the_reviewed_version() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    (version,) = re.findall(r"^ARG PIP_VERSION=([\d.]+)$", dockerfile, re.M)
    pip = next(c for c in COMPONENTS["component"] if c["name"] == "pip")
    assert pip["version"] == version
    assert pip["corresponding_source"][0]["file"] == f"pip-{version}.tar.gz"
    for component in COMPONENTS["component"]:
        if component["id"].startswith("pip-vendored-"):
            assert component["in"] == [{"dist": "pip", "version": version}]


def test_requests_in_pip_carries_its_notice_file() -> None:
    """Apache-2.0 asks for a NOTICE file's attributions to be carried; pip vendors Requests
    without it, so the image carries Requests' own (the other Apache-2.0 components ship none)."""
    (requests,) = [c for c in COMPONENTS["component"] if c["id"] == "pip-vendored-requests"]
    assert {t["file"] for t in requests["texts"]} == {"LICENSE", "NOTICE"}
    assert "Kenneth Reitz" in (MANIFEST / "texts/pip-vendored-requests/NOTICE").read_text()


def test_source_md_says_where_the_release_publishes_the_source() -> None:
    text = (MANIFEST / "SOURCE.md").read_text()
    assert "-sources" in text and "/sources" in text and "docker cp" in text
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert '"docker://ghcr.io/${OWNER,,}/${NAME}:${TAG}-sources"' in release
    assert "COPY . /sources/" in (MANIFEST / "sources.Dockerfile").read_text()


# -- collecting the corresponding source ---------------------------------------------------------

DSC = """Format: 3.0 (quilt)
Source: {name}
Checksums-Sha256:
 {orig} 3 {name}_{upstream}.orig.tar.gz
 {debian} 4 {name}_{version}.debian.tar.xz
Files:
 x
"""


def _dsc(name: str, version: str, orig: bytes, debian: bytes) -> bytes:
    return DSC.format(
        name=name,
        version=version,
        upstream=version.split("-")[0],
        orig=hashlib.sha256(orig).hexdigest(),
        debian=hashlib.sha256(debian).hexdigest(),
    ).encode()


def test_a_debian_source_package_is_fetched_from_the_pool_and_checked(tmp_path: Path) -> None:
    dsc = _dsc("bash", "5.2-1", b"abc", b"abcd")
    pool = "https://deb.debian.org/debian/pool/main/b/bash/"
    served = {
        pool + "bash_5.2-1.dsc": dsc,
        pool + "bash_5.2.orig.tar.gz": b"abc",
        pool + "bash_5.2-1.debian.tar.xz": b"abcd",
    }
    written = sources.debian_source("bash", "1:5.2-1", tmp_path, served.get)
    assert sorted(p.name for p in written) == [
        "bash_5.2-1.debian.tar.xz",
        "bash_5.2-1.dsc",
        "bash_5.2.orig.tar.gz",
    ]
    served[pool + "bash_5.2.orig.tar.gz"] = b"tampered"
    with pytest.raises(SystemExit, match=r"not what its \.dsc says"):
        sources.debian_source("bash", "1:5.2-1", tmp_path, served.get)


def test_a_version_gone_from_the_archive_comes_from_the_snapshot(tmp_path: Path) -> None:
    dsc = _dsc("libfoo", "1.0-1", b"abc", b"abcd")
    files = {
        "libfoo_1.0-1.dsc": dsc,
        "libfoo_1.0.orig.tar.gz": b"abc",
        "libfoo_1.0-1.debian.tar.xz": b"abcd",
    }
    served = {
        f"https://snapshot.debian.org/file/{hashlib.sha1(d).hexdigest()}": d for d in files.values()
    }
    fileinfo = {hashlib.sha1(d).hexdigest(): [{"name": n}] for n, d in files.items()}
    served["https://snapshot.debian.org/mr/package/libfoo/1.0-1/srcfiles?fileinfo=1"] = json.dumps(
        {"fileinfo": fileinfo}
    ).encode()
    written = sources.debian_source("libfoo", "1.0-1", tmp_path, served.get)
    assert {p.name for p in written} == set(files)
    assert sources.dsc_files(dsc.decode())[0][1] == "libfoo_1.0.orig.tar.gz"


def test_sources_are_merged_once_and_a_conflict_fails() -> None:
    one = {
        "sources": [
            {"kind": "debian", "package": "bash", "version": "5.2-1"},
            {"kind": "file", "file": "a.tgz", "sha256": "1", "url": "u"},
        ]
    }
    two = {
        "sources": [
            {"kind": "debian", "package": "bash", "version": "5.2-1"},
            {"kind": "file", "file": "a.tgz", "sha256": "1", "url": "u"},
        ]
    }
    debian, files = sources.merged([one, two])
    assert debian == [("bash", "5.2-1")] and len(files) == 1
    two["sources"][1]["sha256"] = "2"
    with pytest.raises(SystemExit, match="two different files"):
        sources.merged([one, two])


def test_a_component_file_must_match_its_pin(tmp_path: Path) -> None:
    item = {
        "file": "x.tgz",
        "url": "https://example.org/x.tgz",
        "sha256": hashlib.sha256(b"x").hexdigest(),
    }
    assert sources.component_file(item, tmp_path, {item["url"]: b"x"}.get).read_bytes() == b"x"
    with pytest.raises(SystemExit, match="pinned SHA-256"):
        sources.component_file(item, tmp_path, {item["url"]: b"y"}.get)


def test_source_collection_preserves_existing_output_and_publishes_only_complete_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = tmp_path / "sources.json"
    listing.write_text(json.dumps({"image": "example", "arch": "arm64", "sources": []}))
    out = tmp_path / "release"
    out.mkdir()
    inode = out.stat().st_ino
    saved = out / "previous"
    saved.write_text("keep")
    command = ["--out", str(out), str(listing)]
    with pytest.raises(SystemExit, match="empty directory"):
        sources.main(command)
    assert saved.read_text() == "keep"
    saved.unlink()

    with monkeypatch.context() as patch:
        patch.setattr(sources, "readme", lambda *_: pytest.fail("interrupted"))
        with pytest.raises(pytest.fail.Exception, match="interrupted"):
            sources.main(command)
    assert list(out.iterdir()) == []
    rename = Path.rename

    def interrupt_publication(entry: Path, destination: Path) -> Path:
        if entry.name == "README.md":
            # A second creator cannot write over the first one's in-flight files.
            with pytest.raises(SystemExit, match="empty directory"):
                sources.main(command)
            raise OSError("interrupted before the inventory was published")
        return rename(entry, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupt_publication)
        with pytest.raises(OSError, match="before the inventory"):
            sources.main(command)
    assert list(out.iterdir()) == []
    assert sources.main(command) == 0
    assert out.stat().st_ino == inode
    assert (out / "README.md").is_file()

    fresh = tmp_path / "new-output"
    assert sources.main(["--out", str(fresh), str(listing)]) == 0
    assert (fresh / "README.md").read_bytes() == (out / "README.md").read_bytes()

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="working directory"):
        sources.main(["--out", ".", str(listing)])


def test_source_filenames_are_single_components() -> None:
    assert sources.source_name("libfoo_2.1~rc1.orig.tar.xz") == "libfoo_2.1~rc1.orig.tar.xz"
    with pytest.raises(SystemExit, match="invalid source filename"):
        sources.source_name("folder/file.tar.gz")


def test_embedded_pip_bom_takes_its_identity_from_matching_installed_metadata() -> None:
    site = "/usr/local/lib/python3.12/site-packages"
    purl = "pkg:pypi/pip@26.3"
    found = {
        "python": {"version": "3.12.16"},
        "dists": [{"name": "pip", "version": "26.3", "env": "base"}],
    }
    installed = _package(
        "pip",
        purl,
        "MIT",
        sourceInfo=(
            "acquired package info from installed python package manifest file: "
            f"{site}/pip-26.3.dist-info/METADATA"
        ),
    )
    origin = f"acquired package info from SBOM: {site}/pip/_vendor/bom.cdx.json"
    embedded = {"name": "pip", "versionInfo": "UNKNOWN", "sourceInfo": origin}
    other = {**embedded, "sourceInfo": "acquired package info from SBOM: /other/bom.cdx.json"}
    check.identify_pip_vendor_bom(_sbom(embedded), found)
    assert embedded["versionInfo"] == "UNKNOWN"
    check.identify_pip_vendor_bom(_sbom(installed, embedded, other), found)
    assert embedded["versionInfo"] == "26.3"
    assert check._purl(embedded) == purl
    assert "externalRefs" not in other
