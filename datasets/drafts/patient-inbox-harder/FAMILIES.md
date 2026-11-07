# Harder inbox benchmark: families and sources

**Draft, not clinically reviewed.** Every frame and value below was written by a build session for
the owner's review (`notes/clinician-review-harder-inbox-benchmark.md`). A family is
one way of asking for one thing; its labels are on every case drawn from it. `{slot}`
is filled from the family's split's values (the end of this sheet).

## Train split

### `train.en.chest_pain`

**nurse, urgent** (possible emergency; sign: chest pain or pressure). Pressure told as a weight, a band or someone standing on the chest, without the words chest pain.

1. There's a heavy weight sitting on the middle of my chest and it hasn't let up for {minutes} minutes.
   - e.g. `hib-train-0001`: Good afternoon, There's a heavy weight sitting on the middle of my chest and it hasn't let up for 25 minutes. No rush on this. / Appreciate it
2. My chest feels squeezed, like a band being pulled tight, and now my left shoulder aches too.
   - e.g. `hib-train-0002`: my chest feels squeezed, like a band being pulled tight, and now my left shoulder aches too
3. {man} keeps pressing a fist against his breastbone and he's sweaty and grey.
   - e.g. `hib-train-0003`: My brother keeps pressing a fist against his breastbone and he's sweaty and grey. Hopefully it's nothing. Can someone look at my last statement? It looked off. / Appreciate it
4. It feels like someone is standing on my chest. Started while I was carrying the laundry upstairs.
   - e.g. `hib-train-0004`: It feels like someone is standing on my chest. Started while I was carrying the laundry upstairs. I'm sure it's fine, just checking. / Appreciate it

### `train.en.breathing`

**nurse, urgent** (possible emergency; sign: trouble breathing). Air hunger told by what the person cannot do (finish a sentence, get relief from a nebulizer), rarely by the word breathe.

1. I can only get out a few words before I have to stop and pull in more air.
   - e.g. `hib-train-0013`: Hi there, I can only get out a few words before I have to stop and pull in more air. Hopefully it's nothing. Can someone look at my last statement? It looked off.
2. My {child} is sucking in under the ribs with every breath and the nebulizer did nothing.
   - e.g. `hib-train-0014`: Good afternoon, My daughter is suking in under the ribs with every breath and the nebulizer did nothing. I'm almost out of my pantoprazole, if someone can send that in.
3. Every breath feels like pulling air through a straw and it's getting worse by the minute.
   - e.g. `hib-train-0015`: Hi there, Sorry to bother you with this. Every breath feels like pulling air through a straw and it's getting worse by the minute.
4. {woman} is wheezing so loud I can hear it from the next room and she can't finish a sentence.
   - e.g. `hib-train-0016`: Sorry to bother you with this. My sister is wheezing so loud I can hear it from the next room and she can't finish a sentence. / Thanks in advance

### `train.en.stroke`

**nurse, urgent** (possible emergency; sign: stroke signs (face drooping, arm weakness, slurred speech)). A lopsided smile, a hand that stops working, words that come out wrong, told as what was noticed.

1. {man}'s smile went lopsided all of a sudden and his words are coming out mushy.
   - e.g. `hib-train-0025`: my brother's smile went lopsided all of a sudden and his words are coming out mushy. can i push my physical on monday to later in the day?
2. My left hand just stopped working right, I keep dropping things, and half my face feels heavy.
   - e.g. `hib-train-0026`: my left hand just stopped working right, i keep dorpping things, and half my face feels heavy
3. {woman} was fine at breakfast and now she can't get the right words out and one side of her mouth sags.
   - e.g. `hib-train-0027`: My sister was fine at breakfat and now she can't get the right words out and one side of her mouth sags. I'm sure it's fine, just checking.
4. Out of nowhere half my vision is gone on one side and my right leg is dragging.
   - e.g. `hib-train-0028`: This is probably nothing. Out of nowhere half my vision is gone on one side and my right leg is dragging. I'm almost out of my pravastatin, if someone can send that in. / Thanks in advance

### `train.en.self_harm`

**nurse, urgent** (possible emergency; sign: thoughts of suicide or self-harm, or an intentional overdose). Intent stated without the words suicide or kill: a plan, pills counted out, not being safe tonight, a handful swallowed.

1. I've decided I'm done. I have the pills counted out and I don't see a reason to keep going.
   - e.g. `hib-train-0037`: Can I push my physical on Tuesday to later in the day? Sorry to bother you with this. I've decided I'm done. I have the pills counted out and I don't see a reason to keep going. / Thanks in advance
2. Lately I keep thinking everyone would be better off without me, and tonight it feels like a plan.
   - e.g. `hib-train-0038`: Hi there, Sorry to bother you with this. Lately I keep thinking everyone would be better off without me, and tonight it feels like a plan.
3. I swallowed a handful of my {med} about an hour ago because I wanted everything to stop.
   - e.g. `hib-train-0039`: Sorry to bother you with this. I swallowed a handful of my glipizide about an hour ago because I wanted everything to stop. Can someone look at my last statement? It looked off.
4. I don't think I can keep myself safe tonight.
   - e.g. `hib-train-0040`: not sure if this is worth a message. i don't think i can keep myself safe tonight

### `train.en.bleeding`

**nurse, urgent** (possible emergency; sign: heavy bleeding, vomiting or coughing up blood, bleeding in pregnancy). Bleeding told by what it soaks or how it spurts, as coffee grounds, or on a blood thinner; pregnancy implied by weeks along.

1. I'm {weeks_preg} weeks along and I'm passing clots and soaking through a pad every hour.
   - e.g. `hib-train-0049`: Hi there, I'm nine weeks along and I'm passing clots and soaking through a pad every hour. Hopefully it's nothing. / Appreciate it
2. The cut on my thigh spurts every time my heart beats, the towel is soaked through.
   - e.g. `hib-train-0050`: Good afternoon, The cut on my thigh spurts every time my heart beats, the towel is soaked through. I'm sure it's fine, just checking.
3. I've been throwing up what looks like coffee grounds since lunch.
   - e.g. `hib-train-0051`: I've been throwing up what looks like coffee grounds since lunch. / Thanks in advance
4. My nose has been gushing for over half an hour, pinching it does nothing, and I'm on {thinner}.
   - e.g. `hib-train-0052`: Not sure if this is worth a message. My nose has been gushing for over half an hour, pinching it does nothing, and I'm on warfarin.

### `train.en.anaphylaxis`

**nurse, urgent** (possible emergency; sign: a severe allergic reaction). Swelling and welts after a trigger, told as a funny voice, puffy lips, a tongue too big or trouble swallowing.

1. Ate a cookie that must have had {allergen} in it and now my voice sounds funny and my lips are puffing up.
   - e.g. `hib-train-0061`: ate a cookie that must have had walnuts in it and now my voice sounds funny and my lips r puffing up. hopefully it's nothing
2. Got stung by a wasp {minutes} minutes ago, I'm covered in welts and swallowing feels difficult.
   - e.g. `hib-train-0062`: Good afternoon, Can I push my physical on the 9th to later in the day? This is probably nothing. Got stung by a wasp 20 minutes ago, I'm covered in welts and swallowing feels difficult. / Thanks in advance
3. Right after the first dose of {antibiotic} my tongue started to feel too big for my mouth.
   - e.g. `hib-train-0063`: Right after the frst dose of amoxicillin my tongue started to feel too big for my mouth.
4. My {child} broke out in welts after eating {allergen} and is coughing, and their face is getting puffy.
   - e.g. `hib-train-0064`: Good afternoon, I'm almost out of my pantoprazole, if someone can send that in. My son broke out in welts after eating peanuts and is coughing, and their face is getting puffy.

### `train.en.infant_fever`

**nurse, urgent** (possible emergency; sign: fever in a baby under 3 months). The baby's age as an adjective (a five-week-old), the temperature in Fahrenheit or Celsius, with poor feeding or sleepiness.

1. Our {baby_age} baby feels hot, the thermometer says {infant_temp}, and she isn't interested in feeding.
   - e.g. `hib-train-0073`: Our five-week-old baby feels hot, the thermometer says 100.6, and she isn't interested in feeding.
2. My {baby_age} son is burning up at {infant_temp} and is much sleepier than usual.
   - e.g. `hib-train-0074`: My five-week-old son is burning up at 100.6 and is much sleepier than usual. I'm sure it's fine, just checking. / Thanks in advance
3. Took our {baby_age}'s temperature and it read {infant_temp}. Is that a problem at this age?
   - e.g. `hib-train-0075`: Took our seven-week-old's temperature and it read 101.3. Is that a problem at this age?

### `train.en.altered`

**nurse, urgent** (possible emergency; sign: hard to wake, fainting or newly confused). New confusion, a blackout or drowsiness told as behaviour (does not know the year, woke up on the floor, drifts off).

1. {woman} doesn't know what year it is and keeps asking where she is, this started today.
   - e.g. `hib-train-0085`: Good afternoon, My wife doesn't know what year it is and keeps asking where she is, this started today.
