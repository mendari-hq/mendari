# SPDX-License-Identifier: Apache-2.0
"""Bulk FHIR (Bulk Data Access 2.0) `$export` into local-validation datasets.

`export` asks a FHIR server for a patient-level or group export, polls the status URL (honouring
`Retry-After`), and downloads the NDJSON files, checking each file's line count against the
manifest. With every file downloaded, it asks the server to delete the export (a `DELETE` to the
status URL, which Bulk Data STU2 says a client MAY send, so the server can drop the files at
once): one try, whose outcome is one of `DELETE_OUTCOMES` in the result, and never a failure of
the export, which is complete by then. Every URL, including the status URL and the file URLs the
server hands back, goes through the allow-listed `GuardedHttp`.

Authentication is none (the sandbox), a bearer token, or SMART Backend Services: a
`client_credentials` grant with a `private_key_jwt` assertion signed RS384 or ES384, for which the
token endpoint comes from the server's SMART configuration.

`dataset` turns an export into a pack's evaluation dataset: one case per resource the pack
decides about (with that patient's other resources, through the pack's FHIR mapping), and labels
joined from a file the site provides, keyed by an identifier system. Ground truth lives in chart
review, not in the EHR, so the export supplies the inputs and the site supplies the answers.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from mendari.errors import FhirError, MendariError
from mendari.standards import fhir_structure
from mendari.standards.fhir import FhirAdapter, is_absolute, link_target
from mendari.standards.fhir_client import GuardedHttp

MAX_FILE_BYTES = 1024 * 1024 * 1024  # one NDJSON file
MAX_WAIT_SECONDS = 3600
_SOURCE = "mendari-export-source.json"
_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
# What became of the request to delete a finished export (`export`'s `delete`): the server
# accepted it (any 2xx), answered anything else (a 4xx, a 5xx, a redirect), did not answer in
# time, or could not be reached. Nothing of the server's answer is kept.
DELETE_OUTCOMES = ("deleted", "delete_refused", "delete_timeout", "delete_unreachable")
_ID_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-.")


class BulkError(MendariError):
    """The export could not be started, did not finish, or its files don't match the manifest."""


def backend_token(
    fhir: str,
    *,
    client_id: str,
    private_key: str,
    key_id: str,
    scope: str,
    http: GuardedHttp,
    now: Callable[[], float] = time.time,
) -> str:
    """An access token by SMART Backend Services (client credentials, private_key_jwt)."""
    config = http.get_json(f"{fhir.rstrip('/')}/.well-known/smart-configuration")
    endpoint = config.get("token_endpoint")
    if not isinstance(endpoint, str):
        raise BulkError("the server's SMART configuration has no token_endpoint")
    try:
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise BulkError(
            "backend services need an unencrypted RSA or P-384 EC private key"
        ) from None
    if isinstance(key, rsa.RSAPrivateKey):
        algorithm = "RS384"
    elif isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP384R1):
        algorithm = "ES384"
    else:
        raise BulkError("backend services need an RSA or P-384 EC private key")
    issued = int(now())
    claims: dict[str, Any] = {"iss": client_id, "sub": client_id, "aud": endpoint}
    claims |= {"exp": issued + 300, "iat": issued, "jti": str(uuid.uuid4())}
    try:
        assertion = jwt.encode(claims, key, algorithm=algorithm, headers={"kid": key_id})
    except (ValueError, TypeError, jwt.PyJWTError):
        raise BulkError(f"the private key cannot sign {algorithm}") from None
    form = {
        "grant_type": "client_credentials",
        "scope": scope,
        "client_assertion_type": _ASSERTION_TYPE,
        "client_assertion": assertion,
    }
    try:
        token = http.post_form(endpoint, form, {"accept": "application/json"})
    except FhirError as error:
        raise BulkError(f"the backend-services token request failed: {error}") from None
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise BulkError("the token response has no access_token")
    return access_token


