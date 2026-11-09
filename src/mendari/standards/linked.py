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
- is bounded per decision, by purpose: at most `MAX_NOTE_READS` note Binaries
  (`attachment_text`) and `MAX_FOLLOW_READS` followed references (`follow`: the drug behind a
  MedicationRequest, the payer behind a Coverage), so a chart's notes never use up what its
  drugs need; a link past its purpose's budget is `cap`, not read. A note field whose text read
  so far is already longer than the pack's input lets it be (`fhir.text_limits`) reads no more
  of its notes: the decision abstains `too_large` whatever the rest hold, and each note left
  unread for it is `too_large` too. Each read is at most `MAX_BYTES` once decoded (a Binary's
  content, another resource's JSON as it arrives); an answer over the size is `too_large`, and
  stops arriving;
- is bounded per request: at most `MAX_REQUEST_READS` reads sent, its decisions together (a
  link past them is `cap`, not read), and `LINK_SECONDS` spent in them, added up read by read;
  and inside the decision's deadline (`providers/deadline.py`), which `Decision.decide_fhir`
  starts before these reads and its providers share. Each read waits at most what remains of
  `TIMEOUT`, the request's seconds and the deadline, and an answer still arriving once one has
  run out stops; past one, a link is `timeout` and nothing is sent. A read the clocks stopped is
  not kept for the request's later decisions, which have their own deadline; a read that was
  not sent counts against nothing;
- is made once a request: the draft orders of one order hook share what was read, and a link
  met twice in one decision is read once, by its URL under the base whatever its spelling, and
  counts once against its budget. What was read is kept (parsed, at most `BINARY_ANSWER` x
  `MAX_BYTES` an answer) until the request ends (threat-model D14).

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
name and a reason code from the closed set (`errors.ReadReason`). What a decision's linked reads
used (`Linker.used`: its reads by purpose, its budgets, and the HTTP statuses of the reads that
failed with one, as numbers) is its data-coverage record's `links` section.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from mendari.errors import FhirError, ReadReason
from mendari.providers import deadline
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
from mendari.standards.fhir_client import TIMEOUT, FhirClient

MAX_NOTE_READS = 50  # a decision's note Binaries (`attachment_text`)
MAX_FOLLOW_READS = 50  # a decision's followed references (`follow`: a Medication, a payer)
MAX_REQUEST_READS = 100  # reads one request sends, its decisions together
LINK_SECONDS = 60.0  # what one request's linked reads may take, added up read by read
MAX_BYTES = 1024 * 1024  # each, once decoded: a Binary's content, another resource's JSON
# A Binary's answer holds its content as base64 (4 characters for 3 bytes, line breaks allowed)
# inside its JSON: the answer may be this many times `MAX_BYTES`, then the content is measured
# exactly.
BINARY_ANSWER = 2
BINARY = "Binary"

Purpose = Literal["notes", "follows"]  # what a link is read for: its budget in a decision
PURPOSES: tuple[Purpose, ...] = ("notes", "follows")
Read = Mapping[str, Any] | ReadReason  # a link's resource, or why it was not read


def budget(purpose: Purpose) -> int:
    """The links one decision may read for `purpose`."""
    return MAX_NOTE_READS if purpose == "notes" else MAX_FOLLOW_READS


def _now() -> float:
    return time.monotonic()


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
        self._statuses: dict[str, int] = {}  # a failed read's HTTP status, by URL
        self._sent = 0  # reads sent this request (`MAX_REQUEST_READS`)
        self._spent = 0.0  # seconds spent in them, added up (`LINK_SECONDS`)
        self._decisions: list[_Decision] = []

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

    def read(
        self,
        mapping: FhirMapping,
        record: Indexed,
        *,
        limits: Mapping[str, int] | None = None,
    ) -> list[Mapping[str, Any]]:
        """Read what `mapping` reaches by a link `record` does not hold, into `record`: each
        field in the mapping's order, a `follow` before the paths it leads to, a field's paths in
        order until one gives a value, as the mapping reads them. `limits` gives a top-level note
        field the most text the pack's input takes (`fhir.text_limits`): once its notes read are
        longer, the rest are not read. The resources read for this decision, each once, in
        order; each link not read is in `record.unread`, with why."""
        if not reads_links(mapping):
            return []
        decision = _Decision(record)
        self._decisions.append(decision)
        reading = Reading.of(mapping, record)
        for target, kind, mapped, scopes in _fields(mapping, record):
            if mapped.follow is None and mapped.transform != "attachment_text":
                continue
            types = followed_types(mapped, kind)
            limit = None if target is None else (limits or {}).get(target)
            for scope in scopes:
                if mapped.follow is not None:
                    for link in read_field(mapped, scope, kind, reading).missing:
                        self._link(link, types, decision, "follows")
                if mapped.transform == "attachment_text":
                    self._notes(mapped, scope, kind, reading, decision, limit)
        return decision.resources

    def used(self, record: Indexed) -> dict[str, Any] | None:
        """What the linked reads of the decision on `record` used, for its data-coverage record:
        the links it read by purpose (from the request's earlier reads too) and its budgets, and,
        when any of its reads failed with an HTTP status, how many did with each (`statuses`, a
        status number as text). None when it read no link and left none unread, and for a
        record this linker did not read."""
        decision = next((d for d in self._decisions if d.record is record), None)
        if decision is None:
            return None
        read = {purpose: len(decision.urls[purpose]) for purpose in PURPOSES}
        if not any(read.values()) and not decision.left:
            return None
        found: dict[str, Any] = {
            "read": read,
            "budget": {purpose: budget(purpose) for purpose in PURPOSES},
        }
        if decision.statuses:
            found["statuses"] = {str(code): n for code, n in sorted(decision.statuses.items())}
        return found

    def _notes(
        self,
        mapped: FieldMapping,
        scope: Mapping[str, Any],
        kind: str | None,
        reading: Reading,
        decision: _Decision,
        limit: int | None,
    ) -> None:
        """The Binaries a note field's attachments wait on, path by path until one gives the
        field a value. Of renditions of one document, none is read once one reads. With a
        `limit`, once the path's text (what the record held, and each note read, joined as the
        field joins them) is longer, the notes still waiting are `too_large`, not read: the
        decision abstains so (`fhir.overlong`) whatever they hold."""
        record = reading.record
        for path in mapped.tried:
            one = mapped.model_copy(update={"path": path, "paths": None})
            held = read_field(one, scope, kind, reading)
            length = len(held.value) if isinstance(held.value, str) else 0
            for at, _, pairs in held.reads:
                alike = renditions(at)
                for container, attachments in _by_resource(pairs):
                    if alike and any(_text(a, container, record) for a in attachments):
                        continue  # one rendition reads: the others are the same document
                    for attachment in attachments:
                        url = waiting_link(attachment, container, record)
                        if url is None:
                            continue
                        if limit is not None and length > limit:
                            if url not in record.linked and url not in record.unread:
                                record.unread[url] = "too_large"
                                decision.left += 1
                            continue
                        self._link(url, {BINARY}, decision, "notes")
                        text = _text_of(attachment, container, record)
                        if text:
                            length += len(text) + (2 if length else 0)  # joined by a blank line
                            if alike:
                                break
            if read_field(one, scope, kind, reading).value is not None:
                break

    def _link(self, target: str, types: set[str], decision: _Decision, purpose: Purpose) -> None:
        """Read the resource `target` names (a link the record does not hold) into the record,
        or keep why it was not read. `purpose` names the budget it is read against."""
        record = decision.record
        if target in record.linked or target in record.unread:
            return  # met before in this decision
        found = self._resolve(target, types, decision, purpose)
        if isinstance(found, str):
            record.unread[target] = found
            decision.left += 1
            return
        record.link(target, found)
        if all(found is not other for other in decision.resources):
            decision.resources.append(found)

    def _resolve(self, target: str, types: set[str], decision: _Decision, purpose: Purpose) -> Read:
        """What `target` reaches: a resource the record holds by another spelling, one read now
        or earlier in the request, or why it was not read."""
        client = self._client_or_none()
        if client is None:
            return self._refused or "off_server"
        try:
            url = client.url_of(target)
        except FhirError as error:
            return error.reason or "off_server"
        named = _under(client, url)
        if named is None:
            return "not_fetched"  # under the server, but no `Type/id`
        if named[0] not in types:
            return "unsupported_type"
        held = decision.record.ids.get(named)
        if held is not None:  # an absolute URL to a resource the record holds
            return held
        urls = decision.urls[purpose]
        if url not in urls and len(urls) >= budget(purpose):
            return "cap"
        found = self._read.get(url)
        if found is None:
            if self._sent >= MAX_REQUEST_READS:
                return "cap"  # the request's reads are used up: nothing is sent
            wait = self._wait()
            if wait is None:
                return "timeout"  # a clock has run out: nothing is sent
            found, kept = self._fetch(client, url, named[0], wait)
            if kept:
                self._read[url] = found
        urls.add(url)
        if isinstance(found, str) and url in self._statuses:
            decision.statuses[self._statuses[url]] += 1
        return found

    def _fetch(self, client: FhirClient, url: str, kind: str, wait: float) -> tuple[Read, bool]:
        """One resource of type `kind` at `url`, waiting at most `wait` (`_wait`), or why it was
        not read; and whether the request's later decisions may reuse that: not when the clocks
        stopped it (its answer did not arrive before them), since each decision has a deadline
        of its own."""
        started = _now()

        def stop() -> bool:
            return deadline.past() or self._spent + (_now() - started) >= LINK_SECONDS

        self._sent += 1
        try:
            most = BINARY_ANSWER * MAX_BYTES if kind == BINARY else MAX_BYTES
            found = client.read_resource(url, max_bytes=most, timeout=wait, stop=stop)
        except FhirError as error:
            if self._fatal is not None and self._fatal(error):
                raise
            reason: ReadReason = error.reason or "network"
            if error.status is not None:
                self._statuses[url] = error.status
            clocked = reason == "timeout" and (wait < TIMEOUT or self._out_of_time(started))
            return reason, not clocked
        finally:
            self._spent += _now() - started
        if found.get("resourceType") == "OperationOutcome":
            return "operation_outcome", True
        if found.get("resourceType") != kind:
            return "unsupported_type", True
        if fhir_structure.blocking(fhir_structure.check(found)):
            return "not_r4", True
        if kind == BINARY and decoded_size(found.get("data")) > MAX_BYTES:
            return "too_large", True
        return found, True

    def _wait(self) -> float | None:
        """How long the next read may wait: `TIMEOUT`, or what remains of the request's
        `LINK_SECONDS` or the decision's deadline when less; None once either has run out."""
        left = [TIMEOUT, LINK_SECONDS - self._spent]
        remaining = deadline.remaining()
        if remaining is not None:
            left.append(remaining)
        wait = min(left)
        return wait if wait > 0 else None

    def _out_of_time(self, started: float) -> bool:
        return deadline.past() or self._spent + (_now() - started) >= LINK_SECONDS

    def _client_or_none(self) -> FhirClient | None:
        if self._client is None and self._refused is None:
            try:
                self._client = self._open()
            except FhirError as error:  # the server is not on the allow-list
                self._refused = error.reason or "off_server"
        return self._client


class _Decision:
    """What one decision's linked reads have used: the record, the URLs it read by purpose (its
    share of each budget), the resources read for it, how many links it left unread, and the
    HTTP statuses of its reads that failed with one."""

    def __init__(self, record: Indexed):
        self.record = record
        self.urls: dict[Purpose, set[str]] = {purpose: set() for purpose in PURPOSES}
        self.resources: list[Mapping[str, Any]] = []
        self.left = 0
        self.statuses: Counter[int] = Counter()


def _under(client: FhirClient, url: str) -> tuple[str, str] | None:
    """The `Type/id` an absolute URL under the client's base names (no query), or None."""
    parts, base = urlsplit(url), urlsplit(client.base)
    if parts.query:
        return None
    return link_target(parts.path[len(base.path) :])


def _fields(
    mapping: FhirMapping, record: Indexed
) -> Iterator[tuple[str | None, str | None, FieldMapping, list[Mapping[str, Any]]]]:
    """Each field of a mapping, in its order, with its target (a top-level field's; None for a
    list's), its list's resource type (None for a top-level field) and where it is read: the
    record for a top-level field; for a list's, each resource of its type the list can hold
    (`fhir.listed`), as {its type: it}. A resource the list leaves out (a MedicationRequest
    entered in error) is not an item, so nothing it links to is read."""
    for target, mapped in mapping.fields.items():
        yield target, None, mapped, [record]
    for spec in mapping.lists.values():
        for kind, source in spec.sources().items():
            resources = record.get(kind) or []
            scopes: list[Mapping[str, Any]] = [{kind: r} for r in resources if listed(source, r)]
            for mapped in source.fields.values():
                yield None, kind, mapped, scopes


def _by_resource(pairs: list[tuple[Any, Any]]) -> list[tuple[Any, list[Any]]]:
    """A path's values grouped by the resource holding them, in order."""
    groups: dict[int, tuple[Any, list[Any]]] = {}
    for container, value in pairs:
        groups.setdefault(id(container), (container, []))[1].append(value)
    return list(groups.values())


def _text(attachment: Any, container: Any, record: Indexed) -> bool:
    return _text_of(attachment, container, record) is not None


def _text_of(attachment: Any, container: Any, record: Indexed) -> str | None:
    return read_attachment(attachment, container=container, record=record)[0]