2. I blacked out in the kitchen and woke up on the floor, I don't know how long I was down.
   - e.g. `hib-train-0086`: I blacked out in the kitchen and woke up on the floor, I don't know how long I was down. / Appreciate it
3. {man} is so drowsy I have to shake him to get an answer, and then he drifts off again.
   - e.g. `hib-train-0087`: Not sure if this is worth a message. My brother is so drowsy I have to shake him to get an answer, and then he drifts off again.

### `train.en.head_injury`

**nurse, urgent** (possible emergency; sign: a head injury followed by vomiting or confusion). A fall or knock to the head followed by vomiting, queasiness or a memory gap.

1. My {child} fell off the monkey bars and smacked the back of the head, and has thrown up twice since.
   - e.g. `hib-train-0097`: my toddler fell off the monkey bars and smacked the back of the head, and has thrown up twice since. hopefully it's nothing
2. I slipped on ice and cracked my head on the step, now I'm queasy and can't remember the last hour.
   - e.g. `hib-train-0098`: not sure if this is worth a message. i slipped on ice and cracked my head on the step, now i'm queasy and can't remember the last hour
3. {man} hit his head getting out of the car and now he's vomiting and seems foggy.
   - e.g. `hib-train-0099`: Can someone look at my last statement? It looked off. My husband hit his head getting out of the car and now he's vomiting and seems foggy. No rush on this.

### `train.en.severe_pain`

**nurse, urgent** (possible emergency; sign: sudden severe pain in the head or belly). Sudden severe head or belly pain told by its onset and what it stops the person doing.

1. Out of nowhere the worst headache I've ever had hit me, like a thunderclap, about an hour ago.
   - e.g. `hib-train-0109`: Good afternoon, Out of nowhere the worst headache I've ever had hit me, like a thunderclap, about an hour ago. I'm sure it's fine, just checking.
2. My stomach suddenly hurts so much I can't stand up straight, and it feels hard as a board.
   - e.g. `hib-train-0110`: Sorry to bother you with this. My stomach suddenly hurts so much I can't stand up straight, and it feels hard as a board. / Thanks in advance
3. Sudden stabbing pain low on my right side, I'm doubled over and can't walk.
   - e.g. `hib-train-0111`: Can someone look at my last statement? It looked off. Not sure if this is worth a message. Sudden stabbing pain low on my right side, I'm doubled over and can't walk.

### `train.en.schedule`

**front_desk, routine** (routine). Booking, moving and cancelling, told around the person's week.

1. Is there any chance of a {visit_type} with {doctor} on {day}? I work nights so late morning is ideal.
   - e.g. `hib-train-0121`: Is thre any chance of a physical with Dr. Ferreira on Monday? I work nighst so late morning is ideal. / Thanks in advance
2. Something came up and I won't make it on {day} at {time}. Could you find me another slot?
   - e.g. `hib-train-0122`: Good afternoon, Something came up and I won't make it on Tuesday at 8:40. Could you find me another slot?
3. Please take me off the schedule for {day}, I'll reach out when things settle down.
   - e.g. `hib-train-0123`: pls take me off the shcedule for monday, i'll reach out when things settle down
4. I'd like to get on the books for a {visit_type}, whatever is soonest.
   - e.g. `hib-train-0124`: i'd like to get on the books for a follow-up, whatever is soonest

### `train.en.logistics`

**front_desk, routine** (routine). Entrances, parking, hours, the portal and contact details.

1. Which entrance do I use for the lab, and is the parking garage validated?
   - e.g. `hib-train-0137`: Which entrance do I use for the lab, and is the parking garage validated?
2. My portal password reset link never arrives. Can someone unlock my account?
   - e.g. `hib-train-0138`: my portal psasword reset link never arrives. can someone unlock my account?
3. Do you have evening hours during the week, or only weekends?
   - e.g. `hib-train-0139`: Do you have evening hours during the week, or only weekends?
4. I changed jobs and my cell number is different now. Who do I give it to?
   - e.g. `hib-train-0140`: Good afternoon, I changde jobs and my cell numbre is different now. Who do I give it to?

### `train.en.refill`

**medical_assistant, routine** (routine). Routine refills and renewals, told by what the pharmacy said or how many are left.

1. Down to my last few {med} tablets. Can a new prescription go to {pharmacy}?
   - e.g. `hib-train-0153`: Down to my last few glipizide tablets. Can a new prescription go to the pharmacy on Birch Road? / Appreciate it
2. {pharmacy_cap} says the {med} script expired and they need your office to okay it.
   - e.g. `hib-train-0154`: the drive-through pharmacy says the carvedilol scritp expierd and they need your office to okay it
3. Could I get a three month supply of my {med} this time instead of thirty days?
   - e.g. `hib-train-0155`: Could I get a three month supply of my bupropion this time instead of thirty days? Can someone look at my last statement? It looked off. / Appreciate it
4. My {med} renewal has been pending for a week, is it waiting on something from me?
   - e.g. `hib-train-0156`: my pravastatin renewal has been pending for a week, is it waiting on something from me?

### `train.en.records`

**medical_assistant, routine** (routine). Records to send or receive, vaccine history, a form needing clinical dates, a lost referral.

1. Please send my last two years of visit notes to the {specialty} clinic at {hospital}.
   - e.g. `hib-train-0169`: Hi there, Please send my last two years of visit notes to the rheumatology clinic at Harbor View Hospital. Can I push my follow-up on Tuesday to later in the day?
2. I need a copy of my vaccine history for a new job. Can you upload it to the portal?
   - e.g. `hib-train-0170`: I need a copy of my vaccine history for a new job. Can you upload it to the portal? / Appreciate it
3. My employer needs the {form} completed with my diagnosis dates. Where should I drop it off?
   - e.g. `hib-train-0171`: Can I push my follow-up on Tuesday to later in the day? My employer needs the FMLA paperwork completed with my diagnosis dates. Where should I drop it off?
4. The {specialty} office keeps telling me they never got the referral. Can you resend it?
   - e.g. `hib-train-0172`: The urology office keeps telling me they never got the referral. Can you resend it?

### `train.en.symptoms`

**nurse, routine** (routine). Mild, new or lingering symptoms with a question about being seen.

1. I've had a {symptom} for {duration}. It isn't getting better, should someone take a look?
   - e.g. `hib-train-0185`: I've had a scratchy throat for four days. It isn't getting better, should someone take a look? / Thanks in advance
2. Is a {symptom} that comes and goes for {duration} worth an appointment, or should I wait?
   - e.g. `hib-train-0186`: Is a mild headache that comes and goes for two days worth an appointment, or should I wait? Hopefully it's nothing.
3. Woke up with a {symptom} again today. I'm eating and drinking fine, just wondering what to try.
   - e.g. `hib-train-0187`: Hi there, Not sure if this is worth a message. Woke up with a scratchy throat again today. I'm eating and drinking fine, just wondering what to try. / Thanks in advance

### `train.en.side_effects`

**nurse, routine** (routine). A side effect of a medication, asked about with its timing.

1. Ever since the {med} was started I've had {side_effect}. Is that something that settles down?
   - e.g. `hib-train-0201`: I'm almost out of my carvedilol, if someone can send that in. Ever since the bupropion was started I've had a metallic taste. Is that something that settlse down?
2. I'm getting {side_effect} on the {med}. Should I keep taking it until my next visit?
   - e.g. `hib-train-0202`: Hi there, I'm getting headaches on the carvedilol. Should I keep taking it until my next visit?
3. Is {side_effect} a known thing with {med}? It started a few days after the first dose.
   - e.g. `hib-train-0203`: Is headaches a known thing with carvedilol? It started a few days after the first dose. / Thanks in advance

### `train.en.post_procedure`

**nurse, routine** (routine). Stitches, healing and vaccines after a procedure or a shot.

1. The stitches from {procedure} are itchy and a bit pink. How do I keep them clean?
   - e.g. `hib-train-0217`: the stitches from the skin biopsy r itchy and a bit pink. how do i keep them clean? i'm almost out of my glipizide, if someone can send that in
2. How long after {procedure} can I go swimming?
   - e.g. `hib-train-0218`: How long afetr my carapl tunnel release can I go swimming?
3. My arm is sore and warm where I got my shot yesterday. Is ice okay?
   - e.g. `hib-train-0219`: Good afternoon, My arm is sore and warm whee I got my shot yesterday. Is ice okay?
4. Am I due for a pneumonia vaccine at my age, and can I get it with the flu shot?
   - e.g. `hib-train-0220`: i'm almost out of my pantoprazole, if someone can send that in. not sure if this is worth a message. am i due for a pneumonia vaccine at my age, and can i get it with the flu shot?

### `train.en.results`

**physician, routine** (routine). A result to interpret, with the value and what it means for treatment.

1. The portal shows {lab_result} with a little red flag next to it. What does that mean for me?
   - e.g. `hib-train-0233`: this is probably nothing. the portal shows an ldl of 171 with a little red flag next to it. what does that mean for me?
2. Could {doctor} look at my latest labs and tell me if {lab_result} changes anything?
   - e.g. `hib-train-0234`: Hi there, Can I push my follow-up on Monday to later in the day? Could Dr. Kowalski look at my latest labs and tell me if an LDL of 171 changes anything?
