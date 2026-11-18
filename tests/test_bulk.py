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
    """Kick-off, a status URL that answers 202 a few times, then a manifest, and the files; a
    DELETE to the status URL gets `deleted` (202 Accepted, as Bulk Data's own server answers), or
    raises it when it is an exception."""

    def __init__(self, files: dict[str, list[dict[str, Any]]], *, pending: int = 2) -> None:
        self.files = files
        self.pending = pending
        self.requests: list[httpx.Request] = []
        self.manifest_extra: dict[str, Any] = {}
        self.kickoff: httpx.Response | None = None
        self.counts: dict[str, int] = {}
        self.deleted: httpx.Response | Exception = httpx.Response(202)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.method == "DELETE":
            assert url == STATUS
            if isinstance(self.deleted, Exception):
                raise self.deleted
            return self.deleted
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


def test_a_finished_export_is_deleted_on_the_server_once_every_file_is_here(
    tmp_path: Path,
) -> None:
    """Bulk Data STU2's "Bulk Data Delete Request" (a client MAY send it): one DELETE to the
    status URL, with the token, after the last download, so the server can drop the files."""
    fake = FakeBulk({"Patient": [{"resourceType": "Patient", "id": "p1"}]}, pending=0)
    manifest, _ = run(fake, tmp_path, token="t0")
    assert manifest["delete"] == "deleted"
    last = fake.requests[-1]
    assert (last.method, str(last.url)) == ("DELETE", STATUS)
    assert last.headers["authorization"] == "Bearer t0"
    assert [r.method for r in fake.requests].count("DELETE") == 1
    assert "/files/" in str(fake.requests[-2].url)  # after the download


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (httpx.Response(404, json={"issue": [{"diagnostics": "patient 123 secret"}]}), "refused"),
        (httpx.Response(500, text="patient 123 secret"), "refused"),
        (httpx.Response(302, headers={"location": "https://ehr.example.org/x"}), "refused"),
        (httpx.ReadTimeout("patient 123 secret"), "timeout"),
        (httpx.ConnectError("patient 123 secret"), "unreachable"),
    ],
    ids=["404", "500", "redirect", "timeout", "refused-connection"],
)
def test_a_delete_that_fails_never_fails_retries_or_changes_the_export(
    tmp_path: Path, answer: httpx.Response | Exception, outcome: str
) -> None:
    """The export is complete when the DELETE goes: whatever becomes of it is one stable code,
    never the server's answer, and the files and manifest are as they were."""
    rows = [{"resourceType": "Patient", "id": "p1"}]
    fake = FakeBulk({"Patient": rows}, pending=0)
    fake.deleted = answer
    manifest, slept = run(fake, tmp_path)
    assert manifest["delete"] == f"delete_{outcome}" and manifest["delete"] in bulk.DELETE_OUTCOMES
    assert "secret" not in json.dumps(manifest)
    assert [(o["type"], o["file"], o["count"]) for o in manifest["output"]] == [
        ("Patient", "Patient.001.ndjson", 1)
    ]
    assert json.loads((tmp_path / "Patient.001.ndjson").read_text()) == rows[0]
    assert [r.method for r in fake.requests].count("DELETE") == 1 and slept == []


def test_the_cli_says_whether_the_server_deleted_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for outcome, said in (
        ("deleted", "asked to delete the export, and accepted"),
        ("delete_refused", "did not delete the export (delete_refused)"),
    ):
        monkeypatch.setattr(
            bulk, "export", lambda *a, outcome=outcome, **k: {"output": [], "delete": outcome}
        )
        args = ["bulk", "export", "--fhir", FHIR, "--out", str(tmp_path)]
        assert main(args) == 0  # the export is done either way
        printed = capsys.readouterr()
        assert said in printed.out + printed.err


def test_a_group_export_uses_the_group_endpoint(tmp_path: Path) -> None:
    fake = FakeBulk({}, pending=0)
    run(fake, tmp_path, group="site-cohort")
    assert fake.requests[0].url.path == "/fhir/Group/site-cohort/$export"
    with pytest.raises(BulkError, match="not a FHIR id"):
        run(FakeBulk({}), tmp_path, group="../Patient")


def test_a_file_that_does_not_match_the_manifest_fails(tmp_path: Path) -> None:
    fake = FakeBulk(
        {
            "Observation": [{"resourceType": "Observation"}],
            "Patient": [{"resourceType": "Patient"}],
        },
        pending=0,
    )
    fake.counts = {"Patient": 5}
    with pytest.raises(BulkError, match="says 5 lines, got 1"):
        run(fake, tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert not any(request.method == "DELETE" for request in fake.requests)
    fake.counts.clear()
    run(fake, tmp_path)
    assert len(list(tmp_path.glob("*.ndjson"))) == 2


def test_an_export_cannot_replace_the_calling_shells_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeBulk({})
    with pytest.raises(BulkError, match="working directory"):
        run(fake, Path("."))
    assert not fake.requests and Path.cwd().samefile(tmp_path)


def test_reusing_a_completed_export_preserves_the_first_cohort(tmp_path: Path) -> None:
    run(FakeBulk({"Patient": [{"resourceType": "Patient", "id": "synthetic"}]}), tmp_path)
    fresh = FakeBulk({"Observation": [{"resourceType": "Observation"}]})
    with pytest.raises(BulkError, match="new or empty"):
        run(fresh, tmp_path)
    assert fresh.requests == []
    assert [r["id"] for r in bulk.read_ndjson(tmp_path)] == ["synthetic"]


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        [],
        {},
        {"output": {}},
        {"output": [None]},
        {"output": [{"type": "Patient"}]},
        {"output": [{"type": [], "url": "unused"}]},
        {"output": [{"type": "Patient", "url": "unused", "count": False}]},
        {"output": [], "requiresAccessToken": "false"},
        {"output": [], "error": 1},
    ],
)
def test_an_invalid_manifest_never_completes_an_export(tmp_path: Path, manifest: Any) -> None:
    requests = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == STATUS:
            return httpx.Response(200, content=json.dumps(manifest))
        return httpx.Response(202, headers={"content-location": STATUS})

    with http_for(reply) as http, pytest.raises(BulkError):
        bulk.export(FHIR, tmp_path / "cohort", http=http)
    assert len(requests) == 2
    assert list(tmp_path.iterdir()) == []


