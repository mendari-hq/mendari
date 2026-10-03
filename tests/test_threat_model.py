"""The threat model stays true to the code (docs/security/threat-model.md): every threat has one
id, the rows run in STRIDE order with numbers in order, and every test a row names as "Checked
by" exists. Parallel lanes once added rows with the same id; this catches that and a test that
was renamed or removed."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THREAT_MODEL = ROOT / "docs" / "security" / "threat-model.md"
ROW = re.compile(r"^\| ([STRIDE])(\d+) \|")
STRIDE = "STRIDE"


def rows() -> list[str]:
    return [line for line in THREAT_MODEL.read_text().splitlines() if ROW.match(line)]


def test_every_threat_has_one_id_and_the_rows_are_in_order() -> None:
    ids = []
    for line in rows():
        match = ROW.match(line)
        assert match is not None
        ids.append((STRIDE.index(match[1]), int(match[2])))
    assert ids, "no threat rows found"
    assert len(ids) == len(set(ids)), "an id is used twice"
    assert ids == sorted(ids), "rows are not in STRIDE order with their numbers in order"


def test_every_test_a_row_names_exists() -> None:
    tests = "\n".join(path.read_text() for path in (ROOT / "tests").rglob("test_*.py"))
    defined = set(re.findall(r"^\s*def (test_\w+)\(", tests, flags=re.MULTILINE))
    named = set()
    for line in rows():
        checked_by = line.rstrip(" |").rsplit(" | ", 1)[-1]
        named |= set(re.findall(r"`(test_\w+)`", checked_by))
        for path in re.findall(r"`(tests/[\w/.]+)`", checked_by):
            assert (ROOT / path).exists(), path
    assert named, "no tests named"
    assert sorted(named - defined) == []
