# SPDX-License-Identifier: Apache-2.0
"""A vendor-shaped FHIR server of the chart-summary fixture's records, for the tests of record
reading: what an EHR's FHIR API answers, inside the test (an `httpx.MockTransport` handler; no
network, no container).

Synthetic test fixture. Not clinically reviewed. Not for patient care.

**Its records** (`vendor_shaped`) are a case's (`tests/support/fixture_records.py`) as an EHR
like Epic writes them: a condition's codings carry the site's own code first; every medication
is a MedicationRequest naming its drug by reference to a Medication beside it (which a search
includes only when asked, `_include`), its period a dosage `boundsPeriod`; every note is a
DocumentReference whose attachments are only urls to Binaries, in one rendition or two
(`rendition`: plain text, HTML, RTF, or HTML with an RTF twin of the same note, as Epic sends).

**Its API** (`ChartEhr`):

- `GET <base>/<Type>/<id>` answers the resource as FHIR JSON (a Binary too); one it does not
  hold is a 404 OperationOutcome whose text (`SERVER_TEXT`) must reach no record.
- `GET <base>/<Type>?patient=<id>` answers one searchset page of that patient's resources of the
  type (by `subject`, `patient` or `beneficiary`), narrowed by `category`, `code` and `status`
  tokens (the code alone, as `system|code` or bare; a comma is "or"), with
  `_include=MedicationRequest:medication` adding each request's Medication.
- A MedicationStatement search is refused with a 403 OperationOutcome (`statements="refused"`,
  as Epic has no R4 MedicationStatement API: what the EHR declaration of
  `fixture_records.ehr_declaration` says), answered empty (`"absent"`), or answered
  (`"offered"`).
- With `token`, a request without that bearer token is a 401. `faults` gives a status to a path
  (`"Medication/x"` for a read, `"Condition"` for that type's search), answered with an
  OperationOutcome. Strict (the default), any other method, url or parameter is a 400.
- Every request is kept (`requests`), and `reads()`, `searches()` say what was asked.

`client()` is a `FhirClient` for it (for `smart_routes.decide_for` and `linked.Linker`), and
`install(monkeypatch)` serves it to the engine's CDS hooks, as `tests/test_fhir_client.py`
`allow_ehr` does.
"""

from __future__ import annotations

import base64
import functools
import html
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import date
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from mendari.standards import cds_hooks
from mendari.standards.fhir_client import FhirClient
from tests.support.fixture_records import (
    BASE,
    CLINICAL_NOTE,
    PROGRESS_NOTE,
    TODAY,
    Case,
    bundle_entry,
    tagged,
)

ORIGIN = "https://ehr.example.org"
TOKEN = "chart-ehr-test-token"  # made for these tests; no server outside them takes it
SERVER_TEXT = "SERVER-SAYS-CHART-SECRET"  # what the server's errors say: never in a record
INCLUDE = "MedicationRequest:medication"
TOKENS = ("category", "code", "status")  # the search parameters read as tokens
Rendition = Literal["plain", "html", "rtf", "twin"]
Statements = Literal["refused", "absent", "offered"]
# Each rendition's attachments, as (content type, how the note's sentences are written).
RENDITIONS: dict[str, tuple[str, ...]] = {
    "plain": ("text/plain",),
    "html": ("text/html",),
    "rtf": ("text/rtf",),
    "twin": ("text/html", "text/rtf"),
}


def _written(sentences: tuple[str, ...], content_type: str) -> str:
    """A note's sentences as a document of the content type: plain text, an HTML page, RTF."""
    if content_type == "text/html":
        body = "".join(f"<p>{html.escape(sentence)}</p>" for sentence in sentences)
        return f"<!DOCTYPE html><html><body>{body}</body></html>"
    if content_type == "text/rtf":
        return "{\\rtf1\\ansi " + "\\par ".join(sentences) + "\\par}"
    return " ".join(sentences)


