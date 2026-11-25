# SPDX-License-Identifier: Apache-2.0
"""Bulk FHIR (Bulk Data Access 2.0) `$export` into local-validation datasets.

`export` asks a FHIR server for a patient-level or group export, polls the status URL (honouring
`Retry-After`), and downloads the NDJSON files, checking each file's line count against the
manifest. With every file downloaded, it asks the server to delete the export (a `DELETE` to the
status URL, which Bulk Data STU2 says a client MAY send, so the server can drop the files at
once): one try, whose outcome is one of `DELETE_OUTCOMES` in the result, and never a failure of
the export, which is complete by then. Every URL, including the status URL and the file URLs the
server hands back, goes through the allow-listed `GuardedHttp`. The token goes only to the origin
of the FHIR base it was issued for: a status URL, or a file the manifest says `requiresAccessToken`,
on another origin (even an allow-listed one) is refused before anything is sent there.

Authentication is none (the sandbox), a bearer token, or SMART Backend Services: a
`client_credentials` grant with a `private_key_jwt` assertion signed RS384 or ES384, for which the
token endpoint comes from the server's SMART configuration.

`dataset` turns an export into an agent's evaluation dataset: one case per resource the agent
decides about (with that patient's other resources, through the agent's FHIR mapping), and labels
joined from a file the site provides, keyed by an identifier system. Ground truth lives in chart
review, not in the EHR, so the export supplies the inputs and the site supplies the answers.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from mendari.errors import FhirError, MendariError
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import GuardedHttp, origin_of

MAX_FILE_BYTES = 1024 * 1024 * 1024  # one NDJSON file
MAX_FILES = 1000  # the files one export's manifest may list
MAX_EXPORT_BYTES = 16 * MAX_FILE_BYTES  # one export's files together
MAX_TOKEN_CHARS = 16 * 1024
_TOKEN = re.compile(r"[\x21-\x7e]+")  # RFC 6749 VSCHAR without the space: safe in a header
MAX_WAIT_SECONDS = 3600
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
        loaded_key = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        loaded_key = None
    if isinstance(loaded_key, ec.EllipticCurvePrivateKey):
        algorithm = "ES384"
    elif isinstance(loaded_key, rsa.RSAPrivateKey):
        algorithm = "RS384"
    else:
        algorithm = "ES384" if "BEGIN EC PRIVATE KEY" in private_key else "RS384"
    issued = int(now())
    claims: dict[str, Any] = {"iss": client_id, "sub": client_id, "aud": endpoint}
    claims |= {"exp": issued + 300, "iat": issued, "jti": str(uuid.uuid4())}
    try:
        assertion = jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": key_id})
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
    access = token.get("access_token")
    if not access:
        raise BulkError("the token response has no access_token")
    if not isinstance(access, str) or len(access) > MAX_TOKEN_CHARS or not _TOKEN.fullmatch(access):
        raise BulkError("the token response's access_token is not a valid OAuth token")
    return access


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
    base = fhir.rstrip("/")
    if group is not None and not set(group) <= _ID_CHARS:
        raise BulkError("the group id is not a FHIR id")
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
    origin = origin_of(base)
    _check_token_origin(status_url, origin, auth, "status URL")
    manifest = _wait(status_url, http=http, auth=auth, sleep=sleep, limit=max_wait)
    outputs = _outputs(manifest)
    needs_token = bool(manifest.get("requiresAccessToken", False))
    if needs_token:
        # Every file's origin before any download: a manifest naming another server fails whole.
        for output in outputs:
            _check_token_origin(output["url"], origin, auth, "file")
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = defaultdict(int)
    left = MAX_EXPORT_BYTES
    for output in outputs:
        kind = str(output.get("type", "Resource"))
        if not (kind.isascii() and kind.isalnum()):
            raise BulkError(f"the manifest names an odd resource type {kind!r}")
        counts[kind] += 1
        path = out / f"{kind}.{counts[kind]:03d}.ndjson"
        most = min(MAX_FILE_BYTES, left)
        try:
            left -= http.download(
                output["url"], path, headers=auth if needs_token else {}, max_bytes=most
            )
        except FhirError as error:
            if error.reason == "too_large" and most < MAX_FILE_BYTES:
                raise BulkError(
                    f"the export's files are larger than {MAX_EXPORT_BYTES} bytes together"
                ) from None
            raise
        lines = _lines(path)
        if isinstance(output.get("count"), int) and output["count"] != lines:
            raise BulkError(f"{path.name}: the manifest says {output['count']} lines, got {lines}")
        output["file"], output["count"] = path.name, lines
    manifest["delete"] = _delete(status_url, http=http, auth=auth)
    return manifest


def _check_token_origin(url: str, origin: str | None, auth: Mapping[str, str], what: str) -> None:
    """A `BulkError` when the token (`auth`) would go to `url` on another origin than the FHIR
    base's (`origin`): a token is the server's own, never another allow-listed server's."""
    if auth and origin_of(url) != origin:
        raise BulkError(
            f"the export's {what} is on another server than the FHIR base; "
            "its access token is sent only to the server it was issued for"
        )


