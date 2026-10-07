# SPDX-License-Identifier: Apache-2.0
"""Synthetic patients for the Clinical Trial Prescreener (the synthetic GLYCEMIA-2 study).

Each record is a structured patient summary plus clinic notes, with ground-truth labels for
every criterion. Ground truth is what a careful human reviewer would conclude from the record,
not what the pack's configuration happens to check. So at the local site, where HbA1c may be
recorded under another LOINC code, metformin under a brand name and notes in Spanish, the pack
can be wrong, and local validation shows it.

Everything is generated from facts sampled here; no provider is used to label anything.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

Site = Literal["public", "local"]
AS_OF = date(2026, 9, 1)
STRUCTURED = (
    "age_18_75",
    "t2d_diagnosis",
    "hba1c_in_range",
    "stable_metformin",
    "egfr_30_or_more",
    "no_insulin",
    "no_recent_cv_event",
)
NOTES = (
    "note_pregnancy",
    "note_pancreatitis",
    "note_active_cancer_treatment",
    "note_recent_cv_event",
)

ICD = {"t2d": ["E11.9", "E11.65", "E11.22"], "t1d": ["E10.9"], "mi": ["I21.4"], "stroke": ["I63.9"]}
SNOMED = {"t2d": ["44054006"], "t1d": ["46635009"], "mi": ["22298006"], "stroke": ["230690007"]}
TEXT = {
    "t2d": "Type 2 diabetes mellitus",
    "t1d": "Type 1 diabetes mellitus",
    "mi": "Myocardial infarction",
    "stroke": "Cerebral infarction",
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto"]
CANCERS = {
    "female": [("breast", "mama"), ("colon", "colon"), ("lung", "pulmón")],
    "male": [("prostate", "próstata"), ("colon", "colon"), ("lung", "pulmón")],
}


@dataclass
class Facts:
    site: Site
    language: str
    age: int | None
    sex: str
    diabetes: str  # t2d | t1d | none
    hba1c: tuple[float, int, str, str] | None  # (percent, days ago, unit, LOINC)
    egfr: tuple[float, int] | None  # (value, days ago)
    metformin: tuple[str, int, str, int | None] | None  # (status, days since start, name, ended)
    insulin: tuple[str, int] | None  # (status, days since it ended; 0 if active)
    coded_cv: tuple[str, int | None] | None  # (mi|stroke, days ago or None when undated)
    exclusions: set[str] = field(default_factory=set)


def generate(n: int, *, site: Site, seed: int, id_prefix: str) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    return [_record(_facts(rng, site), rng, f"{id_prefix}-{i:04d}") for i in range(n)]


def public_splits(seed: int = 17) -> dict[str, list[dict[str, Any]]]:
    records = generate(500, site="public", seed=seed, id_prefix="trial")
    return {"calibration": records[:200], "test": records[200:]}


def local_site(seed: int = 23, n: int = 150) -> list[dict[str, Any]]:
    return generate(n, site="local", seed=seed, id_prefix="trial-local")


# -- facts -------------------------------------------------------------------------------------


def _facts(rng: random.Random, site: Site) -> Facts:
    local = site == "local"
    sex = rng.choice(["female", "male"])
    age = None if rng.random() < 0.02 else rng.choice([*range(30, 76)] * 9 + [*range(76, 86)])
    diabetes = _pick(rng, {"t2d": 0.88, "t1d": 0.04, "none": 0.08})

    hba1c = None
    kind = _pick(rng, {"recent": 0.8, "stale": 0.13, "none": 0.07})
    if kind != "none":
        band = _pick(rng, {"in": 0.7, "low": 0.15, "high": 0.15})
        percent = {
            "in": rng.uniform(7.0, 10.5),
            "low": rng.uniform(6.0, 6.9),
            "high": rng.uniform(10.6, 13.0),
        }[band]
        days = rng.randint(3, 85) if kind == "recent" else rng.randint(100, 400)
        unit = "mmol/mol" if local and rng.random() < 0.5 else "%"
        code = "17856-6" if local and rng.random() < 0.15 else "4548-4"
        hba1c = (round(percent, 1), days, unit, code)

    egfr = None
    kind = _pick(rng, {"recent": 0.84, "stale": 0.1, "none": 0.06})
    if kind != "none":
        value = rng.randint(31, 110) if rng.random() < 0.9 else rng.randint(12, 29)
        egfr = (float(value), rng.randint(5, 360) if kind == "recent" else rng.randint(380, 900))

    metformin: tuple[str, int, str, int | None] | None = None
    kind = _pick(rng, {"stable": 0.78, "new": 0.1, "stopped": 0.04, "none": 0.08})
    if kind != "none":
        brand = local and rng.random() < 0.3
        name = "Glucophage 500 mg tablet" if brand else "metformin 1000 mg tablet"
        if kind == "stopped":
            metformin = ("stopped", rng.randint(400, 1500), name, rng.randint(30, 300))
        else:
            days = rng.randint(95, 2000) if kind == "stable" else rng.randint(7, 80)
            metformin = ("active", days, name, None)

    insulin = None
    kind = _pick(rng, {"active": 0.08, "recent": 0.04, "old": 0.06, "none": 0.82})
    if kind == "active":
        insulin = ("active", 0)
    elif kind in ("recent", "old"):
        insulin = ("completed", rng.randint(10, 85) if kind == "recent" else rng.randint(120, 900))

    coded_cv = None
    kind = _pick(rng, {"recent": 0.04, "remote": 0.06, "undated": 0.015, "none": 0.885})
    if kind != "none":
        event = rng.choice(["mi", "stroke"])
        coded_cv = (
            event,
            None
            if kind == "undated"
            else (rng.randint(10, 170) if kind == "recent" else rng.randint(400, 3000)),
        )

    exclusions = set()
    if sex == "female" and age is not None and age <= 45 and rng.random() < 0.2:
        exclusions.add("note_pregnancy")
    for question, rate in (
        ("note_pancreatitis", 0.05),
        ("note_active_cancer_treatment", 0.05),
        ("note_recent_cv_event", 0.04),
    ):
        if rng.random() < rate:
            exclusions.add(question)
    spanish = rng.random() < (0.3 if local else 0.05)
    return Facts(
        site,
        "es" if spanish else "en",
        age,
        sex,
        diabetes,
        hba1c,
        egfr,
        metformin,
        insulin,
        coded_cv,
        exclusions,
    )


def _pick(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


# -- ground truth (what a careful reviewer concludes from the record) ---------------------------


def labels(f: Facts) -> dict[str, str]:
    truth = {
        "age_18_75": "unknown" if f.age is None else ("yes" if 18 <= f.age <= 75 else "no"),
        "t2d_diagnosis": {"t2d": "yes", "t1d": "no", "none": "unknown"}[f.diabetes],
        "hba1c_in_range": "unknown",
        "stable_metformin": "no",
        "egfr_30_or_more": "unknown",
        "no_insulin": "yes",
        "no_recent_cv_event": "yes",
    }
    if f.hba1c is not None and f.hba1c[1] <= 90:
        truth["hba1c_in_range"] = "yes" if 7.0 <= f.hba1c[0] <= 10.5 else "no"
    if f.metformin is not None and f.metformin[0] == "active":
        truth["stable_metformin"] = "yes" if f.metformin[1] >= 90 else "no"
    if f.egfr is not None and f.egfr[1] <= 365:
        truth["egfr_30_or_more"] = "yes" if f.egfr[0] >= 30 else "no"
    if f.insulin is not None and (f.insulin[0] == "active" or f.insulin[1] <= 90):
        truth["no_insulin"] = "no"
    if f.coded_cv is not None:
        days = f.coded_cv[1]
        truth["no_recent_cv_event"] = (
            "unknown" if days is None else ("no" if days <= 180 else "yes")
        )
    truth.update({q: ("yes" if q in f.exclusions else "no") for q in NOTES})
    truth["eligibility"] = eligibility(truth)
    return truth


def eligibility(truth: dict[str, str]) -> str:
    """The study's decision table, applied to ground truth."""
    if any(truth[q] == "no" for q in STRUCTURED) or any(truth[q] == "yes" for q in NOTES):
        return "not_eligible"
    if any(truth[q] == "unknown" for q in STRUCTURED):
        return "needs_review"
    return "candidate"


