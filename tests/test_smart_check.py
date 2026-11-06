# SPDX-License-Identifier: Apache-2.0
"""`mendari smart check <base>` (server/smart_check.py) against the fake EHR of test_smart.py:
what a launch from that base would meet here, before go-live. The real launcher is checked in
the CI job `sandbox-e2e`."""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable
from pathlib import Path

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec

from mendari.cli import main
from mendari.server import smart_check
from mendari.standards.fhir_client import GuardedHttp
from mendari.standards.smart import DEFAULT_SCOPES
from tests.test_smart import CLIENT, EHR, ISS, FakeEhr

ROOT = Path(__file__).resolve().parents[1]
SIGN_IN = {"MENDARI_OIDC_ISSUER": "https://idp.example.org", "MENDARI_OIDC_CLIENT_ID": "mendari"}
# What smart-launcher-v2 publishes, as `sandbox-e2e` runs it: the compose service `launcher`,
# this image, whose `app/backend/routes/fhir/.well-known/smart-configuration.ts` (sha256
# f223ccb910f68d4ec149d3bf467c7dbdc4859cfff5a711f83202f548686ee8e5, the same as upstream's)
# lists these. A new image needs the list read again from it.
LAUNCHER_IMAGE = (
    "smartonfhir/smart-launcher-2@sha256:"
    "72bd3e3c682ce4c74e6dddb605d89acad7c8aae446ae38079e8dfe8455b84793"
)
LAUNCHER_SCOPES = [
    "openid",
    "profile",
    "fhirUser",
    "launch",
    "launch/patient",
    "launch/encounter",
    "patient/*.*",
    "user/*.*",
    "offline_access",
]


@pytest.fixture
def ehr(monkeypatch: pytest.MonkeyPatch) -> FakeEhr:
    """A registered EHR (sign-in on, people's organization from the `org` claim,
    `clinic:decide`) whose every request is recorded."""
    fake = FakeEhr()
    for name, value in SIGN_IN.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MENDARI_OIDC_ORG_CLAIM", "org")
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", EHR)
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{ISS}=clinic:decide")
    serve(monkeypatch, fake)
    return fake


def serve(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        smart_check, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
    )


def report(capsys: pytest.CaptureFixture[str], base: str = ISS) -> tuple[int, dict[str, str]]:
    """The command's exit code, and each check's status (columns two spaces apart)."""
    code = main(["smart", "check", base])
    rows = [re.split(r"\s{2,}", line, maxsplit=2) for line in capsys.readouterr().out.splitlines()]
    return code, {check: status for status, check, _ in rows}


def test_a_registered_ehr_ready_to_launch_passes_every_check(
    ehr: FakeEhr, capsys: pytest.CaptureFixture[str]
) -> None:
    """The base as an EHR may send it (any case, a trailing slash) is reported as the key
    MENDARI_SMART_EHRS uses; only the public configuration and keys are fetched."""
    code = main(["smart", "check", "HTTPS://EHR.Example.org/fhir/"])
    out = capsys.readouterr().out
    assert code == 0, out
    lines = out.splitlines()
    assert [line.split()[0] for line in lines] == ["ok"] * 8
    # The status column fits `unknown`, the longest status, and two spaces.
    assert all(line[7:9] == "  " and line[9] != " " for line in lines), out
    assert f"registration key  {ISS}" in out
    assert "organization clinic, level decide" in out
    assert f"{DEFAULT_SCOPES}: each among the 6 the EHR publishes" in out
    assert "1 of 1 keys verify RS256/ES384" in out
    assert [str(r.url) for r in ehr.requests] == [
        f"{ISS}/.well-known/smart-configuration",
        f"{EHR}/keys",
    ]


