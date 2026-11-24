# SPDX-License-Identifier: Apache-2.0
"""One install command: `pip install mendari`, with no extra, is enough for the first five
minutes (the demo, an agent of one's own, the server). The `server` extra, which the server
needed before, is still accepted, so instructions and images that ask for it keep working.

What an install holds is the wheel: CI's `quickstart-wheel` job builds it, checks it holds
every tracked file of the folders it is built from (`scripts/check_sdist.py --wheel`), and runs
those first five minutes from it, timed (`scripts/run_quickstart.py --wheel`). The checks
themselves are tested here; the run is CI's, since it installs from the package index."""

from __future__ import annotations

import importlib.util
import re
import shlex
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari.cli import CI_WORKFLOW, build_parser
from mendari.sandbox import demo
from mendari.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/mendari-hq/mendari"


def _project() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _locked() -> dict[str, Any]:
    packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    (mendari,) = [package for package in packages if package["name"] == "mendari"]
    return mendari


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_sdist = _script("check_sdist")
quickstart = _script("run_quickstart")
# The script put its own folder on the import path, to find the script beside it: taken off
# again, so no other test imports a script by accident.
sys.path.remove(str(ROOT / "scripts"))


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


# -- what the wheel holds -------------------------------------------------------------------------


def test_a_wheel_is_built_from_the_package_and_the_bundled_agent() -> None:
    assert check_sdist.wheel_sources() == {"src/mendari": "mendari", "agents": "mendari/_agents"}
    # Both are in the sdist the wheel is built from.
    assert {source.split("/")[0] for source in check_sdist.wheel_sources()} <= check_sdist.INCLUDED


def test_the_wheel_check_wants_every_tracked_file_and_no_other() -> None:
    sources = check_sdist.wheel_sources()
    tracked = [
        "src/mendari/__init__.py",
        "src/mendari/templates/ci/mendari.yml",
        "src/mendari/dashboard/style.css",
        "agents/portal-message-agent/agent.yaml",
        "README.md",  # in the sdist only
        "tests/test_cli.py",  # in neither
        "src/mendari.egg-info/PKG-INFO",  # beside the package, not in it
    ]
    held = [
        "mendari/__init__.py",
        "mendari/templates/ci/mendari.yml",
        "mendari/dashboard/style.css",
        "mendari/_agents/portal-message-agent/agent.yaml",
        "mendari-0.1.0.dist-info/METADATA",
        "mendari-0.1.0.dist-info/licenses/LICENSE",
    ]
    assert check_sdist.wheel_problems(held, tracked, sources) == []
    # What an install would then lack: a template `mendari new` writes, the bundled agent.
    assert check_sdist.wheel_problems(held[:1] + held[2:], tracked, sources) == [
        "tracked but missing from the wheel: src/mendari/templates/ci/mendari.yml"
    ]
    assert check_sdist.wheel_problems(held[:3] + held[4:], tracked, sources) == [
        "tracked but missing from the wheel: agents/portal-message-agent/agent.yaml"
    ]
    # And what git does not track: a file left in the package's folder when it was built.
    assert check_sdist.wheel_problems([*held, "mendari/scratch.py"], tracked, sources) == [
        "in the wheel but not tracked: mendari/scratch.py"
    ]


# -- the first five minutes, from the wheel -------------------------------------------------------


def test_the_first_commands_are_the_ones_a_new_developer_types() -> None:
    assert quickstart.WHEEL_LIMIT_S == 300  # a five-minute path
    assert [" ".join(command) for command in quickstart.FIRST_COMMANDS] == [
        "demo",
        "new my-agent --id my-clinic/my-agent",
        "validate my-agent",
        "test my-agent",
        "eval my-agent --dataset examples",
    ]
    parser = build_parser()
    for command in quickstart.FIRST_COMMANDS:
        parser.parse_args(list(command))  # each is a command as the command line reads it
    # After `mendari new`: the agent, and the workflow that checks it on every change, each
    # written from a template the wheel must hold.
    written = quickstart.SCAFFOLDED
    assert written == ("agent.yaml", CI_WORKFLOW.as_posix())


