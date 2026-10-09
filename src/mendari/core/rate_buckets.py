# SPDX-License-Identifier: Apache-2.0
"""Rate-limit buckets in a store's own database (`server/limits.py` `Limiter`), shared by the
audit store and the registry (LP-18f).

GCRA on one row per bucket: `tat` is when the bucket would be empty again (ms since the epoch).
A request is admitted when the bucket, with it, runs at most `tolerance` ahead of now; each
admitted request moves `tat` one `interval` on. Not part of any chain: no lock but the row's own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mendari.core.db import Backend

# In each store's schema, the same in both.
SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket      TEXT PRIMARY KEY,
    tat         BIGINT NOT NULL,
    expires_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_limits_expiry ON rate_limits (expires_at)"""

_FROM = "(CASE WHEN rate_limits.tat > ? THEN rate_limits.tat ELSE ? END)"


class RateBuckets:
    """The three statements a `Limiter` needs, on the store's `_db`."""

    _db: Backend

    def rate_take(
        self, bucket: str, interval: int, tolerance: int, now: int, cost: int = 1
    ) -> int | None:
        """Admit `cost` requests into `bucket` at once (None), or refuse them all without
        counting any: the milliseconds until they would be admitted. One statement, atomic
        across instances. More than the burst (`cost * interval > tolerance`) never fits, so
        it is refused without touching the bucket."""
        step = interval * cost
        with self._db.session(exclusive=False) as db:
            if step > tolerance:
                row = db.one("SELECT tat FROM rate_limits WHERE bucket = ?", (bucket,))
                return _wait(row, step, tolerance, now)
            admitted = db.one(
                "INSERT INTO rate_limits (bucket, tat, expires_at) VALUES (?, ?, ?) "
                f"ON CONFLICT (bucket) DO UPDATE SET tat = {_FROM} + ?, expires_at = {_FROM} + ? "
                f"WHERE {_FROM} + ? - ? <= ? RETURNING tat",
                (
                    *(bucket, now + step, now + step),
                    *(now, now, step),
                    *(now, now, step),
                    *(now, now, step, tolerance, now),
                ),
            )
            if admitted is not None:
                return None
            row = db.one("SELECT tat FROM rate_limits WHERE bucket = ?", (bucket,))
        return _wait(row, step, tolerance, now)

    def rate_peek(self, bucket: str, interval: int, tolerance: int, now: int) -> int | None:
        """What `rate_take` would answer, without counting anything."""
        with self._db.session(exclusive=False) as db:
            row = db.one("SELECT tat FROM rate_limits WHERE bucket = ?", (bucket,))
        return _wait(row, interval, tolerance, now)

    def rate_purge(self, now: int, batch: int) -> None:
        """Delete up to `batch` buckets that are empty again (equivalent to no row)."""
        with self._db.session(exclusive=False) as db:
            db.all(
                "DELETE FROM rate_limits WHERE bucket IN (SELECT bucket FROM rate_limits "
                "WHERE expires_at <= ? ORDER BY expires_at LIMIT ?)",
                (now, batch),
            )


def _wait(row: Mapping[str, Any] | None, interval: int, tolerance: int, now: int) -> int | None:
    """Milliseconds until a bucket at `row` admits `interval` more (one request's worth, or
    a weighted take's); None: it admits now."""
    tat = max(int(row["tat"]), now) if row is not None else now
    wait = tat + interval - tolerance - now
    return wait if wait > 0 else None
