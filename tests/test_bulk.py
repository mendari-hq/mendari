# SPDX-License-Identifier: Apache-2.0
"""Bulk FHIR $export into local datasets (standards/bulk.py) against a fake server. The real
export runs against HAPI in the CI job `sandbox-e2e`."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from mendari.cli import main
from mendari.core import clock
from mendari.core.agent import load_agent
from mendari.core.registry import resolve_agent
from mendari.errors import FhirError, MendariError
from mendari.sandbox import DEMO_AGENT, fhir_workflow
from mendari.sandbox.code_systems import CASE_SYSTEM
from mendari.standards import bulk
from mendari.standards.bulk import BulkError
from mendari.standards.fhir import FhirAdapter
from mendari.standards.fhir_client import GuardedHttp
from tests.conftest import CHART_DIR, CLINICAL_DIR, DEMO_AGENT_DIR
from tests.support import fixture_records as records
from tests.support.fixture_records import Case

FHIR = "https://ehr.example.org/fhir"
CHART = "mendari-test/chart-summary"  # a patient's record in: a patient-level export reads as it
LABELS = CHART_DIR / "data" / "test.jsonl"  # its test set: the site's labels of its patients
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


class PolledTooSoon(FakeBulk):
    """A status URL that answers its first poll `429 Too Many Requests`, asking for 300 s, as
    Bulk Data STU2 says a server SHOULD answer a client that polls too often; then as FakeBulk."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.slowed = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == STATUS and request.method == "GET" and not self.slowed:
            self.slowed = True
            self.requests.append(request)
            return httpx.Response(429, headers={"retry-after": "300"})
        return super().__call__(request)


def test_a_429_while_polling_is_waited_out_for_as_long_as_the_server_asks(
    tmp_path: Path,
) -> None:
    """A 429 to a poll asks the client to wait, as a 202 does: never a failed export. The wait
    is the one its Retry-After asks for, however long (a client that waited less polled too
    soon again), and `max_wait` bounds the whole wait."""
    rows = [{"resourceType": "Patient", "id": "p1"}]
    manifest, slept = run(PolledTooSoon({"Patient": rows}, pending=1), tmp_path)
    assert slept == [300.0, 3.0]
    assert [(o["type"], o["file"], o["count"]) for o in manifest["output"]] == [
        ("Patient", "Patient.001.ndjson", 1)
    ]
    with pytest.raises(BulkError, match="did not finish within 200 s"):
        run(PolledTooSoon({"Patient": rows}), tmp_path, max_wait=200)


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


OTHER = "https://files.example.org"  # allow-listed, but not the server the token is for


