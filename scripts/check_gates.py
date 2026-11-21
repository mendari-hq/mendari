# SPDX-License-Identifier: Apache-2.0
"""The launch gates (launch/gates.yaml): prints each gate with what is met and what is not, and
exits 1 when asked to assert a gate that is not met. A report never fails for an unmet gate; it
fails (exit 2) only when the file itself is wrong: a shape it does not take, an unknown probe, a
test that does not exist or cannot be collected, an attestation on a build prerequisite, a
half-filled attestation.

    uv run python scripts/check_gates.py                      # the table (CI runs this)
    uv run python scripts/check_gates.py --assert G1-2 G2-3   # exit 1 unless both are met
    uv run python scripts/check_gates.py --collect-only       # tests exist; none is run

A tests probe is met when every test it names was collected and passed: a skipped test proves
nothing, so a test run on both databases needs MENDARI_TEST_DATABASE_URL (Mendari's own
Postgres, CLAUDE.md) for its Postgres case, as CI gives it. A name without a [case] covers each
of its cases. Tests run in one pytest process, from the repository root, with the default marker
filter off (so a browser test runs, and needs Chromium: `uv run playwright install chromium`)
and none of the caller's PYTEST_ADDOPTS or PYTEST_PLUGINS.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
GATES = Path("launch/gates.yaml")
OWNERS = ("build", "owner")
KINDS = ("tests", "file", "setting", "attested", "pending")
GATE_KEYS = {"id", "name", "prerequisites"}
PREREQUISITE_KEYS = {"id", "what", "owner", "probes"}
# A test id, never an option: pytest reads an argument that starts with `-` as one.
TEST_ID = re.compile(r"^tests/[\w/.-]+\.py::\w[^\s]*$")
REPORT_ENV = "CHECK_GATES_REPORT"  # where this file, loaded as a pytest plugin, writes outcomes
UNCOLLECTABLE = "uncollectable"


class GatesError(Exception):
    """The gates file is wrong (not a gate that is unmet)."""


@dataclass
class Result:
    met: bool
    says: str


@dataclass
class Prerequisite:
    id: str
    what: str
    owner: str
    probes: list[dict[str, Any]]
    results: list[Result] = field(default_factory=list)

    @property
    def met(self) -> bool:
        return all(result.met for result in self.results)


@dataclass
class Gate:
    id: str
    name: str
    prerequisites: list[Prerequisite]

    @property
    def met(self) -> list[Prerequisite]:
        return [p for p in self.prerequisites if p.met]

    @property
    def is_met(self) -> bool:
        return bool(self.prerequisites) and len(self.met) == len(self.prerequisites)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def load(path: Path) -> list[Gate]:
    """The gates, checked for shape: every problem is named, never guessed past."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("gates"), list) or not data["gates"]:
        raise GatesError("the file is `gates:`, a list of gates")
    problems: list[str] = []
    gates: list[Gate] = []
    gate_ids: set[str] = set()
    seen: set[str] = set()
    for gate in data["gates"]:
        if not isinstance(gate, dict) or not _text(gate.get("id")) or set(gate) - GATE_KEYS:
            problems.append(f"a gate is {{id, name, prerequisites}}: {gate!r}")
            continue
        if gate["id"] in gate_ids:
            problems.append(f"{gate['id']}: the gate id is used twice")
        gate_ids.add(gate["id"])
        items = gate.get("prerequisites")
        if not isinstance(items, list) or not items:
            problems.append(f"{gate['id']}: no prerequisite")
            continue
        prerequisites = []
        for item in items:
            if not isinstance(item, dict) or not _text(item.get("id")):
                problems.append(f"{gate['id']}: a prerequisite is {{id, what, owner, probes}}")
                continue
            where = f"{gate['id']}/{item['id']}"
            if extra := set(item) - PREREQUISITE_KEYS:
                problems.append(f"{where}: unknown keys {sorted(extra)}")
            if item["id"] in seen:
                problems.append(f"{where}: the id is used twice")
            seen.add(item["id"])
            if item.get("owner") not in OWNERS:
                problems.append(f"{where}: owner is build or owner, not {item.get('owner')!r}")
            probes = item.get("probes")
            if not isinstance(probes, list) or not probes:
                problems.append(f"{where}: no probe")
                continue
            kinds = []
            for probe in probes:
                kind = next(iter(probe), None) if isinstance(probe, dict) else None
                if not isinstance(probe, dict) or len(probe) != 1 or kind not in KINDS:
                    problems.append(f"{where}: a probe is one of {', '.join(KINDS)}: {probe!r}")
                    continue
                kinds.append(kind)
                problems += [f"{where}: {p}" for p in _probe_problems(kind, probe[kind])]
            attested = "attested" in kinds
            if item.get("owner") == "owner" and not attested:
                problems.append(f"{where}: an owner prerequisite needs the owner's attestation")
            if item.get("owner") == "build" and attested:
                problems.append(f"{where}: only the owner attests; a build prerequisite is probed")
            prerequisites.append(
                Prerequisite(item["id"], str(item.get("what", "")), item.get("owner"), probes)
            )
        gates.append(Gate(gate["id"], str(gate.get("name", "")), prerequisites))
    if problems:
        raise GatesError("\n".join(problems))
    return gates