3. Last year my numbers were fine, now it says {lab_result}. Do we need to do something about it?
   - e.g. `hib-train-0235`: Hi there, Last year my numbers were fine, now it says a TSH of 6.8. Do we need to do something about it?

### `train.en.treatment`

**physician, routine** (routine). A dose change, stopping or switching a drug, or the physician's opinion on surgery.

1. I'd like {doctor}'s opinion on whether I still need the {med} now that I've changed my diet.
   - e.g. `hib-train-0249`: I'd like Dr. Abara's opinion on whether I still need the bupropion now that I've changed my diet. No rush on this. / Appreciate it
2. My blood pressure at home keeps running high on the {bp_med}. Is it time to go up on the dose?
   - e.g. `hib-train-0250`: My blood pressre at home keeps running high on the chlorthalidone. Is it time to go up on the dose? Can someone look at my last statement? It looked off.
3. Would {doctor} consider switching me off {med}? I'd rather try something with fewer pills.
   - e.g. `hib-train-0251`: Hi there, Would Dr. Ferreira consider switching me off pravastatin? I'd rather try something with fewer pills. Hopefully it's nothing.
4. Is the surgery my orthopedist suggested really necessary, or are there other options to try first?
   - e.g. `hib-train-0252`: Not sure if this is worth a message. Is the surgery my orthopedist suggested really necessary, or are there other options to try first?

### `train.en.bill`

**billing, routine** (routine). A charge thought covered, a statement, a double bill.

1. There's a charge of {amount} from {date} that I think insurance already paid. Can you check?
   - e.g. `hib-train-0265`: There's a charge of $62.40 from May 2 that I think insuranc alreayd paid. Can you check?
2. Why does my statement show {amount} for a {visit_type}? I was told it was covered.
   - e.g. `hib-train-0266`: Hi there, Why does my statement show $62.40 for a physical? I was told it was covered.
3. I was billed twice for the same visit on {date}.
   - e.g. `hib-train-0267`: i was billed twice for the same visit on february 11

### `train.en.insurance`

**billing, routine** (routine). A new plan, a cost estimate, paying a balance over time.

1. My plan changes on the first of the month. How do I get you the new card?
   - e.g. `hib-train-0281`: Good afternoon, My plan changes on the first of the month. How do I get you the new card? / Appreciate it
2. Can I get an estimate for a {visit_type} before I book it? I have a high deductible.
   - e.g. `hib-train-0282`: Can I get an estimate for a follow-up before I book it? I have a high deductible.
3. I'd like to pay off {amount} over a few months. Is that possible?
   - e.g. `hib-train-0283`: i'd like to pay off $1,080 over a few months. is that possible?

### `train.en.past_assessed`

**medical_assistant, routine** (look-alike; looks like: chest pain or pressure, stroke signs (face drooping, arm weakness, slurred speech), trouble breathing). A past emergency already assessed, asked about as paperwork.

1. I was in the ER at {hospital} last month for chest pressure, they said it was my muscles. Can you get those notes for my file?
   - e.g. `hib-train-0297`: i was in the er at mercy west last month for chest pressure, they said it was my muscles. can you get those ntoes for my file?
2. After the stroke scare in the spring, the neurologist at {hospital} cleared me. Please request the report.
   - e.g. `hib-train-0298`: After the stroke scare in the spring, the neurologist at Harbor View Hospital cleared me. Please request the report. Can I push my follow-up on Tuesday to later in the day?
3. Please add the discharge summary from my asthma admission in {month} to my chart.
   - e.g. `hib-train-0299`: pls add the discharge summary from my asthm admission in mrach to my chart

### `train.en.negated`

**nurse, routine** (look-alike; looks like: chest pain or pressure, trouble breathing, stroke signs (face drooping, arm weakness, slurred speech)). Red-flag words plainly negated, the actual complaint mild.

1. No chest pressure and I'm breathing fine, just a {symptom} that won't quit.
   - e.g. `hib-train-0307`: Good afternoon, No chest pressure and I'm breathing fine, just a scratchy throat that won't quit. / Thanks in advance
2. I'm not short of breath at all, the cough is just keeping me up at night.
   - e.g. `hib-train-0308`: Sorry to bther you with this. I'm not short of breath at all, the cough is just keeping me up at night. / Appreciate it
3. Not having any trouble talking or moving my arms, only a {symptom} for {duration}.
   - e.g. `hib-train-0309`: Hi there, Can someone look at my last statement? It lookde off. Not having any troule talking or moving my arms, only a mild headache for four days. / Appreciate it

### `train.en.idiom`

**billing, routine** (look-alike; looks like: chest pain or pressure, thoughts of suicide or self-harm, or an intentional overdose). Red-flag words used as figures of speech about a bill.

1. This bill for {amount} nearly gave me a heart attack. Can someone explain it?
   - e.g. `hib-train-0317`: This bill for $1,080 nearly gave me a heart attack. Can someoe explain it? / Thanks in advance
2. The copays are killing me. Is there a payment plan?
   - e.g. `hib-train-0318`: Good afternoon, The copays are killing me. Is there a payment plan?
3. I about died when I saw the {amount} charge from {date}. What is it for?
   - e.g. `hib-train-0319`: Good afternoon, I about died when I saw the $215 charge from February 11. What is it for?

### `train.en.education`

**nurse, routine** (look-alike; looks like: stroke signs (face drooping, arm weakness, slurred speech), a severe allergic reaction, fever in a baby under 3 months). Questions about warning signs, asked ahead of time.

1. My dad just got diagnosed with high blood pressure. What stroke warning signs should we watch for?
   - e.g. `hib-train-0327`: Good afternoon, I'm almost out of my pantoprazole, if someone can send that in. My dad just got diagnoesd with high blood pressure. What stroke warning sings should we watch for? No rush on this.
2. What should I do if my son ever has an allergic reaction at school? He's allergic to {allergen}.
   - e.g. `hib-train-0328`: This is probably nothing. What should I do if my son ever has an allergic reaction at school? He's allergic to walnuts. / Appreciate it
3. How do I know when a fever is high enough to call you?
   - e.g. `hib-train-0329`: Good afternoon, How do I know when a fever is high enough to call you? I'm almost out of my carvedilol, if someone can send that in.

### `train.en.older_child_fever`

**nurse, routine** (look-alike; looks like: fever in a baby under 3 months). Fever in a child old enough to be past the infant rule, drinking and playing.

1. My {older_child} has had a fever of {child_temp} since last night but is drinking and playing. Do we need to come in?
   - e.g. `hib-train-0337`: Good afternoon, Can I push my follow-up on the 9th to later in the day? My three year old has had a fever of 100.8 since last night but is drinking and playing. Do we need to come in?
2. Our {older_child} felt warm after daycare, {child_temp}, but is acting like normal.
   - e.g. `hib-train-0338`: Our six year old felt warm aftre daycare, 100.8, but is actin like normal.

### `train.en.minor_injury`

**nurse, routine** (look-alike; looks like: heavy bleeding, vomiting or coughing up blood, bleeding in pregnancy, a head injury followed by vomiting or confusion). A small cut or bump that has already settled.

1. I nicked my finger chopping onions, it stopped bleeding after a minute of pressure. Do I need a tetanus shot?
   - e.g. `hib-train-0347`: Good afternoon, I nicked my finger chopping onions, it stopped bleedig after a minute of pressure. Do I need a tetanus shot?
2. My {older_child} bumped their head on the table this morning, cried for a minute and has been totally normal since. Anything to watch for?
   - e.g. `hib-train-0348`: Hi there, My six year old bumped their head on the table this morning, cried for a minute and has been totally normal since. Anything to watch for? / Thanks in advance
3. Small scrape on my knee from a bike fall, it's clean and scabbing. Should I cover it?
   - e.g. `hib-train-0349`: small scrape on my knee from a bike fall, it's clean and scabbing. should i covr it? can i push my physical on the 9th to latr in the day?

### `train.es.chest_pain`

**nurse, urgent** (possible emergency; sign: chest pain or pressure). A weight or squeeze in the chest with cold sweat or arm pain.

1. Siento un peso enorme en el pecho desde hace {minutes} minutos y estoy sudando frío.
   - e.g. `hib-train-0357`: Ocupo que me surtan el bupropion pronto. Siento un peso enorme en el pecho desde hace 40 minutos y estoy sudando frío.
2. Me aprieta el pecho como si me lo exprimieran y me duele el brazo izquierdo.
   - e.g. `hib-train-0358`: Me aprieta el pecho como si me lo exprimieran y me duele el brazo izquierdo. Seguro no es nada. Tengo una duda sobre mi factura. / Gracias de antemano

### `train.es.breathing`

**nurse, urgent** (possible emergency; sign: trouble breathing). Not enough air to talk, a child pulling in at the ribs.

1. No me alcanza el aire para hablar, tengo que parar a cada rato.
   - e.g. `hib-train-0365`: No me alcanza el aire para hablar, tengo que parar a cada rato.
