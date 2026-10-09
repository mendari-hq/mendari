# SPDX-License-Identifier: Apache-2.0
"""The generated no-leak sweep of hidden evaluation sets (LP-11g, ADR-0011, threat model I13).

One deployment as the hosted engine would run them: memberships mode, sign-in, SMART Backend
Services, PHI mode `encrypt` and the evaluation-set keys. Two organizations each hold a draft set
of hand-written cases the key holder made on the command line, every content field planted with
a mark of its own (`zq<field>mark<organization>`: the input, the author, the stratum, a language
code, a custom attribute, the author's id and the set's name). The key holder draws the cases,
assigns them to two of the organization's labellers and has the disputed one adjudicated by a
third; the labellers label at the store (LP-11h: no route lets a person label yet), one of them
leaving an assignment open; and the set's agreement and coverage are measured and sealed. Each
organization also holds a set in each state of the lifecycle (LP-11i), made, labelled and
frozen the same way: one left frozen, one made active by a grading read at the store (no route
grades yet), one retired, each read with `evalset show`. What the store makes of all this is a
mark too: each case's id, its salt and each label's, each frozen set's freeze salt and its
unsalted content hash, every sealed token (before and after a rotation: inputs, attributes,
headers, labels, aggregates, freeze records), the hidden-case index's hashes (whole and their
digests alone) and the keys themselves. A text seen
before the last of those marks is made (the first start's answers, the key holder's commands) is
kept and scanned again once every mark exists. Then, with the deployment started again on the
keys as they are by then and shown to have the feature on (health names their ids, conflict 37):

- each organization's principals make every write once with a body it takes, so each answers
  with its success while the sets exist (`served`: a decision of every kind, a review, an
  outcome, a rating, an approval, a hold, an evaluation, a key, a client and its token, an
  invitation accepted, a request to join handled; then, on spare ids, what erases, rotates or
  deletes);
- every mounted route and method (`mounted_routes`, and the OpenAPI document) is called by every
  kind of principal: no one, a member of each role, a labeller with assignments (one still
  open) and one without, keys of each machine role, keys from before Phase 4 of the other
  roles, the scoped keys of tests/test_auth.py, a key of every preset, clients with every scope
  and with each scope, an EHR launch alone and with a person's session, the grading key, and
  the scheduled jobs' secret; then, with the deployment started again in claims mode, by no
  one, the scheduled jobs and a person of each claims role;
- each answer (its body with its NDJSON lines, and every header), each security-log line (the
  access line included) and every log record is scanned, and so is the output of every
  `mendari evalset` command, of `mendari audit verify --evalsets`, of `mendari audit export
  [--administrative]` and of the feedback export, the sealed export file and every table of the
  database, the chain included.

No mark appears anywhere but where conflicts 38, 55 and 56 of notes/lp11-plan.md allow, each as
narrowly as it says (`allowed`): a set's name to its organization's curators and in the key
holder's `evalset` commands, its stratum and language to those curators and in `evalset
coverage` and `evalset show` (no curator route exists yet), and case ids and sealed tokens in
the sealed export file. No word of the pack's labels is in a log, in an `evalset` command's
output or in `audit verify --evalsets` (conflict 36: the planted cases' intended label is one,
and so is every label given), but in the four commands that give the key holder aggregates by
label, `evalset agreement`, `evalset coverage`, `evalset freeze` (each critical label's distinct
positives, conflict 48) and `evalset show` (the stored aggregates) (`AGGREGATES`, conflict 57 as
amended in LP-11i: counts and coefficients, naming no case); each evaluation-set event's payload
holds the keys pinned for its kind and no other, each of its shape, and no label word. The
scanner is shown to see: each mark is found where it lives (the export file, the `evalset_`
tables, what the key holder's own store opens), so a scanner that finds nothing fails. Every
principal is the one it is meant to be (`/api/auth/me`), every URL answers someone with a 2xx or
3xx but the pins of `NOT_ANSWERED`, each with its reason, and every evaluation-set route is
swept: until LP-11j mounts the first there is none, and one that appears without being listed
here fails. SQLite and Postgres; keys are made per test, cases are synthetic."""

from __future__ import annotations

import base64
import functools
import json
import logging
import re
import secrets
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from mendari.cli import main
from mendari.core import audit as audit_module
from mendari.core.audit import MACHINE_ROLES, AuditStore
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import SEALED_COLUMNS, EvalsetStore
from mendari.core.phi import key_id
from mendari.core.registry import resolve_pack
from mendari.server import auth as auth_module
from mendari.server import smart_routes
from mendari.server.api_docs import FHIR_EXAMPLE, FHIR_EXAMPLE_PACK
from mendari.server.app import create_app, mounted_routes
from mendari.server.auth import PERMISSIONS, ROLES, permission_for, person_id
from mendari.server.backend_services import ASSERTION_TYPE, check_jwks, default_scope
from mendari.server.presets import PRESETS
from mendari.server.scopes import DECIDE, OUTCOME, parse, supported
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from tests.conftest import (
    encrypt_mode,
    encrypted_store,
    evalset_keys_for_test,
    evalset_on,
    evalsets_are_on,
    hand_written,
    member,
    message,
    security_lines,
    signed_in,
)
from tests.test_ai_guard_pack import SAFE
from tests.test_audit_fhir import recorded_kinds
from tests.test_auth import CLIENT, IDP, SCOPED_KEYS, FakeIdp, legacy_key
from tests.test_backend_services import Signer, assertion
from tests.test_davinci import COMPLETE, PA, package_request, request
from tests.test_memberships import token_of
from tests.test_pas import COVERAGE, INSURER, PATIENT, REQUESTER, REQUIRED
from tests.test_prior_auth_pack import _order
from tests.test_smart import CLIENT as SMART_CLIENT
from tests.test_smart import DECIDE_BODY, REFERRAL, TENANTS, FakeEhr, full_launch, routed, tenant

APP = "https://mendari.test"
INBOX = "mendari/patient-inbox-router"
GUARD = "mendari/ai-guard"
# Each organization, and the suffix of its marks.
ORGS = {"clinic-a": "alpha", "clinic-b": "bravo"}
CRON = "a-cron-secret-for-the-leak-sweep-0123456789"
CASES = 4  # hand-written cases per set; the key holder removes one
# Each set's blind labelling (LP-11h, `labelled`): the three cases drawn are given to two
# labellers; one labels all three, the other two (one assignment left open); the one case they
# disagree on is adjudicated by a third.
ASSIGNED = 2 * (CASES - 1) + 1
LABELS = (CASES - 1) + 2 + 1
# Each organization's sets in the lifecycle's states (LP-11i, `lifecycle`), of this many cases
# each, every one labelled by two labellers who agree.
STATES = ("frozen", "active", "retired")
LIFE_CASES = 2
LIFE = len(STATES) * LIFE_CASES  # cases, per organization, in its sets of the lifecycle
URGENT = "Crushing chest pain and my left arm is numb"
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"
Route = tuple[str, str]

# -- the marks -------------------------------------------------------------------------------------

# What the key holder writes into a set: a mark each, per organization.
CONTENT = ("input", "author", "stratum", "language", "site", "author_id", "name")
# What the store makes of it: each also a mark (a frozen set's content hash, LP-11i, included).
MADE = ("case_id", "salt", "token", "index_hash", "key", "content_hash")


@dataclass(frozen=True)
class Mark:
    kind: str  # one of CONTENT or MADE
    org: str  # the organization whose set holds it ("": the deployment's keys)
    value: str


def content_marks(org: str) -> dict[str, str]:
    """`org`'s content marks: letters only, so no normal form (`norm/v1`) changes them."""
    return {kind: f"zq{kind.replace('_', '')}mark{ORGS[org]}" for kind in CONTENT}


@dataclass(frozen=True)
class Surface:
    """Where a text was seen: an answer, a log, a command's output, a file or a table."""

    kind: str  # "http", "log", "cli", "export file", "file", "table"
    label: str
    command: str = ""  # cli: `evalset create`, `audit export`, ...
    curate_of: str = ""  # http: the organization a `curate` route answered for (none yet)
    table: str = ""


def allowed(mark: Mark, surface: Surface) -> bool:
    """Where a mark may appear (notes/lp11-plan.md, conflicts 38, 55 and 56), each exception as
    narrow as its conflict says; nowhere else, and never in a chain payload."""
    if surface.kind == "table":  # where the store keeps what it made, sealed or keyed
        return (surface.table, mark.kind) in {
            ("evalset_sets", "token"),
            ("evalset_cases", "case_id"),
            ("evalset_cases", "token"),
            ("evalset_index", "case_id"),
            ("evalset_index", "index_hash"),
            # Blind labelling (LP-11h): who labels which case, and each label sealed.
            ("evalset_assignments", "case_id"),
            ("evalset_labels", "case_id"),
            ("evalset_labels", "token"),
        }
    if surface.kind == "export file":  # 56: the rows as stored, the ciphertext bound to the ids
        return mark.kind in ("case_id", "token")
    if mark.kind == "name":  # 38: curator metadata
        return bool(mark.org and surface.curate_of == mark.org) or (
            surface.kind == "cli" and surface.command.startswith("evalset ")
        )
    if mark.kind in ("stratum", "language"):  # 55: the coverage and agreement aggregates
        return bool(mark.org and surface.curate_of == mark.org) or (
            surface.kind == "cli" and surface.command in ("evalset coverage", "evalset show")
        )
    return False


