# SPDX-License-Identifier: Apache-2.0
"""The one-install path's settings (`mendari.install`, which `deploy/site/mendari-site` runs
inside the engine's image):

- **Run twice, made once.** Each secret is generated at the first run, from the system's random
  source, and never again: a second run writes the same file, byte for byte, and a changed
  answer changes that answer alone. A person's own lines are kept.
- **Fail closed.** A wrong answer, an answer the server itself would refuse, a settings file
  that cannot be read, a missing certificate: each is one plain line, and nothing is written.
- **Nothing secret is printed**: not at the terminal, not in the summary.
- **The settings start a server that passes its own checks**, and that server admits the first
  administrator the install named, once.
- The certificate is the site's own, or a self-signed one only when asked for, marked as one.
- Patient data mode is chosen once.
"""

from __future__ import annotations

import base64
import io
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, install
from mendari.core.phi import phi_policy
from mendari.errors import MendariError
from mendari.server import auth as auth_module
from mendari.server.app import create_app
from mendari.standards.fhir_client import GuardedHttp
from tests.conftest import DASHBOARD, ROOT, member
from tests.test_auth import CLIENT, IDP, FakeIdp, sign_in

PUBLIC = "https://mendari.stmarys.example"
ADA = "ada@stmarys.example"
IMAGE = "ghcr.io/mendari-hq/mendari-engine:v0.1.0@sha256:" + "a" * 64
# Each answer a site gives, the client's secret with a mark to search every output for.
CLIENT_SECRET = "zq-client-secret-0f3a"
ANSWERS = {
    "PUBLIC_URL": PUBLIC,
    "OIDC_ISSUER": IDP,
    "OIDC_CLIENT_ID": CLIENT,
    "OIDC_CLIENT_SECRET": CLIENT_SECRET,
    "ADMIN_EMAIL": "Ada@StMarys.Example",
    "ORGANIZATION": "St Mary's Hospital",
    "PATIENT_DATA": "yes",
}
WEBHOOK = "https://hooks.stmarys.example/services/zq-webhook-token-7c1d"


def run(directory: Path, answers: dict[str, str] | None = None, **options: Any) -> install.Made:
    """The installer's `settings`, unattended, as `mendari-site install --answers` runs it."""
    options.setdefault("console", install.Console(interactive=False, out=io.StringIO()))
    options.setdefault("image", IMAGE)
    options.setdefault("timezone", "Europe/Paris")
    options.setdefault("owner", (1000, 1000))
    options.setdefault("make_certificate", True)
    options.setdefault("probe", None)
    return install.settings(directory, given=ANSWERS if answers is None else answers, **options)


def settings_file(directory: Path) -> str:
    return (directory / install.SETTINGS).read_text(encoding="utf-8")


def nothing_written(directory: Path) -> bool:
    return not (directory / install.SETTINGS).exists()


# -- run twice, made once -------------------------------------------------------------------------


def test_installing_twice_generates_each_secret_once_and_writes_the_same_settings(
    tmp_path: Path,
) -> None:
    first = run(tmp_path)
    assert first.generated == tuple(install.GENERATED)
    written = settings_file(tmp_path)
    path = tmp_path / install.SETTINGS
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # its owner's alone
    secrets = {name: first.values[name] for name in install.GENERATED}
    assert len(set(secrets.values())) == len(secrets)  # each its own
    for name in ("MENDARI_PHI_KEY", "MENDARI_INPUT_HASH_KEY", "MENDARI_CHECKPOINT_KEY"):
        assert len(base64.b64decode(secrets[name], validate=True)) == 32, name
    assert len(secrets["POSTGRES_PASSWORD"]) == 48 and len(secrets["CRON_SECRET"]) >= 32
    assert len(secrets["MENDARI_ALERT_SECRET"]) >= 32
    certificate = (tmp_path / "tls" / "fullchain.pem").read_bytes()
    key = (tmp_path / "tls" / "privkey.pem").read_bytes()

    again = run(tmp_path)
    assert again.generated == ()  # nothing is made a second time
    assert settings_file(tmp_path) == written  # the same file, byte for byte
    assert (tmp_path / "tls" / "fullchain.pem").read_bytes() == certificate
    assert (tmp_path / "tls" / "privkey.pem").read_bytes() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # A changed answer changes that answer, and no secret.
    changed = run(tmp_path, {**ANSWERS, "ADMIN_EMAIL": "bo@stmarys.example"})
    assert changed.generated == ()
    assert {name: changed.values[name] for name in install.GENERATED} == secrets
    before, after = install.parse(written), install.parse(settings_file(tmp_path))
    assert {name for name in after if after[name] != before.get(name)} == {
        "MENDARI_BOOTSTRAP_ADMIN"
    }
    # Another machine's install makes its own secrets.
    other = run(tmp_path / "other")
    assert not set(secrets.values()) & {other.values[name] for name in install.GENERATED}


