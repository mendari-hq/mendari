# SPDX-License-Identifier: Apache-2.0
"""The PHI detector (LP-20d, `core/phi_detect.py`): seven kinds found by file and line, on RE2,
never a matched value in a report, a log or an error; every file of the agents the repository
ships or tests with, synthetic datasets included, passes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from mendari.cli import main
from mendari.core import phi_detect
from mendari.core.phi_detect import KINDS, NAME_THRESHOLD, Finding, detect, detect_files, rejects
from tests.conftest import FIXTURE_AGENTS, ROOT, TRIAGE_DIR, cpu_time

# One planted line per kind: the value each holds is searched for in everything the detector
# says. Every value is made up.
PLANTED = {
    "ssn": ("SSN on file 219-09-9999 per intake", "219-09-9999"),
    "phone": ("call me back at 617-555-2368 tonight", "617-555-2368"),
    "record_number": ("chart MRN 00784512 attached", "00784512"),
    "birth_date": ("DOB 03/14/1961, still on the same dose", "03/14/1961"),
    "email": ("write to jdoe.patient@mailhost.net about it", "jdoe.patient@mailhost.net"),
    "street_address": ("now living at 4410 North Lake Drive with family", "4410 North Lake Drive"),
    "name": ("my name is Margaret and the refill is late", "Margaret"),
}


@pytest.mark.parametrize("kind", KINDS)
def test_each_kind_is_found_with_its_file_and_line(kind: str) -> None:
    line, _ = PLANTED[kind]
    text = "a first line with nothing in it\n" + line + "\n"
    assert detect(text, "data/test.jsonl") == [Finding(kind, "data/test.jsonl", 2)]
    report = detect_files({"data/test.jsonl": text.encode()})
    assert report.counts == {kind: 1} and rejects(report)


def test_no_report_log_or_error_holds_a_matched_value(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    text = "\n".join(line for line, _ in PLANTED.values())
    files = {"a.jsonl": text.encode(), "b.bin": b"\xff\xfe" + text.encode("utf-16-le")}
    report = detect_files(files)
    served = json.dumps(report.to_json())
    assert report.counts == {kind: 2 for kind in KINDS}  # b.bin read too, as UTF-16
    assert report.unread == ("b.bin",)
    for _, value in PLANTED.values():
        assert value not in served
        assert value not in caplog.text
        assert value not in repr(report)


def _shipped() -> dict[str, bytes]:
    """Every file of the bundled demo agent and of the test fixtures' agents, their synthetic
    datasets included."""
    files = {}
    for root in (ROOT / "agents", FIXTURE_AGENTS):
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files[str(path.relative_to(ROOT))] = path.read_bytes()
    return files


def test_every_shipped_synthetic_dataset_and_agent_file_passes() -> None:
    """The measurement in the module's docstring, kept: no finding of any kind, names included,
    in the bundled demo agent and the test fixtures' agents, their synthetic datasets
    included."""
    files = _shipped()
    assert len(files) > 40 and any(path.endswith(".jsonl") for path in files)
    report = detect_files(files)
    assert report.findings == () and not rejects(report)
    assert NAME_THRESHOLD == 1


def test_re2_and_pythons_engine_agree_on_every_shipped_line() -> None:
    """The patterns were rewritten without lookaround for RE2: the same sources on Python's
    `re` find the same kinds on every line of every shipped text file, and on the planted ones."""
    ours, theirs = phi_detect.re2_patterns(), phi_detect.re_patterns()
    lines = [line for line, _ in PLANTED.values()]
    for raw in _shipped().values():
        try:
            lines += raw.decode("utf-8").split("\n")
        except UnicodeDecodeError:
            continue
    assert len(lines) > 4_000  # every line of every shipped file, not a few
    for line in lines:
        assert phi_detect.kinds_of(line, ours) == phi_detect.kinds_of(line, theirs)


def test_a_name_alone_is_not_a_finding_and_a_name_beside_a_birth_date_is() -> None:
    assert "Margaret" in phi_detect.names() and "Will" not in phi_detect.names()
    assert detect("Margaret asked about her refill") == []
    assert detect("Will Margaret's refill be ready?") == []
    beside = detect("Margaret Hughes, born 03/14/1961, asked about her refill")
    assert [finding.kind for finding in beside] == ["birth_date", "name"]
    assert detect("Mrs Garcia asked about her refill")[0].kind == "name"


def test_the_fictional_phone_range_and_example_domains_pass() -> None:
    for text in (
        "call 617-555-0123 any time",
        "fax (617) 555-0199",
        "cell 6175550142",
        "write to someone@example.org or a@b.test",
        "lab result 6175552368 cells per microliter",  # ten digits, no phone word: an id
        "a newborn, seen 03/14/2024",  # "born" inside a word
    ):
        assert detect(text) == [], text


@pytest.mark.parametrize("separator", [" ", "/"])
def test_a_phone_number_right_after_another_is_found(separator: str) -> None:
    """The character after a number (not a word character or a hyphen) was matched, as RE2 has
    no lookahead, and it is also the edge before the next number: one written in the range
    kept for fiction hid a real one right after it ("617-555-0100 617-555-1234" held no phone),
    on purpose or not. A number now ends at a word boundary, which matches no character, and
    the hyphen is checked apart, so the next one is found; on both engines."""
    for engine in (phi_detect.re2_patterns(), phi_detect.re_patterns()):
        for first, second in (("617-555-0100", "617-555-1234"), ("6175550100", "6175551234")):
            assert phi_detect.kinds_of(f"cell {first}{separator}{second}", engine) == ["phone"]
            assert phi_detect.kinds_of(f"cell {first}{separator}{first}", engine) == []


def test_a_long_hostile_line_costs_one_pass() -> None:
    """Lines built to make a backtracking engine search: on RE2 each is one pass."""
    hostile = [
        "1" * 2_000_000,
        ("a@" * 300_000) + "b",
        ("Mr " * 200_000) + "x",
        ("12 " + "Abc " * 3) * 100_000,
        ("born 1/1/1 " * 100_000),
    ]
    with cpu_time() as spent:
        for line in hostile:
            detect(line)
    assert spent() < 10.0


def test_the_same_bytes_give_the_same_report() -> None:
    files = {"z.jsonl": "\n".join(line for line, _ in PLANTED.values()).encode(), "a.md": b"x"}
    first = detect_files(files).to_json()
    assert detect_files(dict(reversed(files.items()))).to_json() == first
    again = detect_files(files).to_json()
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)


def test_no_file_goes_unread_and_no_escape_hides_a_value() -> None:
    """Found by the security review of LP-20f: a file that is not UTF-8, a value written with
    JSON or YAML escapes, a value cut in two by the pieces of a long line, a path; and a YAML
    alias bomb, which cannot be read through, is refused."""
    phone = "617-555-2368"
    latin = f"Call Jane at {phone}\n".encode() + b"\xff"
    assert detect_files({"data/notes.txt": latin}).counts == {"phone": 1}
    escaped = json.dumps({"input": {"body": f"call me at {phone}"}}).replace("-", "\\u002d")
    assert "-" not in escaped
    assert detect_files({"data/x.jsonl": escaped.encode()}).findings == (
        Finding("phone", "data/x.jsonl", 1),
    )
    yaml_escaped = 'body: "call me at 617\\x2d555\\x2d2368"\n'
    assert detect_files({"tests/cases.yaml": yaml_escaped.encode()}).counts == {"phone": 1}
    boundary = phi_detect.MAX_LINE_CHARS - 5
    long = "x " * (boundary // 2) + "SSN 219-09-9999 on file"
    assert "ssn" in phi_detect.kinds_of(long)
    assert detect_files({f"data/Jane_{phone}.txt": b"x"}).findings == (
        Finding("phone", f"data/Jane_{phone}.txt", 0),
    )
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [*{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, "
        f"*{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}]\n"
        for i in range(8)
    )
    bombed = detect_files({"x.yaml": bomb.encode()})
    assert bombed.unscannable == ("x.yaml",) and rejects(bombed)


def test_data_check_phi_reports_kinds_and_places(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    planted = tmp_path / "planted.jsonl"
    line, value = PLANTED["phone"]
    record = {"id": "c1", "input": {"message": {"body": line}}, "labels": {}}
    planted.write_text(json.dumps(record) + "\n")
    assert main(["data", "check", str(TRIAGE_DIR), str(planted), "--phi", "--json"]) == 1
    printed = capsys.readouterr().out
    [report] = json.loads(printed)
    assert report["phi"]["counts"] == {"phone": 1}
    assert report["phi"]["findings"] == [{"kind": "phone", "file": "planted.jsonl", "line": 1}]
    assert value not in printed
    assert main(["data", "check", str(TRIAGE_DIR), "--phi"]) == 0
