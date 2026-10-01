"""Regenerate src/mendari/dashboard/favicon.ico from favicon.svg (run after editing the SVG).

The .ico holds 16, 32 and 48 px PNGs rendered by Chromium (the browser tests' dev dependency):
    uv run python scripts/make-favicon.py
"""

from __future__ import annotations

import struct
from pathlib import Path

from playwright.sync_api import sync_playwright

DASHBOARD = Path(__file__).resolve().parents[1] / "src" / "mendari" / "dashboard"
SIZES = (16, 32, 48)


def render(svg: str) -> list[bytes]:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pngs = []
        for size in SIZES:
            page = browser.new_page(viewport={"width": size, "height": size})
            page.set_content(
                f"<style>*{{margin:0}}svg{{display:block;width:{size}px;height:{size}px}}</style>{svg}"
            )
            pngs.append(page.screenshot(omit_background=True))
            page.close()
        browser.close()
    return pngs


def ico(pngs: list[bytes]) -> bytes:
    """An ICO container with PNG-compressed entries (read by every current browser)."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = len(header) + 16 * len(pngs)
    entries, data = b"", b""
    for size, png in zip(SIZES, pngs, strict=True):
        entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return header + entries + data


if __name__ == "__main__":
    target = DASHBOARD / "favicon.ico"
    target.write_bytes(ico(render((DASHBOARD / "favicon.svg").read_text())))
    print(f"wrote {target.relative_to(Path.cwd())} ({target.stat().st_size} bytes)")
