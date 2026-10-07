#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# The positive control (LP-02): the scan (scripts/secret_scan.sh), exactly as CI runs it, must find
# both keys planted where only a full scan looks (scripts/plant_fake_secret.sh). Without it a
# clean scan proves nothing.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
control=$(mktemp -d)
trap 'rm -rf "$control"' EXIT
sh "$here/plant_fake_secret.sh" "$control/repo"
set +e
sh "$here/secret_scan.sh" "$control/repo" > /dev/null 2> "$control/log"
found=$?
set -e
if [ "$found" -ne 3 ] || ! grep -q 'leaks found: 2' "$control/log"; then
  echo "::error::the scan missed a planted key (exit $found): $(grep -E 'leaks|commits scanned' "$control/log" | tr '\n' ' ')" >&2
  exit 1
fi
echo "positive control: both planted keys found"
