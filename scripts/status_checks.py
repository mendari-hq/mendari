#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the status checks (deploy/status/checks.yaml, LP-21b) against a deployment: what an
operator's status page checks, for a first look before setting it up, or a job of its own.

    python scripts/status_checks.py --api https://mendari.example.org \\
        [--registry https://registry.example.org] [--hub https://hub.example.org]

One line per check: `ok`, `failed` and why, or `skipped` and why (a base URL not given, a key not
set, sign-in off or not known). Exit 1 when any check failed. Give `--api` the deployment's
public address (`MENDARI_PUBLIC_URL`): sign-in elsewhere first moves there. The decide check's
key comes from the environment variable the file names (`STATUS_DECIDE_KEY`), never an
argument; nothing printed holds a key, a body, a cookie or a header's value.

    python scripts/status_checks.py --export json --api https://mendari.example.org ...

prints the checks instead, sending nothing: each one whose base URL is given, ready to set up in
a status provider (`export`), as JSON on stdout; the ones left out are a line each on stderr. A
key the export does not know is refused, never dropped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

CHECKS = Path(__file__).resolve().parent.parent / "deploy" / "status" / "checks.yaml"
TIMEOUT_S = 10.0
_BASE = re.compile(r"^\{(api|registry|hub)\}")
# Mendari's own sign-in route. A redirect there on another host is Mendari moving sign-in to its
# public address (MENDARI_PUBLIC_URL), not the identity provider answering.
SIGN_IN_PATH = "/auth/login"
# Every key of a check the export knows, and the fields it gives each `expect` key the checker
# enforces (`_problem`). tests/test_status_checks.py holds these to what `run`, `_problem` and
# `export` read, and changes each key a check uses to see that the export changes with it: a
# key the checker starts to read fails there until it is exported.
CHECK_KEYS = frozenset({"name", "what", "request", "expect", "only_with_sign_in"})
REQUEST_KEYS = frozenset({"method", "url", "json", "key_from"})
EXPECT_FIELDS = {
    "status": ("expected_statuses",),
    "location_leaves": ("location_leaves_host", "location_path_not"),
    "json_has": ("body_keyword",),
}


@dataclass(frozen=True)
class Result:
    name: str
    outcome: str  # ok, failed or skipped
    detail: str


def load(path: Path = CHECKS) -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return spec


def run(
    spec: Mapping[str, Any],
    bases: Mapping[str, str | None],
    client_for: Callable[[str], httpx.Client],
    environ: Mapping[str, str] = os.environ,
) -> list[Result]:
    """Each check of `spec` against `bases` (`api`, `registry`, `hub`: a base URL or None), with
    the client `client_for(base)` gives (its own timeout: the command's is TIMEOUT_S)."""
    results = []
    sign_in: bool | None = None
    if bases.get("api"):
        sign_in = _sign_in(client_for("api"), str(bases["api"]))
    for check in spec["checks"]:
        name, request, expect = check["name"], check["request"], check["expect"]
        found = _BASE.match(request["url"])
        base = found.group(1) if found else ""
        root = bases.get(base)
        if not root:
            results.append(Result(name, "skipped", f"no --{base} given"))
            continue
        if check.get("only_with_sign_in") and sign_in is not True:
            why = "sign-in is off here" if sign_in is False else "/api/health did not say"
            results.append(Result(name, "skipped", why))
            continue
        headers = {}
        if "key_from" in request:
            key = environ.get(request["key_from"], "").strip()
            if not key:
                results.append(Result(name, "skipped", f"set {request['key_from']}"))
                continue
            headers["authorization"] = f"Bearer {key}"
        url = request["url"].replace(found.group(0), root.rstrip("/")) if found else request["url"]
        try:
            answer = client_for(base).request(
                request["method"],
                url,
                json=request.get("json"),
                headers=headers,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            results.append(Result(name, "failed", f"no answer ({type(error).__name__})"))
            continue
        problem = _problem(answer, expect, root)
        results.append(
            Result(name, "failed" if problem else "ok", problem or str(answer.status_code))
        )
    return results


def _sign_in(client: httpx.Client, api: str) -> bool | None:
    """Whether sign-in is on, from the public health answer; None when it cannot be read."""
    try:
        answer = client.get(f"{api.rstrip('/')}/api/health")
        value = answer.json().get("sign_in") if answer.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def _problem(answer: httpx.Response, expect: Mapping[str, Any], root: str) -> str | None:
    """What is wrong with an answer, in a few words, or None."""
    if answer.status_code not in expect["status"]:
        return f"answered {answer.status_code}, not {' or '.join(map(str, expect['status']))}"
    if "location_leaves" in expect:
        there = urlsplit(answer.headers.get("location", ""))
        if not there.netloc or there.netloc == urlsplit(root).netloc:
            return "the redirect stays here: it does not reach the identity provider"
        if there.path == SIGN_IN_PATH:
            return "the redirect goes to another of Mendari's addresses: give --api that one"
    if "json_has" in expect:
        try:
            body = answer.json()
        except ValueError:
            return "the answer is not JSON"
        if not isinstance(body, dict) or expect["json_has"] not in body:
            return f"the answer has no {expect['json_has']}"
    return None


def export(
    spec: Mapping[str, Any], bases: Mapping[str, str | None]
) -> tuple[list[dict[str, Any]], list[Result]]:
    """Each check of `spec` whose base URL `bases` gives, for a status provider to run as the
    checker does: its absolute URL, method, JSON body and interval; redirects never followed
    (the checker judges the answer itself, and a redirect followed to a sign-in page would look
    up); the statuses that are up; the host the redirect's Location must leave and the path it
    must not have (Mendari's own sign-in, on another of its addresses); `json_has` as a keyword
    of the body (the key, quoted, and its colon); and headers that name the key's environment
    variable, never its value. Then the checks left out, with why. Raises ValueError on a key
    it does not know, rather than leave it out."""
    exported, left = [], []
    for check in spec["checks"]:
        name = check.get("name", "?")
        _known(check, CHECK_KEYS, f"check {name}")
        request, expect = check["request"], check["expect"]
        _known(request, REQUEST_KEYS, f"check {name}, request")
        _known(expect, EXPECT_FIELDS, f"check {name}, expect")
        url, base = _absolute(request["url"], bases)
        if url is None:
            left.append(Result(name, "skipped", f"no --{base} given"))
            continue
        entry: dict[str, Any] = {
            "name": name,
            "what": check["what"],
            "method": request["method"],
            "url": url,
            "interval_seconds": spec["interval_seconds"],
            "follow_redirects": False,
        }
        headers = {}
        if "json" in request:
            headers["Content-Type"] = "application/json"
            entry["json_body"] = request["json"]
        if "key_from" in request:
            headers["Authorization"] = f"Bearer ${{{request['key_from']}}}"
            entry["secret_from_environment"] = request["key_from"]
        if headers:
            entry["headers"] = headers
        entry["expected_statuses"] = list(expect["status"])
        if "location_leaves" in expect:
            there, base = _absolute(expect["location_leaves"], bases)
            if there is None:
                left.append(Result(name, "skipped", f"no --{base} given"))
                continue
            entry["location_leaves_host"] = urlsplit(there).netloc
            entry["location_path_not"] = SIGN_IN_PATH
        if "json_has" in expect:
            entry["body_keyword"] = f'"{expect["json_has"]}":'
        if check.get("only_with_sign_in"):
            entry["only_with_sign_in"] = True
        exported.append(entry)
    return exported, left


def _known(part: Mapping[str, Any], keys: Any, where: str) -> None:
    unknown = sorted(set(part) - set(keys))
    if unknown:
        raise ValueError(
            f"{where}: the export does not know {', '.join(unknown)}; export it "
            "(scripts/status_checks.py) before the checks file uses it"
        )


def _absolute(template: str, bases: Mapping[str, str | None]) -> tuple[str | None, str]:
    """`template` with its base URL in place, and the base's name; None when it is not given."""
    found = _BASE.match(template)
    if not found:
        raise ValueError(f"{template} does not start with {{api}}, {{registry}} or {{hub}}")
    root = bases.get(found.group(1))
    if not root:
        return None, found.group(1)
    return template.replace(found.group(0), root.rstrip("/"), 1), found.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the status checks against a deployment.")
    parser.add_argument("--api", help="the deployment's base URL")
    parser.add_argument("--registry", help="the pack registry's base URL")
    parser.add_argument("--hub", help="the Hub's base URL")
    parser.add_argument("--checks", type=Path, default=CHECKS, help="the checks file")
    parser.add_argument(
        "--export",
        choices=["json"],
        help="print the checks for a status provider instead of running them (nothing is sent)",
    )
    args = parser.parse_args(argv)
    bases = {"api": args.api, "registry": args.registry, "hub": args.hub}
    if args.export:
        for base, root in bases.items():
            parts = urlsplit(root or "")
            if root and (parts.scheme not in ("http", "https") or not parts.netloc):
                parser.error(
                    f"--{base} must be an absolute URL, such as https://{base}.example.org"
                )
        try:
            exported, left = export(load(args.checks), bases)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(json.dumps({"checks": exported}, indent=2))
        for result in left:
            print(f"{result.name}: {result.outcome} ({result.detail})", file=sys.stderr)
        return 0
    with httpx.Client(timeout=TIMEOUT_S) as client:
        results = run(load(args.checks), bases, lambda base: client)
    for result in results:
        print(f"{result.name}: {result.outcome} ({result.detail})")
    return 1 if any(result.outcome == "failed" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
