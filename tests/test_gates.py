# SPDX-License-Identifier: Apache-2.0
"""The launch gates (launch/gates.yaml, scripts/check_gates.py; LP-03): the file holds the
audit's five gates and their prerequisites, every test it names exists, only the owner attests,
and the check reports each probe kind truly, asserting a gate only when asked."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari.core.pack import load_pack

ROOT = Path(__file__).resolve().parents[1]


def _script() -> Any:
    path = ROOT / "scripts" / "check_gates.py"
    spec = importlib.util.spec_from_file_location("check_gates_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _script()


# The real file.


def test_the_file_holds_the_audits_gates_in_order() -> None:
    """notes/vision-audit.md, "Stage gates at this commit": five gates, with 6, 8, 7, 6 and 6
    prerequisites in the order notes/vision-audit-evidence.md lists them."""
    gates = check.load(ROOT / check.GATES)
    assert [(g.id, len(g.prerequisites)) for g in gates] == [
        ("G1-2", 6),
        ("G2-3", 8),
        ("G3-4", 7),
        ("G4-5", 6),
        ("G5-6", 6),
    ]


def test_every_test_the_file_names_exists(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(["--collect-only"]) == 0, capsys.readouterr().err
    assert "| G1-2 Stage 1 to 2: design partners |" in capsys.readouterr().out


def test_no_build_session_attested_for_the_owner() -> None:
    """Every attestation in the file is the owner's to fill in; none was filled in by the build
    that wrote it (LP-03: "Do not mark an owner prerequisite met"). The owner's own commit that
    fills one in changes this list. Three were attested by the owner in chat on 2026-11-07 and
    recorded at the owner's instruction: nda-pilot-agreement (no design partners, waived),
    patent-decision (no provisional, decided with counsel) and rotation (the scan's report read:
    none found)."""
    attested = {
        p["id"]: probe["attested"]
        for gate in yaml.safe_load((ROOT / check.GATES).read_text())["gates"]
        for p in gate["prerequisites"]
        for probe in p["probes"]
        if "attested" in probe
    }
    assert len(attested) == 17
    assert {name: (a["by"], a["date"]) for name, a in attested.items() if a["by"]} == {
        "nda-pilot-agreement": ("the owner (in chat)", "2026-11-07"),
        "patent-decision": ("the owner (in chat)", "2026-11-07"),
        "rotation": ("the owner (in chat)", "2026-11-07"),
    }


# The check, on a project of its own.

PROBES = """
import pytest

def test_passes():
    pass

def test_fails():
    assert False

def test_skips():
    pytest.skip("not here")

@pytest.mark.parametrize("case", ["a", "b"])
def test_cases(case):
    assert case == "a"
