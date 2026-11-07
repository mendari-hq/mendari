# SPDX-License-Identifier: Apache-2.0
"""A harder synthetic benchmark for the Patient Inbox Router. Draft, not clinically reviewed.

Entirely fictional: no PHI. Local, seeded and deterministic: no network and no model calls, so
the same seed writes the same bytes.

The shipped benchmark (`synthetic_inbox`) draws its train and test splits from one template
bank, so its test split repeats the sentences its model was trained on and overstates accuracy.
This draft is built so that its held-out split shares nothing that a model or a rule could have
learned or been tuned on:

* **Families.** A family is one way of asking for one thing, with its labels: a few frames
  (sentence skeletons with slots), written for this set and not copied from `synthetic_inbox`.
  Each family belongs to one split. The critical label (`urgency=urgent`) has a family in each
  split for every sign the pack's `urgent` criterion lists, and look-alike families (past,
  negated or figurative red-flag words) are routine.
* **Paraphrase sources.** Everything that varies a frame comes in two disjoint banks, one per
  split, each with its own id: the slot values, hedges, mixed-topic asides, greetings,
  sign-offs, subjects, texting style and the typo operators.
* **The leakage check** (`leakage_check`) proves the separation on what was generated: no
  family and no source in both splits, no case filed under the other split's family, no
  sentence of 16 characters or more and no run of 6 words shared by the two splits, none shared
  by the held-out split and the shipped pack's training and tuning data or templates, and none
  shared by the train split and the shipped held-out red-flag set.
* **The coverage check** (`coverage_problems`) proves every label of every question, every
  critical label in each language, every red-flag sign and every frame is in each split.

`scripts/harder_inbox_draft.py` writes the draft to `datasets/drafts/patient-inbox-harder/`
(and `--check` proves the committed files are this module's). How the owner reviews it, and
what step would adopt it as the pack's benchmark:
`notes/clinician-review-harder-inbox-benchmark.md`. Until the owner records that review, nothing
here may be described as reviewed, and the pack's benchmark stays the shipped one.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mendari.sandbox import synthetic_inbox

Split = Literal["train", "heldout"]
Records = Sequence[Mapping[str, Any]]
SPLITS: tuple[Split, ...] = ("train", "heldout")
REVIEW_STATUS = "Draft, not clinically reviewed"
VERSION = "harder-inbox/1"
DEFAULT_SEED = 20261107
PACK_NAME = "patient-inbox-router"
DRAFT_DIR = Path("datasets") / "drafts" / "patient-inbox-harder"
FILES = ("train.jsonl", "heldout.jsonl", "REPORT.md", "FAMILIES.md")
LANGUAGES = ("en", "es")

# The signs the pack's `urgent` criterion lists, in its order (the inbox pack's pack.yaml).
RED_FLAGS: dict[str, str] = {
    "chest_pain": "chest pain or pressure",
    "breathing": "trouble breathing",
    "stroke": "stroke signs (face drooping, arm weakness, slurred speech)",
    "self_harm": "thoughts of suicide or self-harm, or an intentional overdose",
    "bleeding": "heavy bleeding, vomiting or coughing up blood, bleeding in pregnancy",
    "anaphylaxis": "a severe allergic reaction",
    "infant_fever": "fever in a baby under 3 months",
    "altered": "hard to wake, fainting or newly confused",
    "head_injury": "a head injury followed by vomiting or confusion",
    "severe_pain": "sudden severe pain in the head or belly",
}
# An aside may join a family only when a less specialised team would handle it alone, so the
# family's label stays right: "choose the most clinically qualified team needed" (the pack's
# `destination` instructions). Two administrative requests for different teams are never mixed.
TEAM_RANK = {"front_desk": 0, "billing": 0, "medical_assistant": 1, "nurse": 2, "physician": 2}
# Cases per family, by kind and language.
COUNTS: Mapping[tuple[str, str], int] = {
    ("urgent", "en"): 12,
    ("urgent", "es"): 8,
    ("routine", "en"): 16,
    ("routine", "es"): 8,
    ("look-alike", "en"): 10,
    ("look-alike", "es"): 8,
}
HEDGED = ("nurse", "physician")  # a hedge softens a clinical message only
P_HEDGE = {"urgent": 0.5, "routine": 0.3, "look-alike": 0.3}
P_ASIDE = 0.3
P_SMS = 0.3
P_OPENER = 0.5
P_CLOSER = 0.4
P_SUBJECT = 0.7
P_TYPO = 0.3
P_TEXTING_SWAP = 0.6
ADULT_BANDS = (("18-39", 0.3), ("40-64", 0.4), ("65+", 0.3))
PEDIATRIC_SLOTS = frozenset(
    {"child", "older_child", "teen_girl", "baby_age", "baby_weeks", "nino_es"}
)
# The leakage check's text units: a sentence of at least this many characters once folded, and
# a run of this many words within a sentence.
SENTENCE_CHARS = 16
RUN_WORDS = 6
# The coverage check's minimums, per split.
MIN_PER_LABEL = 20
MIN_CRITICAL = 100
MIN_PER_SIGN = 10


@dataclass(frozen=True)
class Family:
    split: Split
    language: str
    topic: str
    destination: str
    urgency: str
    how: str
    frames: tuple[str, ...]
    red_flag: str | None = None
    mimics: tuple[str, ...] = ()
    pediatric: bool = False

    @property
    def id(self) -> str:
        return f"{self.split}.{self.language}.{self.topic}"

    @property
    def kind(self) -> str:
        if self.urgency == "urgent":
            return "urgent"
        return "look-alike" if self.mimics else "routine"


def _urgent(
    split: Split, language: str, sign: str, how: str, *frames: str, pediatric: bool = False
) -> Family:
    return Family(
        split, language, sign, "nurse", "urgent", how, frames, red_flag=sign, pediatric=pediatric
    )


def _routine(
    split: Split,
    language: str,
    topic: str,
    destination: str,
    how: str,
    *frames: str,
    mimics: tuple[str, ...] = (),
    pediatric: bool = False,
) -> Family:
    return Family(
        split, language, topic, destination, "routine", how, frames, mimics=mimics,
        pediatric=pediatric,
    )  # fmt: skip


FAMILIES: tuple[Family, ...] = (
    # == train, English, possible emergencies =================================================
    _urgent(
        "train", "en", "chest_pain",
        "pressure told as a weight, a band or someone standing on the chest, without the words "
        "chest pain",
        "There's a heavy weight sitting on the middle of my chest and it hasn't let up for "
        "{minutes} minutes.",
        "My chest feels squeezed, like a band being pulled tight, and now my left shoulder aches "
        "too.",
        "{man} keeps pressing a fist against his breastbone and he's sweaty and grey.",
        "It feels like someone is standing on my chest. Started while I was carrying the laundry "
        "upstairs.",
    ),
    _urgent(
        "train", "en", "breathing",
        "air hunger told by what the person cannot do (finish a sentence, get relief from a "
        "nebulizer), rarely by the word breathe",
        "I can only get out a few words before I have to stop and pull in more air.",
        "My {child} is sucking in under the ribs with every breath and the nebulizer did nothing.",
        "Every breath feels like pulling air through a straw and it's getting worse by the minute.",
        "{woman} is wheezing so loud I can hear it from the next room and she can't finish a "
        "sentence.",
    ),
    _urgent(
        "train", "en", "stroke",
        "a lopsided smile, a hand that stops working, words that come out wrong, told as what "
        "was noticed",
        "{man}'s smile went lopsided all of a sudden and his words are coming out mushy.",
        "My left hand just stopped working right, I keep dropping things, and half my face feels "
        "heavy.",
        "{woman} was fine at breakfast and now she can't get the right words out and one side of "
        "her mouth sags.",
        "Out of nowhere half my vision is gone on one side and my right leg is dragging.",
    ),
    _urgent(
        "train", "en", "self_harm",
        "intent stated without the words suicide or kill: a plan, pills counted out, not being "
        "safe tonight, a handful swallowed",
        "I've decided I'm done. I have the pills counted out and I don't see a reason to keep "
        "going.",
        "Lately I keep thinking everyone would be better off without me, and tonight it feels "
        "like a plan.",
        "I swallowed a handful of my {med} about an hour ago because I wanted everything to stop.",
        "I don't think I can keep myself safe tonight.",
    ),
    _urgent(
        "train", "en", "bleeding",
        "bleeding told by what it soaks or how it spurts, as coffee grounds, or on a blood "
        "thinner; pregnancy implied by weeks along",
        "I'm {weeks_preg} weeks along and I'm passing clots and soaking through a pad every hour.",
        "The cut on my thigh spurts every time my heart beats, the towel is soaked through.",
        "I've been throwing up what looks like coffee grounds since lunch.",
        "My nose has been gushing for over half an hour, pinching it does nothing, and I'm on "
        "{thinner}.",
    ),
    _urgent(
        "train", "en", "anaphylaxis",
        "swelling and welts after a trigger, told as a funny voice, puffy lips, a tongue too big "
        "or trouble swallowing",
        "Ate a cookie that must have had {allergen} in it and now my voice sounds funny and my "
        "lips are puffing up.",
        "Got stung by a wasp {minutes} minutes ago, I'm covered in welts and swallowing "
        "feels difficult.",
        "Right after the first dose of {antibiotic} my tongue started to feel too big for my "
        "mouth.",
        "My {child} broke out in welts after eating {allergen} and is coughing, and their face is "
        "getting puffy.",
    ),
    _urgent(
        "train", "en", "infant_fever",
        "the baby's age as an adjective (a five-week-old), the temperature in Fahrenheit or "
        "Celsius, with poor feeding or sleepiness",
        "Our {baby_age} baby feels hot, the thermometer says {infant_temp}, and she isn't "
        "interested in feeding.",
        "My {baby_age} son is burning up at {infant_temp} and is much sleepier than usual.",
        "Took our {baby_age}'s temperature and it read {infant_temp}. Is that a problem at this "
        "age?",
        pediatric=True,
    ),
    _urgent(
        "train", "en", "altered",
        "new confusion, a blackout or drowsiness told as behaviour (does not know the year, woke "
        "up on the floor, drifts off)",
        "{woman} doesn't know what year it is and keeps asking where she is, this started today.",
        "I blacked out in the kitchen and woke up on the floor, I don't know how long I was down.",
        "{man} is so drowsy I have to shake him to get an answer, and then he drifts off again.",
    ),
    _urgent(
        "train", "en", "head_injury",
        "a fall or knock to the head followed by vomiting, queasiness or a memory gap",
        "My {child} fell off the monkey bars and smacked the back of the head, and has thrown up "
        "twice since.",
        "I slipped on ice and cracked my head on the step, now I'm queasy and can't remember the "
        "last hour.",
        "{man} hit his head getting out of the car and now he's vomiting and seems foggy.",
    ),
    _urgent(
        "train", "en", "severe_pain",
        "sudden severe head or belly pain told by its onset and what it stops the person doing",
        "Out of nowhere the worst headache I've ever had hit me, like a thunderclap, about an "
        "hour ago.",
        "My stomach suddenly hurts so much I can't stand up straight, and it feels hard as a "
        "board.",
        "Sudden stabbing pain low on my right side, I'm doubled over and can't walk.",
    ),
    # == train, English, routine =============================================================
    _routine(
        "train", "en", "schedule", "front_desk",
        "booking, moving and cancelling, told around the person's week",
        "Is there any chance of a {visit_type} with {doctor} on {day}? I work nights so late "
        "morning is ideal.",
        "Something came up and I won't make it on {day} at {time}. Could you find me another "
        "slot?",
        "Please take me off the schedule for {day}, I'll reach out when things settle down.",
        "I'd like to get on the books for a {visit_type}, whatever is soonest.",
    ),
    _routine(
        "train", "en", "logistics", "front_desk",
        "entrances, parking, hours, the portal and contact details",
        "Which entrance do I use for the lab, and is the parking garage validated?",
        "My portal password reset link never arrives. Can someone unlock my account?",
        "Do you have evening hours during the week, or only weekends?",
        "I changed jobs and my cell number is different now. Who do I give it to?",
    ),
    _routine(
        "train", "en", "refill", "medical_assistant",
        "routine refills and renewals, told by what the pharmacy said or how many are left",
        "Down to my last few {med} tablets. Can a new prescription go to {pharmacy}?",
        "{pharmacy_cap} says the {med} script expired and they need your office to okay it.",
        "Could I get a three month supply of my {med} this time instead of thirty days?",
        "My {med} renewal has been pending for a week, is it waiting on something from me?",
    ),
    _routine(
        "train", "en", "records", "medical_assistant",
        "records to send or receive, vaccine history, a form needing clinical dates, a lost "
        "referral",
        "Please send my last two years of visit notes to the {specialty} clinic at {hospital}.",
        "I need a copy of my vaccine history for a new job. Can you upload it to the portal?",
        "My employer needs the {form} completed with my diagnosis dates. Where should I drop it "
        "off?",
        "The {specialty} office keeps telling me they never got the referral. Can you resend it?",
    ),
    _routine(
        "train", "en", "symptoms", "nurse",
        "mild, new or lingering symptoms with a question about being seen",
        "I've had a {symptom} for {duration}. It isn't getting better, should someone take a "
        "look?",
        "Is a {symptom} that comes and goes for {duration} worth an appointment, or should I "
        "wait?",
        "Woke up with a {symptom} again today. I'm eating and drinking fine, just wondering what "
        "to try.",
    ),
    _routine(
        "train", "en", "side_effects", "nurse",
        "a side effect of a medication, asked about with its timing",
        "Ever since the {med} was started I've had {side_effect}. Is that something that settles "
        "down?",
        "I'm getting {side_effect} on the {med}. Should I keep taking it until my next visit?",
        "Is {side_effect} a known thing with {med}? It started a few days after the first dose.",
    ),
    _routine(
        "train", "en", "post_procedure", "nurse",
        "stitches, healing and vaccines after a procedure or a shot",
        "The stitches from {procedure} are itchy and a bit pink. How do I keep them clean?",
        "How long after {procedure} can I go swimming?",
        "My arm is sore and warm where I got my shot yesterday. Is ice okay?",
        "Am I due for a pneumonia vaccine at my age, and can I get it with the flu shot?",
    ),
    _routine(
        "train", "en", "results", "physician",
        "a result to interpret, with the value and what it means for treatment",
        "The portal shows {lab_result} with a little red flag next to it. What does that mean "
        "for me?",
        "Could {doctor} look at my latest labs and tell me if {lab_result} changes anything?",
        "Last year my numbers were fine, now it says {lab_result}. Do we need to do something "
        "about it?",
    ),
    _routine(
        "train", "en", "treatment", "physician",
        "a dose change, stopping or switching a drug, or the physician's opinion on surgery",
        "I'd like {doctor}'s opinion on whether I still need the {med} now that I've changed my "
        "diet.",
        "My blood pressure at home keeps running high on the {bp_med}. Is it time to go up on "
        "the dose?",
        "Would {doctor} consider switching me off {med}? I'd rather try something with fewer "
        "pills.",
        "Is the surgery my orthopedist suggested really necessary, or are there other options to "
        "try first?",
    ),
    _routine(
        "train", "en", "bill", "billing",
        "a charge thought covered, a statement, a double bill",
        "There's a charge of {amount} from {date} that I think insurance already paid. Can you "
        "check?",
        "Why does my statement show {amount} for a {visit_type}? I was told it was covered.",
        "I was billed twice for the same visit on {date}.",
    ),
    _routine(
        "train", "en", "insurance", "billing",
        "a new plan, a cost estimate, paying a balance over time",
        "My plan changes on the first of the month. How do I get you the new card?",
        "Can I get an estimate for a {visit_type} before I book it? I have a high deductible.",
        "I'd like to pay off {amount} over a few months. Is that possible?",
    ),
    # == train, English, look-alikes (routine) ===============================================
    _routine(
        "train", "en", "past_assessed", "medical_assistant",
        "a past emergency already assessed, asked about as paperwork",
        "I was in the ER at {hospital} last month for chest pressure, they said it was my "
        "muscles. Can you get those notes for my file?",
        "After the stroke scare in the spring, the neurologist at {hospital} cleared me. Please "
        "request the report.",
        "Please add the discharge summary from my asthma admission in {month} to my chart.",
        mimics=("chest_pain", "stroke", "breathing"),
    ),
    _routine(
        "train", "en", "negated", "nurse",
        "red-flag words plainly negated, the actual complaint mild",
        "No chest pressure and I'm breathing fine, just a {symptom} that won't quit.",
        "I'm not short of breath at all, the cough is just keeping me up at night.",
        "Not having any trouble talking or moving my arms, only a {symptom} for {duration}.",
        mimics=("chest_pain", "breathing", "stroke"),
    ),
    _routine(
        "train", "en", "idiom", "billing",
        "red-flag words used as figures of speech about a bill",
        "This bill for {amount} nearly gave me a heart attack. Can someone explain it?",
        "The copays are killing me. Is there a payment plan?",
        "I about died when I saw the {amount} charge from {date}. What is it for?",
        mimics=("chest_pain", "self_harm"),
    ),
    _routine(
        "train", "en", "education", "nurse",
        "questions about warning signs, asked ahead of time",
        "My dad just got diagnosed with high blood pressure. What stroke warning signs should we "
        "watch for?",
        "What should I do if my son ever has an allergic reaction at school? He's allergic to "
        "{allergen}.",
        "How do I know when a fever is high enough to call you?",
        mimics=("stroke", "anaphylaxis", "infant_fever"),
    ),
    _routine(
        "train", "en", "older_child_fever", "nurse",
        "fever in a child old enough to be past the infant rule, drinking and playing",
        "My {older_child} has had a fever of {child_temp} since last night but is drinking and "
        "playing. Do we need to come in?",
        "Our {older_child} felt warm after daycare, {child_temp}, but is acting like normal.",
        mimics=("infant_fever",),
        pediatric=True,
    ),
    _routine(
        "train", "en", "minor_injury", "nurse",
        "a small cut or bump that has already settled",
        "I nicked my finger chopping onions, it stopped bleeding after a minute of pressure. Do I "
        "need a tetanus shot?",
        "My {older_child} bumped their head on the table this morning, cried for a minute and "
        "has been totally normal since. Anything to watch for?",
        "Small scrape on my knee from a bike fall, it's clean and scabbing. Should I cover it?",
        mimics=("bleeding", "head_injury"),
    ),
    # == train, Spanish =======================================================================
    _urgent(
        "train", "es", "chest_pain",
        "a weight or squeeze in the chest with cold sweat or arm pain",
        "Siento un peso enorme en el pecho desde hace {minutes} minutos y estoy sudando frío.",
        "Me aprieta el pecho como si me lo exprimieran y me duele el brazo izquierdo.",
    ),
    _urgent(
        "train", "es", "breathing",
        "not enough air to talk, a child pulling in at the ribs",
        "No me alcanza el aire para hablar, tengo que parar a cada rato.",
        "Mi {nino_es} respira muy rápido y se le hunden las costillas.",
    ),
    _urgent(
        "train", "es", "stroke",
        "a twisted mouth and tangled speech, a hand that will not work",
        "A {hombre_es} se le torció la boca de repente y habla enredado.",
        "De la nada no puedo mover bien la mano derecha y se me duerme la cara.",
    ),
    _urgent(
        "train", "es", "self_harm",
        "not wanting to go on with pills ready, fear of acting on thoughts of self-harm",
        "Ya no quiero seguir viviendo y tengo las pastillas listas.",
        "Pienso todo el tiempo en hacerme daño y hoy tengo miedo de hacerlo.",
    ),
    _routine(
        "train", "es", "schedule", "front_desk",
        "asking for or moving an appointment",
        "¿Tienen algún espacio para una consulta el {dia_es} en la mañana?",
        "No voy a poder llegar a mi cita del {dia_es}, ¿me la pueden cambiar?",
    ),
    _routine(
        "train", "es", "refill", "medical_assistant",
        "a refill running out, the pharmacy waiting on the office",
        "Ya casi se me termina el {med}, ¿pueden enviar otra receta a la farmacia?",
        "En la farmacia dicen que necesitan autorización de ustedes para mi {med}.",
    ),
    _routine(
        "train", "es", "symptoms", "nurse",
        "a mild symptom that will not go away",
        "Tengo {sintoma_es} desde hace {duracion_es} y no se me quita. ¿Necesito cita?",
        "Desde el {dia_es} ando con {sintoma_es}, ¿qué me recomiendan?",
    ),
    _routine(
        "train", "es", "results", "physician",
        "a result to interpret and whether treatment changes",
        "Me salió {resultado_es} en los análisis. ¿Qué quiere decir y tengo que cambiar algo?",
        "Quisiera que el doctor me explique si mis resultados cambian mi tratamiento.",
    ),
    _routine(
        "train", "es", "bill", "billing",
        "a charge the insurance should have paid, a bill to explain",
        "Me cobraron {amount} por una consulta que mi seguro debía cubrir.",
        "¿Me pueden explicar la cuenta de {amount} que me mandaron?",
    ),
    _routine(
        "train", "es", "past_assessed", "medical_assistant",
        "a past emergency visit already assessed, asked about as paperwork",
        "Estuve en emergencias en {hospital} por un dolor de pecho y me dijeron que no era del "
        "corazón. ¿Pueden pedir esos papeles?",
        "Hace un mes me revisaron en urgencias por falta de aire y salió todo bien. Necesito el "
        "reporte para mi expediente.",
        mimics=("chest_pain", "breathing"),
    ),
    # == held-out, English, possible emergencies ==============================================
    _urgent(
        "heldout", "en", "chest_pain",
        "chest discomfort as an ache, a vise or a burning squeeze, with the spread, clamminess "
        "or pallor that goes with it",
        "Weird ache in the center of my chest that runs up into my jaw, it started {minutes} "
        "minutes ago and I'm clammy.",
        "{man} says his chest feels like it's caught in a vise and he has gone pale.",
        "Something is pressing down hard behind my sternum and my left arm feels heavy and odd.",
        "This burning squeeze across my chest is worse than any heartburn I've had, and it's "
        "still going.",
    ),
    _urgent(
        "heldout", "en", "breathing",
        "breathing trouble told as panting, gulping air, grey lips or an inhaler that does not "
        "help",
        "I'm working so hard to get air in that I can't lie down, I'm panting just sitting here.",
        "My {child} has gone grey around the lips and is breathing fast and noisy.",
        "Air won't go all the way in no matter what I do, and my rescue inhaler isn't touching "
        "it.",
        "{man} keeps stopping mid-word to gulp air and his chest is heaving.",
    ),
    _urgent(
        "heldout", "en", "stroke",
        "speech like being drunk, a mouth corner that will not move, a cup that cannot be held, "
        "an uneven face",
        "{woman} is talking like she's had too much to drink, but she hasn't touched any, and "
        "her arm keeps flopping down.",
        "One corner of my mouth won't move and my tongue feels thick, it came on within the last "
        "half hour.",
        "{man} is suddenly mixing up his words and can't hold his coffee cup in his right hand.",
        "I just looked in the mirror and my face is uneven, and the arm on that side is tingly "
        "and weak.",
    ),
    _urgent(
        "heldout", "en", "self_harm",
        "goodbye letters, saving pills, a teenager's disclosure, not wanting to wake up, extra "
        "pills taken on purpose",
        "I wrote goodbye letters to my kids today and I keep driving past the bridge near work.",
        "I just want to go to sleep and not wake up, and I've been saving up my {med} for that.",
        "My {teen_girl} told me she has been cutting herself and wishes she were dead.",
        "No point in any of this anymore. I took extra {med} on purpose a little while ago.",
        "I keep picturing how I would do it and tonight I'm scared I actually might.",
    ),
    _urgent(
        "heldout", "en", "bleeding",
        "bleeding in pregnancy implied by expecting, a hand soaking towels, black stools with "
        "near-fainting, dark red vomit, a long nosebleed on a blood thinner",
        "Expecting our second baby, about {weeks_preg} weeks, and all of a sudden there's a lot "
        "of bright red blood.",
        "{man} sliced his hand open on a broken glass and it's pouring through two dish towels.",
        "My stools have turned black and sticky and I nearly went down on the bathroom floor.",
        "I keep bringing up dark red stuff when I vomit and I feel weak.",
        "My nosebleed has gone on for forty minutes, it's running down my throat, and I take "
        "{thinner}.",
    ),
    _urgent(
        "heldout", "en", "anaphylaxis",
        "a thick throat, eyes swelling shut, blotches with gagging, numb lips after a new drug, "
        "a food or an injection",
        "My mouth is tingling and my throat feels thick ever since I took the new {antibiotic}, "
        "and I'm wheezy.",
        "After the {allergen} at dinner my eyes are swelling shut and I'm getting dizzy.",
        "My {child} got into a granola bar with {allergen} and now has red blotches everywhere "
        "and keeps gagging.",
        "Itchy bumps popped up all over after my injection and now my lips are numb and I feel "
        "faint.",
    ),
    _urgent(
        "heldout", "en", "infant_fever",
        "the baby's age in weeks spelt out or as weeks since birth, temperatures in Fahrenheit "
        "and Celsius, with not latching or falling asleep feeding",
        "{infant_temp} on the rectal thermometer for our little one, who was born {baby_weeks} "
        "weeks ago, and she won't latch.",
        "My daughter is only {baby_weeks} weeks and feels warm all over, reading {infant_temp}, "
        "and she's barely eating.",
        "The baby turned {baby_weeks} weeks yesterday and is running hot, {infant_temp}, and "
        "keeps falling asleep mid-bottle.",
        pediatric=True,
    ),
    _urgent(
        "heldout", "en", "altered",
        "being out of it, hard to rouse, going down in the shower or limp, told by a relative",
        "{man} has been out of it all afternoon, calling me by my mother's name, and he's never "
        "done that.",
        "I went down in the shower this morning, everything went black, and my partner found me "
        "on the floor.",
        "{woman} took a nap and we can barely rouse her, she mumbles and goes right back under.",
        "My {child} is limp and floppy and won't really respond to me.",
    ),
    _urgent(
        "heldout", "en", "head_injury",
        "a tumble, a beam or a counter, then being sick, sleepy, muddled or repeating questions",
        "Our {child} tumbled down the stairs and landed head first, and now keeps being sick and "
        "is extra sleepy.",
        "I walked into a low beam at full speed this morning and since then I've thrown up and "
        "feel muddled.",
        "{woman} took a fall and knocked her head on the counter, now she keeps asking the same "
        "questions over and over.",
    ),
    _urgent(
        "heldout", "en", "severe_pain",
        "something bursting in the head, a belly too painful to touch, ten out of ten all at once",
        "It felt like something burst inside my head while I was lifting weights, and the pain "
        "is unbearable.",
        "My belly hurts so much I'm curled up on the floor, it came on suddenly and I can't even "
        "touch it.",
        "{man} is lying on the floor holding his stomach, says it's ten out of ten and it started "
        "all at once.",
    ),
    # == held-out, English, routine ==========================================================
    _routine(
        "heldout", "en", "schedule", "front_desk",
        "asking for room, bailing on a visit, dropping one, setting one up loosely",
        "Would {doctor} have room for me on {day}? Any time after lunch is fine.",
        "I have to bail on my {time} visit on {day}, my kid's school called. What else is "
        "open?",
        "Go ahead and drop my {visit_type} on {day}, I'm out of town that week.",
        "Need to set up a {visit_type} in the next month or so, I'm flexible.",
    ),
    _routine(
        "heldout", "en", "logistics", "front_desk",
        "holiday closures, the app, where an X-ray is, a new email",
        "Is the clinic closed for the holiday on Monday?",
        "The app logs me out every time I open my messages. Who can help with that?",
        "Where do I go for the X-ray, the main building or the annex?",
        "Please put my new email on file, the old one bounces.",
    ),
    _routine(
        "heldout", "en", "refill", "medical_assistant",
        "running out, the pharmacy waiting, the usual dose, mail order",
        "I'll run out of {med} on the weekend. Can you send more to {pharmacy}?",
        "{pharmacy_cap} keeps saying they're waiting on you for my {med}.",
        "Requesting my usual {med}, same dose, I have two left.",
        "Can the {med} be sent as a 90 day mail order going forward?",
    ),
    _routine(
        "heldout", "en", "records", "medical_assistant",
        "old results to a new doctor, proof of shots, a form's history section, a referral "
        "that went quiet",
        "My new {specialty} doctor wants my old test results. Can you fax them over to "
        "{hospital}?",
        "I need proof of my shots for college. Is there a printout you can give me?",
        "Can someone fill out the {form} for me? It needs my medical history section done.",
        "Still no call from {specialty} about the referral, could you check it went through?",
    ),
    _routine(
        "heldout", "en", "symptoms", "nurse",
        "an annoying, lingering or mild symptom with no fever",
        "Had a {symptom} for {duration} now and it's annoying more than anything. Anything I can "
        "do at home?",
        "My {symptom} has hung around for {duration}. Do I need to come in?",
        "Mild {symptom} that started {duration} ago, no fever. Should I just ride it out?",
    ),
    _routine(
        "heldout", "en", "side_effects", "nurse",
        "a side effect with the time of day, the timing of the dose, or whether to stop",
        "The new {med} gives me {side_effect} most afternoons. Should I expect that?",
        "Can I take the {med} at night instead? It's causing {side_effect}.",
        "Been on {med} two weeks and the {side_effect} isn't going away. Should I stop?",
    ),
    _routine(
        "heldout", "en", "post_procedure", "nurse",
        "fluid from a site, lifting after a procedure, a shot after a cold, a scab",
        "A little clear fluid is coming from where they did {procedure}. Normal?",
        "When can I lift my toddler again after {procedure}?",
        "Is it okay to get the shingles shot if I just had a cold?",
        "What should I put on the scab from {procedure}?",
    ),
    _routine(
        "heldout", "en", "results", "physician",
        "a value seen this morning, whether to repeat it, a value outside the printed range",
        "Saw {lab_result} on my results this morning. Is that bad, and does it change my "
        "treatment?",
        "What should I make of {lab_result}? Would {doctor} want to repeat it?",
        "The lab printed {lab_result} and marked it outside the range. What happens next?",
    ),
    _routine(
        "heldout", "en", "treatment", "physician",
        "lowering or stopping a drug, a relative's different medicine, whether to see a surgeon",
        "Could we try lowering my {med}? I've been feeling great and would like fewer meds.",
        "{doctor}, what do you think about stopping the {med} for a while?",
        "My sister got a different medicine for the same thing. Should I switch from {med}?",
        "Is it worth seeing a surgeon for my knee, or would you keep treating it with therapy?",
    ),
    _routine(
        "heldout", "en", "bill", "billing",
        "a statement not understood, a charge on a covered visit, a copay charged twice",
        "Got a statement for {amount} for {date} and I don't understand what it's for.",
        "Why am I being charged {amount} when my plan covers preventive visits?",
        "My card was charged two times for the copay. I'd like one refunded.",
    ),
    _routine(
        "heldout", "en", "insurance", "billing",
        "a new county plan, a denied claim, the price without insurance",
        "Do you take the new county health plan? I'm switching next month.",
        "My claim from {date} was denied. What do I do next?",
        "How much would a {visit_type} run without insurance?",
    ),
    # == held-out, English, look-alikes (routine) ============================================
    _routine(
        "heldout", "en", "past_assessed", "medical_assistant",
        "a past hospital stay, urgent care visit or cardiac test already assessed, asked about "
        "as paperwork",
        "Last winter I spent a night at {hospital} for a blood clot in my lung. Could you get "
        "that record for my new specialist?",
        "Two years ago I went to urgent care for a bad allergic reaction. I need the visit notes "
        "sent to my new allergist.",
        "The cardiologist at {hospital} did a stress test after my chest pain in {month} and it "
        "was normal. Please pull in the results.",
        mimics=("breathing", "anaphylaxis", "chest_pain"),
    ),
    _routine(
        "heldout", "en", "negated", "nurse",
        "red-flag words negated in other ways, the actual complaint mild",
        "Breathing is totally normal and there's no pain in my chest, I just feel run down with "
        "a {symptom}.",
        "To be clear, nothing like the stroke signs from the TV ads, just a {symptom} that won't "
        "go away.",
        "No blood in anything and no fever, but my stomach has been unsettled for {duration}.",
        mimics=("breathing", "chest_pain", "stroke", "bleeding"),
    ),
    _routine(
        "heldout", "en", "idiom", "billing",
        "other figures of speech about a bill: dying to know, breath taken away, a heart that "
        "sank",
        "I'm dying to know why this {amount} bill showed up, I paid in full.",
        "Opening this statement took my breath away, {amount} for a ten minute visit?",
        "My heart sank when I saw the {amount} charge from {date}. Can you explain it?",
        mimics=("self_harm", "breathing", "chest_pain"),
    ),
    _routine(
        "heldout", "en", "education", "nurse",
        "warning signs to teach a sitter, bleeding on a blood thinner, a newborn's temperature "
        "before the birth",
        "What are the signs of a serious allergic reaction I should teach the babysitter?",
        "My mom is starting blood thinners. What kind of bleeding should make us call?",
        "At what temperature should I worry with a newborn? We're expecting in {month}.",
        mimics=("anaphylaxis", "bleeding", "infant_fever"),
    ),
    _routine(
        "heldout", "en", "older_child_fever", "nurse",
        "fever after a vaccine or with a cold in an older child who is eating and playing",
        "My {older_child} spiked {child_temp} after the vaccine yesterday but is eating and "
        "running around. Is that normal?",
        "Our {older_child} has a fever around {child_temp} and a runny nose, still playing as "
        "usual. Should we be seen?",
        mimics=("infant_fever",),
        pediatric=True,
    ),
    _routine(
        "heldout", "en", "minor_injury", "nurse",
        "a paper cut, a bump on the forehead, a bruise, each already settled",
        "Paper cut on my thumb bled for a few seconds, it's fine now, but it looks a little red. "
        "Should I put anything on it?",
        "My {older_child} bonked their forehead on the swing, has a small bump, and is acting "
        "completely normal. Should we ice it?",
        "Bruise on my shin from the coffee table, no swelling. How long do these take to fade?",
        mimics=("bleeding", "head_injury"),
    ),
    # == held-out, Spanish ====================================================================
    _urgent(
        "heldout", "es", "chest_pain",
        "an odd pressure rising to the jaw, a burning squeeze with pallor and sweat",
        "Tengo una presión rara en medio del pecho que me sube a la mandíbula y no se me quita.",
        "A {hombre_es} le arde y le aprieta el pecho, está pálido y sudado.",
    ),
    _urgent(
        "heldout", "es", "breathing",
        "struggling to pull in air, an inhaler that does nothing, a relative panting",
        "Me cuesta muchísimo jalar aire y el inhalador no me hace nada.",
        "{mujer_es} está jadeando y no puede terminar una frase.",
    ),
    _urgent(
        "heldout", "es", "stroke",
        "words that will not come with a weak arm, a crooked face with a dragging leg",
        "De pronto a mi abuela no le salen las palabras y tiene el brazo sin fuerza.",
        "Se me ve la cara chueca en el espejo y arrastro la pierna izquierda.",
    ),
    _urgent(
        "heldout", "es", "self_harm",
        "everyone better off without me with pills saved, many pills taken on purpose",
        "Siento que todos estarían mejor sin mí y estuve guardando mis pastillas para eso.",
        "Me tomé muchas de mis pastillas a propósito hace un rato.",
    ),
    _routine(
        "heldout", "es", "schedule", "front_desk",
        "booking an afternoon visit, cancelling for a trip",
        "Quisiera sacar una cita con el doctor para el {dia_es}, de preferencia en la tarde.",
        "Cancelen por favor mi consulta del {dia_es}, estaré de viaje.",
    ),
    _routine(
        "heldout", "es", "refill", "medical_assistant",
        "few pills left, renewing the same dose",
        "Me quedan pocas pastillas de {med}, ¿me hacen el favor de mandar la receta?",
        "¿Pueden renovar mi {med}? Es la misma dosis de siempre.",
    ),
    _routine(
        "heldout", "es", "symptoms", "nurse",
        "a lingering mild symptom, what to take for it",
        "Llevo {duracion_es} con {sintoma_es}. ¿Es para ir a consulta o espero?",
        "Tengo un poco de {sintoma_es} y quería saber qué puedo tomar.",
    ),
    _routine(
        "heldout", "es", "results", "physician",
        "a value seen in the portal, a worrying result",
        "En el portal dice que tengo {resultado_es}. ¿Me puede decir el doctor qué significa?",
        "Vi que salió {resultado_es} y me preocupa. ¿Hay que hacer algo?",
    ),
    _routine(
        "heldout", "es", "bill", "billing",
        "an unrecognised charge, paying a balance in parts",
        "Tengo un cargo de {amount} en mi cuenta que no reconozco.",
        "¿Puedo pagar mi saldo de {amount} en partes?",
    ),
    _routine(
        "heldout", "es", "past_assessed", "medical_assistant",
        "a past allergic reaction or heart scare already assessed, asked about as paperwork",
        "El año pasado me atendieron en {hospital} por una reacción alérgica fuerte. ¿Pueden "
        "pedir el informe para mi nuevo doctor?",
        "Me hicieron estudios del corazón en {hospital} después de un susto en {mes_es} y todo "
        "salió normal. Quiero esos resultados en mi expediente.",
        mimics=("anaphylaxis", "chest_pain"),
    ),
)  # fmt: skip
FAMILY_BY_ID: Mapping[str, Family] = {family.id: family for family in FAMILIES}

# Slot values, one bank per slot and split (its source id: `slot.<name>.<split>`). A value that
# opens a sentence is capitalised (`man`, `woman`, `mujer_es`, `pharmacy_cap`).
SLOTS: Mapping[Split, Mapping[str, tuple[str, ...]]] = {
    "train": {
        "minutes": ("20", "25", "40"),
        "man": ("My husband", "My dad", "My brother"),
        "woman": ("My wife", "My mom", "My sister"),
        "child": ("son", "daughter", "toddler"),
        "older_child": ("three year old", "six year old"),
        "med": ("pravastatin", "glipizide", "carvedilol", "bupropion", "pantoprazole"),
        "weeks_preg": ("nine", "14", "22"),
        "thinner": ("warfarin", "apixaban"),
        "bp_med": ("chlorthalidone", "carvedilol"),
        "allergen": ("peanuts", "walnuts"),
        "antibiotic": ("amoxicillin", "cephalexin"),
        "baby_age": ("five-week-old", "seven-week-old", "two-month-old"),
        "infant_temp": ("100.6", "101.3", "38.2 C"),
        "child_temp": ("100.8", "101.5"),
        "visit_type": ("physical", "follow-up"),
        "doctor": ("Dr. Abara", "Dr. Kowalski", "Dr. Ferreira"),
        "day": ("Monday", "Tuesday", "the 9th"),
        "time": ("8:40", "11:15", "2:30"),
        "pharmacy": ("the pharmacy on Birch Road", "the drive-through pharmacy"),
        "pharmacy_cap": ("The pharmacy on Birch Road", "The drive-through pharmacy"),
        "specialty": ("rheumatology", "neurology", "urology"),
        "hospital": ("Harbor View Hospital", "Mercy West"),
        "form": ("FMLA paperwork", "disability form"),
        "symptom": ("scratchy throat", "runny nose", "rash on my elbow", "mild headache"),
        "duration": ("two days", "four days", "about a week"),
        "side_effect": ("a metallic taste", "loose stools", "headaches"),
        "procedure": ("the skin biopsy", "my carpal tunnel release"),
        "lab_result": ("an A1c of 8.2", "an LDL of 171", "a TSH of 6.8", "a ferritin of 9"),
        "amount": ("$62.40", "$215", "$1,080"),
        "date": ("February 11", "May 2"),
        "month": ("January", "March"),
        "nino_es": ("niño", "hijo"),
        "hombre_es": ("mi esposo", "mi papá"),
        "dia_es": ("lunes", "martes"),
        "sintoma_es": ("dolor de garganta", "tos seca"),
        "duracion_es": ("dos días", "una semana"),
        "resultado_es": ("el azúcar en 180", "el colesterol en 260"),
    },
    "heldout": {
        "minutes": ("15", "35", "50"),
        "man": ("My grandfather", "My uncle", "My father-in-law"),
        "woman": ("My grandmother", "My aunt", "My mother-in-law"),
        "child": ("little boy", "little girl", "four year old"),
        "older_child": ("two year old", "nine year old"),
        "teen_girl": ("16 year old daughter", "daughter in high school"),
        "med": ("rosuvastatin", "sitagliptin", "propranolol", "duloxetine", "famotidine"),
        "weeks_preg": ("eleven", "18", "26"),
        "thinner": ("rivaroxaban", "dabigatran"),
        "allergen": ("tree nuts", "sesame"),
        "antibiotic": ("clindamycin", "doxycycline"),
        "baby_weeks": ("six", "eight", "ten"),
        "infant_temp": ("100.9", "101.8", "38.5 C"),
        "child_temp": ("101.2", "38.6 C"),
        "visit_type": ("med check", "video visit"),
        "doctor": ("Dr. Mbeki", "Dr. Sorensen", "Dr. Takahashi"),
        "day": ("Thursday", "Friday", "the 23rd"),
        "time": ("9:50", "1:10", "4:45"),
        "pharmacy": ("the grocery store pharmacy", "the pharmacy near the hospital"),
        "pharmacy_cap": ("The grocery store pharmacy", "The pharmacy near the hospital"),
        "specialty": ("allergy", "sleep medicine", "pulmonology"),
        "hospital": ("Pine Ridge Medical Center", "the children's hospital downtown"),
        "form": ("school physical form", "camp health form"),
        "symptom": ("plugged ear", "dry cough", "sore knee", "stye"),
        "duration": ("three days", "about five days", "almost a week"),
        "side_effect": ("muscle aches", "a dry mouth", "trouble focusing"),
        "procedure": ("my hernia repair", "the toenail removal"),
        "lab_result": ("a B12 of 180", "an ALT of 96", "an eGFR of 52", "a vitamin D of 14"),
        "amount": ("$37.15", "$480", "$2,310"),
        "date": ("July 19", "September 30"),
        "month": ("June", "October"),
        "hombre_es": ("mi abuelo", "mi suegro"),
        "mujer_es": ("Mi abuela", "Mi tía"),
        "mes_es": ("junio", "octubre"),
        "dia_es": ("jueves", "viernes"),
        "sintoma_es": ("dolor de oído", "comezón en la piel"),
        "duracion_es": ("cuatro días", "casi una semana"),
        "resultado_es": ("la hemoglobina en 10", "los triglicéridos en 400"),
    },
}


@dataclass(frozen=True)
class Bank:
    """A paraphrase source other than slot values: one kind, language and split."""

    kind: str
    language: str  # "*": every language
    split: Split
    how: str
    items: tuple[Any, ...]

    @property
    def id(self) -> str:
        return f"{self.kind}.{self.split}" if self.language == "*" else (
            f"{self.kind}.{self.language}.{self.split}"
        )  # fmt: skip


BANKS: tuple[Bank, ...] = (
    # Hedges: (where, sentence). The pack's criterion: an emergency "even when described casually
    # or with uncertainty" is urgent, so a hedge never changes a label.
    Bank("hedge", "en", "train", "a softening sentence before or after the message", (
        ("before", "Sorry to bother you with this."),
        ("before", "Not sure if this is worth a message."),
        ("before", "This is probably nothing."),
        ("after", "I'm sure it's fine, just checking."),
        ("after", "Hopefully it's nothing."),
        ("after", "No rush on this."),
    )),
    Bank("hedge", "en", "heldout", "a softening sentence before or after the message", (
        ("before", "I don't want to overreact."),
        ("before", "Could be nothing at all."),
        ("before", "I feel a bit silly writing this."),
        ("after", "Maybe I'm being dramatic."),
        ("after", "Get back to me whenever."),
        ("after", "Not a big deal, I think."),
    )),
    Bank("hedge", "es", "train", "a softening sentence before or after the message", (
        ("before", "Perdón por molestar."),
        ("before", "No sé si es importante."),
        ("after", "Seguro no es nada."),
    )),
    Bank("hedge", "es", "heldout", "a softening sentence before or after the message", (
        ("before", "A lo mejor no es nada."),
        ("before", "No quiero exagerar."),
        ("after", "Cuando puedan me dicen."),
    )),
    # Asides: (the team that would handle it alone, sentence), joined before or after the message.
    Bank("aside", "en", "train", "a second, administrative request (mixed topics)", (
        ("front_desk", "Can I push my {visit_type} on {day} to later in the day?"),
        ("billing", "Can someone look at my last statement? It looked off."),
        ("medical_assistant", "I'm almost out of my {med}, if someone can send that in."),
    )),
    Bank("aside", "en", "heldout", "a second, administrative request (mixed topics)", (
        ("front_desk", "Please note my new email for the portal."),
        ("billing", "I still owe {amount} and want to set up payments."),
        ("medical_assistant", "My {specialty} office still hasn't gotten the referral."),
    )),
    Bank("aside", "es", "train", "a second, administrative request (mixed topics)", (
        ("front_desk", "Quiero cambiar mi cita del {dia_es}."),
        ("billing", "Tengo una duda sobre mi factura."),
        ("medical_assistant", "Ocupo que me surtan el {med} pronto."),
    )),
    Bank("aside", "es", "heldout", "a second, administrative request (mixed topics)", (
        ("front_desk", "Cambié de número de teléfono, por favor actualícenlo."),
        ("billing", "Me llegó una cuenta que no entiendo."),
        ("medical_assistant", "Todavía no me llaman de la referencia."),
    )),
    Bank("opener", "en", "train", "a portal greeting", ("Hi there,", "Good afternoon,")),
    Bank("opener", "en", "heldout", "a portal greeting", ("Hey,", "Hello team,")),
    Bank("opener", "es", "train", "a portal greeting", ("Hola, buenas tardes.", "Buenas,")),
    Bank("opener", "es", "heldout", "a portal greeting", ("Buen día,", "Qué tal,")),
    Bank("closer", "en", "train", "a portal sign-off", ("Thanks in advance", "Appreciate it")),
    Bank("closer", "en", "heldout", "a portal sign-off", ("Thank you very much", "Talk soon")),
    Bank("closer", "es", "train", "a portal sign-off", ("Gracias de antemano", "Que estén bien")),
    Bank("closer", "es", "heldout", "a portal sign-off", ("Saludos", "Bendiciones")),
    Bank("subject", "en", "train", "a portal subject that does not name the team", (
        "Quick question", "Message for the office", "Need some help",
    )),
    Bank("subject", "en", "heldout", "a portal subject that does not name the team", (
        "Checking in", "Please read when you can", "Hi",
    )),
    Bank("subject", "es", "train", "a portal subject", ("Pregunta", "Mensaje")),
    Bank("subject", "es", "heldout", "a portal subject", ("Consulta", "Ayuda por favor")),
    # Texting: lowercased, the final full stop dropped, and each swap made with P_TEXTING_SWAP.
    Bank("texting", "en", "train", "SMS style: lowercase and abbreviations", (
        (" you ", " u "), ("please", "pls"), ("thanks", "thx"), (" because ", " bc "),
        (" are ", " r "),
    )),
    Bank("texting", "en", "heldout", "SMS style: lowercase and abbreviations", (
        (" your ", " ur "), ("please", "plz"), ("i don't know", "idk"), (" right now", " rn"),
        ("thank you", "ty"), (" with ", " w/ "),
    )),
    Bank("texting", "es", "train", "SMS style: lowercase and abbreviations", (
        ("por favor", "porfa"), (" que ", " q "), ("gracias", "grax"),
    )),
    Bank("texting", "es", "heldout", "SMS style: lowercase and abbreviations", (
        ("también", "tmb"), (" para ", " pa "), ("por favor", "xfa"),
    )),
    # Typos: one or two words of five letters or more, each by one of the split's operators.
    Bank("typo", "*", "train", "adjacent letters swapped, or a letter dropped", (
        "transpose", "drop",
    )),
    Bank("typo", "*", "heldout", "a neighbouring key struck, or a letter doubled", (
        "neighbour", "double",
    )),
)  # fmt: skip
_BANKS: Mapping[tuple[str, str, str], Bank] = {(b.kind, b.language, b.split): b for b in BANKS}
_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
_NEIGHBOURS: Mapping[str, str] = {
    key: "".join(
        row[j]
        for r, row in enumerate(_KEYBOARD_ROWS)
        for j in range(len(row))
        if (r, j) != (i, k) and abs(r - i) <= 1 and abs(j - k) <= 1
    )
    for i, keys in enumerate(_KEYBOARD_ROWS)
    for k, key in enumerate(keys)
}
_SLOT = re.compile(r"\{(\w+)\}")


def families(split: Split) -> list[Family]:
    return [family for family in FAMILIES if family.split == split]


def bank(kind: str, language: str, split: Split) -> Bank:
    return _BANKS[(kind, language, split)]


def slot_sources() -> list[str]:
    return [f"slot.{name}.{split}" for split in SPLITS for name in SLOTS[split]]


# -- generation -------------------------------------------------------------------------------


def generate(seed: int = DEFAULT_SEED) -> dict[Split, list[dict[str, Any]]]:
    """The draft's two splits. Each family draws from its own generator, seeded by the seed and
    its id, so changing one family changes only its cases."""
    splits: dict[Split, list[dict[str, Any]]] = {}
    for split in SPLITS:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for family in families(split):
            rng = random.Random(f"{VERSION}/{seed}/{family.id}")
            for index in range(COUNTS[(family.kind, family.language)]):
                number = index % len(family.frames)
                for _attempt in range(25):  # a case repeated within the split is drawn again
                    case = _case(family, number, rng)
                    key = json.dumps(case["input"], sort_keys=True, ensure_ascii=False)
                    if key not in seen:
                        break
                seen.add(key)
                records.append({"id": f"hib-{split}-{len(records) + 1:04d}", **case})
        splits[split] = records
    return splits


def _case(family: Family, number: int, rng: random.Random) -> dict[str, Any]:
    split, language = family.split, family.language
    frame = family.frames[number]
    used: set[str] = set()
    transforms: list[str] = []
    core = _fill(frame, split, rng, used)
    parts = [core]
    if family.destination in HEDGED and rng.random() < P_HEDGE[family.kind]:
        hedges = bank("hedge", language, split)
        where, hedge = rng.choice(hedges.items)
        parts = [hedge, *parts] if where == "before" else [*parts, hedge]
        used.add(hedges.id)
        transforms.append(f"hedge_{where}")
    asides = bank("aside", language, split)
    fitting = [item for item in asides.items if TEAM_RANK[item[0]] < TEAM_RANK[family.destination]]
    if fitting and rng.random() < P_ASIDE:
        aside = _fill(rng.choice(fitting)[1], split, rng, used)
        before = rng.random() < 0.5
        parts = [aside, *parts] if before else [*parts, aside]
        used.add(asides.id)
        transforms.append("aside_before" if before else "aside_after")
    body = " ".join(parts)
    channel = "sms" if rng.random() < P_SMS else "portal"
    subject = ""
    if channel == "portal":
        for kind, chance in (("opener", P_OPENER), ("closer", P_CLOSER), ("subject", P_SUBJECT)):
            if rng.random() < chance:
                source = bank(kind, language, split)
                text = rng.choice(source.items)
                used.add(source.id)
                if kind == "opener":
                    body = f"{text} {body}"
                elif kind == "closer":
                    body = f"{body}\n{text}"
                else:
                    subject = text
    else:
        texting = bank("texting", language, split)
        body = _texting(body, texting, rng)
        used.add(texting.id)
        transforms.append("texting")
    if rng.random() < P_TYPO:
        typos = bank("typo", "*", split)
        typed = _typos(body, typos, rng)
        if typed != body:
            body = typed
            used.add(typos.id)
            transforms.append("typo")
    if family.pediatric or set(_SLOT.findall(frame)) & PEDIATRIC_SLOTS:
        age_band = "0-17"
    elif any(word in core.lower() for word in ("grand", "abuel")):
        age_band = "65+"
    else:
        age_band = rng.choices([b for b, _ in ADULT_BANDS], [w for _, w in ADULT_BANDS])[0]
    message: dict[str, Any] = {"body": body, "channel": channel}
    if subject:
        message["subject"] = subject
    return {
        "input": {
            "message": message,
            "patient": {"age_band": age_band, "preferred_language": language},
        },
        "labels": {"destination": family.destination, "urgency": family.urgency},
        "attributes": {
            "review": REVIEW_STATUS,
            "split": split,
            "family": family.id,
            "frame": number + 1,
            "language": language,
            "channel": channel,
            "age_band": age_band,
            "topic": family.topic,
            "red_flag": family.red_flag,
            "mimics": list(family.mimics),
            "transforms": transforms,
            "sources": sorted(used),
        },
    }


def _fill(text: str, split: Split, rng: random.Random, used: set[str]) -> str:
    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        used.add(f"slot.{name}.{split}")
        return rng.choice(SLOTS[split][name])

    return _SLOT.sub(value, text)


def _texting(text: str, source: Bank, rng: random.Random) -> str:
    text = text.lower()
    for old, new in source.items:
        if rng.random() < P_TEXTING_SWAP:
            text = text.replace(old, new)
    return text.rstrip(".")


def _typos(text: str, source: Bank, rng: random.Random) -> str:
    words = text.split(" ")
    eligible = [i for i, word in enumerate(words) if len(word) > 4 and word.isalpha()]
    for index in rng.sample(eligible, k=min(len(eligible), rng.choice((1, 2)))):
        words[index] = _typo(words[index], rng.choice(source.items), rng)
    return " ".join(words)


def _typo(word: str, operator: str, rng: random.Random) -> str:
    if operator == "transpose":
        at = rng.randrange(len(word) - 1)
        return word[:at] + word[at + 1] + word[at] + word[at + 2 :]
    if operator == "drop":
        at = rng.randrange(1, len(word))
        return word[:at] + word[at + 1 :]
    if operator == "neighbour":
        keys = [i for i, letter in enumerate(word) if letter.lower() in _NEIGHBOURS]
        if keys:
            at = rng.choice(keys)
            struck = rng.choice(_NEIGHBOURS[word[at].lower()])
            return word[:at] + (struck.upper() if word[at].isupper() else struck) + word[at + 1 :]
        operator = "double"
    if operator == "double":
        at = rng.randrange(len(word))
        return word[: at + 1] + word[at] + word[at + 1 :]
    raise ValueError(f"unknown typo operator {operator!r}")


# -- the leakage check ------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    compared: int  # what was compared: families, sources, cases or text units
    problems: tuple[str, ...]


@dataclass(frozen=True)
class Corpus:
    """The shipped pack's text, as (where, text): what its model was trained and its rules tuned
    on (`tuning`, which the held-out split must not share), and its hand-written held-out
    red-flag set (`held_out`, which the train split must not share)."""

    tuning: tuple[tuple[str, str], ...]
    held_out: tuple[tuple[str, str], ...]


TUNING_DATA = ("train", "calibration", "red_flags")
HELD_OUT_DATA = ("red_flags_holdout",)


def shipped_corpus(pack_dir: Path) -> Corpus:
    """The shipped inbox pack's data (`TUNING_DATA`, `HELD_OUT_DATA` under its `data/`), and the
    literal text of every `synthetic_inbox` template (the text between its slots)."""
    tuning = [
        (f"{PACK_NAME}/data/{name}.jsonl {record.get('id')}", text)
        for name in TUNING_DATA
        for record in _read_jsonl(pack_dir / "data" / f"{name}.jsonl")
        for text in texts(record)
    ]
    tuning += [
        (f"synthetic_inbox.TEMPLATES[{number}]", piece)
        for number, template in enumerate(synthetic_inbox.TEMPLATES)
        for body in template.bodies
        for piece in _SLOT.split(body)[::2]
    ]
    held_out = [
        (f"{PACK_NAME}/data/{name}.jsonl {record.get('id')}", text)
        for name in HELD_OUT_DATA
        for record in _read_jsonl(pack_dir / "data" / f"{name}.jsonl")
        for text in texts(record)
    ]
    return Corpus(tuple(tuning), tuple(held_out))


def leakage_check(train: Records, heldout: Records, corpus: Corpus) -> list[Check]:
    """Each check of the module docstring, with what it compared and its problems."""
    splits = {"train": train, "heldout": heldout}
    checks = []
    families_of = {split: {_family(r) for r in splits[split]} for split in SPLITS}
    shared = sorted(families_of["train"] & families_of["heldout"])
    checks.append(
        Check(
            "families in both splits",
            len(families_of["train"] | families_of["heldout"]),
            tuple(f"family {family} has cases in both splits" for family in shared),
        )
    )
    sources_of = {split: {s for r in splits[split] for s in _sources(r)} for split in SPLITS}
    shared = sorted(sources_of["train"] & sources_of["heldout"])
    checks.append(
        Check(
            "paraphrase sources in both splits",
            len(sources_of["train"] | sources_of["heldout"]),
            tuple(f"source {source} is used by both splits" for source in shared),
        )
    )
    misfiled = []
    for split in SPLITS:
        for record in splits[split]:
            family = FAMILY_BY_ID.get(_family(record))
            attributes = record.get("attributes") or {}
            if attributes.get("split") != split or family is None or family.split != split:
                misfiled.append(f"{record.get('id')} is in {split} but filed under another split")
    checks.append(
        Check("cases filed under the other split", len(train) + len(heldout), tuple(misfiled))
    )
    labelled = [(str(r.get("id")), text) for r in train for text in texts(r)]
    checks.append(_text_check("held-out text found in the train split", heldout, labelled, "train"))
    checks.append(
        _text_check(
            "held-out text found in the shipped pack's training and tuning data or templates",
            heldout,
            corpus.tuning,
            "the shipped pack",
        )
    )
    checks.append(
        _text_check(
            "train text found in the shipped pack's held-out red-flag set",
            train,
            corpus.held_out,
            "the shipped held-out set",
        )
    )
    return checks


def leakage_problems(train: Records, heldout: Records, corpus: Corpus) -> list[str]:
    return [p for check in leakage_check(train, heldout, corpus) for p in check.problems]


def _text_check(
    name: str,
    records: Sequence[Mapping[str, Any]],
    against: Sequence[tuple[str, str]],
    label: str,
) -> Check:
    """Each sentence of `SENTENCE_CHARS` or more and each run of `RUN_WORDS` words of `records`
    that is also in `against`: one problem per shared unit, naming the first case and source."""
    index: dict[tuple[str, str], str] = {}
    for source, text in against:
        for unit in fragments(text):
            index.setdefault(unit, source)
    problems: dict[tuple[str, str], str] = {}
    compared = 0
    for record in records:
        for text in texts(record):
            for unit in fragments(text):
                compared += 1
                where = index.get(unit)
                if where is not None and unit not in problems:
                    kind = "sentence" if unit[0] == "sentence" else f"{RUN_WORDS}-word run"
                    problems[unit] = (
                        f"{record.get('id')} shares a {kind} with {label} ({where}): {unit[1]!r}"
                    )
    return Check(name, compared, tuple(problems.values()))


def fragments(text: str) -> Iterator[tuple[str, str]]:
    """The leakage check's units of `text`, folded: ("sentence", s) for each sentence of at
    least `SENTENCE_CHARS` characters, and ("run", r) for each run of `RUN_WORDS` words inside
    a sentence."""
    for raw in re.split(r"[.!?\n]+", text):
        sentence = fold(raw)
        if len(sentence) >= SENTENCE_CHARS:
            yield "sentence", sentence
        words = sentence.split()
        for start in range(len(words) - RUN_WORDS + 1):
            yield "run", " ".join(words[start : start + RUN_WORDS])


def fold(text: str) -> str:
    """NFKC, casefolded, each run of characters other than letters and digits one space."""
    return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def texts(record: Mapping[str, Any]) -> list[str]:
    message = (record.get("input") or {}).get("message") or {}
    return [
        value
        for value in (message.get("subject"), message.get("body"))
        if isinstance(value, str) and value
    ]


def _family(record: Mapping[str, Any]) -> str:
    return str((record.get("attributes") or {}).get("family"))


def _sources(record: Mapping[str, Any]) -> list[str]:
    return [str(source) for source in (record.get("attributes") or {}).get("sources") or ()]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# -- the coverage check -----------------------------------------------------------------------


def coverage_problems(
    train: Records,
    heldout: Records,
    labels: Mapping[str, Sequence[str]],
    critical: Sequence[tuple[str, str]],
) -> list[str]:
    """What either split lacks: `MIN_PER_LABEL` cases of every label of every question (the
    pack's), `MIN_CRITICAL` of each critical label with some in every language, `MIN_PER_SIGN`
    of every red-flag sign for `urgency=urgent`, and a case from every frame of its families;
    and any label the pack does not have."""
    problems = []
    for split, records in zip(SPLITS, (train, heldout), strict=True):
        for question, names in labels.items():
            counts = Counter(str(record["labels"].get(question)) for record in records)
            for label in names:
                if counts[label] < MIN_PER_LABEL:
                    problems.append(
                        f"{split}: {question}={label} has {counts[label]} cases, under "
                        f"{MIN_PER_LABEL}"
                    )
            for label in sorted(set(counts) - set(names)):
                problems.append(f"{split}: {question}={label} is not a label of the pack")
        for question, label in critical:
            cases = [r for r in records if r["labels"].get(question) == label]
            if len(cases) < MIN_CRITICAL:
                problems.append(
                    f"{split}: critical {question}={label} has {len(cases)} cases, under "
                    f"{MIN_CRITICAL}"
                )
            languages = Counter(r["attributes"].get("language") for r in cases)
            problems += [
                f"{split}: critical {question}={label} has no case in {language}"
                for language in LANGUAGES
                if not languages[language]
            ]
            if (question, label) == ("urgency", "urgent"):
                signs = Counter(r["attributes"].get("red_flag") for r in cases)
                problems += [
                    f"{split}: the sign {sign} has {signs[sign]} urgent cases, under {MIN_PER_SIGN}"
                    for sign in RED_FLAGS
                    if signs[sign] < MIN_PER_SIGN
                ]
        drawn = Counter(
            (r["attributes"].get("family"), r["attributes"].get("frame")) for r in records
        )
        for family in families(split):
            missing = [n for n in range(1, len(family.frames) + 1) if not drawn[(family.id, n)]]
            if missing:
                problems.append(f"{split}: family {family.id} has no case from frames {missing}")
    return problems


def pack_labels(manifest: Any) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """(every question's labels, the critical labels) of a pack manifest."""
    labels = {name: list(question.labels) for name, question in manifest.questions.items()}
    critical = [(c.question, c.label) for c in manifest.evaluation.critical]
    return labels, critical


# -- the files ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    seed: int
    splits: dict[Split, list[dict[str, Any]]]
    leakage: list[Check]
    coverage: list[str]
    files: dict[str, bytes]

    @property
    def problems(self) -> list[str]:
        return [p for check in self.leakage for p in check.problems] + self.coverage


def build(pack_dir: Path, seed: int = DEFAULT_SEED) -> Draft:
    """The draft generated at `seed`, checked against the shipped pack at `pack_dir`, and its
    files' bytes (`FILES`)."""
    from mendari import load_pack

    splits = generate(seed)
    leakage = leakage_check(splits["train"], splits["heldout"], shipped_corpus(pack_dir))
    labels, critical = pack_labels(load_pack(pack_dir).manifest)
    coverage = coverage_problems(splits["train"], splits["heldout"], labels, critical)
    files = {f"{split}.jsonl": jsonl(splits[split]) for split in SPLITS}
    files["FAMILIES.md"] = families_sheet(splits).encode()
    files["REPORT.md"] = report(seed, splits, leakage, coverage, files).encode()
    return Draft(seed, splits, leakage, coverage, {name: files[name] for name in FILES})


def jsonl(records: Sequence[Mapping[str, Any]]) -> bytes:
    """As `mendari sandbox generate` writes a pack's data."""
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()


def report(
    seed: int,
    splits: Mapping[Split, Sequence[Mapping[str, Any]]],
    leakage: Sequence[Check],
    coverage: Sequence[str],
    files: Mapping[str, bytes],
) -> str:
    problems = [p for check in leakage for p in check.problems]
    lines = [
        "# Harder inbox benchmark: draft report",
        "",
        f"**{REVIEW_STATUS}.** No case here has been read by a clinician. The labels are the",
        "generator's, from the pack's written criteria. This set is not the Patient Inbox Router's",
        "benchmark: adopting it is a separate decision for the owner",
        "(`notes/clinician-review-harder-inbox-benchmark.md`).",
        "",
        f"Generated by `mendari.sandbox.synthetic_inbox_harder` ({VERSION}, seed {seed}) with",
        "`uv run python scripts/harder_inbox_draft.py`; `--check` proves these files are its",
        "output. Entirely synthetic: no PHI, no network, no model calls.",
        "",
        "| File | Cases | SHA-256 |",
        "|---|---|---|",
    ]
    cases = {f"{split}.jsonl": str(len(splits[split])) for split in SPLITS}
    for name in ("train.jsonl", "heldout.jsonl", "FAMILIES.md"):
        digest = hashlib.sha256(files[name]).hexdigest()
        lines.append(f"| `{name}` | {cases.get(name, '')} | `{digest}` |")
    lines += ["", "## Counts", "", "| Question | Label | train | heldout |", "|---|---|---|---|"]
    for question in ("destination", "urgency"):
        lines += _count_rows(
            question, {s: [r["labels"][question] for r in splits[s]] for s in SPLITS}
        )
    lines += [
        "",
        "Possible emergencies (`urgency=urgent`, the critical label) by the sign the pack's",
        "criterion lists, and by language:",
        "",
        "| Sign | train (en / es) | heldout (en / es) |",
        "|---|---|---|",
    ]
    for sign, words in RED_FLAGS.items():
        cells = []
        for split in SPLITS:
            urgent = [r for r in splits[split] if r["attributes"]["red_flag"] == sign]
            by = Counter(r["attributes"]["language"] for r in urgent)
            cells.append(f"{len(urgent)} ({by['en']} / {by['es']})")
        lines.append(f"| {words} | {cells[0]} | {cells[1]} |")
    lines += ["", "| Attribute | Value | train | heldout |", "|---|---|---|---|"]
    for attribute in ("language", "channel", "age_band"):
        values = {s: [r["attributes"][attribute] for r in splits[s]] for s in SPLITS}
        lines += _count_rows(attribute, values)
    kinds = {s: [_kind(FAMILY_BY_ID[r["attributes"]["family"]]) for r in splits[s]] for s in SPLITS}
    lines += _count_rows("family kind", kinds)
    transforms = {s: [t for r in splits[s] for t in r["attributes"]["transforms"]] for s in SPLITS}
    lines += _count_rows("transform", transforms)
    lines += [
        "",
        "Urgent cases are about a third of each split, far above a real inbox's share: the set",
        "is built to measure the critical label, so its precision and accuracy are not a",
        "site's.",
        "",
        "## Leakage check",
        "",
        f"Result: **{'passed' if not problems else 'failed'}** ({len(problems)} problems). Text is",
        f"compared folded (NFKC, casefolded, punctuation as spaces), by sentence ({SENTENCE_CHARS}",
        f"characters or more) and by every run of {RUN_WORDS} words inside a sentence. The shipped",
        f"pack's training and tuning data is `packs/{PACK_NAME}/data/`",
        f"{_names(TUNING_DATA)} and the text of every `synthetic_inbox` template; its held-out",
        f"red-flag set is {_names(HELD_OUT_DATA)}.",
        "",
        "| Check | Compared | Problems |",
        "|---|---|---|",
    ]
    lines += [f"| {check.name} | {check.compared} | {len(check.problems)} |" for check in leakage]
    lines += [f"- {problem}" for problem in problems]
    lines += [
        "",
        "## Coverage check",
        "",
        f"Result: **{'passed' if not coverage else 'failed'}** ({len(coverage)} problems): in each",
        f"split, every label of both questions ({MIN_PER_LABEL} cases or more), the critical label",
        f"({MIN_CRITICAL} or more, in English and Spanish), each red-flag sign ({MIN_PER_SIGN} or",
        "more) and every frame of every family.",
    ]
    lines += [f"- {problem}" for problem in coverage]
    lines += [
        "",
        "## How each family was made",
        "",
        "Frames are written for this set, one family per split; `FAMILIES.md` lists each frame.",
        "Each case takes one frame (in turn) and fills its slots from its split's banks. Then,",
        "by chance, it takes a hedge before or after it (on a nurse's or physician's family:",
        f"{P_HEDGE['urgent']:.0%} of urgent cases, {P_HEDGE['routine']:.0%} of others), an",
        f"administrative aside before or after it ({P_ASIDE:.0%}, only where the family's team",
        f"outranks the aside's), the SMS channel and texting style ({P_SMS:.0%}) or on the",
        f"portal a greeting, a sign-off and a subject, and typos ({P_TYPO:.0%}).",
        "",
        "| Family | Labels | Kind | Frames | Cases | How it paraphrases |",
        "|---|---|---|---|---|---|",
    ]
    drawn = {s: Counter(r["attributes"]["family"] for r in splits[s]) for s in SPLITS}
    for family in FAMILIES:
        lines.append(
            f"| `{family.id}` | {family.destination}, {family.urgency} | {_kind(family)} | "
            f"{len(family.frames)} | {drawn[family.split][family.id]} | {family.how} |"
        )
    lines += [
        "",
        "## Paraphrase sources",
        "",
        "Every source has one bank per split; no case draws on the other split's.",
        "",
        "| Source | Items | How |",
        "|---|---|---|",
    ]
    lines += [f"| `{b.id}` | {len(b.items)} | {b.how} |" for b in BANKS]
    for split in SPLITS:
        lines.append(
            f"| `slot.*.{split}` | {sum(len(v) for v in SLOTS[split].values())} | slot values, "
            f"{len(SLOTS[split])} slots (`FAMILIES.md`) |"
        )
    return "\n".join(lines) + "\n"


def families_sheet(splits: Mapping[Split, Sequence[Mapping[str, Any]]]) -> str:
    """The review sheet: every family's labels and frames, with a case drawn from each frame,
    then every slot's values and every other source's items."""
    lines = [
        "# Harder inbox benchmark: families and sources",
        "",
        f"**{REVIEW_STATUS}.** Every frame and value below was written by a build session for",
        "the owner's review (`notes/clinician-review-harder-inbox-benchmark.md`). A family is",
        "one way of asking for one thing; its labels are on every case drawn from it. `{slot}`",
        "is filled from the family's split's values (the end of this sheet).",
    ]
    for split in SPLITS:
        title = "Train" if split == "train" else "Held-out"
        lines += ["", f"## {title} split", ""]
        for family in families(split):
            sign = (
                f"; sign: {RED_FLAGS[family.red_flag]}"
                if family.red_flag
                else f"; looks like: {', '.join(RED_FLAGS[m] for m in family.mimics)}"
                if family.mimics
                else ""
            )
            lines += [
                f"### `{family.id}`",
                "",
                f"**{family.destination}, {family.urgency}** ({_kind(family)}{sign}). "
                f"{family.how[:1].upper()}{family.how[1:]}.",
                "",
            ]
            for number, frame in enumerate(family.frames, start=1):
                example = next(
                    (
                        r
                        for r in splits[split]
                        if r["attributes"]["family"] == family.id
                        and r["attributes"]["frame"] == number
                    ),
                    None,
                )
                lines.append(f"{number}. {frame}")
                if example is not None:
                    body = example["input"]["message"]["body"].replace("\n", " / ")
                    lines.append(f"   - e.g. `{example['id']}`: {body}")
            lines.append("")
    lines += ["## Slot values", "", "| Slot | train | heldout |", "|---|---|---|"]
    for name in sorted(set(SLOTS["train"]) | set(SLOTS["heldout"])):
        cells = ["; ".join(SLOTS[s].get(name, ())) or "(none)" for s in SPLITS]
        lines.append(f"| `{name}` | {cells[0]} | {cells[1]} |")
    lines += ["", "## Other sources", ""]
    for source in BANKS:
        items = "; ".join(_item(source.kind, item) for item in source.items)
        lines.append(f"- `{source.id}` ({source.how}): {items}")
    return "\n".join(lines) + "\n"


def _item(kind: str, item: Any) -> str:
    """A source's item as the review sheet shows it."""
    if kind == "texting":
        return f'"{item[0].strip()}" as "{item[1].strip()}"'
    if isinstance(item, tuple):  # a hedge's place, or the team that would handle an aside
        return f"({item[0]}) {item[1]}"
    return str(item)


def _kind(family: Family) -> str:
    return {"urgent": "possible emergency", "look-alike": "look-alike", "routine": "routine"}[
        family.kind
    ]


def _count_rows(name: str, values: Mapping[Split, Sequence[str]]) -> list[str]:
    """One table row per value: the name, the value, and how often it is in each split."""
    counts = {split: Counter(values[split]) for split in SPLITS}
    every = sorted(set(counts["train"]) | set(counts["heldout"]))
    return [f"| {name} | {v} | {counts['train'][v]} | {counts['heldout'][v]} |" for v in every]


def _names(names: Sequence[str]) -> str:
    return ", ".join(f"`{name}.jsonl`" for name in names)


def check_files(directory: Path, draft: Draft) -> list[str]:
    """The files in `directory` that are not the draft's, byte for byte, and the draft's own
    problems."""
    problems = []
    for name, content in draft.files.items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{path} is missing")
        elif path.read_bytes() != content:
            problems.append(f"{path} is not the generator's output: run the script without --check")
    return problems + draft.problems
