# Mendari Hub website

The public face of the registry: search Decision Packs by words, risk level, standard and
publisher type, then read each pack's Decision Card, benchmark, provider leaderboard, versions
and the evaluations sites have shared. Every page reads the registry API
(`src/mendari/hub/registry.py`) at request time, from `REGISTRY_URL`.

```bash
# a registry with the five reference packs
MENDARI_REGISTRY_ADMIN_TOKEN=dev uv run mendari registry serve --db var/registry.db &
MENDARI_REGISTRY_ADMIN_TOKEN=dev uv run python scripts/hub_seed.py --registry http://127.0.0.1:8100

cd hub && npm ci
REGISTRY_URL=http://127.0.0.1:8100 npm run dev        # http://localhost:3100
npm run lint && npm run typecheck && npm run build && npm test   # what CI runs
```

Next.js (App Router, TypeScript), no client state: search is a plain GET form. Styles use
tokens (`app/globals.css`), with light and dark from the system setting.
