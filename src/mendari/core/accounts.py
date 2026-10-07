# SPDX-License-Identifier: Apache-2.0
"""People, organizations and memberships (LP-12, ADR-0008): the rules the store and the server
share. The records live in the audit store (`core/audit.py`); memberships mode reads them
(`MENDARI_ACCOUNTS=memberships`), claims mode never does.

- A person is their identity provider's issuer and subject (`person_id`, hashed so it names
  nobody), with the e-mail address it verified, kept to match invitations.
- An organization is a record with an id of lower-case letters, digits and hyphens. `default`
  is reserved for the owner's existing data: only the bootstrap creates its record.
- A membership holds a set of roles: the server's roles, plus `pack_author` and `labeller`,
  which grant nothing until LP-20 and LP-11 give them permissions. `grading` is reserved for a
  machine (LP-11) and is never held by a membership.
- What the chain records of an e-mail address is a digest, keyed with the input-hash key
  (`MENDARI_INPUT_HASH_KEY`) when there is one, so the chain names nobody; without that key it
  is a plain SHA-256, which a dictionary of addresses could reverse.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable

from mendari.core.phi import PhiPolicy, key_id
from mendari.core.tenancy import DEFAULT_ORG

ORG_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
RESERVED_ORG = DEFAULT_ORG
# The server's roles for people and machines (server/auth.py `ROLES`, which a test keeps equal).
PERSON_ROLES = ("viewer", "reviewer", "service", "governance", "admin")
RESERVED_ROLES = ("pack_author", "labeller", "grading")
NEVER_ON_A_MEMBERSHIP = frozenset({"grading"})
MEMBERSHIP_ROLES = (*PERSON_ROLES, *(r for r in RESERVED_ROLES if r not in NEVER_ON_A_MEMBERSHIP))
ADMIN = "admin"
INVITE_DAYS = 7
INVITE_MAX_DAYS = 30
TOKEN_PREFIX = "ohi_"
_EMAIL_DIGEST_PREFIX = b"mendari-email\x00"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")


def person_id(issuer: str, subject: str) -> str:
    """A signed-in person's id (`Principal.id`): their subject at their identity provider, which
    is stable and unique only together with the issuer, hashed so it names nobody."""
    digest = hashlib.sha256(f"{issuer}|{subject}".encode()).hexdigest()
    return f"user:{digest[:16]}"


def check_org_id(org: str) -> str:
    """`org` when it is an organization id; a `ValueError` otherwise."""
    if not isinstance(org, str) or not ORG_ID.fullmatch(org):
        raise ValueError(
            "an organization id is 2 to 63 lower-case letters, digits or hyphens, starting "
            "with a letter or digit"
        )
    return org


def check_roles(roles: Iterable[str]) -> list[str]:
    """The roles a membership may hold, sorted and each once; a `ValueError` for none, an
    unknown one, or one no membership holds (`grading`)."""
    chosen = sorted(set(roles))
    if not chosen:
        raise ValueError("a membership holds at least one role")
    for role in chosen:
        if role in NEVER_ON_A_MEMBERSHIP:
            raise ValueError(f"no membership holds {role!r}: it is a machine's (LP-11)")
        if role not in MEMBERSHIP_ROLES:
            raise ValueError(f"unknown role {role!r} (one of {', '.join(MEMBERSHIP_ROLES)})")
    return chosen


def normalise_email(email: str) -> str:
    """An e-mail address as it is matched: trimmed and case-folded. A `ValueError` for what is
    not one address."""
    if not isinstance(email, str):
        raise ValueError("an e-mail address is text")
    address = email.strip().casefold()
    if len(address) > 254 or not _EMAIL.fullmatch(address):
        raise ValueError("not an e-mail address")
    return address


def email_digest(policy: PhiPolicy, email: str) -> str:
    """What the chain records of an address: an HMAC under the input-hash key when there is one
    (`hmac-sha256:<key id>:<hex>`), else the plain `sha256:<hex>`."""
    address = normalise_email(email).encode()
    if not policy.hash_key:
        return "sha256:" + hashlib.sha256(_EMAIL_DIGEST_PREFIX + address).hexdigest()
    digest = hmac.new(policy.hash_key, _EMAIL_DIGEST_PREFIX + address, hashlib.sha256)
    return f"hmac-sha256:{key_id(policy.hash_key)}:{digest.hexdigest()}"


def name_digest(name: str) -> str:
    """What the chain records of an organization's name (ids and hashes only)."""
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()
