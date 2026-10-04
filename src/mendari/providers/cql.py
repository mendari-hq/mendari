"""CQL: answers computed by a Clinical Quality Language library on a CQL engine.

CQL is how eCQMs and much clinical decision support logic are written, so a site can bring its
own eligibility or quality logic to a pack. The library ships in the pack, and each question the
slot answers maps to one of its definitions:

    variants:
      cql:
        type: cql
        library: logic/criteria.cql
        as_of: as_of                        # input field with the screening date; today if absent
        expressions:
          age_18_75: "Age 18 To 75"         # a String result is the label itself
          t2d_diagnosis: "T2D Diagnosis"    # a Boolean one: true = yes, false = no, null = unknown

The engine is a FHIR server with Clinical Reasoning (HAPI FHIR with `hapi.fhir.cr.enabled`) at
`$MENDARI_CQL_URL`, which must be on the FHIR allow-list (`MENDARI_FHIR_ALLOWED_SERVERS`).
The provider uploads the library once, then calls `Library/{id}/$evaluate` for each decision
with the patient's record as a FHIR Bundle (`standards/record_fhir.py`), `useServerData=false`
and the screening date as the `Screening Date` parameter. Nothing is read from the server's own
data.

The engine is shared by every pack on a deployment, so a pack's library is uploaded under names
no other pack's library can take (`engine_names`): its resource id, and the name it declares,
each end in a hash of the pack id and one of the library's text. A FHIR PUT by id replaces what
is there, and the engine finds a library by its declared name and version, so with the declared
name used as is, two packs whose libraries share a name and version (an outside pack's, say)
overwrote or shadowed each other, and one pack's decisions ran the other's CQL. The declaration
must therefore be the library's first statement (comments aside), where CQL puts it, and the name
is rewritten there. Evidence and the provider's model id keep the declared name.

An answer is as certain as a `criteria` answer (it is computed, not guessed). A failure (the
engine unreachable, an evaluation error, a result that isn't a label) is a `ProviderError`, and
the policy then sends the decision to a person.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.core import clock
from mendari.errors import FhirError, PackValidationError, ProviderError
from mendari.providers.base import Provider, ProviderOutput, fingerprint, get_path
from mendari.providers.criteria import DECIDED, _date
from mendari.standards import record_fhir
from mendari.standards.fhir_client import GuardedHttp

URL_ENV = "MENDARI_CQL_URL"
_HEADER = re.compile(r"^\s*library\s+([A-Za-z][A-Za-z0-9_]*)\s+version\s+'([^']+)'", re.MULTILINE)
# Reading the first statement: spaces, a `//` comment to the end of its line, a declaration.
_SPACES = re.compile(r"\s*")
_LINE_COMMENT = re.compile(r"//[^\r\n]*")
_DECLARATION = re.compile(r"library\s+([A-Za-z][A-Za-z0-9_]*)\s+version\s+'([^']+)'")


def library_declaration(source: str) -> re.Match[str] | None:
    """The library's first statement (after spaces and `//` or `/* */` comments, as CQL reads
    it) when it is `library Name version '...'`, else None; linear in `source`."""
    position = 0
    while True:
        position = _SPACES.match(source, position).end()  # type: ignore[union-attr]
        if source.startswith("//", position):
            position = _LINE_COMMENT.match(source, position).end()  # type: ignore[union-attr]
        elif source.startswith("/*", position):
            end = source.find("*/", position + 2)
            if end < 0:
                return None
            position = end + 2
        else:
            return _DECLARATION.match(source, position)


def engine_names(pack_id: str, name: str, source: str) -> tuple[str, str]:
    """The library's resource id (FHIR: at most 64 of `[A-Za-z0-9.-]`) and the name it is
    declared under on the engine, both this pack's and this text's alone (module docstring)."""
    owner = hashlib.sha256(pack_id.encode("utf-8")).hexdigest()[:16]
    content = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"mendari-{owner}-{content}", f"{name}_{owner}_{content}"


class CqlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    library: str
    as_of: str = "as_of"
    expressions: dict[str, str] = Field(min_length=1)


def cql_url() -> str:
    url = os.environ.get(URL_ENV, "").strip().rstrip("/")
    if not url:
        raise ProviderError("not_configured", f"set {URL_ENV} (a FHIR server with CQL)")
    return url


class CqlProvider(Provider):
    type = "cql"
    remote = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        try:
            self.settings = CqlConfig.model_validate(self.config)
        except ValidationError as error:
            raise PackValidationError(self.pack.id, [f"{self.info.slot}: {error}"]) from None
        library = self.settings.library
        if not self.pack.path(library).is_file():
            raise PackValidationError(self.pack.id, [f"{self.info.slot}: no file {library}"])
        self.source = self.pack.read_text(library)
        header = _HEADER.search(self.source)
        if header is None:
            problem = f"{library}: needs a `library Name version '...'` declaration"
            raise PackValidationError(self.pack.id, [problem])
        self.name, self.version = header.group(1), header.group(2)
        declaration = library_declaration(self.source)
        if declaration is None or declaration.span(1) != header.span(1):
            problem = (
                f"{library}: the `library Name version '...'` declaration must be the library's "
                "first statement (only comments before it)"
            )
            raise PackValidationError(self.pack.id, [problem])
        if set(self.settings.expressions) != set(self.questions):
            problem = f"{self.info.slot}: `expressions` must map exactly the slot's questions"
            raise PackValidationError(self.pack.id, [problem])
        self.info = replace(self.info, model=f"cql:{self.name}@{fingerprint(self.pack, library)}")
        self.resource_id, self.engine_name = engine_names(self.pack.id, self.name, self.source)
        start, end = declaration.span(1)
        self.engine_source = self.source[:start] + self.engine_name + self.source[end:]
        self._uploaded_to: str | None = None

    def preflight(self) -> None:
        cql_url()

    def library_resource(self) -> dict[str, Any]:
        return {
            "resourceType": "Library",
            "id": self.resource_id,
            "url": f"https://mendari.co/packs/{self.pack.id}/Library/{self.engine_name}",
            "version": self.version,
            "name": self.engine_name,
            "status": "active",
            "type": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/library-type",
                        "code": "logic-library",
                    }
                ]
            },
            "content": [
                {
                    "contentType": "text/cql",
                    "data": base64.b64encode(self.engine_source.encode("utf-8")).decode("ascii"),
                }
            ],
        }

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        base = cql_url()
        as_of = _date(get_path(inputs, self.settings.as_of)) or clock.today()
        data = record_fhir.bundle(inputs, as_of)
        parameters = {
            "resourceType": "Parameters",
            "parameter": [
                {"name": "subject", "valueString": f"Patient/{record_fhir.PATIENT_ID}"},
                {"name": "useServerData", "valueBoolean": False},
                {"name": "data", "resource": data},
                {
                    "name": "parameters",
                    "resource": {
                        "resourceType": "Parameters",
                        "parameter": [{"name": "Screening Date", "valueDate": as_of.isoformat()}],
                    },
                },
                *(
                    {"name": "expression", "valueString": expression}
                    for expression in sorted(set(self.settings.expressions.values()))
                ),
            ],
        }
        try:
            with GuardedHttp() as http:
                library = f"{base}/Library/{self.resource_id}"
                if self._uploaded_to != base:
                    http.send_json("PUT", library, self.library_resource())
                    self._uploaded_to = base
                result = http.send_json("POST", f"{library}/$evaluate", parameters)
        except FhirError as failure:
            raise ProviderError("unavailable", f"the CQL engine: {failure}") from None
        values = _results(result)
        answers = {}
        for question, expression in self.settings.expressions.items():
            label = _label(values.get(expression))
            labels = list(self.questions[question].criteria)
            if label not in labels:
                shown = "null" if label is None else repr(label)
                raise ProviderError(
                    "invalid_response", f"{expression} gave {shown}, not a label of {question}"
                )
            rest = (1 - DECIDED) / (len(labels) - 1)
            probabilities = {lab: (DECIDED if lab == label else rest) for lab in labels}
            evidence = f"CQL {self.name} {self.version}: {expression} = {label}"
            answers[question] = self.make_answer(question, probabilities, (evidence,))
        return ProviderOutput(answers=answers)


def _results(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Each returned definition's value. An `evaluation error` fails the call (its detail stays
    with the engine: remote error text never reaches the audit record)."""
    if parameters.get("resourceType") != "Parameters":
        raise ProviderError("invalid_response", "the CQL engine did not answer with Parameters")
    values: dict[str, Any] = {}
    for parameter in parameters.get("parameter") or []:
        if not isinstance(parameter, Mapping):
            continue
        name = parameter.get("name")
        if name == "evaluation error":
            raise ProviderError("invalid_response", "the CQL engine reported an evaluation error")
        typed = [key for key in parameter if key.startswith("value")]
        if isinstance(name, str) and typed:
            values[name] = parameter[typed[0]]
    return values


def _label(value: Any) -> str | None:
    """A String result is a label; a Boolean is yes / no; null (no result) is unknown."""
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    return None