def export(
    fhir: str,
    out: Path,
    *,
    http: GuardedHttp,
    group: str | None = None,
    types: Sequence[str] = (),
    since: str | None = None,
    token: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_wait: float = MAX_WAIT_SECONDS,
) -> dict[str, Any]:
    """Run an export to completion; the files land in `out`. Returns the server's manifest, with
    each output's local `file` and line `count`, and what became of the request to delete the
    export on the server (`delete`, one of `DELETE_OUTCOMES`)."""
    base = _canonical_base(fhir)
    if group is not None and (
        not 1 <= len(group) <= 64 or group in (".", "..") or not set(group) <= _ID_CHARS
    ):
        raise BulkError("the group id is not a FHIR id")
    out = out.absolute()
    if out == Path.cwd() or (out.is_dir() and out.samefile(Path.cwd())):
        raise BulkError("choose an export directory other than the current working directory")
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise BulkError("choose a new or empty directory for the export")
    url = f"{base}/Group/{group}/$export" if group else f"{base}/Patient/$export"
    params = {"_outputFormat": "application/fhir+ndjson"}
    if types:
        params["_type"] = ",".join(types)
    if since:
        params["_since"] = since
    auth = {"authorization": f"Bearer {token}"} if token else {}
    headers = {"accept": "application/fhir+json", "prefer": "respond-async", **auth}
    status, answer, body = http.raw("GET", url, headers=headers, params=params)
    if status != 202 or not answer.get("content-location"):
        raise BulkError(f"the server did not start an export (HTTP {status}){_diagnostics(body)}")
    status_url = answer["content-location"]
    manifest = _wait(status_url, http=http, auth=auth, sleep=sleep, limit=max_wait)
    # Publish a single complete cohort, including when two jobs target the same directory.
    # POSIX rename can replace an empty directory but never one containing a completed export.
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".mendari-export-") as scratch:
        stage = Path(scratch) / "cohort"
        stage.mkdir(mode=0o700)
        counts: dict[str, int] = defaultdict(int)
        for output in manifest["output"]:
            kind = output["type"]
            counts[kind] += 1
            path = stage / f"{kind}.{counts[kind]:03d}.ndjson"
            http.download(
                output["url"],
                path,
                headers=auth if manifest.get("requiresAccessToken") else {},
                max_bytes=MAX_FILE_BYTES,
            )
            lines = 0
            for number, resource in _json_lines(path):
                if not isinstance(resource, dict) or resource.get("resourceType") != kind:
                    raise BulkError(f"{path.name}:{number}: expected a {kind} resource")
                lines += 1
            if "count" in output and output["count"] != lines:
                raise BulkError(
                    f"{path.name}: the manifest says {output['count']} lines, got {lines}"
                )
            output["file"], output["count"] = path.name, lines
        # NDJSON resources have logical ids but no Bundle fullUrl. Keep their issuing base
        # so an absolute Patient reference can only join a patient from this source.
        (stage / _SOURCE).write_text(json.dumps({"fhir_base": base}) + "\n", encoding="utf-8")
        if os.name == "nt" and out.is_dir():
            # Windows rename only accepts an absent destination. rmdir refuses a concurrent
            # writer's completed cohort, just as POSIX rename does.
            out.rmdir()
        stage.replace(out)
    manifest["delete"] = _delete(status_url, http=http, auth=auth)
    return manifest


def _delete(status_url: str, *, http: GuardedHttp, auth: Mapping[str, str]) -> str:
    """Ask the server to delete a finished export (Bulk Data STU2, "Bulk Data Delete Request"),
    once: one of `DELETE_OUTCOMES`. A refusal or a failure is the server's to clean up when the
    export expires, so it never fails, retries or changes the export."""
    try:
        status, _, _ = http.raw(
            "DELETE", status_url, headers={"accept": "application/json", **auth}
        )
    except FhirError as error:
        if error.reason == "timeout":
            return "delete_timeout"
        return "delete_unreachable" if error.reason == "network" else "delete_refused"
    return "deleted" if 200 <= status < 300 else "delete_refused"


def _wait(
    status_url: str,
    *,
    http: GuardedHttp,
    auth: Mapping[str, str],
    sleep: Callable[[float], None],
    limit: float,
) -> dict[str, Any]:
    waited, pause = 0.0, 1.0
    while True:
        headers = {"accept": "application/json", **auth}
        status, answer, body = http.raw("GET", status_url, headers=headers)
        if status == 200:
            try:
                manifest = json.loads(body)
            except (ValueError, RecursionError):
                raise BulkError("the export's manifest is not JSON") from None
            if not isinstance(manifest, dict) or not isinstance(manifest.get("output"), list):
                raise BulkError("the export's manifest needs an output list")
            errors = manifest.get("error", [])
            if not isinstance(errors, list):
                raise BulkError("the manifest's error field must be a list")
            if errors:
                raise BulkError(f"the export reported {len(errors)} error file(s)")
            if not isinstance(manifest.get("requiresAccessToken", False), bool):
                raise BulkError("the manifest's requiresAccessToken field must be a boolean")
            for item in manifest["output"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("url"), str)
                    or not item["url"]
                ):
                    raise BulkError("each manifest output needs a download URL")
                kind = item.get("type")
                if not isinstance(kind, str) or kind not in fhir_structure.rules().resource_types:
                    raise BulkError("each manifest output needs an R4 resource type")
                if "count" in item and (type(item["count"]) is not int or item["count"] < 0):
                    raise BulkError("the manifest's output count must be a nonnegative integer")
            return manifest
        if status != 202:
            raise BulkError(f"the export failed (HTTP {status}){_diagnostics(body)}")
        pause = _retry_after(answer.get("retry-after")) or min(pause * 2, 60.0)
        if waited + pause > limit:
            raise BulkError(f"the export did not finish within {int(limit)} s")
        sleep(pause)
        waited += pause


def _retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header (seconds or an HTTP date), kept within 1-120 s."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            return None
    return max(1.0, min(seconds, 120.0))


def _diagnostics(body: bytes) -> str:
    """The issue codes of an OperationOutcome body (codes only, never free text)."""
    try:
        issues = json.loads(body).get("issue", [])
    except (ValueError, AttributeError, RecursionError):
        return ""
    if not isinstance(issues, list):
        return ""
    valid = fhir_structure.rules().definitions["OperationOutcome_Issue"]["properties"]["code"][
        "enum"
    ]
    codes = sorted({i["code"] for i in issues if isinstance(i, dict) and i.get("code") in valid})
    return f" ({', '.join(codes)})" if codes else ""


