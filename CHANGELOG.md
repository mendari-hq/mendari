# Changelog

Notable changes to Mendari, newest first. Each release has a section named by its version
(`## 0.1.0`), written before the release: the release workflow refuses a tag whose version has
no section, or whose section is not the newest. What is merged and not yet released is under
`## Unreleased`, which the workflow does not read: it takes its version's name at the release.
Changes to an agent are in the agent's own `CHANGELOG.md`.

## Unreleased

No free text is released unread
([ADR-0027](docs/decisions/ADR-0027-no-free-text-is-released-unread.md)). Until now what an
agent wrote came back with every decision, an action whose argument is free text was released
with an automated one, and confirming a label released every held action. This changes what a
decision returns and what a review takes, so it ships under a new version, never as 0.1.2. The
spec version stays `"0.4"`: no manifest key is added, and a manifest that was valid is valid.

- **A decision's answer.** What an agent wrote (`outputs`) comes back only with a decision
  that waits for a person (`HUMAN_REVIEW`, `ABSTAIN`), as that person's draft. An automated or
  a blocked decision no longer returns it, and keeps none of it (`POST …/decide`,
  `mendari decide`). A decision whose agent wrote a text says where each stands, in its answer
  and its record: `written`, `held` or `not_released`, never the text. The outcome does not
  change: a text stops no label, and no action of enumerated arguments, from automating. **If
  your caller sent the text of an automated decision, it has nothing to send for those now.**
  Send a text through an action a person releases, as the demo agent's `send_acknowledgement`
  is. A judge that clears a text when the decision is made is not built.
- **Actions.** An action whose arguments can hold free text (a string that is not an `enum` or
  a `const`, an array with no `items`, or no schema) is never released without a person where
  its gate would be `auto`: a `write` or a `contact`, and a `read` or a `draft` of an agent
  nobody vouched for. Proposing one sends an automated decision to a person, with the reason
  `action '<name>' can carry free text and is never released without a person`. **For an
  author:** an action meant to go without a person takes enumerated arguments only, and an
  identifier that is not enumerated counts as free text. The bundled demo agent's two actions
  take enumerated arguments, so both are gated as before.
- **Reviews: a breaking change for a caller that only confirmed labels.**
  `POST /api/review/{id}` takes two more fields. `texts` gives each text the decision holds a
  verdict, `release` or `withhold`: a review of a decision that holds a text, without a verdict
  on each, is refused (`409`). `release_actions` names the held actions to release: one whose
  arguments can hold free text, and every held action once a text is withheld, is released
  only when named. One left out is denied (`not_named`), and the review still succeeds: read
  `actions` in its answer. A decision with no text and no free-text action is reviewed as
  before. The review queue and a decision's record list the held texts under `outputs`, and
  mark an action whose arguments can hold free text (`free_text`).
- **What is kept.** A text held for review is kept for its reviewer as the decision's input
  is, apart from the audit chain: sealed under PHI mode `encrypt` and read by whoever may read
  inputs, each read chained; in the clear under `off`; not at all under `hash`, or for an
  agent whose `audit.store_input` is `hash`. Retention removes it, and erasing a decision's
  input erases it. No table is added: a held text is a note.
- **The audit trail.** Two new event kinds on a decision, `output.held` and `output.resolved`:
  the output's name, a commitment to the text, then the reviewer's verdict; never the text. The
  FHIR export carries both, as codes. An `action.proposed` event says when its arguments can
  hold free text (`free_text`).
- **The words a person reads.** For an `order`, or an action with `allow_auto: false`, whose
  arguments can hold free text, the Decision Card, the console and the committee packet said
  it is released "when a reviewer confirms the decision". They now say what is so: when a
  reviewer confirms the decision and names the action. The Card and the packet no longer say
  that a text is returned with the decision and not kept.

## 0.1.2

