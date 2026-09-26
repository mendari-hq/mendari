"""Synthetic patient-portal messages for the Patient Inbox Router. Entirely fictional: no PHI.

Two populations are generated from one template bank:

* ``public`` — the published benchmark population (train / calibration / test splits).
* ``local``  — a simulated deploying site with a different case mix (more Spanish, more SMS,
  more requests for outside records) and phrasings the public data never contains. It exists
  to demonstrate that local validation catches degradation a public benchmark cannot.

Labels follow one routing principle, stated in the pack's criteria: route to the least
specialised team that can fully handle the request, except that anything needing clinical
judgment goes to a clinical team, and possible emergencies go to nurse triage.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

Site = Literal["public", "local"]


@dataclass(frozen=True)
class Template:
    topic: str
    destination: str
    bodies: tuple[str, ...]
    urgency: str = "routine"
    language: str = "en"
    site: str = "all"  # "all", or the only site whose patients phrase requests this way
    pediatric: bool = False


def T(topic: str, destination: str, *bodies: str, **options: Any) -> Template:
    return Template(topic, destination, bodies, **options)


TEMPLATES: tuple[Template, ...] = (
    # --- front desk ---------------------------------------------------------------------
    T(
        "schedule",
        "front_desk",
        "I'd like to schedule an appointment for {visit_reason}. Do you have anything on {day}?",
        "Can I book a {visit_type} with {doctor}? Mornings work best for me.",
        "I need to make an appointment for my annual physical. Any openings next week?",
        "Is {doctor} accepting new patients? I would like to set up a first visit.",
        "Could you find me a {visit_type} slot sometime after 3pm?",
    ),
    T(
        "reschedule",
        "front_desk",
        "I need to reschedule my appointment on {day} at {time}. Something came up at work.",
        "Can we move my {visit_type} from {day} to sometime next week?",
        "Is it possible to switch my {time} appointment to later in the day?",
        "I have a conflict with my appointment {day}. What other times are available?",
    ),
    T(
        "cancel",
        "front_desk",
        "Please cancel my appointment on {day}. I'll call to rebook later.",
        "I won't be able to make it {day} at {time}, please cancel.",
        "Cancel my {visit_type} please, I'm traveling that week.",
    ),
    T(
        "logistics",
        "front_desk",
        "What are your hours on Saturday?",
        "Where should I park for the {clinic} office?",
        "Is the clinic open the day after Thanksgiving?",
        "What is the address of the new {clinic} location?",
        "Do I need to arrive early to fill out paperwork for my {visit_type}?",
    ),
    T(
        "portal",
        "front_desk",
        "I can't log in to the portal, it says my password expired.",
        "How do I give my daughter proxy access to my portal account?",
        "The app keeps logging me out when I try to see my appointments.",
    ),
    T(
        "contact_update",
        "front_desk",
        "I have a new phone number, {phone}. Please update my file.",
        "I moved to {street}. Can you update my address?",
        "Please change my preferred contact method to text messages.",
    ),
    T(
        "vague",
        "front_desk",
        "Please call me back when you get a chance.",
        "I have a question about my last visit.",
        "Can someone give me a call?",
        "Following up on my message from last week.",
    ),
    # --- medical assistant --------------------------------------------------------------
    T(
        "refill",
        "medical_assistant",
        "I need a refill on my {med}. I have about {count} pills left.",
        "Can you send a refill for {med} to {pharmacy}?",
        "My {med} prescription ran out, could you renew it?",
        "The pharmacy says they need authorization from your office to refill my {med}.",
        "Requesting a 90 day supply of {med} please.",
    ),
    T(
        "outside_records",
        "medical_assistant",
        "Can you request my records from {hospital}? I saw a specialist there last year.",
        "Please send a copy of my immunization records to {school}.",
        "I need my records sent to my new {specialty} office.",
        "Could you fax my medical records to {hospital} before my appointment there?",
    ),
    T(
        "outside_records",
        "medical_assistant",
        "I was in the ER at {hospital} last weekend, can you get the notes from that visit "
        "added to my chart?",
        "My {specialist} at {hospital} did some testing, please pull those results into your "
        "system before I see {doctor}.",
        "I had an MRI done at {imaging_center}. Can you get the images and the report sent over?",
        "The hospital discharge papers from {hospital} should go in my file. Can someone "
        "request them?",
        site="local",
    ),
    T(
        "referral_status",
        "medical_assistant",
        "Has my referral to {specialty} been sent? They said they haven't received it.",
        "Any update on the referral to {specialty}? I'm still waiting for a call.",
        "{specialty} office needs the referral paperwork faxed again.",
    ),
    T(
        "forms",
        "medical_assistant",
        "I need my {form} form filled out, can I drop it off at the front?",
        "My son needs a sports physical form signed for school.",
        "Can you complete the {form} paperwork from my last visit?",
    ),
    T(
        "order_status",
        "medical_assistant",
        "Did the doctor put in the order for my blood work? The lab says they don't see it.",
        "The imaging center says they need the order before they can schedule me.",
        "Can you resend the lab order for my {lab} test?",
    ),
    # --- nurse (routine) ----------------------------------------------------------------
    T(
        "symptoms",
        "nurse",
        "I've had a {symptom} for {duration}. Should I come in or wait it out?",
        "I've been having a {symptom} on and off, is this something to worry about?",
        "No chest pain or trouble breathing, just a {symptom} for {duration}. What should I do?",
        "Woke up with a {symptom} and a low grade fever. Do I need to be seen?",
    ),
    T(
        "symptoms",
        "nurse",
        "My {child} has had a fever of {temp} since yesterday but is still eating and drinking. "
        "What should I do?",
        "My {child} has a {symptom} and is pulling at their ear. Should we bring them in?",
        pediatric=True,
    ),
    T(
        "side_effects",
        "nurse",
        "Since starting {med} I've been feeling {side_effect}. Is that normal?",
        "The {med} is giving me {side_effect}, should I keep taking it?",
        "I started {med} last week and now I have {side_effect}.",
    ),
    T(
        "wound_care",
        "nurse",
        "My incision from {procedure} looks a little red around the edges. How should I clean it?",
        "When can I shower after {procedure}? I don't want to get the stitches wet.",
        "There's some clear drainage from the site of {procedure}. Is that expected?",
    ),
    T(
        "vaccines",
        "nurse",
        "Is it too soon to get my flu shot after my COVID booster last week?",
        "Does my daughter need any vaccines before kindergarten?",
        "I think I missed my tetanus booster, how often do I need one?",
    ),
    T(
        "mixed_clinical",
        "nurse",
        "Can you move my appointment to Friday? Also I've been {side_effect} since starting {med}.",
        "I need a refill on {med}, but I've had a {symptom} for {duration} that won't go away.",
        "Question about my bill, and also I have a {symptom} that's getting worse.",
    ),
    # --- nurse (possible emergency) -----------------------------------------------------
    T(
        "chest_pain",
        "nurse",
        "I've had chest pain for the last {minutes} minutes and it's spreading to my left arm.",
        "Having tight chest pressure and I'm sweating a lot right now.",
        "Crushing pain in my chest since this morning, feels hard to breathe.",
        urgency="urgent",
    ),
    T(
        "breathing",
        "nurse",
        "I can't catch my breath and my lips look a little blue.",
        "My {child} is struggling to breathe and wheezing badly, the inhaler isn't helping.",
        "Shortness of breath getting worse over the last hour, I can barely talk.",
        urgency="urgent",
    ),
    T(
        "stroke",
        "nurse",
        "My husband's face is drooping on one side and his speech is slurred.",
        "Sudden weakness in my right arm and I can't find my words.",
        "My mother is suddenly confused and can't lift her left arm.",
        urgency="urgent",
    ),
    T(
        "self_harm",
        "nurse",
        "I don't want to be alive anymore and I've been thinking about how to end it.",
        "I've been having thoughts of killing myself and I'm scared.",
        "I took a lot more of my {med} than I should have on purpose.",
        urgency="urgent",
    ),
    T(
        "bleeding",
        "nurse",
        "I cut my hand and it won't stop bleeding after 20 minutes of pressure.",
        "I'm pregnant and I'm bleeding heavily.",
        "Throwing up blood since this morning.",
        urgency="urgent",
    ),
    T(
        "anaphylaxis",
        "nurse",
        "My throat feels like it's closing up after taking the new {med}.",
        "Swelling of my lips and tongue after eating peanuts, hives everywhere.",
        urgency="urgent",
    ),
    T(
        "infant_fever",
        "nurse",
        "My 6 week old baby has a fever of 101.2 and won't feed.",
        "Newborn is very sleepy and has a temperature of 100.9, hard to wake up.",
        urgency="urgent",
        pediatric=True,
    ),
    # --- physician ----------------------------------------------------------------------
    T(
        "results",
        "physician",
        "My {lab} came back at {value}. What does that mean? Do I need to change anything?",
        "I saw my {lab} results in the portal and they're flagged high. Can {doctor} explain?",
        "My {lab} went up since last time. Should I be worried about that?",
        "What do my latest {lab} numbers mean for my treatment?",
    ),
    T(
        "treatment_change",
        "physician",
        "I'd like to talk to {doctor} about stopping {med}. I don't think I need it anymore.",
        "Can we increase my {med} dose? My home blood pressure readings are still around {bp}.",
        "I want to discuss switching from {med} to something else with the doctor.",
        "Should I still be on {med} now that I've lost weight?",
    ),
    T(
        "diagnosis",
        "physician",
        "The specialist suggested surgery for my {condition}. What does {doctor} think?",
        "Do you think my fatigue could be related to my thyroid? Should we test for it?",
        "I'd like the doctor's opinion on whether I need a colonoscopy at my age.",
        "Is my {condition} something that needs more treatment or can we keep watching it?",
    ),
    # --- billing ------------------------------------------------------------------------
    T(
        "bill",
        "billing",
        "I got a bill for {amount} for my visit on {date}, but I thought my insurance covered it.",
        "Why was I charged {amount} for a {visit_type}?",
        "My statement shows a balance of {amount} that I don't understand.",
        "I received an invoice for {amount}. Can you explain the charges?",
    ),
    T(
        "payment",
        "billing",
        "Can I set up a payment plan for my balance of {amount}?",
        "How can I pay my bill online?",
        "I was charged twice for my copay on {date}. Can I get a refund?",
    ),
    T(
        "insurance",
        "billing",
        "I have new insurance starting in {month}. Where do I send the card information?",
        "My insurance denied the claim from {date}. What do I need to do?",
        "How much will a {visit_type} cost with my insurance?",
        "Do you accept the marketplace plans? I'm switching insurance.",
    ),
    # --- Spanish ------------------------------------------------------------------------
    T(
        "schedule",
        "front_desk",
        "Quisiera hacer una cita para {visit_reason_es}.",
        "Necesito cambiar mi cita del {day_es}.",
        "Por favor cancelen mi cita del {day_es}, no puedo ir.",
        "¿A qué hora abren los sábados?",
        language="es",
    ),
    T(
        "refill",
        "medical_assistant",
        "Necesito un resurtido de mi {med}.",
        "¿Pueden mandar la receta de {med} a mi farmacia?",
        language="es",
    ),
    T(
        "outside_records",
        "medical_assistant",
        "¿Pueden pedir mis expedientes de {hospital}?",
        "Necesito que manden mis expedientes médicos a {hospital}.",
        language="es",
    ),
    T(
        "refill",
        "medical_assistant",
        "Me hace falta mi medicina de {med}, ya casi no me queda.",
        "Ya se me acabaron las pastillas de {med}, ¿me pueden mandar la receta otra vez?",
        language="es",
        site="local",
    ),
    T(
        "symptoms",
        "nurse",
        "Tengo {symptom_es} desde hace {duration_es}. ¿Debo ir a la clínica?",
        "Mi hijo tiene fiebre de {temp} desde ayer pero está comiendo bien.",
        "Desde que empecé el {med} me siento mareado. ¿Es normal?",
        language="es",
    ),
    T(
        "chest_pain",
        "nurse",
        "Tengo dolor de pecho muy fuerte y me falta el aire.",
        "Mi esposo no puede hablar bien y tiene la cara caída de un lado.",
        urgency="urgent",
        language="es",
    ),
    T(
        "results",
        "physician",
        "Mis resultados de {lab} salieron en {value}. ¿Qué significa?",
        "Quisiera hablar con el doctor sobre cambiar mi {med}.",
        language="es",
    ),
    T(
        "bill",
        "billing",
        "Recibí una factura de {amount} y no entiendo por qué.",
        "¿Puedo hacer un plan de pagos para mi saldo de {amount}?",
        "Mi seguro no pagó la consulta del {date}.",
        language="es",
    ),
    T(
        "bill",
        "billing",
        "Me llegó un cobro de {amount} que no debo, ya había pagado todo.",
        "La cuenta que me mandaron está mal, me cobraron dos veces.",
        language="es",
        site="local",
    ),
)

SLOTS: dict[str, Sequence[str]] = {
    "med": (
        "lisinopril",
        "metformin",
        "atorvastatin",
        "levothyroxine",
        "amlodipine",
        "sertraline",
        "omeprazole",
        "losartan",
        "gabapentin",
        "albuterol",
        "escitalopram",
        "metoprolol",
        "hydrochlorothiazide",
        "montelukast",
    ),
    "count": ("two", "three", "five", "a week's worth of"),
    "pharmacy": (
        "my pharmacy on Main Street",
        "the pharmacy on Oak Avenue",
        "the mail-order pharmacy",
        "the pharmacy by my work",
    ),
    "lab": (
        "A1c",
        "cholesterol",
        "TSH",
        "vitamin D",
        "potassium",
        "creatinine",
        "PSA",
        "hemoglobin",
    ),
    "value": ("7.8", "240", "6.1", "18", "5.6", "1.4", "4.9", "10.2"),
    "doctor": (
        "Dr. Rivera",
        "Dr. Okafor",
        "Dr. Lindqvist",
        "Dr. Nakamura",
        "Dr. Haddad",
        "the doctor",
    ),
    "day": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "the 14th", "next Tuesday"),
    "time": ("9:15", "10:30", "1:45", "3:00", "4:20"),
    "date": ("March 3", "April 18", "June 9", "August 22", "October 5"),
    "month": ("January", "July", "the new year"),
    "amount": ("$45.20", "$120", "$310.75", "$89", "$1,240", "$25"),
    "visit_type": (
        "follow-up",
        "physical",
        "telehealth visit",
        "new patient visit",
        "lab appointment",
    ),
    "visit_reason": (
        "my yearly physical",
        "a blood pressure follow-up",
        "a well-child visit",
        "a medication check",
        "a skin check",
    ),
    "hospital": (
        "County General",
        "St. Brigid's Hospital",
        "Lakeside Medical Center",
        "Northgate Hospital",
        "the urgent care on 5th Street",
    ),
    "imaging_center": ("Riverside Imaging", "the imaging center downtown"),
    "specialty": (
        "cardiology",
        "dermatology",
        "orthopedics",
        "gastroenterology",
        "endocrinology",
        "physical therapy",
    ),
    "specialist": (
        "cardiologist",
        "dermatologist",
        "orthopedic surgeon",
        "GI doctor",
        "endocrinologist",
    ),
    "form": ("FMLA", "disability", "school medication", "DMV", "camp"),
    "school": ("my son's school", "my daughter's daycare", "the summer camp"),
    "symptom": (
        "sore throat",
        "cough",
        "rash on my arm",
        "headache",
        "stuffy nose",
        "upset stomach",
        "earache",
        "lower back pain",
        "sinus pressure",
    ),
    "duration": ("two days", "three days", "about a week", "since Sunday", "a few days"),
    "side_effect": (
        "dizzy",
        "nauseous",
        "really tired",
        "a dry cough",
        "some swelling in my ankles",
        "trouble sleeping",
    ),
    "procedure": (
        "my knee arthroscopy",
        "the mole removal",
        "my colonoscopy",
        "the biopsy",
        "my cyst removal",
    ),
    "child": ("son", "daughter", "toddler", "little one"),
    "temp": ("100.4", "100.8", "101"),
    "condition": ("knee arthritis", "gallbladder", "hernia", "carpal tunnel"),
    "bp": ("150/95", "148/92", "160/100"),
    "minutes": ("20", "30", "45"),
    "clinic": ("Eastside", "Main Street", "downtown"),
    "street": ("an apartment on Elm Street", "a new place across town"),
    "phone": ("555-0142", "555-0187", "555-0110"),
    "day_es": ("lunes", "martes", "miércoles", "jueves", "viernes"),
    "symptom_es": (
        "dolor de garganta",
        "tos",
        "un sarpullido",
        "dolor de cabeza",
        "dolor de estómago",
    ),
    "duration_es": ("dos días", "una semana", "tres días"),
    "visit_reason_es": ("mi examen anual", "un chequeo de la presión", "una consulta de control"),
}

SUBJECTS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "front_desk": ("Appointment", "Scheduling", "Question", "Office visit"),
        "medical_assistant": ("Refill request", "Records", "Referral", "Form", "Question"),
        "nurse": ("Question", "Not feeling well", "Medication question", "Help"),
        "physician": ("Question for the doctor", "Lab results", "Medication", "Question"),
        "billing": ("Billing question", "My bill", "Insurance", "Payment"),
    },
    "es": {"*": ("Pregunta", "Cita", "Ayuda", "Consulta")},
}
GREETINGS = {
    "en": ("Hi,", "Hello,", "Good morning,", "Hello there,"),
    "es": ("Hola,", "Buenos días,"),
}
SIGNOFFS = {
    "en": ("Thanks", "Thank you!", "Thanks so much", "- sent from my phone"),
    "es": ("Gracias", "Muchas gracias"),
}

TOPIC_WEIGHTS: dict[Site, dict[str, float]] = {
    "public": {
        "schedule": 9,
        "reschedule": 7,
        "cancel": 4,
        "logistics": 4,
        "portal": 3,
        "contact_update": 2,
        "vague": 3,
        "refill": 12,
        "outside_records": 4,
        "referral_status": 4,
        "forms": 3,
        "order_status": 3,
        "symptoms": 9,
        "side_effects": 5,
        "wound_care": 3,
        "vaccines": 2,
        "mixed_clinical": 2,
        "results": 6,
        "treatment_change": 4,
        "diagnosis": 3,
        "bill": 7,
        "payment": 3,
        "insurance": 3,
        "chest_pain": 1.2,
        "breathing": 1,
        "stroke": 0.8,
        "self_harm": 0.8,
        "bleeding": 0.8,
        "anaphylaxis": 0.6,
        "infant_fever": 0.5,
    },
    "local": {
        "schedule": 7,
        "reschedule": 6,
        "cancel": 3,
        "logistics": 3,
        "portal": 2,
        "contact_update": 2,
        "vague": 3,
        "refill": 10,
        "outside_records": 12,
        "referral_status": 5,
        "forms": 2,
        "order_status": 3,
        "symptoms": 8,
        "side_effects": 4,
        "wound_care": 2,
        "vaccines": 2,
        "mixed_clinical": 2,
        "results": 5,
        "treatment_change": 3,
        "diagnosis": 2,
        "bill": 7,
        "payment": 3,
        "insurance": 2,
        "chest_pain": 1,
        "breathing": 1,
        "stroke": 0.6,
        "self_harm": 0.6,
        "bleeding": 0.6,
        "anaphylaxis": 0.5,
        "infant_fever": 0.4,
    },
}
SITE_PROFILE: dict[Site, dict[str, float]] = {
    "public": {"spanish": 0.08, "sms": 0.2, "typo": 0.15},
    "local": {"spanish": 0.3, "sms": 0.4, "typo": 0.25},
}
AGE_BANDS: tuple[tuple[str, float], ...] = (("18-39", 0.3), ("40-64", 0.4), ("65+", 0.3))
_SLOT = re.compile(r"\{(\w+)\}")


def generate(n: int, *, site: Site, seed: int, id_prefix: str) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    profile = SITE_PROFILE[site]
    records = []
    for index in range(n):
        language = "es" if rng.random() < profile["spanish"] else "en"
        candidates = [t for t in TEMPLATES if t.language == language and t.site in ("all", site)]
        topics = sorted({t.topic for t in candidates})
        weights = [TOPIC_WEIGHTS[site].get(topic, 1.0) for topic in topics]
        topic = rng.choices(topics, weights)[0]
        template = rng.choice([t for t in candidates if t.topic == topic])
        channel = "sms" if rng.random() < profile["sms"] else "portal"

        body = _fill(rng.choice(template.bodies), rng)
        subject = ""
        if channel == "portal":
            if rng.random() < 0.6:
                body = f"{rng.choice(GREETINGS[language])} {body}"
            if rng.random() < 0.5:
                body = f"{body}\n{rng.choice(SIGNOFFS[language])}"
            subjects = SUBJECTS[language]
            subject = rng.choice(subjects.get(template.destination) or subjects["*"])
        else:
            body = _texting_style(body, rng)
        if rng.random() < profile["typo"]:
            body = _typo(body, rng)

        bands, band_weights = [b for b, _ in AGE_BANDS], [w for _, w in AGE_BANDS]
        age_band = "0-17" if template.pediatric else rng.choices(bands, band_weights)[0]
        message: dict[str, Any] = {"body": body, "channel": channel}
        if subject:
            message["subject"] = subject
        records.append(
            {
                "id": f"{id_prefix}-{index:05d}",
                "input": {
                    "message": message,
                    "patient": {"age_band": age_band, "preferred_language": language},
                },
                "labels": {"destination": template.destination, "urgency": template.urgency},
                "attributes": {
                    "site": site,
                    "language": language,
                    "channel": channel,
                    "age_band": age_band,
                    "topic": template.topic,
                },
            }
        )
    return records


def _fill(text: str, rng: random.Random) -> str:
    return _SLOT.sub(lambda match: rng.choice(SLOTS[match.group(1)]), text)


def _texting_style(text: str, rng: random.Random) -> str:
    text = text.lower()
    replacements = ((" you ", " u "), ("please", "pls"), (" are ", " r "), ("thanks", "thx"))
    for old, new in replacements:
        if rng.random() < 0.5:
            text = text.replace(old, new)
    return text.rstrip(".")


def _typo(text: str, rng: random.Random) -> str:
    words = text.split(" ")
    eligible = [i for i, word in enumerate(words) if len(word) > 4 and word.isalpha()]
    for index in rng.sample(eligible, k=min(len(eligible), rng.choice((1, 2)))):
        word = words[index]
        position = rng.randrange(len(word) - 1)
        words[index] = word[:position] + word[position + 1] + word[position] + word[position + 2 :]
    return " ".join(words)


def public_splits(seed: int = 7) -> dict[str, list[dict[str, Any]]]:
    records = generate(2400, site="public", seed=seed, id_prefix="pub")
    return {"train": records[:1500], "calibration": records[1500:1800], "test": records[1800:]}


def local_site(seed: int = 11, n: int = 400) -> list[dict[str, Any]]:
    return generate(n, site="local", seed=seed, id_prefix="local")