def test_export_checks_the_resource_on_every_line_before_deleting(tmp_path: Path) -> None:
    fake = FakeBulk({"Patient": [{"resourceType": "Patient"}, {"resourceType": "Observation"}]})
    with pytest.raises(BulkError, match=r"Patient\.001\.ndjson:2: expected a Patient"):
        run(fake, tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert not any(request.method == "DELETE" for request in fake.requests)


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
                {"severity": "error", "code": "forbidden", "diagnostics": "patient 123 secret"},
                {"code": "patient 123 secret"},
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


@pytest.mark.parametrize(
    "encoding", [serialization.PrivateFormat.TraditionalOpenSSL, serialization.PrivateFormat.PKCS8]
)
@pytest.mark.parametrize(("curve", "algorithm"), [(False, "RS384"), (True, "ES384")])
def test_backend_services_signs_an_assertion_for_the_token_endpoint(
    curve: bool, algorithm: str, encoding: serialization.PrivateFormat
) -> None:
    key: Any = (
        ec.generate_private_key(ec.SECP384R1())
        if curve
        else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        encoding,
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


def test_a_saved_export_resolves_relative_and_same_source_absolute_patient_links(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    cases = [json.loads(line) for line in LABELS.read_text().splitlines()[:2]]
    resources = list(bulk.read_ndjson(referral_export(plain, cases)))

    def make_dataset(folder: Path, out: Path, *options: str) -> int:
        return main(
            [
                "bulk",
                "dataset",
                "--pack",
                "mendari/referral-router",
                "--ndjson",
                str(folder),
                "--labels",
                str(LABELS),
                "--out",
                str(out),
                *options,
            ]
        )

    expected = tmp_path / "relative.jsonl"
    assert make_dataset(plain, expected) == 0
    files: dict[str, list[dict[str, Any]]] = {}
    for row in resources:
        if "subject" in row:
            relative = row["subject"]["reference"]
            # Absolute on the same host/base, with default port and version, or relative/versioned.
            prefix = (
                "https://EHR.Example.org:443/fhir/"
                if row["resourceType"] == "ServiceRequest"
                else ""
            )
            row["subject"]["reference"] = f"{prefix}{relative}/_history/v1"
        files.setdefault(row["resourceType"], []).append(row)
    directory = tmp_path / "export"
    run(FakeBulk(files), directory)
    result = tmp_path / "absolute.jsonl"
    assert make_dataset(directory, result) == 0
    assert expected.read_bytes() == result.read_bytes()
    assert make_dataset(directory, result, "--fhir-base", "https://other.example/fhir") == 1
    assert "differs from the source recorded" in capsys.readouterr().err
    # Raw NDJSON imported from a legacy or third-party exporter needs its declared source.
    external = tmp_path / "external"
    external.mkdir()
    for path in directory.glob("*.ndjson"):
        (external / path.name).write_bytes(path.read_bytes())
    assert make_dataset(external, result) == 1
    assert "absolute Patient references need" in capsys.readouterr().err
    assert make_dataset(external, result, "--fhir-base", FHIR) == 0
    assert expected.read_bytes() == result.read_bytes()


@pytest.mark.parametrize("foreign", ["https://other.example/fhir", "https://ehr.example.org/other"])
def test_another_fhir_base_cannot_contribute_to_a_patient_by_matching_local_id(
    foreign: str,
) -> None:
    adapter = FhirAdapter.for_pack(resolve_pack("mendari/referral-router"))
    rows: list[dict[str, Any]] = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Observation", "subject": {"reference": f"{foreign}/Patient/p1"}},
    ]
    with pytest.raises(BulkError, match="different FHIR base"):
        bulk.dataset(adapter, rows, {}, key_system=CASE_SYSTEM, fhir_base=FHIR)


def test_conflicting_patient_rows_are_never_silently_resolved_by_file_order() -> None:
    adapter = FhirAdapter.for_pack(resolve_pack("mendari/referral-router"))
    rows = [{"resourceType": "Patient", "id": "p1", "active": value} for value in (True, False)]
    with pytest.raises(BulkError, match="conflicting records for one Patient id"):
        bulk.dataset(adapter, rows, {}, key_system=CASE_SYSTEM)


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