def _probe_problems(kind: str, value: Any) -> list[str]:
    if kind == "tests":
        if not isinstance(value, list) or not value:
            return ["tests: a list of test ids"]
        return [f"tests: {t!r} is not a test id (tests/….py::name)" for t in value
                if not isinstance(t, str) or not TEST_ID.match(t)]  # fmt: skip
    if kind in ("file", "pending"):
        return [] if _text(value) else [f"{kind}: a non-empty text"]
    if kind == "setting":
        ok = isinstance(value, dict) and set(value) == {"file", "key", "equals"}
        if not ok or not _text(value["file"]) or not _text(value["key"]):
            return ["setting: {file, key, equals}, the file and the key as text"]
        return []
    # attested. (`date`, not `on`: YAML reads a bare `on` as true.) `digests` binds it to packs.
    if not isinstance(value, dict) or not {"what", "by", "date"} <= set(value) <= {
        "what", "by", "date", "digests",
    }:  # fmt: skip
        return ["attested: {what, by, date}, and digests where it is bound to the packs"]
    by, date = str(value["by"] or "").strip(), str(value["date"] or "").strip()
    if bool(by) != bool(date):
        return ["attested: by and date are filled in together (a name and a date)"]
    if date:
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            return [f"attested: date is YYYY-MM-DD, not {date!r}"]
    digests = value.get("digests", {})
    if not isinstance(digests, dict) or (by and "digests" in value and not digests):
        return ["attested: digests maps each pack id to the digest attested (all packs/)"]
    return []


def _setting(root: Path, file: str, key: str) -> Any:
    path = root / file
    text = path.read_text(encoding="utf-8")
    value: Any = (
        tomllib.loads(text)
        if path.suffix == ".toml"
        else json.loads(text)
        if path.suffix == ".json"
        else yaml.safe_load(text)
    )
    for part in key.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def _pack_digests(root: Path) -> dict[str, str]:
    """Each pack under packs/: its id and the digest it has now."""
    from mendari.core.pack import load_pack  # only when an attestation names digests

    packs = (load_pack(manifest.parent) for manifest in sorted(root.glob("packs/*/pack.yaml")))
    return {pack.id: pack.digest for pack in packs}


