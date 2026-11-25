# SPDX-License-Identifier: Apache-2.0
"""What every surface says of an agent's status, and what a person reads instead of its keys.

The status line is derived from the manifest's `card.provenance`, never written by an agent.
Mendari has no way yet to record a clinician's review of an agent's digest, so every agent is
"not clinically reviewed"; an agent built from synthetic data is also only an example.
"""

from __future__ import annotations

from mendari.core.spec import AgentManifest, Question, readable

SYNTHETIC_EXAMPLE = "Synthetic example. Not clinically reviewed. Not for patient care."
NOT_REVIEWED = "Not clinically reviewed."


def status_label(manifest: AgentManifest) -> str:
    return SYNTHETIC_EXAMPLE if manifest.card.provenance == "synthetic" else NOT_REVIEWED


def title(key: str, question: Question) -> str:
    """What a person reads for a question: its `display`, else its key as words."""
    return question.display or readable(key)
