# SPDX-License-Identifier: Apache-2.0
"""Signed site reports, checked on receipt (LP-22f, ADR-0017).

A site shares one evaluation as a `mendari.site-submission/1` record it signs with one of its
live keys (`hub/site_signing.py`): `{format, site, key_id, nonce, at, ticket, report,
signature}`, `report` the `mendari.site-report/2` payload (whole counts, suppressed). It answers
one ticket (LP-22e), and the ticket's one-time secret is its bearer credential: the registry
finds the ticket by the secret's SHA-256 before it reads a byte of the body.

Then, in order: at most `MAX_REPORT_BYTES` read; the body parsed and validated (a closed model,
nothing it refuses echoed); the ticket is the one the record names and the route's version,
still open (not expired, withdrawn or reported); the key is a live key of the ticket's verified
site; the signature over the record's exact fields; the nonce is new; the report's pack, version,
digest, configuration, n and remote consent are the ticket's; its names are the pack's; and
`suppress` changes nothing. The report and the ticket's `reported` status are written in one
transaction. There is no clock check: a submission may be read first and sent later, by hand,
within its ticket's 30 days (`mendari eval --share-out`, `mendari site submit`).

One live result: a ticket takes one report, once. A correction asks for a new ticket that names
the report it supersedes (`supersedes`, LP-22e's request); the old report stays stored and
listed as superseded, and leaves the pool (LP-22g). Reports signed with a key later revoked as
compromised are listed as such.

`hub_reports` (unsigned reports, `/1` or `/2`, from before) is never altered and no longer
served: `reports()` lists signed reports only, each `verified: true` with its site, key id and
ticket, and `GET …/reports/{id}` serves the signed record, so anyone can check it against the
site's public key (`GET /v1/sites/{id}`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.errors import HubError
from mendari.evals import figures
from mendari.evals.share import SiteReportV2, composite_members, unchanged_by_suppression
from mendari.hub import archive, site_signing
from mendari.hub.sites import NOT_A_SITE_KEY, _canonical_text, _iso, _recall_free, _status

if TYPE_CHECKING:
    from mendari.core.db import Backend, Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_site_reports (
    id            TEXT PRIMARY KEY,
    site          TEXT NOT NULL,
    key_id        TEXT NOT NULL,
    ticket        TEXT NOT NULL UNIQUE,
    namespace     TEXT NOT NULL,
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    digest        TEXT NOT NULL,
    configuration TEXT NOT NULL,
    body_sha256   TEXT NOT NULL,
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    supersedes    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS hub_tickets_submit ON hub_tickets (submit_sha256);
CREATE TABLE IF NOT EXISTS hub_ticket_corrections (
    ticket      TEXT PRIMARY KEY,
    supersedes  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

_REPORT_ID = r"^[0-9a-f]{16}$"


class Submission(BaseModel):
    """A signed `mendari.site-submission/1` (closed; its common fields are checked by
    `site_signing.shape_problem`, its signature by the store)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    format: Literal["mendari.site-submission/1"]
    site: str
    key_id: str
    nonce: str
    at: str
    signature: str
    ticket: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
    report: SiteReportV2


