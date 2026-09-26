#!/usr/bin/env bash
# Rebuild the Patient Inbox Router's derived artifacts from source, deterministically:
# synthetic data -> trained model -> published benchmark -> Decision Card.
set -euo pipefail
cd "$(dirname "$0")/.."

PACK=packs/patient-inbox-router

uv run openhealth sandbox generate --pack-dir "$PACK" --local-out datasets/local-site-example.jsonl
uv run openhealth train "$PACK"
uv run openhealth test "$PACK"
uv run openhealth eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run openhealth card "$PACK" --write
