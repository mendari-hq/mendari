# SPDX-License-Identifier: Apache-2.0
"""The sandbox's own code and identifier systems: what marks a resource as synthetic, and the
fictional systems that synthetic examples are coded in.

Every code here was invented for synthetic data, in a system named as fictional: none is SNOMED
CT, CPT or any other real code system, and none is for billing. A site maps its own codes.
`standards/terminology.py` publishes them (`sandbox_resources`), so a validator knows every code
a synthetic resource carries.
"""

from __future__ import annotations

# The tag every resource the sandbox writes carries, and the identifier system of a synthetic
# case's id, which joins a site's labels back to its cases.
SANDBOX_TAG = {"system": "https://mendari.co/fhir/sandbox", "code": "synthetic"}
CASE_SYSTEM = "https://mendari.co/fhir/synthetic-case"
# How a synthetic patient message arrived (Communication.medium).
CHANNEL_SYSTEM = "https://mendari.co/fhir/CodeSystem/message-channel"
CHANNELS = {"portal": "Patient portal", "sms": "Text message (SMS)", "email": "Email"}
# What marks a synthetic ServiceRequest as a specialty referral. (LP-14: it replaced a SNOMED CT
# code.)
REFERRAL_SYSTEM = "https://mendari.co/fhir/CodeSystem/fictional-referrals"
REFERRALS = {"FR101": "Specialty referral"}
REFERRAL = {"system": REFERRAL_SYSTEM, "code": "FR101", "display": REFERRALS["FR101"]}
# A synthetic EHR site's own codes, which a vendor-shaped record carries beside the standard
# ones: each ICD-10-CM code -> the site's diagnosis code for it and its display; and the site's
# order types.
SITE_DIAGNOSIS_SYSTEM = "https://mendari.co/fhir/CodeSystem/fictional-site-diagnoses"
SITE_DIAGNOSES = {
    "E11.9": ("D1001", "Type 2 diabetes mellitus"),
    "I10": ("D1002", "Essential hypertension"),
    "I21.4": ("D1003", "Myocardial infarction"),
    "J45.909": ("D1004", "Asthma"),
    "M51.26": ("D1005", "Lumbar disc displacement"),
    "M54.16": ("D1006", "Radiculopathy, lumbar region"),
    "G47.33": ("D1007", "Obstructive sleep apnea"),
    "E78.5": ("D1008", "Hyperlipidemia"),
    "R06.83": ("D1009", "Snoring"),
}
SITE_ORDER_TYPE_SYSTEM = "https://mendari.co/fhir/CodeSystem/fictional-site-order-types"
SITE_ORDER_TYPES = {"OT20": "Outpatient referral"}