2. Mi {nino_es} respira muy rápido y se le hunden las costillas.
   - e.g. `hib-train-0366`: Buenas, Mi hijo respira muy árpido y se le hunden las costillas.

### `train.es.stroke`

**nurse, urgent** (possible emergency; sign: stroke signs (face drooping, arm weakness, slurred speech)). A twisted mouth and tangled speech, a hand that will not work.

1. A {hombre_es} se le torció la boca de repente y habla enredado.
   - e.g. `hib-train-0373`: Hola, buenas tardes. A mi esposo se le torció la boca de repente y habla enredado. Seguro no es nada.
2. De la nada no puedo mover bien la mano derecha y se me duerme la cara.
   - e.g. `hib-train-0374`: Perdón por molestar. De la nada no puedo mover bien la mano derecha y se me duerme la cara. Quiero cambiar mi cita del martes. / Que estén bien

### `train.es.self_harm`

**nurse, urgent** (possible emergency; sign: thoughts of suicide or self-harm, or an intentional overdose). Not wanting to go on with pills ready, fear of acting on thoughts of self-harm.

1. Ya no quiero seguir viviendo y tengo las pastillas listas.
   - e.g. `hib-train-0381`: ya no quiero seguir viviedno y tengo las pastillas listas. seguro no es nada
2. Pienso todo el tiempo en hacerme daño y hoy tengo miedo de hacerlo.
   - e.g. `hib-train-0382`: Hola, buenas tardes. Pineso todo el tiemp en hacerme daño y hoy tengo miedo de hacerlo. Quiero cambiar mi cita del lunes. / Que estén bien

### `train.es.schedule`

**front_desk, routine** (routine). Asking for or moving an appointment.

1. ¿Tienen algún espacio para una consulta el {dia_es} en la mañana?
   - e.g. `hib-train-0389`: Buenas, ¿Tienen alún espacio para una consluta el lunes en la mañana?
2. No voy a poder llegar a mi cita del {dia_es}, ¿me la pueden cambiar?
   - e.g. `hib-train-0390`: no voy a poder llegar a mi cita del lunes, ¿me la pueden cambiar?

### `train.es.refill`

**medical_assistant, routine** (routine). A refill running out, the pharmacy waiting on the office.

1. Ya casi se me termina el {med}, ¿pueden enviar otra receta a la farmacia?
   - e.g. `hib-train-0397`: quiero cambiar mi cita del martes. ya casi se me termina el bupropion, ¿pueden enviar otra receta a la farmacia?
2. En la farmacia dicen que necesitan autorización de ustedes para mi {med}.
   - e.g. `hib-train-0398`: en la farmacia dicen que necesitan autorización de ustedes para mi glipizie

### `train.es.symptoms`

**nurse, routine** (routine). A mild symptom that will not go away.

1. Tengo {sintoma_es} desde hace {duracion_es} y no se me quita. ¿Necesito cita?
   - e.g. `hib-train-0405`: Tengo dolor de garganta desde hace dos días y no se me quita. ¿Necesito cita? Seguro no es nada. / Gracias de antemano
2. Desde el {dia_es} ando con {sintoma_es}, ¿qué me recomiendan?
   - e.g. `hib-train-0406`: Hola, buenas tardes. Desde el lues ando con tos seca, ¿qué me recomiendan? Seguro no es nada. uQiero cambiar mi cita del lunes.

### `train.es.results`

**physician, routine** (routine). A result to interpret and whether treatment changes.

1. Me salió {resultado_es} en los análisis. ¿Qué quiere decir y tengo que cambiar algo?
   - e.g. `hib-train-0413`: me salió el azúcar en 180 en los análisis. ¿qué quiere decir y tengo que cambiar algo?
2. Quisiera que el doctor me explique si mis resultados cambian mi tratamiento.
   - e.g. `hib-train-0414`: Hola, buenas tardes. Quisiera que el doctor me explique si mis resultados cambian mi tratamiento.

### `train.es.bill`

**billing, routine** (routine). A charge the insurance should have paid, a bill to explain.

1. Me cobraron {amount} por una consulta que mi seguro debía cubrir.
   - e.g. `hib-train-0421`: Hola, buenas tardes. Me cobraron $1,080 por una consulta que mi seguro debía cubrir.
2. ¿Me pueden explicar la cuenta de {amount} que me mandaron?
   - e.g. `hib-train-0422`: ¿me puedn expicar la cuenta de $1,080 que me mandaron?

### `train.es.past_assessed`

**medical_assistant, routine** (look-alike; looks like: chest pain or pressure, trouble breathing). A past emergency visit already assessed, asked about as paperwork.

1. Estuve en emergencias en {hospital} por un dolor de pecho y me dijeron que no era del corazón. ¿Pueden pedir esos papeles?
   - e.g. `hib-train-0429`: Estuve en emergencias en Harbor View Hospital por un dolor de pecho y me dijeron que no era del corazón. ¿Pueden pedir esos papeles? Quiero cambiar mi cita del martes.
2. Hace un mes me revisaron en urgencias por falta de aire y salió todo bien. Necesito el reporte para mi expediente.
   - e.g. `hib-train-0430`: hace un mes me revisaron en urgencias por falta de aire y salió todo bien. necesito el reporte para mi expediente. quiero cambiar mi cita del lnues


## Held-out split

### `heldout.en.chest_pain`

**nurse, urgent** (possible emergency; sign: chest pain or pressure). Chest discomfort as an ache, a vise or a burning squeeze, with the spread, clamminess or pallor that goes with it.

1. Weird ache in the center of my chest that runs up into my jaw, it started {minutes} minutes ago and I'm clammy.
   - e.g. `hib-heldout-0001`: please note my new email for the portal. weird ache in the center of my chest that runs up into my jaw, it started 50 minutes ago and i'm clammy. get back to me whenever
2. {man} says his chest feels like it's caught in a vise and he has gone pale.
   - e.g. `hib-heldout-0002`: My father-in-law says his chest feels like it's caughtt in a vise and he has gone pale. / Thank you very much
3. Something is pressing down hard behind my sternum and my left arm feels heavy and odd.
   - e.g. `hib-heldout-0003`: Something is pressing down hard behind my sternum and my left arm feels heavy and odd.
4. This burning squeeze across my chest is worse than any heartburn I've had, and it's still going.
   - e.g. `hib-heldout-0004`: Hey, This burning squeeze across my chest is worse than any heartburn I've had, and it's still going. / Thank you very much

### `heldout.en.breathing`

**nurse, urgent** (possible emergency; sign: trouble breathing). Breathing trouble told as panting, gulping air, grey lips or an inhaler that does not help.

1. I'm working so hard to get air in that I can't lie down, I'm panting just sitting here.
   - e.g. `hib-heldout-0013`: I'm working so hard to get air in that I can't lie down, I'm pantiing just sitring here.
2. My {child} has gone grey around the lips and is breathing fast and noisy.
   - e.g. `hib-heldout-0014`: Please note my new email for the portal. I feel a bit silly writing this. My little boy has gone grey around the lips and is breathing fast and noisy. / Talk soon
3. Air won't go all the way in no matter what I do, and my rescue inhaler isn't touching it.
   - e.g. `hib-heldout-0015`: Air won't go all the way in no matter what I do, and my rescue inhaleer isn't toouching it.
4. {man} keeps stopping mid-word to gulp air and his chest is heaving.
   - e.g. `hib-heldout-0016`: Hey, My grandfather keeps stopping mid-word to gulp air and his chest is heaving. Not a big deal, I think.

### `heldout.en.stroke`

**nurse, urgent** (possible emergency; sign: stroke signs (face drooping, arm weakness, slurred speech)). Speech like being drunk, a mouth corner that will not move, a cup that cannot be held, an uneven face.

1. {woman} is talking like she's had too much to drink, but she hasn't touched any, and her arm keeps flopping down.
   - e.g. `hib-heldout-0025`: Could be nothing at all. My mother-in-law is talking like she's had too much to drink, but she hasn't touched any, and her arm keeps flolping down. / Thank you very much
2. One corner of my mouth won't move and my tongue feels thick, it came on within the last half hour.
   - e.g. `hib-heldout-0026`: one corner of my mouth won't move and my tongue feels thick, it came on within the last half hour
3. {man} is suddenly mixing up his words and can't hold his coffee cup in his right hand.
   - e.g. `hib-heldout-0027`: my uncle is suddenly mixing up his wordds and can't hold his coffee cup in his roght hand
4. I just looked in the mirror and my face is uneven, and the arm on that side is tingly and weak.
   - e.g. `hib-heldout-0028`: I just looked in the mirror and my face is uneven, and the arm on that side is tingly and weak. / Talk soon

### `heldout.en.self_harm`

**nurse, urgent** (possible emergency; sign: thoughts of suicide or self-harm, or an intentional overdose). Goodbye letters, saving pills, a teenager's disclosure, not wanting to wake up, extra pills taken on purpose.

1. I wrote goodbye letters to my kids today and I keep driving past the bridge near work.
   - e.g. `hib-heldout-0037`: Hey, Please note my new email for the portal. I wrote goodbye letters to my kids today and I keep driving past the bridge near work. Maybe I'm being dramatic.