def vendor_shaped(
    case: Case, today: date = TODAY, *, rendition: Rendition = "plain"
) -> list[dict[str, Any]]:
    """The case's record as the EHR keeps it (see the module): its resources, the Medications
    and Binaries beside the resources that name them. What its notes say is the case's; an RTF
    rendition alone cannot be read, and the decision abstains."""
    requests = tuple(
        replace(
            medication,
            source="request",
            drug="reference",
            period="bounds" if medication.source == "statement" else medication.period,
        )
        for medication in case.medications
    )
    shaped = replace(case, medications=requests, notes=())
    resources = shaped.resources(today, site="beside")
    order = resources.pop()  # the order last, after the notes
    subject = {"reference": f"Patient/{case.id}"}
    for n, note in enumerate(case.notes, start=1):
        reference = f"{case.id}-note-{n}"
        content, binaries = [], []
        for m, content_type in enumerate(RENDITIONS[rendition], start=1):
            binary_id = f"{reference}-{m}"
            content.append(
                {"attachment": {"contentType": content_type, "url": f"Binary/{binary_id}"}}
            )
            data = _written(note.sentences, content_type)
            binaries.append(
                tagged(
                    {
                        "resourceType": "Binary",
                        "id": binary_id,
                        "contentType": content_type,
                        "data": base64.b64encode(data.encode("utf-8")).decode("ascii"),
                    }
                )
            )
        document = {
            "resourceType": "DocumentReference",
            "id": reference,
            "status": "current",
            "type": PROGRESS_NOTE,
            "category": [{"coding": [CLINICAL_NOTE]}],
            "subject": subject,
            "date": f"{note.written}T12:00:00Z",
            "content": content,
        }
        resources += [tagged(document, "us-core-documentreference"), *binaries]
    resources.append(order)
    return resources


def _outcome(status: int, code: str) -> httpx.Response:
    body = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": code, "diagnostics": SERVER_TEXT}],
    }
    return httpx.Response(status, json=body, headers={"content-type": "application/fhir+json"})


