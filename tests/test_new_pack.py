# SPDX-License-Identifier: Apache-2.0
"""`mendari new`: the template pack validates, passes its tests and evaluates as written, as
the authoring guide (docs/guides/authoring-a-pack.md) says."""

from __future__ import annotations

import shutil
import subprocess
import sys
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


@pytest.mark.parametrize("community", [False, True])
def test_new_keeps_publisher_text_as_yaml_strings(tmp_path: Path, community: bool) -> None:
    target = tmp_path / "new-pack"
    args = ["--community"] if community else ["--id", "my-clinic/new-pack"]
    name = 'Operations: "A" #1 {{publisher}}'
    assert main(["new", str(target), *args, "--name", name, "--publisher", "On"]) == 0
    pack = resolve_pack(str(target))
    assert pack.manifest.name == name
    assert pack.manifest.publisher.name == "On"


def test_scaffolding_the_working_directory_preserves_it_and_retries_a_failed_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    before = tmp_path.stat()
    rename = Path.rename

    def fail_manifest(source: Path, destination: Path) -> Path:
        if source.name == "pack.yaml":
            raise OSError("interrupted")
        return rename(source, destination)

    args = ["new", ".", "--id", "clinic/example"]
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_manifest)
        assert main(args) == 1
    assert list(tmp_path.iterdir()) == []
    assert main(args) == 0
    assert Path.cwd().samefile(tmp_path)
    assert tmp_path.stat().st_ino == before.st_ino
    assert resolve_pack(".").id == "clinic/example"


def test_concurrent_scaffolds_leave_one_complete_pack(tmp_path: Path) -> None:
    target = tmp_path / "new"
    cli = shutil.which("mendari", path=str(Path(sys.executable).parent))
    assert cli is not None
    creators = [
        subprocess.Popen(
            [cli, "new", str(target), "--id", f"clinic/{name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for name in ("alpha", "beta")
    ]
    exits = [creator.wait(timeout=30) for creator in creators]
    assert sorted(exits) == [0, 1]
    assert resolve_pack(str(target)).id == ("clinic/alpha" if exits[0] == 0 else "clinic/beta")
