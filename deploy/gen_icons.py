# Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Re-render the PNG icons from static/icon.svg (run after editing the SVG).

Usage:  .venv/bin/python deploy/gen_icons.py

Renders square, full-bleed PNGs via headless Chromium — platforms mask their
own corners on touch icons, so the SVG's rounded corners are flattened first.
"""
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
SIZES = [(180, "apple-touch-icon.png"), (512, "icon-512.png")]

svg = (REPO / "static" / "icon.svg").read_text(encoding="utf-8")
svg_square = re.sub(r'rx="14"', 'rx="0"', svg)

with sync_playwright() as p:
    browser = p.chromium.launch()
    for size, name in SIZES:
        page = browser.new_page(viewport={"width": size, "height": size})
        page.set_content(
            f"<body style='margin:0'>"
            f"<div style='width:{size}px;height:{size}px'>{svg_square}</div>"
            f"</body>"
        )
        page.locator("svg").evaluate(
            "el => { el.setAttribute('width','100%'); el.setAttribute('height','100%'); }"
        )
        page.screenshot(path=str(REPO / "static" / name),
                        clip={"x": 0, "y": 0, "width": size, "height": size})
        page.close()
        print(f"wrote static/{name} ({size}x{size})")
    browser.close()