def test_the_settings_are_the_safe_defaults(tmp_path: Path) -> None:
    values = run(tmp_path, {**ANSWERS, "ALERT_WEBHOOK": WEBHOOK}).values
    assert values["MENDARI_IMAGE"] == IMAGE
    assert (values["MENDARI_PUBLIC_URL"], values["SITE_HOST"]) == (
        PUBLIC,
        "mendari.stmarys.example",
    )
    assert (values["SITE_HTTPS_PORT"], values["SITE_HTTP_PORT"]) == ("443", "80")
    assert (values["SITE_UID"], values["SITE_GID"]) == ("1000", "1000")
    assert values["MENDARI_ACCOUNTS"] == "memberships"
    assert values["MENDARI_OPERATOR_ORG"] == install.ORGANIZATION == "default"
    assert values["MENDARI_BOOTSTRAP_ADMIN"] == "ada@stmarys.example"
    assert values["MENDARI_BOOTSTRAP_ORG_NAME"] == "St Mary's Hospital"
    assert values["MENDARI_PHI_MODE"] == "encrypt" and values["MENDARI_RETENTION_DAYS"] == "30"
    assert values["MENDARI_RATE_LIMITS"] == "on"
    assert values["MENDARI_TIMEZONE"] == "Europe/Paris"
    assert values["MENDARI_REGISTRY_COUNTERSIGNATURE"] == "required"
    assert values["MENDARI_ALERT_WEBHOOK"] == WEBHOOK
    # Nothing leaves the network until the site lists it: a list that names no origin, and
    # an empty FHIR allow-list.
    assert values["MENDARI_PROVIDER_ALLOWED_ORIGINS"] == install.NONE_LISTED
    assert values["MENDARI_FHIR_ALLOWED_SERVERS"] == ""
    # What the engine pulls is kept in the template's state volume, across upgrades.
    assert values["MENDARI_CACHE_DIR"] == "/data/cache"
    assert values["MENDARI_CONFIG_DIR"] == "/data/config"
    assert values["BACKUP_KEEP_DAYS"] == "14"
    # Every setting the file holds is one the installer knows how to write.
    assert set(values) <= set(install.MANAGED)
    listed = [name for _, names in install.SECTIONS for name in names]
    assert sorted(listed) == sorted(install.MANAGED) and len(set(listed)) == len(listed)
    # Without patient data mode: no input is kept, and the keys are there all the same (the
    # fingerprints are keyed).
    hashed = run(tmp_path / "hash", {**ANSWERS, "PATIENT_DATA": "no"}).values
    assert hashed["MENDARI_PHI_MODE"] == "hash" and hashed["MENDARI_INPUT_HASH_KEY"]
    # Another port in the public address is the port the front listens on.
    ported = run(tmp_path / "port", {**ANSWERS, "PUBLIC_URL": "https://10.20.30.40:8443/"}).values
    assert ported["MENDARI_PUBLIC_URL"] == "https://10.20.30.40:8443"
    assert (ported["SITE_HOST"], ported["SITE_HTTPS_PORT"]) == ("10.20.30.40", "8443")


def test_a_persons_own_lines_and_tuned_settings_are_kept(tmp_path: Path) -> None:
    run(tmp_path)
    path = tmp_path / install.SETTINGS
    text = settings_file(tmp_path)
    tuned = text.replace(
        f"MENDARI_PROVIDER_ALLOWED_ORIGINS={install.NONE_LISTED}",
        "MENDARI_PROVIDER_ALLOWED_ORIGINS=https://agents.stmarys.example",
    ).replace("MENDARI_ACCOUNTS=memberships", "MENDARI_ACCOUNTS=memberships\nSITE_ENGINE_MEMORY=2g")
    assert tuned != text
    own = "# The agent service the informatics team runs.\nMENDARI_AGENT_URL=https://agents.stmarys.example/v1\n"
    path.write_text(tuned + f"\n# {install.OWN}\n" + own, encoding="utf-8")

    run(tmp_path)
    kept = settings_file(tmp_path)
    values = install.parse(kept)
    assert values["MENDARI_PROVIDER_ALLOWED_ORIGINS"] == "https://agents.stmarys.example"
    assert values["SITE_ENGINE_MEMORY"] == "2g"  # a line added among the installer's own
    assert values["MENDARI_AGENT_URL"] == "https://agents.stmarys.example/v1"
    assert "# The agent service the informatics team runs." in kept
    assert kept.count(f"# {install.OWN}") == 1
    run(tmp_path)
    assert settings_file(tmp_path) == kept  # and a third run writes the same again
    # A person's own line the server refuses stops the install, and the file stays.
    path.write_text(kept + "MENDARI_ORG_DAILY_LIMITS=decisions=many\n", encoding="utf-8")
    refused = path.read_text(encoding="utf-8")
    with pytest.raises(install.Refused, match="MENDARI_ORG_DAILY_LIMITS"):
        run(tmp_path)
    assert path.read_text(encoding="utf-8") == refused


