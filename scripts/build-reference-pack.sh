#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Rebuild the reference packs' derived artifacts from source, deterministically:
# synthetic data -> trained model -> published benchmark and its ledger -> Decision Card.
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
