# SPDX-License-Identifier: Apache-2.0
"""Mendari: evaluate, govern and deploy healthcare AI agents.

from mendari import Decision

agent = Decision.from_hub("mendari/portal-message-agent")
result = agent({"message": {"body": "I need a refill on my lisinopril."}})
result.outcome, result.recommendation
"""

import os

from mendari.legacy import adopt_legacy_settings

# Before anything reads a setting: deployments configured as OpenHealth Hub keep working.
adopt_legacy_settings(os.environ)

from mendari.core.audit import AuditStore  # noqa: E402
from mendari.core.decision import CallBudget, Decision, DecisionResult  # noqa: E402
from mendari.core.pack import Pack, load_pack  # noqa: E402
from mendari.core.policy import GovernanceOverride  # noqa: E402
from mendari.core.registry import resolve_pack  # noqa: E402
from mendari.core.spec import Outcome, Priority, RiskLevel  # noqa: E402

__version__ = "0.1.0"

__all__ = [
    "AuditStore",
    "CallBudget",
    "Decision",
    "DecisionResult",
    "GovernanceOverride",
    "Outcome",
    "Pack",
    "Priority",
    "RiskLevel",
    "load_pack",
    "resolve_pack",
]
