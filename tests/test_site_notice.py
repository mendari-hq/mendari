# SPDX-License-Identifier: Apache-2.0
"""The site-notice check (scripts/check_site_notice.py): every page of the built docs site, the
404 page too, shows the notice from `mendari.notices`. CI runs it on the real build (jobs docs,
and the docs workflow before a deploy)."""

from __future__ import annotations

import html
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from mendari import notices

ROOT = Path(__file__).resolve().parents[1]


def _script() -> Any:
    path = ROOT / "scripts" / "check_site_notice.py"
    spec = importlib.util.spec_from_file_location("check_site_notice", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _script()
WITH = f"<html><div class='md-banner'>{html.escape(notices.NOTICE)}</div></html>"


def site(root: Path, pages: dict[str, str]) -> Path:
    for name, text in pages.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_the_notice_is_read_from_its_one_source() -> None:
    assert check.notice() == notices.NOTICE


@pytest.mark.parametrize("folder", ["site", "search/site"])
def test_a_page_without_the_notice_is_named(
    folder: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = site(
        tmp_path / folder,
        {"index.html": WITH, "guides/a/index.html": "<html>no</html>", "404.html": "<p>404</p>"},
    )
    assert check.main([str(root)]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "404.html: no notice",
        "guides/a/index.html: no notice",
    ]


def test_a_site_with_the_notice_on_every_page_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = site(tmp_path, {"index.html": WITH, "404.html": WITH, "a/index.html": WITH})
    assert check.main([str(root)]) == 0
    assert capsys.readouterr().out == "ok  3 pages, each with the notice\n"


def test_an_empty_or_missing_site_fails(tmp_path: Path) -> None:
    assert check.main([str(tmp_path)]) == 1
    assert check.main([str(tmp_path / "nothing")]) == 1
