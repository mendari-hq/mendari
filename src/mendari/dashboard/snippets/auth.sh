#!/usr/bin/env bash
# Authentication: whether sign-in is on, and what your API key may do. People sign in at
# /auth/login; machines send `Authorization: Bearer ohk_…`, a key a signed-in administrator
# creates (never in a URL or a form, and never with another key).
#
# MENDARI is the server; MENDARI_KEY is your API key (open mode ignores it). Needs curl and jq.
# CI runs this file in open mode and with a service key (scripts/check_doc_snippets.py).

# step: mode — Is sign-in on here? (public, no credential)
curl -sS --fail-with-body "$MENDARI/api/auth/me" | jq '{mode, signed_in}'

# step: me — What your key may do: its role, permissions, scopes and expiry
curl -sS --fail-with-body "$MENDARI/api/auth/me" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq .principal
