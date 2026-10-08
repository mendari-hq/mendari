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
"""

from __future__ import annotations

import argparse
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
        location = answer.headers.get("location", "")
        here = urlsplit(root)
        there = urlsplit(location)
        if not there.netloc or (there.scheme, there.netloc) == (here.scheme, here.netloc):
            return "the redirect stays here: it does not reach the identity provider"
        if there.path == "/auth/login":  # Mendari's own move to its public address
            return "the redirect goes to another of Mendari's addresses: give --api that one"
    if "json_has" in expect:
        try:
            body = answer.json()
        except ValueError:
            return "the answer is not JSON"
        if not isinstance(body, dict) or expect["json_has"] not in body:
            return f"the answer has no {expect['json_has']}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the status checks against a deployment.")
    parser.add_argument("--api", help="the deployment's base URL")
    parser.add_argument("--registry", help="the pack registry's base URL")
    parser.add_argument("--hub", help="the Hub's base URL")
    parser.add_argument("--checks", type=Path, default=CHECKS, help="the checks file")
    args = parser.parse_args(argv)
    bases = {"api": args.api, "registry": args.registry, "hub": args.hub}
    with httpx.Client(timeout=TIMEOUT_S) as client:
        results = run(load(args.checks), bases, lambda base: client)
    for result in results:
        print(f"{result.name}: {result.outcome} ({result.detail})")
    return 1 if any(result.outcome == "failed" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