Fixes from a bug and dead-code audit of the whole source tree (the details, and what was
removed, are in `notes/bug-audit-2026-11-27.md`). None raised automation or released an action.

- **The audit trail.** `mendari audit verify` and the System checks read the trail as one
  snapshot: a grade, a store version or an erased note written while they run is no longer
  reported as a broken chain (with its `audit.broken` alert). A written round's grade moved to
  another grader in the database breaks verification, and a round whose stored grades the chain
  does not hold is not closed. A stored checkpoint statement this deployment's key did not sign
  is named as such, and never called signed.
- **The registry.** A key or publish token revoked while a publish is checked publishes
  nothing, and a token revoked while it adds a key, recalls or publishes an evaluation writes
  nothing. An agent's versions are ordered by semantic-version precedence, in the registry and
  in the engine: a release is newer than its pre-releases, and `rc.10` newer than `rc.2`.
- **Refusals where there was a crash (a 500 or a traceback):** a FHIR base that does not even
  split as a URL (`http://[`) on the SMART launch, a CDS hook and the EHR check; an input schema
  that is not an object; a number no float holds, from an agent service, a model, a mock file,
  a ledger or a benchmark; a `Retry-After` date no clock holds; an impossible date on a
  publisher's evaluation.
- **Decisions.** NaN or an infinity in an input is an input problem (a recorded BLOCK, its
  input kept with null in each such place, so the record stays JSON); each
  schema reason is listed once; a question named `recommendation` or `priority` is refused at
  load; on an agent from a registry, a `$` with more of the pattern after it is refused (write
  `\Z` or `\n?\Z`); a phone number right after another is found by the PHI check.
- **Deadlines and Bulk Data.** A call to an agent service or a model ends at its deadline as a
  whole, whatever the server waits before each part. A 429 while an export is polled is waited
  out.
- **`mendari mcp`** holds an agent from a registry until its registry's recalls are read, as a
  running server does.
- **The command line.** `mendari audit export --since/--until` read times, the basic form a
  backup's name carries included, and refuse anything else. `--limit` is 1 case or more, or
  `all`: `calibrate --limit 0` is now `--limit all`. Options a run would have ignored are
  refused (on `eval`, `leaderboard` and `sandbox smart-launch`). The agent kit refuses an id
  ending in a newline, as the engine does.
- **Packaging.** `cryptography`, which Mendari imports directly, is declared at 50.0.0 or later:
  the first release with no known advisory (it came only through `pyjwt[crypto]`, from 3.4).
- **The console and the Hub.** The audit trail says it lists the newest decisions, of how many,
  and pages to older ones; a whole decision id searched for opens it. Another agent's record is
  shown in its own codes and not offered for replay. A failed read of the holds is said, never
  taken for none. The palette opens a decision by its whole id only. On the Hub, a risk filter
  that is not a number is ignored, not shown as an outage.

## 0.1.1

A security fix to 0.1.0. Update if you serve an agent store (`MENDARI_AGENT_STORE=on`).

