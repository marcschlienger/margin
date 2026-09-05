# Margin — self-hosted read-later server that preserves JS/math rendering.
# Copyright (C) 2026 Marc Schlienger
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Headless-Chromium page rendering via Playwright.

Loads a page in headless Chromium, waits for client-side rendering to finish —
including MathJax / KaTeX typesetting and web-font loading — then returns the
rendered HTML plus a PDF export of the page.

Playwright is an optional dependency: the module imports cleanly without it,
and `Renderer.available` reports whether rendering is possible. Install with:

    pip install playwright
    playwright install --with-deps chromium
"""
from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from typing import Callable

try:
    from playwright.async_api import (
        Browser,
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
    PLAYWRIGHT_AVAILABLE = True
except ModuleNotFoundError:
    PLAYWRIGHT_AVAILABLE = False

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Resolves once math typesetting and web fonts are done. Covers MathJax 3
# (startup.promise resolves after the initial typeset pass), MathJax 2 (a
# callback pushed on the Hub queue runs after all pending typesets), and KaTeX
# (renders synchronously during page load — nothing to wait for beyond fonts).
_WAIT_FOR_MATH_JS = """
async () => {
  if (document.fonts && document.fonts.status !== 'loaded') {
    try { await document.fonts.ready; } catch (e) {}
  }
  const mj = window.MathJax;
  if (mj) {
    if (mj.startup && mj.startup.promise) {
      try { await mj.startup.promise; } catch (e) {}
    } else if (mj.Hub && typeof mj.Hub.Queue === 'function') {
      await new Promise((resolve) => mj.Hub.Queue(resolve));
    }
  }
  return true;
}
"""


# Titles of bot-wall / JS-challenge interstitials (Cloudflare and friends).
_RE_CHALLENGE_TITLE = re.compile(
    r"just a moment|attention required|access denied|verify you are"
    r"|checking your browser|are you a robot|captcha",
    re.I,
)


def looks_blocked(title: str) -> bool:
    """True if a page title is a bot-challenge interstitial, not real content."""
    return bool(_RE_CHALLENGE_TITLE.search(title or ""))


_RE_NOT_FOUND_TITLE = re.compile(r"page not found|\b404\b", re.I)


def looks_missing(title: str, status: int) -> bool:
    """True if the rendered page is an error page rather than content.

    HTTP status alone is unreliable: some sites return 403 to headless
    browsers while serving a soft-404 body, so the title is checked too.
    """
    return status in (404, 410) or bool(_RE_NOT_FOUND_TITLE.search(title or ""))


@dataclass
class RenderResult:
    html: str
    title: str
    pdf: bytes
    status: int  # HTTP status of the main navigation (0 if unknown)


class RendererUnavailable(RuntimeError):
    """Playwright or its Chromium build is not installed."""


class Renderer:
    """Shared Chromium instance, launched lazily on first render.

    One fresh browser context per render (no cookie/state bleed between
    saves); a semaphore caps concurrent renders to bound memory use.
    """

    def __init__(self, max_concurrent: int = 2,
                 url_allowed: Callable[[str], bool] | None = None):
        self._playwright = None
        self._browser: Browser | None = None
        self._launch_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrent)
        self._url_allowed = url_allowed

    @property
    def available(self) -> bool:
        return PLAYWRIGHT_AVAILABLE

    async def _ensure_browser(self) -> Browser:
        if not PLAYWRIGHT_AVAILABLE:
            raise RendererUnavailable(
                "playwright is not installed — run: pip install playwright && "
                "playwright install --with-deps chromium"
            )
        async with self._launch_lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(headless=True)
            except PlaywrightError as e:
                raise RendererUnavailable(
                    f"Could not launch Chromium: {e} — "
                    "run: playwright install --with-deps chromium"
                ) from e
            return self._browser

    async def render(self, url: str, timeout_s: float = 60.0) -> RenderResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s

        def remaining(cap: float | None = None) -> float:
            left = deadline - loop.time()
            if left <= 0:
                raise PlaywrightTimeoutError(
                    f"render exceeded its {timeout_s:g}s deadline"
                )
            return min(left, cap) if cap is not None else left

        browser = await asyncio.wait_for(self._ensure_browser(), remaining())
        await asyncio.wait_for(self._sem.acquire(), remaining())
        try:
            context = await asyncio.wait_for(
                browser.new_context(
                    user_agent=CHROME_UA,
                    viewport={"width": 1280, "height": 1024},
                ),
                remaining(),
            )
            try:
                if self._url_allowed is not None:
                    async def enforce_url_policy(route):
                        # The gate may need to resolve a name to answer, so
                        # it is allowed to be a coroutine.
                        verdict = self._url_allowed(route.request.url)
                        if inspect.isawaitable(verdict):
                            verdict = await verdict
                        if verdict:
                            await route.continue_()
                        else:
                            await route.abort("blockedbyclient")

                    # This sees the main navigation again after every redirect
                    # and all subresources. A public page therefore cannot use
                    # Chromium as a bridge to a loopback/LAN service.
                    await asyncio.wait_for(
                        context.route("**/*", enforce_url_policy), remaining()
                    )
                page = await asyncio.wait_for(context.new_page(), remaining())
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=remaining() * 1000
                )
                status = response.status if response else 0
                # Best effort: pages with long-polling/analytics never go idle.
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=remaining(15) * 1000
                    )
                except PlaywrightTimeoutError:
                    pass
                try:
                    await asyncio.wait_for(
                        page.evaluate(_WAIT_FOR_MATH_JS), timeout=remaining(20)
                    )
                except (asyncio.TimeoutError, PlaywrightError):
                    pass
                await page.wait_for_timeout(remaining(.5) * 1000)

                title = await asyncio.wait_for(page.title(), remaining())
                # JS challenges sometimes clear on their own after a moment —
                # give them a few seconds before capturing.
                for _ in range(8):
                    if not looks_blocked(title):
                        break
                    await page.wait_for_timeout(remaining(1) * 1000)
                    title = await asyncio.wait_for(page.title(), remaining())
                html = await asyncio.wait_for(page.content(), remaining())
                # Print stylesheets on blogs often hide content or math; export
                # the PDF with screen CSS instead.
                await asyncio.wait_for(
                    page.emulate_media(media="screen"), remaining()
                )
                pdf = await asyncio.wait_for(
                    page.pdf(
                        format="A4",
                        print_background=True,
                        margin={
                            "top": "15mm", "bottom": "15mm",
                            "left": "12mm", "right": "12mm",
                        },
                    ),
                    remaining(),
                )
                return RenderResult(html=html, title=title, pdf=pdf, status=status)
            finally:
                await context.close()
        finally:
            self._sem.release()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
