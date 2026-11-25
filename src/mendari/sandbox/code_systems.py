# SPDX-License-Identifier: Apache-2.0
"""The sandbox's own code and identifier systems: what marks a resource as synthetic, which
synthetic case it is, and how a synthetic message arrived. `standards/terminology.py` publishes
the code systems (`sandbox_resources`), so a validator knows every code a synthetic resource
carries.
"""

from __future__ import annotations

# The tag every resource the sandbox writes carries, and the identifier system of a synthetic
# case's id, which joins a site's labels back to its cases.
SANDBOX_TAG = {"system": "https://mendari.co/fhir/sandbox", "code": "synthetic"}
CASE_SYSTEM = "https://mendari.co/fhir/synthetic-case"
# How a synthetic patient message arrived (Communication.medium).
CHANNEL_SYSTEM = "https://mendari.co/fhir/CodeSystem/message-channel"
CHANNELS = {"portal": "Patient portal", "sms": "Text message (SMS)", "email": "Email"}
# The conditions a synthetic local site codes in its own system. A record's condition code names
# no system: `standards/record_fhir.py` reads one of these as this system's. Each display is
# ours. (LP-14: they replaced the SNOMED CT codes.)
CONDITION_SYSTEM = "https://mendari.co/fhir/CodeSystem/fictional-conditions"
CONDITIONS = {
    "FC101": "Type 2 diabetes mellitus",
    "FC102": "Type 1 diabetes mellitus",
    "FC201": "Myocardial infarction",
    "FC202": "Stroke (cerebrovascular accident)",
    "FC203": "Cerebral infarction",
}
