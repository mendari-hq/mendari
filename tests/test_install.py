# SPDX-License-Identifier: Apache-2.0
"""One install command: `pip install mendari`, with no extra, is enough for the first five
minutes (the demo, an agent of one's own, the server). The `server` extra, which the server
needed before, is still accepted, so instructions and images that ask for it keep working."""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path
from typing import Any

from mendari.cli import build_parser
from mendari.sandbox import demo

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/mendari-hq/mendari"


def _project() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _locked() -> dict[str, Any]:
    packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    (mendari,) = [package for package in packages if package["name"] == "mendari"]
    return mendari


def test_the_plain_install_brings_what_the_server_needs() -> None:
    assert "uvicorn>=0.29" in _project()["dependencies"]
    assert {"name": "uvicorn"} in _locked()["dependencies"]


def test_the_server_extra_is_still_accepted_and_adds_nothing() -> None:
    # An extra a project does not declare is a warning from pip and an error from uv.
    assert _project()["optional-dependencies"] == {"server": []}
    locked = _locked()
    assert locked["metadata"]["provides-extras"] == ["server"]
    assert "optional-dependencies" not in locked
    # Who still asks for it: the image, the release's check of what TestPyPI serves, the README.
    assert "--extra server" in (ROOT / "Dockerfile").read_text()
    assert "--extra server" in (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "pip install '.[server]'" in (ROOT / "README.md").read_text()


def test_the_demo_ends_with_what_an_installed_mendari_can_do() -> None:
    """Its last words name a URL, not a folder of the clone (an installed Mendari has no
    `examples/` beside it), and commands as the command line reads them, with no flag it had
    before (`mendari new --agent`)."""
    assert demo.EXAMPLES in demo.NEXT and " examples/" not in demo.NEXT
    prefix = f"{REPOSITORY}/tree/main/"
    assert demo.EXAMPLES.startswith(prefix)
    assert (ROOT / demo.EXAMPLES.removeprefix(prefix)).is_dir()
    commands = [shlex.split(command) for command in re.findall(r"`(mendari [^`]+)`", demo.NEXT)]
    assert [command[1] for command in commands] == ["new", "serve"]
    parser = build_parser()
    for command in commands:
        parser.parse_args(command[1:])  # a usage error raises
    assert "--agent" not in demo.NEXT