# -- fail closed ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answers", "said"),
    [
        ({"PUBLIC_URL": "http://mendari.stmarys.example"}, "never served over plain http"),
        ({"PUBLIC_URL": "https://mendari.stmarys.example/mendari"}, "nothing after it"),
        ({"PUBLIC_URL": "https://ada:pw@mendari.stmarys.example"}, "nothing after it"),
        ({"PUBLIC_URL": "https://[::1]"}, "DNS name or an IPv4 address"),
        ({"PUBLIC_URL": "mendari.stmarys.example"}, "starts with https://"),
        ({"ADMIN_EMAIL": "ada"}, "administrator's sign-in address"),
        ({"ADMIN_EMAIL": ""}, "is needed"),
        ({"OIDC_CLIENT_SECRET": " "}, "is needed"),
        ({"ORGANIZATION": "x" * 201}, "1 to 200 characters"),
        ({"PATIENT_DATA": "maybe"}, "answer yes or no"),
        ({"TIMEZONE": "Mars/Olympus"}, "not a time zone"),
        ({"HTTPS_PORT": "0"}, "HTTPS_PORT is a whole number"),
        ({"RETENTION_DAYS": "forever"}, "RETENTION_DAYS is a whole number"),
        ({"BACKUP_KEEP_DAYS": "-1"}, "BACKUP_KEEP_DAYS is a whole number"),
        ({"ALERT_WEBHOOK": "http://hooks.stmarys.example/x"}, "https"),
        ({"REGISTRY_URL": "http://registry.stmarys.example"}, "registry's address is https"),
        ({"ADMIN_PASSWORD": "x"}, "ADMIN_PASSWORD, which is not an answer"),
        # What only the server knows to refuse, said with its own message.
        ({"OIDC_ISSUER": "http://idp.stmarys.example"}, "would not start on these answers"),
    ],
)
def test_a_wrong_answer_is_refused_and_nothing_is_written(
    tmp_path: Path, answers: dict[str, str], said: str
) -> None:
    with pytest.raises(install.Refused, match=said) as refused:
        run(tmp_path, {**ANSWERS, **answers})
    assert "\n" not in str(refused.value)  # one plain line
    assert CLIENT_SECRET not in str(refused.value)
    assert nothing_written(tmp_path)
    assert list(tmp_path.iterdir()) == []  # not the settings, and no certificate either


def test_a_missing_answer_stops_an_unattended_run(tmp_path: Path) -> None:
    for name in install.ANSWERS:
        answers = {key: value for key, value in ANSWERS.items() if key != name}
        with pytest.raises(install.Refused, match="is needed"):
            run(tmp_path, answers)
    assert nothing_written(tmp_path)


def test_a_settings_file_that_cannot_be_read_stops_the_install(tmp_path: Path) -> None:
    """Its secrets are never made again over it: a new database password, or a new sealing key,
    would lock the deployment out of its own data."""
    run(tmp_path)
    path = tmp_path / install.SETTINGS
    written = path.read_bytes()
    path.write_bytes(b"\xff\xfe not text")
    with pytest.raises(install.Refused, match="cannot be read"):
        run(tmp_path)
    assert path.read_bytes() == b"\xff\xfe not text"
    path.write_bytes(written)
    if os.geteuid() != 0:  # root reads any file
        path.chmod(0)
        try:
            with pytest.raises(install.Refused, match="cannot be read"):
                run(tmp_path)
            with pytest.raises(install.Refused, match="cannot be read"):
                install.set_image(tmp_path, IMAGE)
        finally:
            path.chmod(0o600)
    assert path.read_bytes() == written


def test_a_run_cut_short_leaves_the_file_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path)
    written = settings_file(tmp_path)

    def cut(*_: Any) -> None:
        raise OSError("the disk is full")

    monkeypatch.setattr(install.os, "replace", cut)
    with pytest.raises(OSError, match="disk is full"):
        run(tmp_path, {**ANSWERS, "ADMIN_EMAIL": "bo@stmarys.example"})
    assert settings_file(tmp_path) == written
    assert sorted(p.name for p in tmp_path.iterdir()) == [install.SETTINGS, "tls"]  # no stray file


@pytest.mark.parametrize("lost", list(install.GENERATED))
def test_a_secret_that_was_lost_is_never_made_again(tmp_path: Path, lost: str) -> None:
    """A new database password would not open the database, nor a new key what the old one
    sealed: a settings file that lost one of its secrets stops the install, as it is."""
    made = run(tmp_path)
    path = tmp_path / install.SETTINGS
    damaged = "".join(
        line
        for line in settings_file(tmp_path).splitlines(keepends=True)
        if not line.startswith(f"{lost}=")
    )
    path.write_text(damaged, encoding="utf-8")
    with pytest.raises(install.Refused, match=f"no longer holds {lost}, generated when"):
        run(tmp_path)
    assert settings_file(tmp_path) == damaged
    # Emptied is lost too.
    emptied = damaged + f"{lost}=\n"
    path.write_text(emptied, encoding="utf-8")
    with pytest.raises(install.Refused, match=f"no longer holds {lost}"):
        run(tmp_path)
    # Put back from the copy the site keeps, the install goes on, with the same secrets.
    path.write_text(damaged + f"{lost}={made.values[lost]}\n", encoding="utf-8")
    again = run(tmp_path)
    assert again.generated == ()
    assert {name: again.values[name] for name in install.GENERATED} == {
        name: made.values[name] for name in install.GENERATED
    }