2. I just want to go to sleep and not wake up, and I've been saving up my {med} for that.
   - e.g. `hib-heldout-0038`: I don't want to overreact. I just want to go to sleep and not wake up, and I've been saving up my propranolol for that.
3. My {teen_girl} told me she has been cutting herself and wishes she were dead.
   - e.g. `hib-heldout-0039`: My 16 year old daughter told me she has been cutting herself and wishes she were dead. / Talk soon
4. No point in any of this anymore. I took extra {med} on purpose a little while ago.
   - e.g. `hib-heldout-0040`: No point in any of this anymore. I took extra famotidine on purppose a little while ago.
5. I keep picturing how I would do it and tonight I'm scared I actually might.
   - e.g. `hib-heldout-0041`: I keep picturing how I would do it and tonight I'm scared I actually might. Maybe I'm being dramatic.

### `heldout.en.bleeding`

**nurse, urgent** (possible emergency; sign: heavy bleeding, vomiting or coughing up blood, bleeding in pregnancy). Bleeding in pregnancy implied by expecting, a hand soaking towels, black stools with near-fainting, dark red vomit, a long nosebleed on a blood thinner.

1. Expecting our second baby, about {weeks_preg} weeks, and all of a sudden there's a lot of bright red blood.
   - e.g. `hib-heldout-0049`: Expecting our second baby, about 18 weeks, and all of a sudden there's a lot of bright red blood. / Thank you very much
2. {man} sliced his hand open on a broken glass and it's pouring through two dish towels.
   - e.g. `hib-heldout-0050`: my uncle sliced his hand open on a broken glass and it's pouring through two dish towels
3. My stools have turned black and sticky and I nearly went down on the bathroom floor.
   - e.g. `hib-heldout-0051`: My stools have turned black and sticky and I nearly went down on the bathroom floor.
4. I keep bringing up dark red stuff when I vomit and I feel weak.
   - e.g. `hib-heldout-0052`: I keep bringing up dark red stuff when I vomit and I feel weak. / Thank you very much
5. My nosebleed has gone on for forty minutes, it's running down my throat, and I take {thinner}.
   - e.g. `hib-heldout-0053`: i don't want to overreact. my nosebleed has gone on for fforty minutes, it's running down my throat, and i take rivvaroxaban

### `heldout.en.anaphylaxis`

**nurse, urgent** (possible emergency; sign: a severe allergic reaction). A thick throat, eyes swelling shut, blotches with gagging, numb lips after a new drug, a food or an injection.

1. My mouth is tingling and my throat feels thick ever since I took the new {antibiotic}, and I'm wheezy.
   - e.g. `hib-heldout-0061`: Hello team, My mouth is tingling and my throat feels thick ever since I took the new clindamycin, and I'm wheezy. / Talk soon
2. After the {allergen} at dinner my eyes are swelling shut and I'm getting dizzy.
   - e.g. `hib-heldout-0062`: could be nothing at all. after the tree nuts at dinner my eyes are swelling shut and i'm getting dizzy. i still owe $480 and want to set up payments
3. My {child} got into a granola bar with {allergen} and now has red blotches everywhere and keeps gagging.
   - e.g. `hib-heldout-0063`: Hey, My four year old got into a granola bar with sessame and now has red blotches everywheree and keeps gagging. Maybe I'm being dramatic. / Talk soon
4. Itchy bumps popped up all over after my injection and now my lips are numb and I feel faint.
   - e.g. `hib-heldout-0064`: Hello team, Itchy bumps popped up all over after my injection and now my lips are numb and I feel faint. / Talk soon

### `heldout.en.infant_fever`

**nurse, urgent** (possible emergency; sign: fever in a baby under 3 months). The baby's age in weeks spelt out or as weeks since birth, temperatures in Fahrenheit and Celsius, with not latching or falling asleep feeding.

1. {infant_temp} on the rectal thermometer for our little one, who was born {baby_weeks} weeks ago, and she won't latch.
   - e.g. `hib-heldout-0073`: 101.8 on the rectal thermometer for our little one, who was born eight weeks ago, and she won't latch
2. My daughter is only {baby_weeks} weeks and feels warm all over, reading {infant_temp}, and she's barely eating.
   - e.g. `hib-heldout-0074`: Hey, I don't want to overreact. My dauughter is only six weeks and feels warm all over, reading 38.5 C, and she's barely eating. I still owe $480 and want to set up payments. / Thank you very much
3. The baby turned {baby_weeks} weeks yesterday and is running hot, {infant_temp}, and keeps falling asleep mid-bottle.
   - e.g. `hib-heldout-0075`: Hey, I don't want to overreact. The baby turned six weeks yesterday and is running hot, 38.5 C, and keeps falling asleep mid-bottle.

### `heldout.en.altered`

**nurse, urgent** (possible emergency; sign: hard to wake, fainting or newly confused). Being out of it, hard to rouse, going down in the shower or limp, told by a relative.

1. {man} has been out of it all afternoon, calling me by my mother's name, and he's never done that.
   - e.g. `hib-heldout-0085`: My uncle has been out of it all afternoon, calling me by my mother's name, and he's never done that. Maybe I'm being dramatic.
2. I went down in the shower this morning, everything went black, and my partner found me on the floor.
   - e.g. `hib-heldout-0086`: I went down in the shower this morning, everything went black, and my partner found me on the floor. Get back to me whenever.
3. {woman} took a nap and we can barely rouse her, she mumbles and goes right back under.
   - e.g. `hib-heldout-0087`: i feel a bit silly writing this. my grandmother took a nap and we can barely rouse her, she mumbles and goes right back under
4. My {child} is limp and floppy and won't really respond to me.
   - e.g. `hib-heldout-0088`: My luttle girl is limp and floopy and won't really respond to me. / Talk soon

### `heldout.en.head_injury`

**nurse, urgent** (possible emergency; sign: a head injury followed by vomiting or confusion). A tumble, a beam or a counter, then being sick, sleepy, muddled or repeating questions.

1. Our {child} tumbled down the stairs and landed head first, and now keeps being sick and is extra sleepy.
   - e.g. `hib-heldout-0097`: Hello team, Please note my new email for the portal. Our little boy tumbled down the stairs and landed head first, and now keeps being sick and is extra sleepy. Not a big deal, I think. / Thank you very much
2. I walked into a low beam at full speed this morning and since then I've thrown up and feel muddled.
   - e.g. `hib-heldout-0098`: Hello team, I feel a bit silly sriting this. I walked into a low beam at full speed this morniing and since then I've thrown up and feel muddled.
3. {woman} took a fall and knocked her head on the counter, now she keeps asking the same questions over and over.
   - e.g. `hib-heldout-0099`: Hey, Could be nothing at all. My grandmother took a fall and knocked her head on the counter, now she keeps asking the same questions over and over. / Thank you very much

### `heldout.en.severe_pain`

**nurse, urgent** (possible emergency; sign: sudden severe pain in the head or belly). Something bursting in the head, a belly too painful to touch, ten out of ten all at once.

1. It felt like something burst inside my head while I was lifting weights, and the pain is unbearable.
   - e.g. `hib-heldout-0109`: I still owe $2,310 and want to set up payments. It felt like something burst inside my head while I was lifting weights, and the pain is unbearable. Not a big deal, I think.
2. My belly hurts so much I'm curled up on the floor, it came on suddenly and I can't even touch it.
   - e.g. `hib-heldout-0110`: Hey, My pulmonology office still hasn't gotten the referral. My belly hurts so much I'm curled up on the floor, it came on suddenly and I can't even touch it. Maybe I'm being dramatic.
3. {man} is lying on the floor holding his stomach, says it's ten out of ten and it started all at once.
   - e.g. `hib-heldout-0111`: Hey, I feel a bit silly writing this. My father-in-law is lying on the floor holding his stomach, says it's ten out of ten and it started all at once.

### `heldout.en.schedule`

**front_desk, routine** (routine). Asking for room, bailing on a visit, dropping one, setting one up loosely.

1. Would {doctor} have room for me on {day}? Any time after lunch is fine.
   - e.g. `hib-heldout-0121`: Would Dr. Takahashi have room for me on the 23rd? Any time after lunch is fine.
2. I have to bail on my {time} visit on {day}, my kid's school called. What else is open?
   - e.g. `hib-heldout-0122`: Hello team, I have to bail on my 1:10 visit on Thursday, my kid's school called. What else is open?
3. Go ahead and drop my {visit_type} on {day}, I'm out of town that week.
   - e.g. `hib-heldout-0123`: Go ahead and drop my med check on Thursday, I'm out of town that week.
4. Need to set up a {visit_type} in the next month or so, I'm flexible.
   - e.g. `hib-heldout-0124`: Need to set up a med checkk in the next moonth or so, I'm flexible.

### `heldout.en.logistics`

**front_desk, routine** (routine). Holiday closures, the app, where an X-ray is, a new email.

1. Is the clinic closed for the holiday on Monday?
   - e.g. `hib-heldout-0137`: Hey, Is the clinic closed for the holiday on Monday? / Talk soon