"""


def project(tmp_path: Path, gates: str) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_probes.py").write_text(PROBES)
    (tmp_path / "launch").mkdir()
    (tmp_path / "launch" / "gates.yaml").write_text(gates)
    (tmp_path / "RUNBOOK.md").write_text("# Runbook\n")
    (tmp_path / "rules.json").write_text('{"rules": [{"action": "log"}]}')
    return tmp_path


T = "tests/test_probes.py::"


def prerequisite(name: str, owner: str, *probes: dict[str, Any]) -> dict[str, Any]:
    return {"id": name, "what": name, "owner": owner, "probes": list(probes)}


def setting(key: str, equals: str) -> dict[str, Any]:
    return {"setting": {"file": "rules.json", "key": key, "equals": equals}}


def attested(by: str, date: str) -> dict[str, Any]:
    return {"attested": {"what": "done", "by": by, "date": date}}


GATES = yaml.safe_dump(
    {
        "gates": [
            {
                "id": "GA",
                "name": "All met",
                "prerequisites": [
                    prerequisite("passing", "build", {"tests": [T + "test_passes"]}),
                    prerequisite("case-a", "build", {"tests": [T + "test_cases[a]"]}),
                    prerequisite("runbook", "build", {"file": "RUNBOOK.md"}),
                    prerequisite("logged", "build", setting("rules.0.action", "log")),
                    prerequisite("signed", "owner", attested("The Owner", "2026-10-07")),
                ],
            },
            {
                "id": "GB",
                "name": "None met",
                "prerequisites": [
                    prerequisite("failing", "build", {"tests": [T + "test_fails"]}),
                    prerequisite("skipped", "build", {"tests": [T + "test_skips"]}),
                    prerequisite("every-case", "build", {"tests": [T + "test_cases"]}),
                    prerequisite("no-file", "build", {"file": "docs/missing.md"}),
                    prerequisite("enforced", "build", setting("rules.0.action", "deny")),
                    prerequisite("unread", "build", setting("rules.3.action", "deny")),
                    prerequisite("later", "build", {"pending": "LP-99"}),
                    prerequisite("unsigned", "owner", attested("", "")),
                    prerequisite(
                        "half", "build", {"tests": [T + "test_passes"]}, {"pending": "LP-98"}
                    ),
                ],
            },
        ]
    }
)


def test_each_probe_kind_is_reported_truly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path, GATES)
    assert check.main(["--root", str(root)]) == 0  # a report, whatever is met
    out = capsys.readouterr().out
    assert "| GA All met | 5 of 5 | none |" in out
    assert (
        "| GB None met | 0 of 9 | failing, skipped, every-case, no-file, enforced, unread, later,"
        " unsigned, half |"
    ) in out
    for line in (
        "- [x] passing (build): 1 tests passed",
        "- [x] case-a (build): 1 tests passed",
        "- [x] runbook (build): RUNBOOK.md",
        "- [x] logged (build): rules.json: rules.0.action is 'log'",
        "- [x] signed (owner): attested by The Owner on 2026-10-07",
        "- [ ] failing (build): 1 of 1 tests did not pass: tests/test_probes.py::test_fails failed",
        "- [ ] skipped (build): 1 of 1 tests did not pass: tests/test_probes.py::test_skips"
        " skipped",
        "- [ ] every-case (build): 1 of 2 tests did not pass: tests/test_probes.py::test_cases[b]"
        " failed",
        "- [ ] no-file (build): no docs/missing.md",
        "- [ ] enforced (build): rules.json: rules.0.action is 'log'",
        "- [ ] unread (build): rules.json: rules.3.action not readable (IndexError)",
        "- [ ] later (build): pending: LP-99",
        "- [ ] unsigned (owner): not attested",
        "- [ ] half (build): 1 tests passed; pending: LP-98",
    ):
        assert line in out, line


def test_a_gate_is_asserted_only_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path, GATES)
    assert check.main(["--root", str(root), "--assert", "GA"]) == 0
    assert check.main(["--root", str(root), "--assert", "GA", "GB"]) == 1
    err = capsys.readouterr().err
    assert "GB is not met: failing, skipped, every-case" in err and "GA is not met" not in err
    assert check.main(["--root", str(root), "--assert", "GC"]) == 2
    assert "no such gate: GC" in capsys.readouterr().err


def test_workers_flag_reports_all_outcomes_under_xdist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path, GATES)
    assert check.main(["--root", str(root), "--workers", "2"]) == 0
    out = capsys.readouterr().out
    assert "| GA All met | 5 of 5 | none |" in out
    assert "- [x] passing (build): 1 tests passed" in out
    assert (
        "- [ ] failing (build): 1 of 1 tests did not pass: tests/test_probes.py::test_fails failed"
        in out
    )


def test_collect_only_runs_nothing_and_meets_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path, GATES)
    assert check.main(["--root", str(root), "--collect-only"]) == 0
    out = capsys.readouterr().out
    assert "- [ ] passing (build): 1 tests exist (not run)" in out
    assert "- [ ] every-case (build): 2 tests exist (not run)" in out


ONE = """
gates:
  - id: G
    name: g
    prerequisites:
      - {{id: p, what: x, owner: {owner}, probes: [{probe}]}}