def _pytest(root: Path, args: list[str]) -> dict[str, str]:
    """Run pytest from the repository root with this file as a plugin: node id -> outcome
    ("collected" with --collect-only; else "passed", "failed" or "skipped")."""
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "report.json"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
        env[REPORT_ENV] = str(report)
        # The checked repository's own source first, then this file (the plugin).
        paths = [str(root.resolve() / "src"), str(Path(__file__).parent)]
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [*paths, os.environ.get("PYTHONPATH")]))
        command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts="]
        ran = subprocess.run(
            [*command, "-q", "-p", "check_gates", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )
        if not report.exists():
            tail = "\n".join((ran.stdout + ran.stderr).splitlines()[-20:])
            raise GatesError(f"pytest exited {ran.returncode} before reporting, in {root}:\n{tail}")
        outcomes: dict[str, str] = json.loads(report.read_text())
        return outcomes


def _matches(probe: str, nodeids: dict[str, str]) -> list[str]:
    return [
        n
        for n, outcome in nodeids.items()
        if outcome != UNCOLLECTABLE
        and (n == probe or (n.startswith(probe + "[") and "[" not in probe))
    ]


def check(gates: list[Gate], root: Path, run: bool = True, *, workers: str | None = None) -> None:
    """Fill in each prerequisite's results. Tests are collected (and run) once, together."""
    wanted = [
        test
        for gate in gates
        for p in gate.prerequisites
        for probe in p.probes
        for test in probe.get("tests", [])
    ]
    files = sorted({test.split("::")[0] for test in wanted})
    missing_files = [f for f in files if not (root / f).is_file()]
    if missing_files:
        raise GatesError("\n".join(f"{f}: no such test file" for f in missing_files))
    collected = _pytest(root, ["--collect-only", *files]) if files else {}
    if broken := sorted(n for n, outcome in collected.items() if outcome == UNCOLLECTABLE):
        raise GatesError("\n".join(f"{n}: could not be collected" for n in broken))
    unknown = [test for test in wanted if not _matches(test, collected)]
    if unknown:
        raise GatesError("\n".join(f"{t}: no such test" for t in unknown))
    chosen = sorted({n for test in wanted for n in _matches(test, collected)})
    run_args = ["-n", workers, "--dist", "loadscope", *chosen] if workers else chosen
    outcomes = _pytest(root, run_args) if run and chosen else {}
    digests: dict[str, str] | None = None
    for gate in gates:
        for p in gate.prerequisites:
            results = []
            for probe in p.probes:
                if "digests" in probe.get("attested", {}) and digests is None:
                    digests = _pack_digests(root)
                results.append(_result(probe, root, collected, outcomes, run, digests or {}))
            p.results = results


def _result(
    probe: dict[str, Any],
    root: Path,
    collected: dict[str, str],
    outcomes: dict[str, str],
    run: bool,
    digests: dict[str, str],
) -> Result:
    kind, value = next(iter(probe.items()))
    if kind == "tests":
        cases = [n for test in value for n in _matches(test, collected)]
        if not run:
            return Result(False, f"{len(cases)} tests exist (not run)")
        failed = [n for n in cases if outcomes.get(n) != "passed"]
        if failed:
            states = {n: outcomes.get(n, "not run") for n in failed}
            first = ", ".join(f"{n} {s}" for n, s in list(states.items())[:3])
            more = f" and {len(failed) - 3} more" if len(failed) > 3 else ""
            return Result(False, f"{len(failed)} of {len(cases)} tests did not pass: {first}{more}")
        return Result(True, f"{len(cases)} tests passed")
    if kind == "file":
        return Result(
            (root / value).is_file(), value if (root / value).is_file() else f"no {value}"
        )
    if kind == "setting":
        try:
            found = _setting(root, value["file"], value["key"])
        except (OSError, KeyError, IndexError, ValueError, TypeError) as error:
            return Result(
                False, f"{value['file']}: {value['key']} not readable ({type(error).__name__})"
            )
        said = f"{value['file']}: {value['key']} is {found!r}"
        # The type too: YAML's `yes` is true, and JSON's 1 equals true in Python.
        expected = value["equals"]
        return Result(type(found) is type(expected) and found == expected, said)
    if kind == "attested":
        by, date = str(value["by"] or "").strip(), str(value["date"] or "").strip()
        if not by:
            return Result(False, "not attested")
        if "digests" in value and value["digests"] != digests:
            changed = sorted(set(digests) ^ set(value["digests"]) | {
                pack for pack in set(digests) & set(value["digests"])
                if digests[pack] != value["digests"][pack]
            })  # fmt: skip
            return Result(False, f"void: attested by {by} on {date} for other digests: {changed}")
        return Result(True, f"attested by {by} on {date}")
    return Result(False, f"pending: {value}")


def table(gates: list[Gate]) -> str:
    lines = ["| Gate | Met | Not met |", "|---|---|---|"]
    for gate in gates:
        unmet = [p.id for p in gate.prerequisites if not p.met]
        count = f"{len(gate.met)} of {len(gate.prerequisites)}"
        lines.append(f"| {gate.id} {gate.name} | {count} | {', '.join(unmet) or 'none'} |")
    for gate in gates:
        lines += ["", f"{gate.id} {gate.name}: {len(gate.met)} of {len(gate.prerequisites)}"]
        for p in gate.prerequisites:
            mark = "x" if p.met else " "
            lines.append(f"- [{mark}] {p.id} ({p.owner}): {'; '.join(r.says for r in p.results)}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Report the launch gates (launch/gates.yaml).")
    parser.add_argument("--assert", dest="asserted", nargs="+", metavar="GATE", default=[])
    parser.add_argument("--collect-only", action="store_true", help="check tests exist; run none")
    parser.add_argument("--file", type=Path, default=GATES, help="the gates file (from --root)")
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="the repository to check (its tests run in this environment: for another "
        "checkout, run that checkout's own script)",
    )
    parser.add_argument(
        "--workers",
        default=None,
        help="pytest-xdist worker count for the test run (e.g. auto or 4)",
    )
    args = parser.parse_args(argv)
    try:
        gates = load(args.root / args.file)
        known = {gate.id for gate in gates}
        if unknown := [g for g in args.asserted if g not in known]:
            raise GatesError(f"no such gate: {', '.join(unknown)} (the gates: {', '.join(known)})")
        check(gates, args.root, run=not args.collect_only, workers=args.workers)
    except (GatesError, OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        print(f"{args.file}: {error}", file=sys.stderr)
        return 2
    print(table(gates))
    failing = [g for g in gates if g.id in args.asserted and not g.is_met]
    for gate in failing:
        unmet = ", ".join(p.id for p in gate.prerequisites if not p.met)
        print(f"{gate.id} is not met: {unmet}", file=sys.stderr)
    return 1 if failing else 0


# As a pytest plugin (`-p check_gates`, from _pytest above): write each test's outcome. Every
# hook does nothing unless REPORT_ENV is set, as only _pytest sets it.
_outcomes: dict[str, str] = {}


def pytest_collectreport(report: Any) -> None:
    if os.environ.get(REPORT_ENV) and report.failed:
        _outcomes[report.nodeid] = UNCOLLECTABLE


def pytest_collection_finish(session: Any) -> None:
    if os.environ.get(REPORT_ENV) and session.config.option.collectonly:
        _outcomes.update({item.nodeid: "collected" for item in session.items})


def pytest_runtest_logreport(report: Any) -> None:
    if not os.environ.get(REPORT_ENV):
        return
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif report.skipped and _outcomes.get(report.nodeid) != "failed":
        _outcomes[report.nodeid] = "skipped"  # an expected failure too
    elif report.when == "call" and report.passed:
        _outcomes.setdefault(report.nodeid, "passed")


def pytest_sessionfinish(session: Any) -> None:
    # Under pytest-xdist, only the controller has every worker's logreport outcomes.
    if hasattr(session.config, "workerinput"):
        return
    if path := os.environ.get(REPORT_ENV):
        Path(path).write_text(json.dumps(_outcomes), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