def secret_sha256(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _report_listed(
    row: Mapping[str, Any], superseded_by: str | None, compromised: bool
) -> dict[str, Any]:
    record = json.loads(row["body"])
    return {
        "id": row["id"],
        "version": row["version"],
        "digest": row["digest"],
        "configuration": json.loads(row["configuration"]),
        "site": row["site"],
        "site_name": row["site_name"],
        "key_id": row["key_id"],
        "ticket": row["ticket"],
        "created_at": row["created_at"],
        "verified": True,
        "remote_consent": bool(record["report"]["remote_consent"]),
        "supersedes": row["supersedes"],
        "superseded_by": superseded_by,
        "compromised_key": compromised,
        "figures": figures.guarded(lambda: figures.site(record["report"])),
    }


class SiteReportStore:
    """The signed-report methods of `RegistryStore` (a mixin: the store's `_db`)."""

    _db: Backend

    def ticket_for_secret(self, secret: str) -> dict[str, Any] | None:
        """The ticket whose one-time secret this is, before any body is read; None for any
        secret that is not one."""
        if not secret:
            return None
        wanted = secret_sha256(secret)
        with self._db.session(exclusive=False) as db:
            if not db.columns("hub_tickets"):
                return None
            row = db.one("SELECT * FROM hub_tickets WHERE submit_sha256 = ?", (wanted,))
        if row is None or not hmac.compare_digest(str(row["submit_sha256"]), wanted):
            return None
        return row

    def accept_submission(
        self,
        namespace: str,
        name: str,
        version: str,
        record: Mapping[str, Any],
        *,
        ticket_id: str | None,
        by: str,
    ) -> dict[str, Any]:
        """Check a signed submission and store it (LP-22f): see the module's docstring for the
        order. `ticket_id` is the ticket the bearer secret named (None: the operator's
        `mendari registry accept-report`, a submission carried by hand, which names its own
        ticket and passes every other check). Chained `report.shared` (the report, with its
        site, key, ticket and body's SHA-256) and `site.ticket_reported` (the ticket)."""
        from mendari.hub.registry import _unknown_names, record_event

        problem = site_signing.shape_problem(record, site_signing.SUBMISSION)
        if problem:
            raise HubError(f"not a {site_signing.SUBMISSION} record: {problem}")
        try:
            submission = Submission.model_validate(record)
        except ValidationError:
            raise HubError(
                f"not a {site_signing.SUBMISSION} record: its fields are the ticket and a "
                "mendari.site-report/2 report (whole counts, as `mendari eval --share-out` "
                "writes it)"
            ) from None
        if ticket_id is not None and submission.ticket != ticket_id:
            raise HubError("the record answers another ticket than its secret's", 403)
        report = submission.report
        found = self.version(namespace, name, version)  # type: ignore[attr-defined]
        names = found.get("names")
        if not isinstance(names, dict):
            raise HubError(
                f"{namespace}/{name}@{version} was published before site reports were checked "
                "against their pack; publish a new version to receive them",
                409,
            )
        composite = any(composite_members(p) is not None for p in report.providers.values())
        if composite and not isinstance(names.get("members"), dict):
            raise HubError(
                f"{namespace}/{name}@{version} was published before reports from composite "
                "providers were checked against their pack; publish a new version to receive them",
                409,
            )
        # Everything that needs no lock is checked first, outside the registry's write lock
        # (the adversarial review of LP-22f: a payload that kept `suppress` busy held every
        # write): the ticket, the key and signature, the report against the ticket, its names,
        # its suppression and its figures. Inside the lock, only what may have changed since.
        with self._db.session(exclusive=False) as db:
            ticket = db.one("SELECT * FROM hub_tickets WHERE id = ?", (submission.ticket,))
            signer = self._signer(db, record)  # type: ignore[attr-defined]
        self._ticket_takes(ticket, namespace, name, version)
        assert ticket is not None
        if signer is None or signer["site"] != ticket["site"]:
            raise HubError(NOT_A_SITE_KEY, 403)
        public = archive.load_public_key(str(signer["public_pem"]).encode())
        if not site_signing.signature_holds(record, public):
            raise HubError("the record's signature does not hold", 403)
        configuration = {slot: p.variant for slot, p in report.providers.items()}
        if (
            (report.pack.id, report.pack.version) != (f"{namespace}/{name}", version)
            or report.pack.digest != ticket["digest"]
            or _canonical_text(configuration) != ticket["configuration"]
            or report.dataset.n != int(ticket["n"])
            or report.remote_consent != bool(ticket["remote_consent"])
        ):
            raise HubError(
                "the report is not the ticket's: its pack, digest, configuration, size or "
                "consent differ",
                409,
            )
        unknown = _unknown_names(report, names, found.get("primary"))
        if unknown:
            raise HubError(f"the report names {unknown} the pack does not have")
        if not unchanged_by_suppression(report):
            raise HubError(
                "the report shares a cell its suppression rule hides, or counts that do not add "
                "up (mendari.site-report/2: cells of 1 to 9 and their complements are null)"
            )
        shown = figures.site(report.model_dump(mode="json"))  # before anything is written
        body = json.dumps(dict(record), sort_keys=True, separators=(",", ":"))
        report_id = secrets.token_hex(8)
        with self._db.session(exclusive=True) as db:
            ticket = db.one("SELECT * FROM hub_tickets WHERE id = ?", (submission.ticket,))
            self._ticket_takes(ticket, namespace, name, version)
            assert ticket is not None
            signer = self._signer(db, record)  # type: ignore[attr-defined]
            if signer is None or signer["site"] != ticket["site"]:
                raise HubError(NOT_A_SITE_KEY, 403)
            # The version may have been recalled, deleted or unlisted since the ticket.
            if not _recall_free(db, namespace, name, version) or (
                db.columns("hub_claims")
                and db.one(
                    "SELECT namespace FROM hub_claims WHERE namespace = ? AND status <> 'approved'",
                    (namespace,),
                )
            ):
                raise HubError("that version is recalled or not listed: it takes no report", 409)
            if db.one(
                "SELECT nonce FROM hub_site_nonces WHERE site = ? AND nonce = ?",
                (submission.site, submission.nonce),
            ):
                raise HubError("that record was sent before (its nonce is used)", 409)
            db.all(
                "INSERT INTO hub_site_nonces (site, nonce, created_at) VALUES (?, ?, ?)",
                (submission.site, submission.nonce, _iso(datetime.now(UTC))),
            )
            correction = db.one(
                "SELECT supersedes FROM hub_ticket_corrections WHERE ticket = ?",
                (submission.ticket,),
            )
            body_sha256 = hashlib.sha256(body.encode()).hexdigest()
            db.all(
                "INSERT INTO hub_site_reports (id, site, key_id, ticket, namespace, name, "
                "version, digest, configuration, body_sha256, body, created_at, supersedes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    ticket["site"],
                    submission.key_id,
                    submission.ticket,
                    namespace,
                    name,
                    version,
                    ticket["digest"],
                    ticket["configuration"],
                    body_sha256,
                    body,
                    _iso(datetime.now(UTC)),
                    None if correction is None else correction["supersedes"],
                ),
            )
            db.all("UPDATE hub_tickets SET status = 'reported' WHERE id = ?", (submission.ticket,))
            subject = f"{namespace}/{name}@{version}"
            record_event(
                db,
                "report.shared",
                subject,
                {
                    "report_id": report_id,
                    "pack": f"{namespace}/{name}",
                    "version": version,
                    "site": ticket["site"],
                    "org": signer["org"],
                    "key_id": submission.key_id,
                    "ticket": submission.ticket,
                    "body_sha256": body_sha256,
                    "by": by,
                },
                row=("site_reports", "id = ?", (report_id,)),
            )
            record_event(
                db,
                "site.ticket_reported",
                f"site:{ticket['site']}",
                {"ticket": submission.ticket, "site": ticket["site"], "report_id": report_id},
                row=("tickets", "id = ?", (submission.ticket,)),
            )
        return {
            "id": report_id,
            "site": ticket["site"],
            "version": version,
            "ticket": submission.ticket,
            "verified": True,
            "figures": shown,
        }

    @staticmethod
    def _ticket_takes(
        ticket: Mapping[str, Any] | None, namespace: str, name: str, version: str
    ) -> None:
        """Refuse a ticket that is not this version's, or no longer open."""
        if ticket is None or (ticket["namespace"], ticket["name"], ticket["version"]) != (
            namespace,
            name,
            version,
        ):
            raise HubError("no such ticket for this version", 404)
        status = _status(ticket, _iso(datetime.now(UTC)))
        if status != "open":
            raise HubError(f"the ticket is {status}: it takes no report", 409)

    def site_reports(self, namespace: str, name: str) -> list[dict[str, Any]]:
        """A pack's signed reports, oldest first, for anyone who reads the pack: each with its
        site, key, ticket, whether it was superseded (and by which) and whether its key was
        revoked as compromised, and its figures, worked out from its counts."""
        with self._db.session(exclusive=False) as db:
            if not db.columns("hub_site_reports"):
                return []
            rows = db.all(
                "SELECT r.*, s.name AS site_name FROM hub_site_reports r JOIN hub_sites s "
                "ON s.id = r.site WHERE r.namespace = ? AND r.name = ? "
                "ORDER BY r.created_at, r.id",
                (namespace, name),
            )
            compromised = {
                str(k["key_id"])
                for k in db.all("SELECT key_id FROM hub_site_keys WHERE compromised = 1")
            }
        superseded = {str(r["supersedes"]): str(r["id"]) for r in rows if r["supersedes"]}
        return [
            _report_listed(row, superseded.get(str(row["id"])), str(row["key_id"]) in compromised)
            for row in rows
        ]

    def signed_report(self, namespace: str, name: str, report_id: str) -> dict[str, Any]:
        """One signed report as it was received: the site's signed record, to check against
        the site's key (`GET /v1/sites/{id}`), and its body's SHA-256."""
        with self._db.session(exclusive=False) as db:
            row = None
            if db.columns("hub_site_reports"):
                row = db.one(
                    "SELECT * FROM hub_site_reports WHERE id = ? AND namespace = ? AND name = ?",
                    (report_id, namespace, name),
                )
        if row is None:
            raise HubError("no such report", 404)
        return {
            "id": row["id"],
            "body_sha256": row["body_sha256"],
            "record": json.loads(row["body"]),
        }

    def correction_target(
        self, db: Session, site: str, supersedes: str, digest: str, configuration: str
    ) -> dict[str, Any]:
        """The report a correction's ticket names: the site's own live report for the same
        digest and configuration, not already superseded nor named by another correction."""
        row = None
        if db.columns("hub_site_reports"):
            row = db.one("SELECT * FROM hub_site_reports WHERE id = ?", (supersedes,))
        if row is None or row["site"] != site:
            raise HubError("no such report of this site to supersede", 404)
        if row["digest"] != digest or row["configuration"] != configuration:
            raise HubError(
                "a correction supersedes a report of the same digest and configuration", 409
            )
        now = _iso(datetime.now(UTC))
        for other in db.all(
            "SELECT t.status, t.expires_at FROM hub_ticket_corrections c JOIN hub_tickets t "
            "ON t.id = c.ticket WHERE c.supersedes = ?",
            (supersedes,),
        ):
            if _status(other, now) in ("open", "reported"):
                raise HubError(
                    "that report is superseded already, or a correction to it is open", 409
                )
        return row
