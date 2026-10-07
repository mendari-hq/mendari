# SPDX-License-Identifier: Apache-2.0
"""NOTICE (Apache-2.0 section 4(d)) is generated from licenses/notice.toml by
scripts/build_notice.py, names every package vendored at the version vendored, carries Swagger
UI's own NOTICE, and ships in the sdist, the wheel and the image."""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _script() -> Any:
    path = ROOT / "scripts" / "build_notice.py"
    spec = importlib.util.spec_from_file_location("build_notice", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_notice = _script()
NOTICE = (ROOT / "NOTICE").read_text()


def test_notice_is_what_the_manifest_says() -> None:
    assert build_notice.render() == NOTICE, "run: uv run python scripts/build_notice.py"
    assert build_notice.main(["--check"]) == 0


def test_notice_names_the_project_and_its_licence() -> None:
    project = build_notice.manifest()["project"]
    assert NOTICE.startswith(f"Mendari\n{project['copyright']}\n")
    assert "Apache License, Version 2.0" in NOTICE


def test_every_vendored_package_is_listed_at_its_vendored_version() -> None:
    manifests = [
        *ROOT.glob("src/mendari/dashboard/vendor/*/manifest.json"),
        *ROOT.glob("tests/browser/vendor/*/manifest.json"),
    ]
    assert len(manifests) >= 2  # Swagger UI and axe-core
    listed = {m["vendored"]: m for m in build_notice.manifest()["material"] if "vendored" in m}
    for manifest in manifests:
        directory = manifest.parent.relative_to(ROOT).as_posix()
        assert directory in listed, f"{directory} is vendored but not in licenses/notice.toml"
        version = json.loads(manifest.read_text())["version"]
        assert f"{listed[directory]['name']} {version}\n" in NOTICE
        # The shipped package is in the first section, a test-only one in the second.
        shipped = directory.startswith("src/")
        assert listed[directory]["distributed"] is shipped, directory


def test_upstream_notices_are_carried_and_every_path_exists() -> None:
    swagger = ROOT / "src/mendari/dashboard/vendor/swagger-ui"
    version = json.loads((swagger / "manifest.json").read_text())["version"]
    for line in (swagger / version / "NOTICE").read_text().strip().splitlines():
        assert line.strip() in NOTICE
    for material in build_notice.manifest()["material"]:
        release = build_notice.version(material) or ""
        for key in ("notice_file",):
            if key in material:
                assert (ROOT / material[key].replace("<version>", release)).is_file()
    assert (ROOT / "src/mendari/dashboard/LICENSE-lucide.txt").is_file()
    lucide = (ROOT / "src/mendari/dashboard/LICENSE-lucide.txt").read_text()
    assert "ISC License" in lucide and "Permission is hereby granted" in lucide  # and Feather's MIT


def test_notice_ships_in_the_sdist_the_wheel_and_the_image() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "/NOTICE" in project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    # The wheel: hatchling puts NOTICE* in .dist-info/licenses by default (PEP 639); a
    # `license-files` setting would replace that default.
    assert "license-files" not in project["project"]
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY README.md LICENSE NOTICE ./" in dockerfile
