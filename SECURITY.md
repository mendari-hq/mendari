# Security and safety

## Scope of v0.1

OpenHealth Hub v0.1 is a research preview for **synthetic data only**.

- It is not a medical device and makes no clinical claims.
- The HTTP server has **no authentication or authorization**. It binds to `127.0.0.1` by
  default, and the docker sandbox publishes ports on localhost only. Do not expose it to a
  network.
- Do not send real protected health information (PHI) to it, to the sandbox FHIR server, or
  to remote providers.
- Audit records store full inputs by default (`audit.store_input: full`). Use `hash` for any
  pack that might see sensitive text.

## Remote providers

The Jev adapter reads its key from `JEV_API_KEY`, sends only a pack's `state_fields`, requires
HTTPS (plain HTTP is allowed only for localhost), refuses redirects, and caps response size.
Credentials and provider error bodies are never written to audit records or error messages.

## Pack supply chain

Packs contain behaviour: regular expressions, models and policies. Review a pack like code
before running it, and pin it by digest (recorded in every decision and report). v0.1 loads
packs only from local paths; signed remote packs are planned.

## Reporting a vulnerability

Please report security issues privately to the maintainers rather than in public issues.
Include steps to reproduce, the affected version, and the impact.
