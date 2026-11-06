# SPDX-License-Identifier: Apache-2.0
"""Linked record data (LP-23h): what a spec 0.3 mapping reaches by a link the record does not
hold, read from the EHR's FHIR server before the mapping runs.

A CDS hook and the SMART launch's decide read a decision's record from the EHR (its prefetch,
`cds_hooks.read_each`). A 0.3 mapping may reach more by a link: a note's attachment `url` to a
Binary (`attachment_text`), and a reference a field follows (`follow`: the payer behind
`Coverage.payor`, the drug behind `medicationReference`). What the record holds resolves inside
it (`fhir.resolve`: contained, a Bundle entry, `Type/id`). What it does not, `Linker.read` reads
and adds to the record (`Indexed.link`), as spec 0.3 §2.3 allows a runtime to; the mapping itself
never fetches. Each read:

- goes through `FhirClient`: to the request's `fhirServer` only (on the allow-list), under its
  base URL. A link anywhere else is `off_server` and is never fetched. A CDS hook's `fhirServer`
  off the allow-list is refused before anything is decided (`cds_hooks.linker`); should `open`
  still fail so, every link is `off_server`;
- reads one resource by its `Type/id`, of the type the field reads (a Binary for a note's url),
  with one request: no page followed, no redirect. A Binary is read as FHIR JSON (`Accept:
  application/fhir+json`), the only answer `GuardedHttp` takes. The answer must be of that type
  (`unsupported_type`, `operation_outcome`) and R4 (`not_r4`), and a failed read keeps its
  reason (`timeout`, `unauthorized` and the others);
- is bounded: at most `MAX_READS` links a decision, and `MAX_BYTES` each once decoded (a
  Binary's content, another resource's JSON as it arrives). A link past the first `MAX_READS`
  is `cap`, not read; an answer over the size is `too_large`, and stops arriving;
- is made once a request: the draft orders of one order hook share what was read, and a link
  met twice in one decision is read once, by its URL under the base whatever its spelling. The
  bound is a decision's, so a request's is its decisions': an order hook of `MAX_ORDERS` (50)
  draft orders, each with links of its own, can read up to 50 x `MAX_READS` links, and what was
  read is kept (parsed, at most `BINARY_ANSWER` x `MAX_BYTES` an answer) until the request
  ends (threat-model D14).

A link not read keeps its reason (`Indexed.unread`): the data-coverage record counts it, and the
decision abstains with `could not read <field>: <reason>` (`data_coverage.unreadable_notes`,
`unreadable_links`). A reference that reached nothing is a link when it is `Type/id` (relative,
or ending an absolute URL) of a type the field reads; `#id`, `urn:…` and a reference by
identifier only are not, and are left as they are. Notes are read as their renditions allow: a
DocumentReference's `content` and a DiagnosticReport's `presentedForm` are one document in
other formats (`fhir.renditions`), so once one rendition of a resource reads, the others are not
read. A note's url that names another type than a Binary is `unsupported_type`, never read. A
resource its list leaves out (`fhir.listed`: a MedicationRequest entered in error, when its
source derives its period) is not an item, so nothing it links to is read. Without a linker
(`fhir/decide`, `$decide`, `mendari decide --fhir`) nothing is fetched, and a link the record
does not hold is `not_fetched`. A 0.1 or 0.2 mapping has no link: nothing is read for it.

Nothing a link names reaches a reason, the data-coverage record or the chain: only the field's
name and a reason code from the closed set (`errors.ReadReason`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

from mendari.errors import FhirError, ReadReason
from mendari.standards import fhir_structure
from mendari.standards.fhir import (
    FhirMapping,
    FieldMapping,
    Indexed,
    Reading,
    decoded_size,
    followed_types,
    link_target,
    listed,
    located_fields,
    read_attachment,
    read_field,
    renditions,
    waiting_link,
)
from mendari.standards.fhir_client import FhirClient

MAX_READS = 20  # links one decision may read
MAX_BYTES = 1024 * 1024  # each, once decoded: a Binary's content, another resource's JSON
# A Binary's answer holds its content as base64 (4 characters for 3 bytes, line breaks allowed)
# inside its JSON: the answer may be this many times `MAX_BYTES`, then the content is measured
# exactly.
BINARY_ANSWER = 2
BINARY = "Binary"

Read = Mapping[str, Any] | ReadReason  # a link's resource, or why it was not read


def reads_links(mapping: FhirMapping) -> bool:
    """Whether a mapping can reach anything by a link: a `follow`, or an `attachment_text`
    note (spec 0.3). A 0.1 or 0.2 mapping cannot."""
    return any(
        mapped.follow is not None or mapped.transform == "attachment_text"
        for _, _, mapped in located_fields(mapping)
    )


class Linker:
    """Reads the links a decision's record does not hold from one FHIR server
    (`FhirClient`), for one request: `read` for each decision it makes.

    `open` gives the client the first time a link needs it; a `FhirError` from it (a server off
    the allow-list) is every link's reason. `owned`: the linker closes what `open` gave
    (`close`, or leaving a `with`). `fatal` says which failed read is raised rather than kept as
    a reason (SMART's `401`: the launch has ended)."""

    def __init__(
        self,
        open: Callable[[], FhirClient],
        *,
        owned: bool = True,
        fatal: Callable[[FhirError], bool] | None = None,
    ):
        self._open = open
        self._owned = owned
        self._fatal = fatal
        self._client: FhirClient | None = None
        self._refused: ReadReason | None = None
        self._read: dict[str, Read] = {}  # by URL under the base: each link once a request

    @classmethod
    def of(cls, client: FhirClient, *, fatal: Callable[[FhirError], bool] | None = None) -> Linker:
        """A linker reading through `client`, which stays its owner's to close."""
        return cls(lambda: client, owned=False, fatal=fatal)

    def __enter__(self) -> Linker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned and self._client is not None:
            self._client.close()

    def read(self, mapping: FhirMapping, record: Indexed) -> list[Mapping[str, Any]]:
        """Read what `mapping` reaches by a link `record` does not hold, into `record`: each
        field in the mapping's order, a `follow` before the paths it leads to, a field's paths in
        order until one gives a value, as the mapping reads them. The resources read for this
        decision, each once, in order; each link not read is in `record.unread`, with why."""
        if not reads_links(mapping):
            return []
        decision = _Decision(record)
        reading = Reading.of(mapping, record)
        for kind, mapped, scopes in _fields(mapping, record):
            if mapped.follow is None and mapped.transform != "attachment_text":
                continue
            types = followed_types(mapped, kind)
            for scope in scopes:
                if mapped.follow is not None:
                    for target in read_field(mapped, scope, kind, reading).missing:
                        self._link(target, types, decision)
                if mapped.transform == "attachment_text":
                    self._notes(mapped, scope, kind, reading, decision)
        return decision.resources

    def _notes(
        self,
        mapped: FieldMapping,
        scope: Mapping[str, Any],
        kind: str | None,
        reading: Reading,
        decision: _Decision,
    ) -> None:
        """The Binaries a note field's attachments wait on, path by path until one gives the
        field a value. Of renditions of one document, none is read once one reads."""
        record = reading.record
        for path in mapped.tried:
            one = mapped.model_copy(update={"path": path, "paths": None})
            for at, _, pairs in read_field(one, scope, kind, reading).reads:
                alike = renditions(at)
                for container, attachments in _by_resource(pairs):
                    if alike and any(_text(a, container, record) for a in attachments):
                        continue  # one rendition reads: the others are the same document
                    for attachment in attachments:
                        url = waiting_link(attachment, container, record)
                        if url is None:
                            continue
                        self._link(url, {BINARY}, decision)
                        if alike and _text(attachment, container, record):
                            break
            if read_field(one, scope, kind, reading).value is not None:
                break

    def _link(self, target: str, types: set[str], decision: _Decision) -> None:
        """Read the resource `target` names (a link the record does not hold) into the record,
        or keep why it was not read."""
        record = decision.record
        if target in record.linked or target in record.unread:
            return  # met before in this decision
        client = self._client_or_none()
        if client is None:
            record.unread[target] = self._refused or "off_server"
            return
        try:
            url = client.url_of(target)
        except FhirError as error:
            record.unread[target] = error.reason or "off_server"
            return
        named = _under(client, url)
        if named is None:
            record.unread[target] = "not_fetched"  # under the server, but no `Type/id`
            return
        if named[0] not in types:
            record.unread[target] = "unsupported_type"
            return
        held = record.ids.get(named)
        if held is not None:  # an absolute URL to a resource the record holds
            record.link(target, held)
            return
        if url not in decision.urls:
            if len(decision.urls) >= MAX_READS:
                record.unread[target] = "cap"
                return
            decision.urls.add(url)
        found = self._read.get(url)
        if found is None:
            found = self._fetch(client, url, named[0])
            self._read[url] = found
        if isinstance(found, str):
            record.unread[target] = found
            return
        record.link(target, found)
        if all(found is not other for other in decision.resources):
            decision.resources.append(found)

    def _fetch(self, client: FhirClient, url: str, kind: str) -> Read:
        """One resource of type `kind` at `url`, or why it was not read."""
        try:
            most = BINARY_ANSWER * MAX_BYTES if kind == BINARY else MAX_BYTES
            found = client.read_resource(url, max_bytes=most)
        except FhirError as error:
            if self._fatal is not None and self._fatal(error):
                raise
            return error.reason or "network"
        if found.get("resourceType") == "OperationOutcome":
            return "operation_outcome"
        if found.get("resourceType") != kind:
            return "unsupported_type"
        if fhir_structure.blocking(fhir_structure.check(found)):
            return "not_r4"
        if kind == BINARY and decoded_size(found.get("data")) > MAX_BYTES:
            return "too_large"
        return found

    def _client_or_none(self) -> FhirClient | None:
        if self._client is None and self._refused is None:
            try:
                self._client = self._open()
            except FhirError as error:  # the server is not on the allow-list
                self._refused = error.reason or "off_server"
        return self._client


class _Decision:
    """What one decision's linked reads have used: the record, the URLs it read (its share of
    `MAX_READS`) and the resources read for it."""

    def __init__(self, record: Indexed):
        self.record = record
        self.urls: set[str] = set()
        self.resources: list[Mapping[str, Any]] = []


def _under(client: FhirClient, url: str) -> tuple[str, str] | None:
    """The `Type/id` an absolute URL under the client's base names (no query), or None."""
    parts, base = urlsplit(url), urlsplit(client.base)
    if parts.query:
        return None
    return link_target(parts.path[len(base.path) :])


def _fields(
    mapping: FhirMapping, record: Indexed
) -> Iterator[tuple[str | None, FieldMapping, list[Mapping[str, Any]]]]:
    """Each field of a mapping, in its order, with its list's resource type (None for a
    top-level field) and where it is read: the record for a top-level field; for a list's, each
    resource of its type the list can hold (`fhir.listed`), as {its type: it}. A resource the
    list leaves out (a MedicationRequest entered in error) is not an item, so nothing it links to
    is read."""
    for mapped in mapping.fields.values():
        yield None, mapped, [record]
    for spec in mapping.lists.values():
        for kind, source in spec.sources().items():
            resources = record.get(kind) or []
            scopes: list[Mapping[str, Any]] = [{kind: r} for r in resources if listed(source, r)]
            for mapped in source.fields.values():
                yield kind, mapped, scopes


def _by_resource(pairs: list[tuple[Any, Any]]) -> list[tuple[Any, list[Any]]]:
    """A path's values grouped by the resource holding them, in order."""
    groups: dict[int, tuple[Any, list[Any]]] = {}
    for container, value in pairs:
        groups.setdefault(id(container), (container, []))[1].append(value)
    return list(groups.values())


def _text(attachment: Any, container: Any, record: Indexed) -> bool:
    return read_attachment(attachment, container=container, record=record)[0] is not None
