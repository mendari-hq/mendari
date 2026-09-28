# Architecture

## Components

```text
            ┌──────────────────────────── openhealth serve ────────────────────────────┐
 EHR/app ──►│ REST /api/packs/{ns}/{name}/decide      FHIR /…/fhir/decide   /cds-services│
            │        │                                   │ FhirAdapter          │        │
            │        ▼                                   ▼                      ▼        │
            │   ┌─────────────────────────── Decision (runtime) ───────────────────┐    │
            │   │ input schema ─► provider slots ─► policy ─► DecisionResult        │    │
            │   │                 rules│naive_bayes│jev    rules, thresholds, caps  │    │
            │   └──────────────────────────────────────────┬───────────────────────┘    │
            │                                              ▼                             │
            │   AuditStore (SQLite): decisions · hash-chained events · review queue ·    │
            │                        eval reports                                         │
            │   Dashboard (static HTML/JS)  ◄── /api/stats /api/review /api/audit /api/evals│
            └────────────────────────────────────────────────────────────────────────────┘
```

- **Spec and pack loading** (`core/spec.py`, `core/pack.py`): pydantic models, cross-reference
  validation, file confinement, content digest.
- **Registry** (`core/registry.py`): resolves `namespace/name[@version]` from
  `OPENHEALTH_PACKS_PATH` and the bundled packs.
- **Runtime** (`core/decision.py`): binds a pack to concrete provider variants. It validates
  input, collects answers, applies policy and optionally records the result.
- **Derived questions** (`core/derive.py`): computes decision-table questions from their
  members' answers with exact probabilities (spec 0.2 §4.3), and adds expected values to score
  answers.
- **Policy** (`core/policy.py`): a pure function from answers and failures to an outcome. It
  holds all automation limits, including risk-level caps and governance overrides.
- **Providers** (`providers/`): a common `answer(inputs) -> ProviderOutput` contract.
  Within one decision, remote providers that share an endpoint and model are merged into a
  single call (`providers/batch.py`): the inbox router and the Jev half of its safety screen
  make one Jev call and use one budget unit.
- **Audit** (`core/audit.py`): write-once decision records. Reviews and outcomes are appended
  as events, the hash chain is verifiable, and the store also serves the review queue and
  live statistics.
- **Evals** (`evals/`): pure metrics, the harness, the local-vs-benchmark comparison, and the
  pack behaviour tests.
- **Standards** (`standards/`): FHIR R4 mapping and `Task` output, CDS Hooks discovery and
  cards.

## Request lifecycle

1. The input arrives as JSON, a FHIR Bundle, or a CDS Hooks request with prefetch. FHIR is
   mapped onto the pack input by `fhir/mapping.yaml`.
2. JSON Schema validation. Invalid input becomes `BLOCK`, and no provider sees it.
3. Each provider slot answers its questions. A provider error is recorded per question and
   never raised to the caller. Derived questions are then computed from their members'
   answers, and score answers get their expected value.
4. Policy: rules, then thresholds, then caps.
5. The `DecisionResult` is returned. With a store attached, the record is written and
   `decision.created` is chained. `HUMAN_REVIEW` and `ABSTAIN` enter the review queue.
6. A reviewer resolves the item (`review.resolved`, with the override flag), or a downstream
   system reports the final label (`outcome.recorded`). Both feed the override rate and the
   observed accuracy.

## Trust boundaries

- **Pack content is code-adjacent.** Regexes, models and policies change behaviour. Packs are
  identified by digest and SHOULD be reviewed like code. v0.1 loads packs only from local
  paths.
- **Remote providers** (Jev, LLMs) receive only `state_fields` over HTTPS (plain http only
  to a model server on localhost), with credentials read from the environment or the
  deployment's OIDC token. LLM prompts carry the input as JSON data under a system prompt that
  forbids following it, and the response schema is fixed by the pack. Their responses are validated strictly and bounded in size. A batched
  call sends the union of its variants' `state_fields`; a variant that must not share its
  state sets `batch: false`.
- **The server** has no authentication in v0.1 and binds to localhost by default. The docker
  sandbox publishes ports on 127.0.0.1 only.
- **CDS Hooks** does not call back into caller-supplied FHIR servers, which avoids SSRF. Data
  must come through prefetch.

## Sandbox

`docker compose up` runs HAPI FHIR (R4, in-memory), the engine image, and a worker that seeds
synthetic Patients and Communications tagged `https://openhealth.dev/fhir/sandbox|synthetic`.
The worker then routes each in-progress Communication through the engine, writes a `Task`, and
marks the Communication completed. Only tagged resources are ever touched.
