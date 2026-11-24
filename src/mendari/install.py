# SPDX-License-Identifier: Apache-2.0
"""The one-install path's settings (`deploy/site`), made and checked inside the engine's image:
`python -m mendari.install <command>`.

A hospital's machine needs Docker and nothing else. `deploy/site/mendari-site` (POSIX shell)
does what must happen on the host (Docker itself, `docker compose`, waiting for health), and
runs these commands in the image for everything else, so that one implementation answers, the
server's own: an address, an issuer or a key the server would refuse is refused here, with the
server's message, before anything starts.

- `settings`: ask the few things only the site can answer (its public address, its identity
  provider's issuer, client id and secret, the first administrator's sign-in address, what the
  organization is called, whether each case's input is kept), generate everything else (every
  secret from the system's random source, `secrets`), build the server once on an empty
  in-memory database with the result (`starts`), and only then write `.env` (mode 0600, whole
  or not at all). Without a certificate in `tls/` it stops, unless a self-signed one was asked
  for, which it makes and marks as one.
- `check`: build the server on an empty in-memory database with the environment as it is.
  Whatever would stop the server stops here.
- `migrate`: open the deployment's database as the server does, which brings it up to date.
- `image`: record a new engine image, keeping the one before for a rollback; `--back` swaps
  them.
- `summary`: what was made and where. Never a secret.

**Run it again.** A generated secret is never made twice: what `.env` holds is kept, and so is
every line a person added to it. The answers are asked again with what is there as their
default. Patient data mode is chosen once: an install that keeps sealed inputs is not switched
to one that keeps none by answering differently.

**The settings file** is read by `docker compose` (`env_file`, and for the compose file's own
`${...}`), so it is written as compose reads one: a value of plain characters as it is, any
other in single quotes (nothing in them is special), and one that holds a single quote in
double quotes with `\\`, `"` and `$` escaped. `parse` reads the same three forms back.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import ipaddress
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mendari import __version__
from mendari.errors import MendariError

SETTINGS = ".env"
TLS_DIR = "tls"
CERTIFICATE = "fullchain.pem"
PRIVATE_KEY = "privkey.pem"
SELF_SIGNED_NOTE = "SELF-SIGNED.txt"
SELF_SIGNED_DAYS = 90
SELF_SIGNED_ORG = "Mendari self-signed certificate (trial only)"
# The one organization of a single-site install: its people's, its keys' and its EHRs'.
ORGANIZATION = "default"
RETENTION_DAYS = 30
BACKUP_KEEP_DAYS = 14
# Where the engine keeps what it pulls from a registry and the registry keys it pins: the
# image's own data folder, which the template's `state` volume holds across upgrades.
STATE_CACHE = "/data/cache"
STATE_CONFIG = "/data/config"
# An allow-list that names no origin (`providers/http.py`: blank would be "no list at all").
NONE_LISTED = ","
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLAIN = re.compile(r"[A-Za-z0-9_./:@%+=,-]*")
_ORGANIZATION_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,200}")

# What only the site can answer, by the name an answers file gives it.
ANSWERS = (
    "PUBLIC_URL",
    "OIDC_ISSUER",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "ADMIN_EMAIL",
    "ORGANIZATION",
    "PATIENT_DATA",
)
# What an answers file may also set; none of it is asked.
OPTIONAL = (
    "TIMEZONE",
    "SELF_SIGNED",
    "HTTPS_PORT",
    "HTTP_PORT",
    "REGISTRY_URL",
    "ALERT_WEBHOOK",
    "RETENTION_DAYS",
    "BACKUP_KEEP_DAYS",
)
# The secrets this installer generates, each with what it is for (the summary names them) and
# how one is made. Two of them are keys the engine reads as 32 random bytes in base64.


def _key() -> str:
    """32 bytes from the system's random source, in base64: what the engine reads as a key."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