2. The app logs me out every time I open my messages. Who can help with that?
   - e.g. `hib-heldout-0138`: Hello team, The app logs me out every time I open my messages. Who can help with that?
3. Where do I go for the X-ray, the main building or the annex?
   - e.g. `hib-heldout-0139`: Hey, Where do I go for the X-ray, the main building or the annex? / Thank you very much
4. Please put my new email on file, the old one bounces.
   - e.g. `hib-heldout-0140`: Please put my new email on file, the old one bounces.

### `heldout.en.refill`

**medical_assistant, routine** (routine). Running out, the pharmacy waiting, the usual dose, mail order.

1. I'll run out of {med} on the weekend. Can you send more to {pharmacy}?
   - e.g. `hib-heldout-0153`: I'll run out of duloxetine on the weekend. Can you send more to the grocery store pharmacy? / Talk soon
2. {pharmacy_cap} keeps saying they're waiting on you for my {med}.
   - e.g. `hib-heldout-0154`: Hello team, The pharmmacy near the hospital keeps saying they're waiging on you for my propranolol.
3. Requesting my usual {med}, same dose, I have two left.
   - e.g. `hib-heldout-0155`: Hey, Requesting my usual sitagliptin, same dose, I have two left. / Talk soon
4. Can the {med} be sent as a 90 day mail order going forward?
   - e.g. `hib-heldout-0156`: can the duloxetine be sent as a 90 day mail order going forward?

### `heldout.en.records`

**medical_assistant, routine** (routine). Old results to a new doctor, proof of shots, a form's history section, a referral that went quiet.

1. My new {specialty} doctor wants my old test results. Can you fax them over to {hospital}?
   - e.g. `hib-heldout-0169`: Hello team, I still owe $37.15 and want to set up payments. My new allergy doctor wants my old test results. Can you fax them over to the children's hospital downtown?
2. I need proof of my shots for college. Is there a printout you can give me?
   - e.g. `hib-heldout-0170`: i need proof of my shots for college. is there a printout you can give me?
3. Can someone fill out the {form} for me? It needs my medical history section done.
   - e.g. `hib-heldout-0171`: can someone fill out the camp health form for me? it needs my medical history section done
4. Still no call from {specialty} about the referral, could you check it went through?
   - e.g. `hib-heldout-0172`: still no call from sleep medicine about the referral, couldd you check it went through? i still owe $37.15 and want to set up payments

### `heldout.en.symptoms`

**nurse, routine** (routine). An annoying, lingering or mild symptom with no fever.

1. Had a {symptom} for {duration} now and it's annoying more than anything. Anything I can do at home?
   - e.g. `hib-heldout-0185`: had a stye for about five days now and it's annoying more than anything. anythinr i can do at home?
2. My {symptom} has hung around for {duration}. Do I need to come in?
   - e.g. `hib-heldout-0186`: Hello team, My sore knee has hung around for almost a week. Do I need to come in? Maybe I'm being dramatic.
3. Mild {symptom} that started {duration} ago, no fever. Should I just ride it out?
   - e.g. `hib-heldout-0187`: Mild dry cough that started abbout five days ago, no fever. Sjould I just ride it out?

### `heldout.en.side_effects`

**nurse, routine** (routine). A side effect with the time of day, the timing of the dose, or whether to stop.

1. The new {med} gives me {side_effect} most afternoons. Should I expect that?
   - e.g. `hib-heldout-0201`: The new propranolol gives me muccle achhes most afternoons. Should I expect that? / Talk soon
2. Can I take the {med} at night instead? It's causing {side_effect}.
   - e.g. `hib-heldout-0202`: Can I take the rosuvastatin at night instead? It's causing muscle aches. Not a big deal, I think. / Thank you very much
3. Been on {med} two weeks and the {side_effect} isn't going away. Should I stop?
   - e.g. `hib-heldout-0203`: Hey, Been on sitagliptin two weeks and the a dry mouth isn't going away. Should I stop? Please note my new email for the portal.

### `heldout.en.post_procedure`

**nurse, routine** (routine). Fluid from a site, lifting after a procedure, a shot after a cold, a scab.

1. A little clear fluid is coming from where they did {procedure}. Normal?
   - e.g. `hib-heldout-0217`: i still owe $2,310 and want to set up payments. could be nothing at all. a luttle clear fluid is coming from dhere they did my hernia repair. normal?
2. When can I lift my toddler again after {procedure}?
   - e.g. `hib-heldout-0218`: Hey, When can I lift my toddler again after the toenail removal?
3. Is it okay to get the shingles shot if I just had a cold?
   - e.g. `hib-heldout-0219`: I still owe $480 and want to set up payments. Is it okay to get the shingles shot if I just had a cold?
4. What should I put on the scab from {procedure}?
   - e.g. `hib-heldout-0220`: Hello team, What should I put on the scab from the toenail removal?

### `heldout.en.results`

**physician, routine** (routine). A value seen this morning, whether to repeat it, a value outside the printed range.

1. Saw {lab_result} on my results this morning. Is that bad, and does it change my treatment?
   - e.g. `hib-heldout-0233`: Saw an eGFR of 52 on my resultq this morning. Is that bad, and does it chanhe my treatment? Get back to me whenever. / Thank you very much
2. What should I make of {lab_result}? Would {doctor} want to repeat it?
   - e.g. `hib-heldout-0234`: Hello team, What should I make of a vitamin D of 14? Would Dr. Mbeki want to repeat it? / Thank you very much
3. The lab printed {lab_result} and marked it outside the range. What happens next?
   - e.g. `hib-heldout-0235`: The lab printed a vitamin D of 14 and marked it outside the range. What happens next? I still owe $480 and want to set up payments. / Talk soon

### `heldout.en.treatment`

**physician, routine** (routine). Lowering or stopping a drug, a relative's different medicine, whether to see a surgeon.

1. Could we try lowering my {med}? I've been feeling great and would like fewer meds.
   - e.g. `hib-heldout-0249`: Hey, Could be nothing at all. Could we try lowering my duloxetine? I've been feeling great and would like fewer meds.
2. {doctor}, what do you think about stopping the {med} for a while?
   - e.g. `hib-heldout-0250`: dr. sorensen, what do you think about stopping the rosuvastatin for a while? my sleep medicine office still hasn't gotten the referral
3. My sister got a different medicine for the same thing. Should I switch from {med}?
   - e.g. `hib-heldout-0251`: My allergy office still hasn't gotten the referral. My sister got a different medicine for the same thing. Should I switch from famotidine?
4. Is it worth seeing a surgeon for my knee, or would you keep treating it with therapy?
   - e.g. `hib-heldout-0252`: is it worth seeing a surgeon for my knee, or wwould you keep treating it with therapy?

### `heldout.en.bill`

**billing, routine** (routine). A statement not understood, a charge on a covered visit, a copay charged twice.

1. Got a statement for {amount} for {date} and I don't understand what it's for.
   - e.g. `hib-heldout-0265`: Got a statement for $2,310 for July 19 and I don't understand what it's for.
2. Why am I being charged {amount} when my plan covers preventive visits?
   - e.g. `hib-heldout-0266`: Hey, Why am I being charged $480 when my plan covers preventive visits?
3. My card was charged two times for the copay. I'd like one refunded.
   - e.g. `hib-heldout-0267`: my card was charged two times for the copay. i'd like one refunded

### `heldout.en.insurance`

**billing, routine** (routine). A new county plan, a denied claim, the price without insurance.

1. Do you take the new county health plan? I'm switching next month.
   - e.g. `hib-heldout-0281`: do you take the new county health plan? i'm switching next month
2. My claim from {date} was denied. What do I do next?
   - e.g. `hib-heldout-0282`: Hello team, My claim from July 19 was denied. What do I do next? / Talk soon
3. How much would a {visit_type} run without insurance?
   - e.g. `hib-heldout-0283`: Hey, How much would a viseo visit run without insurance?

### `heldout.en.past_assessed`

**medical_assistant, routine** (look-alike; looks like: trouble breathing, a severe allergic reaction, chest pain or pressure). A past hospital stay, urgent care visit or cardiac test already assessed, asked about as paperwork.

1. Last winter I spent a night at {hospital} for a blood clot in my lung. Could you get that record for my new specialist?
   - e.g. `hib-heldout-0297`: Last winter I spent a night at Pine Ridge Medical Center for a blood clot in my lung. Could you get that record for my new specialist?
2. Two years ago I went to urgent care for a bad allergic reaction. I need the visit notes sent to my new allergist.
   - e.g. `hib-heldout-0298`: Hello team, Two years ago I went to urgent care for a bad allergic reaction. I need the visit notes sent to my new allergist. / Talk soon
3. The cardiologist at {hospital} did a stress test after my chest pain in {month} and it was normal. Please pull in the results.
   - e.g. `hib-heldout-0299`: Heplo team, The cardiologist at the children's hospital downtown did a stress test after my chest pain in June and it was normal. Please pull in the results.

### `heldout.en.negated`

