<!-- A community pack: community-agents/<name>/, id community/<name>. The guide:
     docs/guides/contributing-an-agent.md. Change only your pack's folder and its entry in
     community-agents/MAINTAINERS.yaml, and sign off every commit (git commit -s).
     Never paste real patient data here or anywhere in the pack. -->

## The pack

- Id and version: `community/<name>` <!-- e.g. community/refill-router 0.1.0 -->
- New pack or update: <!-- new / update -->
- Proposal issue: <!-- #123, or "none" -->
- What it decides, for whom: <!-- one or two sentences -->

## Risk level

- Level (0 or 1): <!-- 0 / 1 -->
- Why this level, and not a higher one: <!-- the table in the guide, "Risk level" -->

## Clinical review

<!-- A clinician who is not the author: name, role, and what they reviewed (criteria, patterns,
     critical labels, card). "None yet" is allowed: the pack is then merged as
     needs-clinical-review, and it is labelled "not clinically reviewed" in the product either
     way. -->
- Reviewer: 
- What they reviewed: 

## Data

- How the synthetic data was made: <!-- by hand / templates (data/cases.yaml) / other -->
- [ ] Every case is invented: no real person's name, date of birth, address, record number,
      phone number or message, not even de-identified.
- [ ] The PHI warnings of `community-check`, if any, are answered below.

## Content provenance

- Sources of the criteria and of `card.evidence`: 
- [ ] Nothing is copied from proprietary criteria (InterQual, MCG, payer policies) or
      copyrighted instruments, and code systems with licence terms (CPT, SNOMED CT) are used
      only as their licences allow.

## Providers

- Default providers (local only): <!-- rules / criteria / dosing / naive_bayes / a composite -->
- Remote variants declared (used only if a deployment opts this pack in): <!-- none / llm / jev -->

## Checks

- [ ] `uv run mendari community-check community-agents/<name> --base origin/main` passes.
- [ ] `CHANGELOG.md` has an entry for this version (an update raises the version).
- [ ] My entry in `community-agents/MAINTAINERS.yaml` lists who answers for the pack.
- [ ] Every commit is signed off (Developer Certificate of Origin).