GENERATED: dict[str, tuple[str, Callable[[], str]]] = {
    "POSTGRES_PASSWORD": ("the database's password", lambda: secrets.token_hex(24)),
    "MENDARI_PHI_KEY": ("the key that seals kept inputs and notes", _key),
    "MENDARI_INPUT_HASH_KEY": ("the key of each input's fingerprint", _key),
    "MENDARI_CHECKPOINT_KEY": ("the key that signs the daily checkpoint", _key),
    "CRON_SECRET": ("the scheduled jobs' secret", lambda: secrets.token_urlsafe(32)),
    "MENDARI_ALERT_SECRET": ("the secret that signs alerts", lambda: secrets.token_hex(32)),
}
# Every setting this installer writes, in the order of the file. Anything else found in the
# file is a person's own addition, and is kept as written.
MANAGED = (
    "MENDARI_IMAGE",
    "MENDARI_IMAGE_PREVIOUS",
    "MENDARI_PUBLIC_URL",
    "SITE_HOST",
    "SITE_HTTPS_PORT",
    "SITE_HTTP_PORT",
    "SITE_UID",
    "SITE_GID",
    "MENDARI_OIDC_ISSUER",
    "MENDARI_OIDC_CLIENT_ID",
    "MENDARI_OIDC_CLIENT_SECRET",
    "MENDARI_ACCOUNTS",
    "MENDARI_OPERATOR_ORG",
    "MENDARI_BOOTSTRAP_ADMIN",
    "MENDARI_BOOTSTRAP_ORG_NAME",
    "MENDARI_PHI_MODE",
    "MENDARI_PHI_KEY",
    "MENDARI_INPUT_HASH_KEY",
    "MENDARI_RETENTION_DAYS",
    "POSTGRES_PASSWORD",
    "MENDARI_CHECKPOINT_KEY",
    "CRON_SECRET",
    "BACKUP_KEEP_DAYS",
    "MENDARI_ALERT_WEBHOOK",
    "MENDARI_ALERT_SECRET",
    "MENDARI_TIMEZONE",
    "MENDARI_RATE_LIMITS",
    "MENDARI_ENVIRONMENT",
    "MENDARI_PROVIDER_ALLOWED_ORIGINS",
    "MENDARI_FHIR_ALLOWED_SERVERS",
    "MENDARI_REGISTRY_URL",
    "MENDARI_REGISTRY_COUNTERSIGNATURE",
    "MENDARI_CACHE_DIR",
    "MENDARI_CONFIG_DIR",
)
# The file's sections: a heading, what a person should know, and the settings under it.
SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "The engine's image. `mendari-site upgrade` changes it and keeps the one before for a\n"
        "# rollback.",
        ("MENDARI_IMAGE", "MENDARI_IMAGE_PREVIOUS"),
    ),
    (
        "The address people open, and the ports this machine listens on. The files in tls/ are\n"
        "# the certificate for that address. SITE_UID and SITE_GID own the backups.",
        (
            "MENDARI_PUBLIC_URL",
            "SITE_HOST",
            "SITE_HTTPS_PORT",
            "SITE_HTTP_PORT",
            "SITE_UID",
            "SITE_GID",
        ),
    ),
    (
        "Sign-in with your identity provider. People and their roles are managed in the\n"
        "# console's Access view. The first administrator's address is used once, at their first\n"
        "# sign-in, and ignored after that.",
        (
            "MENDARI_OIDC_ISSUER",
            "MENDARI_OIDC_CLIENT_ID",
            "MENDARI_OIDC_CLIENT_SECRET",
            "MENDARI_ACCOUNTS",
            "MENDARI_OPERATOR_ORG",
            "MENDARI_BOOTSTRAP_ADMIN",
            "MENDARI_BOOTSTRAP_ORG_NAME",
        ),
    ),
    (
        "Patient data mode, chosen at install and not changed here. The two keys were generated\n"
        "# for this deployment: keep a copy of this file where your organization keeps its\n"
        "# secrets. Without MENDARI_PHI_KEY, what was sealed cannot be read again.",
        ("MENDARI_PHI_MODE", "MENDARI_PHI_KEY", "MENDARI_INPUT_HASH_KEY", "MENDARI_RETENTION_DAYS"),
    ),
    (
        "The database, the audit trail's signed daily checkpoint, the scheduled jobs and the\n"
        "# nightly backups (kept in backups/ for BACKUP_KEEP_DAYS days). All generated.",
        ("POSTGRES_PASSWORD", "MENDARI_CHECKPOINT_KEY", "CRON_SECRET", "BACKUP_KEEP_DAYS"),
    ),
    (
        "Alerts. Set MENDARI_ALERT_WEBHOOK to the webhook of your on-call channel (treat it as a\n"
        "# secret), run `mendari-site start`, then send a test alert from the System view. Every\n"
        "# alert is signed with the generated secret.",
        ("MENDARI_ALERT_WEBHOOK", "MENDARI_ALERT_SECRET"),
    ),
    (
        "Calendar days and displayed times, limits, and what this deployment is called in its\n"
        "# own records.",
        ("MENDARI_TIMEZONE", "MENDARI_RATE_LIMITS", "MENDARI_ENVIRONMENT"),
    ),
    (
        "What may leave your network: nothing, until you list it. A model or agent service is\n"
        "# called only at an origin listed in the first, and a FHIR server is read only when\n"
        "# listed in the second (comma-separated origins, like https://agents.example.org). As\n"
        '# installed neither lists any: the lone comma says "none" (left empty, the first would\n'
        '# say "no list", and any https address could be called).',
        ("MENDARI_PROVIDER_ALLOWED_ORIGINS", "MENDARI_FHIR_ALLOWED_SERVERS"),
    ),
    (
        "The agent registry agents are adopted from (its https address), and where what is\n"
        "# pulled from it is kept. A pull is refused unless the registry counter-signed it.",
        (
            "MENDARI_REGISTRY_URL",
            "MENDARI_REGISTRY_COUNTERSIGNATURE",
            "MENDARI_CACHE_DIR",
            "MENDARI_CONFIG_DIR",
        ),
    ),
)
HEADER = (
    "# Mendari, one machine: this deployment's settings and secrets.\n"
    "# Written by `mendari-site install`; run it again to change an answer. It never makes a\n"
    "# secret twice, and it keeps the lines you add at the end. Keep this file private (it is\n"
    "# readable by its owner only) and never commit it.\n"
)
OWN = "Your own additions, kept as you wrote them."


class Refused(MendariError):
    """An answer or a file this installer does not take: said in one plain line, and nothing is
    written."""


# -- the settings file ----------------------------------------------------------------------------


