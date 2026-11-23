# SPDX-License-Identifier: Apache-2.0
"""One validation function (LP-20b, `evals/validation.py`): `mendari validate`, the community
check and the registry's publish call it, so a pack one takes the others take; each problem has
a kind, a file and a place, and never a case's text."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari.cli import main
from mendari.community.check import check
from mendari.core.pack import Pack, load_pack
from mendari.core.reference import bundled_dir
from mendari.errors import PackValidationError
from mendari.evals import validation
from mendari.evals.pack_tests import validate_pack
from mendari.hub import archive
from mendari.hub import registry as registry_module
from mendari.hub.registry import RegistryStore
from tests.conftest import INBOX_DIR, Registry, cpu_time
from tests.test_community_budget import BOUND_S, fan_out
from tests.test_community_caps import sorter

SECRET = "PLANTED-CASE-TEXT-7731"  # in a planted case's input: never in any problem


def _planted_inbox(base: Path) -> Pack:
    """The inbox pack at a new version (so a registry checks it as a pack it did not ship), with
    one behaviour test whose `message` is text where the schema wants an object."""
    root = base / "inbox"
    shutil.copytree(INBOX_DIR, root)
    manifest = (root / "pack.yaml").read_text()
    version = load_pack(INBOX_DIR).version
    (root / "pack.yaml").write_text(manifest.replace(f"version: {version}", "version: 9.9.9", 1))
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text()
        + f"\n- name: planted\n  input: {{message: {SECRET!r}}}\n  expect: {{not_outcome: AUTO}}\n"
    )
    return load_pack(root)


def _case_number(pack: Pack, name: str) -> int:
    cases = yaml.safe_load((pack.root / str(pack.manifest.tests)).read_text())
    return next(number for number, case in enumerate(cases, start=1) if case["name"] == name)


def test_the_command_the_community_check_and_the_registry_call_one_function(
    tmp_path: Path, registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each caller is given a pack with one planted problem (a behaviour test's input the schema
    refuses) and reports the same kind, file and place."""
    pack = _planted_inbox(tmp_path)
    place = f"case {_case_number(pack, 'planted')}"
    found = validation.problems(pack)
    assert [(p.kind, p.file, p.place) for p in found] == [("tests", "tests/cases.yaml", place)]
    line = found[0].text
    assert line.startswith(f"tests/cases.yaml {place} (planted): input message: ")

    # The command line: the same line, as `mendari validate` has always printed problems.
    assert main(["validate", str(pack.root)]) == 1
    assert line in capsys.readouterr().err
    with pytest.raises(PackValidationError) as error:
        validate_pack(pack)  # the old name calls the one function
    assert error.value.problems == [line]

    # The registry: refused, naming it, and nothing stored.
    refused = registry.publish(pack)
    assert refused.status_code == 422, refused.text
    assert line in refused.json()["detail"]
    assert registry.client.get("/v1/search").json() == []

    # The community check, on a community pack with the same kind of problem.
    root = sorter(tmp_path / "community")
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text() + f"\n- name: planted\n  input: {{message: {{note: {SECRET!r}}}}}\n"
        "  expect: {not_outcome: AUTO}\n"
    )
    community = load_pack(root, origin="community")
    [problem] = validation.problems(community)
    assert (problem.kind, problem.file) == ("tests", "tests/cases.yaml")
    assert problem.place == f"case {_case_number(community, 'planted')}"
    report = check(root, scope=False)
    assert f"`mendari validate`: {problem.text}" in [f.message for f in report.errors]


def test_every_reference_pack_is_valid() -> None:
    root = bundled_dir()
    assert root is not None
    packs = [load_pack(d) for d in sorted(root.iterdir()) if (d / "pack.yaml").is_file()]
    assert len(packs) == 5
    for pack in packs:
        assert validation.problems(pack) == [], pack.id
        validation.validate(pack)


def test_a_publish_the_validator_refuses_stores_nothing_and_names_the_problem(
    tmp_path: Path, registry: Registry, db_target: str
) -> None:
    pack = _planted_inbox(tmp_path)
    refused = registry.publish(pack)
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail.startswith("the agent is not valid (`mendari validate`): tests/cases.yaml case ")
    assert SECRET not in detail
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        assert db.one("SELECT COUNT(*) AS n FROM hub_versions")["n"] == 0
        assert db.one("SELECT COUNT(*) AS n FROM hub_archives")["n"] == 0


