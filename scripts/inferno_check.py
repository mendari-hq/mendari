# SPDX-License-Identifier: Apache-2.0
"""Compare an Inferno run with its expected results (sandbox/inferno/README.md).

The expected results (`sandbox/inferno/<suite>_expected.json`) list every test the run executes,
by its Inferno id, with the result it gives today: `pass`, `fail`, `skip` or `omit` (Inferno's
result for a test that does not apply, such as the `$next-question` tests when no form is
adaptive). Each result that is not a pass names the requirement ids the kit says the test
verifies and the reason Mendari gives that result. Any difference fails: a test that newly
fails, a known gap that now passes (update the expected results in the same pull request, so
the list only ever shrinks by a decision), a test the run did not execute, and a test the
expected results do not list. A run that ended in an error (Inferno's `error`, or no results at
all) fails too.

The run's raw output (`inferno execute --outputter json`, whose log lines come first) stays out
of the repository: it can quote request and response bodies.

    uv run python scripts/inferno_check.py var/inferno/dtr_payer_server_v220_results.json \\
      --expected sandbox/inferno/dtr_payer_server_v220_expected.json \\
      --tests var/inferno/dtr_payer_server_v220_tests.json

`--tests` is the run's list of tests (sandbox/inferno/list_tests.rb): it adds short ids and
titles to what is printed. With `--write`, the expected results are written from the run
instead, keeping each reason whose result did not change; a result without a reason must get
one before the check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

RESULTS = ("pass", "fail", "skip", "omit")
# What the expected results say of the kit, besides the tests.
HEADER = ("kit", "version", "commit", "suite", "groups")


class CheckError(ValueError):
    """The run's output or the expected results cannot be read as they should."""


def run_results(raw: str) -> dict[str, str]:
    """Each test's result in `inferno execute --outputter json` output, by test id. The JSON
    comes after Inferno's log lines; a run that stopped before any test (a missing input, an
    unknown suite) prints its error as a JSON string instead."""
    decoder = json.JSONDecoder()
    lines = raw.splitlines(keepends=True)
    offset = 0
    for line in lines:
        if line.rstrip("\r\n") == "[":
            results, _ = decoder.raw_decode(raw[offset:])
            break
        offset += len(line)
    else:
        for line in lines:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, str):
                raise CheckError(f"Inferno stopped before any test ran: {message}")
        raise CheckError("no Inferno results found (expected `inferno execute --outputter json`)")
    found: dict[str, str] = {}
    for result in results:
        test = result.get("test_id") if isinstance(result, Mapping) else None
        if test is None:
            continue  # a group's or the suite's own result: its tests are compared one by one
        if test in found:
            raise CheckError(f"{test}: two results in one run")
        found[test] = str(result.get("result"))
    if not found:
        raise CheckError("the run has no test results")
    return found


def expected_results(golden: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The expected results by test id, each entry checked: a known result, and for any result
    but a pass, a reason; a failure or a skip also names its requirement ids."""
    missing = [key for key in HEADER if not golden.get(key)]
    if missing:
        raise CheckError(f"the expected results lack {', '.join(missing)}")
    entries: dict[str, dict[str, Any]] = {}
    for entry in golden.get("tests") or []:
        test = entry.get("id")
        if not test:
            raise CheckError(f"an expected result without an id: {entry}")
        if test in entries:
            raise CheckError(f"{test}: listed twice")
        result = entry.get("result")
        if result not in RESULTS:
            raise CheckError(f"{test}: result {result!r} is not one of {', '.join(RESULTS)}")
        requirements = entry.get("requirements")
        if not isinstance(requirements, list) or not all(isinstance(r, str) for r in requirements):
            raise CheckError(f"{test}: requirements must be a list of requirement ids")
        if result != "pass" and not str(entry.get("reason") or "").strip():
            raise CheckError(f"{test}: a {result} needs its reason")
        if result in ("fail", "skip") and not requirements:
            raise CheckError(f"{test}: a {result} needs the requirement ids it leaves unmet")
        entries[test] = dict(entry)
    if not entries:
        raise CheckError("the expected results list no test")
    return entries


def differences(found: Mapping[str, str], expected: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """What differs between the run and the expected results, one line each."""
    lines = []
    for test, result in found.items():
        entry = expected.get(test)
        if entry is None:
            lines.append(f"{test}: {result}, but the expected results do not list this test")
            continue
        wanted = entry["result"]
        if result == wanted:
            continue
        where = entry.get("short_id") or test
        if result == "pass":
            lines.append(
                f"{where}: passes, expected {wanted} ({entry['reason']}): if the gap is closed, "
                "record the pass in the expected results"
            )
        elif wanted == "pass":
            lines.append(f"{where}: {result}, expected pass (a new failure)")
        else:
            lines.append(f"{where}: {result}, expected {wanted} ({entry['reason']})")
    for test, entry in expected.items():
        if test not in found:
            lines.append(f"{entry.get('short_id') or test}: expected {entry['result']}, not run")
    return lines


def listed_tests(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    listing = json.loads(path.read_text(encoding="utf-8"))
    return {test["id"]: test for test in listing.get("tests") or []}


def written(
    found: Mapping[str, str],
    golden: Mapping[str, Any],
    tests: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """New expected results from a run: the run's results, in Inferno's order (by short id),
    each reason kept where the result is unchanged and left empty where it changed."""
    before = {entry.get("id"): entry for entry in golden.get("tests") or []}

    def order(test: str) -> tuple[int, ...] | tuple[float]:
        short = str(tests.get(test, {}).get("short_id") or before.get(test, {}).get("short_id"))
        try:
            return tuple(int(part) for part in short.split("."))
        except ValueError:
            return (float("inf"),)

    entries = []
    for test in sorted(found, key=lambda t: (order(t), t)):
        known = tests.get(test, {})
        old = before.get(test, {})
        entry: dict[str, Any] = {
            "id": test,
            "short_id": known.get("short_id") or old.get("short_id"),
            "title": known.get("title") or old.get("title"),
            "result": found[test],
            "requirements": list(known.get("requirements") or old.get("requirements") or []),
        }
        if found[test] != "pass":
            entry["reason"] = old.get("reason", "") if old.get("result") == found[test] else ""
        entries.append(entry)
    return {**{key: golden.get(key) for key in HEADER}, "tests": entries}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", type=Path, help="the output of `inferno execute -o json`")
    parser.add_argument("--expected", type=Path, required=True, help="the expected results")
    parser.add_argument("--tests", type=Path, help="the run's tests (list_tests.rb)")
    parser.add_argument(
        "--write", action="store_true", help="write the expected results from this run"
    )
    args = parser.parse_args(argv)
    golden = json.loads(args.expected.read_text(encoding="utf-8"))
    tests = listed_tests(args.tests)
    try:
        found = run_results(args.results.read_text(encoding="utf-8"))
        if args.write:
            new = written(found, golden, tests)
            args.expected.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
            expected_results(new)  # says which new result still needs its reason
            print(f"{args.expected}: written from the run")
            return 0
        expected = expected_results(golden)
    except CheckError as error:
        print(f"inferno check: {error}", file=sys.stderr)
        return 1
    for test, result in found.items():
        entry = expected.get(test, {})
        short = tests.get(test, {}).get("short_id") or entry.get("short_id") or test
        wanted = entry.get("result", "not listed")
        print(f"{short:10} {result}" + ("" if result == wanted else f" (expected {wanted})"))
    problems = differences(found, expected)
    suite = golden["suite"]
    if problems:
        print(f"\n{suite}: the run differs from {args.expected}:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"\n{suite}: every test gave its expected result ({args.expected})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