def quote(value: str) -> str:
    """`value` as `docker compose` reads it back from an env file: plain characters as they
    are, anything else in single quotes (where nothing is special), and a value that holds a
    single quote in double quotes, with `\\`, `"` and `$` escaped."""
    if any(character in value for character in "\n\r\x00"):
        raise Refused("a setting is one line of text")
    if _PLAIN.fullmatch(value):
        return value
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def unquote(raw: str) -> str:
    """What `quote` wrote, read back; and a line a person wrote by hand, as compose reads it."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1]
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return re.sub(r"\\(.)", r"\1", text[1:-1])
    return re.split(r"\s+#", text, maxsplit=1)[0].strip()


def parse(text: str) -> dict[str, str]:
    """The settings of an env file, by name; comments, blank lines and anything that is not
    `NAME=value` are left out."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, raw = line.strip().partition("=")
        name = name.removeprefix("export ").strip()
        if separator and _NAME.fullmatch(name):
            values[name] = unquote(raw)
    return values


def read_settings(path: Path) -> tuple[dict[str, str], list[str]]:
    """What a settings file holds, and the lines of it that are a person's own: everything
    under the installer's marker, as written (their comments too), and any setting among the
    installer's own lines that it does not write. A file that is there and cannot be read stops
    the install: its secrets must never be made again over it."""
    if not path.exists():
        return {}, []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise Refused(
            f"{path.name} is there but cannot be read ({type(error).__name__}): nothing was "
            "changed. Fix its permissions, or restore it from where you keep its copy."
        ) from None
    lines = [line.rstrip() for line in text.splitlines()]
    marker = f"# {OWN}"
    under = lines.index(marker) + 1 if marker in lines else len(lines)
    own = []
    for line in lines[:under]:  # a line added among the installer's own is theirs too
        name, separator, _ = line.strip().partition("=")
        if separator and _NAME.fullmatch(name.strip()) and name.strip() not in MANAGED:
            own.append(line)
    own += lines[under:]
    while own and not own[-1]:
        own.pop()
    return parse(text), own