**nurse, routine** (look-alike; looks like: trouble breathing, chest pain or pressure, stroke signs (face drooping, arm weakness, slurred speech), heavy bleeding, vomiting or coughing up blood, bleeding in pregnancy). Red-flag words negated in other ways, the actual complaint mild.

1. Breathing is totally normal and there's no pain in my chest, I just feel run down with a {symptom}.
   - e.g. `hib-heldout-0307`: Breathing is totally normal and there's no pain in my chest, I just feel run down with a dry cough.
2. To be clear, nothing like the stroke signs from the TV ads, just a {symptom} that won't go away.
   - e.g. `hib-heldout-0308`: Hello team, To be clear, nothing like the stroke signs from the TV ads, just a sore knee that won't go away.
3. No blood in anything and no fever, but my stomach has been unsettled for {duration}.
   - e.g. `hib-heldout-0309`: i don't want to overreact. no blood in anything and no fever, but my stomach has been unsettled for almost a week

### `heldout.en.idiom`

**billing, routine** (look-alike; looks like: thoughts of suicide or self-harm, or an intentional overdose, trouble breathing, chest pain or pressure). Other figures of speech about a bill: dying to know, breath taken away, a heart that sank.

1. I'm dying to know why this {amount} bill showed up, I paid in full.
   - e.g. `hib-heldout-0317`: Hey, I'm dying to know why this $2,310 bill showec up, I paid in full. / Talk soon
2. Opening this statement took my breath away, {amount} for a ten minute visit?
   - e.g. `hib-heldout-0318`: opening this stqtement took my breath away, $37.15 for a ten minhte visit?
3. My heart sank when I saw the {amount} charge from {date}. Can you explain it?
   - e.g. `hib-heldout-0319`: Hey, My heaft sank when I saw the $37.15 chxrge from September 30. Can you explain it?

### `heldout.en.education`

**nurse, routine** (look-alike; looks like: a severe allergic reaction, heavy bleeding, vomiting or coughing up blood, bleeding in pregnancy, fever in a baby under 3 months). Warning signs to teach a sitter, bleeding on a blood thinner, a newborn's temperature before the birth.

1. What are the signs of a serious allergic reaction I should teach the babysitter?
   - e.g. `hib-heldout-0327`: Hello team, What are the signs of a serious allergic reaction I should teach the babysitter? / Talk soon
2. My mom is starting blood thinners. What kind of bleeding should make us call?
   - e.g. `hib-heldout-0328`: my mom is sstarting blood thinners. what kind of bleeding should make us call?
3. At what temperature should I worry with a newborn? We're expecting in {month}.
   - e.g. `hib-heldout-0329`: Hello team, At what temperature should I worry with a newborn? We're expecting in June.

### `heldout.en.older_child_fever`

**nurse, routine** (look-alike; looks like: fever in a baby under 3 months). Fever after a vaccine or with a cold in an older child who is eating and playing.

1. My {older_child} spiked {child_temp} after the vaccine yesterday but is eating and running around. Is that normal?
   - e.g. `hib-heldout-0337`: Hey, My nine year old spiked 101.2 after the vaccine yesterday but is eating and running around. Is that normal?
2. Our {older_child} has a fever around {child_temp} and a runny nose, still playing as usual. Should we be seen?
   - e.g. `hib-heldout-0338`: our two year old has a fever around 101.2 and a runny nose, still playing as usual. should we be seen?

### `heldout.en.minor_injury`

**nurse, routine** (look-alike; looks like: heavy bleeding, vomiting or coughing up blood, bleeding in pregnancy, a head injury followed by vomiting or confusion). A paper cut, a bump on the forehead, a bruise, each already settled.

1. Paper cut on my thumb bled for a few seconds, it's fine now, but it looks a little red. Should I put anything on it?
   - e.g. `hib-heldout-0347`: Paper cut on my thumb bled for a few seconds, it's fine now, but it looks a little red. Should I put anything on it?
2. My {older_child} bonked their forehead on the swing, has a small bump, and is acting completely normal. Should we ice it?
   - e.g. `hib-heldout-0348`: Hey, My two year old bonked their forehead on the swing, has a small bump, and is acting completely normal. Should we ice it?
3. Bruise on my shin from the coffee table, no swelling. How long do these take to fade?
   - e.g. `hib-heldout-0349`: Hello team, Bruise on my shin from the coffee table, no swelling. How long do these take to fade? I still owe $480 and want to set up payments. / Thank you very much

### `heldout.es.chest_pain`

**nurse, urgent** (possible emergency; sign: chest pain or pressure). An odd pressure rising to the jaw, a burning squeeze with pallor and sweat.

1. Tengo una presión rara en medio del pecho que me sube a la mandíbula y no se me quita.
   - e.g. `hib-heldout-0357`: no quiero exagerar. tengo una presión rara en medio del pecho que me sube a la mandíbula y no se me quita
2. A {hombre_es} le arde y le aprieta el pecho, está pálido y sudado.
   - e.g. `hib-heldout-0358`: A mi suegro le arde y le aprieta el pecho, está pálido y sudado. Todavía no me llaman de la referencia.

### `heldout.es.breathing`

**nurse, urgent** (possible emergency; sign: trouble breathing). Struggling to pull in air, an inhaler that does nothing, a relative panting.

1. Me cuesta muchísimo jalar aire y el inhalador no me hace nada.
   - e.g. `hib-heldout-0365`: Qué tal, No quiero exagerar. Me cuesta muchísimo jalar aire y el inhalador no me hace nada.
2. {mujer_es} está jadeando y no puede terminar una frase.
   - e.g. `hib-heldout-0366`: Mi tía está jadeando y no puede terminar una frase. / Bendiciones

### `heldout.es.stroke`

**nurse, urgent** (possible emergency; sign: stroke signs (face drooping, arm weakness, slurred speech)). Words that will not come with a weak arm, a crooked face with a dragging leg.

1. De pronto a mi abuela no le salen las palabras y tiene el brazo sin fuerza.
   - e.g. `hib-heldout-0373`: A lo mejor no es nada. De pronto a mi abuela no le salen las palabras y tiene el brazo sin fuerza.
2. Se me ve la cara chueca en el espejo y arrastro la pierna izquierda.
   - e.g. `hib-heldout-0374`: se me ve la cara chueca en el espejo y arrastro la pierna izquierda. cuando puedan me dicen

### `heldout.es.self_harm`

**nurse, urgent** (possible emergency; sign: thoughts of suicide or self-harm, or an intentional overdose). Everyone better off without me with pills saved, many pills taken on purpose.

1. Siento que todos estarían mejor sin mí y estuve guardando mis pastillas para eso.
   - e.g. `hib-heldout-0381`: todavía no me llaman de la referencia. a lo mejor no es nada. siento que todos estarían mejor sin mí y estuve guardando mis pasttillas pa eso
2. Me tomé muchas de mis pastillas a propósito hace un rato.
   - e.g. `hib-heldout-0382`: Me tomé muchas de mis pasttillas a propósitto hace un rato.

### `heldout.es.schedule`

**front_desk, routine** (routine). Booking an afternoon visit, cancelling for a trip.

1. Quisiera sacar una cita con el doctor para el {dia_es}, de preferencia en la tarde.
   - e.g. `hib-heldout-0389`: quisiera sacar una cita con el doctor para el jueves, de preferencia en la tarde
2. Cancelen por favor mi consulta del {dia_es}, estaré de viaje.
   - e.g. `hib-heldout-0390`: Buen día, Cancelen por favor mi consulta del viernes, estagé de viaje. / Saludos

### `heldout.es.refill`

**medical_assistant, routine** (routine). Few pills left, renewing the same dose.

1. Me quedan pocas pastillas de {med}, ¿me hacen el favor de mandar la receta?
   - e.g. `hib-heldout-0397`: Qué tal, Me quedan pocas pastillas de propranolol, ¿me hacen el favor de mandar la receta?
2. ¿Pueden renovar mi {med}? Es la misma dosis de siempre.
   - e.g. `hib-heldout-0398`: me llegó una cuenta que no entiendo. ¿pueden renovar mi famotidine? es la misma dosis de siempre

### `heldout.es.symptoms`

**nurse, routine** (routine). A lingering mild symptom, what to take for it.

1. Llevo {duracion_es} con {sintoma_es}. ¿Es para ir a consulta o espero?
   - e.g. `hib-heldout-0405`: Buen día, A lo mejor no es nada. Llevo casi una semaana con ddolor de oído. ¿Es para ir a consulta o espero?
2. Tengo un poco de {sintoma_es} y quería saber qué puedo tomar.
   - e.g. `hib-heldout-0406`: A lo mejor no es nada. Trngo un poco de comezón en la piel y quería saber qué pueddo tomar.

### `heldout.es.results`

**physician, routine** (routine). A value seen in the portal, a worrying result.

1. En el portal dice que tengo {resultado_es}. ¿Me puede decir el doctor qué significa?
   - e.g. `hib-heldout-0413`: En el portal dice que tengo los triglicéridos en 400. ¿Me puede decir el doctor qué significa?