def test_a_file_with_only_a_persons_own_lines_is_a_first_install(tmp_path: Path) -> None:
    (tmp_path / install.SETTINGS).write_text("SITE_ENGINE_MEMORY=2g\n", encoding="utf-8")
    made = run(tmp_path)
    assert made.generated == tuple(install.GENERATED)
    assert install.parse(settings_file(tmp_path))["SITE_ENGINE_MEMORY"] == "2g"


def test_patient_data_mode_is_chosen_once(tmp_path: Path) -> None:
    run(tmp_path)
    written = settings_file(tmp_path)
    with pytest.raises(install.Refused, match="chosen when this deployment was installed"):
        run(tmp_path, {**ANSWERS, "PATIENT_DATA": "no"})
    assert settings_file(tmp_path) == written
    # Said again as it is, or not said at all: nothing changes.
    run(tmp_path, {**ANSWERS, "PATIENT_DATA": "YES"})
    run(tmp_path, {key: value for key, value in ANSWERS.items() if key != "PATIENT_DATA"})
    assert settings_file(tmp_path) == written


# -- nothing secret is printed --------------------------------------------------------------------


def test_nothing_secret_is_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = io.StringIO()
    console = install.Console(interactive=False, out=out)
    made = run(tmp_path, {**ANSWERS, "ALERT_WEBHOOK": WEBHOOK}, console=console)
    install.summary(tmp_path, console, shown="/opt/mendari")
    told = out.getvalue() + "".join(capsys.readouterr())
    secret = [made.values[name] for name in install.GENERATED] + [CLIENT_SECRET, WEBHOOK]
    assert all(len(value) >= 16 for value in secret)
    assert not [value for value in secret if value in told]
    assert (tmp_path / "tls" / "privkey.pem").read_text() not in told
    # What it does say: where to go, what to register, who signs in first, what was made.
    for expected in (
        "Mendari is set up in /opt/mendari",
        PUBLIC,
        f"{PUBLIC}/auth/callback",
        ADA,
        "It works once.",
        "inputs kept encrypted, deleted after 30 days",
        "Europe/Paris",
        "/opt/mendari/.env",
        "MENDARI_PHI_KEY: the key that seals kept inputs and notes",
        "SELF-SIGNED",
        "/opt/mendari/backups/",
        "Copy them off this machine",
        "Finish setting up",
    ):
        assert expected in told, expected
    for name in install.GENERATED:  # each generated secret is named, with what it is for
        assert f"{name}: " in told, name


def test_an_identity_provider_that_does_not_answer_is_said_and_stops_nothing(
    tmp_path: Path,
) -> None:
    out = io.StringIO()
    asked: list[str] = []

    def unreachable(issuer: str) -> bool:
        asked.append(issuer)
        return False

    run(tmp_path, console=install.Console(interactive=False, out=out), probe=unreachable)
    assert asked == [IDP]
    assert "could not be read from this machine" in out.getvalue()
    assert not nothing_written(tmp_path)  # a provider that is down today is no reason to stop


# -- asked at a terminal --------------------------------------------------------------------------


def test_at_a_terminal_each_answer_is_asked_once_with_what_is_there_as_its_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = io.StringIO()
    typed = [
        "http://mendari.stmarys.example\n",  # refused, and asked again
        f"{PUBLIC}\n",
        f"{IDP}\n",
        f"{CLIENT}\n",
        "Ada@StMarys.Example\n",
        "St Mary's Hospital\n",
        "perhaps\n",  # refused, and asked again
        "yes\n",
    ]
    hidden: list[str] = []
    secret = [CLIENT_SECRET]

    def secretly(prompt: str) -> str:
        hidden.append(prompt)
        return secret[0]

    monkeypatch.setattr(install.sys, "stdin", io.StringIO("".join(typed)))
    monkeypatch.setattr(install.getpass, "getpass", secretly)
    console = install.Console(interactive=True, out=out)
    first = run(tmp_path, {}, console=console)
    asked = out.getvalue()
    assert first.values["MENDARI_OIDC_CLIENT_SECRET"] == CLIENT_SECRET and len(hidden) == 1
    assert CLIENT_SECRET not in asked  # typed hidden, never echoed
    assert "never served over plain http" in asked and "answer yes or no" in asked
    for prompt, _ in install.QUESTIONS.values():
        assert prompt in asked or prompt in hidden[0], prompt
    written = settings_file(tmp_path)

    # Again: Enter alone keeps each answer; the secret's default is never shown; patient data
    # mode is not asked a second time.
    out = io.StringIO()
    hidden.clear()
    monkeypatch.setattr(install.sys, "stdin", io.StringIO("\n" * 5))
    secret[0] = ""  # Enter alone
    run(tmp_path, {}, console=install.Console(interactive=True, out=out))
    assert settings_file(tmp_path) == written
    assert f"[{PUBLIC}]" in out.getvalue() and "[kept as it is]" in hidden[0]
    assert CLIENT_SECRET not in out.getvalue() + hidden[0]
    assert install.QUESTIONS["PATIENT_DATA"][0] not in out.getvalue()
    # No terminal and no answers: stopped, never a guess.
    monkeypatch.setattr(install.sys, "stdin", io.StringIO(""))
    with pytest.raises(install.Refused, match="no answer could be read"):
        run(tmp_path / "other", {}, console=install.Console(interactive=True, out=io.StringIO()))


