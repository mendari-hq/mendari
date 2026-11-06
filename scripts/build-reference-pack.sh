#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Rebuild the reference packs' derived artifacts from source, deterministically:
# synthetic data -> trained model -> published benchmark and its ledger -> Decision Card, then
# the early scoreboard's page (docs/grading/scoreboard.md) and the public page's scores
# (web/index.html), which name each result's pack digest or say it is stale.
set -euo pipefail
cd "$(dirname "$0")/.."

PACK=packs/patient-inbox-router
uv run mendari sandbox generate --kind inbox
uv run mendari train "$PACK"
uv run mendari test "$PACK"
rm -f "$PACK"/evals/ledgers/*.jsonl  # the benchmark's ledger is written again beside it
uv run mendari eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run mendari card "$PACK" --write

PACK=packs/clinical-trial-prescreener
uv run mendari sandbox generate --kind trial
uv run mendari test "$PACK"
rm -f "$PACK"/evals/ledgers/*.jsonl  # the benchmark's ledger is written again beside it
uv run mendari eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run mendari card "$PACK" --write

PACK=packs/referral-router
uv run mendari sandbox generate --kind referral
uv run mendari train "$PACK"
uv run mendari test "$PACK"
rm -f "$PACK"/evals/ledgers/*.jsonl  # the benchmark's ledger is written again beside it
uv run mendari eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run mendari card "$PACK" --write

PACK=packs/prior-auth-preflight
uv run mendari sandbox generate --kind prior-auth
uv run mendari test "$PACK"
rm -f "$PACK"/evals/ledgers/*.jsonl  # the benchmark's ledger is written again beside it
uv run mendari eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run mendari card "$PACK" --write

PACK=packs/ai-guard
uv run mendari sandbox generate --kind guard
uv run mendari test "$PACK"
rm -f "$PACK"/evals/ledgers/*.jsonl  # the benchmark's ledger is written again beside it
uv run mendari eval "$PACK" --dataset test --baseline none --out "$PACK/evals/benchmark.json"
uv run mendari card "$PACK" --write

# The page is written from the results store and the packs: a pack change makes its results
# stale, and the page says so. Nothing is run here (scripts/build-scoreboard.sh runs results).
uv run python scripts/scoreboard_page.py
echo "commit the pages with the packs' data, models, benchmarks and cards: docs/grading/scoreboard.md web/index.html"