# -- the record --------------------------------------------------------------------------------


def _record(f: Facts, rng: random.Random, record_id: str) -> dict[str, Any]:
    codes = SNOMED if f.site == "local" else ICD
    conditions = []
    if f.diabetes != "none":
        conditions.append(
            {
                "code": rng.choice(codes[f.diabetes]),
                "text": TEXT[f.diabetes],
                "status": "active",
                "onset": _ago(rng.randint(400, 5000)),
            }
        )
    conditions.append({"code": "I10", "text": "Essential hypertension", "status": "active"})
    if f.coded_cv is not None:
        event, days = f.coded_cv
        item = {"code": rng.choice(codes[event]), "text": TEXT[event], "status": "resolved"}
        if days is not None:
            item["onset"] = _ago(days)
        conditions.append(item)

    observations = []
    if f.hba1c is not None:
        percent, days, unit, code = f.hba1c
        value = round((percent - 2.15) / 0.0915) if unit == "mmol/mol" else percent
        observations.append(
            {"code": code, "name": "HbA1c", "value": value, "unit": unit, "date": _ago(days)}
        )
    if f.egfr is not None:
        observations.append(
            {
                "code": "33914-3",
                "name": "eGFR",
                "value": f.egfr[0],
                "unit": "mL/min/1.73m2",
                "date": _ago(f.egfr[1]),
            }
        )

    medications = []
    if f.metformin is not None:
        status, days, name, ended = f.metformin
        item = {"name": name, "status": status, "start": _ago(days)}
        if ended is not None:
            item["end"] = _ago(ended)
        medications.append(item)
    if f.insulin is not None:
        status, ended = f.insulin
        item = {
            "name": "insulin glargine 100 units/mL",
            "status": status,
            "start": _ago(ended + 400),
        }
        if status != "active":
            item["end"] = _ago(ended)
        medications.append(item)

    patient: dict[str, Any] = {"sex": f.sex}
    if f.age is not None:
        patient["age"] = f.age
    band = (
        "missing" if f.age is None else "18-39" if f.age < 40 else "40-64" if f.age < 65 else "65+"
    )
    return {
        "id": record_id,
        "input": {
            "as_of": AS_OF.isoformat(),
            "patient": patient,
            "conditions": conditions,
            "observations": observations,
            "medications": medications,
            "notes": _note(f, rng),
        },
        "labels": labels(f),
        "attributes": {"site": f.site, "language": f.language, "sex": f.sex, "age_band": band},
    }


