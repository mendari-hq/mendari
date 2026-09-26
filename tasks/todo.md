# Task: OpenHealth v0.1 — Decision Pack runtime + Patient Inbox Router

## Objective
A runnable, fully synthetic reference implementation of OpenHealth Hub v0.1: a Decision Pack
spec, a model-agnostic runtime, and one high-quality reference pack (Patient Inbox Router) that
routes synthetic patient messages while measuring accuracy, calibration, abstention, overrides,
latency and cost.

## Plan
- [ ] Step 1: Repo scaffold (uv, pyproject, ruff, pytest, CI) → `uv run pytest` green on empty suite
- [ ] Step 2: Decision Pack spec (pydantic models + validation + risk-level rules) → `openhealth validate` works
- [ ] Step 3: Providers: rules, naive_bayes (local ML), jev (HTTP adapter) → common `Answer` contract
- [ ] Step 4: Policy engine: AUTO / HUMAN_REVIEW / ABSTAIN / BLOCK with risk caps and governance overrides
- [ ] Step 5: Audit trail (SQLite, hash-chained events) + human-review queue
- [ ] Step 6: Synthetic dataset generator (public benchmark + shifted "local site" dataset)
- [ ] Step 7: Patient Inbox Router pack (schemas, FHIR mapping, rules, trained model, policy, tests, card)
- [ ] Step 8: Evaluation harness (accuracy, P/R/F1, ECE/Brier, abstention, false automation,
      overrides, latency, cost, subgroups, baseline comparison / degradation detection)
- [ ] Step 9: FHIR R4 adapter (Communication/Patient → input, decision → Task) + CDS Hooks service
- [ ] Step 10: FastAPI server + developer dashboard (packs, review queue, audit, evals)
- [ ] Step 11: CLI (`list validate test decide eval train card serve audit sandbox demo`)
- [ ] Step 12: docker compose sandbox (HAPI FHIR + engine + workflow worker)
- [ ] Step 13: Docs (README, spec, architecture, ADRs) and private GitHub repo

## Blockers
- Jev requires a `JEV_API_KEY`; everything else must work without credentials.

## Notes
- Jev contract mirrored from ExpertsRising `packages/model_gateway/src/judgments.ts`
  (POST `https://api.typesafe.ai/v1/systemone`, typed `choice` questions, ≤8 questions,
  2–255 labels, probabilities must sum to 1, chosen label must be the argmax).
- Core abstraction: a pack declares *questions* (labels + criteria). Providers answer questions
  with probability distributions. Policy turns answers into an outcome. Criteria text doubles
  as human documentation and as instructions for Jev/LLM providers.
- LLM provider, CQL, the other four reference packs and remote hub `pull` are v0.2+.
