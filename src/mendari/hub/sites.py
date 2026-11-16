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

import secrets
from typing import TYPE_CHECKING, Any, Literal

from mendari.errors import HubError
from mendari.hub import archive

if TYPE_CHECKING:
    from mendari.core.db import Backend, Session

MAX_SITE_KEYS = 5
SiteStatus = Literal["pending", "verified", "refused", "closed"]

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
    note        TEXT
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

_SITE_FIELDS = "id, org, name, status, created_at, created_by, decided_at, decided_by, note"
_KEY_FIELDS = "key_id, site, public_pem, created_at, added_by, revoked_at, revoked_by, compromised"


def _site_row(db: Session, site_id: str) -> dict[str, Any] | None:
    if not db.columns("hub_sites"):  # a database from before sites, read as it is
        return None
    return db.one(f"SELECT {_SITE_FIELDS} FROM hub_sites WHERE id = ?", (site_id,))


def _keys(db: Session, site_id: str) -> list[dict[str, Any]]:
    rows = db.all(
        f"SELECT {_KEY_FIELDS} FROM hub_site_keys WHERE site = ? ORDER BY created_at, key_id",
        (site_id,),
    )
    return [{**row, "compromised": bool(row["compromised"])} for row in rows]


class SiteStore:
    """The site methods of `RegistryStore` (a mixin: the store's `_db`)."""

    _db: Backend

    def _site_or_404(self, db: Session, site_id: str, org: str | None) -> dict[str, Any]:
        """A site, to its own organization or to the operator (`org` None). Another
        organization's site answers as one that does not exist."""
        row = _site_row(db, site_id)
        if row is None or (org is not None and row["org"] != org):
            raise HubError(f"no site {site_id!r}", 404)
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
        self, site_id: str, *, verify: bool, by: str, note: str, by_org: str | None = None
    ) -> dict[str, Any]:
        """Verify or refuse a pending site, once, with a note (chained and kept). `by_org` is
        the deciding reviewer's organization: a reviewer never decides a site their own
        organization registered. Chained `site.verified` or `site.refused`."""
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
                "UPDATE hub_sites SET status = 'closed', decided_at = ?, decided_by = ?, "
                "note = ? WHERE id = ?",
                (_now(), by, reason, site_id),
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
        self, site_id: str, public_pem: str, *, org: str | None, by: str
    ) -> dict[str, Any]:
        """Add an Ed25519 public key to a site (its organization's, or the operator's with
        `org` None). Refused: a key that is a namespace's publisher key or any site's already
        (409), a closed or refused site (409), a site with `MAX_SITE_KEYS` live keys (409).
        Chained `site.key_added` with the organization."""
        from mendari.hub.registry import _label, _now, record_event

        by = _label(by, "who adds the key", 200)
        key = archive.load_public_key(public_pem.encode())
        key_id = archive.key_id(key)
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
            if row is None:
                raise HubError(f"no site key {key_id!r}", 404)
            site = self._site_or_404(db, row["site"], org)
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
                raise HubError(f"no site {site_id!r}", 404)
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
        namespace's claim (LP-19c)."""
        with self._db.session(exclusive=False) as db:
            site = _site_row(db, site_id)
            if site is None or not db.columns("hub_claims"):
                return False
            claim = db.one("SELECT org FROM hub_claims WHERE namespace = ?", (namespace,))
        return claim is not None and claim["org"] == site["org"]