@functools.cache
def lowered(value: str) -> str:
    return value.lower()


def found(text: str, marks: Iterable[Mark]) -> list[Mark]:
    """The marks a text holds, in any case, JSON-escaped or URL-encoded."""
    seen = text.lower()
    if "\\/" in text:
        seen += "\n" + text.replace("\\/", "/").lower()
    if "%" in text:
        seen += "\n" + unquote(text).lower()
    return [mark for mark in marks if lowered(mark.value) in seen]


# Label values are words from the pack's label list, so no planted mark can stand for one
# (conflict 36): no such word may be in a log, in an `evalset` command's output or in `audit
# verify --evalsets`. Answers and the operator's exports of decisions show labels by design;
# payloads are held to their pinned keys and shapes (`SHAPES`).
_MANIFEST = resolve_pack(INBOX).manifest
LABEL_WORDS = sorted(
    {word for question in _MANIFEST.questions for word in _MANIFEST.labels(question)}
)
LABEL_WORD = re.compile(r"\b(?:" + "|".join(map(re.escape, LABEL_WORDS)) + r")\b", re.IGNORECASE)


# The key holder's aggregates over a set's labels (LP-11h): they count by label, so they name
# the pack's label words, as counts and coefficients beside no case; and so do freezing (each
# critical label's distinct positives, conflict 48) and `show` (the stored aggregates), LP-11i's
# amendment to conflict 57. Every other `evalset` command, `audit verify` and every log stay
# without one.
AGGREGATES = ("evalset agreement", "evalset coverage", "evalset freeze", "evalset show")


def wordless(surface: Surface) -> bool:
    """Whether `surface` may hold no label word."""
    return surface.kind == "log" or (
        surface.kind == "cli"
        and surface.command.startswith(("evalset ", "audit verify"))
        and surface.command not in AGGREGATES
    )


# -- what the chain may say ----------------------------------------------------------------------

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
KEY_ID = re.compile(r"^[0-9a-f]{16}$")
CASE_HASH = re.compile(r"^hmac-sha256:[0-9a-f]{16}:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
USER = re.compile(r"^user:[0-9a-f]{16}$")
GRADER = re.compile(r"^key:[0-9a-f]{12}$")
PACK_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def counts_of(keys: Iterable[str], pattern: re.Pattern[str] | None) -> Any:
    """A mapping of names (each matching `pattern`, or one of `keys`) to counts."""
    names = set(keys)

    def check(value: Any) -> bool:
        return isinstance(value, Mapping) and all(
            (pattern.fullmatch(str(k)) if pattern else k in names)
            and isinstance(n, int)
            and not isinstance(n, bool)
            for k, n in value.items()
        )

    return check


def matches(pattern: re.Pattern[str]) -> Any:
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


SHAPES: dict[str, Any] = {
    "set": matches(UUID),
    "org": lambda value: value in ORGS,
    "case": matches(CASE_HASH),
    "case_ref": matches(SHA256),
    "key_id": matches(KEY_ID),
    "to": matches(KEY_ID),
    "from": counts_of((), KEY_ID),
    "counts": counts_of(SEALED_COLUMNS, None),
    "count": lambda value: isinstance(value, int) and not isinstance(value, bool),
    # Blind labelling (LP-11h): counts and the seed, assignments, labellers by user id, rounds,
    # and a label's commitment (its id and a SHA-256).
    "n": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "seed": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "assignment": matches(UUID),
    "labeller": matches(USER),
    "round": lambda value: value in ("first", "adjudication"),
    "label_ref": lambda value: (
        isinstance(value, Mapping)
        and set(value) == {"id", "sha256"}
        and UUID.fullmatch(str(value["id"])) is not None
        and SHA256.fullmatch(str(value["sha256"])) is not None
    ),
    # The lifecycle (LP-11i): a frozen set's salted commitment, its rotation rule, the grading
    # key a read was for, the pack's digest and the run's number.
    "commitment": matches(SHA256),
    "max_runs": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "max_age_days": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "grader": matches(GRADER),
    "pack_digest": matches(PACK_DIGEST),
    "run": lambda value: isinstance(value, int) and not isinstance(value, bool),
}
# Every kind of evaluation-set event the code chains, with the payload keys it may hold: ids,
# keyed case hashes, commitments, key ids and counts, never a label word or a case's content.
# A kind the code chains without an entry here fails (conflict 36).
PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "evalset.created": frozenset({"set", "org"}),
    "evalset.case_added": frozenset({"set", "org", "case", "case_ref"}),
    "evalset.case_removed": frozenset({"set", "org", "case"}),
    "evalset_index.epoch": frozenset({"key_id"}),
    "evalset.rotated": frozenset({"to", "from", "counts"}),
    "evalset_index.rebuilt": frozenset({"key_id", "count"}),
    "evalset.sampled": frozenset({"set", "org", "n", "seed"}),
    "evalset.assigned": frozenset({"set", "org", "case", "assignment", "labeller", "round"}),
    "evalset.case_read": frozenset({"set", "org", "case", "assignment", "labeller"}),
    "evalset.labelled": frozenset({"set", "org", "case", "label_ref", "labeller", "round"}),
    "evalset.measured": frozenset({"set", "org", "n"}),
    "evalset.frozen": frozenset({"set", "org", "commitment", "n", "max_runs", "max_age_days"}),
    "evalset.activated": frozenset({"set", "org"}),
    "evalset.graded": frozenset({"set", "org", "commitment", "grader", "pack_digest", "n", "run"}),
    "evalset.retired": frozenset({"set", "org", "commitment"}),
}
EVALSET_KINDS = ("evalset.", "evalset_index.")

# -- the routes ----------------------------------------------------------------------------------

# Evaluation-set routes: those needing `label`, `curate` or `grade`, or under a path that names
# hidden sets. LP-11j and later add theirs here, each with the ids its placeholders are filled
# with (`fill`); one mounted and not listed fails. None is mounted yet.
EVALSET_PERMISSIONS = {"label", "curate", "grade"}
EVALSET_PATH = re.compile(r"evalset|labell|grading|hidden")
EVALSET_ROUTES: dict[Route, str] = {}
# Routes no principal here is answered on with a 2xx or 3xx, in either mode, each with why: they
# are still called and scanned. (A write the sweep sends an empty body is answered by `served`.)
NOT_ANSWERED: dict[Route, str] = {
    ("GET", "/auth/callback"): "a sign-in's return: needs the identity provider's code "
    "(tests/test_auth.py)",
    ("GET", "/smart/callback"): "a launch's return: needs the EHR's code (tests/test_smart.py)",
    ("GET", "/api/providers/jev/models"): "Jev's model list: needs Jev set up",
}
# Calls that end the caller's session: made last.
LAST = (("POST", "/auth/logout"), ("POST", "/api/smart/logout"))
# Query strings the reads need to answer.
QUERIES: dict[str, dict[str, str]] = {
    "/api/audit/administrative": {"limit": "500"},  # every event of the organization's
    "/api/monitoring": {"pack_id": INBOX},
    "/api/smart/focus": {"pack_id": REFERRAL},
}
UNKNOWN = "x0leaksweep"  # an id of nothing: what a call that would delete or rotate is given
CLIENT_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SIGNER = Signer("RS384", "k", CLIENT_KEY)  # what signs a Backend Services client's assertion
# What the seed and `served` send.
REVIEW = {"final_label": "nurse", "reviewer": "x", "note": "a review note"}
RATING = {"rating": "down", "note": "a rating note"}
APPROVAL = {"pack_id": GUARD, "reference": "minutes", "days": 30}
HOLD = {"pack_id": GUARD, "reason": "a drill"}
RUN = {"dataset": "test", "limit": 2}

# -- the world -----------------------------------------------------------------------------------


@dataclass
class Org:
    name: str
    ehr: FakeEhr
    marks: dict[str, str]
    people: dict[str, dict[str, str]] = field(default_factory=dict)  # role: headers (members)
    ids: dict[str, str] = field(default_factory=dict)  # what fills a route's placeholders
    principals: dict[str, dict[str, str]] = field(default_factory=dict)  # name: headers
    set_id: str = ""
    case_hashes: list[str] = field(default_factory=list)  # as `evalset add` printed them
    labellers: list[str] = field(default_factory=list)  # user ids, of those given assignments
    lifecycle: dict[str, str] = field(default_factory=dict)  # state: a set left in it (LP-11i)

    @property
    def sets(self) -> list[str]:
        """Every set of the organization's: its first, then those of the lifecycle."""
        return [self.set_id, *self.lifecycle.values()]