def test_the_time_zone_is_the_machines_unless_an_answer_names_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(tmp_path / "a", timezone="America/Chicago").values["MENDARI_TIMEZONE"] == (
        "America/Chicago"
    )
    named = run(tmp_path / "b", {**ANSWERS, "TIMEZONE": "Europe/Dublin"}, timezone="UTC")
    assert named.values["MENDARI_TIMEZONE"] == "Europe/Dublin"
    # Kept at the next run, whatever the machine says by then.
    assert run(tmp_path / "b", timezone="Asia/Tokyo").values["MENDARI_TIMEZONE"] == "Europe/Dublin"
    # A machine whose zone cannot be read: an unattended run stops, a terminal is asked.
    for unread in ("", "/etc/localtime", None):
        with pytest.raises(install.Refused, match="add TIMEZONE to the answers file"):
            run(tmp_path / "c", timezone=unread)
    assert nothing_written(tmp_path / "c")
    out = io.StringIO()
    monkeypatch.setattr(install.sys, "stdin", io.StringIO("Chicago\nAmerica/Chicago\n"))
    asked = run(
        tmp_path / "d",
        {**ANSWERS, "SELF_SIGNED": "yes"},
        console=install.Console(interactive=True, out=out),
        timezone="",
    )
    assert asked.values["MENDARI_TIMEZONE"] == "America/Chicago"
    assert "could not be read" in out.getvalue() and "not a time zone" in out.getvalue()


# -- the settings file, as docker compose reads one ----------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain-Value_1.2:3/4@5%6+7=8,9",
        "two words",
        "St Mary's Hospital",
        'a "quoted" word',
        'it\'s "both" kinds',
        "a $DOLLAR and ${BRACES}",
        "it's $HOME",
        "a # that is no comment",
        "back\\slash",
        "it's a back\\slash",
        " leading and trailing ",
        "ünïcödé",
    ],
)
def test_a_value_is_read_back_as_it_was_written(value: str) -> None:
    assert install.unquote(install.quote(value)) == value
    assert install.parse(f"NAME={install.quote(value)}\n") == {"NAME": value}
    assert install.parse(install.render({"MENDARI_BOOTSTRAP_ORG_NAME": value})) == {
        "MENDARI_BOOTSTRAP_ORG_NAME": value
    }


def test_a_setting_is_one_line_and_a_hand_written_file_reads_as_compose_reads_it() -> None:
    for text in ("two\nlines", "a\rreturn", "nul\x00"):
        with pytest.raises(install.Refused, match="one line"):
            install.quote(text)
    read = install.parse(
        "# a comment\n\nPLAIN=value\nexport EXPORTED=1\nCOMMENTED=value # a comment\n"
        'SPACED = spaced \nHASH=a#b\nnot a setting\n=nameless\nDOUBLE="a \\$b"\n'
    )
    assert read == {
        "PLAIN": "value",
        "EXPORTED": "1",
        "COMMENTED": "value",
        "SPACED": "spaced",
        "HASH": "a#b",
        "DOUBLE": "a $b",
    }


# -- the certificate ------------------------------------------------------------------------------