"""


@pytest.mark.parametrize(
    ("owner", "probe", "says"),
    [
        ("build", "{tests: [tests/test_probes.py::test_gone]}", "test_gone: no such test"),
        ("build", "{tests: [tests/test_nowhere.py::test_x]}", "no such test file"),
        ("build", "{test: [tests/test_probes.py::test_passes]}", "a probe is one of"),
        ("build", "{attested: {what: x, by: '', date: ''}}", "only the owner attests"),
        ("owner", "{pending: LP-99}", "needs the owner's attestation"),
        ("owner", "{attested: {what: x, by: Someone, date: ''}}", "filled in together"),
        ("owner", "{attested: {what: x, by: Someone, date: 7 Oct}}", "YYYY-MM-DD"),
        ("owner", "{attested: {what: x, by: '', on: ''}}", "{what, by, date}"),
        ("crew", "{pending: LP-99}", "owner is build or owner"),
        ("build", "{setting: {file: rules.json, key: a}}", "{file, key, equals}"),
        ("build", "{setting: {file: rules.json, key: 5, equals: x}}", "the key as text"),
        ("build", "{tests: ['-p', tests/test_probes.py::test_passes]}", "'-p' is not a test id"),
        ("owner", "{attested: {what: x, by: A, date: '2026-10-07', digests: {}}}", "digests maps"),
    ],
)
def test_a_wrong_file_is_named_and_exits_2(
    owner: str, probe: str, says: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path, ONE.format(owner=owner, probe=probe))
    assert check.main(["--root", str(root)]) == 2
    assert says in capsys.readouterr().err


@pytest.mark.parametrize(
    ("gates", "says"),
    [
        ("gates:\n  - {id: G, name: g, prerequisite: []}\n", "a gate is {id, name, prerequisites}"),
        ("gates:\n  - {id: G, name: g, prerequisites: []}\n", "G: no prerequisite"),
        ("gates:\n  - G1\n", "a gate is {id, name, prerequisites}"),
        ("gates:\n  - {id: G, prerequisites: [P1]}\n", "a prerequisite is"),
        ("gates: {G: 1}\n", "a list of gates"),
        (
            "gates:\n"
            "  - {id: G, prerequisites: [{id: a, what: x, owner: build, probes: [{pending: b}]}]}\n"
            "  - {id: G, prerequisites: [{id: c, owner: build, probes: [{pending: d}]}]}\n",
            "G: the gate id is used twice",
        ),
    ],
)
def test_a_file_of_another_shape_is_named_and_exits_2(
    gates: str, says: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path, gates)
    assert check.main(["--root", str(root), "--assert", "G"]) == 2
    assert says in capsys.readouterr().err


def test_a_file_that_is_not_text_exits_2(tmp_path: Path) -> None:
    root = project(tmp_path, "")
    (root / "launch" / "gates.yaml").write_bytes(b"gates: \xff\xfe")
    assert check.main(["--root", str(root)]) == 2


def test_a_setting_is_compared_with_its_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = "{setting: {file: flags.json, key: enabled, equals: true}}"
    root = project(tmp_path, ONE.format(owner="build", probe=probe))
    (root / "flags.json").write_text('{"enabled": 1}')  # 1 == True in Python, but not the same
    assert check.main(["--root", str(root)]) == 0
    assert "- [ ] p (build): flags.json: enabled is 1" in capsys.readouterr().out


def test_a_module_that_cannot_be_collected_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path, ONE.format(owner="build", probe="{tests: [tests/test_broken.py::t]}"))
    (root / "tests" / "test_broken.py").write_text("import no_such_module\n\ndef t(): pass\n")
    assert check.main(["--root", str(root)]) == 2
    assert "tests/test_broken.py: could not be collected" in capsys.readouterr().err


def test_the_callers_pytest_options_are_not_the_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path, GATES)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--deselect tests/test_probes.py::test_passes")
    assert check.main(["--root", str(root), "--assert", "GA"]) == 0
    assert "- [x] passing (build): 1 tests passed" in capsys.readouterr().out


def test_an_attestation_bound_to_packs_is_void_once_one_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path, "")
    shutil.copytree(ROOT / "packs" / "ai-guard", root / "packs" / "ai-guard")
    digest = load_pack(root / "packs" / "ai-guard").digest

    def attest(digests: str) -> int:
        probe = f"{{attested: {{what: x, by: The Owner, date: '2026-10-07', digests: {digests}}}}}"
        (root / "launch" / "gates.yaml").write_text(ONE.format(owner="owner", probe=probe))
        return int(check.main(["--root", str(root), "--assert", "G"]))

    assert attest(f"{{mendari/ai-guard: '{digest}'}}") == 0
    assert "- [x] p (owner): attested by The Owner on 2026-10-07" in capsys.readouterr().out
    assert attest("{mendari/ai-guard: 'sha256:0'}") == 1
    out = capsys.readouterr().out
    assert (
        "void: attested by The Owner on 2026-10-07 for other digests: ['mendari/ai-guard']" in out
    )
