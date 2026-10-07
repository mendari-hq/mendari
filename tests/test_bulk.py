# SPDX-License-Identifier: Apache-2.0
"""Bulk FHIR $export into local datasets (standards/bulk.py) against a fake server. The real
export runs against HAPI in the CI job `sandbox-e2e`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from mendari.cli import main
from mendari.core.registry import resolve_pack
from mendari.errors import FhirError, MendariError
from mendari.sandbox import example_datasets_dir
from mendari.sandbox.fhir_records import CASE_SYSTEM, referral_entries
from mendari.standards import bulk
from mendari.standards.bulk import BulkError
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import GuardedHttp

FHIR = "https://ehr.example.org/fhir"
LABELS = (example_datasets_dir() or Path("datasets")) / "referral-local-site-example.jsonl"
STATUS = "https://ehr.example.org/fhir/$export-poll-status?_jobId=1"


class FakeBulk:
    """Kick-off, a status URL that answers 202 a few times, then a manifest, and the files."""

    def __init__(self, files: dict[str, list[dict[str, Any]]], *, pending: int = 2) -> None:
        self.files = files
        self.pending = pending
        self.requests: list[httpx.Request] = []
        self.manifest_extra: dict[str, Any] = {}
        self.kickoff: httpx.Response | None = None
        self.counts: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "/$export?" in url or url.endswith("/$export"):
            assert request.headers["prefer"] == "respond-async"
            return self.kickoff or httpx.Response(202, headers={"content-location": STATUS})
        if url == STATUS:
            if self.pending:
                self.pending -= 1
                return httpx.Response(202, headers={"retry-after": "3", "x-progress": "working"})
            output = [
                {
                    "type": kind,
                    "url": f"https://ehr.example.org/files/{kind}.ndjson",
                    "count": self.counts.get(kind, len(rows)),
                }
                for kind, rows in self.files.items()
            ]
            return httpx.Response(
                200,
                json={
                    "transactionTime": "2026-09-30T00:00:00Z",
                    "requiresAccessToken": True,
                    "output": output,
                    "error": [],
                    **self.manifest_extra,
                },
            )
        if url.startswith("https://ehr.example.org/files/"):
            kind = url.rsplit("/", 1)[-1].removesuffix(".ndjson")
            body = "\n".join(json.dumps(r) for r in self.files[kind]) + "\n"
            return httpx.Response(
                200, text=body, headers={"content-type": "application/fhir+ndjson"}
            )
        return httpx.Response(404)


def http_for(fake: Any, allowed: set[str] | None = None) -> GuardedHttp:
    return GuardedHttp(
        allowed=allowed or {"https://ehr.example.org"}, transport=httpx.MockTransport(fake)
    )


def run(fake: FakeBulk, out: Path, **kwargs: Any) -> tuple[dict[str, Any], list[float]]:
    slept: list[float] = []
    with http_for(fake) as http:
        manifest = bulk.export(FHIR, out, http=http, sleep=slept.append, **kwargs)
    return manifest, slept


def test_an_export_polls_then_downloads_every_file(tmp_path: Path) -> None:
    fake = FakeBulk(
        {
            "Patient": [{"resourceType": "Patient", "id": "p1"}],
            "ServiceRequest": [{"resourceType": "ServiceRequest", "id": "s1"}] * 2,
        }
    )
    manifest, slept = run(
        fake, tmp_path, types=["Patient", "ServiceRequest"], since="2026-01-01", token="t0"
    )
    assert slept == [3.0, 3.0]  # Retry-After honoured
    assert [(o["type"], o["file"], o["count"]) for o in manifest["output"]] == [
        ("Patient", "Patient.001.ndjson", 1),
        ("ServiceRequest", "ServiceRequest.001.ndjson", 2),
    ]
    kickoff = fake.requests[0]
    assert kickoff.url.path == "/fhir/Patient/$export"
    assert kickoff.url.params["_type"] == "Patient,ServiceRequest"
    assert kickoff.url.params["_since"] == "2026-01-01"
    downloads = [r for r in fake.requests if "/files/" in str(r.url)]
    assert {r.headers["authorization"] for r in downloads} == {"Bearer t0"}  # requiresAccessToken
    assert len((tmp_path / "ServiceRequest.001.ndjson").read_text().splitlines()) == 2


def test_a_group_export_uses_the_group_endpoint(tmp_path: Path) -> None:
    fake = FakeBulk({}, pending=0)
    run(fake, tmp_path, group="site-cohort")
    assert fake.requests[0].url.path == "/fhir/Group/site-cohort/$export"
    with pytest.raises(BulkError, match="not a FHIR id"):
        run(FakeBulk({}), tmp_path, group="../Patient")


def test_a_file_that_does_not_match_the_manifest_fails(tmp_path: Path) -> None:
    fake = FakeBulk({"Patient": [{"resourceType": "Patient", "id": "p1"}]}, pending=0)
    fake.counts = {"Patient": 5}
    with pytest.raises(BulkError, match="says 5 lines, got 1"):
        run(fake, tmp_path)


def test_an_export_that_reports_errors_fails(tmp_path: Path) -> None:
    fake = FakeBulk({}, pending=0)
    fake.manifest_extra = {
        "error": [{"type": "OperationOutcome", "url": "https://ehr.example.org/files/e"}]
    }
    with pytest.raises(BulkError, match="1 error file"):
        run(fake, tmp_path)


def test_a_refused_kickoff_names_the_issue_codes_only(tmp_path: Path) -> None:
    fake = FakeBulk({})
    fake.kickoff = httpx.Response(
        403,
        json={
            "resourceType": "OperationOutcome",
            "issue": [
                {"severity": "error", "code": "forbidden", "diagnostics": "patient 123 secret"}
            ],
        },
    )
    with pytest.raises(BulkError, match=r"HTTP 403\) \(forbidden\)") as error:
        run(fake, tmp_path)
    assert "secret" not in str(error.value)


def test_an_export_that_never_finishes_gives_up(tmp_path: Path) -> None:
    with pytest.raises(BulkError, match="did not finish within 5 s"):
        run(FakeBulk({}, pending=100), tmp_path, max_wait=5)


def test_files_on_another_server_are_not_downloaded(tmp_path: Path) -> None:
    class Elsewhere(FakeBulk):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            response = super().__call__(request)
            if str(request.url) == STATUS and response.status_code == 200:
                manifest = json.loads(response.content)
                manifest["output"] = [
                    {"type": "Patient", "url": "https://collector.example/x.ndjson"}
                ]
                return httpx.Response(200, json=manifest)
            return response

    with pytest.raises(FhirError, match="allow-list"):
        run(Elsewhere({}, pending=0), tmp_path)


@pytest.mark.parametrize(
    ("value", "seconds"), [("7", 7.0), ("0", 1.0), ("99999", 120.0), (None, None), ("soon", None)]
)
def test_retry_after_is_kept_sensible(value: str | None, seconds: float | None) -> None:
    assert bulk._retry_after(value) == seconds


# -- SMART Backend Services -------------------------------------------------------------------


@pytest.mark.parametrize(("curve", "algorithm"), [(False, "RS384"), (True, "ES384")])
def test_backend_services_signs_an_assertion_for_the_token_endpoint(
    curve: bool, algorithm: str
) -> None:
    key: Any = (
        ec.generate_private_key(ec.SECP384R1())
        if curve
        else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("smart-configuration"):
            return httpx.Response(
                200, json={"token_endpoint": "https://ehr.example.org/auth/token"}
            )
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        seen.update(form)
        return httpx.Response(200, json={"access_token": "backend-token", "token_type": "bearer"})

    with http_for(handler) as http:
        token = bulk.backend_token(
            FHIR,
            client_id="site-app",
            private_key=pem,
            key_id="k1",
            scope="system/*.rs",
            http=http,
            now=lambda: 1_790_000_000,
        )
    assert token == "backend-token"
    assert seen["grant_type"] == "client_credentials" and seen["scope"] == "system/*.rs"
    header = jwt.get_unverified_header(seen["client_assertion"])
    assert (header["alg"], header["kid"]) == (algorithm, "k1")
    claims = jwt.decode(
        seen["client_assertion"],
        key.public_key(),
        algorithms=[algorithm],
        audience="https://ehr.example.org/auth/token",
        options={"verify_exp": False},
    )
    assert claims["iss"] == claims["sub"] == "site-app"
    assert claims["exp"] - claims["iat"] == 300 and claims["jti"]


# -- export -> dataset ------------------------------------------------------------------------


def referral_export(tmp_path: Path, cases: list[dict[str, Any]]) -> Path:
    """What a server exports for these referral cases: NDJSON per type, ids and references
    resolved as a server stores them."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for n, record in enumerate(cases):
        entries = referral_entries(record)
        ids = {
            e["fullUrl"]: f"{e['resource']['resourceType']}/c{n}-{i}" for i, e in enumerate(entries)
        }
        for i, entry in enumerate(entries):
            resource = json.loads(json.dumps(entry["resource"]))
            resource["id"] = f"c{n}-{i}"
            if "subject" in resource:
                resource["subject"] = {"reference": ids[resource["subject"]["reference"]]}
            by_type.setdefault(resource["resourceType"], []).append(resource)
    for kind, rows in by_type.items():
        (tmp_path / f"{kind}.001.ndjson").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return tmp_path