def certificate_for(host: str, *, days: int = 30, start: int = -1) -> tuple[bytes, bytes]:
    """A certificate a site's own authority might issue: here, signed by another key."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key, authority = (
        ec.generate_private_key(ec.SECP256R1()),
        ec.generate_private_key(ec.SECP256R1()),
    )
    now = datetime.now(UTC)
    made = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "St Mary's test CA")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=start))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(authority, hashes.SHA256())
    )
    return (
        made.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def test_without_a_certificate_it_stops_unless_a_self_signed_one_is_asked_for(
    tmp_path: Path,
) -> None:
    with pytest.raises(install.Refused, match="no certificate: put the certificate") as refused:
        run(tmp_path, make_certificate=False)
    assert "--self-signed" in str(refused.value) and "private network only" in str(refused.value)
    assert nothing_written(tmp_path) and not (tmp_path / "tls").exists()  # never plain http

    made = run(tmp_path, make_certificate=True)
    assert made.certificate.self_signed
    assert made.certificate.names == ("mendari.stmarys.example",)
    assert timedelta(days=89) < made.certificate.expires - datetime.now(UTC) <= timedelta(days=90)
    tls = tmp_path / "tls"
    assert sorted(p.name for p in tls.iterdir()) == [
        "SELF-SIGNED.txt",
        "fullchain.pem",
        "privkey.pem",
    ]
    assert stat.S_IMODE((tls / "privkey.pem").stat().st_mode) == 0o600
    note = (tls / "SELF-SIGNED.txt").read_text()
    assert "signed by its own key" in note and "trial on a private network only" in note
    assert "mendari.stmarys.example" in note
    # An address that is a number gets a certificate for that number.
    numbered = run(tmp_path / "ip", {**ANSWERS, "PUBLIC_URL": "https://10.20.30.40"})
    assert numbered.certificate.names == ("10.20.30.40",)


def test_the_sites_own_certificate_is_used_and_never_written_over(tmp_path: Path) -> None:
    tls = tmp_path / "tls"
    tls.mkdir()
    chain, key = certificate_for("mendari.stmarys.example")
    (tls / "fullchain.pem").write_bytes(chain)
    (tls / "privkey.pem").write_bytes(key)
    made = run(tmp_path, make_certificate=True)  # asked for or not: theirs is there
    assert not made.certificate.self_signed
    assert (tls / "fullchain.pem").read_bytes() == chain and (
        tls / "privkey.pem"
    ).read_bytes() == key
    assert not (tls / "SELF-SIGNED.txt").exists()
    out = io.StringIO()
    install.summary(tmp_path, install.Console(interactive=False, out=out))
    assert "your certificate" in out.getvalue() and "SELF-SIGNED" not in out.getvalue()


def test_a_certificate_the_front_could_not_serve_stops_the_install(tmp_path: Path) -> None:
    tls = tmp_path / "tls"
    tls.mkdir()
    chain, key = certificate_for("mendari.stmarys.example")
    _, another = certificate_for("mendari.stmarys.example")
    expired, expired_key = certificate_for("mendari.stmarys.example", days=-1, start=-30)
    cases = [
        ({"fullchain.pem": chain}, "the front needs both"),
        ({"privkey.pem": key}, "the front needs both"),
        ({"fullchain.pem": chain, "privkey.pem": another}, "is not the key of"),
        ({"fullchain.pem": b"not a certificate", "privkey.pem": key}, "must be a PEM certificate"),
        ({"fullchain.pem": chain, "privkey.pem": b"not a key"}, "must be a PEM certificate"),
        ({"fullchain.pem": expired, "privkey.pem": expired_key}, "expired on"),
    ]
    for files, said in cases:
        for name in ("fullchain.pem", "privkey.pem"):
            (tls / name).unlink(missing_ok=True)
        for name, content in files.items():
            (tls / name).write_bytes(content)
        with pytest.raises(install.Refused, match=said):
            run(tmp_path, make_certificate=True)
        assert nothing_written(tmp_path)
        assert {p.name: p.read_bytes() for p in tls.iterdir()} == files  # nothing made over them


# -- the settings start a server ------------------------------------------------------------------


def serving(
    directory: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdp, database: Path
) -> TestClient:
    """A server on the settings file an install wrote, as the template's engine reads it
    (`env_file`), with the folders the template keeps in its state volume inside `directory`."""
    values, _ = install.read_settings(directory / install.SETTINGS)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MENDARI_CACHE_DIR", str(directory / "state" / "cache"))
    monkeypatch.setenv("MENDARI_CONFIG_DIR", str(directory / "state" / "config"))
    transport = httpx.MockTransport(idp)
    monkeypatch.setattr(
        auth_module, "GuardedHttp", lambda **kw: GuardedHttp(transport=transport, **kw)
    )
    # The product's own agents (the bundled demo agent alone), as the engine's image has them.
    app = create_app(db_path=database, packs_paths=[ROOT / "agents"])
    return TestClient(app, base_url=PUBLIC, headers=DASHBOARD)


def states(client: TestClient) -> dict[str, str]:
    answer = client.get("/api/system/checks")
    assert answer.status_code == 200, answer.text
    return {check["key"]: check["state"] for check in answer.json()["checks"]}


def test_the_generated_settings_start_a_server_that_passes_its_own_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdp()
    made = run(tmp_path, {**ANSWERS, "ALERT_WEBHOOK": WEBHOOK})
    with serving(tmp_path, monkeypatch, idp, tmp_path / "site.db") as client:
        # Signed out, nothing but the sign-in: the console sends the browser to the provider.
        assert client.get("/api/system/checks").status_code == 401
        started = client.get("/auth/login", follow_redirects=False)
        assert started.status_code in (302, 307)
        assert started.headers["location"].startswith(f"{IDP}/authorize?")
        assert (
            f"redirect_uri={PUBLIC.replace(':', '%3A').replace('/', '%2F')}%2Fauth%2Fcallback"
            in (started.headers["location"])
        )
        # Anyone else who signs in belongs nowhere; the administrator the install named is
        # admitted, as the operator of the deployment.
        sign_in(client, idp, sub="bo", email="bo@stmarys.example", email_verified=True)
        assert client.get("/api/system/checks").status_code == 403
        assert sign_in(client, idp, sub="ada", email=ADA, email_verified=True).status_code == 302
        me = client.get("/api/auth/me").json()["principal"]
        assert (me["org"], me["roles"]) == ("default", ["admin"]) and "operate" in me["permissions"]
        found = states(client)
        # What the install itself is answerable for is in order from the first start.
        settled = ("sign_in", "public_address", "client_address", "patient_data", "sealing_key")
        for key in (*settled, "limits", "version"):
            assert found[key] == "ok", (key, found[key])
        # What is left is a step for the administrator, or the daily jobs' first run (a few
        # minutes after the start), each said as that: a second administrator, verifying the
        # trail, a test alert; then a registry, an EHR and an agent of the site's own. (The
        # database is the template's Postgres there; this test's is a file.)
        to_do = {key: state for key, state in found.items() if state != "ok"}
        assert to_do == {
            "operators": "attention",
            "database": "attention",
            "audit_chain": "attention",
            "checkpoint": "attention",
            "checkpoint_signing": "attention",
            "scheduled_jobs": "attention",
            "backup": "not_set",
            "alerts": "attention",
            "ehr": "not_set",
            "registry": "not_set",
            "agents": "not_set",
        }
        body = client.get("/api/system/checks").json()
        assert body["setup"] == {"fresh": True, "finished": False}  # "Finish setting up" shows
        every = {check["key"]: check for check in body["checks"]}
        assert "none has run yet" in every["scheduled_jobs"]["detail"]
        assert "St Mary's Hospital" in every["operators"]["detail"]
        assert "has done its work and is ignored from now on" in every["operators"]["detail"]
        assert all(every[key]["action"] for key in to_do)  # each says what to do about it
        # Nothing the install generated, and nothing the site answered in confidence, is in it.
        secret = [made.values[name] for name in install.GENERATED] + [CLIENT_SECRET, WEBHOOK]
        assert not [value for value in secret if value in client.get("/api/system/checks").text]
        # The scheduled jobs' routes answer the generated secret, and nothing else.
        assert client.get("/api/internal/checkpoint").status_code == 401
        jobs = {"Authorization": f"Bearer {made.values['CRON_SECRET']}"}
        checkpoint = client.get("/api/internal/checkpoint", headers=jobs).json()
        assert checkpoint == {"ok": True, "recorded": True, "signed": True}
        assert client.get("/api/internal/retention", headers=jobs).status_code == 200
        assert client.get("/api/internal/monitor", headers=jobs).status_code == 200
        backed = client.post("/api/internal/backup", params={"outcome": "ok"}, headers=jobs)
        assert backed.json() == {"recorded": True}
        # A second administrator joins (recorded beside the server, under its own keys).
        with AuditStore(tmp_path / "site.db", phi=phi_policy()) as store:
            member(store, org="default", roles=["admin"], sub="bo", email="bo@stmarys.example")
        verified = client.post("/api/system/audit/verify")
        assert verified.status_code == 200 and verified.json()["ok"] is True
        after = states(client)
        done = ("operators", "audit_chain", "checkpoint", "checkpoint_signing", "scheduled_jobs")
        for key in (*done, "backup"):
            assert after[key] == "ok", (key, after[key])
        # Only what the site brings itself is left: its alert channel's first delivery, its
        # registry, its EHR, an agent of its own (and, in this test, the database).
        assert {key for key, state in after.items() if state != "ok"} == {
            "database",
            "alerts",
            "ehr",
            "registry",
            "agents",
        }
    # The server's own start-up check, as `mendari-site` runs it before anything starts.
    install.starts()


def test_the_check_refuses_what_the_server_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")  # needs sign-in, which is not set
    monkeypatch.delenv("MENDARI_OIDC_ISSUER", raising=False)
    with pytest.raises(MendariError, match="MENDARI_ACCOUNTS"):
        install.starts()


# -- the image, for upgrades and rollbacks --------------------------------------------------------


def test_an_upgrade_records_the_image_and_keeps_the_one_before(tmp_path: Path) -> None:
    newer = IMAGE.replace("v0.1.0", "v0.2.0").replace("a" * 64, "b" * 64)
    with pytest.raises(install.Refused, match="run `mendari-site install`"):
        install.set_image(tmp_path, newer)
    run(tmp_path)
    written = settings_file(tmp_path)
    with pytest.raises(install.Refused, match="no earlier image"):
        install.set_image(tmp_path, None, back=True)
    assert install.set_image(tmp_path, IMAGE) == (IMAGE, "")  # the same image: nothing changes
    assert settings_file(tmp_path) == written

    assert install.set_image(tmp_path, newer) == (newer, IMAGE)
    upgraded = install.parse(settings_file(tmp_path))
    assert (upgraded["MENDARI_IMAGE"], upgraded["MENDARI_IMAGE_PREVIOUS"]) == (newer, IMAGE)
    before = install.parse(written)
    assert {k: v for k, v in upgraded.items() if not k.startswith("MENDARI_IMAGE")} == {
        k: v for k, v in before.items() if not k.startswith("MENDARI_IMAGE")
    }  # nothing else changed: no secret, no answer
    assert stat.S_IMODE((tmp_path / install.SETTINGS).stat().st_mode) == 0o600
    # Back, and forward again: the two swap.
    assert install.set_image(tmp_path, None, back=True) == (IMAGE, newer)
    assert install.set_image(tmp_path, None, back=True) == (newer, IMAGE)
    # Installing again keeps the image an upgrade recorded, and the one before.
    kept = run(tmp_path, image=None).values
    assert (kept["MENDARI_IMAGE"], kept["MENDARI_IMAGE_PREVIOUS"]) == (newer, IMAGE)
    with pytest.raises(install.Refused, match="no engine image was named"):
        run(tmp_path / "new", image=None)


# -- the commands, as the script runs them --------------------------------------------------------


def test_the_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    answers = tmp_path / "answers"
    answers.write_text(
        "".join(f"{name}={install.quote(value)}\n" for name, value in ANSWERS.items())
        + "SELF_SIGNED=yes\nTIMEZONE=Europe/Paris\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(install, "reachable", lambda issuer: True)
    made = ["settings", "--dir", str(site), "--answers", str(answers), "--image", IMAGE]
    made += ["--owner", "1001:1002", "--shown-dir", "/opt/mendari"]
    assert install.main(made) == 0
    told = capsys.readouterr()
    values = install.parse(settings_file(site))
    assert (values["SITE_UID"], values["SITE_GID"]) == ("1001", "1002")
    assert told.err == ""
    assert told.out.strip() == (
        "The settings are written: /opt/mendari/.env. Generated now, and never again: "
        + ", ".join(install.GENERATED)
        + "."
    )
    # Again, the answers on standard input (as `mendari-site` gives them): nothing is made.
    written = settings_file(site)
    monkeypatch.setattr(install.sys, "stdin", io.StringIO(answers.read_text(encoding="utf-8")))
    again = [*made]
    again[again.index(str(answers))] = "-"
    assert install.main(again) == 0
    assert capsys.readouterr().out.strip() == (
        "The settings are written: /opt/mendari/.env. No secret was generated: the 6 this "
        "deployment has are kept as they were."
    )
    assert settings_file(site) == written

    assert install.main(["summary", "--dir", str(site), "--shown-dir", "/opt/mendari"]) == 0
    told = capsys.readouterr()
    assert "Mendari is set up in /opt/mendari" in told.out
    assert "First administrator  ada@stmarys.example" in told.out
    assert not [values[name] for name in install.GENERATED if values[name] in told.out]
    assert CLIENT_SECRET not in told.out

    newer = IMAGE.replace("a" * 64, "c" * 64)
    assert install.main(["image", "--dir", str(site), "--image", newer]) == 0
    assert capsys.readouterr().out.strip() == f"image: {newer} (before: {IMAGE})"
    assert install.main(["image", "--dir", str(site), "--back"]) == 0
    assert capsys.readouterr().out.strip() == f"image: {IMAGE} (before: {newer})"

    # `check` and `migrate` read the environment, as the template's containers have it.
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    assert install.main(["check"]) == 0
    assert "these settings start Mendari" in capsys.readouterr().out
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert install.main(["migrate"]) == 1
    assert "DATABASE_URL is not set" in capsys.readouterr().err
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "site.db"))
    assert install.main(["migrate"]) == 0
    assert "the database is up to date" in capsys.readouterr().out
    assert install.main(["migrate"]) == 0  # again: nothing to do, and nothing refused
    capsys.readouterr()

    # A refusal is one line on stderr and exit 1: never a traceback, never a secret.
    monkeypatch.setenv("MENDARI_PHI_KEY", "not-a-key")
    assert install.main(["check"]) == 1
    refused = capsys.readouterr()
    assert refused.out == "" and refused.err.startswith("error: ")
    assert refused.err.count("\n") == 1 and "Traceback" not in refused.err
    assert install.main(["settings", "--dir", str(site), "--answers", str(tmp_path / "none")]) == 1
    assert "the answers file cannot be read" in capsys.readouterr().err
    assert (
        install.main(["settings", "--dir", str(site), "--owner", "ada", "--answers", str(answers)])
        == 1
    )
    assert "--owner is <uid>:<gid>" in capsys.readouterr().err
    monkeypatch.setattr(install.sys, "stdin", io.StringIO(""))  # no terminal, no answers
    assert install.main(["settings", "--dir", str(site)]) == 1
    assert "run this in a terminal, or give --answers" in capsys.readouterr().err
    assert install.main(["summary", "--dir", str(tmp_path / "empty")]) == 1
    assert "run `mendari-site install`" in capsys.readouterr().err


def test_an_error_that_is_not_mendaris_own_is_one_line_without_the_librarys_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A library's error can hold a connection string: what was being done and the kind of
    error are said, and nothing of its text. MENDARI_DEBUG=1 lets it through whole."""

    def unreachable() -> None:
        raise ConnectionError("postgresql://mendari:zq-database-password@postgres:5432/mendari")

    monkeypatch.setattr(install, "migrate", unreachable)
    assert install.main(["migrate"]) == 1
    told = capsys.readouterr()
    assert told.out == "" and told.err.count("\n") == 1
    assert told.err.startswith("error: the database could not be opened or brought up to date")
    assert "ConnectionError" in told.err and "the engine is not started on it" in told.err
    assert "zq-database-password" not in told.err and "Traceback" not in told.err
    monkeypatch.setenv("MENDARI_DEBUG", "1")
    with pytest.raises(ConnectionError):
        install.main(["migrate"])

    def interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(install, "migrate", interrupted)
    assert install.main(["migrate"]) == 130
    assert capsys.readouterr().err.strip() == "interrupted"