def _json(body: Mapping[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=dict(body), headers={"content-type": "application/fhir+json"})


def _codes(value: Any) -> set[str]:
    """Every code of a CodeableConcept, a list of them, or a code."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return set().union(*(_codes(item) for item in value)) if value else set()
    if isinstance(value, Mapping):
        return {
            coding["code"]
            for coding in value.get("coding", [])
            if isinstance(coding, Mapping) and isinstance(coding.get("code"), str)
        }
    return set()


def _owner(resource: Mapping[str, Any]) -> str | None:
    if resource.get("resourceType") == "Patient":
        return f"Patient/{resource.get('id')}"
    for key in ("subject", "patient", "beneficiary"):
        reference = resource.get(key)
        if isinstance(reference, Mapping) and isinstance(reference.get("reference"), str):
            return str(reference["reference"])
    return None


class ChartEhr:
    """A vendor-shaped FHIR server of `resources` (see the module), under `base`."""

    def __init__(
        self,
        resources: Iterable[Mapping[str, Any]],
        *,
        base: str = BASE,
        statements: Statements = "refused",
        token: str | None = None,
        faults: Mapping[str, int] | None = None,
        strict: bool = True,
    ) -> None:
        self.base = base.rstrip("/")
        self.held: dict[str, dict[str, Any]] = {}
        for resource in resources:
            self.held.setdefault(f"{resource['resourceType']}/{resource['id']}", dict(resource))
        self.statements = statements
        self.token = token
        self.faults = dict(faults or {})
        self.strict = strict
        self.requests: list[httpx.Request] = []

    @classmethod
    def of(
        cls,
        *cases: Case,
        today: date = TODAY,
        rendition: Rendition | Mapping[str, Rendition] = "plain",
        **options: Any,
    ) -> ChartEhr:
        """A server holding each case's vendor-shaped record; `rendition` for every case's notes,
        or by case id (`"plain"` for a case it does not name)."""
        resources: list[dict[str, Any]] = []
        for case in cases:
            chosen = rendition if isinstance(rendition, str) else rendition.get(case.id, "plain")
            resources += vendor_shaped(case, today, rendition=chosen)
        return cls(resources, **options)

    def bundle(self) -> dict[str, Any]:
        """Everything it holds, as a collection Bundle."""
        return {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [bundle_entry(resource) for resource in self.held.values()],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.method != "GET" or not url.startswith(f"{self.base}/"):
            return _outcome(400, "not-supported")
        if (
            self.token is not None
            and request.headers.get("authorization") != f"Bearer {self.token}"
        ):
            return _outcome(401, "login")
        parts = urlsplit(url)
        path = parts.path.removeprefix(urlsplit(self.base).path).strip("/")
        if path in self.faults:
            return _outcome(self.faults[path], "exception")
        if "/" in path:
            found = self.held.get(path)
            return _json(found) if found is not None else _outcome(404, "not-found")
        return self._search(path, parse_qsl(parts.query, keep_blank_values=True))

    def _search(self, kind: str, query: list[tuple[str, str]]) -> httpx.Response:
        parameters: dict[str, list[str]] = {}
        for name, value in query:
            parameters.setdefault(name, []).append(value)
        known = {"patient", "_include", *TOKENS}
        if self.strict and (
            set(parameters) - known or "patient" not in parameters or len(parameters["patient"]) > 1
        ):
            return _outcome(400, "not-supported")
        if kind == "MedicationStatement" and self.statements == "refused":
            return _outcome(403, "forbidden")
        includes = parameters.get("_include", [])
        if self.strict and any(include != INCLUDE for include in includes):
            return _outcome(400, "not-supported")
        owner = f"Patient/{parameters.get('patient', [''])[0]}"
        matches = [
            resource
            for resource in self.held.values()
            if resource["resourceType"] == kind
            and _owner(resource) == owner
            and all(
                self._token_matches(resource, name, parameters[name])
                for name in TOKENS
                if name in parameters
            )
        ]
        if kind == "MedicationStatement" and self.statements == "absent":
            matches = []
        included: list[dict[str, Any]] = []
        if kind == "MedicationRequest" and INCLUDE in includes:
            for request in matches:
                reference = request.get("medicationReference", {}).get("reference")
                drug = self.held.get(reference) if isinstance(reference, str) else None
                if drug is not None and drug not in included:
                    included.append(drug)
        entries = [{**bundle_entry(resource), "search": {"mode": "match"}} for resource in matches]
        entries += [
            {**bundle_entry(resource), "search": {"mode": "include"}} for resource in included
        ]
        return _json(
            {"resourceType": "Bundle", "type": "searchset", "total": len(matches), "entry": entries}
        )

    @staticmethod
    def _token_matches(resource: Mapping[str, Any], name: str, values: list[str]) -> bool:
        wanted = {code.rpartition("|")[2] for value in values for code in value.split(",")}
        return bool(_codes(resource.get(name)) & wanted)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def client(self, *, token: str | None = TOKEN) -> FhirClient:
        """A client of this server, its origin allowed, with `token`."""
        return FhirClient(self.base, token=token, allowed={ORIGIN}, transport=self.transport())

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve it to the engine's CDS hooks: its origin on the allow-list, and every client a
        hook opens (its prefetch reads and linked reads) talking to it."""
        monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", ORIGIN)
        patched = functools.partial(FhirClient, transport=self.transport())
        monkeypatch.setattr(cds_hooks, "FhirClient", patched)

    def reads(self) -> list[str]:
        """The paths read by id, in order (`Binary/n1`, `Medication/m1`)."""
        return [path for path, _ in self._paths() if "/" in path]

    def searches(self) -> list[str]:
        """The searches made, in order, as `<Type>?<query>`."""
        return [f"{path}?{query}" for path, query in self._paths() if "/" not in path]

    def _paths(self) -> list[tuple[str, str]]:
        """Each request's path under the base and its query, in order."""
        found = []
        for request in self.requests:
            parts = urlsplit(str(request.url))
            found.append(
                (parts.path.removeprefix(urlsplit(self.base).path).strip("/"), parts.query)
            )
        return found