def _ago(days: int) -> str:
    return (AS_OF - timedelta(days=days)).isoformat()


# -- notes -------------------------------------------------------------------------------------

EXCLUSION_EN = {
    "note_pregnancy": [
        "Currently {weeks} weeks pregnant, followed by OB.",
        "She is breastfeeding her {months}-month-old.",
        "Pregnancy confirmed on ultrasound this month.",
        "Expecting her baby in {future_month}.",
    ],
    "note_pancreatitis": [
        "History of acute pancreatitis ({year}).",
        "Admitted for pancreatitis in {year}.",
        "Chronic pancreatitis on enzyme replacement.",
    ],
    "note_active_cancer_treatment": [
        "Currently receiving chemotherapy for {cancer} cancer.",
        "On radiation therapy for {cancer} cancer, finishing soon.",
        "Started immunotherapy for melanoma in {month}.",
    ],
    "note_recent_cv_event": [
        "NSTEMI {cv_weeks} weeks ago, two stents placed.",
        "Stroke in {month} 2026, still in rehab.",
        "Heart attack {cv_weeks} weeks ago; cardiac rehab ongoing.",
    ],
}
EXCLUSION_LOCAL = {
    "note_pregnancy": ["G1 at {weeks} wks.", "Pt breastfeeding, baby {months} mo."],
    "note_pancreatitis": ["hx pancreatitis {year}.", "PMH: pancreatitis x1 ({year})."],
    "note_active_cancer_treatment": ["on chemo for {cancer} ca.", "getting XRT for {cancer} ca."],
    "note_recent_cv_event": ["s/p NSTEMI {cv_weeks} wks ago.", "CVA {month} 2026, in rehab."],
}
EXCLUSION_ES = {
    "note_pregnancy": [
        "Embarazada de {weeks} semanas, en control prenatal.",
        "Está amamantando a su bebé de {months} meses.",
    ],
    "note_pancreatitis": ["Antecedente de pancreatitis aguda en {year}."],
    "note_active_cancer_treatment": ["En quimioterapia por cáncer de {cancer_es}."],
    "note_recent_cv_event": [
        "Infarto hace {cv_weeks} semanas.",
        "Derrame cerebral en {mes} de 2026.",
    ],
}
# Sentences that mention an exclusion topic without documenting the exclusion: negated, family
# history, remote or resolved. Each names the question it would contradict, and is never used
# in a note that documents that exclusion.
DISTRACTORS: dict[str, list[tuple[str, str | None]]] = {
    "en": [
        ("No history of pancreatitis.", "note_pancreatitis"),
        ("Mother had pancreatitis.", None),
        ("Breast cancer in {old_year}, in remission.", None),
        ("Remote MI in {old_year}, stable.", None),
        ("Father had a stroke.", None),
        ("Denies chest pain.", None),
    ],
    "es": [
        ("Sin antecedentes de pancreatitis.", "note_pancreatitis"),
        ("Madre con pancreatitis.", None),
        ("Infarto antiguo en {old_year}.", None),
    ],
}
FEMALE_DISTRACTORS: list[tuple[str, str | None]] = [("Pregnancy test negative.", "note_pregnancy")]

