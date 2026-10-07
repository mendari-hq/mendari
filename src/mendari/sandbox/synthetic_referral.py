# SPDX-License-Identifier: Apache-2.0
"""Synthetic specialty referrals for the Referral Router. Entirely fictional: no PHI.

Each record is a referral (reason text and the priority the referrer chose) with the patient's
documents, reports and labs, and ground-truth labels for every question. Ground truth is what a
careful referral coordinator concludes from the record, not what the pack's configuration
checks. So at the local site, where visit notes are filed under another document code and
clinicians write in shorthand, the pack can be wrong, and local validation shows it.

Everything is generated from facts sampled here; no provider labels anything.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

Site = Literal["public", "local"]
AS_OF = date(2026, 9, 1)
SPECIALTIES = (
    "cardiology",
    "dermatology",
    "endocrinology",
    "gastroenterology",
    "neurology",
    "orthopedics",
    "pulmonology",
    "rheumatology",
    "other",
)
MIX = {
    "public": dict(zip(SPECIALTIES, (15, 14, 10, 14, 12, 14, 10, 7, 4), strict=True)),
    "local": dict(zip(SPECIALTIES, (12, 8, 8, 12, 10, 26, 10, 8, 6), strict=True)),
}
NOTE, NOTE_HP, ECG, IMAGING, CBC = "11506-3", "34117-2", "11524-6", "18748-4", "58410-2"
LAB_SPECIALTIES = ("endocrinology", "gastroenterology", "rheumatology")

# Reason templates per specialty: routine, urgent (should be seen within 2 weeks), and routine
# look-alikes that mention alarming words about something remote, stable or reassuring.
ROUTINE = {
    "cardiology": [
        "Palpitations for {months} months, ECG normal. Please assess.",
        "Blood pressure still high on two medicines. Please advise on management.",
        "Murmur heard at a routine check, no symptoms. Please assess.",
        "Atrial fibrillation on the ECG, rate controlled. Please advise on anticoagulation.",
        "High cholesterol with a strong family history of heart disease. Please advise on risk.",
    ],
    "dermatology": [
        "Acne not improving after {months} months of treatment. Please advise.",
        "Eczema on the {site} not controlled with steroid creams. Please assess.",
        "Psoriasis on the {site}; wants to discuss other treatments. Please advise.",
        "Rosacea not responding to treatment. Please advise.",
    ],
    "endocrinology": [
        "Hypothyroidism with TSH still raised despite dose changes. Please advise.",
        "Type 2 diabetes with HbA1c {a1c} % on two medicines. Please advise on next steps.",
        "Osteopenia with low vitamin D. Please advise on management.",
        "1 cm thyroid nodule with benign features on ultrasound. Please advise.",
    ],
    "gastroenterology": [
        "Reflux symptoms despite a PPI for {months} months. Please assess.",
        "IBS-type symptoms with normal bloods. Please advise on management.",
        "Raised liver enzymes (ALT {alt}) on two occasions. Please assess.",
        "Positive coeliac serology. Please see.",
    ],
    "neurology": [
        "Migraine becoming more frequent, no new features. Please advise on prevention.",
        "Tremor in both hands for {years} years. Please assess.",
        "Numbness in the fingers at night. Please assess for carpal tunnel syndrome.",
        "Memory concerns over {years} years; family worried. Please assess.",
    ],
    "orthopedics": [
        "Knee osteoarthritis, pain on stairs for {years} years. Please assess for surgery.",
        "Shoulder pain limiting overhead movement for {months} months. Please assess.",
        "Bunion causing pain in shoes. Please assess for surgery.",
        "Hip pain on walking for {months} months. Please assess.",
    ],
    "pulmonology": [
        "Asthma not controlled despite inhalers. Please advise.",
        "COPD with frequent exacerbations this winter. Please review.",
        "Chronic cough for {months} months with a normal chest X-ray. Please assess.",
        "Snoring and daytime sleepiness. Please assess for sleep apnoea.",
    ],
    "rheumatology": [
        "Hand joint pain, stiff for over an hour each morning, RF positive. Please assess.",
        "Gout with frequent flares. Please advise on treatment.",
        "Widespread pain and fatigue with normal inflammatory markers. Please advise.",
        "Psoriatic arthritis not controlled on methotrexate. Please review.",
    ],
    "other": [
        "Recurrent ear infections. Please see ENT.",
        "Requests a vasectomy consultation.",
        "Cataract affecting driving. Please see ophthalmology.",
        "Recurrent kidney stones. Please see urology.",
    ],
}  # fmt: skip
URGENT = {
    "cardiology": [
        "New chest tightness on exertion for {weeks} weeks, eases with rest. ?Angina, please see.",
        "Fainted twice during exercise. Please evaluate for a cardiac cause.",
        "Breathless on exertion with ankle swelling for {weeks} weeks. ?Heart failure, please see.",
        "Chest pain on walking that is becoming more frequent. Please see soon.",
    ],
    "dermatology": [
        "Mole on the {site} changing shape and colour for {weeks} weeks. ?Melanoma, please see.",
        "Non-healing ulcer on the {site} for {weeks} weeks. ?Skin cancer, please assess.",
        "Pigmented lesion on the {site} that has started to bleed. Please see.",
    ],
    "endocrinology": [
        "Calcium {ca} mmol/L on repeat testing, with confusion. Please see.",
        "High glucose with ketones and weight loss at age {age}. ?Type 1 diabetes, please see.",
        "Sodium {na} with falls and confusion. Please assess.",
    ],
    "gastroenterology": [
        "Difficulty swallowing with weight loss of {kg} kg. Please arrange assessment.",
        "Iron-deficiency anaemia (Hb {hb}) with a change in bowel habit. Please investigate.",
        "Rectal bleeding with weight loss at age {age}. Please see.",
        "Painless jaundice. Please see.",
    ],
    "neurology": [
        "Progressive weakness in the {limb} over {weeks} weeks. Please see.",
        "New headaches waking from sleep, with vomiting. Please assess.",
        "First seizure {when}. Please see.",
        "Double vision that is getting worse. Please assess.",
    ],
    "orthopedics": [
        "Back pain with saddle numbness and difficulty passing urine. Please see.",
        "Hot, swollen {joint} with fever; unable to bear weight. Please see.",
        "Fall {weeks} weeks ago with hip pain; unable to bear weight, X-ray equivocal. Please see.",
        "Bone pain at night with weight loss at age {age}. Please assess.",
    ],
    "pulmonology": [
        "Coughing up blood in a smoker. Please assess.",
        "CT shows a new {cm} cm lung nodule. Please review.",
        "Chest X-ray shows a mass. Please see.",
        "Worsening breathlessness over {weeks} weeks with weight loss. Please see.",
    ],
    "rheumatology": [
        "New headache and jaw pain on chewing, ESR {esr}. ?GCA, please see.",
        "Shoulder and hip stiffness with sudden visual blurring at age {age}. Please advise.",
        "Swollen joints with a rash and blood in the urine. Please see.",
    ],
    "other": [
        "Hoarse voice for {weeks} weeks in a smoker. Please see ENT.",
        "Visible blood in the urine at age {age}. Please see urology.",
    ],
}  # fmt: skip
LOOK_ALIKE = {
    "cardiology": [
        "Stable angina, unchanged for {years} years. Please review medication.",
        "Chest pain {years} years ago, normal tests since, no symptoms now. Please advise.",
    ],
    "dermatology": [
        "Mole on the {site}, unchanged for {years} years; would like a check.",
        "Seborrhoeic keratosis on the {site}, unchanged; cosmetic concern. Please advise.",
    ],
    "endocrinology": [
        "Known thyroid nodule, unchanged on repeat ultrasound. Please advise on follow-up.",
        "Type 2 diabetes, stable; would like a review of newer medicines.",
    ],
    "gastroenterology": [
        "Surveillance colonoscopy due; polyps removed {years} years ago.",
        "Long-standing heartburn, no alarm features. Please advise.",
    ],
    "neurology": [
        "TIA {years} years ago with full recovery. Please advise on driving.",
        "Headaches unchanged for years, no red flags. Please advise.",
    ],
    "orthopedics": [
        "Chronic low back pain, no bladder or bowel change, no leg weakness. Please advise.",
        "Old ankle fracture, healed, occasional ache. Please assess.",
    ],
    "pulmonology": [
        "Stable {mm} mm lung nodule, unchanged for {years} years. Please advise on follow-up.",
        "COPD, stable; annual review please.",
    ],
    "rheumatology": [
        "Hand osteoarthritis with brief morning stiffness. Please advise.",
        "Known polymyalgia, stable on low-dose steroids. Please review.",
    ],
    "other": ["Hearing aid review. Please see audiology."],
}  # fmt: skip
# No stated request: the referral names a problem but not what the referrer wants.
VAGUE = {
    "cardiology": ["Palpitations.", "High blood pressure."],
    "dermatology": ["Rash.", "Moles."],
    "endocrinology": ["Thyroid.", "Diabetes."],
    "gastroenterology": ["Reflux.", "Tummy pain."],
    "neurology": ["Headaches.", "Tremor."],
    "orthopedics": ["Knee pain.", "Shoulder."],
    "pulmonology": ["Cough.", "Asthma."],
    "rheumatology": ["Joint pains.", "Gout."],
    "other": ["Ears.", "Eyes."],
}
# The local site: clinician shorthand, and requests like "pls eval".
LOCAL_ROUTINE = {
    "cardiology": [
        "palps intermittent, holter nl. pls advise",
        "HTN uncontrolled on 2 meds, pls eval",
    ],
    "dermatology": ["eczema not resp to topicals, pls see"],
    "endocrinology": ["DM2 A1c {a1c} on metformin + SU, pls advise"],
    "gastroenterology": ["GERD on PPI, sx persist. pls eval"],
    "neurology": ["migraines incr freq, no focal sx. pls advise"],
    "orthopedics": [
        "OA knee failed conservative mgmt, pls eval for TKA",
        "L shoulder pain ?RC tear, pls eval",
    ],
    "pulmonology": ["COPD freq exac, pls review"],
    "rheumatology": ["inflamm arthritis hands, RF+, pls see"],
    "other": ["recurrent OM, pls see ENT"],
}  # fmt: skip
LOCAL_URGENT = {
    "cardiology": [
        "CP on exertion x{weeks}wk, relieved w/ rest. r/o angina",
        "exertional syncope x2, pls eval",
    ],
    "dermatology": ["changing pigmented lesion {site}, r/o MM"],
    "endocrinology": ["Ca {ca}, confused. pls see"],
    "gastroenterology": ["dysphagia + wt loss {kg}kg, pls eval", "IDA Hb {hb} + BRBPR, pls eval"],
    "neurology": ["prog R arm weakness x{weeks}wk, pls see"],
    "orthopedics": ["LBP w/ saddle anesthesia + urinary retention, pls see"],
    "pulmonology": ["hemoptysis, 40 PY smoker, pls eval"],
    "rheumatology": ["?GCA - HA, jaw claud, ESR {esr}"],
    "other": ["hoarseness x{weeks}wk, smoker, pls see ENT"],
}  # fmt: skip
SLOTS: dict[str, Any] = {
    "weeks": range(2, 9),
    "months": range(3, 19),
    "years": range(2, 16),
    "site": ["back", "left calf", "right forearm", "scalp", "shin"],
    "joint": ["knee", "ankle", "wrist"],
    "limb": ["right hand", "left leg", "left arm"],
    "when": ["last week", "3 days ago", "last month"],
    "a1c": [7.6, 8.2, 8.9, 9.4, 10.1],
    "ca": [2.9, 3.0, 3.1, 3.2, 3.4],
    "na": [118, 120, 121, 123],
    "hb": [7.9, 8.4, 8.8, 9.2, 9.6],
    "kg": range(4, 11),
    "alt": range(90, 181, 10),
    "esr": range(60, 111, 5),
    "cm": [1.5, 2.0, 2.5, 3.2],
    "mm": [3, 4, 5, 6],
}
_SLOT = re.compile(r"\{(\w+)\}")


@dataclass
class Facts:
    site: Site
    specialty: str
    urgent: bool
    kind: str  # routine | urgent | look_alike | vague
    shorthand: bool
    age: int
    sex: str
    priority: str
    note: tuple[str, int] | None  # (document code, days ago)
    ecg: int | None  # days ago
    imaging: int | None
    labs: int | None


def generate(n: int, *, site: Site, seed: int, id_prefix: str) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    return [_record(_facts(rng, site), rng, f"{id_prefix}-{i:05d}") for i in range(n)]


def public_splits(seed: int = 29) -> dict[str, list[dict[str, Any]]]:
    records = generate(2400, site="public", seed=seed, id_prefix="ref")
    return {"train": records[:1500], "calibration": records[1500:1800], "test": records[1800:]}


def local_site(seed: int = 31, n: int = 300) -> list[dict[str, Any]]:
    return generate(n, site="local", seed=seed, id_prefix="ref-local")


# -- facts -------------------------------------------------------------------------------------


def _facts(rng: random.Random, site: Site) -> Facts:
    local = site == "local"
    specialty = rng.choices(list(MIX[site]), weights=list(MIX[site].values()))[0]
    kind = _pick(rng, {"routine": 0.64, "urgent": 0.16, "look_alike": 0.12, "vague": 0.08})
    urgent = kind == "urgent"
    if urgent:
        priority = rng.choice(["urgent", "asap"]) if rng.random() < 0.5 else "routine"
    else:
        priority = "urgent" if rng.random() < 0.04 else "routine"  # over-marked
    note = None
    notes = (
        {"recent": 0.8, "stale": 0.1, "none": 0.1}
        if local
        else {"recent": 0.85, "stale": 0.08, "none": 0.07}
    )
    state = _pick(rng, notes)
    if state != "none":
        code = NOTE_HP if local and rng.random() < 0.6 else NOTE
        note = (code, rng.randint(3, 170) if state == "recent" else rng.randint(200, 500))
    return Facts(
        site=site,
        specialty=specialty,
        urgent=urgent,
        kind=kind,
        shorthand=local and kind != "vague" and rng.random() < 0.5,
        age=rng.randint(25, 85),
        sex=rng.choice(["female", "male"]),
        priority=priority,
        note=note,
        ecg=_dated(rng, 365, 0.7 if specialty == "cardiology" else 0.2, 0.12),
        imaging=_dated(rng, 365, 0.7 if specialty == "orthopedics" else 0.15, 0.12),
        labs=_dated(rng, 90, 0.7 if specialty in LAB_SPECIALTIES else 0.5, 0.15),
    )


def _dated(rng: random.Random, window: int, recent: float, stale: float) -> int | None:
    """Days since an item was dated: within the window, older, or no item at all."""
    roll = rng.random()
    if roll < recent:
        return rng.randint(2, window - 5)
    if roll < recent + stale:
        return rng.randint(window + 10, window + 400)
    return None


def _pick(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


# -- ground truth ------------------------------------------------------------------------------


def labels(f: Facts) -> dict[str, str]:
    truth = {
        "specialty": f.specialty,
        "urgency": "urgent" if f.urgent else "routine",
        "clear_question": "no" if f.kind == "vague" else "yes",
        # A careful coordinator counts any visit note, whatever document code it is filed under.
        "recent_visit_note": _yes(f.note is not None and f.note[1] <= 180),
        "ecg_on_file": _yes(f.ecg is not None and f.ecg <= 365),
        "imaging_on_file": _yes(f.imaging is not None and f.imaging <= 365),
        "recent_labs": _yes(f.labs is not None and f.labs <= 90),
        "marked_urgent": _yes(f.priority in ("urgent", "asap", "stat")),
    }
    truth["next_step"] = next_step(truth)
    return truth


def next_step(truth: dict[str, str]) -> str:
    """The pack's decision table, applied to ground truth."""
    requirements = {
        "cardiology": "ecg_on_file",
        "orthopedics": "imaging_on_file",
        "endocrinology": "recent_labs",
        "gastroenterology": "recent_labs",
        "rheumatology": "recent_labs",
    }
    if truth["urgency"] == "urgent" or truth["marked_urgent"] == "yes":
        return "urgent_review"
    if truth["specialty"] == "other":
        return "coordinator_review"
    if truth["clear_question"] == "no" or truth["recent_visit_note"] == "no":
        return "return_for_info"
    required = requirements.get(truth["specialty"])
    if required is not None and truth[required] == "no":
        return "return_for_info"
    return "schedule"