- **An author who may not read inputs runs no version over them.** In 0.1.0 a member whose
  only role was `agent_author` could run a draft over the organization's improvement dataset
  (its reviewed decisions' inputs, LP-20i) and read what the draft decided of each case; a
  draft whose rules spell an input out could so read the inputs back, with no read recorded.
  Every route that runs a version over that dataset (the proposed tests, deciding one, the
  accepted ones in a draft's tests, jobs and comparisons) now needs the permission to read
  inputs, and each case decided is chained as the runner's `input.read` of its decision.
- **A path with an encoded `?` or `#` is refused** (`404`) before any check, so the sign-in
  guard, the origin check and the limits decide on the path that is routed.
- The package's Documentation link, the README and what `mendari new` prints and writes name
  the documentation site, https://mendari-hq.github.io/mendari/.

## 0.1.0

The first release, a research preview: everything in it runs on synthetic
data. Mendari is not a medical device, and must not hold real PHI or decide about real patients
until its external security review has happened. What the release contains:

- **An agent, declared in a manifest.** An agent is a folder whose manifest, `agent.yaml`, says
  what it is for and how it is governed: the questions it answers, each with a fixed set of
  answers and criteria written in words; the input it takes (a JSON Schema, and a FHIR mapping
  where it reads FHIR); its risk level, 0 to 4; its policy; and the slots that answer the
  questions. A slot's variants are of four types: `rules` (weighted patterns, with typo
  tolerance), `llm` (a chat model through any OpenAI-compatible API, the Vercel AI Gateway by
  default), `agent` (a service of one's own, over the agent HTTP contract) and `check` (one
  variant answers and a second checks it). A manifest says `spec_version: "0.4"`, the one
  version this release reads ([the agent manifest](docs/spec/agent-manifest.md)). An agent's
  files are hashed into its digest, which every decision records, and it reads no file outside
  them.
- **Four outcomes, and a policy that only lowers automation**
  ([ADR-0002](docs/decisions/ADR-0002-runtime-enforced-risk-caps.md)). Every decision is
  `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`: named rules first, then confidence thresholds
  on the primary question, then caps, which may turn `AUTO` into `HUMAN_REVIEW` and never the
  reverse: a per-label limit, a provider that failed or ran out of time, the risk level (levels
  2 and 3 automate only under a recorded governance approval of one digest, level 4 never), a
  value read through a site's own code, a list the site's EHR may not hold whole, canned
  answers on a server with sign-in, a calibration made for another version of the service, a
  second signal that disagreed, and an action that needs a person (`core/policy.py`). Each cap
  says why in the decision's reasons. A decision has one wall-clock deadline
  (`MENDARI_DECISION_DEADLINE_S`, 120 s unless set).
- **What an agent does is gated, not only what it answers**
  ([ADR-0023](docs/decisions/ADR-0023-an-agent-proposes-actions.md)). A manifest declares the
  actions an agent may ask for (`actions:`), each with an effect: `read`, `draft`, `write`,
  `contact` or `order`. The agent proposes actions with its answers, and each comes back
  `released`, `held` or `blocked`: a `read` or a `draft` with any decision that is not blocked,
  a `write` or a `contact` only with an automated decision or once the decision's reviewer
  confirms it, an `order` never without a person. The gate is the runtime's, and a manifest can
  only tighten it (`allow_auto: false`). An action the agent never declared, or arguments that
  do not match its schema, is blocked and sends the decision to a person. Mendari authorizes
  and records (`action.proposed`, `action.resolved`, with a hash of the arguments); it runs
  nothing ([Governing actions](docs/guides/governing-actions.md)).
- **A confidence that was checked, and a second signal**
  ([ADR-0024](docs/decisions/ADR-0024-a-checked-confidence-and-a-second-signal.md)).
  `mendari calibrate` fits an `llm` or `agent` variant's confidence on labelled cases, one
  temperature a question; an external agent's fit (`mendari.agent-calibration/1`) is for the
  version its service reported, and when the service reports another version, or none, nothing
  is rescaled and no decision is automated until it is calibrated again. `repeat: N` asks each
  case N times, and a `check` variant has a second model check the first: where the answers
  disagree on a question the decision turns on, a person decides, and agreement raises nothing
  ([Calibration and a second signal](docs/guides/calibration-and-a-second-signal.md)).
- **Written output, graded by clinicians and by a judge whose agreement is measured**
  ([ADR-0025](docs/decisions/ADR-0025-a-judge-is-trusted-by-measured-agreement.md)). A manifest
  declares what an agent writes (`outputs:`) and how it is graded (`rubrics:`: yes/no and
  scored criteria, each with instructions and an anchor for every level). Clinicians grade a
  sample blind, a model judge grades every text in the same words, and the report gives, per
  criterion, the judge's weighted kappa with clinicians beside clinician with clinician, and
  one of three states: validated, not validated, insufficient evidence. The judge's grades of
  the texts no clinician graded are reported only where the criterion is validated, and
  withheld below the floors. `mendari written eval` runs and reports it; `mendari written round
  create | assign | progress | close | list` runs a grading round in a server's database,
  graded in the console by the `labeller` role, whose texts are sealed and erased when the
  round closes ([Evaluating written output](docs/guides/written-output.md)).
- **A trail of every decision, and a person for the rest.** Every decision is an event in an
  append-only, hash-chained audit trail on SQLite or Postgres: the agent's id, version and
  digest, each answer with its provider and evidence, the outcome and its reasons.
  `mendari audit verify` checks the chain, `audit checkpoint` records its head for the next
  check to start from, and `audit export` writes it as FHIR `AuditEvent` and `Provenance`. A
  `HUMAN_REVIEW` decision waits in the review queue, by priority, and what the reviewer decides
  is chained; review notes are kept apart from the chain, which holds a salted commitment to
  each. Automation can be held for one agent or for all, also with the server down
  (`mendari automation hold`). Each store's schema is marked as this release's
  (`mendari-engine/1`, `mendari-registry/1`), and a database without the mark, or with a newer
  one, is refused before anything is read.
- **Patient data kept as the deployment says.** PHI mode `off`, `hash` (no input kept) or
  `encrypt` (inputs sealed apart from the chain, every read chained), with key rotation,
  retention and erasure (`mendari phi rotate | purge | erase`); the input hash is an HMAC under
  `MENDARI_INPUT_HASH_KEY`. Reports, ledgers, exports and shared results carry ids and counts,
  never case text.
- **Evaluation with exact statistics.** `mendari eval` runs an agent on a labelled dataset and
  keeps a per-case ledger, with no input text, from which its report is computed
  (`mendari.eval/1`): each proportion k of n with its exact Clopper-Pearson interval, the Brier
  score with a bootstrap interval, the calibration error, the outcomes, false automation and
  each critical label's detection ("insufficient evidence" under 100 positives).
  `mendari compare` compares two runs case by case (exact McNemar, Holm-adjusted),
  `mendari leaderboard` an agent's configurations on the same cases, in one order (critical
  cases first, never accuracy alone), and `mendari data check | coverage` reads a dataset.
  Every evaluation reports its **operating point**: what the automation threshold lets
  through, as critical misses and false automation, each with an exact one-sided bound; given a
  target (`--target-critical-miss`, `--target-false-automation`), it recommends the lowest
  threshold that keeps it with the stated confidence, or says why none can be certified
  ([Choosing an automation threshold](docs/guides/choosing-a-threshold.md)). Limits
  (`--min-accuracy`, `--max-critical-misses`, `--no-worse-than RESULT` and others) fail a run,
  and `--markdown` writes it for a CI job's summary.
