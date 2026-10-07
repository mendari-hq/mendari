# SPDX-License-Identifier: Apache-2.0
"""Synthetic prior-authorization cases for the Prior Authorization Preflight. Entirely
fictional: no PHI, and the payers (Acme, Beacon) and their requirements are made up.

Each record is an order (service code), the patient's coverage, coded diagnoses, prior
procedures and a clinic note, with ground-truth labels for every question. Ground truth is what
a careful authorization specialist concludes from the record. So at the local site, where
lumbar X-rays are filed under a code the pack does not list and notes are in shorthand, the
pack can be wrong, and local validation shows it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

Site = Literal["public", "local"]
AS_OF = date(2026, 9, 1)
ORDERS = {
    "lumbar_mri": [("72148", "MRI lumbar spine without contrast"), ("72158", "MRI lumbar spine")],
    "knee_arthroscopy": [
        ("29881", "Knee arthroscopy with meniscectomy"),
        ("29880", "Knee arthroscopy"),
    ],
    "sleep_study": [("95810", "Polysomnography"), ("95811", "Polysomnography with CPAP titration")],
    "other": [("70553", "MRI brain"), ("93306", "Echocardiogram"), ("45378", "Colonoscopy")],
}
SUPPORTING = {
    "lumbar_mri": [
        ("M54.16", "Radiculopathy, lumbar region"),
        ("M51.26", "Lumbar disc displacement"),
    ],
    "knee_arthroscopy": [
        ("S83.241A", "Tear of medial meniscus"),
        ("M23.221", "Derangement of meniscus"),
    ],
    "sleep_study": [("G47.33", "Obstructive sleep apnea"), ("R06.83", "Snoring")],
}
UNRELATED = [("I10", "Essential hypertension"), ("E78.5", "Hyperlipidemia"), ("J45.909", "Asthma")]
IMAGING = {"lumbar_mri": ("72100", "X-ray lumbar spine"), "knee_arthroscopy": ("73721", "MRI knee")}
LOCAL_LUMBAR_XRAY = ("72110", "X-ray lumbar spine, 4+ views")  # not listed by the pack
PAYERS = {
    "public": {"acme": 0.5, "beacon": 0.4, "other": 0.1},
    "local": {"acme": 0.25, "beacon": 0.55, "other": 0.2},
}
OTHER_PAYERS = ["summit", "harbor"]

THERAPY = {
    "yes": [
        "Completed {weeks} weeks of physical therapy without improvement.",
        "{weeks} weeks of home exercises and naproxen; still symptomatic.",
        "Physiotherapy for {months} months with little relief.",
        "Ibuprofen and a structured exercise programme for {weeks} weeks.",
    ],
    "no": [
        "Started physical therapy {short} weeks ago.",
        "No treatment tried yet.",
        "Physical therapy has not started yet.",
    ],
    "unknown": ["Has tried some physical therapy.", "Did physiotherapy at some point in the past."],
}
THERAPY_LOCAL = {
    "yes": [
        "s/p {weeks} wks PT, no improvement.",
        "failed conservative tx x{months}mo (PT, NSAIDs).",
    ],
    "no": ["PT x{short}wk so far.", "no tx yet."],
    "unknown": ["some PT in past."],
}
RED_FLAGS = [
    "History of {cancer} cancer, now new back pain at night.",
    "Unintentional weight loss of {kg} kg with back pain.",
    "New saddle numbness and difficulty passing urine.",
    "Fever with back pain; injects drugs.",
    "Back pain after a fall from a ladder.",
    "Progressive weakness in the {side} leg over {days} days.",
]
RED_FLAGS_LOCAL = ["hx {cancer} CA, new night pain.", "saddle anesthesia + urinary retention."]
NO_RED_FLAG = [
    "No red flags.",
    "Denies fever, weight loss or bladder changes.",
    "{relative} had {cancer} cancer.",
    "Neurological exam normal.",
]
MECHANICAL = {
    "yes": ["The knee locks intermittently.", "Catching and giving way when pivoting."],
    "no": [
        "Knee pain on stairs, no locking or catching.",
        "Aching knee without mechanical symptoms.",
    ],
}
MECHANICAL_LOCAL = {"yes": ["c/o locking R knee."], "no": ["knee pain, no mech sx."]}
SLEEP = {
    "yes": [
        "Partner reports loud snoring and pauses in breathing.",
        "Excessive daytime sleepiness; falls asleep at work.",
        "Loud snoring every night.",
    ],
    "no": [
        "Trouble falling asleep because of stress; no snoring.",
        "Difficulty staying asleep; no snoring.",
    ],
    "unknown": ["Tired lately.", "Low energy during the day."],
}
SLEEP_LOCAL = {
    "yes": ["+snoring, +EDS, witnessed apneas."],
    "no": ["insomnia, no snoring."],
    "unknown": ["fatigue."],
}
OPENERS = {
    "lumbar_mri": [
        "Low back pain radiating to the {side} leg for {months} months.",
        "Back pain for {months} months.",
    ],
    "knee_arthroscopy": [
        "Right knee pain after a twisting injury.",
        "Left knee pain for {months} months.",
    ],
    "sleep_study": ["Referred for suspected sleep apnea.", "Evaluation of sleep complaints."],
    "other": ["Follow-up visit."],
}
SLOTS: dict[str, Any] = {
    "weeks": range(6, 17),
    "months": range(2, 7),
    "short": range(1, 5),
    "kg": range(5, 13),
    "days": range(5, 15),
    "side": ["left", "right"],
    "cancer": ["prostate", "breast", "lung"],
    "relative": ["Father", "Mother"],
}


@dataclass
class Facts:
    site: Site
    service: str
    payer: str
    diagnosis: str  # match | mismatch | none
    imaging: str  # recent | stale | none (lumbar MRI and knee arthroscopy only)
    local_xray_code: bool
    therapy: str  # yes | no | unknown
    red_flag: bool
    mechanical: str  # yes | no
    sleep: str  # yes | no | unknown
    shorthand: bool
    age: int
    sex: str


def generate(n: int, *, site: Site, seed: int, id_prefix: str) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    return [_record(_facts(rng, site), rng, f"{id_prefix}-{i:04d}") for i in range(n)]


def public_splits(seed: int = 37) -> dict[str, list[dict[str, Any]]]:
    records = generate(900, site="public", seed=seed, id_prefix="pa")
    return {"calibration": records[:300], "test": records[300:]}


def local_site(seed: int = 41, n: int = 300) -> list[dict[str, Any]]:
    return generate(n, site="local", seed=seed, id_prefix="pa-local")


# -- facts -------------------------------------------------------------------------------------


def _facts(rng: random.Random, site: Site) -> Facts:
    local = site == "local"
    service = _pick(
        rng, {"lumbar_mri": 0.35, "knee_arthroscopy": 0.25, "sleep_study": 0.25, "other": 0.15}
    )
    imaged = service in IMAGING
    return Facts(
        site=site,
        service=service,
        payer=_pick(rng, PAYERS[site]),
        # No requirement set, so nothing counts as a supporting diagnosis for other services.
        diagnosis="none"
        if service == "other"
        else _pick(rng, {"match": 0.88, "mismatch": 0.07, "none": 0.05}),
        imaging=_pick(rng, {"recent": 0.75, "stale": 0.1, "none": 0.15}) if imaged else "none",
        local_xray_code=local and service == "lumbar_mri" and rng.random() < 0.5,
        therapy=_pick(rng, {"yes": 0.65, "no": 0.2, "unknown": 0.15}) if imaged else "no",
        red_flag=service == "lumbar_mri" and rng.random() < 0.12,
        mechanical=_pick(rng, {"yes": 0.7, "no": 0.3}) if service == "knee_arthroscopy" else "no",
        sleep=_pick(rng, {"yes": 0.75, "no": 0.15, "unknown": 0.1})
        if service == "sleep_study"
        else "no",
        shorthand=local and rng.random() < 0.5,
        age=rng.randint(22, 80),
        sex=rng.choice(["female", "male"]),
    )


def _pick(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


# -- ground truth ------------------------------------------------------------------------------


def labels(f: Facts) -> dict[str, str]:
    truth = {
        "service": f.service,
        "payer": f.payer,
        "diagnosis_supports": "yes" if f.diagnosis == "match" else "no",
        # A careful specialist counts the lumbar X-ray whatever code it is filed under.
        "prior_imaging": "yes" if f.imaging == "recent" else "no",
        "conservative_therapy": f.therapy,
        "red_flag": "yes" if f.red_flag else "no",
        "mechanical_symptoms": f.mechanical,
        "sleep_symptoms": f.sleep,
    }
    truth["next_step"] = next_step(truth)
    return truth


def next_step(t: dict[str, str]) -> str:
    """The pack's decision table, applied to ground truth."""
    if "other" in (t["service"], t["payer"]):
        return "not_checked"
    if t["payer"] == "beacon" and t["service"] == "sleep_study":
        return "not_required"
    gap = "needs_attention"
    if t["diagnosis_supports"] != "yes":
        return gap
    lumbar, knee = t["service"] == "lumbar_mri", t["service"] == "knee_arthroscopy"
    if lumbar and t["red_flag"] != "yes" and t["conservative_therapy"] != "yes":
        return gap
    if knee and t["conservative_therapy"] != "yes":
        return gap
    if (lumbar or knee) and t["payer"] == "acme" and t["prior_imaging"] != "yes":
        return gap
    if knee and t["mechanical_symptoms"] != "yes":
        return gap
    if t["service"] == "sleep_study" and t["sleep_symptoms"] != "yes":
        return gap
    return "submit_ready"


