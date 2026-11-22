# SPDX-License-Identifier: Apache-2.0
"""`mendari new`: the template pack validates, passes its tests and evaluates as written, as
the authoring guide (docs/guides/authoring-an-agent.md) says."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mendari.cli import AUTHORING_GUIDE, main
from mendari.core.decision import Decision
from mendari.core.registry import resolve_pack
from mendari.evals import evaluate, resolve_dataset
from mendari.evals.pack_tests import run_pack_tests

ROOT = Path(__file__).resolve().parents[1]


def test_a_new_pack_works_as_written(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "refill-router"
    args = ["new", str(target), "--id", "my-clinic/refill-router", "--publisher", "My Clinic"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "mendari test" in out
    assert f"The guide: {AUTHORING_GUIDE}" in out  # a URL: it works from any directory
    pack = resolve_pack(str(target))
    assert pack.id == "my-clinic/refill-router" and pack.manifest.name == "Refill router"
    assert pack.manifest.publisher.name == "My Clinic"
    assert "{{" not in (target / "pack.yaml").read_text()
    decision = Decision(pack)
    assert all(outcome.passed for outcome in run_pack_tests(decision))
    report = evaluate(decision, resolve_dataset(pack, "examples"))
    assert report["questions"]["request"]["accuracy"] > 0.9
    for command in (["validate", str(target)], ["test", str(target)]):
        assert main(command) == 0


def test_the_template_is_yaml_only_and_says_where_the_guide_is() -> None:
    from mendari.cli import TEMPLATE

    suffixes = {p.suffix for p in TEMPLATE.rglob("*") if p.is_file()}
    assert suffixes <= {".yaml", ".json", ".jsonl"}  # nothing to program
    manifest = yaml.safe_load(
        (TEMPLATE / "pack.yaml").read_text().replace("{{", "").replace("}}", "")
    )
    assert manifest["spec_version"] == "0.2"
    assert AUTHORING_GUIDE in (TEMPLATE / "pack.yaml").read_text()


def test_the_guide_url_names_the_guide_in_this_repository() -> None:
    prefix = "https://github.com/mendari-hq/mendari/blob/main/"
    assert AUTHORING_GUIDE.startswith(prefix)
    assert (ROOT / AUTHORING_GUIDE.removeprefix(prefix)).is_file()


def test_new_never_overwrites_and_checks_the_id(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "notes.txt").write_text("mine")
    assert main(["new", str(tmp_path / "taken"), "--id", "a/b"]) == 1
    assert (tmp_path / "taken" / "notes.txt").read_text() == "mine"
    assert main(["new", str(tmp_path / "fresh"), "--id", "Not An Id"]) == 1
    assert not (tmp_path / "fresh").exists()


def test_new_agent_pack_works_offline_as_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mendari.cli import AGENT_TEMPLATE

    suffixes = {p.suffix for p in AGENT_TEMPLATE.rglob("*") if p.is_file()}
    assert suffixes <= {".yaml", ".json", ".jsonl"}  # YAML/JSON only

    target = tmp_path / "agent-router"
    args = [
        "new",
        str(target),
        "--agent",
        "--id",
        "my-clinic/agent-router",
        "--publisher",
        "My Clinic",
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "MENDARI_AGENT_URL=" in out
    assert f"The guide: {AUTHORING_GUIDE}" in out

    pack = resolve_pack(str(target))
    assert pack.id == "my-clinic/agent-router"
    assert pack.manifest.providers["reader"].use == "agent"
    assert pack.manifest.providers["reader"].variants["agent"].type == "agent"
    assert "{{" not in (target / "pack.yaml").read_text()

    decision = Decision(pack)
    assert all(outcome.passed for outcome in run_pack_tests(decision))
    report = evaluate(decision, resolve_dataset(pack, "examples"))
    assert report["questions"]["request"]["accuracy"] > 0.9
    for command in (
        ["validate", str(target)],
        ["test", str(target)],
        ["eval", str(target), "--dataset", "examples"],
    ):
        assert main(command) == 0

    assert main(["new", str(tmp_path / "both"), "--community", "--agent", "--id", "a/b"]) == 1
    assert not (tmp_path / "both").exists()