@dataclass
class World:
    db: str
    tmp: Path
    store: AuditStore
    capsys: pytest.CaptureFixture[str]
    orgs: dict[str, Org]
    keys: EvalsetKeys
    client: TestClient | None = None
    marks: list[Mark] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    answered: set[Route] = field(default_factory=set)
    called: set[Route] = field(default_factory=set)
    requests: int = 0
    commands: set[str] = field(default_factory=set)
    swept: set[tuple[str, Route]] = field(default_factory=set)  # (principal, route) called
    complete: bool = False  # every mark made (`marked`)
    early: list[tuple[Surface, str]] = field(default_factory=list)  # seen before that
    _seen: dict[tuple[int, int, int], list[Mark]] = field(default_factory=dict)

    def mark(self, kind: str, org: str, *values: str) -> None:
        """Take `values` as marks of `kind` (each once)."""
        assert not self.complete, "every mark is made before `marked`"
        for value in values:
            if value and Mark(kind, org, value) not in self.marks:
                self.marks.append(Mark(kind, org, value))

    def marked(self) -> None:
        """Every mark is made: each text seen before is scanned again, against all of them (a
        command can print what it makes before the sweep takes it as a mark)."""
        self.complete = True
        early, self.early = self.early, []
        for surface, text in early:
            self.scan(surface, text)

    def scan(self, surface: Surface, text: str) -> list[Mark]:
        """Record every mark `text` shows where it may not, and any label word where none may
        be (`wordless`); the marks it shows. Kept to be scanned again until every mark is made."""
        if not self.complete:
            self.early.append((surface, text))
        memo = (len(self.marks), len(text), hash(text))
        shown = self._seen.get(memo)
        if shown is None:
            shown = self._seen[memo] = found(text, self.marks)
        for mark in shown:
            if not allowed(mark, surface):
                self.problems.append(f"{surface.label}: {mark.kind} of {mark.org or 'keys'}")
        if wordless(surface):
            for word in sorted({word.lower() for word in LABEL_WORD.findall(text)}):
                self.problems.append(f"{surface.label}: the label word {word}")
        return shown

    def drain(self, label: str) -> None:
        """Scan what has been printed since: the security log's lines, and anything else."""
        out, err = self.capsys.readouterr()
        lines = security_lines(out)
        assert all(line.get("event") for line in lines)
        self.scan(Surface("log", f"log after {label}"), out + "\n" + err)

    def call(
        self,
        credential: Mapping[str, str],
        method: str,
        url: str,
        *,
        route: Route | None = None,
        name: str = "",
        curate_of: str = "",
        **options: Any,
    ) -> Any:  # TestClient's response
        """One request as the principal whose headers are `credential`, with no cookie of an
        earlier one: its answer with every header and what it logged, scanned. `route` is
        answered when the call is (a 2xx or 3xx)."""
        assert self.client is not None
        self.client.cookies.clear()
        sent = {**credential, **options.pop("headers", {})}
        answer = self.client.request(method, url, headers=sent, follow_redirects=False, **options)
        self.requests += 1
        label = f"{method} {url} as {name or 'seed'} ({answer.status_code})"
        headers = "\n".join(f"{k}: {unquote(v)}" for k, v in answer.headers.multi_items())
        self.scan(Surface("http", label, curate_of=curate_of), answer.text + "\n" + headers)
        self.drain(label)
        if route is not None:
            self.called.add(route)
            if answer.status_code < 400:
                self.answered.add(route)
        return answer

    def run(self, *argv: str) -> tuple[int, str, str]:
        """A command of the key holder's, on the deployment's database: its output scanned."""
        self.drain("the calls before a command")
        code = main([*argv, "--db", self.db])
        out, err = self.capsys.readouterr()
        command = " ".join(argv[:2])
        self.commands.add(command)
        self.scan(Surface("cli", f"mendari {command} ({code})", command=command), out + err)
        return code, out, err


def deployment(monkeypatch: pytest.MonkeyPatch, orgs: Mapping[str, Org]) -> EvalsetKeys:
    """The settings of a deployment with the most routes mounted, its identity provider and EHRs
    faked (nothing leaves the machine), and the evaluation-set keys: those keys."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    encrypt_mode(monkeypatch)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", APP)
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", SMART_CLIENT)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", TENANTS)
    monkeypatch.setenv(
        "MENDARI_SMART_EHRS", ",".join(f"{org.ehr.iss}={name}:decide" for name, org in orgs.items())
    )
    monkeypatch.setenv("CRON_SECRET", CRON)
    # A throwaway signing key, so the daily checkpoint is signed and published.
    monkeypatch.setenv("MENDARI_CHECKPOINT_KEY", b64(secrets.token_bytes(32)))
    idp = httpx.MockTransport(FakeIdp())
    monkeypatch.setattr(auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=idp, **kw))
    ehrs = routed(*(org.ehr for org in orgs.values()))
    monkeypatch.setattr(smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=ehrs))
    monkeypatch.setattr(smart_routes, "FhirClient", functools.partial(FhirClient, transport=ehrs))
    key, index = evalset_on(monkeypatch)
    return EvalsetKeys(key, index)


def b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


def keyed(monkeypatch: pytest.MonkeyPatch, keys: EvalsetKeys) -> None:
    """The deployment's (and so the command line's) evaluation-set keys are now `keys`."""
    monkeypatch.setenv("MENDARI_EVALSET_KEY", b64(keys.key))
    monkeypatch.setenv("MENDARI_EVALSET_INDEX_KEY", b64(keys.index_key))
    if keys.read_keys:
        monkeypatch.setenv("MENDARI_EVALSET_READ_KEYS", ",".join(b64(k) for k in keys.read_keys))
    else:
        monkeypatch.delenv("MENDARI_EVALSET_READ_KEYS", raising=False)


def planted(org: Org) -> list[dict[str, Any]]:
    """`org`'s hand-written cases, each holding every content mark but the set's name."""
    marks = org.marks
    return [
        {
            **hand_written(
                f"{marks['input']} {number}: my knee aches after the long walk",
                author=marks["author"],
                stratum=marks["stratum"],
                language=marks["language"],
                site=marks["site"],
            ),
            "id": f"{marks['author_id']}{number}",
            "labels": {"urgency": "routine"},
        }
        for number in range(CASES)
    ]


def lines_file(path: Path, records: Sequence[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def rows(store: AuditStore, table: str) -> list[dict[str, Any]]:
    with store._db.session(exclusive=False) as db:
        return db.all(f"SELECT * FROM {table}")


def tables() -> list[str]:
    return re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", audit_module._TABLES)


# -- the deployment's own data, through its API ----------------------------------------------------


def people(world: World, org: Org) -> None:
    """`org`'s members of each role (its record made with the first), and the three labellers
    its hidden set's cases are assigned to (LP-11h)."""
    for role in ROLES:
        org.people[role] = member(
            world.store,
            org=org.name,
            roles=[role],
            sub=f"{role}-{org.name}",
            email=f"{role}@{org.name}.example.org",
        )
    for number in range(3):
        sub = f"labeller-{number}-{org.name}"
        member(world.store, org=org.name, roles=["labeller"], sub=sub)
        org.labellers.append(person_id(IDP, sub))


def seed(world: World, org: Org) -> None:
    """`org`'s work through the API: decisions (one waiting for review, then reviewed with a
    note), a rating with a note, an approval, a hold, an evaluation, an invitation and a request
    to join; what fills the routes' placeholders recorded."""
    admin, reviewer = org.people["admin"], org.people["reviewer"]
    made = world.store.create_api_key(name="seed", role="service", org=org.name, created_by="t")
    org.ids["key"] = made["id"]
    service = {"authorization": f"Bearer {made['key']}"}
    decided = {
        words: world.call(service, "POST", f"/api/packs/{INBOX}/decide", json=message(words)).json()
        for words in (URGENT, ROUTINE)
    }
    org.ids["decision"] = decided[URGENT]["id"]
    world.call(reviewer, "POST", f"/api/review/{org.ids['decision']}", json=REVIEW)
    world.call(reviewer, "POST", f"/api/audit/{decided[ROUTINE]['id']}/feedback", json=RATING)
    governance = org.people["governance"]
    approved = world.call(governance, "POST", "/api/governance", json=APPROVAL)
    org.ids["approval"] = approved.json()["id"]
    org.ids["hold"] = world.call(governance, "POST", "/api/holds", json=HOLD).json()["id"]
    assert world.call(admin, "POST", f"/api/packs/{INBOX}/evals", json=RUN).status_code == 200
    invitation = {"email": f"joiner@{org.name}.example.org", "roles": ["viewer"]}
    invited = world.call(admin, "POST", "/api/auth/invitations", json=invitation)
    org.ids["invitation"] = invited.json()["id"]
    asker = member(world.store, org=None, sub=f"asker-{org.name}")
    asked = world.call(asker, "POST", "/api/auth/access-requests", json={"org": org.name})
    assert asked.status_code == 202, asked.text
    requests = world.call(admin, "GET", "/api/auth/access-requests").json()
    org.ids["request"] = requests[0]["id"]
    org.ids["membership"] = world.call(admin, "GET", "/api/auth/members").json()[0]["id"]