def test_the_commands_run_as_on_a_new_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("MENDARI_AGENTS_PATH", "MENDARI_AGENT_URL", "LLM_API_KEY", "PYTHONPATH"):
        monkeypatch.setenv(name, "set")
    monkeypatch.setenv("VIRTUAL_ENV", str(ROOT / ".venv"))
    monkeypatch.setenv("PATH", "/usr/bin")
    env = quickstart._env(tmp_path / "home", tmp_path / "venv")
    assert not [name for name in env if name.startswith(("MENDARI_", "LLM_", "PYTHON"))]
    assert "VIRTUAL_ENV" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["PATH"] == f"{tmp_path / 'venv' / 'bin'}:/usr/bin"


def test_the_console_names_its_own_files_and_the_server_serves_each(tmp_path: Path) -> None:
    """What the wheel run asks of `mendari serve` after /api/health: the console's page, and
    every file of its own the page names. Here on the app itself, so the probe finds them."""
    assert quickstart.static_files(
        '<link href="/static/style.css?v=1"><script src="/static/main.js?v=2"></script>'
        '<a href="/docs">docs</a><link href="/static/style.css?v=1">'
    ) == ["/static/style.css?v=1", "/static/main.js?v=2"]
    client = TestClient(create_app(db_path=tmp_path / "console.db"))
    files = quickstart.static_files(client.get("/").text)
    assert len(files) > 20 and any(path.startswith("/static/style.css") for path in files)
    assert all(client.get(path).status_code == 200 for path in files)


def test_a_file_that_is_not_a_wheel_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sdist = tmp_path / "mendari-0.1.0.tar.gz"
    sdist.write_bytes(b"")
    assert quickstart.main(["--wheel", str(sdist)]) == 1
    assert quickstart.main(["--wheel", str(tmp_path / "missing.whl")]) == 1
    assert capsys.readouterr().out.count("not a wheel (build one with `uv build") == 2


def test_ci_builds_the_wheel_checks_it_and_runs_the_first_five_minutes_from_it() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    job = ci["jobs"]["quickstart-wheel"]
    assert "if" not in job and not any("if" in step for step in job["steps"])
    runs = [step["run"] for step in job["steps"] if "run" in step]
    assert runs == [
        "uv build --out-dir /tmp/dist",
        "python3 scripts/check_sdist.py /tmp/dist/mendari-*.tar.gz --wheel /tmp/dist/*.whl",
        "python3 scripts/run_quickstart.py --wheel /tmp/dist/*.whl",
    ]
    # Nothing of the clone's environment is installed: the wheel is all the run has.
    (setup,) = [step for step in job["steps"] if step.get("uses", "").startswith("astral-sh/")]
    assert setup["with"]["enable-cache"] is False
    # The docs' quickstart, in the clone, is still its own job.
    assert "python3 scripts/run_quickstart.py" in [
        step.get("run") for step in ci["jobs"]["quickstart"]["steps"]
    ]


def test_a_release_runs_both_checks_on_the_wheel_it_would_publish() -> None:
    """In the build job, which every publishing job needs (tests/test_workflow_safety.py):
    after the build, before the files are handed on, and with no way around."""
    release = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    steps = release["jobs"]["build"]["steps"]
    runs = [step.get("run", "") for step in steps]
    (check,) = [i for i, run in enumerate(runs) if "scripts/run_quickstart.py --wheel" in run]
    assert runs[check].splitlines() == [
        "python3 scripts/check_sdist.py dist/mendari-*.tar.gz --wheel dist/*.whl",
        "python3 scripts/run_quickstart.py --wheel dist/*.whl",
    ]
    assert not {"if", "continue-on-error"} & set(steps[check])
    (handed_on,) = [
        i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/upload-")
    ]
    assert runs.index("uv build") < check < handed_on