OPENERS = {
    ("public", "en"): ["Seen for diabetes follow-up.", "Annual visit.", "Diabetes check-in."],
    ("local", "en"): ["f/u DM2.", "DM2 check.", "RTC for A1c."],
    ("public", "es"): ["Control de diabetes."],
    ("local", "es"): ["Control de DM2."],
}
CLOSERS = {
    "en": ["Plan: continue current regimen.", "Return in 3 months."],
    "es": ["Plan: continuar tratamiento.", "Regresar en 3 meses."],
}


def _note(f: Facts, rng: random.Random) -> str:
    slots = {
        "weeks": rng.randint(6, 30),
        "cv_weeks": rng.randint(2, 20),
        "months": rng.randint(2, 10),
        "future_month": rng.choice(["October", "November", "December"]),
        "year": rng.randint(2012, 2024),
        "old_year": rng.randint(2008, 2018),
        "month": rng.choice(MONTHS[3:]),
        "mes": rng.choice(MESES[3:]),
    }
    slots["cancer"], slots["cancer_es"] = rng.choice(CANCERS[f.sex])
    if f.language == "es":
        templates = EXCLUSION_ES
    else:
        templates = EXCLUSION_LOCAL if f.site == "local" and rng.random() < 0.6 else EXCLUSION_EN
    sentences = [rng.choice(OPENERS[(f.site, f.language)])]
    for question in sorted(f.exclusions):
        sentences.append(rng.choice(templates[question]).format(**slots))
    if rng.random() < 0.3:
        pool = DISTRACTORS[f.language]
        if f.language == "en" and f.sex == "female":
            pool = pool + FEMALE_DISTRACTORS
        text = rng.choice([text for text, contradicts in pool if contradicts not in f.exclusions])
        sentences.append(text.format(**slots))
    sentences.append(rng.choice(CLOSERS[f.language]))
    return " ".join(sentences)