def test_a_valid_publish_is_stored_as_before(
    tmp_path: Path, registry: Registry, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metadata stored for a valid pack is what the registry stored before it validated:
    `describe` of the same archive, unchanged."""
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "off")  # its result is the run's own
    root = tmp_path / "inbox"
    shutil.copytree(INBOX_DIR, root)
    manifest = (root / "pack.yaml").read_text()
    version = load_pack(INBOX_DIR).version
    (root / "pack.yaml").write_text(manifest.replace(f"version: {version}", "version: 9.9.9", 1))
    pack = load_pack(root)
    data = archive.build(pack, registry.key)
    published = registry.client.put(
        "/v1/packs/mendari/patient-inbox-router/9.9.9",
        content=data,
        headers={"authorization": f"Bearer {registry.token}"},
    )
    assert published.status_code == 201, published.text
    public = registry.key.public_key()
    verified = archive.verify(data, {archive.key_id(public): public})
    with tempfile.TemporaryDirectory() as scratch:
        extracted = archive.extract(verified, Path(scratch) / "pack", origin="hub")
        expected = json.loads(json.dumps(registry_module.describe(extracted, verified)))
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        row = db.one("SELECT metadata FROM hub_versions")
    assert row is not None and json.loads(row["metadata"]) == expected


def test_a_not_owned_pack_is_validated_inside_the_budget(tmp_path: Path) -> None:
    """A pack from a registry whose schema fans one pattern out a million times: each dataset
    record and each behaviour test is checked within the CPU budget a decision gives it, and the
    stop is a problem, not a hang."""

    def noted(schema: dict[str, Any]) -> None:
        schema["properties"]["note"] = {"type": "string", "$ref": "#/$defs/t0"}
        schema["$defs"] = fan_out(22)

    def declared(manifest: dict[str, Any]) -> None:
        manifest.setdefault("evaluation", {})["datasets"] = {"examples": "data/examples.jsonl"}

    root = sorter(tmp_path, schema=noted, manifest=declared, pack_id="someone/visit-sorter")
    (root / "data").mkdir()
    record = {"id": "c1", "input": {"message": "my bill", "note": "x"}, "labels": {}}
    (root / "data" / "examples.jsonl").write_text(json.dumps(record) + "\n")
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text() + '- name: noted\n  input: {message: "my bill", note: x}\n'
        "  expect: {recommendation: billing}\n"
    )
    hub = load_pack(root, origin="hub")
    with cpu_time() as spent:
        found = validation.problems(hub)
    assert spent() < 2 * BOUND_S  # one budget for the test, one for the record
    kinds = {(p.kind, p.place) for p in found if "stopped" in p.text}
    assert ("dataset", "case c1") in kinds
    assert any(kind == "tests" for kind, _ in kinds)


def test_a_problem_names_a_file_and_a_place_and_never_case_text(tmp_path: Path) -> None:
    """A dataset record and a behaviour test whose inputs hold planted text in a place the
    schema refuses, and a record whose label is not the pack's: each problem names the file and
    the case, and no problem holds the text."""

    def declared(manifest: dict[str, Any]) -> None:
        manifest.setdefault("evaluation", {})["datasets"] = {"examples": "data/examples.jsonl"}

    root = sorter(tmp_path, manifest=declared)
    (root / "data").mkdir()
    records = [
        {"id": "c1", "input": {"message": {"text": SECRET}}, "labels": {"topic": "billing"}},
        {"id": "c2", "input": {"message": "a bill"}, "labels": {"topic": SECRET}},
    ]
    (root / "data" / "examples.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    cases = root / "tests" / "cases.yaml"
    cases.write_text(
        cases.read_text() + f"\n- name: planted\n  input: {{message: [{SECRET!r}]}}\n"
        "  expect: {not_outcome: AUTO}\n"
    )
    found = validation.problems(load_pack(root, origin="community"))
    assert {(p.kind, p.file, p.place) for p in found} == {
        ("tests", "tests/cases.yaml", f"case {_case_number(load_pack(root), 'planted')}"),
        ("dataset", "data/examples.jsonl", "case c1"),
        ("dataset", "data/examples.jsonl", "case c2"),
    }
    for problem in found:
        assert SECRET not in json.dumps(problem.to_json())
        assert problem.text.startswith(problem.file)
