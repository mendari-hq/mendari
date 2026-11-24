# SPDX-License-Identifier: Apache-2.0
"""Make `before-actions.sqlite.gz`: an audit database written by the code as it was before
actions (commit e40fcecc, the last of wave 3: before the calibration of an agent's confidence
and the second signal too), which `tests/test_action_audit.py` opens with the code as it is.

Run it with that commit's sources first on the path (an export of the commit into a folder of
its own, `<copy>`), then compress what it writes:

    PYTHONPATH=<copy>/src python make_before_actions.py <copy> before-actions.sqlite
    gzip -9 -n before-actions.sqlite

It records, with the inbox fixture agent on synthetic messages: decisions that wait for review,
an abstention and a refused input, a review with a note, a rating with a note, a downstream
outcome, and a follow-up event with a note. PHI mode off, no keys: the plain database a local
install has.
"""

from __future__ import annotations

import sys
from pathlib import Path

import mendari
from mendari import AuditStore, Decision, load_pack
from mendari.core import policy

root, out = Path(sys.argv[1]), Path(sys.argv[2])
assert str(root) in str(mendari.__file__), "run it with the earlier commit's sources on the path"
assert not hasattr(policy, "EFFECT_GATES"), "these sources already have actions"
if out.exists():
    out.unlink()
inbox = load_pack(root / "tests" / "fixtures" / "reference-agents" / "patient-inbox-router")
messages = [
    "Can you send a refill for metformin to my pharmacy?",
    "Crushing chest pain and my left arm is numb",
    "I have a question about my last bill.",
    "Please cancel my appointment on Friday.",
    "asdf",
]
with AuditStore(out) as store:
    decision = Decision(inbox, store=store)
    made = [decision({"message": {"body": body}}) for body in messages]
    refused = decision({"message": {"nothing": True}})
    pending = [item["id"] for item in store.pending_reviews(limit=50)]
    assert pending, "a decision waits for review"
    store.resolve_review(pending[0], final_label="nurse", reviewer="rn-1", note="a synthetic note")
    store.record_feedback(made[0].id, rating="down", reporter="rn-2", note="a synthetic note")
    store.record_outcome(pending[0], final_label="nurse", source="ehr", recorded_by="ehr-feed")
    store.record_event(made[0].id, "pas.submitted", {"outcome": "queued"}, note="the payer's")
    found = store.verify(notes=True)
    assert found["ok"], found
    print(found, [m.outcome.value for m in made], refused.outcome.value, len(pending))