class FilesElsewhere(FakeBulk):
    """The manifest's file is on another allow-listed origin, which serves it."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(OTHER):
            self.requests.append(request)
            return httpx.Response(200, text='{"resourceType": "Patient", "id": "p1"}\n')
        response = super().__call__(request)
        if str(request.url) == STATUS and response.status_code == 200:
            manifest = json.loads(response.content)
            manifest["output"] = [{"type": "Patient", "url": f"{OTHER}/Patient.ndjson"}]
            return httpx.Response(200, json=manifest)
        return response


def test_the_token_never_goes_to_a_file_on_another_allow_listed_server(tmp_path: Path) -> None:
    """requiresAccessToken with a file on another origin: refused, and nothing is sent there."""
    fake = FilesElsewhere({}, pending=0)
    with (
        http_for(fake, {"https://ehr.example.org", OTHER}) as http,
        pytest.raises(BulkError, match="sent only to the server it was issued for"),
    ):
        bulk.export(FHIR, tmp_path, http=http, token="t0", sleep=lambda _: None)
    assert not [r for r in fake.requests if str(r.url).startswith(OTHER)]


def test_one_file_on_another_server_fails_the_export_before_any_download(tmp_path: Path) -> None:
    """The manifest's own files come first, another server's last: nothing is downloaded."""

    class OneElsewhere(FilesElsewhere):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            response = FakeBulk.__call__(self, request)
            if str(request.url) == STATUS and response.status_code == 200:
                manifest = json.loads(response.content)
                manifest["output"].append({"type": "Patient", "url": f"{OTHER}/Patient.ndjson"})
                return httpx.Response(200, json=manifest)
            return response

    fake = OneElsewhere({"Patient": [{"resourceType": "Patient", "id": "p1"}]}, pending=0)
    with (
        http_for(fake, {"https://ehr.example.org", OTHER}) as http,
        pytest.raises(BulkError, match="sent only to the server it was issued for"),
    ):
        bulk.export(FHIR, tmp_path, http=http, token="t0", sleep=lambda _: None)
    assert not [r for r in fake.requests if "/files/" in str(r.url) or str(r.url).startswith(OTHER)]
    assert not list(tmp_path.glob("*.ndjson"))


def test_a_file_on_another_server_without_requires_access_token_is_fetched_without_it(
    tmp_path: Path,
) -> None:
    fake = FilesElsewhere({}, pending=0)
    fake.manifest_extra = {"requiresAccessToken": False}
    with http_for(fake, {"https://ehr.example.org", OTHER}) as http:
        bulk.export(FHIR, tmp_path, http=http, token="t0", sleep=lambda _: None)
    (download,) = [r for r in fake.requests if str(r.url).startswith(OTHER)]
    assert "authorization" not in download.headers


def test_the_token_never_goes_to_a_status_url_on_another_allow_listed_server(
    tmp_path: Path,
) -> None:
    fake = FakeBulk({}, pending=0)
    fake.kickoff = httpx.Response(202, headers={"content-location": f"{OTHER}/status/1"})
    with (
        http_for(fake, {"https://ehr.example.org", OTHER}) as http,
        pytest.raises(BulkError, match="status URL is on another server"),
    ):
        bulk.export(FHIR, tmp_path, http=http, token="t0", sleep=lambda _: None)
    assert len(fake.requests) == 1  # the kick-off only


@pytest.mark.parametrize(
    ("manifest", "said"),
    [
        ({"output": [{"type": "Patient"}]}, "not a file with a url"),  # KeyError
        ({"output": ["https://ehr.example.org/files/Patient.ndjson"]}, "not a file with a url"),
        ({"output": {"type": "Patient"}}, "not a file with a url"),  # iterated its keys
        ({"output": [{"type": "Patient", "url": 7}]}, "not a file with a url"),
    ],
)
def test_a_manifest_of_the_wrong_shape_fails_before_any_download(
    tmp_path: Path, manifest: dict[str, Any], said: str
) -> None:
    fake = FakeBulk({}, pending=0)
    fake.manifest_extra = manifest
    with pytest.raises(BulkError, match=said):
        run(fake, tmp_path)
    assert not [r for r in fake.requests if "/files/" in str(r.url)]


@pytest.mark.parametrize("body", [b"[1, 2]", b"[" * 100_000])
def test_a_manifest_that_is_not_a_json_object_fails(tmp_path: Path, body: bytes) -> None:
    """A list raised AttributeError; nesting past the parser's depth raised RecursionError."""

    class Odd(FakeBulk):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if str(request.url) == STATUS:
                return httpx.Response(200, content=body)
            return super().__call__(request)

    with pytest.raises(BulkError, match="manifest"):
        run(Odd({}, pending=0), tmp_path)