# -- the key holder's sets, from the command line --------------------------------------------------


def hidden_sets(world: World, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each organization's draft set made and filled by the key holder, a case removed, refused
    files tried; the sets listed, verified and checked; the key rotated by the runbook's steps
    (the next key read first, swapped in, the sets re-sealed, the old key dropped) and the
    hidden-case index rebuilt under a new index key; then exported. What the store made is taken
    as marks as it is made, and every command's output is scanned against every mark once the
    last is made (`World.marked`). The export file."""
    first = world.keys
    for org in world.orgs.values():
        code, out, _ = world.run(
            "evalset", "create", INBOX, "--org", org.name, "--name", org.marks["name"]
        )
        assert code == 0
        org.set_id = json.loads(out)["set"]
        path = lines_file(world.tmp / "hidden" / f"{org.name}.jsonl", planted(org))
        code, out, _ = world.run("evalset", "add", org.set_id, str(path))
        assert code == 0
        org.case_hashes = [case["case"] for case in json.loads(out)["cases"]]
    made(world, before_rotation=True)
    for org in world.orgs.values():  # refused, all or none, naming a line only
        records = planted(org)[:2]
        records[1] = {**records[1], "system": {"outcome": "AUTO"}}
        code, _, err = world.run(
            "evalset",
            "add",
            org.set_id,
            str(lines_file(world.tmp / "hidden" / "bad.jsonl", records)),
        )
        assert code == 1 and "line 2: a `system` block is refused" in err
        garbled = world.tmp / "hidden" / "garbled.jsonl"
        garbled.write_text(json.dumps(planted(org)[0]) + "\n{" + org.marks["input"] + "\n")
        code, _, err = world.run("evalset", "add", org.set_id, str(garbled))
        assert code == 1 and err.strip() == "error: line 2: not JSON"
        code, _, _ = world.run("evalset", "remove-case", org.set_id, org.case_hashes.pop(0))
        assert code == 0
        assert world.run("evalset", "list", "--org", org.name)[0] == 0
    assert world.run("evalset", "list")[0] == 0
    for org in world.orgs.values():
        labelled(world, org)
    for org in world.orgs.values():
        lifecycle(world, org)
    made(world, before_rotation=True)  # the labels, re-sealed attributes, aggregates, freezes
    code, out, _ = world.run("audit", "verify", "--evalsets")
    verified = json.loads(out)["evalsets"]
    assert code == 0 and verified["checked"] == 2 * (CASES - 1 + LIFE)
    assert (verified["assignments"], verified["labels"]) == (
        2 * (ASSIGNED + 2 * LIFE),
        2 * (LABELS + 2 * LIFE),
    )
    assert verified["frozen"] == 2 * len(STATES)
    assert world.run("evalset", "rotate", "--check")[0] == 0
    # The next key: read first (a deployment starts with it), swapped in, the sets re-sealed.
    nxt = evalset_keys_for_test().key
    with encrypted_store(world.db) as store:
        store.evalset_ready(EvalsetKeys(first.key, first.index_key, (nxt,)))
    keyed(monkeypatch, EvalsetKeys(nxt, first.index_key, (first.key,)))
    assert world.run("evalset", "rotate", "--to", key_id(nxt))[0] == 0
    assert world.run("evalset", "rotate", "--check")[0] == 0
    keyed(monkeypatch, EvalsetKeys(nxt, first.index_key))  # the read key dropped
    # A new index key, and the index rebuilt under it.
    index = evalset_keys_for_test().key
    world.keys = EvalsetKeys(nxt, index)
    keyed(monkeypatch, world.keys)
    assert world.run("evalset", "rotate", "--check")[0] == 1  # the index waits for reindex
    assert world.run("evalset", "reindex")[0] == 0
    assert world.run("evalset", "rotate", "--check")[0] == 0
    code, out, _ = world.run("audit", "verify", "--evalsets")
    assert code == 0 and json.loads(out)["ok"]
    every_key = (first.key, first.index_key, nxt, index)
    world.mark("key", "", *(text for k in every_key for text in (b64(k), k.hex())))
    made(world, before_rotation=False)
    world.marked()  # what was printed before its mark was made, scanned again
    exported = world.tmp / "backup" / "evalsets.jsonl"
    exported.parent.mkdir()
    for name in ("MENDARI_EVALSET_KEY", "MENDARI_EVALSET_INDEX_KEY"):
        monkeypatch.delenv(name)  # the export needs no key
    assert world.run("evalset", "export", "--out", str(exported))[0] == 0
    keyed(monkeypatch, world.keys)
    return exported


def labelled(world: World, org: Org) -> None:
    """`org`'s set labelled blind (LP-11h): its cases drawn and given to two labellers by the
    key holder's commands; at the store (no route yet), the first labels every case and the
    second all but one, which stays open, disagreeing on one; the key holder has that one
    adjudicated by the third, who splits it three ways (it stays unresolved); then the set's
    progress, agreement and coverage, as text and JSON. Every output is scanned; none names a
    case."""
    first, second, third = org.labellers
    code, out, _ = world.run("evalset", "sample", org.set_id, "--per-stratum", str(CASES))
    assert code == 0 and json.loads(out)["drawn"] == CASES - 1
    pool = ("--labeller", first, "--labeller", second)
    code, out, _ = world.run("evalset", "assign", org.set_id, *pool)
    assert code == 0 and json.loads(out)["assigned"] == 2 * (CASES - 1)
    hidden = EvalsetStore(world.store, world.keys)

    def answer(user: str, destination: str) -> None:
        shown = hidden.next_case(user, org=org.name)
        assert shown is not None and set(shown) == {"assignment", "questions", "input"}
        labels = {"destination": destination, "urgency": "routine"}
        assert set(hidden.record_label(shown["assignment"], user, labels, org=org.name)) == {
            "remaining"
        }

    for _ in range(CASES - 1):
        answer(first, "nurse")
    for destination in ("nurse", "physician"):
        answer(second, destination)
    assert hidden.next_case(second, org=org.name) is not None  # one assignment left open
    code, out, _ = world.run("evalset", "adjudicate", org.set_id, "--labeller", third)
    assert code == 0 and json.loads(out)["assigned"] == 1
    answer(third, "billing")
    for argv in (
        ("progress",),
        ("agreement", "--resamples", "50"),
        ("agreement", "--json", "--resamples", "50"),
        ("coverage",),
        ("coverage", "--json"),
    ):
        code, out, _ = world.run("evalset", argv[0], org.set_id, *argv[1:])
        assert code == 0 and not any(case in out for case in org.case_hashes), argv
        if "--json" in argv:  # what `AGGREGATES` lets through: counts by label, words and all
            assert LABEL_WORD.search(out), argv


def lifecycle(world: World, org: Org) -> None:
    """`org`'s sets in each state of the lifecycle (LP-11i): each made and filled with `org`'s
    planted cases by the key holder's commands, drawn and assigned to the two labellers who have
    no open assignment (the second keeps the first set's open), labelled at the store (they
    agree), frozen (`evalset freeze`); then one is left frozen, one made active by a grading read
    at the store with `org`'s grading key (no route grades yet; the run sees the cases in the
    process and answers a count), and one read and retired (`evalset retire`). Each is read
    with `evalset show`; every output is scanned, and none names a case."""
    first, _, third = org.labellers
    hidden = EvalsetStore(world.store, world.keys)
    made = world.store.create_grading_key(
        org=org.name, created_by="operator", reason="the leak sweep's grading reads"
    )
    grader, digest = f"key:{made['id']}", resolve_pack(INBOX).digest
    for state in STATES:
        code, out, _ = world.run(
            "evalset", "create", INBOX, "--org", org.name, "--name", org.marks["name"]
        )
        assert code == 0
        set_id = org.lifecycle[state] = json.loads(out)["set"]
        path = world.tmp / "hidden" / f"{org.name}-{state}.jsonl"
        code, out, _ = world.run(
            "evalset", "add", set_id, str(lines_file(path, planted(org)[:LIFE_CASES]))
        )
        assert code == 0
        hashes = [case["case"] for case in json.loads(out)["cases"]]
        org.case_hashes += hashes
        code, _, _ = world.run("evalset", "sample", set_id, "--per-stratum", str(LIFE_CASES))
        assert code == 0
        pool = ("--labeller", first, "--labeller", third)
        assert world.run("evalset", "assign", set_id, *pool)[0] == 0
        for user in (first, third):
            while (shown := hidden.next_case(user, org=org.name)) is not None:
                labels = {"destination": "nurse", "urgency": "routine"}
                hidden.record_label(shown["assignment"], user, labels, org=org.name)
        freeze = ("--max-runs", "3", "--max-age-days", "30")
        code, out, _ = world.run("evalset", "freeze", set_id, *freeze)
        assert code == 0 and json.loads(out)["state"] == "frozen"
        assert LABEL_WORD.search(out)  # what `AGGREGATES` lets through: the critical labels
        if state != "frozen":
            read = hidden.grade_read(set_id, grader, digest, lambda data: len(data.records))
            assert read == LIFE_CASES
        if state == "retired":
            assert world.run("evalset", "retire", set_id)[0] == 0
        code, out, _ = world.run("evalset", "show", set_id)
        assert code == 0 and json.loads(out)["state"] == state
        assert not any(case in out for case in hashes)


def made(world: World, *, before_rotation: bool) -> None:
    """What the store made of each organization's cases, as marks: case ids, sealed tokens and
    index hashes as stored (before the rotation, the removed case's too), and each case's and
    each label's salt and each frozen set's freeze salt and content hash (LP-11i), opened with
    the key holder's own keys."""
    org_of = {set_id: org.name for org in world.orgs.values() for set_id in org.sets}
    keys = world.keys
    for table, sealed in SEALED_COLUMNS.items():
        for row in rows(world.store, table):
            org = org_of[str(row[sealed.set_id])]
            tokens = [str(row[column]) for column in sealed.columns if row[column] is not None]
            # The whole token and its ciphertext alone (`v2:<key id>:<ciphertext>`).
            world.mark("token", org, *(part for t in tokens for part in (t, t.split(":", 2)[2])))
            if table == "evalset_cases":
                world.mark("case_id", org, str(row["id"]))
                if before_rotation:
                    body, _ = keys.open(
                        "attributes", str(row["attributes"]), str(row["set_id"]), str(row["id"])
                    )
                    salt = base64.b64decode(body["salt"])
                    world.mark("salt", org, body["salt"], salt.hex())
            if table == "evalset_labels" and before_rotation:
                bound = (str(row["set_id"]), str(row["case_id"]), str(row["id"]))
                body, _ = keys.open("label", str(row["body"]), *bound)
                salt = base64.b64decode(body["salt"])
                world.mark("salt", org, body["salt"], salt.hex())
            if table == "evalset_sets" and before_rotation and row["freeze_record"] is not None:
                record, _ = keys.open("freeze", str(row["freeze_record"]), str(row["id"]))
                salt = base64.b64decode(record["salt"])
                world.mark("salt", org, record["salt"], salt.hex())
                world.mark("content_hash", org, record["content_hash"])
    for row in rows(world.store, "evalset_index"):  # `hmac-sha256:<key id>:<digest>`, and alone
        hashed = str(row["hash"])
        world.mark("index_hash", org_of[str(row["set_id"])], hashed, hashed.rsplit(":", 1)[1])


# -- every kind of principal -----------------------------------------------------------------------


def principals(world: World, org: Org) -> None:
    """`org`'s principals in memberships mode, by name (the plan's list): members of each role
    and a labeller; keys of each machine role, keys from before Phase 4 of the other roles, the
    scoped keys and a key of each preset; Backend Services clients with everything their role
    may use and with each scope; an EHR launch alone and with a member's session; the grading
    key. (Its people of each claims role are `claims_people`, in claims mode.)"""
    found = org.principals
    store, name = world.store, org.name
    for role in ROLES:
        found[f"member {role}"] = org.people[role]
    found["member labeller"] = member(store, org=name, roles=["labeller"], sub=f"labeller-{name}")
    # A labeller with assignments (LP-11h): some answered, one still open (`labelled`).
    found["member labeller with assignments"] = member(
        store, org=name, roles=["labeller"], sub=f"labeller-1-{name}"
    )

    def key(label: str, role: str, scopes: str | None = None, preset: str | None = None) -> None:
        made = store.create_api_key(
            name=label, role=role, org=name, created_by="t", scopes=scopes, preset=preset
        )
        found[f"key {label}"] = {"authorization": f"Bearer {made['key']}"}

    for role in MACHINE_ROLES:
        key(role, role)
    for role in sorted(set(PERMISSIONS) - {*MACHINE_ROLES, "grading"}):
        found[f"legacy key {role}"] = {
            "authorization": f"Bearer {legacy_key(world.db, role, name)}"
        }
    for role, scope in SCOPED_KEYS:
        key(f"{role} {scope}", role, parse(scope, permissions=PERMISSIONS[role]).scope)
    for preset in PRESETS.values():
        narrowed = preset.scope and parse(preset.scope, permissions=PERMISSIONS[preset.role]).scope
        key(f"preset {preset.id}", preset.role, narrowed, preset.id)
    public = json.loads(RSAAlgorithm.to_jwk(CLIENT_KEY.public_key()))
    jwks = check_jwks({"keys": [{**public, "kid": "k"}]})
    registered = [(role, f"all {role}", default_scope(role)) for role in MACHINE_ROLES]
    registered += [
        ("service" if scope in (DECIDE, OUTCOME) else "viewer", scope, scope)
        for scope in supported()
    ]
    for role, label, scope in registered:
        granted = parse(scope, permissions=PERMISSIONS[role]).scope
        client = store.register_client(
            name=label, role=role, org=name, created_by="t", scopes=granted, jwks=jwks
        )
        token, _ = store.issue_token(client["client_id"], granted, 3600)
        found[f"client {label}"] = {"authorization": f"Bearer {token}"}
        org.ids.setdefault("client", client["client_id"])
    grading = store.create_grading_key(org=name, created_by="operator", reason="the leak sweep")
    found["grading key"] = {"authorization": f"Bearer {grading['key']}"}
    assert world.client is not None
    launcher = TestClient(world.client.app, base_url=APP)  # a browser of its own
    assert full_launch(launcher, org.ehr).status_code == 302
    cookie = "; ".join(f"{k}={v}" for k, v in launcher.cookies.items())
    found["ehr launch"] = {"cookie": cookie}
    person = org.people["reviewer"]["cookie"]
    found["ehr launch with a session"] = {"cookie": f"{cookie}; {person}"}


# -- the sweep -------------------------------------------------------------------------------------


def fill(method: str, path: str, org: Org) -> str:
    """A mounted route's URL as `org`'s principals call it: each placeholder filled with an id of
    `org`'s (an unknown one where a call would delete or rotate), and the query a read needs. A
    placeholder not named here fails: a new route is classified before it ships."""
    destructive = method == "DELETE" or path.endswith("/rotate")
    namespace, name = INBOX.split("/")
    by_type = {
        "Task": org.ids["decision"],
        "AuditEvent": org.ids["auditevent"],
        "Provenance": org.ids["provenance"],
        "CodeSystem": "audit-event",
        "NamingSystem": "decision-id",
        "OperationDefinition": "decide",
    }
    fills = {
        "namespace": namespace,
        "name": name,
        "service_id": "mendari.prior-auth-preflight",
        "version_id": "1",
        "resource_id": by_type.get(path.split("/")[2], "") if path.startswith("/fhir/") else "",
        **{
            placeholder: UNKNOWN if destructive else org.ids[kind]
            for placeholder, kind in (
                ("decision_id", "decision"),
                ("key_id", "key"),
                ("client_id", "client"),
                ("membership_id", "membership"),
                ("invitation_id", "invitation"),
                ("request_id", "request"),
                ("approval_id", "approval"),
                ("hold_id", "hold"),
            )
        },
        # Evaluation-set placeholders (none yet: LP-11j and later fill theirs here).
    }
    url = re.sub(r"\{(\w+)\}", lambda match: fills.get(match[1]) or match[0], path)
    assert "{" not in url, f"{method} {path}: a placeholder no rule here fills"
    query = dict(QUERIES.get(path, {}))
    if path == "/smart/launch":
        query = {"iss": org.ehr.iss, "launch": "xyz", "pack": REFERRAL}
    return f"{url}?{urlencode(query)}" if query else url


def options(method: str, path: str) -> dict[str, Any]:
    """What a call sends: a FHIR search's empty form, or an empty JSON body to a write."""
    if path.endswith("/_search"):
        return {"content": "", "headers": {"content-type": "application/x-www-form-urlencoded"}}
    return {} if method in ("GET", "HEAD") else {"json": {}}


Caller = tuple[Org, str, Mapping[str, str]]  # whose ids it calls on, its name, its headers


def callers(world: World, mode: str, people: Iterable[Caller]) -> list[Caller]:
    """Who the sweep calls as in `mode`: no one and the scheduled jobs, then `people`."""
    first = next(iter(world.orgs.values()))
    named = {"no one": {}, "the scheduled jobs": {"authorization": f"Bearer {CRON}"}}
    return [(first, f"{name} ({mode})", headers) for name, headers in named.items()] + [*people]


def claims_people(world: World) -> list[Caller]:
    """A person of each claims role in each organization (claims mode: ADR-0008)."""
    return [
        (org, f"{org.name} claims {role}", signed_in(world.store, role=role, org=org.name))
        for org in world.orgs.values()
        for role in ROLES
    ]


def sweep(
    world: World,
    routes: Sequence[Route],
    called: Sequence[Caller],
    *,
    anonymous: set[str],
    curators: set[str],
) -> None:
    """Every route as every caller (each on its organization's ids); the calls that end a
    session last. Each caller is first shown to be who it is meant to be: those named in
    `anonymous` hold no principal and every other one does (so a credential that falls back to
    no one fails), and those holding `curate` are `curators`. A `curate` route's answer to a
    principal who holds it (an administrator who signed in) is its organization's curators'
    (conflicts 38 and 55)."""
    held = {
        name: world.call(headers, "GET", "/api/auth/me", name=name).json().get("principal", {})
        for _, name, headers in called
    }
    assert {name for name, principal in held.items() if not principal.get("kind")} == anonymous
    assert {
        name for name, principal in held.items() if "curate" in principal.get("permissions", ())
    } == curators
    ordered = [r for r in routes if r not in LAST] + [r for r in routes if r in LAST]
    for method, path in ordered:
        curate = permission_for(method, path) == "curate"
        for org, name, headers in called:
            world.call(
                headers,
                method,
                fill(method, path, org),
                route=(method, path),
                name=name,
                curate_of=org.name if curate and name in curators else "",
                **options(method, path),
            )
            world.swept.add((name, (method, path)))


def served(world: World, org: Org) -> None:
    """Each write `org`'s principals make, once, with a body it takes, so its success answer
    is scanned while the hidden sets exist (the sweep sends writes an empty body, which most
    refuse unread): a decision of every kind (the API's, FHIR's, the guard's, CDS Hooks', PAS's,
    DTR's and an EHR launch's), a review, an outcome and a rating, an approval and a hold, an
    evaluation, a key, a client and its token, an invitation made and accepted, a request to
    join made and handled, an organization chosen, a member's roles changed; then, on spare ids
    made here, what erases, rotates or deletes (tests/test_two_organizations.py's phase 2)."""
    m = org.name
    admin, reviewer, governance = (org.people[role] for role in ("admin", "reviewer", "governance"))
    service, launch = org.principals["key service"], org.principals["ehr launch"]

    def ok(who: Mapping[str, str], route: Route, url: str, expect: int, **sent: Any) -> Any:
        answer = world.call(who, route[0], url, route=route, name=f"{m} served", **sent)
        assert answer.status_code == expect, (route, answer.status_code, answer.text)
        return answer

    decide = ("POST", "/api/packs/{namespace}/{name}/decide")
    decided = {
        kind: ok(service, decide, f"/api/packs/{INBOX}/decide", 200, json=message(words)).json()
        for kind, words in (("pending", URGENT), ("routine", ROUTINE), ("spare", ROUTINE))
    }
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "pack", "valueString": FHIR_EXAMPLE_PACK},
            {"name": "data", "resource": FHIR_EXAMPLE},
        ],
    }
    ok(service, ("POST", "/fhir/$decide"), "/fhir/$decide", 200, json=parameters)
    packed = f"/api/packs/{FHIR_EXAMPLE_PACK}/fhir/decide"
    ok(
        reviewer,
        ("POST", "/api/packs/{namespace}/{name}/fhir/decide"),
        packed,
        200,
        json=FHIR_EXAMPLE,
    )
    guard = ("POST", "/api/packs/{namespace}/{name}/guard")
    ok(service, guard, f"/api/packs/{GUARD}/guard", 200, json=SAFE)
    hook = ok(
        service,
        ("POST", "/cds-services/{service_id}"),
        "/cds-services/mendari.prior-auth-preflight",
        200,
        json=request("acme", COMPLETE, _order("mri", "FX101")),
    ).json()
    pas = {
        "decision_id": hook["cards"][0]["extension"]["mendari.decision_id"],
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
        "service_type": "62",
        "place_of_service": "22",
        **REQUIRED,
    }
    ok(
        service,
        ("POST", "/api/packs/{namespace}/{name}/pas"),
        f"/api/packs/{PA}/pas",
        200,
        json=pas,
    )
    ordered = {"name": "order", "resource": hook["systemActions"][0]["resource"]}
    package = "/fhir/Questionnaire/$questionnaire-package"
    ok(service, ("POST", package), package, 200, json=package_request(ordered))
    launch_id = world.call(launch, "GET", "/api/smart/session").json()["launch_id"]
    pinned = {"x-mendari-launch": str(launch_id)}
    smart = ("POST", "/api/smart/decide")
    ok(launch, smart, "/api/smart/decide", 200, json=DECIDE_BODY, headers=pinned)
    # A review, an outcome and a rating; approvals and holds (a spare of each); an evaluation.
    pending, routine = decided["pending"]["id"], decided["routine"]["id"]
    ok(reviewer, ("POST", "/api/review/{decision_id}"), f"/api/review/{pending}", 200, json=REVIEW)
    outcome = {"final_label": "nurse", "source": "ehr"}
    url = f"/api/audit/{pending}/outcome"
    ok(service, ("POST", "/api/audit/{decision_id}/outcome"), url, 200, json=outcome)
    url = f"/api/audit/{routine}/feedback"
    ok(reviewer, ("POST", "/api/audit/{decision_id}/feedback"), url, 200, json=RATING)
    approval = ok(governance, ("POST", "/api/governance"), "/api/governance", 201, json=APPROVAL)
    hold = ok(governance, ("POST", "/api/holds"), "/api/holds", 201, json=HOLD)
    run = ("POST", "/api/packs/{namespace}/{name}/evals")
    ok(admin, run, f"/api/packs/{INBOX}/evals", 200, json=RUN)
    # Keys (spares to rotate and revoke), a client (a spare) and its token.
    keys = [
        ok(admin, ("POST", "/api/auth/keys"), "/api/auth/keys", 201, json=body).json()["id"]
        for body in ({"name": f"spare {n}", "role": "service"} for n in range(2))
    ]
    body = {"name": "spare client", "role": "viewer", "jwks": {"keys": [SIGNER.jwk]}}
    client = ok(admin, ("POST", "/api/auth/clients"), "/api/auth/clients", 201, json=body).json()
    form = {
        "grant_type": "client_credentials",
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": assertion(SIGNER, client["client_id"]),
        "scope": client["scope"],
    }
    ok({}, ("POST", "/auth/token"), "/auth/token", 200, data=form)
    # People: invitations (one accepted, one revoked, two that join to be changed and removed),
    # a request to join, refused, and the organization chosen.
    invited = {
        who: ok(
            admin,
            ("POST", "/api/auth/invitations"),
            "/api/auth/invitations",
            201,
            json={"email": f"{who}-swept@{m}.example.org", "roles": ["viewer"]},
        ).json()
        for who in ("joiner", "spare", "changed", "removed")
    }
    for who in ("joiner", "changed", "removed"):
        joiner = member(
            world.store, org=None, sub=f"{who}-swept-{m}", email=f"{who}-swept@{m}.example.org"
        )
        accept = ("POST", "/api/auth/invitations/accept")
        ok(joiner, accept, accept[1], 200, json={"token": token_of(invited[who])})
    joined = {
        found["email"].split("-swept@")[0]: found["id"]
        for found in world.call(admin, "GET", "/api/auth/members").json()
        if "-swept@" in (found.get("email") or "")
    }
    asker = member(world.store, org=None, sub=f"asker-swept-{m}")
    asked = ("POST", "/api/auth/access-requests")
    ok(asker, asked, asked[1], 202, json={"org": m})
    handled = ("POST", "/api/auth/access-requests/{request_id}")
    url = f"/api/auth/access-requests/{org.ids['request']}"
    ok(admin, handled, url, 200, json={"outcome": "refused"})
    chosen = ("POST", "/api/auth/organization")
    ok(admin, chosen, chosen[1], 200, json={"org": m})
    roles = ("PUT", "/api/auth/members/{membership_id}/roles")
    ok(
        admin,
        roles,
        f"/api/auth/members/{joined['changed']}/roles",
        200,
        json={"roles": ["reviewer"]},
    )
    # What erases, rotates or deletes: spares only.
    erase = ("POST", "/api/audit/{decision_id}/erase")
    url = f"/api/audit/{decided['spare']['id']}/erase"
    ok(admin, erase, url, 200, json={"reason": "request"})
    rotate = ("POST", "/api/auth/keys/{key_id}/rotate")
    ok(admin, rotate, f"/api/auth/keys/{keys[0]}/rotate", 201, json={})
    ok(admin, ("DELETE", "/api/auth/keys/{key_id}"), f"/api/auth/keys/{keys[1]}", 204)
    url = f"/api/auth/clients/{client['client_id']}"
    replaced = {"jwks": {"keys": [SIGNER.jwk]}}
    ok(admin, ("POST", "/api/auth/clients/{client_id}/keys"), f"{url}/keys", 200, json=replaced)
    ok(admin, ("DELETE", "/api/auth/clients/{client_id}"), url, 204)
    url = f"/api/holds/{hold.json()['id']}"
    ok(governance, ("DELETE", "/api/holds/{hold_id}"), url, 204)
    url = f"/api/governance/{approval.json()['id']}"
    ok(governance, ("DELETE", "/api/governance/{approval_id}"), url, 204)
    url = f"/api/auth/members/{joined['removed']}"
    ok(admin, ("DELETE", "/api/auth/members/{membership_id}"), url, 204)
    url = f"/api/auth/invitations/{invited['spare']['id']}"
    ok(admin, ("DELETE", "/api/auth/invitations/{invitation_id}"), url, 204)


