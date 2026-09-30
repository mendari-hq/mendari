# Threat model

_OpenHealth Hub engine, dashboard and registry, as of v1.0 (2026-09-30). Reviewed before the
public launch; update it with any change to a trust boundary. The checks named here run in CI._

## What we protect

| Asset | Why it matters |
|---|---|
| Decision inputs (messages, records; PHI in a real deployment) | Patient privacy. |
| Decisions, reviews, outcomes and the audit hash chain | Clinical accountability: what was decided, by what, and who changed it. |
| Governance approvals | They lift the automation cap at risk levels 2–3. |
| Sessions, API keys, OIDC and SMART tokens | Whoever holds them acts as a clinician, an integration or an admin. |
| Provider credentials (Jev, LLM gateway keys) | Cost, and a channel to send data out. |
| Pack integrity (signed archives, publisher keys) | A tampered pack changes clinical logic at every site that pulls it. |
| PHI encryption key | It opens every stored input. |

## Trust boundaries

1. **Browser ↔ engine:** the dashboard and the API.
2. **EHR ↔ engine:** CDS Hooks, SMART launch, DTR and FHIR decide.
3. **Engine → outside servers:** FHIR servers, the CQL engine, the identity provider, the payer
   and alert webhooks.
4. **Engine → model providers:** Jev, and LLMs through a gateway.
5. **Registry ↔ clients:** pushing and pulling packs.
6. **Engine ↔ database:** SQLite or Postgres.

## Threats and mitigations (STRIDE)

| # | Threat | Boundary | Mitigation | Checked by |
|---|---|---|---|---|
| S1 | Someone uses the API or dashboard without an account | 1, 2 | With sign-in configured, every route has a permission and unlisted routes need admin (deny by default). The Vercel deployment sits behind Vercel Authentication. | `test_every_route_has_the_permission_it_should`; ZAP scan with sign-in on |
| S2 | A forged `id_token`, replayed sign-in or open redirect | 1 | PKCE S256, state and nonce used once, `id_token` verified with the issuer's JWKS, `next` limited to this site. | `tests/test_auth.py`; `pilot-check` against a real OIDC provider |
| S3 | A stolen API key | 2 | Keys are stored as SHA-256, shown once, scoped by role and organization, and revocable. | `test_api_keys_work_for_machines_are_hashed_and_revocable` |
| S4 | A forged pack or publisher | 5 | Ed25519-signed deterministic archives, keys pinned on first use, and immutable versions. | `tests/test_archive.py`, `tests/test_hub_client.py` |
| T1 | The audit trail is edited after the fact | 6 | Every event is hash-chained, and `openhealth audit verify` detects any change. PHI inputs live outside the chain, so retention can delete them without breaking it. | `tests/test_audit.py`, `tests/test_phi.py` |
| T2 | A pack's logic is swapped at a site | 5, 6 | The pack digest is recorded on every decision and deployment. A governance approval is bound to the digest. | `tests/test_governance.py` |
| R1 | Someone denies a review or an approval they made | 1 | With sign-in on, the reviewer, reporter and approver are the signed-in principal, whatever the request body says. Each is chained. | `test_reviews_and_ratings_are_recorded_for_whoever_is_signed_in` |
| I1 | PHI leaks through storage | 6 | PHI mode `hash` stores no input. `encrypt` uses AES-256-GCM bound to the decision id, apart from the chain, with a retention purge. | `tests/test_phi.py` |
| I2 | PHI leaks to someone who shouldn't read it | 1 | Encrypted inputs open only for signed-in reviewers and admins, and every read is chained. Lists never carry inputs. | `test_signed_in_reviewers_read_inputs_and_each_read_is_chained` |
| I3 | PHI leaks into reports, errors or exports | all | Reports carry ids and metrics, never case text. Provider errors are stable codes without remote bodies. Shared and population reports suppress cells under 10. The FHIR audit export carries codes and hashes. | `tests/test_share.py`, `tests/test_omop.py`, `tests/test_audit_fhir.py` |
| I4 | One organization sees another's data | 1 | Tenancy lives in a context variable. The store filters every read and stamps every write. | `test_organizations_never_see_each_others_work` |
| I5 | Data goes to a model provider unexpectedly | 4 | Remote providers are off until configured. A pack declares the `state_fields` a remote provider may receive, and nothing else is sent. | `tests/test_jev_batching.py`, `tests/test_llm.py` |
| I6 | The browser leaks a session or page | 1 | HttpOnly, Secure, SameSite=Lax cookies. Headers: a CSP with the dashboard's inline scripts by hash only, `frame-ancestors 'none'`, nosniff, no referrer, COOP/COEP/CORP. | `tests/test_security_headers.py`; ZAP |
| D1 | Oversized or slow upstream answers hold the server | 3 | One outbound client: allow-listed origins, no redirects, timeouts and size caps. | `tests/test_fhir_client.py` |
| D2 | Provider costs run away | 4 | Daily call budgets per provider type, visible in `/api/health`. | `test_daily_budget_is_enforced` (`tests/test_audit.py`) |
| E1 | SSRF: making the engine call internal addresses | 3 | Every outbound call (FHIR, CQL, OIDC, PAS, webhook) goes through the allow-list. Webhooks must use https. | `tests/test_fhir_client.py`, `tests/test_monitoring.py` |
| E2 | A role does more than it may | 1 | A role table grants permissions, a matrix test covers every route, and the dashboard hides what the server would refuse. | `tests/test_auth.py`, browser tests |
| E3 | Automation beyond what the risk allows | runtime | Policy only lowers automation. Level 4 is never automated, and levels 2–3 only under a recorded, unexpired approval of that digest. | `tests/test_policy.py`, `tests/test_governance.py` |
| E4 | Injection (SQL, template, command) | all | SQL is parameterised everywhere (the one dynamic part, a schema name, is checked). No shell. Templates fill only FHIR ids into queries. | `tests/test_fhir_client.py`, `tests/test_omop.py`; ZAP active rules |
| E5 | A vulnerable dependency | all | `pip-audit` (Python lockfile) and `npm audit` (Hub site) in CI, with pinned versions and lockfiles. | CI job `security` |

## Residual risks and assumptions

- **The deployment is the site's.** TLS termination, network placement, backups, the
  database's own access control and the PHI key's custody (a KMS or secret manager) belong to
  the deploying organization (`docs/deploying-with-phi.md`).
- **Open mode trusts its network.** Without sign-in the server is open. Production sits behind
  Vercel Authentication, and the sandbox listens on 127.0.0.1 only.
- **Clinical content** (pack criteria, rules, thresholds) is outside a security review. It
  needs clinician review and local validation, and every pack's card says so.
- **Synthetic data only** until the owner's go-ahead for real patients, after this review.

## Reporting a vulnerability

See [`SECURITY.md`](https://github.com/Junebugg1214/openhealth-hub/blob/main/SECURITY.md).
