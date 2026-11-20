# SPDX-License-Identifier: Apache-2.0
"""Sites and their keys in the registry's store (LP-22d, ADR-0017).

A site is a record owned by an organization: a health system that evaluates packs on its own
chart-reviewed data and shares signed counts. It is registered `pending`, and only a registry
reviewer (LP-22i) or the operator's command, with a reason, makes it `verified` (or `refused`);
its organization or the operator may `close` it. Until it is verified it gets no ticket and
shares nothing (LP-22e, LP-22f). A closed or refused site stays stored and is never served.

A site signs with its own Ed25519 keys (`hub_site_keys`), named as every key here is
(`archive.key_id`). A key is never a namespace's publisher key nor another site's: one key
never signs in two roles. A site holds at most `MAX_SITE_KEYS` live keys; rotating is adding a
key and revoking the old one. A revocation says whether the key is thought compromised: if it
is, every report it signed leaves the pooled figures and stays listed as such (LP-22g); if not,
reports received before it stand.

Who a site is to a pack: a site whose organization holds the pack's namespace (its claim,
LP-19c) is the publisher's own site (`own_site`), listed and left out of the pool.

Every write is chained in the registry's `hub_events` with its row's hash (`site.registered`,
`site.verified`, `site.refused`, `site.closed`, `site.key_added`, `site.key_revoked`), each with
the organization, as every other registry write is; `RegistryStore.verify` covers the rows.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.core.origin import COMPOSITE_PROVIDER_TYPES, LOCAL_PROVIDER_TYPES
from mendari.errors import HubError
from mendari.evals.share import MIN_DATASET, Name, SharedPack
from mendari.hub import archive, site_signing

if TYPE_CHECKING:
    from mendari.core.db import Backend, Session

MAX_SITE_KEYS = 5
MAX_PEM_BYTES = 2_000  # an Ed25519 public key in PEM is 113 bytes
SiteStatus = Literal["pending", "verified", "refused", "closed"]
# Who decides a site: the operator's command (no organization), or a reviewer of one.
OPERATOR = None
# What a site key's proof of possession signs: the prefix, then the site's id and the key's id.
# The registry adds a key only with the key's own signature over these bytes, so nobody can
# register a key whose private half they do not hold (to claim another site's reports, or to
# block its registration or a publisher's key).
KEY_PROOF_PREFIX = b"mendari-site-key-v1\x00"
_SITE_ID = re.compile(r"^[0-9a-f]{16}\Z")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_sites (
    id          TEXT PRIMARY KEY,
    org         TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  TEXT,
    note        TEXT,
    closed_at   TEXT,
    closed_by   TEXT
);
CREATE TABLE IF NOT EXISTS hub_site_keys (
    key_id      TEXT PRIMARY KEY,
    site        TEXT NOT NULL,
    public_pem  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    added_by    TEXT NOT NULL,
    revoked_at  TEXT,
    revoked_by  TEXT,
    compromised INTEGER NOT NULL DEFAULT 0
);
"""

_SITE_FIELDS = (
    "id, org, name, status, created_at, created_by, decided_at, decided_by, note, closed_at, "
    "closed_by"
)
_KEY_FIELDS = "key_id, site, public_pem, created_at, added_by, revoked_at, revoked_by, compromised"


def key_proof_bytes(site_id: str, key_id: str) -> bytes:
    return KEY_PROOF_PREFIX + site_id.encode() + b"\x00" + key_id.encode()


def key_proof_holds(public: Ed25519PublicKey, site_id: str, proof: str) -> bool:
    """Whether `proof` (base64) is `public`'s signature over `key_proof_bytes`: the key's
    holder asked for it on this site."""
    try:
        signature = base64.b64decode(proof, validate=True)
        public.verify(signature, key_proof_bytes(site_id, archive.key_id(public)))
    except (binascii.Error, ValueError, InvalidSignature):
        return False
    return True


def _site_row(db: Session, site_id: str) -> dict[str, Any] | None:
    if not _SITE_ID.match(site_id) or not db.columns("hub_sites"):  # one from before sites
        return None
    return db.one(f"SELECT {_SITE_FIELDS} FROM hub_sites WHERE id = ?", (site_id,))