def test_an_export_becomes_the_packs_dataset_with_the_sites_labels(tmp_path: Path) -> None:
    lines = LABELS.read_text().splitlines()
    cases = [json.loads(line) for line in lines[:6]]
    export_dir = referral_export(tmp_path, cases)
    labels = {c["id"]: c["labels"] for c in cases[:4]}  # the site labelled four of six
    adapter = FhirAdapter.for_pack(resolve_pack("mendari/referral-router"))
    records, skipped = bulk.dataset(
        adapter, list(bulk.read_ndjson(export_dir)), labels, key_system=CASE_SYSTEM
    )
    assert [r["id"] for r in records] == [c["id"] for c in cases[:4]]
    assert skipped == {"unlabelled": 2, "no_key": 0}
    first = records[0]["input"]
    assert first["referral"] == cases[0]["input"]["referral"]
    assert len(first["documents"]) == len(
        cases[0]["input"]["documents"]
    )  # the patient's record came along
    assert records[0]["labels"] == cases[0]["labels"]


def test_the_cli_builds_a_dataset_that_mendari_eval_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = LABELS
    cases = [json.loads(line) for line in labels.read_text().splitlines()[:5]]
    (tmp_path / "export").mkdir()
    export_dir = referral_export(tmp_path / "export", cases)
    out = tmp_path / "bulk.jsonl"
    assert (
        main(
            [
                "bulk",
                "dataset",
                "--pack",
                "mendari/referral-router",
                "--ndjson",
                str(export_dir),
                "--labels",
                str(labels),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert "5 cases written" in capsys.readouterr().out
    assert (
        main(["eval", "mendari/referral-router", "--dataset", str(out), "--baseline", "none"]) == 0
    )


def test_the_sandbox_bulk_check_compares_the_export_with_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit check's comparison (its export runs against HAPI in `sandbox-e2e`)."""
    from mendari.sandbox import bulk_check

    cases = [json.loads(line) for line in LABELS.read_text().splitlines()[:8]]

    def fake_export(fhir: str, out: Path, **kwargs: Any) -> dict[str, Any]:
        out.mkdir(parents=True)
        referral_export(out, cases)
        return {
            "output": [{"type": p.name.split(".")[0], "count": 1} for p in out.glob("*.ndjson")]
        }

    monkeypatch.setattr(bulk_check.bulk, "export", fake_export)
    result = bulk_check.run("http://fhir:8080/fhir", "mendari/referral-router")
    assert result["cases"] == 8 and result["identical"] and result["differences"] == []
    with pytest.raises(MendariError, match="runs for"):
        bulk_check.run("http://fhir:8080/fhir", "mendari/ai-guard")
