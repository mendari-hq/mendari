# Mendari Hub website

The public face of the registry: search Decision Packs by words, risk level, standard and
publisher type, then read, for each pack, what the registry checked (its signature, archive,
digest, engine and review status) apart from what the publisher claims (its name, the risk
level it declares, its licence), the Decision Card the registry rendered from the pack's own
files, and its versions (ADR-0012). The site shows no figure the registry did not compute
itself, and renders publisher text without raw HTML or unsafe links. Every page reads the
registry API (`src/mendari/hub/registry.py`) at request time, from `REGISTRY_URL`.

```bash
# a registry with the five reference packs, and the test pack whose text tries to run a script
MENDARI_REGISTRY_ADMIN_TOKEN=dev uv run mendari registry serve --db var/registry.db &
MENDARI_REGISTRY_ADMIN_TOKEN=dev uv run python scripts/hub_seed.py --registry http://127.0.0.1:8100 --hostile-example

cd hub && npm ci
REGISTRY_URL=http://127.0.0.1:8100 npm run dev        # http://localhost:3100
npm run lint && npm run typecheck && npm run build && npm test   # what CI runs
```

Next.js (App Router, TypeScript), no client state: search is a plain GET form. Styles use
tokens (`app/globals.css`), with light and dark from the system setting.