def _yes(value: bool) -> str:
    return "yes" if value else "no"


# -- the record --------------------------------------------------------------------------------


def _record(f: Facts, rng: random.Random, record_id: str) -> dict[str, Any]:
    documents = []
    if f.note is not None:
        name = "History and physical" if f.note[0] == NOTE_HP else "Progress note"
        documents.append({"code": f.note[0], "name": name, "date": _ago(f.note[1])})
    reports = []
    if f.ecg is not None:
        reports.append({"code": ECG, "name": "ECG", "date": _ago(f.ecg)})
    if f.imaging is not None:
        reports.append({"code": IMAGING, "name": "Imaging report", "date": _ago(f.imaging)})
    labs = []
    if f.labs is not None:
        labs.append({"code": CBC, "name": "CBC", "date": _ago(f.labs)})
    band = "18-39" if f.age < 40 else "40-64" if f.age < 65 else "65+"
    return {
        "id": record_id,
        "input": {
            "as_of": AS_OF.isoformat(),
            "patient": {"age": f.age, "sex": f.sex},
            "referral": {"reason": _reason(f, rng), "priority": f.priority},
            "documents": documents,
            "reports": reports,
            "labs": labs,
        },
        "labels": labels(f),
        "attributes": {
            "site": f.site,
            "specialty": f.specialty,
            "kind": f.kind,
            "style": "shorthand" if f.shorthand else "prose",
            "age_band": band,
            "sex": f.sex,
        },
    }


def _reason(f: Facts, rng: random.Random) -> str:
    if f.kind == "vague":
        templates = VAGUE
    elif f.shorthand:
        templates = LOCAL_URGENT if f.urgent else LOCAL_ROUTINE
    else:
        templates = {"urgent": URGENT, "routine": ROUTINE, "look_alike": LOOK_ALIKE}[f.kind]
    text = rng.choice(templates[f.specialty])
    return _SLOT.sub(lambda m: str(f.age if m[1] == "age" else rng.choice(SLOTS[m[1]])), text)


def _ago(days: int) -> str:
    return (AS_OF - timedelta(days=days)).isoformat()