def render(values: Mapping[str, str], own: Sequence[str] = ()) -> str:
    """The settings file: each section's settings under what a person should know of them, then
    their own lines."""
    lines = [HEADER]
    for about, names in SECTIONS:
        present = [name for name in names if name in values]
        if present:
            lines.append(f"# {about}")
            lines += [f"{name}={quote(values[name])}" for name in present]
            lines.append("")
    if own:
        lines.append(f"# {OWN}")
        lines += list(own)
        lines.append("")
    return "\n".join(lines)


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    """Written whole beside the file, readable by its owner only, then moved in: a run that is
    cut short leaves the file as it was."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(staged, mode)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


# -- the answers ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Address:
    """The public address, taken apart: the URL as the engine is given it, its host, and the
    port people reach it at."""

    url: str
    host: str
    port: int


def public_address(text: str) -> Address:
    """`https://<host>[:port]`, a DNS name or an IPv4 address, with nothing after it."""
    value = text.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise Refused("the public address is like https://mendari.example.org") from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise Refused(
            "the public address starts with https://: a deployment people sign in to is never "
            "served over plain http"
        )
    if not host or parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise Refused(
            "the public address is https, a name and nothing after it, like "
            "https://mendari.example.org"
        )
    if ":" in host:
        raise Refused("the public address is a DNS name or an IPv4 address")
    shown = f"https://{host}" + (f":{port}" if port and port != 443 else "")
    return Address(shown, host, port or 443)


def _yes(text: str, what: str) -> bool:
    answer = text.strip().lower()
    if answer in ("y", "yes", "true", "on", "1"):
        return True
    if answer in ("n", "no", "false", "off", "0"):
        return False
    raise Refused(f"{what}: answer yes or no")


def _whole(text: str, what: str, low: int, high: int) -> int:
    value = text.strip()
    if not (value.isascii() and value.isdigit()) or not low <= int(value) <= high:
        raise Refused(f"{what} is a whole number from {low} to {high}")
    return int(value)


def _timezone(text: str) -> str:
    name = text.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise Refused(
            f"{name or 'that'} is not a time zone: give one like Europe/Paris or America/Chicago"
        ) from None
    return name


def _zone(console: Console, answers: Mapping[str, str], machine: str | None) -> str:
    """The time zone of calendar days and displayed times: the answer's when one names it (or
    the settings file holds one), else the machine's. A machine whose zone cannot be read is
    asked about at a terminal, and stops an unattended run: a deployment is never left on a
    zone nobody chose."""
    named = answers.get("TIMEZONE", "")
    if named:
        return _timezone(named)
    try:
        return _timezone(machine or "")
    except Refused:
        pass
    if not console.interactive:
        raise Refused(
            "this machine's time zone could not be read: add TIMEZONE to the answers file, like "
            "TIMEZONE=America/Chicago"
        )
    console.say()
    console.say(
        "This machine's time zone could not be read. Calendar days (a day's limits, the daily "
        "checkpoint's date) and displayed times follow it."
    )
    while True:
        typed = console.ask("Your time zone, like America/Chicago or Europe/Paris")
        try:
            return _timezone(typed)
        except Refused as refused:
            console.say(f"  {refused}")


def _email(text: str) -> str:
    from mendari.core.accounts import normalise_email

    try:
        return normalise_email(text)
    except ValueError as error:
        raise Refused(f"the administrator's sign-in address: {error}") from None


def _organization(text: str) -> str:
    name = " ".join(text.split())
    if not _ORGANIZATION_NAME.fullmatch(name):
        raise Refused("your organization's name is 1 to 200 characters on one line")
    return name


class Console:
    """Where the installer asks and tells: a terminal, or nothing to ask (an answers file gave
    everything)."""

    def __init__(self, *, interactive: bool, out: TextIO | None = None):
        self.interactive = interactive
        self.out = out or sys.stdout

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def ask(self, prompt: str, default: str = "", *, secret: bool = False) -> str:
        """One line from the person; their Enter alone takes `default`. A secret is typed
        hidden, and its default is never shown."""
        shown = "kept as it is" if secret and default else default
        suffix = f" [{shown}]" if shown else ""
        if secret:
            typed = getpass.getpass(f"{prompt}{suffix}: ")
        else:
            self.out.write(f"{prompt}{suffix}: ")
            self.out.flush()
            typed = sys.stdin.readline()
            if not typed:
                raise Refused("no answer could be read: run this in a terminal, or give --answers")
        return typed.strip() or default


QUESTIONS: dict[str, tuple[str, str]] = {
    "PUBLIC_URL": (
        "The address people will open in their browser",
        "It must be https, and its name must point at this machine.",
    ),
    "OIDC_ISSUER": (
        "Your identity provider's issuer address",
        "The address your sign-in system (Entra ID, Okta, Keycloak...) gives for OpenID Connect.",
    ),
    "OIDC_CLIENT_ID": (
        "The client id your identity provider gave Mendari",
        "Register Mendari there as a web application first; the address to give it is below.",
    ),
    "OIDC_CLIENT_SECRET": (
        "Its client secret",
        "Typed hidden. It is written to the settings file and nowhere else.",
    ),
    "ADMIN_EMAIL": (
        "The first administrator's sign-in address",
        "The person who signs in first with this address becomes the administrator. Once.",
    ),
    "ORGANIZATION": (
        "What your organization is called",
        "Shown in the console and in invitations.",
    ),
    "PATIENT_DATA": (
        "Keep each case's input, encrypted, so reviewers can read it here? (yes/no)",
        "Yes: inputs are sealed with a key made for this deployment, read only by reviewers "
        "and administrators, each read recorded, and deleted after 30 days unless you set "
        "another retention. No: no input is ever stored, only a fingerprint; reviewers cannot "
        "read a case here. This is chosen once.",
    ),
}


def _existing_answers(settings: Mapping[str, str]) -> dict[str, str]:
    """The answers a settings file already holds, as the questions' defaults."""
    found = {
        "PUBLIC_URL": settings.get("MENDARI_PUBLIC_URL", ""),
        "OIDC_ISSUER": settings.get("MENDARI_OIDC_ISSUER", ""),
        "OIDC_CLIENT_ID": settings.get("MENDARI_OIDC_CLIENT_ID", ""),
        "OIDC_CLIENT_SECRET": settings.get("MENDARI_OIDC_CLIENT_SECRET", ""),
        "ADMIN_EMAIL": settings.get("MENDARI_BOOTSTRAP_ADMIN", ""),
        "ORGANIZATION": settings.get("MENDARI_BOOTSTRAP_ORG_NAME", ""),
        "TIMEZONE": settings.get("MENDARI_TIMEZONE", ""),
        "HTTPS_PORT": settings.get("SITE_HTTPS_PORT", ""),
        "HTTP_PORT": settings.get("SITE_HTTP_PORT", ""),
        "REGISTRY_URL": settings.get("MENDARI_REGISTRY_URL", ""),
        "ALERT_WEBHOOK": settings.get("MENDARI_ALERT_WEBHOOK", ""),
        "RETENTION_DAYS": settings.get("MENDARI_RETENTION_DAYS", ""),
        "BACKUP_KEEP_DAYS": settings.get("BACKUP_KEEP_DAYS", ""),
    }
    mode = settings.get("MENDARI_PHI_MODE", "")
    if mode:
        found["PATIENT_DATA"] = "yes" if mode == "encrypt" else "no"
    return {name: value for name, value in found.items() if value}


CHECKS: dict[str, Callable[[str], Any]] = {
    "PUBLIC_URL": public_address,
    "ADMIN_EMAIL": _email,
    "ORGANIZATION": _organization,
    "PATIENT_DATA": lambda text: _yes(text, "keeping each case's input"),
    "TIMEZONE": _timezone,
}


def _required(name: str, text: str) -> str:
    if not text.strip():
        raise Refused(f"{QUESTIONS[name][0].lower()} is needed")
    return text.strip()