# -- the record --------------------------------------------------------------------------------


def _record(f: Facts, rng: random.Random, record_id: str) -> dict[str, Any]:
    code, name = rng.choice(ORDERS[f.service])
    conditions = [dict(zip(("code", "text"), rng.choice(UNRELATED), strict=True))]
    if f.diagnosis == "match":
        conditions.append(
            dict(zip(("code", "text"), rng.choice(SUPPORTING[f.service]), strict=True))
        )
    elif f.diagnosis == "mismatch":
        wrong = rng.choice([s for s in SUPPORTING if s not in (f.service, "other")])
        conditions.append(dict(zip(("code", "text"), rng.choice(SUPPORTING[wrong]), strict=True)))
    procedures = []
    if f.imaging != "none":
        imaging_code, imaging_name = LOCAL_LUMBAR_XRAY if f.local_xray_code else IMAGING[f.service]
        days = rng.randint(10, 170) if f.imaging == "recent" else rng.randint(200, 600)
        procedures.append({"code": imaging_code, "name": imaging_name, "date": _ago(days)})
    payer = rng.choice(OTHER_PAYERS) if f.payer == "other" else f.payer
    band = "18-39" if f.age < 40 else "40-64" if f.age < 65 else "65+"
    return {
        "id": record_id,
        "input": {
            "as_of": AS_OF.isoformat(),
            "patient": {"age": f.age, "sex": f.sex},
            "order": {"code": code, "name": name},
            "coverage": {"payer": payer},
            "conditions": conditions,
            "procedures": procedures,
            "notes": _note(f, rng),
        },
        "labels": labels(f),
        "attributes": {
            "site": f.site,
            "service": f.service,
            "payer": f.payer,
            "style": "shorthand" if f.shorthand else "prose",
            "age_band": band,
            "sex": f.sex,
        },
    }


def _note(f: Facts, rng: random.Random) -> str:
    slots = {key: rng.choice(list(values)) for key, values in SLOTS.items()}
    local = f.shorthand
    sentences = [rng.choice(OPENERS[f.service])]
    if f.service in IMAGING:
        sentences.append(rng.choice((THERAPY_LOCAL if local else THERAPY)[f.therapy]))
    if f.red_flag:
        sentences.append(rng.choice(RED_FLAGS_LOCAL if local else RED_FLAGS))
    elif f.service == "lumbar_mri" and rng.random() < 0.4:
        sentences.append(rng.choice(NO_RED_FLAG))
    if f.service == "knee_arthroscopy":
        sentences.append(rng.choice((MECHANICAL_LOCAL if local else MECHANICAL)[f.mechanical]))
    if f.service == "sleep_study":
        sentences.append(rng.choice((SLEEP_LOCAL if local else SLEEP)[f.sleep]))
    return " ".join(sentence.format(**slots) for sentence in sentences)


def _ago(days: int) -> str:
    return (AS_OF - timedelta(days=days)).isoformat()
