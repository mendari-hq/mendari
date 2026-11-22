# Verifying a scoreboard result

A scoreboard result is one configuration's figures on one set, made under the
[grading method](method-1.0.md) (section 11). Mendari's owner signs each result with a grading key
held offline. Anyone can check a signed result with its public key alone: no secret, no account
and no server. This page is for whoever checks a result, and for the key holder who signs.

No result is signed yet. `scoreboard/keys.json` lists no key until the owner adds one, and until
then every result reads "unsigned".

!!! warning "Engineering evaluation, not clinical validation"
    A signature says that Mendari's owner vouches for a result, exactly as it is. It does not
    make the figures right, and it does not make an agent fit for patient care. A public set's result
    can also be recomputed from its committed inputs; a hidden set's cannot, so its signature is
    the owner's word for it.

## Checking a result

From the repository's root:

```bash
mendari scoreboard verify scoreboard/results/<sha256>.json      # one or more files
mendari scoreboard verify RESULT.json --keys path/to/keys.json   # another keys file
mendari scoreboard verify RESULT.json --public-key <base64>      # one public key, any day
```

It needs no secret and writes nothing. Each result that verifies is one line on standard output,
naming its key and the method it was made under. Each one that does not is one `error:` line,
saying why, and the exit code is 1. A file it cannot read (missing, not UTF-8, not JSON) is one
such line too, and the next file is still checked. A result with no signature reads `unsigned`.

The verifier refuses a result, saying which of these it is:

- it is not a scoreboard result: an entry (`mendari.scoreboard/1`) or a hidden set's scoreboard
  answer (`mendari.hidden_scoreboard/1`);
- its body is not canonical JSON: a key twice in one object, NaN, or an infinity (a number too
  large for a float reads as an infinity), or it holds a whole number too long to read;
- its scope names no method, a method version this engine does not know, or a SHA-256 that is not
  that version's;
- its scope names no day it was measured;
- it has no signature (`unsigned`), or a signature that is not a key id and a value, in one
  spelling of base64;
- its key is a publisher's or a registry's key this machine pinned (`mendari pull` keeps them in
  `trusted_keys.json`), even when the keys file lists it;
- its key is not in the keys file, or is not valid on the day the result was measured;
- its signature does not hold: a byte of the result changed after it was signed.

## What is signed

A signed result is the result with one more field:

```text
"signature": {"key_id": "ed25519:<16 hex>", "value": "<base64>"}
```

The value is an Ed25519 signature over these bytes:

1. `mendari-grading-result-v1`, then a zero byte;
2. the result without its `signature`, as canonical JSON: keys sorted, no spaces, ASCII only,
   no NaN (`mendari.evals.ledger.canonical`).

Adding the signature changes nothing else. The result's body, and the SHA-256 that names it in
the store, stay the same. A key id is `ed25519:` and the first 16 hex digits of the SHA-256 of
the raw public key, as for publisher keys.

Two fields of the result's scope are read:

- `scope.method`: its `version` and `sha256`. The version must be one this engine knows, with
  that version's hash (`mendari.evals.method`). While the method is a draft, any edit to it
  re-pins the hash, so a result made under an earlier hash no longer verifies.
- `scope.measured_on`: the day the result was measured (YYYY-MM-DD). It must fall within the
  key's valid dates.

Every other signature Mendari makes is over canonical JSON with no prefix: the daily checkpoint,
an agent archive's manifest and a recall. So none of them verifies as a result, even made with the
same key, and a result's signature verifies as none of them.

## The keys file

`scoreboard/keys.json` holds the public keys only:

```json
{
  "format": "mendari.grading-keys/1",
  "keys": [
    {
      "key_id": "ed25519:<16 hex>",
      "public_key": "<the 32-byte public key in base64>",
      "valid_from": "YYYY-MM-DD",
      "valid_until": null
    }
  ]
}
```

Each key signs results measured from `valid_from` to `valid_until`, both included; `null` has
no end. A key's id must be its public key's, no id is listed twice, and an entry holds these four
fields and nothing else. The file is read as strictly as a result: a field twice in one entry (a
new `valid_until` written beside the old one, say), NaN or an infinity is refused, never read as
one of its values. A file that breaks any of this stops the verifier.

## For the key holder

The grading key is `MENDARI_GRADING_SIGNING_KEY`: 32 random bytes in base64, the Ed25519 private
key's seed. It is the owner's, made and kept offline.

- It is never a server setting, never a CI secret, and never in a deployment's environment.
  Only `mendari scoreboard sign` uses it, and that refuses to run when `CI` or `GITHUB_ACTIONS`
  is set. A server only compares its evaluation-set keys against it, to refuse an equal one.
- It must differ from every other key Mendari uses: the checkpoint key, the registry's signing
  key, the PHI key and each PHI read key, the input hash key, and the three evaluation-set keys.
  A reused or malformed key stops the command, and the message names variables, never a value.
  Publisher keys are files, not settings: `sign` refuses a key this machine pinned as a
  publisher's or a registry's.
- It does not sign a hidden set's freeze commitment. A signed hidden-set result carries the
  commitment instead.

Making the key, once, straight into a file in your own private folder (never in the
repository), so it is never on the screen:

```bash
mkdir -p ~/private && chmod 700 ~/private
(umask 077 && openssl rand -base64 32 > ~/private/mendari-grading.key)
```

Signing, on your own machine. The key goes from its file into the one command, and nowhere else:

```bash
MENDARI_GRADING_SIGNING_KEY="$(cat ~/private/mendari-grading.key)" \
  mendari scoreboard sign scoreboard/results/<sha256>.json      # one or more files
```

Never type or paste the key itself into a shell, not even after `export`: the shell keeps what
you type in a history file (such as `~/.zsh_history` or `~/.bash_history`), and `unset` does not
take it out. The line above puts only the file's name in the history, and sets the key for that
one command, so there is nothing to unset.

`sign` checks every file before it writes any. It refuses a result that is signed already (a
result is signed once), one the verifier would refuse before its signature, and a symbolic link.
Then it replaces each file whole, with the signature added, and prints each file's body SHA-256
and the key's id and public key. It never prints the key.

The first time, add the public key it prints to `scoreboard/keys.json`, with `valid_from` set to
the first day it signs results for and `valid_until` set to `null`. Then check with `verify`, and
write the scoreboard's page again (`uv run python scripts/scoreboard_page.py`): it marks each
unsigned result, and `sign` writes no page.

Rotating to a new key: add the new key, and set the old key's `valid_until` to the last day it
signs results for. What it signed for results measured up to that day still verifies.

A key that leaked: take it out of the keys file, and make a new key. Nothing the old key signed
verifies any more, since a leaked key could sign a result dated within its valid days: `verify`
says each such result is signed with a key that is not in the keys given. Those results are not
signed again. A stored result changes only by gaining a signature while it is unsigned
([method](method-1.0.md), section 11), so `sign` refuses one that is signed already, and editing
the file by hand to take its signature out breaks the store's rule. Run each one again instead,
as for a stale entry (section 12): the new result supersedes the old one, and you sign it with
the new key. List the new key first, with `valid_from` on or before the day it is run again.
