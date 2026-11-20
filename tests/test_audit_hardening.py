# SPDX-License-Identifier: Apache-2.0
"""Regression tests for security, standards, algorithmic, and dashboard hardening."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision, alerts, settings
from mendari.core import origin, phi_detect
from mendari.errors import MendariError
from mendari.evals import pooling, result
from mendari.providers.base import Provider, ProviderInfo
from mendari.providers.cascade import CascadeProvider
from mendari.standards import fhir_structure

ROOT = Path(__file__).parents[1]
DASHBOARD = ROOT / "src" / "mendari" / "dashboard"
GUARD_MINI = Path(__file__).parent / "fixtures" / "packs" / "guard-mini"

pytestmark = pytest.mark.core


def test_every_dashboard_icon_reference_exists_in_sprite() -> None:
    """Every icon("...") / emptyState({ icon: "..." }) / #i-... reference resolves to a symbol."""
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    symbols = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', html))
    assert symbols, "expected inline SVG icon sprite in index.html"

    used: dict[str, set[str]] = {}
    for use in re.findall(r'href="#i-([a-z0-9-]+)"', html):
        used.setdefault("index.html", set()).add(use)

    for js_file in sorted(DASHBOARD.glob("*.js")):
        text = js_file.read_text(encoding="utf-8")
        refs = set(re.findall(r'\bicon\("([a-z0-9-]+)"\)', text))
        refs |= set(re.findall(r'emptyState\([^)]*?\bicon:\s*"([a-z0-9-]+)"', text, re.DOTALL))
        refs |= set(re.findall(r'href="#i-([a-z0-9-]+)"', text))
        if refs:
            used[js_file.name] = refs

    missing = {
        filename: sorted(refs - symbols) for filename, refs in used.items() if refs - symbols
    }
    assert missing == {}


def test_fhir_patterns_are_linear_time_and_reject_trailing_newline() -> None:
    """FHIR R4 pattern compilation avoids base64 ReDoS and anchors trailing `$` with `\\Z`."""
    pat = fhir_structure._pattern(fhir_structure._B64_REDOS)
    bad_long = "AAAA" * 2500 + "!"
    started = time.monotonic()
    assert pat.search(bad_long) is None
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"base64Binary match took {elapsed:.2f}s (possible ReDoS)"
    assert pat.search("QUFB") is not None

    # Trailing newline on an R4 primitive with `$` anchor (e.g. Patient.id, Binary.contentType)
    # must fail structural validation.
    issues_id = fhir_structure.check({"resourceType": "Patient", "id": "p1\n"})
    assert any(issue.expression == "Patient.id" for issue in issues_id)

    issues_code = fhir_structure.check({"resourceType": "Binary", "contentType": "text/plain\n"})
    assert any(issue.expression == "Binary.contentType" for issue in issues_code)

    assert fhir_structure.check({"resourceType": "Patient", "id": "p1"}) == []


def test_origin_long_text_sanitizes_untrusted_keys() -> None:
    """Untrusted client keys must not leak PHI into DecisionResult.reasons / audit trail."""
    secret_key = "John Doe SSN 123-45-6789"
    hit = origin.long_text({secret_key: "x" * 200}, limit=100)
    assert hit is not None
    assert "John Doe" not in hit
    assert "123-45-6789" not in hit


def test_phi_detect_handles_yaml_alias_bomb_and_lone_surrogates() -> None:
    """YAML alias bombs mark the file unscannable; lone UTF-16 surrogates never crash RE2."""
    bomb_lines = ["a: &a [x, x, x, x, x, x, x, x, x, x]"]
    prev = "a"
    for idx in range(12):
        name = f"b{idx}"
        bomb_lines.append(f"{name}: &{name} [*{prev}, *{prev}, *{prev}, *{prev}, *{prev}]")
        prev = name
    bomb_bytes = ("\n".join(bomb_lines) + "\n").encode("utf-8")
    report = phi_detect.detect_files({"bomb.yaml": bomb_bytes})
    assert "bomb.yaml" in report.unscannable

    # Lone surrogate must not crash RE2 UTF-8 encoding.
    findings = phi_detect.detect("hello \ud800 world")
    assert isinstance(findings, list)


