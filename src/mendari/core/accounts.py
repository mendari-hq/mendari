# SPDX-License-Identifier: Apache-2.0
"""People, organizations and memberships (LP-12, ADR-0008): the rules the store and the server
share. The records live in the audit store (`core/audit.py`); memberships mode reads them
(`MENDARI_ACCOUNTS=memberships`), claims mode never does.

- A person is their identity provider's issuer and subject (`person_id`, hashed so it names
  nobody), with the e-mail address it verified, kept to match invitations: ASCII, compared
  without regard to the case of its letters (`normalise_email`).
- An organization is a record with an id of lower-case letters, digits and hyphens. `default`
  is reserved for the owner's existing data: only the bootstrap creates its record.
- A membership holds a set of roles: the server's roles, plus `pack_author` (which grants
  nothing until LP-20 gives it permissions) and `labeller` (which grants `label`, labelling the
  cases of hidden evaluation sets, LP-11, in memberships mode only). `grading` is reserved for
  a machine (a grading key, LP-11) and is never held by a membership.
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
USER_ID = re.compile(r"user:[0-9a-f]{16}")  # `person_id`'s form
RESERVED_ORG = DEFAULT_ORG
# The server's roles for people and machines (server/auth.py `ROLES`, which a test keeps equal).
PERSON_ROLES = ("viewer", "reviewer", "service", "governance", "admin")
RESERVED_ROLES = ("pack_author", "labeller", "grading")
NEVER_ON_A_MEMBERSHIP = frozenset({"grading"})
# The reserved roles that grant something on a membership (server/auth.py `PERMISSIONS`).
GRANTING_RESERVED = ("labeller",)
MEMBERSHIP_ROLES = (*PERSON_ROLES, *(r for r in RESERVED_ROLES if r not in NEVER_ON_A_MEMBERSHIP))
ADMIN = "admin"
# Who labels a hidden set's cases (`label`), and the roles that manage hidden sets (`curate`,
# server/auth.py `PERMISSIONS`, which a test keeps equal): the store assigns a case only to a
# labeller who holds none of them in the set's organization (separation of duties, LP-11h).
LABELLER = "labeller"
CURATING = (ADMIN,)
INVITE_DAYS = 7
INVITE_MAX_DAYS = 30
# Open requests to join one person may have at once (`AuditStore.request_access`): a bound on
# what any account that signs in can add to the chain.
OPEN_REQUESTS_MAX = 5
TOKEN_PREFIX = "ohi_"
_EMAIL_DIGEST_PREFIX = b"mendari-email\x00"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")


class AccessRefused(ValueError):
    """A request to join that is not made (`AuditStore.request_access`), with the fixed `reason`
    the security log gives it: `unknown_organization`, `member` or `open_requests`."""

    def __init__(self, message: str, reason: str):
        self.reason = reason
        super().__init__(message)


class MembershipRefused(ValueError):
    """A membership change that is not made, with its fixed `reason`: `no_membership` (unknown,
    another organization's or ended), `last_admin` (it would leave the organization without an
    administrator who signed in) or `member` (the person, or the pending address, is already a
    member)."""

    def __init__(self, message: str, reason: str):
        self.reason = reason
        super().__init__(message)


def person_id(issuer: str, subject: str) -> str:
    """A signed-in person's id (`Principal.id`): their subject at their identity provider, which
    is stable and unique only together with the issuer, hashed so it names nobody."""
    digest = hashlib.sha256(f"{issuer}|{subject}".encode()).hexdigest()
    return f"user:{digest[:16]}"


def check_org_id(org: str) -> str:
    """`org` when it is an organization id; a `ValueError` otherwise."""
    if not is_org_id(org):
        raise ValueError(
            "an organization id is 2 to 63 lower-case letters, digits or hyphens, starting "
            "with a letter or digit"
        )
    return org


def is_org_id(org: str) -> bool:
    """Whether `org` can be an organization's id, and so have a record."""
    return isinstance(org, str) and ORG_ID.fullmatch(org) is not None


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
    """An e-mail address as it is matched: trimmed, ASCII only, ASCII letters lower-cased. A
    `ValueError` for what is not one such address. Never case-folded: folding maps different
    mailboxes to one ("ß" to "ss", the Kelvin sign to "k"), and the match proves who someone
    is (an invitation, a pending membership); an internationalised address is refused."""
    if not isinstance(email, str):
        raise ValueError("an e-mail address is text")
    address = email.strip()
    if not address.isascii():
        raise ValueError("an e-mail address here is ASCII (an internationalised one is refused)")
    address = address.lower()
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
