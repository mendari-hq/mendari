#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# The secret scan (LP-02): gitleaks, pinned here and nowhere else, over every commit of every ref
# of the repository at $1 (default: this one), merge resolutions included (--diff-merges=remerge:
# without it git shows no diff for a merge, and what a merge alone adds is never read), with this
# repository's reviewed rules (.gitleaks.toml) and no other allowance.
#   exit 0: nothing found; 3: a finding; anything else: the scan did not run as it should (a git
#   or docker error, an ignore file) and proves nothing.
set -eu
repo=$(cd "${1:-.}" && pwd)
config="$(cd "$(dirname "$0")/.." && pwd)/.gitleaks.toml"
image="ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
if [ -e "$repo/.gitleaksignore" ]; then
  echo "secret scan: $repo/.gitleaksignore would hide findings; allow in .gitleaks.toml instead" >&2
  exit 2
fi
log=$(mktemp)
set +e
# Read-write: a merge's resolution is rebuilt in temporary objects git removes after.
docker run --rm -v "$repo:/repo" -v "$config:/gitleaks.toml:ro" "$image" \
  git /repo --config /gitleaks.toml --log-opts='--all --full-history --diff-filter=tuxdb --diff-merges=remerge' \
  --redact --no-banner --no-color --exit-code 3 2> "$log"
status=$?
set -e
cat "$log" >&2
# gitleaks logs a git error (a bad option, a missing object) and then reports a clean scan.
if grep -q ' ERR ' "$log"; then
  rm -f "$log"
  echo "secret scan: gitleaks logged an error; the history was not fully read" >&2
  exit 2
fi
rm -f "$log"
exit "$status"