- **`mendari probe`: the failures nobody wrote a case for.** It rewrites an agent's own cases in
  ways that keep their meaning (typing errors, letter case, punctuation, white space, a
  greeting or a signature, an instruction slipped into the message), decides each variant, and
  reports the answers that changed, worst first. Offline by default and the same on every
  machine for a seed; with a model it also writes paraphrases, translations and denials.
  `--write` keeps each failure as a behaviour test, and `--fail-on critical` fails a CI job
  ([Finding failures](docs/guides/finding-failures.md)).
- **Building an agent.** `mendari new <dir> --id <namespace>/<name>` writes a working copy of the
  demo agent under that id, with a keyword-rules variant to compare it with, labelled examples,
  behaviour tests and a workflow for its own repository's CI. `mendari validate` checks the
  manifest and every file it names, `mendari test` runs its behaviour tests, `mendari card`
  renders its Decision Card (intended use, risk, policy, limits) and `mendari schema` prints
  its input schema. A service answers the agent HTTP contract: one `POST` per decision, with the
  questions and the input fields the manifest allows, every answer checked, and a wrong,
  missing or late answer a provider failure that never automates. The agent kit
  (`mendari.agent_kit`) answers it, and three starters are built on the kit:
  `examples/http-agent`, `examples/langgraph-agent` and `examples/claude-agent`
  ([the authoring guide](docs/guides/authoring-an-agent.md)).
