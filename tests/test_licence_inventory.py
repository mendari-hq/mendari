# SPDX-License-Identifier: Apache-2.0
"""Every dependency's licence is on the allow-list (scripts/check_licences.py, LP-14): the Python
runtime closure as uv.lock installs it, and the Hub's production packages. An unknown or
copyleft licence fails with the package named, unless it is a named exception with its reason."""

from __future__ import annotations

import email.message
import importlib.util
import json
import sys
import tomllib
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _script() -> Any:
    path = ROOT / "scripts" / "check_licences.py"
    spec = importlib.util.spec_from_file_location("check_licences", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _script()


def test_the_python_runtime_closure_is_allowed() -> None:
    packages = check.python_closure()
    names = {p.name for p in packages}
    assert {"mendari", "fastapi", "pydantic", "psycopg", "uvicorn", "certifi"} <= names
    assert not names & {"pytest", "ruff", "mypy", "mkdocs"}  # development tools are not in it
    assert check.problems(packages) == []


def test_the_closure_is_the_locked_one() -> None:
    """Each package walked is in uv.lock at the version installed: the walk reads what the lock
    installed, not a stray environment."""
    locked = {
        check._canonical(p["name"]): p["version"]
        for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    }
    for package in check.python_closure():
        assert locked.get(package.name) == package.version, package


def test_the_hub_production_packages_are_allowed() -> None:
    packages = check.hub_packages()
    names = {p.name for p in packages}
    assert "next" in names and "react" in names
    assert "typescript" not in names  # a development dependency
    assert check.problems(packages) == []


def test_every_exception_names_a_package_still_used_and_says_why() -> None:
    used = {p.name for p in check.python_closure()} | {p.name for p in check.hub_packages()}
    for pattern, (licence, reason) in check.EXCEPTIONS.items():
        covered = [name for name in used if check.exception(name) == (licence, reason)]
        assert covered, f"{pattern} is no longer a dependency: drop its exception"
        assert not check.allowed(licence), f"{pattern}: {licence} is permissive, no exception"
        assert len(reason) > 40


@pytest.mark.parametrize(
    ("expression", "ok"),
    [
        ("MIT", True),
        ("Apache-2.0 OR BSD-3-Clause", True),
        ("MIT AND PSF-2.0", True),
        ("(MIT OR GPL-3.0-only) AND BSD-2-Clause", True),
        ("GPL-3.0-only", False),
        ("AGPL-3.0-or-later", False),
        ("LGPL-3.0-only", False),
        ("MPL-2.0", False),
        ("MIT AND GPL-2.0-only", False),
        ("GPL-2.0-only OR LGPL-2.1-only", False),
        ("Apache-2.0 WITH LLVM-exception", False),  # not reasoned about: fails closed
        ("Apache 2.0", False),
        ("MIT OR", False),
        ("(MIT", False),
        ("", False),
        ("SEE LICENSE IN LICENSE.txt", False),
    ],
)
def test_an_expression_passes_only_when_permissive(expression: str, ok: bool) -> None:
    assert check.allowed(expression) is ok


def test_an_unknown_or_copyleft_licence_fails_with_the_package_named() -> None:
    packages = [
        check.Package("left-pad", "1.0.0", "MIT"),
        check.Package("strong", "2.0.0", "GPL-3.0-only"),
        check.Package("mystery", "0.1.0", None),
        check.Package("psycopg", "3.2.0", "GPL-3.0-only"),  # an exception covers one licence only
        check.Package("@img/sharp-libvips-linux-x64", "1.0.0", "LGPL-3.0-or-later"),
    ]
    assert check.problems(packages) == [
        "strong 2.0.0: GPL-3.0-only is not allowed",
        "mystery 0.1.0: no licence we can read",
        "psycopg 3.2.0: GPL-3.0-only, but its exception accepts LGPL-3.0-only only",
    ]


def test_a_new_copyleft_package_in_the_hub_lockfile_fails(tmp_path: Path) -> None:
    lock = json.loads((ROOT / "hub" / "package-lock.json").read_text())
    lock["packages"]["node_modules/copyleft-widget"] = {"version": "1.0.0", "license": "AGPL-3.0"}
    lock["packages"]["node_modules/dev-only"] = {
        "version": "1.0.0",
        "license": "GPL-3.0",
        "dev": True,
    }
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps(lock))
    assert check.problems(check.hub_packages(path)) == [
        "copyleft-widget 1.0.0: AGPL-3.0 is not allowed"
    ]


def distribution(
    licence: str | None = None, expression: str | None = None, *classifiers: str
) -> Any:
    fields = email.message.Message()
    if expression is not None:
        fields["License-Expression"] = expression
    if licence is not None:
        fields["License"] = licence
    for classifier in classifiers:
        fields["Classifier"] = f"License :: OSI Approved :: {classifier}"
    return types.SimpleNamespace(metadata=fields)


@pytest.mark.parametrize(
    ("metadata", "read"),
    [
        (distribution(None, "MIT", "MIT License"), "MIT"),
        (distribution("GPL-3.0-only", "MIT"), "MIT"),  # License-Expression wins (PEP 639)
        # A stated licence wins over classifiers, which cannot turn copyleft into a choice:
        (
            distribution(
                "MPL-2.0 AND MIT", None, "MIT License", "Mozilla Public License 2.0 (MPL 2.0)"
            ),
            "MPL-2.0 AND MIT",
        ),
        (distribution("GPL-3.0-only", None, "MIT License"), "GPL-3.0-only"),
        # Classifiers alone list parts, all of which must pass:
        (
            distribution(None, None, "MIT License", "Mozilla Public License 2.0 (MPL 2.0)"),
            "MIT AND MPL-2.0",
        ),
        (distribution("UNKNOWN", None, "MIT License"), "MIT"),
        (
            distribution(
                "Apache License\n  Version 2.0, January 2004", None, "Apache Software License"
            ),
            "Apache-2.0",
        ),
        (distribution("Apache 2.0", None, "Apache Software License"), "Apache-2.0"),
        (distribution(None, None, "MIT License", "GNU General Public License v3 (GPLv3)"), None),
        (distribution(None, None), None),
    ],
)
def test_a_distributions_licence_is_read_failing_closed(metadata: Any, read: str | None) -> None:
    assert check.python_licence(metadata) == read


def test_a_copyleft_part_fails_even_beside_a_permissive_classifier() -> None:
    tqdm_like = distribution(
        "MPL-2.0 AND MIT", None, "MIT License", "Mozilla Public License 2.0 (MPL 2.0)"
    )
    package = check.Package("tqdm", "4.67.1", check.python_licence(tqdm_like))
    assert check.problems([package]) == ["tqdm 4.67.1: MPL-2.0 AND MIT is not allowed"]