def gather(
    console: Console, given: Mapping[str, str], existing: Mapping[str, str]
) -> dict[str, str]:
    """Each answer, checked: from the answers file, else asked (with what the settings file
    holds as its default), else what the settings file holds. A wrong answer is asked again at
    a terminal, and stops an unattended run."""
    unknown = sorted(set(given) - set(ANSWERS) - set(OPTIONAL))
    if unknown:
        raise Refused(
            f"the answers file names {', '.join(unknown)}, which is not an answer: one of "
            f"{', '.join((*ANSWERS, *OPTIONAL))}"
        )
    before = _existing_answers(existing)
    answers = {name: given[name] for name in OPTIONAL if given.get(name)}
    answers = {**{k: v for k, v in before.items() if k in OPTIONAL}, **answers}
    for name in ANSWERS:
        prompt, about = QUESTIONS[name]
        fixed = name == "PATIENT_DATA" and name in before
        if fixed:
            # Chosen once. A different answer now would leave sealed inputs nobody can read.
            if name in given and _yes(given[name], "PATIENT_DATA") != _yes(before[name], name):
                raise Refused(
                    "patient data mode was chosen when this deployment was installed and is "
                    "not changed by installing again: docs/deploying-with-phi.md (section 3) "
                    "says what changing it means"
                )
            answers[name] = before[name]
            continue
        check = CHECKS.get(name, lambda text, name=name: _required(name, text))
        if name in given or not console.interactive:
            value = given.get(name, before.get(name, ""))
            _required(name, value)
            check(value)
            answers[name] = value.strip()
            continue
        console.say()
        console.say(about)
        while True:
            typed = console.ask(prompt, before.get(name, ""), secret=name == "OIDC_CLIENT_SECRET")
            try:
                _required(name, typed)
                check(typed)
            except Refused as refused:
                console.say(f"  {refused}")
                continue
            answers[name] = typed.strip()
            break
    return answers


# -- the certificate ------------------------------------------------------------------------------


def self_signed(host: str) -> tuple[bytes, bytes]:
    """A certificate for `host` signed by its own key (an EC P-256 key, 90 days): its PEM and
    its key's. For a trial on a private network only: browsers warn about it, and it proves
    nothing about who answers."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, host[:64]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, SELF_SIGNED_ORG),
        ]
    )
    try:
        alternative: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        alternative = x509.DNSName(host)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=SELF_SIGNED_DAYS))
        .add_extension(x509.SubjectAlternativeName([alternative]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


@dataclass(frozen=True)
class Certificate:
    """The certificate in `tls/` as the summary tells of it."""

    self_signed: bool
    expires: datetime
    names: tuple[str, ...]


def certificate(directory: Path) -> Certificate | None:
    """The certificate the front will serve, read and checked against its key; None when
    `tls/` holds neither file. One without the other, a file that is not a certificate or a
    key, a key that is not the certificate's or a certificate past its date stops the install:
    the front would not start."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    chain, key_file = directory / TLS_DIR / CERTIFICATE, directory / TLS_DIR / PRIVATE_KEY
    if not chain.exists() and not key_file.exists():
        return None
    if not chain.exists() or not key_file.exists():
        raise Refused(
            f"{TLS_DIR}/ holds one of {CERTIFICATE} and {PRIVATE_KEY}: the front needs both, the "
            "certificate (with its chain) and its private key"
        )
    try:
        found = x509.load_pem_x509_certificates(chain.read_bytes())
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as error:
        raise Refused(
            f"{TLS_DIR}/{CERTIFICATE} and {TLS_DIR}/{PRIVATE_KEY} must be a PEM certificate "
            f"and its PEM private key, without a passphrase ({type(error).__name__})"
        ) from None
    leaf = found[0]
    public = serialization.PublicFormat.SubjectPublicKeyInfo
    encoding = serialization.Encoding.DER
    if leaf.public_key().public_bytes(encoding, public) != key.public_key().public_bytes(
        encoding, public
    ):
        raise Refused(f"{TLS_DIR}/{PRIVATE_KEY} is not the key of {TLS_DIR}/{CERTIFICATE}")
    expires = leaf.not_valid_after_utc
    if expires <= datetime.now(UTC):
        raise Refused(f"the certificate in {TLS_DIR}/ expired on {expires:%Y-%m-%d}")
    try:
        extension = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names = tuple(str(name.value) for name in extension.value)
    except x509.ExtensionNotFound:
        names = ()
    return Certificate(leaf.issuer == leaf.subject, expires, names)


def no_certificate(host: str) -> Refused:
    """What stops an install whose `tls/` is empty when no self-signed certificate was asked
    for: never plain http instead."""
    return Refused(
        f"no certificate: put the certificate for {host} (with its chain) in "
        f"{TLS_DIR}/{CERTIFICATE} and its private key in {TLS_DIR}/{PRIVATE_KEY}, then run "
        "this again. For a trial on a private network only, run it with --self-signed: "
        "browsers will warn about that certificate"
    )


def make_self_signed(directory: Path, host: str) -> Certificate:
    """A self-signed certificate for `host` in an empty `tls/`, with a note that says what it
    is. Never over a file that is there."""
    if certificate(directory) is not None:
        raise Refused(f"{TLS_DIR}/ holds a certificate already: it is never written over")
    chain, key = self_signed(host)
    folder = directory / TLS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    write_private(folder / PRIVATE_KEY, key.decode())
    write_private(folder / CERTIFICATE, chain.decode(), 0o644)
    write_private(
        folder / SELF_SIGNED_NOTE,
        f"The certificate in this folder was made by the installer on {datetime.now(UTC):%Y-%m-%d}"
        f" for {host}. It is signed by its own key: browsers warn about it, and it proves\n"
        "nothing about who answers. It is for a trial on a private network only.\n\n"
        f"Before anyone else uses this deployment, replace {CERTIFICATE} and {PRIVATE_KEY} with\n"
        "your organization's certificate and key for that name, delete this note, and run\n"
        "`mendari-site start`.\n",
        0o644,
    )
    made = certificate(directory)
    assert made is not None
    return made


# -- building the server on the settings ----------------------------------------------------------


@contextmanager
def _environment(values: Mapping[str, str]) -> Iterator[None]:
    """The process's environment with `values` in it, and as it was afterwards."""
    before = dict(os.environ)
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


