# The committee packet

A hospital's AI governance committee has to approve an agent before it touches a workflow. The
evidence it needs is in five places: the Decision Card, the publisher's evaluation, your own
decisions and shadow pilot, monitoring, and the approvals already recorded. The committee
packet joins them: one document per agent, at the version your deployment serves, as your
organization sees it.

It is a reading aid prepared by the software. It is not signed, and it is not a certification,
an attestation of compliance or legal advice.

## What it contains

Eleven sections, in the order a committee reads, then two appendices.

| # | Section | Where it comes from |
|---|---|---|
| 1 | What it is and who made it | The agent's manifest and digest |
| 2 | Intended use, users and population, and what it must not be used for | The manifest, in the publisher's words |
| 3 | Risk level and what the runtime enforces because of it | The declared risk level, and the limits the engine applies at that level |
| 4 | How a person stays in the loop | The four outcomes, the agent's thresholds and rules, the review queue, the automation holds in force |
| 5 | The publisher's evidence | The publisher's evaluation: counts with intervals, critical cases, the configuration it ran on |
| 6 | This site's evidence | Your decisions so far, the shadow pilot, reviewers' overrides, the validation brief's gates, evaluations run here |
| 7 | Known failure modes and limits | The manifest, and the limits of what the packet itself can show |
| 8 | Monitoring and what triggers an alert | The monitor's checks, what each needs, the last weeks, the alerts now |
| 9 | Change control | Signed versions, recalls, what runs here, and what happens when the agent or its model changes |
| 10 | Data handling and privacy | What the agent reads, what is kept and for how long, what leaves the deployment, who can read it |
| 11 | Who approved what and when | The approvals recorded here, and a blank block for the committee's own decision |
| A | Not stated | Every item a framework asks for that neither the publisher nor your site has stated |
| B | Crosswalk | Where each section relates to four published frameworks |

Three rules hold throughout:

- **No case text, no patient data.** The packet holds counts, ids, label codes, the names of
  configurations and dates. The only people it names are those who recorded an approval.
- **Every number says what it is out of.** A share of cases is written as *k of n* with its
  exact 95% interval, for example `12 of 40 (30.0%; 95% interval 16.6% to 46.5%)`. With no
  cases it says `no cases (0 of 0)`, not a percentage.
- **An empty section says why.** A deployment that has made no decision yet gets a packet that
  says so. A readiness gate with nothing to measure reads "Not enough data", never "Pass".

Each statement is marked by who makes it: the publisher (the manifest), your site (your audit
trail), or the software (what the engine enforces).

## How to produce it

**In the console.** Open the agent's Overview. Under *Deployment readiness & operations*, in the
*Governance & ROI* lens, choose **Committee packet**. It downloads one HTML page.

**From the API.** The route answers JSON, or the page with `format=html`:

```bash
curl -sS -H "Authorization: Bearer $MENDARI_KEY" \
  "https://mendari.example.org/api/agents/mendari/portal-message-agent/committee-packet?format=html" \
  -o committee-packet.html
```

`days` sets the window for the counts, from 14 to 180 (90 unless given). Every role that may read
an agent's evidence may read its packet: viewer, reviewer, service, governance and
administrator. The answer covers your organization only. The route counts against the same
hourly limit as an export.

**From the command line.** This reads the server's database and records nothing, so it works
with the server down:

```bash
mendari packet mendari/portal-message-agent --db "$DATABASE_URL" --org my-hospital --out committee-packet.html
```

Add `--json` for the JSON document. Without `--org` the counts cover every organization the
deployment holds. A packet made this way says it was made from the command line: it shows the
settings of the environment the command ran in, and marks what only a running server knows
(sign-in, sessions, the schedule) as not known.

## Printing it

The page is self-contained: no script, and nothing loaded from anywhere. It prints in black and
white, starts each section on a new page, and repeats table headings across pages.

