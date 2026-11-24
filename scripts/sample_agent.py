# SPDX-License-Identifier: Apache-2.0
"""The bundled demo agent as the FHIR samples decide with it, and the synthetic messages they
send it.

The agent is read by its keyword rules (its `reader=rules` variant). They run in this process
whatever `MENDARI_AGENT_URL` holds, so nothing is sent anywhere, and they give every kind of
decision the HL7 validator should see: one for review, one automated, one abstained (two
requests in one message leave neither label confident) and one refused.

    decide = demo_decision(store)
    decide(REFILL)
"""

from __future__ import annotations

from pathlib import Path

from mendari import AuditStore, Decision

DEMO_AGENT_DIR = Path(__file__).resolve().parent.parent / "agents" / "portal-message-agent"
LOCAL = {"reader": "rules"}  # the demo agent's local variant, by its provider slot
EMERGENCY = {"message": {"body": "Crushing chest pain and my left arm is numb"}}  # for review
REFILL = {"message": {"body": "Can you send a refill for metformin?"}}  # automated
RESCHEDULE = {"message": {"body": "Please reschedule my appointment to next week"}}  # automated
# Abstained: a refill and a visit asked for at once, so neither label is confident.
TWO_REQUESTS = {"message": {"body": "I need to renew my medicine and reschedule my visit"}}
NO_BODY: dict[str, dict[str, str]] = {"message": {}}  # refused: the input's schema requires one


def demo_decision(store: AuditStore | None = None) -> Decision:
    """The demo agent, deciding with its rules, recording in `store` when one is given."""
    return Decision.from_path(DEMO_AGENT_DIR, providers=LOCAL, store=store)