def test_an_export_with_more_files_than_the_cap_downloads_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest listing a million files (each up to the per-file cap) had no bound at all."""
    monkeypatch.setattr(bulk, "MAX_FILES", 2)
    fake = FakeBulk({}, pending=0)
    fake.manifest_extra = {
        "output": [
            {"type": "Patient", "url": f"https://ehr.example.org/files/Patient{n}.ndjson"}
            for n in range(3)
        ]
    }
    with pytest.raises(BulkError, match="lists 3 files; at most 2"):
        run(fake, tmp_path)
    assert not [r for r in fake.requests if "/files/" in str(r.url)]


def test_an_export_larger_than_the_cap_together_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each file under the per-file cap, the files together over the export's."""
    rows = [{"resourceType": "Patient", "id": f"p{n}"} for n in range(3)]
    size = len("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(bulk, "MAX_EXPORT_BYTES", size + 10)
    fake = FakeBulk({"Patient": rows, "Group": rows}, pending=0)
    with pytest.raises(BulkError, match=r"larger than .* together"):
        run(fake, tmp_path)


def test_lines_are_counted_a_part_at_a_time(tmp_path: Path) -> None:
    path = tmp_path / "x.ndjson"
    for text, lines in (("", 0), ("a", 1), ("a\n", 1), ("a\n\n  \nb", 2), ("\n a \n", 1)):
        path.write_text(text)
        assert bulk._lines(path) == lines
    path.write_bytes(b"x" * (3 * 1024 * 1024) + b"\n" + b" " * (1024 * 1024) + b"\ny")
    assert bulk._lines(path) == 2


@pytest.mark.parametrize("access", ["two words", "line\r\nbreak", 7])
def test_a_backend_services_token_that_cannot_be_a_header_is_refused(access: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("smart-configuration"):
            return httpx.Response(
                200, json={"token_endpoint": "https://ehr.example.org/auth/token"}
            )
        return httpx.Response(200, json={"access_token": access, "token_type": "bearer"})

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    with http_for(handler) as http, pytest.raises(BulkError, match="not a valid OAuth token"):
        bulk.backend_token(
            FHIR, client_id="site-app", private_key=pem, key_id="k1", scope="s", http=http
        )


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("7", 7.0), ("0", 1.0), ("99999", 99999.0), (None, None), ("soon", None)],
)
def test_retry_after_is_kept_sensible(value: str | None, seconds: float | None) -> None:
    """At least a second, and otherwise what the server asks: `max_wait` bounds the whole wait
    (`test_a_429_while_polling_is_waited_out_for_as_long_as_the_server_asks`)."""
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


@pytest.fixture
def on_the_day() -> Iterator[None]:
    """Read the chart records on the day they were built for: an age read from a birth date is
    the case's age, as its dataset line says."""
    with mock.patch.object(clock, "today", lambda: records.TODAY):
        yield


def chart_export(folder: Path, cases: Sequence[Case]) -> Path:
    """What a server exports for these chart fixture patients, as a patient-level export: each
    one's record, NDJSON per type, its resources with the ids and references the server stores
    (the datasets' cases keep their drugs and notes inside the resources that name them)."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        for resource in case.resources(records.TODAY):
            by_type.setdefault(resource["resourceType"], []).append(resource)
    for kind, rows in by_type.items():
        (folder / f"{kind}.001.ndjson").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return folder


def exported(tmp_path: Path, cases: list[list[dict[str, Any]]]) -> Path:
    """What a server exports for these cases (each its transaction entries): NDJSON per type,
    ids and references resolved as a server stores them."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for n, entries in enumerate(cases):
        ids = {
            e["fullUrl"]: f"{e['resource']['resourceType']}/c{n}-{i}" for i, e in enumerate(entries)
        }
        for i, entry in enumerate(entries):
            resource = json.loads(json.dumps(entry["resource"]))
            resource["id"] = f"c{n}-{i}"
            for name in ("subject", "sender"):
                if name in resource:
                    resource[name] = {"reference": ids[resource[name]["reference"]]}
            by_type.setdefault(resource["resourceType"], []).append(resource)
    for kind, rows in by_type.items():
        (tmp_path / f"{kind}.001.ndjson").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return tmp_path


@pytest.mark.usefixtures("on_the_day")
def test_an_export_becomes_the_agents_dataset_with_the_sites_labels(tmp_path: Path) -> None:
    cases = records.cases("test")[:6]
    export_dir = chart_export(tmp_path, cases)
    labels = {case.id: case.labels for case in cases[:4]}  # the site labelled four of six
    adapter = FhirAdapter.for_agent(resolve_agent(CHART))
    made, skipped = bulk.dataset(
        adapter, list(bulk.read_ndjson(export_dir)), labels, key_system=CASE_SYSTEM
    )
    assert [r["id"] for r in made] == [case.id for case in cases[:4]]
    assert skipped == {"unlabelled": 2, "no_key": 0}
    first = made[0]["input"]
    assert first == cases[0].input()  # the patient's record came along, read as the dataset's
    assert first["conditions"] and first["medications"]
    assert made[0]["labels"] == cases[0].labels