Chrome and Edge print the agent's name, version, digest and the time the packet was generated at
the foot of every page, with the page number. Other browsers print their own header instead,
which carries the page's title: the same words. Turn on "Headers and footers" in the print
dialog if you do not see it.

The JSON document is the source: the page is made from it, and it carries `sha256`, the hash of
the document without that field. The hash tells two copies apart. It is not a signature.

## Reading the "Not stated" appendix

Appendix A lists what a framework asks for and nobody has stated, in two tables: what the
publisher has not stated, and what your site has not stated. Each line names the frameworks
that ask for it.

- **It is a list of questions, not of findings.** A line there says nothing against the agent.
  It says the committee would have to ask.
- **The software never fills one in.** If the publisher gave no funding source, the packet
  does not guess one.
- **Send the first table to the publisher.** It is the list of what to ask for: a contact,
  the funding source, the data the agent was developed on, how fairness was addressed, external
  validation, the update schedule.
- **Assign the second table at your site.** It is what only you can state: who owns the
  agent's use, how staff are trained, how patients are told, how safety events are reported,
  the committee's conditions and its next review date.

Some lines leave the appendix once the evidence exists: an evaluation by the publisher, a
decision or an evaluation on your own cases, a recorded approval. Most lines stay, because no
field or record holds them yet: the manifest has no place for a publisher's statements, and
the deployment has no site statement record. Both are planned. Until then, keep the answers you
collect with the committee's file.

## The crosswalk: what it is, and what it is not

Appendix B shows, for each section, which parts of four published frameworks it holds
information for, and what of each part it does *not* hold.

| Framework | What the packet maps to | Read on |
|---|---|---|
| [CHAI Applied Model Card](https://mc.chai.org/v0.1/template.pdf) | The card's sections and fields (draft template v0.1, schema v0.2) | 2026-11-23 |
| [ONC HTI-1 source attributes, 45 CFR 170.315(b)(11)](https://www.ecfr.gov/current/title-45/section-170.315) | The nine categories of source attributes for predictive decision support interventions, and intervention risk management | 2026-11-23 |
| [NIST AI Risk Management Framework (AI RMF 1.0)](https://doi.org/10.6028/NIST.AI.100-1) | The four functions' nineteen categories | 2026-11-23 |
| [Joint Commission and CHAI guidance on the Responsible Use of AI in Healthcare](https://digitalassets.jointcommission.org/api/public/content/dcfcf4f1a0cc45cdb526b3cb034c68c2) | Its seven elements | 2026-11-23 |

**What it is.** A way to find things. A committee that works from one of these frameworks can
see where the packet has something for each part, and where it has nothing.

**What it is not.** It does not say that the agent, its publisher, Mendari or your organization
meets, complies with or is certified against any of them. The frameworks' own texts govern.

Three things to know when you read it:

- **Every entry carries its source.** Each part is named as its source names it, with the
  source's title, its address and the day it was read. A part or a note that was not confirmed
  in a primary source is marked **To verify**, in the data and on the page.
- **HTI-1 is leaving the regulation.** A final rule published on 28 October 2026
  ([91 FR 67916](https://www.federalregister.gov/documents/2026/10/28/2026-21943/health-data-technology-and-interoperability-onc-deregulatory-actions-to-unleash-prosperity)),
  effective 28 December 2026, removes the source attributes and intervention risk management
  from the certification criterion. The criterion is addressed to developers of certified
  health IT. The packet keeps the list as a checklist of what such a description holds, says
  that the list is leaving the regulation, and takes no position on what it requires of anyone.
- **Most of the Joint Commission and CHAI guidance is about your organization.** It asks what
  the organization does, so many of its lines are in the second table of Appendix A.

The crosswalk is data, not prose in code: `src/mendari/core/crosswalk.yaml`. When a framework
changes, that file changes: the part, the day it was checked, and the rows that name it. A row
that names a section or a part that does not exist stops the packet from being built.