def starts(database: str = ":memory:") -> None:
    """Build the server as `mendari serve` does, on `database` (an empty in-memory one unless
    given), with the environment as it is. Every setting is read and checked as at start-up, so
    whatever would stop the server raises here, with its message."""
    from mendari.sandbox import example_datasets_dir
    from mendari.server.app import create_app
    from mendari.settings import daily_limits, default_providers, parse_providers

    app = create_app(
        db_path=database,
        providers=parse_providers(None),
        default_providers=default_providers(),
        budget_limits=daily_limits(),
        datasets_dir=example_datasets_dir(),
    )
    registry = getattr(app.state, "registry_store", None)
    if registry is not None:
        registry.close()


def _outbound(values: Mapping[str, str]) -> None:
    """The two addresses an answer may give that the server only checks when it uses them: the
    alert webhook and the registry. Refused here, by the server's own rules."""
    from mendari.alerts import check_webhook
    from mendari.core.https_only import is_https_or_local

    webhook = values.get("MENDARI_ALERT_WEBHOOK", "")
    if webhook:
        try:
            check_webhook(webhook)
        except MendariError as error:
            raise Refused(str(error)) from None
    registry = values.get("MENDARI_REGISTRY_URL", "")
    if registry and not is_https_or_local(registry):
        raise Refused("the registry's address is https, without a user or a password in it")


