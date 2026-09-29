#!/usr/bin/env bash
# Rebuild the reference packs' derived artifacts from source, deterministically:
# synthetic data -> trained model -> published benchmark -> Decision Card.
set -euo pipefail
cd "$(dirname "$0")/.."

PACK=packs/patient-inbox-router
uv run openhealth sandbox generate --kind inbox
uv run openhealth train "$PACK"
uv run openhealth test "$PACK"
uv run openhealth eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run openhealth card "$PACK" --write

PACK=packs/clinical-trial-prescreener
uv run openhealth sandbox generate --kind trial
uv run openhealth test "$PACK"
uv run openhealth eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run openhealth card "$PACK" --write

PACK=packs/referral-router
uv run openhealth sandbox generate --kind referral
uv run openhealth train "$PACK"
uv run openhealth test "$PACK"
uv run openhealth eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run openhealth card "$PACK" --write

PACK=packs/prior-auth-preflight
uv run openhealth sandbox generate --kind prior-auth
uv run openhealth test "$PACK"
uv run openhealth eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run openhealth card "$PACK" --write
