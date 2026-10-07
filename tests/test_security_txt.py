# SPDX-License-Identifier: Apache-2.0
"""mendari.co serves /.well-known/security.txt (RFC 9116) from web/: the private reporting form
as the only contact (the project has no security e-mail address), the policy in SECURITY.md with
its 90-day disclosure window, and an expiry renewed in time. This test fails 30 days before the
file expires, so it is renewed (and web/ deployed again) before it lapses."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "web" / ".well-known" / "security.txt"
REPORT = "https://github.com/Junebugg1214/mendari/security/advisories/new"


def fields() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition(": ")
        assert separator and re.fullmatch(r"[A-Za-z-]+", name), line
        found.setdefault(name, []).append(value)
    return found


def test_the_contact_is_the_private_reporting_form_and_no_address() -> None:
    found = fields()
    assert found["Contact"] == [REPORT]
    assert REPORT in (ROOT / "SECURITY.md").read_text()
    assert "@" not in FILE.read_text() and "mailto:" not in FILE.read_text()


def test_the_policy_and_its_disclosure_window_are_linked() -> None:
    found = fields()
    assert found["Policy"] == ["https://github.com/Junebugg1214/mendari/blob/main/SECURITY.md"]
    assert found["Canonical"] == ["https://mendari.co/.well-known/security.txt"]
    security = (ROOT / "SECURITY.md").read_text()
    assert "Coordinated disclosure, 90 days." in security
    assert "mendari.co/.well-known/security.txt" in security


def test_it_expires_within_a_year_and_not_within_a_month() -> None:
    (expires,) = fields()["Expires"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", expires)
    when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    now = datetime.now(UTC)
    assert when <= now + timedelta(days=366), "RFC 9116: at most a year ahead"
    assert when > now + timedelta(days=30), "renew web/.well-known/security.txt and redeploy web/"


def test_every_field_is_one_rfc_9116_knows() -> None:
    known = {"Contact", "Expires", "Policy", "Preferred-Languages", "Canonical", "Acknowledgments"}
    assert set(fields()) <= known
    assert set(fields()) >= {"Contact", "Expires"}  # the two RFC 9116 requires
    assert all(len(values) == 1 for name, values in fields().items() if name != "Contact")
