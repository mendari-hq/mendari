# SPDX-License-Identifier: Apache-2.0
"""The plainest agent for Mendari: one function from a case to its answers.

`handler` answers the demo agent's two questions (agents/portal-message-agent): `request`
(refill, appointment or other) and `urgent_symptoms` (yes or no), with keyword rules so it runs
offline and explains itself. The kit (`mendari_agent_kit.py`, a copy of `mendari.agent_kit`)
does the rest: it checks the request, builds an answer Mendari accepts, and serves it over HTTP
with a bearer token (api/index.py).

Mendari decides what the answers mean (policy, risk caps, review); this service only answers.
At confidence 0.95 a clear refill or appointment request can be automated under the demo
policy (auto from 0.90); anything that may be urgent is sent to a nurse by the policy's rule.
"""

from __future__ import annotations

import re
from typing import Any

from mendari_agent_kit import Case, respond

SERVICE_VERSION = "0.1.0"

# Symptoms that may need a nurse today, including some everyday wordings. A sentence that
# denies them ("no chest pain") does not count.
URGENT = re.compile(
    r"chest|breath|slurred|drooping|numb|faint|lips look blue"
    r"|side of my (face|mouth)"
)
DENIED = re.compile(r"\b(no|denies|without)\b")
REQUESTS = {
    "refill": re.compile(r"refill|renew|ran out|running low|need more"),
    "appointment": re.compile(r"appointment|reschedul|book me|schedule|cancel my visit"),
}


# -- Replace this function with your model or service call. ---------------------------------
# It gets the parsed `Case` (case.questions, case.input, case.instructions, case.text()) and
# returns `respond(case, answers)`: per question, `(choice, confidence)` or
# `{choice: probability}`.
def handler(case: Case) -> dict[str, Any]:
    text = case.text()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    urgent = any(URGENT.search(s) and not DENIED.search(s) for s in sentences)
    asked = [label for label, pattern in REQUESTS.items() if pattern.search(text)]
    # One clear request, or else (nothing recognized, or several things) the front desk reads it.
    request = (asked[0], 0.95) if len(asked) == 1 else ("other", 0.75)
    return respond(
        case,
        {
            "request": request,
            "urgent_symptoms": {"yes": 0.9, "no": 0.1} if urgent else ("no", 0.95),
        },
        rationales={
            "request": f"Keyword rules: {', '.join(asked) or 'no known request'}.",
            "urgent_symptoms": "A same-day symptom is described."
            if urgent
            else "No same-day symptom found.",
        },
        model="keyword-rules",
        service_version=SERVICE_VERSION,
        usage={"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
    )