def _outputs(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The manifest's `output`: a list of at most `MAX_FILES` objects, each with a `url`, or a
    `BulkError` before any file is downloaded."""
    outputs = manifest.get("output", [])
    if not isinstance(outputs, list) or not all(
        isinstance(output, dict) and isinstance(output.get("url"), str) for output in outputs
    ):
        raise BulkError("the export's manifest has an output that is not a file with a url")
    if len(outputs) > MAX_FILES:
        raise BulkError(f"the export's manifest lists {len(outputs)} files; at most {MAX_FILES}")
    return outputs


def _lines(path: Path) -> int:
    """The NDJSON lines in a file that hold anything but whitespace, read a part at a time (a
    file of one long line is never read whole)."""
    lines, blank = 0, True
    with path.open("rb") as fh:
        while part := fh.read(1024 * 1024):
            for index, piece in enumerate(part.split(b"\n")):
                if index:  # a line ended before this piece
                    lines += not blank
                    blank = True
                blank = blank and not piece.strip()
    return lines + (not blank)


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
                manifest: dict[str, Any] = json.loads(body)
            except (ValueError, RecursionError):
                raise BulkError("the export's manifest is not JSON") from None
            if not isinstance(manifest, dict):
                raise BulkError("the export's manifest is not a JSON object")
            if manifest.get("error"):
                raise BulkError(f"the export reported {len(manifest['error'])} error file(s)")
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
    except (ValueError, AttributeError):
        return ""
    codes = sorted({str(i.get("code")) for i in issues if isinstance(i, dict) and i.get("code")})
    return f" ({', '.join(codes)})" if codes else ""


# -- export -> dataset --------------------------------------------------------------------------


def read_ndjson(directory: Path) -> Iterator[dict[str, Any]]:
    for path in sorted(directory.glob("*.ndjson")):
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if not line.strip():
                continue
            try:
                resource = json.loads(line)
            except json.JSONDecodeError:
                raise BulkError(f"{path.name}:{number}: not JSON") from None
            if isinstance(resource, dict) and resource.get("resourceType"):
                yield resource


def dataset(
    adapter: FhirAdapter,
    resources: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, str]],
    *,
    key_system: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Cases for the agent: each focus resource with its patient's record, labelled by the
    identifier (in `key_system`) of the focus or, failing that, of its patient. Returns the
    labelled cases and counts of what was left out."""
    patients = {r["id"]: r for r in resources if r["resourceType"] == "Patient" and r.get("id")}
    by_patient: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for resource in resources:
        owner = _patient_of(resource)
        if owner is not None:
            by_patient[owner].append(resource)
    records, skipped = [], {"unlabelled": 0, "no_key": 0}
    for focus in (r for r in resources if adapter.is_focus(r)):
        owner = focus.get("id") if focus["resourceType"] == "Patient" else _patient_of(focus)
        patient = patients.get(owner or "")
        key = _identifier(focus, key_system) or (patient and _identifier(patient, key_system))
        if not key:
            skipped["no_key"] += 1
            continue
        if key not in labels:
            skipped["unlabelled"] += 1
            continue
        record = [r for r in by_patient.get(owner or "", []) if r is not focus]
        case = [focus, *record, *([patient] if patient else [])]
        bundle = {"resourceType": "Bundle", "entry": [{"resource": r} for r in case]}
        indexed = adapter.index(bundle, focus=focus)
        records.append({"id": key, "input": adapter.to_input(indexed), "labels": dict(labels[key])})
    return records, skipped


def load_labels(path: Path) -> dict[str, dict[str, str]]:
    """Labels by case id from a JSONL file of `{"id": ..., "labels": {...}}` (an evaluation dataset
    works as it is; its inputs are ignored)."""
    labels = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            raise BulkError(f"{path.name}:{number}: not JSON") from None
        if isinstance(record, dict) and isinstance(record.get("labels"), dict) and record.get("id"):
            labels[str(record["id"])] = {str(k): str(v) for k, v in record["labels"].items()}
    return labels


def _patient_of(resource: Mapping[str, Any]) -> str | None:
    for field in ("subject", "patient", "beneficiary"):
        reference = str((resource.get(field) or {}).get("reference") or "")
        if "Patient/" in reference:
            return reference.rsplit("Patient/", 1)[-1]
    return None


def _identifier(resource: Mapping[str, Any], system: str) -> str | None:
    for identifier in resource.get("identifier") or []:
        if isinstance(identifier, dict) and identifier.get("system") == system:
            value = identifier.get("value")
            return str(value) if value else None
    return None