def test_a_base_off_the_allow_list_is_never_called(
    ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", "https://other.example.org")
    monkeypatch.setenv("MENDARI_SMART_EHRS", "")
    code, checks = report(capsys)
    assert code == 1
    assert checks["allow-list"] == "fail" and checks["registration"] == "fail"
    assert "discovery" not in checks and ehr.requests == []


@pytest.mark.parametrize(
    ("setting", "value", "failed"),
    [
        ("MENDARI_SMART_EHRS", "", "registration"),  # sign-in on: unregistered is refused
        ("MENDARI_SMART_EHRS", f"{ISS}=clinic", "settings"),  # the server would not start
        ("MENDARI_SMART_CLIENT_ID", "", "SMART client"),
        ("MENDARI_SMART_SCOPES", "launch openid fhirUser offline_access", "SMART client"),
        ("MENDARI_SMART_SCOPES", "launch patient/*.rs", "SMART client"),  # who launched?
    ],
)
def test_settings_that_would_refuse_a_launch_fail(
    ehr: FakeEhr,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: str,
    value: str,
    failed: str,
) -> None:
    monkeypatch.setenv(setting, value)
    code, checks = report(capsys)
    assert code == 1
    assert checks[failed] == "fail"


@pytest.mark.parametrize(
    ("overrides", "failed"),
    [
        ({"code_challenge_methods_supported": ["plain"]}, "PKCE S256"),
        ({"jwks_uri": None}, "JWKS"),
        ({"jwks_uri": f"{EHR}/no-keys-here"}, "JWKS"),  # 404
        ({"jwks_uri": "https://keys.example.net/jwks"}, "JWKS"),  # off the allow-list
        ({"jwks_uri": f"{EHR}/symmetric"}, "JWKS"),  # no key verifies RS256 or ES384
        ({"token_endpoint": "https://auth.example.net/token"}, "discovery"),
        ({"authorization_endpoint": None}, "discovery"),
    ],
)
def test_an_ehr_configuration_that_would_refuse_a_launch_fails(
    ehr: FakeEhr,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, object],
    failed: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{EHR}/symmetric":
            keys = [
                {"kty": "oct", "k": "c2VjcmV0", "alg": "HS256"},
                {"kty": "RSA", "n": "AQAB", "e": "AQAB", "use": "enc"},
                {"kty": "EC", "crv": "P-256", "x": "AA", "y": "AA"},
            ]
            return httpx.Response(200, json={"keys": keys})
        return ehr(request)

    serve(monkeypatch, handler)
    ehr.config_overrides = overrides
    code, checks = report(capsys)
    assert code == 1
    assert checks[failed] == "fail"


def test_what_works_but_needs_a_look_is_a_warning(
    ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Open mode with no EHR registered lets any allowed EHR launch; an EHR that does not say it
    supports S256 may still accept it. An ES384 key counts as usable."""
    for name in (*SIGN_IN, "MENDARI_SMART_EHRS"):
        monkeypatch.delenv(name)
    key = ec.generate_private_key(ec.SECP384R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{EHR}/ec-keys":
            return httpx.Response(200, json={"keys": [jwk]})
        return ehr(request)

    serve(monkeypatch, handler)
    ehr.config_overrides = {"code_challenge_methods_supported": None, "jwks_uri": f"{EHR}/ec-keys"}
    code, checks = report(capsys)
    assert code == 0
    assert checks["registration"] == checks["PKCE S256"] == "warn"
    assert checks["JWKS"] == "ok"


@pytest.mark.parametrize(
    ("org_claim", "registered", "sign_in", "status"),
    [
        ("", "clinic", True, "fail"),  # everyone signs in to `default`: nobody reads `clinic`
        ("", "default", True, "ok"),  # one organization: its name is `default`
        ("org", "clinic", True, "ok"),  # people's sign-in names their organization
        ("", "clinic", False, "warn"),  # open mode: it matters once sign-in is on
    ],
)
def test_a_registration_to_an_organization_nobody_signs_in_to_fails(
    ehr: FakeEhr,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    org_claim: str,
    registered: str,
    sign_in: bool,
    status: str,
) -> None:
    """A launch's decisions are its registered organization's. Without MENDARI_OIDC_ORG_CLAIM
    everyone who signs in is in `default`, so an EHR registered to any other organization
    decides where no reviewer can read: HUMAN_REVIEW items would never be reviewed."""
    monkeypatch.setenv("MENDARI_OIDC_ORG_CLAIM", org_claim)
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{ISS}={registered}:decide")
    if not sign_in:
        for name in SIGN_IN:
            monkeypatch.delenv(name)
    code = main(["smart", "check", ISS])
    out = capsys.readouterr().out
    rows = [re.split(r"\s{2,}", line, maxsplit=2) for line in out.splitlines()]
    found = {check: (state, detail) for state, check, detail in rows}
    assert found["registration"][0] == status, out
    assert code == (1 if status == "fail" else 0), out
    if status != "ok":
        assert "MENDARI_OIDC_ORG_CLAIM" in found["registration"][1]
        assert "default" in found["registration"][1]


def test_memberships_mode_warns_a_launch_needs_a_record_and_fails_an_id_none_can_have(
    ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """People reach an organization by membership there, so no organization claim is needed;
    the check cannot read the organization's record, so it says to (`mendari access check`)."""
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    code, checks = report(capsys)
    assert (code, checks["registration"], checks["sign-in"]) == (0, "warn", "warn")
    monkeypatch.delenv("MENDARI_OIDC_ORG_CLAIM")
    code, checks = report(capsys)
    assert (code, checks["registration"], "sign-in" in checks) == (0, "warn", False)
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{ISS}=Clinic_A:decide")
    code, checks = report(capsys)
    assert (code, checks["registration"]) == (1, "fail")


# -- the scopes a launch asks for, against the EHR's `scopes_supported` (LP-23l) -----------------


def rows(out: str) -> dict[str, tuple[str, str]]:
    """Each check's status and detail."""
    found = [re.split(r"\s{2,}", line, maxsplit=2) for line in out.splitlines()]
    return {check: (status, detail) for status, check, detail in found}


@pytest.mark.parametrize(
    ("published", "asked", "missing"),
    [
        (["openid", "fhirUser", "launch", "patient/Patient.rs"], None, "patient/*.rs"),
        (["openid", "fhirUser", "patient/*.*"], None, "launch"),
        (
            ["openid", "fhirUser", "launch", "user/*.*", "system/*.*"],  # another context
            "launch openid fhirUser patient/Observation.rs",
            "patient/Observation.rs",
        ),
        (
            ["openid", "fhirUser", "launch", "patient/*.read"],
            "launch openid fhirUser patient/Condition.rs patient/Condition.c",
            "patient/Condition.c",
        ),
        (
            ["openid", "fhirUser", "launch", "patient/Observation.rs?category=laboratory"],
            "launch openid fhirUser patient/Observation.rs",
            "patient/Observation.rs",  # a granular scope covers only its own query
        ),
    ],
)
def test_a_scope_the_ehr_does_not_publish_fails(
    ehr: FakeEhr,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    published: list[str],
    asked: str | None,
    missing: str,
) -> None:
    """Each scope the launch asks for (MENDARI_SMART_SCOPES, else the default) must be one the
    EHR publishes, or covered by one it does; the detail names those it does not publish (none
    of the EHR's own text) and says the list need not be exhaustive (SMART 2.2.0), so the EHR
    is asked rather than assumed."""
    if asked is not None:
        monkeypatch.setenv("MENDARI_SMART_SCOPES", asked)
    ehr.config_overrides = {"scopes_supported": published}
    code = main(["smart", "check", ISS])
    found = rows(capsys.readouterr().out)
    status, detail = found["scopes"]
    assert (code, status) == (1, "fail")
    assert detail.startswith(f"the EHR does not publish {missing}. ")
    assert "need not list every scope it grants (SMART 2.2.0)" in detail
    assert "MENDARI_SMART_SCOPES" in detail
    assert [check for check, (state, _) in found.items() if state != "ok"] == ["scopes"]


@pytest.mark.parametrize(
    "published",
    [
        "absent",
        None,
        [],
        "openid fhirUser launch patient/*.rs",  # a string, not a list
        ["openid", "fhirUser", "launch", {"scope": "patient/*.rs"}],
        [1, 2],
    ],
    ids=["absent", "null", "empty", "string", "an-object", "numbers"],
)
def test_unpublished_scopes_are_unknown_not_passed(
    ehr: FakeEhr,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    published: object,
) -> None:
    """An EHR that publishes no list of scopes says nothing about them: the status is
    `unknown`, printed in the status column, never `ok` and never assumed. Only `fail` makes the
    command exit 1, so the rest of the check still passes."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = ehr(request)
        if request.url.path.endswith("/.well-known/smart-configuration"):
            config = json.loads(answer.content)
            if published == "absent":
                del config["scopes_supported"]
            else:
                config["scopes_supported"] = published
            return httpx.Response(200, json=config)
        return answer

    serve(monkeypatch, handler)
    code = main(["smart", "check", ISS])
    out = capsys.readouterr().out
    status, detail = rows(out)["scopes"]
    assert (code, status) == (0, "unknown"), out
    assert out.splitlines()[5].startswith("unknown  scopes  ")  # after discovery
    assert detail == (  # Mendari's own scopes, none of the EHR's text
        "the SMART configuration lists no scopes_supported, so nothing is assumed: ask the EHR "
        f"whether it grants {DEFAULT_SCOPES}"
    )


@pytest.mark.parametrize(
    ("asked", "published", "covered"),
    [
        # SMART v1 against v2, either side: .read is rs, .write is cud, .* is every letter.
        ("patient/*.rs", ["patient/*.*"], True),
        ("patient/*.cruds", ["patient/*.*"], True),
        ("patient/*.rs", ["patient/*.read"], True),
        ("patient/Observation.r", ["patient/Observation.read"], True),
        ("patient/Observation.cud", ["patient/Observation.write"], True),
        ("patient/Observation.read", ["patient/Observation.rs"], True),
        ("patient/Observation.*", ["patient/Observation.cruds"], True),
        ("patient/*.rs", ["patient/*.r"], False),
        ("patient/*.cruds", ["patient/*.read"], False),
        ("patient/Observation.write", ["patient/Observation.rs"], False),
        # A scope's permissions published as separate scopes count together.
        ("patient/*.rs", ["patient/*.r", "patient/*.s"], True),
        ("patient/*.*", ["patient/*.read", "patient/*.write"], True),
        ("patient/Observation.rs", ["patient/*.r", "patient/Observation.s"], True),
        ("patient/*.rs", ["patient/*.r", "user/*.s"], False),
        ("patient/*.rs", ["patient/*.r", "patient/Observation.s"], False),
        ("patient/*.*", ["patient/*.read", "patient/*.cud?category=laboratory"], False),
        (
            "patient/Observation.rs?category=laboratory",
            ["patient/Observation.r", "patient/Observation.s?category=laboratory"],
            True,
        ),
        # A type wildcard covers each type, never the other way.
        ("patient/Observation.rs", ["patient/*.rs"], True),
        ("patient/Observation.read", ["patient/*.*"], True),
        ("patient/*.rs", ["patient/Patient.rs", "patient/Observation.rs"], False),
        # The context is the context: patient/, user/ and system/ cover only their own.
        ("user/Patient.rs", ["user/*.*"], True),
        ("system/Patient.rs", ["system/*.read"], True),
        ("patient/*.rs", ["user/*.*"], False),
        ("patient/*.rs", ["system/*.*"], False),
        ("user/*.rs", ["patient/*.*"], False),
        # A granular scope: by its own text, or the same scope without a query.
        ("patient/Observation.rs?category=laboratory", ["patient/Observation.rs"], True),
        ("patient/Observation.rs?category=laboratory", ["patient/*.read"], True),
        (
            "patient/Observation.rs?category=laboratory",
            ["patient/Observation.rs?category=laboratory"],
            True,
        ),
        (
            "patient/Observation.rs?category=vital-signs",
            ["patient/Observation.rs?category=laboratory"],
            False,
        ),
        ("patient/Observation.rs", ["patient/Observation.rs?category=laboratory"], False),
        # Anything else only by itself, exactly.
        ("launch", ["launch"], True),
        ("openid", ["openid", "fhirUser"], True),
        ("launch", ["launch/patient"], False),
        ("launch/patient", ["launch"], False),
        ("fhirUser", ["fhiruser"], False),
        # A malformed scope covers nothing: letters out of SMART's order, an unknown context.
        ("patient/*.rs", ["patient/*.sr"], False),
        ("patient/*.rs", ["Patient/*.*"], False),
        ("patient/*.rs", ["practitioner/*.*"], False),
        ("patient/*.rs", ["patient/*.rs?"], False),
        ("patient/*.rs", [], False),
    ],
)
def test_a_v1_wildcard_scope_covers_its_v2_form(
    asked: str, published: list[str], covered: bool
) -> None:
    """The comparison reads SMART v1 and v2 scopes alike (App Launch 2.2.0, scopes and launch
    context): what the EHR publishes covers what a launch asks when it allows at least as
    much, in the same context."""
    assert smart_check._published(asked, published) is covered


def test_smart_launcher_v2_s_published_scopes_cover_what_the_sandbox_asks(
    ehr: FakeEhr, capsys: pytest.CaptureFixture[str]
) -> None:
    """`sandbox-e2e` runs `mendari smart check http://launcher/v/r4/fhir` in the signed-in
    engine, which sets no MENDARI_SMART_SCOPES, so it asks the default, `patient/*.rs` among
    it; the pinned launcher publishes `patient/*.*` (SMART v1), not that string, and the check
    must pass on it, as it does here with the launcher's own list."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["launcher"]["image"] == LAUNCHER_IMAGE
    assert "MENDARI_SMART_SCOPES" not in compose["services"]["engine-signin"]["environment"]
    assert "patient/*.rs" in DEFAULT_SCOPES.split() and "patient/*.rs" not in LAUNCHER_SCOPES
    ehr.config_overrides = {"scopes_supported": LAUNCHER_SCOPES}
    code = main(["smart", "check", ISS])
    out = capsys.readouterr().out
    assert code == 0, out
    assert rows(out)["scopes"] == ("ok", f"{DEFAULT_SCOPES}: each among the 9 the EHR publishes")


def test_with_smart_off_no_scopes_are_compared(
    ehr: FakeEhr, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "")
    code = main(["smart", "check", ISS])
    found = rows(capsys.readouterr().out)
    assert code == 1 and found["SMART client"][0] == "fail"
    assert "scopes" not in found and found["discovery"][0] == "ok"