2. Vi que salió {resultado_es} y me preocupa. ¿Hay que hacer algo?
   - e.g. `hib-heldout-0414`: Vi que salió la hemoglobina en 10 y me preocupa. ¿Hay que hacer algo?

### `heldout.es.bill`

**billing, routine** (routine). An unrecognised charge, paying a balance in parts.

1. Tengo un cargo de {amount} en mi cuenta que no reconozco.
   - e.g. `hib-heldout-0421`: Buen día, Tengo un cargo de $480 en mi cuenta que no reconozco. / Bendiciones
2. ¿Puedo pagar mi saldo de {amount} en partes?
   - e.g. `hib-heldout-0422`: ¿Puedo pagar mi saldo de $37.15 en partes? / Bendiciones

### `heldout.es.past_assessed`

**medical_assistant, routine** (look-alike; looks like: a severe allergic reaction, chest pain or pressure). A past allergic reaction or heart scare already assessed, asked about as paperwork.

1. El año pasado me atendieron en {hospital} por una reacción alérgica fuerte. ¿Pueden pedir el informe para mi nuevo doctor?
   - e.g. `hib-heldout-0429`: cambié de número de teléfono, por favor actualícenlo. el año pasado me atendieron en pine ridge medical center por una reacción alérgica fuerte. ¿pueden pedir el informe pa mi nuevo doctor?
2. Me hicieron estudios del corazón en {hospital} después de un susto en {mes_es} y todo salió normal. Quiero esos resultados en mi expediente.
   - e.g. `hib-heldout-0430`: me hicieron estudios del corazón en the children's hospital downtown después de un susto en octubre y todo salió normal. quiero esos resultados en mi expediente. me llegó una cuebta que no entiendo

## Slot values

| Slot | train | heldout |
|---|---|---|
| `allergen` | peanuts; walnuts | tree nuts; sesame |
| `amount` | $62.40; $215; $1,080 | $37.15; $480; $2,310 |
| `antibiotic` | amoxicillin; cephalexin | clindamycin; doxycycline |
| `baby_age` | five-week-old; seven-week-old; two-month-old | (none) |
| `baby_weeks` | (none) | six; eight; ten |
| `bp_med` | chlorthalidone; carvedilol | (none) |
| `child` | son; daughter; toddler | little boy; little girl; four year old |
| `child_temp` | 100.8; 101.5 | 101.2; 38.6 C |
| `date` | February 11; May 2 | July 19; September 30 |
| `day` | Monday; Tuesday; the 9th | Thursday; Friday; the 23rd |
| `dia_es` | lunes; martes | jueves; viernes |
| `doctor` | Dr. Abara; Dr. Kowalski; Dr. Ferreira | Dr. Mbeki; Dr. Sorensen; Dr. Takahashi |
| `duracion_es` | dos días; una semana | cuatro días; casi una semana |
| `duration` | two days; four days; about a week | three days; about five days; almost a week |
| `form` | FMLA paperwork; disability form | school physical form; camp health form |
| `hombre_es` | mi esposo; mi papá | mi abuelo; mi suegro |
| `hospital` | Harbor View Hospital; Mercy West | Pine Ridge Medical Center; the children's hospital downtown |
| `infant_temp` | 100.6; 101.3; 38.2 C | 100.9; 101.8; 38.5 C |
| `lab_result` | an A1c of 8.2; an LDL of 171; a TSH of 6.8; a ferritin of 9 | a B12 of 180; an ALT of 96; an eGFR of 52; a vitamin D of 14 |
| `man` | My husband; My dad; My brother | My grandfather; My uncle; My father-in-law |
| `med` | pravastatin; glipizide; carvedilol; bupropion; pantoprazole | rosuvastatin; sitagliptin; propranolol; duloxetine; famotidine |
| `mes_es` | (none) | junio; octubre |
| `minutes` | 20; 25; 40 | 15; 35; 50 |
| `month` | January; March | June; October |
| `mujer_es` | (none) | Mi abuela; Mi tía |
| `nino_es` | niño; hijo | (none) |
| `older_child` | three year old; six year old | two year old; nine year old |
| `pharmacy` | the pharmacy on Birch Road; the drive-through pharmacy | the grocery store pharmacy; the pharmacy near the hospital |
| `pharmacy_cap` | The pharmacy on Birch Road; The drive-through pharmacy | The grocery store pharmacy; The pharmacy near the hospital |
| `procedure` | the skin biopsy; my carpal tunnel release | my hernia repair; the toenail removal |
| `resultado_es` | el azúcar en 180; el colesterol en 260 | la hemoglobina en 10; los triglicéridos en 400 |
| `side_effect` | a metallic taste; loose stools; headaches | muscle aches; a dry mouth; trouble focusing |
| `sintoma_es` | dolor de garganta; tos seca | dolor de oído; comezón en la piel |
| `specialty` | rheumatology; neurology; urology | allergy; sleep medicine; pulmonology |
| `symptom` | scratchy throat; runny nose; rash on my elbow; mild headache | plugged ear; dry cough; sore knee; stye |
| `teen_girl` | (none) | 16 year old daughter; daughter in high school |
| `thinner` | warfarin; apixaban | rivaroxaban; dabigatran |
| `time` | 8:40; 11:15; 2:30 | 9:50; 1:10; 4:45 |
| `visit_type` | physical; follow-up | med check; video visit |
| `weeks_preg` | nine; 14; 22 | eleven; 18; 26 |
| `woman` | My wife; My mom; My sister | My grandmother; My aunt; My mother-in-law |

## Other sources

- `hedge.en.train` (a softening sentence before or after the message): (before) Sorry to bother you with this.; (before) Not sure if this is worth a message.; (before) This is probably nothing.; (after) I'm sure it's fine, just checking.; (after) Hopefully it's nothing.; (after) No rush on this.
- `hedge.en.heldout` (a softening sentence before or after the message): (before) I don't want to overreact.; (before) Could be nothing at all.; (before) I feel a bit silly writing this.; (after) Maybe I'm being dramatic.; (after) Get back to me whenever.; (after) Not a big deal, I think.
- `hedge.es.train` (a softening sentence before or after the message): (before) Perdón por molestar.; (before) No sé si es importante.; (after) Seguro no es nada.
- `hedge.es.heldout` (a softening sentence before or after the message): (before) A lo mejor no es nada.; (before) No quiero exagerar.; (after) Cuando puedan me dicen.
- `aside.en.train` (a second, administrative request (mixed topics)): (front_desk) Can I push my {visit_type} on {day} to later in the day?; (billing) Can someone look at my last statement? It looked off.; (medical_assistant) I'm almost out of my {med}, if someone can send that in.
- `aside.en.heldout` (a second, administrative request (mixed topics)): (front_desk) Please note my new email for the portal.; (billing) I still owe {amount} and want to set up payments.; (medical_assistant) My {specialty} office still hasn't gotten the referral.
- `aside.es.train` (a second, administrative request (mixed topics)): (front_desk) Quiero cambiar mi cita del {dia_es}.; (billing) Tengo una duda sobre mi factura.; (medical_assistant) Ocupo que me surtan el {med} pronto.
- `aside.es.heldout` (a second, administrative request (mixed topics)): (front_desk) Cambié de número de teléfono, por favor actualícenlo.; (billing) Me llegó una cuenta que no entiendo.; (medical_assistant) Todavía no me llaman de la referencia.
- `opener.en.train` (a portal greeting): Hi there,; Good afternoon,
- `opener.en.heldout` (a portal greeting): Hey,; Hello team,
- `opener.es.train` (a portal greeting): Hola, buenas tardes.; Buenas,
- `opener.es.heldout` (a portal greeting): Buen día,; Qué tal,
- `closer.en.train` (a portal sign-off): Thanks in advance; Appreciate it
- `closer.en.heldout` (a portal sign-off): Thank you very much; Talk soon
- `closer.es.train` (a portal sign-off): Gracias de antemano; Que estén bien
- `closer.es.heldout` (a portal sign-off): Saludos; Bendiciones
- `subject.en.train` (a portal subject that does not name the team): Quick question; Message for the office; Need some help
- `subject.en.heldout` (a portal subject that does not name the team): Checking in; Please read when you can; Hi
- `subject.es.train` (a portal subject): Pregunta; Mensaje
- `subject.es.heldout` (a portal subject): Consulta; Ayuda por favor
- `texting.en.train` (SMS style: lowercase and abbreviations): "you" as "u"; "please" as "pls"; "thanks" as "thx"; "because" as "bc"; "are" as "r"
- `texting.en.heldout` (SMS style: lowercase and abbreviations): "your" as "ur"; "please" as "plz"; "i don't know" as "idk"; "right now" as "rn"; "thank you" as "ty"; "with" as "w/"
- `texting.es.train` (SMS style: lowercase and abbreviations): "por favor" as "porfa"; "que" as "q"; "gracias" as "grax"
- `texting.es.heldout` (SMS style: lowercase and abbreviations): "también" as "tmb"; "para" as "pa"; "por favor" as "xfa"
- `typo.train` (adjacent letters swapped, or a letter dropped): transpose; drop
- `typo.heldout` (a neighbouring key struck, or a letter doubled): neighbour; double