def _keys(db: Session, site_id: str) -> list[dict[str, Any]]:
    rows = db.all(
        f"SELECT {_KEY_FIELDS} FROM hub_site_keys WHERE site = ? ORDER BY created_at, key_id",
        (site_id,),
    )
    return [{**row, "compromised": bool(row["compromised"])} for row in rows]


TICKET_DAYS = 30  # owner decision 5: a ticket lasts 30 days; an unreported one is listed for good
MAX_TICKET_BYTES = 8_000
NOT_A_SITE_KEY = "the record is not signed with a live key of a verified site"
_TICKET_FIELDS = (
    "id, site, key_id, namespace, name, version, digest, configuration, remote_consent, "
    "commitment, n, created_at, expires_at, status"  # never the secret's hash or the nonce
)

TICKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_tickets (
    id              TEXT PRIMARY KEY,
    site            TEXT NOT NULL,
    key_id          TEXT NOT NULL,
    namespace       TEXT NOT NULL,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    digest          TEXT NOT NULL,
    configuration   TEXT NOT NULL,
    remote_consent  INTEGER NOT NULL,
    commitment      TEXT NOT NULL,
    n               INTEGER NOT NULL,
    nonce           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    submit_sha256   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_site_nonces (
    site        TEXT NOT NULL,
    nonce       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (site, nonce)
);
"""
SCHEMA += TICKET_SCHEMA


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    site: str
    key_id: str
    nonce: str
    at: str
    signature: str


class TicketWithdrawal(_Record):
    """A signed `mendari.site-ticket-withdrawal/1` (closed)."""

    format: Literal["mendari.site-ticket-withdrawal/1"]
    ticket: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]


def _refuse_skew(record: Mapping[str, Any]) -> None:
    if site_signing.skewed(record):
        raise HubError(
            f"the record's time is more than {site_signing.MAX_SKEW_S} seconds from the "
            "registry's clock"
        )


class TicketRequest(_Record):
    """A signed `mendari.site-ticket-request/1`: its common fields are checked by
    `site_signing.shape_problem`, its own here (closed)."""

    format: Literal["mendari.site-ticket-request/1"]
    pack: SharedPack
    configuration: Annotated[dict[Name, Name], Field(min_length=1, max_length=64)]
    remote_consent: bool
    commitment: Annotated[str, Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")]
    n: Annotated[int, Field(ge=MIN_DATASET, le=10**9)]
    # A correction (LP-22f): the report this ticket's report will supersede.
    supersedes: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")] | None = None


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _canonical_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _status(row: Mapping[str, Any], now: str) -> str:
    """A ticket's status as read: `expired` is worked out from its dates, never written (the
    chain refuses a row that changes without an event)."""
    if row["status"] == "open" and str(row["expires_at"]) <= now:
        return "expired"
    return str(row["status"])


def _ticket_shown(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **{k: row[k] for k in row if k not in ("configuration", "remote_consent")},
        "configuration": json.loads(row["configuration"]),
        "remote_consent": bool(row["remote_consent"]),
        "status": _status(row, _iso(datetime.now(UTC))),
    }


def configuration_problem(configuration: Mapping[str, str], names: Mapping[str, Any]) -> str | None:
    """Why a configuration is not one of the version's (None: it is): each of its slots,
    once, with a variant the version declares (`names.slots`, recorded at publish)."""
    slots: Mapping[str, Mapping[str, str]] = names.get("slots") or {}
    if set(configuration) != set(slots):
        return "must name a variant for each of the pack's provider slots"
    if any(variant not in slots[slot] for slot, variant in configuration.items()):
        return "names a variant the pack does not declare"
    return None


def wholly_local(configuration: Mapping[str, str], names: Mapping[str, Any]) -> bool:
    """Whether every provider a configuration runs decides on the site's machines: a local
    type, or a composite whose members' types (`names.member_types`, recorded at publish since
    LP-22e) all are. A composite of a version published before that counts as remote, as
    `evals.arms.remote_types` counts a composite whose members cannot be read."""
    slots: Mapping[str, Mapping[str, str]] = names.get("slots") or {}
    members: Mapping[str, Mapping[str, list[str]]] = names.get("member_types") or {}
    for slot, variant in configuration.items():
        kind = slots[slot][variant]
        if kind in LOCAL_PROVIDER_TYPES:
            continue
        types = members.get(slot, {}).get(variant)
        if kind not in COMPOSITE_PROVIDER_TYPES or not types:
            return False
        if not all(t in LOCAL_PROVIDER_TYPES for t in types):
            return False
    return True


def _recall_free(db: Session, namespace: str, name: str, version: str) -> bool:
    """Whether a version is still neither yanked nor deleted (read again in the writing
    transaction)."""
    key = (namespace, name, version)
    where = "WHERE namespace = ? AND name = ? AND version = ?"
    return not (
        db.one(f"SELECT version FROM hub_yanks {where}", key)
        or db.one(f"SELECT version FROM hub_deletions {where}", key)
    )


def live_result(
    db: Session,
    site: str,
    digest: str,
    configuration: str,
    commitment: str,
    *,
    excluding: str | None = None,
) -> bool:
    """Whether a site holds a live result for a digest, configuration and dataset: an open
    ticket not yet expired, a reported one (whose report LP-22f may supersede), or a withdrawn
    one: withdrawing a ticket never frees its dataset for a second run (the adversarial review
    of LP-22e). An expired ticket stays counted as unreported, for good."""
    now = _iso(datetime.now(UTC))
    rows = db.all(
        "SELECT id, status, expires_at FROM hub_tickets WHERE site = ? AND digest = ? AND "
        "configuration = ? AND commitment = ?",
        (site, digest, configuration, commitment),
    )
    # A reported ticket whose report a correction superseded is no longer live, so a
    # correction can itself be corrected (the adversarial review of LP-22f).
    superseded: set[str] = set()
    if db.columns("hub_site_reports"):
        superseded = {
            str(row["ticket"])
            for row in db.all(
                "SELECT r.ticket FROM hub_site_reports r WHERE r.site = ? AND r.id IN "
                "(SELECT supersedes FROM hub_site_reports WHERE supersedes IS NOT NULL)",
                (site,),
            )
        }
    return any(
        _status(row, now) in ("open", "reported", "withdrawn")
        for row in rows
        if row["id"] != excluding and row["id"] not in superseded
    )


class SiteStore:
    """The site methods of `RegistryStore` (a mixin: the store's `_db`)."""

    _db: Backend

    def _site_or_404(self, db: Session, site_id: str, org: str | None) -> dict[str, Any]:
        """A site, to its own organization or to the operator (`org` None). Another
        organization's site answers as one that does not exist."""
        row = _site_row(db, site_id)
        if row is None or (org is not None and row["org"] != org):
            raise HubError("no such site", 404)
        return row

    def register_site(
        self, *, org: str, name: str, by: str, reason: str | None = None
    ) -> dict[str, Any]:
        """Register a site for an organization: `pending` until a reviewer or the operator
        verifies it. Chained `site.registered` with the organization (and why, when the
        operator registered it)."""
        from mendari.hub.registry import _label, _now, record_event

        org, by = _label(org, "the organization", 200), _label(by, "who registers", 200)
        name = _label(name, "the site's name", 120)
        payload: dict[str, Any] = {"org": org, "name": name, "by": by}
        if reason is not None:
            payload["reason"] = _label(reason, "the reason", 200)
        site_id = secrets.token_hex(8)
        with self._db.session(exclusive=True) as db:
            db.all(
                "INSERT INTO hub_sites (id, org, name, status, created_at, created_by) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (site_id, org, name, _now(), by),
            )
            record_event(
                db,
                "site.registered",
                f"site:{site_id}",
                {"site": site_id, **payload},
                row=("sites", "id = ?", (site_id,)),
            )
        return self.site_record(site_id)

    def decide_site(
        self, site_id: str, *, verify: bool, by: str, note: str, by_org: str | None
    ) -> dict[str, Any]:
        """Verify or refuse a pending site, once, with a note (chained and kept). `by_org` is
        the deciding reviewer's organization, or `OPERATOR` (None) for the operator's
        command; it is required, so no caller forgets it: a reviewer never decides a site
        their own organization registered. Chained `site.verified` or `site.refused`."""
        from mendari.hub.registry import _label, _now, record_event

        by = _label(by, "who decides", 200)
        note = _label(note, "the note", 300)
        with self._db.session(exclusive=True) as db:
            site = self._site_or_404(db, site_id, None)
            if by_org is not None and site["org"] == by_org:
                raise HubError("a reviewer does not decide their own organization's site", 403)
            if site["status"] != "pending":
                raise HubError(f"the site is already {site['status']}", 409)
            status = "verified" if verify else "refused"
            db.all(
                "UPDATE hub_sites SET status = ?, decided_at = ?, decided_by = ?, note = ? "
                "WHERE id = ?",
                (status, _now(), by, note, site_id),
            )
            decided = {"site": site_id, "org": site["org"], "by": by, "note": note}
            row = ("sites", "id = ?", (site_id,))
            # Each name written out: the route inventory reads them (tests/test_shipped.py).
            if verify:
                record_event(db, "site.verified", f"site:{site_id}", decided, row=row)
            else:
                record_event(db, "site.refused", f"site:{site_id}", decided, row=row)
        return self.site_record(site_id)

    def close_site(self, site_id: str, *, org: str | None, by: str, reason: str) -> dict[str, Any]:
        """Close a site (its organization's, or the operator's with `org` None): it gets no
        ticket and shares nothing again; what it shared stays. Chained `site.closed`."""
        from mendari.hub.registry import _label, _now, record_event

        by, reason = _label(by, "who closes", 200), _label(reason, "the reason", 200)
        with self._db.session(exclusive=True) as db:
            site = self._site_or_404(db, site_id, org)
            if site["status"] == "closed":
                raise HubError("the site is already closed", 409)
            db.all(
                "UPDATE hub_sites SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ?",
                (_now(), by, site_id),
            )
            record_event(
                db,
                "site.closed",
                f"site:{site_id}",
                {"site": site_id, "org": site["org"], "by": by, "reason": reason},
                row=("sites", "id = ?", (site_id,)),
            )
        return self.site_record(site_id)

    def add_site_key(
        self, site_id: str, public_pem: str, proof: str, *, org: str | None, by: str
    ) -> dict[str, Any]:
        """Add an Ed25519 public key to a site (its organization's, or the operator's with
        `org` None), with the key's proof of possession (`key_proof_holds`, made by
        `mendari site init`). Only the key is stored (as the registry writes it out), never
        the text it came in. Refused: no proof that holds (403), a key that is a namespace's
        publisher key or any site's already (409), a closed or refused site (409), a site
        with `MAX_SITE_KEYS` live keys (409). Chained `site.key_added` with the
        organization."""
        from mendari.hub.registry import _label, _now, record_event

        by = _label(by, "who adds the key", 200)
        if len(public_pem.encode()) > MAX_PEM_BYTES:
            raise HubError("a site key is one Ed25519 public key in PEM")
        try:
            key = archive.load_public_key(public_pem.encode())
        except HubError:
            raise HubError("a site key is one Ed25519 public key in PEM") from None
        key_id = archive.key_id(key)
        public_pem = archive.public_pem(key).decode()  # the key alone: nothing that came with it
        if not isinstance(proof, str) or not key_proof_holds(key, site_id, proof):
            raise HubError(
                "the key's proof does not hold: it is the key's own signature over the site "
                "and the key (`mendari site init` makes it)",
                403,
            )
        with self._db.session(exclusive=True) as db:
            site = self._site_or_404(db, site_id, org)
            if site["status"] in ("closed", "refused"):
                raise HubError(f"the site is {site['status']}", 409)
            if db.one("SELECT key_id FROM hub_keys WHERE key_id = ?", (key_id,)):
                raise HubError(
                    "that key is a publisher's key: a site key signs for a site only", 409
                )
            if db.one("SELECT key_id FROM hub_site_keys WHERE key_id = ?", (key_id,)):
                raise HubError("that key is a site key already", 409)
            live = db.one(
                "SELECT COUNT(*) AS n FROM hub_site_keys WHERE site = ? AND revoked_at IS NULL",
                (site_id,),
            )
            if live is not None and int(live["n"]) >= MAX_SITE_KEYS:
                raise HubError(
                    f"a site holds {MAX_SITE_KEYS} live keys at most: revoke one first", 409
                )
            db.all(
                "INSERT INTO hub_site_keys (key_id, site, public_pem, created_at, added_by, "
                "compromised) VALUES (?, ?, ?, ?, ?, 0)",
                (key_id, site_id, public_pem, _now(), by),
            )
            record_event(
                db,
                "site.key_added",
                f"site:{site_id}",
                {"site": site_id, "org": site["org"], "key_id": key_id, "by": by},
                row=("site_keys", "key_id = ?", (key_id,)),
            )
        return {"site": site_id, "key_id": key_id}

    def revoke_site_key(
        self, key_id: str, *, org: str | None, by: str, reason: str, compromised: bool
    ) -> dict[str, Any]:
        """Revoke a site key, at once (its organization's, or the operator's with `org` None),
        saying whether it is thought compromised (owner decision 9: then every report it signed
        leaves the pool). Chained `site.key_revoked` with the organization."""
        from mendari.hub.registry import _label, _now, record_event

        by, reason = _label(by, "who revokes", 200), _label(reason, "the reason", 200)
        with self._db.session(exclusive=True) as db:
            row = None
            if db.columns("hub_site_keys"):
                row = db.one(f"SELECT {_KEY_FIELDS} FROM hub_site_keys WHERE key_id = ?", (key_id,))
            site = None if row is None else _site_row(db, row["site"])
            if row is None or site is None or (org is not None and site["org"] != org):
                raise HubError("no such site key", 404)  # another organization's: the same
            if row["revoked_at"] is not None:
                raise HubError("that key is already revoked", 409)
            db.all(
                "UPDATE hub_site_keys SET revoked_at = ?, revoked_by = ?, compromised = ? "
                "WHERE key_id = ?",
                (_now(), by, 1 if compromised else 0, key_id),
            )
            record_event(
                db,
                "site.key_revoked",
                f"site:{row['site']}",
                {
                    "site": row["site"],
                    "org": site["org"],
                    "key_id": key_id,
                    "by": by,
                    "reason": reason,
                    "compromised": compromised,
                },
                row=("site_keys", "key_id = ?", (key_id,)),
            )
        return self.site_record(row["site"])

    def site_record(self, site_id: str, *, org: str | None = None) -> dict[str, Any]:
        """A site with its keys, to its own organization or the operator (`org` None)."""
        with self._db.session(exclusive=False) as db:
            site = self._site_or_404(db, site_id, org)
            return {**site, "keys": _keys(db, site_id)}

    def sites(self, *, org: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        """The sites with their keys, oldest first: one organization's, in one status, or all
        (the operator's and a reviewer's view)."""
        clauses, params = [], []
        for column, value in (("org", org), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db.session(exclusive=False) as db:
            if not db.columns("hub_sites"):
                return []
            rows = db.all(
                f"SELECT {_SITE_FIELDS} FROM hub_sites{where} ORDER BY created_at, id",
                tuple(params),
            )
            return [{**row, "keys": _keys(db, row["id"])} for row in rows]

    def public_site(self, site_id: str) -> dict[str, Any]:
        """A verified site as anyone may read it: its name, status and key ids with their
        dates. A pending, refused or closed site answers as one that does not exist."""
        with self._db.session(exclusive=False) as db:
            site = _site_row(db, site_id)
            if site is None or site["status"] != "verified":
                raise HubError("no such site", 404)
            keys = _keys(db, site_id)
        return {
            "id": site["id"],
            "name": site["name"],
            "status": site["status"],
            "verified_at": site["decided_at"],
            "keys": [
                {
                    "key_id": key["key_id"],
                    "created_at": key["created_at"],
                    "revoked_at": key["revoked_at"],
                    "compromised": key["compromised"],
                }
                for key in keys
            ],
        }

    def own_site(self, site_id: str, namespace: str) -> bool:
        """Whether a site is the publisher's own for a namespace: its organization holds the
        namespace's claim (LP-19c), in whatever status. A namespace from before claims names
        no organization, so no site is its own: its publisher is the operator's to name."""
        with self._db.session(exclusive=False) as db:
            site = _site_row(db, site_id)
            if site is None or not db.columns("hub_claims"):
                return False
            claim = db.one("SELECT org FROM hub_claims WHERE namespace = ?", (namespace,))
        return claim is not None and claim["org"] == site["org"]

    # -- tickets (LP-22e) --------------------------------------------------------------------

    def _signer(self, db: Session, record: Mapping[str, Any]) -> dict[str, Any] | None:
        """The site key that signed `record`, when it is a live key of the verified site the
        record names (else None: one answer for every way it is not, so none is told apart)."""
        if not db.columns("hub_site_keys"):
            return None
        key = db.one(
            f"SELECT {_KEY_FIELDS} FROM hub_site_keys WHERE key_id = ?", (record["key_id"],)
        )
        if key is None or key["site"] != record["site"] or key["revoked_at"] is not None:
            return None
        site = _site_row(db, key["site"])
        if site is None or site["status"] != "verified":
            return None
        return {**key, "org": site["org"], "site_name": site["name"]}

    def _checked_record(
        self, db: Session, record: Mapping[str, Any], format: str
    ) -> dict[str, Any]:
        """A site record's key, signature, time and nonce, in that order (LP-22e): the key is
        a live key of a verified site, the signature is that key's over the record's exact
        fields, the time is within the skew, and the nonce is new for the site (it is spent
        here, in the caller's transaction)."""
        problem = site_signing.shape_problem(record, format)
        if problem:
            raise HubError(f"not a {format} record: {problem}")
        signer = self._signer(db, record)
        if signer is None:
            raise HubError(NOT_A_SITE_KEY, 403)
        public = archive.load_public_key(str(signer["public_pem"]).encode())
        if not site_signing.signature_holds(record, public):
            raise HubError("the record's signature does not hold", 403)
        _refuse_skew(record)
        # Nonces older than twice the skew can never be sent again (their record's time is
        # refused), so they are forgotten (the adversarial review of LP-22e).
        forget = datetime.now(UTC) - timedelta(seconds=2 * site_signing.MAX_SKEW_S)
        db.all("DELETE FROM hub_site_nonces WHERE created_at < ?", (_iso(forget),))
        if db.one(
            "SELECT nonce FROM hub_site_nonces WHERE site = ? AND nonce = ?",
            (record["site"], record["nonce"]),
        ):
            raise HubError("that record was sent before (its nonce is used)", 409)
        db.all(
            "INSERT INTO hub_site_nonces (site, nonce, created_at) VALUES (?, ?, ?)",
            (record["site"], record["nonce"], _iso(datetime.now(UTC))),
        )
        return signer

    def site_signer(self, record: Any, format: str) -> str:
        """The key id of a site record signed with a live key of a verified site, within the
        clock skew and with a nonce not yet used (read only: the write spends it), for the
        per-key rate a route applies before the write. Refused as the write refuses, so an old
        or replayed record, which anyone who saw it could send, never counts against the key
        (the adversarial review of LP-22e)."""
        problem = site_signing.shape_problem(record, format)
        if problem:
            raise HubError(f"not a {format} record: {problem}")
        with self._db.session(exclusive=False) as db:
            signer = self._signer(db, record)
            used = signer is not None and db.one(
                "SELECT nonce FROM hub_site_nonces WHERE site = ? AND nonce = ?",
                (record["site"], record["nonce"]),
            )
        if signer is None:
            raise HubError(NOT_A_SITE_KEY, 403)
        public = archive.load_public_key(str(signer["public_pem"]).encode())
        if not site_signing.signature_holds(record, public):
            raise HubError("the record's signature does not hold", 403)
        _refuse_skew(record)
        if used:
            raise HubError("that record was sent before (its nonce is used)", 409)
        return str(signer["key_id"])

    def request_ticket(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Issue a ticket for a signed `mendari.site-ticket-request/1` (LP-22e). Checked in this
        order: the record's fields; the key is a live key of a verified site; the signature;
        the clock skew; the nonce is new for the site; the version is published, listed, not
        recalled or deleted, and its digest is the one sent; the configuration is the version's
        (each of its slots, a variant it declares); a configuration that is not wholly local
        carries the site's consent; n is enough; no live ticket or report holds the same site,
        digest, configuration and commitment. Returns the ticket and its one-time submission
        secret, shown once (stored as its SHA-256). Chained `site.ticket_issued`."""
        from mendari.hub.registry import record_event

        try:
            request = TicketRequest.model_validate(record)
        except ValidationError:
            raise HubError(
                f"not a {site_signing.TICKET_REQUEST} record: its fields are the pack (id, "
                "version, digest), the configuration, remote_consent, the commitment and n"
            ) from None
        namespace, name = request.pack.id.split("/")
        # Who signed, first, before anything about the pack is answered; the nonce is spent
        # here whatever follows, so a refused request is signed again, never replayed.
        with self._db.session(exclusive=True) as db:
            signer = self._checked_record(db, record, site_signing.TICKET_REQUEST)
        try:  # as the public reads it: an unlisted namespace's version does not exist
            found = self.version(namespace, name, request.pack.version)  # type: ignore[attr-defined]
        except HubError:
            raise HubError("no such published version", 404) from None
        if found.get("yank") or found.get("deleted"):
            raise HubError("that version is recalled: it takes no evaluation", 409)
        if found.get("digest") != request.pack.digest:
            raise HubError("the digest is not the published version's", 409)
        names = found.get("names") or {}
        problem = configuration_problem(request.configuration, names)
        if problem:
            raise HubError(f"the configuration {problem}")
        if not request.remote_consent and not wholly_local(request.configuration, names):
            raise HubError(
                "this configuration calls a provider off the site's machines: it needs the "
                "site's consent (remote_consent, `--consent-remote`)",
                409,
            )
        configuration = _canonical_text(request.configuration)
        with self._db.session(exclusive=True) as db:
            # Checked again where the ticket is written: the key, the site and the listing
            # may have changed since (the adversarial review of LP-22e).
            if self._signer(db, record) is None:
                raise HubError(NOT_A_SITE_KEY, 403)
            unlisted = db.columns("hub_claims") and db.one(
                "SELECT namespace FROM hub_claims WHERE namespace = ? AND status <> 'approved'",
                (namespace,),
            )
            if unlisted:
                raise HubError("no such published version", 404)
            if not _recall_free(db, namespace, name, request.pack.version):
                raise HubError("that version is recalled: it takes no evaluation", 409)
            corrected = None
            if request.supersedes is not None:
                corrected = self.correction_target(  # type: ignore[attr-defined]
                    db, request.site, request.supersedes, request.pack.digest, configuration
                )
            if live_result(
                db,
                request.site,
                request.pack.digest,
                configuration,
                request.commitment,
                excluding=None if corrected is None else str(corrected["ticket"]),
            ):
                raise HubError(
                    "this site has a live ticket or report for this digest, configuration and "
                    "dataset already: one live result each (a correction names the report it "
                    "supersedes)",
                    409,
                )
            secret = secrets.token_urlsafe(32)
            ticket_id = secrets.token_hex(8)
            created = datetime.now(UTC)
            db.all(
                "INSERT INTO hub_tickets (id, site, key_id, namespace, name, version, digest, "
                "configuration, remote_consent, commitment, n, nonce, created_at, expires_at, "
                "status, submit_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'open', ?)",
                (
                    ticket_id,
                    request.site,
                    request.key_id,
                    namespace,
                    name,
                    request.pack.version,
                    request.pack.digest,
                    configuration,
                    1 if request.remote_consent else 0,
                    request.commitment,
                    request.n,
                    request.nonce,
                    _iso(created),
                    _iso(created + timedelta(days=TICKET_DAYS)),
                    hashlib.sha256(secret.encode()).hexdigest(),
                ),
            )
            record_event(
                db,
                "site.ticket_issued",
                f"site:{request.site}",
                {
                    "ticket": ticket_id,
                    "site": request.site,
                    "org": signer["org"],
                    "key_id": request.key_id,
                    "pack": request.pack.id,
                    "version": request.pack.version,
                    "digest": request.pack.digest,
                },
                row=("tickets", "id = ?", (ticket_id,)),
            )
            if request.supersedes is not None:
                db.all(
                    "INSERT INTO hub_ticket_corrections (ticket, supersedes, created_at) "
                    "VALUES (?, ?, ?)",
                    (ticket_id, request.supersedes, _iso(created)),
                )
                record_event(
                    db,
                    "site.correction_opened",
                    f"site:{request.site}",
                    {"ticket": ticket_id, "site": request.site, "supersedes": request.supersedes},
                    row=("corrections", "ticket = ?", (ticket_id,)),
                )
        return {"ticket": self.ticket(ticket_id), "submit_secret": secret}

    def withdraw_ticket(self, ticket_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        """A site gives up an open ticket with a signed `mendari.site-ticket-withdrawal/1` (any
        live key of the ticket's site). It stays listed, as withdrawn. Chained
        `site.ticket_withdrawn`."""
        from mendari.hub.registry import record_event

        try:
            withdrawal = TicketWithdrawal.model_validate(record)
        except ValidationError:
            raise HubError(
                f"not a {site_signing.TICKET_WITHDRAWAL} record: its one field is the ticket"
            ) from None
        if withdrawal.ticket != ticket_id:
            raise HubError("the withdrawal names another ticket")
        with self._db.session(exclusive=True) as db:
            signer = self._checked_record(db, record, site_signing.TICKET_WITHDRAWAL)
            ticket = db.one(
                "SELECT site, status, expires_at FROM hub_tickets WHERE id = ?", (ticket_id,)
            )
            if ticket is None or ticket["site"] != signer["site"]:
                raise HubError("no such ticket", 404)
            # An expired ticket is not withdrawn: it stays unreported, for good (the
            # adversarial review of LP-22e: withdrawing it would hide the run).
            status = _status(ticket, _iso(datetime.now(UTC)))
            if status != "open":
                raise HubError(f"the ticket is {status}", 409)
            db.all("UPDATE hub_tickets SET status = 'withdrawn' WHERE id = ?", (ticket_id,))
            record_event(
                db,
                "site.ticket_withdrawn",
                f"site:{signer['site']}",
                {"ticket": ticket_id, "site": signer["site"], "org": signer["org"]},
                row=("tickets", "id = ?", (ticket_id,)),
            )
        return self.ticket(ticket_id)

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        """A ticket as its site sees it: never its secret's hash or its nonce."""
        with self._db.session(exclusive=False) as db:
            row = db.one(f"SELECT {_TICKET_FIELDS} FROM hub_tickets WHERE id = ?", (ticket_id,))
        if row is None:
            raise HubError("no such ticket", 404)
        return _ticket_shown(row)

    def pack_ticket_rows(self, namespace: str, name: str) -> list[dict[str, Any]]:
        """A pack's tickets of verified or closed sites, as stored (for site evidence, LP-22g):
        never the secret's hash, the commitment or the nonce."""
        with self._db.session(exclusive=False) as db:
            if not db.columns("hub_tickets"):
                return []
            return db.all(
                "SELECT t.id, t.site, t.version, t.digest, t.configuration, t.remote_consent, "
                "t.status, t.created_at, t.expires_at FROM hub_tickets t JOIN hub_sites s "
                "ON s.id = t.site WHERE t.namespace = ? AND t.name = ? "
                "AND s.status IN ('verified', 'closed') ORDER BY t.created_at, t.id",
                (namespace, name),
            )

    def pack_tickets(self, namespace: str, name: str) -> list[dict[str, Any]]:
        """What a pack's tickets say, for anyone (LP-22e): per site that was issued any (a
        closed one too, with its status: closing a site never hides what it did not report),
        digest, configuration and consent, how many tickets were reported, are unreported
        (open, or expired with no report) and were withdrawn, with the first and last dates.
        No commitment, no secret. A pack the public cannot read is a 404."""
        if not self.versions(namespace, name):  # type: ignore[attr-defined]
            raise HubError("no such pack", 404)
        with self._db.session(exclusive=False) as db:
            if not db.columns("hub_tickets"):
                return []
            rows = db.all(
                "SELECT t.site, t.version, t.digest, t.configuration, t.remote_consent, "
                "t.status, t.created_at, t.expires_at, s.name AS site_name, "
                "s.status AS site_status FROM hub_tickets t JOIN hub_sites s ON s.id = t.site "
                "WHERE t.namespace = ? AND t.name = ? AND s.status IN ('verified', 'closed') "
                "ORDER BY t.created_at, t.id",
                (namespace, name),
            )
        now = _iso(datetime.now(UTC))
        groups: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        for row in rows:
            key = (row["site"], row["digest"], row["configuration"], int(row["remote_consent"]))
            group = groups.setdefault(
                key,
                {
                    "site": row["site"],
                    "site_name": row["site_name"],
                    "site_status": row["site_status"],
                    "version": row["version"],
                    "digest": row["digest"],
                    "configuration": json.loads(row["configuration"]),
                    "remote_consent": bool(row["remote_consent"]),
                    "reported": 0,
                    "unreported": 0,
                    "withdrawn": 0,
                    "first": row["created_at"],
                    "last": row["created_at"],
                },
            )
            status = _status(row, now)
            group["unreported" if status in ("open", "expired") else status] += 1
            group["last"] = row["created_at"]
        return list(groups.values())