@pytest.mark.usefixtures("on_the_day")
def test_the_cli_builds_a_dataset_that_mendari_eval_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = LABELS
    (tmp_path / "export").mkdir()
    export_dir = chart_export(tmp_path / "export", records.cases("test")[:5])
    out = tmp_path / "bulk.jsonl"
    assert (
        main(
            [
                "bulk",
                "dataset",
                "--agent",
                CHART,
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
    assert main(["eval", CHART, "--dataset", str(out), "--baseline", "none"]) == 0


@pytest.mark.usefixtures("on_the_day")
def test_the_sandbox_bulk_check_compares_the_export_with_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit check's comparison (its export runs against HAPI in `sandbox-e2e`)."""
    from mendari.sandbox import bulk_check

    cases = records.cases("test")[:8]
    asked: list[list[str]] = []

    def fake_export(fhir: str, out: Path, **kwargs: Any) -> dict[str, Any]:
        asked.append(kwargs["types"])
        out.mkdir(parents=True)
        chart_export(out, cases)
        return {
            "output": [{"type": p.name.split(".")[0], "count": 1} for p in out.glob("*.ndjson")]
        }

    monkeypatch.setattr(bulk_check.bulk, "export", fake_export)
    result = bulk_check.run("http://fhir:8080/fhir", resolve_agent(CHART), str(LABELS))
    assert result["cases"] == 8 and result["identical"] and result["differences"] == []
    # The export asks for what the agent's mapping reads, and no more: its focus, the types it
    # names (a note's, a request's drug's) and its lists' (one of them read from two types).
    assert asked == [
        [
            "Patient",
            "DocumentReference",
            "Medication",
            "Condition",
            "MedicationStatement",
            "MedicationRequest",
        ]
    ]
    clinical = load_agent(CLINICAL_DIR)  # a note as JSON: no FHIR mapping
    with pytest.raises(MendariError, match=f"{clinical.id} has no FHIR mapping"):
        bulk_check.run("http://fhir:8080/fhir", clinical)


def test_the_sandbox_bulk_check_runs_on_the_demo_agent_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari sandbox bulk-check` names no agent by default: the bundled demo agent, whose
    seeded messages (`mendari sandbox seed`) come back from an export of Patients and
    Communications as its benchmark cases, and evaluate as those cases do from the file."""
    from mendari.sandbox import bulk_check

    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    demo = load_agent(DEMO_AGENT_DIR)
    seeded = fhir_workflow.seed_bundle(demo, 12)["entry"]
    asked: list[list[str]] = []

    def fake_export(fhir: str, out: Path, **kwargs: Any) -> dict[str, Any]:
        asked.append(kwargs["types"])
        out.mkdir(parents=True)
        exported(out, [seeded[start : start + 2] for start in range(0, len(seeded), 2)])
        return {
            "output": [{"type": p.name.split(".")[0], "count": 12} for p in out.glob("*.ndjson")]
        }

    monkeypatch.setattr(bulk_check.bulk, "export", fake_export)
    assert main(["sandbox", "bulk-check", "--fhir", "http://fhir:8080/fhir"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert asked == [["Patient", "Communication"]]
    assert result["files"] == {"Patient": 12, "Communication": 12}
    assert (
        result["cases"] == 12
        and result["identical"]
        and result["left_out"]
        == {
            "unlabelled": 0,
            "no_key": 0,
        }
    )
    assert demo.id == DEMO_AGENT
    # Labels from another file than the sandbox was seeded from: no case is labelled.
    other = tmp_path / "other.jsonl"
    other.write_text('{"id": "x-1", "input": {"message": {"body": "Hello"}}, "labels": {}}\n')
    assert main(["sandbox", "bulk-check", "--fhir", "http://f", "--dataset", str(other)]) == 1
    assert "the export holds no labelled mendari/portal-message-agent cases" in (
        capsys.readouterr().err
    )