# -- export -> dataset --------------------------------------------------------------------------


def _canonical_base(value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise BulkError("the FHIR base must be an absolute HTTP(S) URL") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise BulkError("the FHIR base needs an HTTP(S) host and no credentials, query or fragment")
    return str(url).rstrip("/")


def export_base(directory: Path, supplied: str | None = None) -> str | None:
    """The issuing server for a cohort, checked against any explicit --fhir-base."""
    supplied = _canonical_base(supplied) if supplied is not None else None
    marker = directory / _SOURCE
    if not marker.exists():
        return supplied
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, RecursionError):
        raise BulkError("the export source record is not valid JSON") from None
    if not isinstance(record, dict) or not isinstance(record.get("fhir_base"), str):
        raise BulkError("the export source record needs its FHIR base")
    recorded = _canonical_base(record["fhir_base"])
    if supplied is not None and supplied != recorded:
        raise BulkError("--fhir-base differs from the source recorded for this export")
    return recorded


def _json_lines(path: Path) -> Iterator[tuple[int, Any]]:
    """Read one UTF-8 JSON record at a time. Errors identify its line, without the contents."""
    with path.open("rb") as source:
        for number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError):
                raise BulkError(f"{path.name}:{number}: not JSON") from None
            yield number, value


def read_ndjson(directory: Path) -> Iterator[dict[str, Any]]:
    for path in sorted(directory.glob("*.ndjson")):
        for number, resource in _json_lines(path):
            if not isinstance(resource, dict) or not isinstance(resource.get("resourceType"), str):
                raise BulkError(f"{path.name}:{number}: expected a FHIR resource")
            yield resource


def dataset(
    adapter: FhirAdapter,
    resources: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, str]],
    *,
    key_system: str,
    fhir_base: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Cases for the pack: each focus resource with its patient's record, labelled by the
    identifier (in `key_system`) of the focus or, failing that, of its patient. Returns the
    labelled cases and counts of what was left out."""
    base = _canonical_base(fhir_base) if fhir_base is not None else None
    patients: dict[str, Mapping[str, Any]] = {}
    by_patient: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    focuses = []
    for resource in resources:
        owner = _patient_of(resource, base)
        if owner is not None:
            by_patient[owner].append(resource)
        if resource["resourceType"] == "Patient":
            owner = resource.get("id")
            if owner is not None and not isinstance(owner, str):
                raise BulkError("an exported Patient has a non-text id")
            if owner:
                if owner in patients:
                    if patients[owner] != resource:
                        raise BulkError("the export has conflicting records for one Patient id")
                    continue
                patients[owner] = resource
        if adapter.is_focus(resource):
            focuses.append((resource, owner))
    records, skipped = [], {"unlabelled": 0, "no_key": 0}
    for focus, owner in focuses:
        patient = patients.get(owner or "")
        key = _identifier(focus, key_system) or (patient and _identifier(patient, key_system))
        if not key:
            skipped["no_key"] += 1
            continue
        if key not in labels:
            skipped["unlabelled"] += 1
            continue
        record = [r for r in by_patient.get(owner or "", []) if r is not focus]
        case = [
            focus,
            *record,
            *([patient] if patient is not None and patient is not focus else []),
        ]
        bundle = {"resourceType": "Bundle", "entry": [{"resource": r} for r in case]}
        indexed = adapter.index(bundle, focus=focus)
        records.append({"id": key, "input": adapter.to_input(indexed), "labels": dict(labels[key])})
    return records, skipped


def load_labels(path: Path) -> dict[str, dict[str, str]]:
    """Labels by case id from a JSONL file of `{"id": ..., "labels": {...}}` (an evaluation dataset
    works as it is; its inputs are ignored)."""
    labels = {}
    for _, record in _json_lines(path):
        if isinstance(record, dict) and isinstance(record.get("labels"), dict) and record.get("id"):
            labels[str(record["id"])] = {str(k): str(v) for k, v in record["labels"].items()}
    return labels


def _patient_of(resource: Mapping[str, Any], base: str | None) -> str | None:
    for field in ("subject", "patient", "beneficiary"):
        ref = resource.get(field)
        text = ref.get("reference") if isinstance(ref, Mapping) else None
        if not isinstance(text, str):
            continue
        target = link_target(text)
        if target is None or target[0] != "Patient":
            continue
        if is_absolute(text):
            if base is None:
                raise BulkError("absolute Patient references need the export source or --fhir-base")
            if _canonical_base(text.rsplit("/Patient/", 1)[0]) != base:
                raise BulkError("an absolute Patient reference belongs to a different FHIR base")
        return target[1]
    return None


def _identifier(resource: Mapping[str, Any], system: str) -> str | None:
    for identifier in resource.get("identifier") or []:
        if isinstance(identifier, dict) and identifier.get("system") == system:
            value = identifier.get("value")
            return str(value) if value else None
    return None
