#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# The scoreboard (LP-17f; the grading method's section 14): every local configuration of the
# bundled demo agent on its public sets, stored unsigned in the results store (scoreboard/), then
# the page written from it (docs/grading/scoreboard.md) and the public page's scores
# (web/index.html). A result still current (the agent's digest, the set and the method unchanged,
# and not past its stale date) is kept, never run again; a stale one is run again, and the new
# result supersedes it. Commit scoreboard/ and both pages together.
set -euo pipefail
cd "$(dirname "$0")/.."

for AGENT in $(find agents -mindepth 2 -maxdepth 2 -name pack.yaml | sort); do
  id=$(sed -n 's/^id: //p' "$AGENT")
  uv run mendari scoreboard run "$id" --every-local
done
uv run python scripts/scoreboard_page.py
results=$(find scoreboard/results -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
echo "scoreboard/: $results results"
