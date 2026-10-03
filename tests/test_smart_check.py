"""`mendari smart check <base>` (server/smart_check.py) against the fake EHR of test_smart.py:
what a launch from that base would meet here, before go-live. The real launcher is checked in
the CI job `sandbox-e2e`."""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from mendari.cli import main
from mendari.server import smart_check
from mendari.standards.fhir_client import GuardedHttp
from tests.test_smart import CLIENT, EHR, ISS, FakeEhr

SIGN_IN = {"MENDARI_OIDC_ISSUER": "https://idp.example.org", "MENDARI_OIDC_CLIENT_ID": "mendari"}


@pytest.fixture
def ehr(monkeypatch: pytest.MonkeyPatch) -> FakeEhr:
    """A registered EHR (sign-in on, `clinic:decide`) whose every request is recorded."""
    fake = FakeEhr()
    for name, value in SIGN_IN.items():
        monkeypatch.setenv(name, value)
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
    assert [line.split()[0] for line in out.splitlines()] == ["ok"] * 7
    assert f"registration key  {ISS}" in out
    assert "organization clinic, level decide" in out
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
