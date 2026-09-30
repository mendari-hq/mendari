"""`openhealth new`: the template pack validates, passes its tests and evaluates as written, as
the authoring guide (docs/guides/authoring-a-pack.md) says."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from openhealth.cli import main
from openhealth.core.decision import Decision
from openhealth.core.registry import resolve_pack
from openhealth.evals import evaluate, resolve_dataset
from openhealth.evals.pack_tests import run_pack_tests


def test_a_new_pack_works_as_written(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "refill-router"
    args = ["new", str(target), "--id", "my-clinic/refill-router", "--publisher", "My Clinic"]
    assert main(args) == 0
    assert "openhealth test" in capsys.readouterr().out
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
    from openhealth.cli import TEMPLATE

    suffixes = {p.suffix for p in TEMPLATE.rglob("*") if p.is_file()}
    assert suffixes <= {".yaml", ".json", ".jsonl"}  # nothing to program
    manifest = yaml.safe_load(
        (TEMPLATE / "pack.yaml").read_text().replace("{{", "").replace("}}", "")
    )
    assert manifest["spec_version"] == "0.2"
    assert "docs/guides/authoring-a-pack.md" in (TEMPLATE / "pack.yaml").read_text()


def test_new_never_overwrites_and_checks_the_id(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "notes.txt").write_text("mine")
    assert main(["new", str(tmp_path / "taken"), "--id", "a/b"]) == 1
    assert (tmp_path / "taken" / "notes.txt").read_text() == "mine"
    assert main(["new", str(tmp_path / "fresh"), "--id", "Not An Id"]) == 1
    assert not (tmp_path / "fresh").exists()