def evalset_routes(routes: Iterable[Route]) -> set[Route]:
    """The evaluation-set routes among `routes` (`EVALSET_PERMISSIONS`, `EVALSET_PATH`)."""
    return {
        (m, p)
        for m, p in routes
        if permission_for(m, p) in EVALSET_PERMISSIONS or EVALSET_PATH.search(p)
    }


# -- the test --------------------------------------------------------------------------------------


@pytest.fixture
def world(
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> Iterator[World]:
    """The deployment (`deployment`), started once with its first keys, and each organization's
    people and work through its API (`seed`); every content mark recorded. Its sets are the key
    holder's, made in the test (`hidden_sets`)."""
    caplog.set_level(logging.INFO)
    orgs = {
        name: Org(name, tenant(f"{TENANTS}/tenant-{suffix}/fhir"), content_marks(name))
        for name, suffix in ORGS.items()
    }
    keys = deployment(monkeypatch, orgs)
    store = encrypted_store(db_target)
    world = World(db_target, tmp_path, store, capsys, orgs, keys)
    for org in orgs.values():
        for kind, value in org.marks.items():
            world.mark(kind, org.name, value)
    try:
        with TestClient(create_app(db_path=db_target), base_url=APP) as client:
            world.client = client
            for org in orgs.values():
                people(world, org)
                seed(world, org)
        world.client = None
        yield world
    finally:
        store.close()


def test_no_answer_log_export_or_command_shows_a_hidden_set(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The module's sweep: the key holder's sets and commands, the operator's read-outs, the
    deployment started again with the keys as they are then, every write once with a body it
    takes, every route as every principal, again in claims mode, then the logs and the tables;
    no mark where it may not be, and the scanner shown to see."""
    exported = hidden_sets(world, monkeypatch)
    # The operator's other read-outs of the chain and of the rated decisions.
    for argv in (
        ("audit", "export"),
        ("audit", "export", "--administrative"),
        *(("audit", "export", "--administrative", "--org", org) for org in ORGS),
        *(("audit", "export", "--org", org) for org in ORGS),
    ):
        code, out, _ = world.run(*argv)
        assert code == 0
        if "--administrative" in argv:  # the sets' events are in what was scanned
            shown = {name for name, org in world.orgs.items() if org.set_id in out}
            assert shown == ({argv[-1]} if "--org" in argv else set(ORGS)), argv
    rated = world.tmp / "rated.jsonl"
    assert world.run("feedback", "export", INBOX, "--out", str(rated))[0] == 0
    world.scan(Surface("file", "the feedback export file"), rated.read_text())
    world.scan(Surface("export file", "the sealed export file"), exported.read_text())

    app = create_app(db_path=world.db)  # the deployment, started with the keys as they are now
    with TestClient(app, base_url=APP) as client:
        world.client = client
        alpha, bravo = world.orgs.values()
        evalsets_are_on(client, alpha.people["admin"])  # conflict 37: no route inventory is idle
        for org in world.orgs.values():
            admin = org.people["admin"]
            for kind in ("AuditEvent", "Provenance"):
                bundle = world.call(admin, "GET", f"/fhir/{kind}").json()
                org.ids[kind.lower()] = bundle["entry"][0]["resource"]["id"]
            principals(world, org)
        # Each organization's administrators read their sets' events (in FHIR, without the case
        # hashes), so the sweep scans them; never the other organization's.
        for org in world.orgs.values():
            other = bravo if org is alpha else alpha
            admin = org.people["admin"]
            listed = world.call(admin, "GET", "/api/audit/administrative?limit=500").text
            assert all(case in listed for case in org.case_hashes)
            fhir = world.call(admin, "GET", "/api/audit/administrative/export/fhir").text
            for text in (listed, fhir):
                assert org.set_id in text and other.set_id not in text
        for org in world.orgs.values():
            served(world, org)
        world.call({"authorization": f"Bearer {CRON}"}, "GET", "/api/internal/checkpoint")
        mounted = {*mounted_routes(app), ("GET", "/openapi.json")}
        assert evalset_routes(mounted) == set(EVALSET_ROUTES), "sweep the new evaluation-set route"
        members = callers(
            world,
            "memberships mode",
            (
                (org, f"{org.name} {name}", headers)
                for org in world.orgs.values()
                for name, headers in org.principals.items()
            ),
        )
        alone = {f"{org} ehr launch" for org in ORGS}  # a launch is no one outside its routes
        sweep(
            world,
            sorted(mounted),
            members,
            anonymous={name for _, name, _ in members[:2]} | alone,
            curators={f"{org} member admin" for org in ORGS},
        )
    world.drain("the sweep")
    # The deployment started again in claims mode (the default, ADR-0008), as its people of each
    # claims role: answered too on what memberships mode keeps for the operator.
    monkeypatch.setenv("MENDARI_ACCOUNTS", "claims")
    by_claims = create_app(db_path=world.db)
    with TestClient(by_claims, base_url=APP) as client:
        world.client = client
        people = callers(world, "claims mode", claims_people(world))
        reader = next(headers for _, name, headers in people if name.endswith("claims viewer"))
        evalsets_are_on(client, reader)
        in_claims = {*mounted_routes(by_claims), ("GET", "/openapi.json")}
        assert evalset_routes(in_claims) == set(EVALSET_ROUTES), (
            "sweep the new evaluation-set route"
        )
        sweep(
            world,
            sorted(in_claims),
            people,
            anonymous={name for _, name, _ in people[:2]},
            curators={f"{org} claims admin" for org in ORGS},
        )
    world.drain("the sweep in claims mode")

    # Every log record, and every table of the database (the chain included).
    for record in caplog.records:
        world.scan(Surface("log", f"log record {record.name}"), record.getMessage())
    world.scan(Surface("log", "the captured log"), caplog.text)
    for table in tables():
        stored = json.dumps(rows(world.store, table), default=str)
        world.scan(Surface("table", f"table {table}", table=table), stored)
    payloads_hold_only_their_pinned_keys(rows(world.store, "audit_events"))

    assert world.complete  # every text seen before the last mark was made, scanned again
    assert world.problems == []
    positive_controls(world, exported)
    assert {mark.kind for mark in world.marks} == {*CONTENT, *MADE}
    # Every route and method, as every principal of each mode.
    every = mounted | in_claims
    assert world.called == every
    assert world.swept == {
        (name, route)
        for routes, called in ((mounted, members), (in_claims, people))
        for _, name, _ in called
        for route in routes
    }
    assert set(NOT_ANSWERED) <= every and not set(NOT_ANSWERED) & world.answered
    unanswered = every - set(NOT_ANSWERED) - world.answered
    assert unanswered == set(), "answer it in `served`, or pin it in NOT_ANSWERED with why"
    commands = {"create", "add", "remove-case", "list", "rotate", "reindex", "export"}
    commands |= {"sample", "assign", "adjudicate", "progress", "agreement", "coverage"}
    commands |= {"freeze", "retire", "show"}
    assert {f"evalset {command}" for command in commands} <= world.commands


def payloads_hold_only_their_pinned_keys(chain: Sequence[Mapping[str, Any]]) -> None:
    """Every evaluation-set event the code chains has its pinned keys (`PAYLOAD_KEYS`); each one
    in the chain holds no other, each of its shape; and the world chained every kind."""
    assert {k for k in recorded_kinds() if k.startswith(EVALSET_KINDS)} == set(PAYLOAD_KEYS)
    seen = set()
    for event in chain:
        kind = str(event["event"])
        if not kind.startswith(EVALSET_KINDS):
            continue
        seen.add(kind)
        payload = json.loads(event["payload"])
        assert set(payload) <= PAYLOAD_KEYS[kind], (kind, sorted(payload))
        wrong = [key for key, value in payload.items() if not SHAPES[key](value)]
        assert wrong == [], (kind, wrong)
        assert LABEL_WORD.search(event["payload"]) is None, kind  # conflict 36
    assert seen == set(PAYLOAD_KEYS)  # each kind is in this world


def positive_controls(world: World, exported: Path) -> None:
    """The scanner sees: each mark is found where it lives. The export file holds every live
    case's id and sealed token, labels' and freeze records' included; the `evalset_` tables hold
    the ids, tokens and index hashes; and what the key holder's own store opens holds every
    content mark, each salt and each frozen set's content hash."""
    cases, sets = rows(world.store, "evalset_cases"), rows(world.store, "evalset_sets")
    index, labels = rows(world.store, "evalset_index"), rows(world.store, "evalset_labels")
    assigned = rows(world.store, "evalset_assignments")
    live = {str(row["id"]) for row in cases}
    assert len(live) == len(ORGS) * (CASES - 1 + LIFE)
    assert (len(labels), len(assigned)) == (
        len(ORGS) * (LABELS + 2 * LIFE),
        len(ORGS) * (ASSIGNED + 2 * LIFE),
    )
    states = {str(row["id"]): str(row["state"]) for row in sets}
    for org in world.orgs.values():
        assert {state: states[set_id] for state, set_id in org.lifecycle.items()} == {
            state: state for state in STATES
        }
    live |= {
        str(row[column])
        for table, stored in (
            ("evalset_sets", sets),
            ("evalset_cases", cases),
            ("evalset_labels", labels),
        )
        for row in stored
        for column in SEALED_COLUMNS[table].columns
        if row[column] is not None
    }
    assert live <= {mark.value for mark in found(exported.read_text(), world.marks)}
    stored = json.dumps([cases, sets, index, labels, assigned], default=str)
    assert {mark.kind for mark in found(stored, world.marks)} == {"case_id", "token", "index_hash"}
    hidden = EvalsetStore(world.store, world.keys)  # the key holder's own store
    every = {set_id for org in world.orgs.values() for set_id in org.sets}
    assert {item["set"] for item in hidden.sets()} == every
    for org in world.orgs.values():
        opened: list[Any] = []
        for row in cases:
            if row["set_id"] in org.sets:
                for kind in ("input", "attributes"):
                    value = world.keys.open(kind, str(row[kind]), row["set_id"], str(row["id"]))
                    opened.append(value[0])
        for row in sets:
            if row["id"] in org.sets:
                opened.append(world.keys.open("header", str(row["header"]), row["id"])[0])
                if row["freeze_record"] is not None:
                    record = world.keys.open("freeze", str(row["freeze_record"]), row["id"])
                    opened.append(record[0])
        for label in labels:
            if label["set_id"] in org.sets:
                bound = (str(label["set_id"]), str(label["case_id"]), str(label["id"]))
                opened.append(world.keys.open("label", str(label["body"]), *bound)[0])
        kinds = {
            mark.kind for mark in found(json.dumps(opened), world.marks) if mark.org == org.name
        }
        # A freeze record keeps each case's derived labels by the case's id (sealed, as is all).
        assert kinds == {*CONTENT, "salt", "content_hash", "case_id"}, org.name
    assert all(UUID.fullmatch(case_id) for case_id in (str(row["id"]) for row in cases))


# -- the scanner and its exceptions ----------------------------------------------------------------


def test_the_scanner_finds_a_mark_however_it_is_written() -> None:
    """In any case, JSON-escaped or URL-encoded, as an answer, a header or a log line may hold
    it; and nothing in a text that holds none."""
    token = Mark("token", "clinic-a", "v2:0123456789abcdef:AbC/dE+f==")
    text = Mark("input", "clinic-a", content_marks("clinic-a")["input"])
    for written in (
        f"before {token.value} after",
        json.dumps({"x": token.value}).replace("/", "\\/"),
        f"/x?next={quote(token.value, safe='')}",
    ):
        assert found(written, [token, text]) == [token], written
    assert found(f"A {text.value.upper()} 1: MY KNEE", [token, text]) == [text]
    assert found("zqinputmarkbravo, v2:0123456789abcdef:AbC", [token, text]) == []


def test_each_exception_is_as_narrow_as_its_conflict() -> None:
    """Conflict 56: case ids and sealed tokens in the sealed export file, nothing else there.
    Conflict 38: a set's name to its own organization's curators and in the key holder's
    `evalset` commands. Conflict 55: its stratum and language to those curators and in `evalset
    coverage` and `evalset show`. No mark anywhere else: no answer, log, other command or file,
    and never the chain."""
    every = (*CONTENT, *MADE)

    def shown(surface: Surface, org: str = "clinic-a") -> set[str]:
        return {kind for kind in every if allowed(Mark(kind, org, "v"), surface)}

    def command(name: str) -> Surface:
        return Surface("cli", name, command=name)

    curators = Surface("http", "a curate route", curate_of="clinic-a")
    assert shown(Surface("export file", "the sealed export")) == {"case_id", "token"}
    assert shown(curators) == {"name", "stratum", "language"}
    assert shown(curators, org="clinic-b") == set()  # another organization's curators
    assert shown(command("evalset list")) == shown(command("evalset add")) == {"name"}
    for name in ("evalset show", "evalset coverage"):
        assert shown(command(name)) == {"name", "stratum", "language"}
    for surface in (
        command("audit export"),
        command("audit verify"),
        command("feedback export"),
        Surface("http", "an answer"),
        Surface("log", "a log line"),
        Surface("file", "the feedback export file"),
        Surface("table", "the chain", table="audit_events"),
        Surface("table", "a decision", table="decisions"),
    ):
        assert shown(surface) == set(), surface.label
    assert shown(Surface("table", "the cases", table="evalset_cases")) == {"case_id", "token"}
    assert shown(Surface("table", "the labels", table="evalset_labels")) == {"case_id", "token"}
    assert shown(Surface("table", "assignments", table="evalset_assignments")) == {"case_id"}
    for name in (
        "evalset agreement",
        "evalset progress",
        "evalset assign",
        "evalset sample",
        "evalset freeze",  # LP-11i: the critical labels' counts, no stratum or language
        "evalset retire",
    ):
        assert shown(command(name)) == {"name"}, name  # no stratum or language but coverage's


def test_no_label_word_is_in_a_log_or_an_evalset_command() -> None:
    """Conflict 36: every word of the pack's labels, as a word in any case, is found in a log
    and in the output of an `evalset` command or `audit verify --evalsets`, but the four that
    give the key holder aggregates by label (`AGGREGATES`: agreement and coverage, LP-11h;
    freezing and `show`, LP-11i; naming no case); answers and the operator's exports of
    decisions show labels by design."""
    assert {"routine", "urgent", "nurse", "front_desk"} <= set(LABEL_WORDS)
    found_in = [
        "intended label {'urgency': 'routine'}",
        '{"labels": {"urgency": "Routine"}}',
        "front_desk",
    ]
    assert all(LABEL_WORD.search(text) for text in found_in)
    assert not LABEL_WORD.search("urgency routinely urgently front_desks zqinputmarkalpha")
    for surface in (
        Surface("log", "a log line"),
        Surface("cli", "evalset add", command="evalset add"),
        Surface("cli", "audit verify", command="audit verify"),
        # Blind labelling's commands (LP-11h) but the two that count by label.
        *(
            Surface("cli", name, command=name)
            for name in ("evalset sample", "evalset assign", "evalset adjudicate")
        ),
        Surface("cli", "evalset progress", command="evalset progress"),
        Surface("cli", "evalset retire", command="evalset retire"),  # LP-11i
    ):
        assert wordless(surface), surface.label
    for name in AGGREGATES:  # the key holder's aggregates: counts by label, beside no case
        assert not wordless(Surface("cli", name, command=name)), name
    # Conflict 57, amended in LP-11i: freezing's critical labels and `show`'s stored aggregates.
    assert set(AGGREGATES) == {
        "evalset agreement",
        "evalset coverage",
        "evalset freeze",
        "evalset show",
    }
    for surface in (
        Surface("http", "an answer"),
        Surface("cli", "audit export", command="audit export"),
        Surface("cli", "feedback export", command="feedback export"),
        Surface("file", "the feedback export file"),
    ):
        assert not wordless(surface), surface.label
