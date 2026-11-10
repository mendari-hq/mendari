# Legal pages: placeholders and open questions

2026-11-10. The draft terms of use, privacy notice and acceptable-use policy are on the public
page (`web/terms.html`, `web/privacy.html`, `web/acceptable-use.html`), for launch gate G2-3's
`legal-pages-live` (B16). They are drafts for the owner and counsel: none is legal advice, none
is in effect, each says so under its heading, and each is `noindex`. Nothing is deployed, and
the gate is not met: its attestation is the owner's, after counsel approves the texts.

The pages state only what the code and docs support. Where a fact is the owner's or counsel's
to supply, the page shows a bracketed placeholder. `tests/test_web_legal.py` holds this list and
the pages to the same set of placeholders.

## Placeholders

| Placeholder | Page | What is needed |
|---|---|---|
| `[legal entity name]` | terms, privacy | The legal entity that runs the services. The repository names only "The Mendari authors" |
| `[legal entity address]` | terms, privacy | Its postal address |
| `[addresses of the hosted services]` | terms | The public address of the hosted engine (and of the registry, the Hub and any sandbox once they are open). The docs name only `mendari.co` |
| `[contact address for legal questions]` | terms, acceptable use | An address for questions about the terms and for reports of misuse. The project has no e-mail address today; the security route is GitHub's private form |
| `[contact address for privacy requests]` | privacy | An address for access, correction and deletion requests, and for reporting patient data entered by mistake |
| `[governing law and venue]` | terms | Governing law, the courts or arbitration, and any consumer carve-outs |
| `[minimum age]` | terms, privacy | The age limit (18, or the age of majority) |
| `[whether a business associate agreement is offered]` | terms | Whether a BAA is ever offered for the hosted services. The docs say the hosted site is synthetic only and that a BAA is the deploying site's matter; no document says one is or is not offered |
| `[liability wording from counsel]` | terms | The limitation of liability, the cap, indemnities, and the form the warranty disclaimer must take to hold in the chosen law |
| `[notice period for changes]` | terms | How far ahead a material change is announced, and where |
| `[how the audit trail names people on the hosted engine]` | privacy | Which is true of the hosted engine today: in memberships mode the chain holds an id and a keyed digest of the address (`docs/operations/accounts.md`); ADR-0005 and `SECURITY.md` describe chaining the verified e-mail address itself, for good. Events chained before the switch (2026-11-07) may hold addresses |
| `[use of entered data for model training]` | privacy | Whether Mendari, Jev's operator or the LLM providers may train on what is entered, and under which of their terms |
| `[privacy rights and legal bases]` | privacy | The rights text each applicable law needs (US state laws, GDPR and UK GDPR if EU or UK users are allowed), the legal bases, and any representative or complaint route |
| `[model providers used by the hosted services]` | privacy | The providers the hosted engine can send case fields to today: who operates Jev, and which models sit behind the AI Gateway |
| `[list of subprocessors]` | privacy | The full list with purpose and place. The docs support Vercel (hosting, logs, AI Gateway), Neon (database), Google (sign-in) and GitHub (project, security reports) |
| `[where data is stored]` | privacy | The regions of the Vercel function and the Neon database, and the transfer wording that follows |
| `[retention period for stored cases and notes]` | privacy | `MENDARI_RETENTION_DAYS` on the hosted engine. The default is 30; the guide's example uses 90; production's value is not written down |
| `[retention period for account records]` | privacy | How long a person's sign-in record and membership are kept after access ends. Nothing deletes them today |
| `[log retention period]` | privacy | How long Vercel keeps runtime and request logs on the project's plan (an hour, a day or 30 days by plan), and any log drain |
| `[breach notification commitments]` | privacy | Whether to commit to a time limit. The incident runbook commits to telling affected organizations' administrators and posting to a status page, with no deadline |
| `[safe harbour for security research]` | acceptable use | Whether good-faith testing of the hosted services is authorized, and on what conditions. `SECURITY.md` has no safe-harbour wording and says not to test deployments you do not own |

## Questions for the owner

1. **Acceptance.** Nothing records that a person accepted the terms. The dashboard records the
   notice acknowledgement only (`notices.py`). Should signing in, or the acknowledgement, also
   record acceptance of a version of these terms? That is the unassigned build prompt in the
   gate.
2. **Where the pages are linked.** They are linked from `mendari.co`'s footer. The hosted
   engine's sign-in page, the dashboard, the Hub and the docs site do not link them yet, and
   Google's OAuth consent screen needs the terms and privacy URLs before it leaves Testing
   (B17).
3. **Production's settings.** The privacy notice says stored cases and notes are encrypted and
   purged daily. Confirm the hosted engine's PHI mode (`encrypt`), its retention days and that
   `CRON_SECRET` is set, since the docs list these as not verified by the project.
4. **Remote models on the hosted engine.** Jev keys alone turn Jev on. Confirm which remote
   providers are on, and whether the notice should name them or say none.
5. **Old audit entries.** If earlier chained events hold e-mail addresses, the notice must say
   they are kept for good. Confirm which is the case.
6. **Selling and sharing.** The notice says personal information is not sold or rented. That is
   a promise, not something the code shows: confirm it.
7. **The registry section.** The registry and the Hub are not deployed, and LP-19 is still
   changing how publishing works (claims, approval of a first publish, the administrator token's
   removal). Sections 6 (terms) and 5 (privacy) say they apply "once they are open": read them
   again when LP-19 lands.
8. **The Hub and the docs site.** No analytics were found in either, but neither is deployed.
   The privacy notice makes the "no scripts, no cookies, no analytics" claim only for
   `mendari.co` and the engine's pages.
9. **Removing the draft marker.** When counsel approves: remove the marker and `noindex`, set
   the date, deploy `web/`, and only then fill in the gate's attestation.

## Questions for counsel

1. Is the warranty disclaimer in plain words enough, or does the chosen law need conspicuous
   or set wording? What cap and exclusions belong in the liability section?
2. Do the "not a medical device, not medical advice, not for patient care" statements need
   particular wording for FDA or state-law purposes, given clinical decision support rules?
3. If real PHI is entered against the rules: what must the terms and the notice say about
   deletion, and does any notification duty arise without a BAA?
4. The audit trail and the registry's record are append-only by design. How should the notice
   describe deletion requests that cannot reach them, under each applicable privacy law?
5. Which users are allowed (US only, or wider)? That decides the rights text, transfer wording
   and whether a representative is needed.
6. Does the privacy notice need a section on children beyond the age limit, on "do not track"
   signals, or on state-specific disclosures (for example California's categories)?
7. Is the Developer Certificate of Origin enough for contributions, or is anything more wanted
   for packs published to the registry (a publisher agreement, a takedown procedure, a DMCA
   agent)?
8. Trademark: the terms say the Apache licence grants no right to the Mendari name. Should
   they add usage rules, or wait for the filing (B05)?
9. Security research: should a safe harbour be offered for the hosted services, and with what
   scope?
10. Benchmarks that name third-party models: do the terms need anything about publishing
    comparative results (the audit's `LIC.benchmark-terms`)?
