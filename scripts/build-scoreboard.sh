#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# The early scoreboard (LP-17f; the grading method's section 14): every local configuration of
# each reference pack on each of its public scoring sets (its test set, its enriched set and that
# set's holdout), stored unsigned in the results store (scoreboard/), then the page written from
# it (docs/grading/scoreboard.md). A result still current (the pack's digest, the set and the
# method unchanged, and not past its stale date) is kept, never run again; a stale one is run
# again, and the new result supersedes it. Never part of build-reference-pack.sh: a run record
# holds each case's latency, which no rebuild reproduces. Commit scoreboard/ and the page together.
set -euo pipefail
cd "$(dirname "$0")/.."

for PACK in mendari/ai-guard mendari/clinical-trial-prescreener mendari/patient-inbox-router \
  mendari/prior-auth-preflight mendari/referral-router; do
  uv run mendari scoreboard run "$PACK" --every-local
done
uv run python scripts/scoreboard_page.py
results=$(find scoreboard/results -name '*.json' | wc -l | tr -d ' ')
echo "scoreboard/: $results results, $(du -sh scoreboard | cut -f1) in all"