- **Checked on every change.** Mendari's GitHub Action (`action.yml`) runs `mendari validate`,
  `mendari test` and an evaluation, takes the limits and the targets as inputs, writes the
  figures and the operating point to the job's summary and fails the job when a check fails; it
  needs no secret. `mendari new` writes the workflow that uses it
  ([Evaluating an agent on every change](docs/guides/evaluate-in-ci.md)).
- **The demo agent, offline.** The release bundles one agent, `mendari/portal-message-agent`
  0.3.0. It reads a patient portal message, says what the patient asks for and whether the
  message describes symptoms that need attention today, declares two actions and writes an
  acknowledgement that a rubric grades. Its canned answers need no key or network, and its 600
  synthetic test messages show what they miss, on purpose: 33 of the 114 urgent ones, 9 of
  which would have been automated. `mendari demo` runs it end to end: decisions, actions, the
  review queue and the audit trail, an evaluation, a probe and the written output, then its
  verdict and operating point. A synthetic example, not clinically reviewed and not for patient
  care.
- **Serving decisions.** `mendari serve` runs the API, the FHIR and CDS Hooks services and the
  Governance Console, and documents the API at `/docs`; `mendari decide` decides once from the
  command line and `Decision.from_hub` from Python; `mendari mcp` serves governed decisions to
  other AI agents as Model Context Protocol tools on stdio (`list_agents`, `decide`,
  `authorize_action`, `review_queue`, `verify_audit`). A decision asked with `?shadow=true` (or
  `X-Mendari-Shadow: 1`) goes to a person while the engine records what the agent would have
  automated, and `GET /api/agents/{namespace}/{name}/validation-brief` sums it up.
- **The Governance Console**, served by the engine with no build step: Overview; Adopt (Agents,
  Agent store, Decision Card); Evaluate (Try it, Evaluations, Feedback, Written output); Review
  and monitor (Review queue, Monitoring, Audit trail); Administer (System, Access, Registry
  review); and the patient view an EHR launch opens. Its styles use design tokens only, every
  colour pair passes WCAG AA in both themes, and CI checks every view with axe-core.
- **Publishing an agent: signed archives and a registry that shows only what it checked**
  ([ADR-0012](docs/decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)). An agent
  travels as a deterministic archive (`mendari-agent/1`) signed with its publisher's Ed25519
  key (`mendari keys`, `mendari archive build | verify`). A registry (`mendari registry serve`,
  or under the engine with `MENDARI_REGISTRY=on`) keeps immutable versions under the namespaces
  organizations claim, lists a namespace's first once a reviewer approves it, refuses a version
  `mendari validate` would refuse, renders each version's Decision Card itself, runs its own
  regression check on the publisher's test set, counter-signs what it accepts and chains every
  write (`mendari registry verify`). A publisher recalls a version with a signed record
  (`mendari yank`). `mendari login`, `search`, `push` and `pull`: a pull checks the archive's
  digest, every file's hash, the signature and, where the registry signs, its counter-signature,
  and pins the namespace's key on first use. The Agent Hub (`hub/`, Next.js) is a registry's
  website.
