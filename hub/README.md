# Mendari Hub website

The public face of the registry: search agents by words, risk level, standard and
publisher type, then read, for each agent, what the registry checked (its signature, archive,
digest, engine and review status) apart from what the publisher claims (its name, the risk
level it declares, its licence), the Decision Card the registry rendered from the agent's own
files, and its versions (ADR-0012). The site shows no figure the registry did not compute
itself, and renders publisher text without raw HTML or unsafe links. Every page reads the
registry API (`src/mendari/hub/registry.py`) at request time, from `REGISTRY_URL`.

| Address | What it shows |
|---|---|
| `/` | The catalog, searched by words, risk level, standard and publisher type |
| `/agents/<namespace>/<name>` | An agent: what the registry checked, what the publisher claims, its evaluations, site evidence, its Decision Card |
| `/agents/<namespace>/<name>/evaluation` | The agent's evaluation page, for a README to link: what was evaluated and on what data, its configurations compared (one is named as having done best on that dataset only when the registry's answer names it, `comparison.best`: alone in the first band; otherwise the page says none is clearly best), whose key signed each result, what the numbers do not show (`?version=` for an earlier version). [Publishing an agent's evaluation](../docs/guides/publishing-evaluations.md) |
| `/publishers/<namespace>` | A publisher: its agents and its signing keys |

```bash
# a registry with the bundled agent (and, with MENDARI_AGENTS_PATH, the test fixtures' agents),
# the test agent whose text tries to run a script, two synthetic sites' evidence and the
# publisher's evaluations of the demo agent
uv run mendari registry serve --db var/registry.db &
uv run python scripts/hub_seed.py --registry http://127.0.0.1:8100 --db var/registry.db \
  --hostile-example --sites --evaluations

cd hub && npm ci
REGISTRY_URL=http://127.0.0.1:8100 npm run dev        # http://localhost:3100
npm run lint && npm run typecheck && npm run build && npm test   # what CI runs
```

| Setting | What it does |
|---|---|
| `REGISTRY_URL` | The registry the site reads, on the server, at request time |
| `REGISTRY_PUBLIC_URL` | The registry's address as the public reaches it, for the badge line the evaluation page shows. Only where `REGISTRY_URL` is an address the public cannot reach (a container's name); unset, `REGISTRY_URL` is used when it is https |

The tests (`npm test`, Playwright) start the built site three times: against the seeded registry
above, against an address that refuses (`tests/unavailable.spec.ts`), and against a stand-in
registry that answers an agent's evaluation in every state the page has words for
(`tests/stub-registry.mjs`, from `tests/fixtures/evaluation_pages.json` in the repository, which
`uv run python -m tests.support.evaluation_pages` writes from a real registry's answers).
`HUB_TEST_PORT` moves the ports they listen on (3100 to 3102, and 3199).

Next.js (App Router, TypeScript), no client state: search is a plain GET form. Styles use
tokens (`app/globals.css`), with light and dark from the system setting.
