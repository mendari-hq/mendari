"""What Mendari is, said once and shown everywhere (the launch plan's notice).

`NOTICE` is stamped into every page the server serves (`<!-- mendari:notice -->`, `server/app.py`
`versioned_page`), the Decision Card, CDS Hooks cards and discovery, the OpenAPI description and
every decide response; the Hub's layout carries the same words (a test holds them equal). A
signed-in person acknowledges `ACKNOWLEDGEMENT` before deciding (Try it asks first), once per
`VERSION`, chained in the audit trail as `notice.acknowledged`. Change either text and bump
`VERSION`: everyone is asked again.

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
EVENT = "notice.acknowledged"

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