- **Publishing an agent's evaluations**
  ([ADR-0020](docs/decisions/ADR-0020-publisher-evaluations.md)).
  `mendari push AGENT --key K --evaluate DATASET` publishes a version, runs every configuration
  of the agent on a dataset of 100 cases or more that its manifest declares, and publishes each
  result signed with the same key (`mendari.publisher-evaluation/1`): counts only, and the
  registry runs a local configuration again and refuses counts that differ. The registry
  answers an agent's evaluation (`mendari.agent-evaluation/1`) and a badge for a README, and the
  Agent Hub shows it as a page, `/agents/<namespace>/<name>/evaluation`, that names the
  configuration that did best on that dataset only when one clearly did. `mendari eval
  --publish` publishes one configuration
  ([Publishing an agent's evaluations](docs/guides/publishing-evaluations.md)).
- **Adopting an agent at a site.** `deploy/site/mendari-site install` puts Mendari on one
  machine with Docker: it asks for what only the site knows, generates every secret, and starts
  the engine behind https with its database, daily jobs and a nightly backup; `status`,
  `upgrade`, `rollback`, `backup`, `restore`, `verify`, `hold` and `lift` are the same script
  ([Install Mendari at your site](docs/guides/install-at-your-site.md)). The first
  administrator signs in through the site's identity provider (`MENDARI_BOOTSTRAP_ADMIN`), and
  the administrators of the operator organization (`MENDARI_OPERATOR_ORG`) run the deployment
  ([ADR-0022](docs/decisions/ADR-0022-the-first-administrator-and-the-operator-organization.md)).
  The console's **System** view lists eighteen readiness checks, each with what is so and what
  to do, and its **Agents** view finds an agent in the registry, adds a verified version to the
  organization's agent store and connects the service that answers for it
  ([ADR-0021](docs/decisions/ADR-0021-an-agents-connection-set-in-the-console.md),
  [Adopting an agent from the console](docs/guides/adopting-an-agent.md)). A pulled agent never
  automates until its organization approves its exact digest, nor while its registry recalls
  it.
- **An organization's own versions: the agent store**
  ([ADR-0016](docs/decisions/ADR-0016-an-agent-store-per-organization.md)). With
  `MENDARI_AGENT_STORE=on`, in memberships mode, an organization keeps its own versions of
  agents on the server. Its authors (`agent_author`) upload signed archives or copy an agent
  the deployment serves, edit drafts and run their tests; each new version is checked (the
  archive, a PHI scan of every text file, the load on the bounded engine, the validation and
  the dataset checks), can be evaluated, and is served to that organization alone once someone
  other than its author signs off. `mendari store versions | verify | withdraw | purge` is the
  operator's.
- **The committee packet.** One document per agent for a hospital's AI governance committee:
  what the agent is and what the runtime enforces, the publisher's evidence and the site's own,
  monitoring, change control, data handling and approvals, then what nobody has stated and a
  crosswalk to four published frameworks (the CHAI Applied Model Card, HTI-1's source
  attributes, the NIST AI Risk Management Framework, and the Joint Commission and CHAI
  guidance). A reading aid, not a certification: from the console, or `mendari packet <agent>`,
  from the agent's files alone or with `--db` as a deployment's organization sees it
  ([The committee packet](docs/guides/committee-packet.md)).
- **Sharing results back**
  ([ADR-0017](docs/decisions/ADR-0017-a-site-network-of-signed-counts.md)). A site registered
  with a registry (`mendari site init | key | ticket | submit`) shares an evaluation on its own
  cases as signed counts, never a case (`mendari eval --share`, `mendari.site-report/1`, which
  the registry checks before it stores anything), and the Agent Hub shows them pooled beside
  other sites'.
- **People, organizations and machines.** Sign-in through an organization's OpenID Connect
  provider, with every route denied to a caller without a role. In memberships mode
  (`MENDARI_ACCOUNTS=memberships`), organizations, invitations and memberships with roles
  (`viewer`, `reviewer`, `service`, `governance`, `admin`, and the `agent_author` and
  `labeller` an administrator grants), and each organization reads only its own records
  ([ADR-0008](docs/decisions/ADR-0008-accounts-and-memberships.md)). Machines use API keys,
  which expire and can be scoped, or SMART Backend Services tokens
  ([ADR-0004](docs/decisions/ADR-0004-machine-access-tokens-and-scopes.md)). Rate limits and
  daily quotas per caller and organization, a security log of JSON lines, and strict headers
  and a content security policy on every response.
- **An EHR's standards.** FHIR R4: `POST /fhir/$decide`, decisions as `Task`, the trail as
  `AuditEvent` and `Provenance`, a `CapabilityStatement` at `/fhir/metadata`, and the FHIR it is
  given checked against US Core 6.1.0. An agent's FHIR mapping reads an EHR's records into its
  input: paths, `[system=…]` predicates, references it follows, a note's text, code sets, and
  a site's own codes through its code map (`MENDARI_SITE_CODES`); what a site's EHR does not
  offer is declared (`MENDARI_EHR_DECLARATIONS`), and a decision on a list it may not hold whole
  goes to a person. CDS Hooks 2.0: the services, their cards and card feedback, order hooks
  (`order-sign`), and an EHR that calls with a JWT it signs itself
  ([ADR-0014](docs/decisions/ADR-0014-a-cds-client-is-a-principal.md)). SMART App Launch 2: an
  EHR launches a clinician into the console's patient view, as a principal of its own
  ([ADR-0006](docs/decisions/ADR-0006-an-ehr-launch-is-a-principal.md)), and
  `mendari smart check` says before go-live what a launch would meet. SMART Backend Services,
  for machines. Bulk Data 2.0: `mendari bulk export` and `bulk dataset` turn a FHIR server's
  export into a local dataset. CI runs the HL7 validator over what Mendari renders, with the
  FHIR packages pinned by checksum, and Inferno's SMART App Launch and Bulk Data client suites
  ([Connecting an EHR](docs/guides/connecting-an-ehr.md)).
- **Agents the deployment does not own run bounded**
  ([ADR-0009](docs/decisions/ADR-0009-bounded-evaluation-for-agents-the-deployment-does-not-own.md)).
  An agent pulled from a registry, or a version in an organization's agent store, runs its
  patterns on RE2 within caps, input limits and a CPU budget per decision, on the deployment's
  model and provider limits, and names the input fields it sends to a model. Every agent's
  provider configs are checked at load against their closed schema
  (`docs/spec/provider-configs.schema.json`), its input schema is bounded in shape, and remote
  providers reach only the origins a deployment allows (`MENDARI_PROVIDER_ALLOWED_ORIGINS`).
- **Operations.** `mendari monitor` checks deployed agents for drift and degradation; its
  alerts, a quota reached and a broken audit chain go to the deployment's webhook
  (`MENDARI_ALERT_WEBHOOK`, which `mendari alerts test` checks), signed and with no case text;
  `mendari usage` gives the deployment's totals. Beside the one-machine install, `deploy/` holds
  templates for on-premises Docker Compose, Kubernetes and Vercel, and the checks a status page
  runs ([the incident runbook](docs/operations/incident-runbook.md)).
- **Security** ([SECURITY.md](SECURITY.md),
  [the threat model](docs/security/threat-model.md)). CI runs `pip-audit`, `npm audit` and
  OWASP ZAP scans of the API (with sign-in on) and of the Agent Hub, scans for secrets on every
  pull request and, weekly, every commit of every ref, and fails on any pin loosened: every
  Action by commit, every image by digest, every download by checksum. Every commit is signed
  off (the Developer Certificate of Origin).
- **Distribution.** A wheel and an sdist, and one container image, `mendari-engine`, with the
  bundled agent, for linux/amd64 and linux/arm64. The image carries the licences and notices
  of everything in it (`/licenses`); a release attaches its SBOM, publishes its corresponding
  source beside it, and pushes no image that fails the licence check. The command line shows
  the commands to start with (`mendari --help`) and every one (`mendari help all`), and an
  error is one line, never a traceback (`--debug` shows one). The documentation is a MkDocs
  site built from `docs/`.