def reachable(issuer: str) -> bool:
    """Whether the identity provider's OpenID Connect configuration answers from here."""
    import httpx

    try:
        answer = httpx.get(
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
            timeout=8,
            follow_redirects=False,
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        return False
    return answer.status_code == 200


# -- the commands ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Made:
    """What `settings` did: the file's values, the secrets it generated now (by name), and the
    certificate."""

    values: dict[str, str]
    generated: tuple[str, ...]
    certificate: Certificate


def settings(
    directory: Path,
    *,
    given: Mapping[str, str] | None = None,
    console: Console | None = None,
    image: str | None = None,
    timezone: str | None = None,
    owner: tuple[int, int] | None = None,
    make_certificate: bool = False,
    probe: Callable[[str], bool] | None = None,
) -> Made:
    """Ask, generate, check and write this deployment's settings into `directory` (see the
    module's docstring). `given` are an answers file's; `image` the engine image to run (kept
    from the file when not given); `timezone` the machine's, used unless an answer names one;
    `owner` the uid and gid that own the backups; `make_certificate` asks for a self-signed
    certificate when `tls/` has none; `probe` says whether the issuer answers (the command
    passes `reachable`; None: nothing is asked of the network). Nothing is written unless the
    server builds on the result."""
    console = console or Console(interactive=False)
    path = directory / SETTINGS
    existing, own = read_settings(path)
    # The secrets are made together, once. A file that holds some and not the others lost a
    # line: a new database password would not open the database, nor a new key what the old
    # one sealed. Nothing is made in its place.
    lost = [name for name in GENERATED if not existing.get(name)]
    if lost and len(lost) < len(GENERATED):
        raise Refused(
            f"{SETTINGS} no longer holds {', '.join(lost)}, generated when this deployment was "
            "installed and never made again: a new one would not open what the old one "
            "protects. Put the line back from the copy you keep of this file"
        )
    given = dict(given or {})
    answers = gather(console, given, existing)
    address = public_address(answers["PUBLIC_URL"])
    keep = _yes(answers["PATIENT_DATA"], "PATIENT_DATA")
    zone = _zone(console, answers, timezone)
    chosen_image = image or existing.get("MENDARI_IMAGE", "")
    if not chosen_image:
        raise Refused("no engine image was named: run this through `mendari-site install`")
    https_port = _whole(answers.get("HTTPS_PORT") or str(address.port), "HTTPS_PORT", 1, 65535)
    http_port = _whole(answers.get("HTTP_PORT") or "80", "HTTP_PORT", 1, 65535)
    retention = _whole(
        answers.get("RETENTION_DAYS") or str(RETENTION_DAYS), "RETENTION_DAYS", 1, 36500
    )
    kept = _whole(
        answers.get("BACKUP_KEEP_DAYS") or str(BACKUP_KEEP_DAYS), "BACKUP_KEEP_DAYS", 1, 3650
    )
    uid, gid = owner or (
        int(existing.get("SITE_UID") or os.getuid()),
        int(existing.get("SITE_GID") or os.getgid()),
    )
    generated = tuple(name for name in GENERATED if not existing.get(name))
    values: dict[str, str] = {
        "MENDARI_IMAGE": chosen_image,
        "MENDARI_PUBLIC_URL": address.url,
        "SITE_HOST": address.host,
        "SITE_HTTPS_PORT": str(https_port),
        "SITE_HTTP_PORT": str(http_port),
        "SITE_UID": str(uid),
        "SITE_GID": str(gid),
        "MENDARI_OIDC_ISSUER": answers["OIDC_ISSUER"].rstrip("/"),
        "MENDARI_OIDC_CLIENT_ID": answers["OIDC_CLIENT_ID"],
        "MENDARI_OIDC_CLIENT_SECRET": answers["OIDC_CLIENT_SECRET"],
        "MENDARI_ACCOUNTS": existing.get("MENDARI_ACCOUNTS") or "memberships",
        "MENDARI_OPERATOR_ORG": existing.get("MENDARI_OPERATOR_ORG") or ORGANIZATION,
        "MENDARI_BOOTSTRAP_ADMIN": _email(answers["ADMIN_EMAIL"]),
        "MENDARI_BOOTSTRAP_ORG_NAME": _organization(answers["ORGANIZATION"]),
        "MENDARI_PHI_MODE": "encrypt" if keep else "hash",
        "MENDARI_RETENTION_DAYS": str(retention),
        "BACKUP_KEEP_DAYS": str(kept),
        "MENDARI_ALERT_WEBHOOK": answers.get("ALERT_WEBHOOK", ""),
        "MENDARI_TIMEZONE": zone,
        "MENDARI_RATE_LIMITS": existing.get("MENDARI_RATE_LIMITS") or "on",
        "MENDARI_ENVIRONMENT": existing.get("MENDARI_ENVIRONMENT") or "production",
        # A list that names no origin: no remote service is called until the site lists one.
        "MENDARI_PROVIDER_ALLOWED_ORIGINS": existing.get(
            "MENDARI_PROVIDER_ALLOWED_ORIGINS", NONE_LISTED
        ),
        "MENDARI_FHIR_ALLOWED_SERVERS": existing.get("MENDARI_FHIR_ALLOWED_SERVERS", ""),
        "MENDARI_REGISTRY_URL": answers.get("REGISTRY_URL", "").rstrip("/"),
        "MENDARI_REGISTRY_COUNTERSIGNATURE": existing.get("MENDARI_REGISTRY_COUNTERSIGNATURE")
        or "required",
        "MENDARI_CACHE_DIR": existing.get("MENDARI_CACHE_DIR") or STATE_CACHE,
        "MENDARI_CONFIG_DIR": existing.get("MENDARI_CONFIG_DIR") or STATE_CONFIG,
    }
    _outbound(values)
    if existing.get("MENDARI_IMAGE_PREVIOUS"):
        values["MENDARI_IMAGE_PREVIOUS"] = existing["MENDARI_IMAGE_PREVIOUS"]
    for name, (_, make) in GENERATED.items():
        values[name] = existing.get(name) or make()
    # The site's own certificate, checked; without one, a self-signed one only when asked for.
    found = certificate(directory)
    if found is None and not make_certificate:
        raise no_certificate(address.host)
    # The server's own start-up, on these settings and an empty database: the last word.
    with _environment({**parse("\n".join(own)), **values}):
        try:
            starts()
        except MendariError as error:
            raise Refused(f"the server would not start on these answers: {error}") from None
    # Only now is anything written: the trial certificate if one is needed, then the settings.
    if found is None:
        found = make_self_signed(directory, address.host)
    write_private(path, render(values, own))
    if probe is not None and not probe(values["MENDARI_OIDC_ISSUER"]):
        console.say()
        console.say(
            "Note: your identity provider's configuration could not be read from this machine "
            f"just now ({values['MENDARI_OIDC_ISSUER']}/.well-known/openid-configuration). "
            "Signing in will not work until it answers from here."
        )
    return Made(values, generated, found)


def summary(directory: Path, console: Console, *, shown: str | None = None) -> None:
    """What the install made and where, and what to do next. Never a secret, a key or the
    alert webhook's address: they are in the settings file, which this names."""
    values, _ = read_settings(directory / SETTINGS)
    if not values:
        raise Refused(f"no {SETTINGS} here yet: run `mendari-site install`")
    where = shown or str(directory)
    found = certificate(directory)
    say = console.say
    say()
    say(f"Mendari is set up in {where}")
    say()
    say(f"  Open                 {values.get('MENDARI_PUBLIC_URL', '')}")
    say("  At your identity provider, register this redirect address for the client:")
    say(f"                       {values.get('MENDARI_PUBLIC_URL', '')}/auth/callback")
    say(f"  First administrator  {values.get('MENDARI_BOOTSTRAP_ADMIN', '')}")
    say("                       The first sign-in with this address, verified by your identity")
    say("                       provider, becomes the administrator. It works once.")
    kept = values.get("MENDARI_PHI_MODE") == "encrypt"
    say(
        "  Patient data mode    "
        + (
            f"on: inputs kept encrypted, deleted after {values.get('MENDARI_RETENTION_DAYS')} days"
            if kept
            else "no input is kept, only a fingerprint"
        )
    )
    say(f"  Time zone            {values.get('MENDARI_TIMEZONE', '')}")
    say()
    say("  What was made")
    say(f"    {where}/{SETTINGS}   every setting, readable by its owner only. It holds the")
    say("      generated secrets:")
    for name, (about, _) in GENERATED.items():
        if values.get(name):
            say(f"        {name}: {about}")
    say("      Keep a copy of this file where your organization keeps its secrets.")
    if found is not None:
        kind = (
            "SELF-SIGNED: a trial on a private network only. Browsers will warn. Replace it "
            "with your organization's certificate before anyone else uses this."
            if found.self_signed
            else "your certificate"
        )
        say(f"    {where}/{TLS_DIR}/   {kind}")
        say(f"      It expires on {found.expires:%Y-%m-%d}.")
    say(f"    {where}/backups/   a backup every night, kept {values.get('BACKUP_KEEP_DAYS')} days.")
    say("      Copy them off this machine: a backup on the same disk does not survive the disk.")
    say()
    say("  Next: open the address, sign in as the first administrator, and follow")
    say('  "Finish setting up". The System view shows what is ready and what is not.')


def set_image(directory: Path, image: str | None, *, back: bool = False) -> tuple[str, str]:
    """Record the engine image to run, keeping the one before for a rollback: (now, before).
    `back` swaps the two. Nothing else in the settings file changes."""
    path = directory / SETTINGS
    values, own = read_settings(path)
    if not values:
        raise Refused(f"no {SETTINGS} here yet: run `mendari-site install`")
    current, previous = values.get("MENDARI_IMAGE", ""), values.get("MENDARI_IMAGE_PREVIOUS", "")
    if back:
        if not previous:
            raise Refused("there is no earlier image to go back to: nothing was upgraded here")
        image = previous
    if not image:
        raise Refused("name the image to run")
    if image == current:
        return current, previous
    values["MENDARI_IMAGE"], values["MENDARI_IMAGE_PREVIOUS"] = image, current
    write_private(path, render(values, own))
    return image, current


def migrate() -> None:
    """Open the deployment's database as the server does at start-up, which brings it up to
    date under the schema's lock, and close it. A migration that refuses leaves the database
    as it was and raises, so the engine is never started on it."""
    target = os.environ.get("DATABASE_URL", "").strip()
    if not target:
        raise Refused("DATABASE_URL is not set: there is no database to bring up to date")
    starts(target)


def _owner(text: str) -> tuple[int, int]:
    uid, _, gid = text.partition(":")
    if not (uid.isdigit() and gid.isdigit()):
        raise Refused("--owner is <uid>:<gid>")
    return int(uid), int(gid)


# What each command says when it stops on an error that is not Mendari's own to word.
_UNDONE = {
    "settings": "the settings could not be made, and the settings file is as it was",
    "summary": "the summary could not be shown",
    "check": "the server could not be built on these settings",
    "migrate": "the database could not be opened or brought up to date: it was left as it was, "
    "and the engine is not started on it",
    "image": "the image could not be recorded, and the settings file is as it was",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mendari.install",
        description="The one-machine install's settings: made and checked inside the engine's "
        "image. Run it through deploy/site/mendari-site.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    made = commands.add_parser("settings", help="ask, generate and write the settings")
    made.add_argument("--dir", required=True, help="the install's folder")
    made.add_argument(
        "--answers", help="a file of NAME=value answers, for an unattended run (-: standard input)"
    )
    made.add_argument("--image", help="the engine image to run")
    made.add_argument("--timezone", help="this machine's time zone, used unless an answer says")
    made.add_argument("--owner", help="<uid>:<gid> that owns the backups")
    made.add_argument("--self-signed", action="store_true", help="make a trial certificate")
    made.add_argument("--shown-dir", help="the folder as the person knows it")
    told = commands.add_parser("summary", help="what was made and where")
    told.add_argument("--dir", required=True)
    told.add_argument("--shown-dir")
    commands.add_parser("check", help="build the server on the environment as it is")
    commands.add_parser("migrate", help="bring the deployment's database up to date")
    pinned = commands.add_parser("image", help="record the engine image to run")
    pinned.add_argument("--dir", required=True)
    pinned.add_argument("--image")
    pinned.add_argument("--back", action="store_true", help="go back to the image before")
    args = parser.parse_args(argv)
    try:
        if args.command == "settings":
            given = None
            if args.answers:
                try:
                    given = parse(
                        sys.stdin.read()
                        if args.answers == "-"
                        else Path(args.answers).read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError) as error:
                    raise Refused(
                        f"the answers file cannot be read ({type(error).__name__})"
                    ) from None
            console = Console(interactive=given is None and sys.stdin.isatty())
            if given is None and not console.interactive:
                raise Refused("run this in a terminal, or give --answers with a file of answers")
            directory = Path(args.dir)
            written = settings(
                directory,
                given=given,
                console=console,
                image=args.image,
                timezone=args.timezone,
                owner=_owner(args.owner) if args.owner else None,
                make_certificate=args.self_signed
                or bool(given and _yes(given.get("SELF_SIGNED") or "no", "SELF_SIGNED")),
                probe=reachable,
            )
            where = f"{args.shown_dir or directory}/{SETTINGS}"
            console.say()
            if written.generated:
                console.say(
                    f"The settings are written: {where}. Generated now, and never again: "
                    f"{', '.join(written.generated)}."
                )
            else:
                console.say(
                    f"The settings are written: {where}. No secret was generated: the "
                    f"{len(GENERATED)} this deployment has are kept as they were."
                )
        elif args.command == "summary":
            summary(Path(args.dir), Console(interactive=False), shown=args.shown_dir)
        elif args.command == "check":
            starts()
            print(f"these settings start Mendari {__version__}")
        elif args.command == "migrate":
            migrate()
            print(f"the database is up to date for Mendari {__version__}")
        else:
            now, before = set_image(Path(args.dir), args.image, back=args.back)
            print(f"image: {now}" + (f" (before: {before})" if before else ""))
    except MendariError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as error:
        # Never a traceback, and never a library's own text (it can hold a connection string):
        # what was being done, and the kind of error. MENDARI_DEBUG=1 lets it through whole.
        if os.environ.get("MENDARI_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
            raise
        print(
            f"error: {_UNDONE[args.command]} ({type(error).__name__}). Set MENDARI_DEBUG=1 to "
            "see the whole error.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
