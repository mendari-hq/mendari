#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# The secret scan's positive control (LP-02): a throwaway git repository at $1 holding two access
# keys made up here at random, each where only a full scan looks: one only on a pull request's
# head (refs/pull/1/head, on no branch), one only in a merge's resolution. The keys exist nowhere
# else, are never printed, and are in no file of this repository.
set -eu
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1  # no developer's hooks or signing
# Its own identity: a runner has none, and a merge refuses to guess one.
export GIT_AUTHOR_NAME=control GIT_AUTHOR_EMAIL=control@example.org
export GIT_COMMITTER_NAME=control GIT_COMMITTER_EMAIL=control@example.org
dir="$1"

key() {  # AKIA and 16 base-32 symbols, redrawn until well above the rule's entropy floor (3.0)
  key=""
  until awk -v k="$key" 'BEGIN { n = length(k); for (i = 1; i <= n; i++) c[substr(k, i, 1)]++
      for (s in c) { p = c[s] / n; h -= p * log(p) } exit !(n == 20 && h / log(2) > 3.5) }'; do
    key="AKIA$(LC_ALL=C tr -dc 'A-Z2-7' < /dev/urandom 2> /dev/null | head -c 16)"
  done
  printf '%s' "$key"
}
commit() {
  git -C "$dir" add -A
  git -C "$dir" commit --no-verify -q -m "$1"
}

git init -q "$dir"
git -C "$dir" checkout -q -b main
printf 'control\n' > "$dir/README"
commit start
# A pull request's head: kept by the host, on no branch.
git -C "$dir" checkout -q -b proposal
printf 'aws_access_key_id = %s\n' "$(key)" > "$dir/settings.ini"
commit "a pull request"
git -C "$dir" update-ref refs/pull/1/head HEAD
git -C "$dir" checkout -q main
git -C "$dir" branch -q -D proposal
# A merge whose resolution alone adds a key.
git -C "$dir" checkout -q -b topic
printf 'topic\n' > "$dir/topic.txt"
commit topic
git -C "$dir" checkout -q main
git -C "$dir" merge -q --no-ff --no-commit topic > /dev/null 2>&1
printf 'aws_access_key_id = %s\n' "$(key)" > "$dir/merged.ini"
commit merge
