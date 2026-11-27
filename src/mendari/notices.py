# SPDX-License-Identifier: Apache-2.0
"""What Mendari is, said once and shown everywhere (the launch plan's notice).

`NOTICE` is stamped into the pages the server serves (the dashboard, the launch view, `/docs`,
`/docs/reference` and, in memberships mode, the invitation page `/invite`:
`<!-- mendari:notice -->`, `server/app.py` `versioned_page`; the sign-in and
launch error pages), the OpenAPI description, the Decision Card, CDS Hooks cards and discovery,
and the JSON decide answers (`notice`; a FHIR `$decide` answer is a FHIR resource and does not
carry it). The Hub's layout carries the same words (a test holds them equal).

A signed-in person who may decide or review acknowledges `ACKNOWLEDGEMENT` once per `VERSION`
(the dashboard asks on opening), chained in the audit trail as `notice.acknowledged`; until then
the server refuses them every route that takes data in (`GATED`) and minting an API key or
client (which would decide without asking). An acknowledgement is a person's, across
organizations. Change either text and bump `VERSION`: everyone is asked again.

`MENDARI_NOTICE_ACKNOWLEDGEMENT`: `required` (the default) or `off` (a test suite, or a site
whose own agreements and training replace it). API keys, Backend Services clients, EHR launches
and open mode (a local sandbox, no one signed in) are never asked.
"""

from __future__ import annotations

import os
from typing import Literal

from mendari.errors import MendariError

VERSION = "1"
NOTICE = "Research preview. Not a medical device. Not for emergencies: patients should call 911."
ACKNOWLEDGEMENT = (
    "Mendari is a research preview, not a medical device, and runs on synthetic data. Enter "
    "only synthetic data: never a real patient's name, record, message or details."
)
# The permissions whose routes take data in from a person: refused until they acknowledge.
GATED = frozenset({"decide", "review"})
# Minting a credential a person could then decide with, unasked: every POST the `keys`
# permission guards (creating or rotating an API key, registering a client) and, in memberships
# mode, inviting someone and handling a request to join (giving someone access).
MINTING_PERMISSION = "keys"

Mode = Literal["required", "off"]


def notice() -> dict[str, str]:
    """The notice as an API answer carries it."""
    return {"version": VERSION, "text": NOTICE}


def mode() -> Mode:
    """From `MENDARI_NOTICE_ACKNOWLEDGEMENT`; raises at startup on anything else."""
    raw = os.environ.get("MENDARI_NOTICE_ACKNOWLEDGEMENT", "").strip().lower() or "required"
    if raw not in ("required", "off"):
        raise MendariError(f"MENDARI_NOTICE_ACKNOWLEDGEMENT must be required or off, not {raw!r}")
    return "required" if raw == "required" else "off"


def chain_id(principal_id: str) -> str:
    """Where an acknowledgement of this version by this principal sits in the audit chain."""
    return f"notice:{VERSION}:{principal_id}"