def test_pooling_homogeneity_handles_large_n_without_oom() -> None:
    """Large-n exact homogeneity test completes quickly without allocating O(N) factorial tables."""
    # Expected hit count = 1.0 < 5 -> takes the exact test path with total N = 1_000_000.
    sites = [(1, 500_000), (1, 500_000)]
    started = time.monotonic()
    outcome = pooling.homogeneity(sites)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert outcome["status"] == "ok"
    assert outcome["method"].startswith("exact")
    assert outcome["differ"] is False


def test_dosing_handles_range_intervals_and_fractional_tablets() -> None:
    """Range intervals like q3-4h (hyphen or en-dash) and fractional tablets parse safely."""
    guard = Decision.from_path(GUARD_MINI)
    dosing_provider = guard.providers["dosing"]

    # 1000 mg q3-4h -> conservative lower bound every 3h = 8x/day = 8000 mg/day > 4000 mg/day max.
    overdose_hyphen = dosing_provider.answer({"draft": "Take acetaminophen 1000 mg q3-4h."})
    assert overdose_hyphen.answers["dose_exceeds_limit"].label == "yes"
    overdose_endash = dosing_provider.answer({"draft": "Take acetaminophen 1000 mg q3\u20134h."})
    assert overdose_endash.answers["dose_exceeds_limit"].label == "yes"

    # "1/2 tablet of ibuprofen 200 mg" must not read denominator "2" as 2 tablets (400 mg).
    frac = dosing_provider.answer({"draft": "Take 1/2 tablet of ibuprofen 200 mg."})
    assert frac.answers["dose_exceeds_limit"].label == "no"


def test_cascade_eager_leaves_only_exposes_primary_member() -> None:
    """CascadeProvider.eager_leaves() excludes fallback members while leaves() keeps all."""
    guard = Decision.from_path(GUARD_MINI)
    slot = "dosing"
    spec = guard.pack.manifest.providers[slot]
    variant = spec.variants[spec.use]
    member_cfg = variant.model_dump(exclude_none=True)
    info = ProviderInfo(slot=slot, variant="cascade_test", type="cascade", model=None)
    cascade = CascadeProvider(
        guard.pack,
        info,
        {q: guard.pack.manifest.questions[q] for q in spec.answers},
        {
            "type": "cascade",
            "accept_at": 0.95,
            "members": {"first": member_cfg, "second": dict(member_cfg)},
        },
    )
    all_leaves: list[Provider] = list(cascade.leaves())
    eager: list[Provider] = list(cascade.eager_leaves())
    assert all_leaves == [cascade.members["first"], cascade.members["second"]]
    assert eager == [cascade.members["first"]]


def test_alerts_check_webhook_rejects_obfuscated_hosts() -> None:
    """Webhook validation rejects decimal IPv4 / empty hosts while allowing compose names."""
    with pytest.raises(MendariError):
        alerts.check_webhook("http://2130706433/hook")
    with pytest.raises(MendariError):
        alerts.check_webhook("https:///hook")
    alerts.check_webhook("http://alertmanager:9093/hook")
    alerts.check_webhook("https://hooks.example.org/mendari")


def test_settings_rejects_non_ascii_digits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-ASCII Unicode digits (e.g. Arabic-Indic '١٢٣') must not crash integer settings."""
    monkeypatch.setenv("MENDARI_MAX_BODY_BYTES", "\u0661\u0662\u0663")
    with pytest.raises(MendariError):
        settings.max_body_bytes()


def test_false_automation_returns_none_when_wrong_count_missing() -> None:
    """Missing `wrong` count must return None instead of fabricating `0 of N`."""
    report: dict[str, Any] = {
        "primary": "q",
        "questions": {"q": {"n": 10}},
        "decisions": {
            "automation": {"false_automation_rate": None, "n": 10},
        },
        "dataset": {"n": 10},
    }
    assert result.false_automation(report) is None
