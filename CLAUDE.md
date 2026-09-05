# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Project Overview

Margin — a self-hosted read-later server (sibling of ../footnote, same
conventions). Give it a URL and it saves the page into a folder you control:
a pixel-faithful PDF rendered in headless Chromium, clean Markdown with LaTeX
math preserved, or both. It exists because mainstream read-later apps parse
raw HTML and lose anything JavaScript rendered — MathJax and KaTeX formulas
most painfully. Margin renders the page first and captures it after the JS,
the math typesetting and the web fonts have finished.

## Layout

- `app.py` — FastAPI app: routes, token auth, the cross-site write guard, the
  save pipelines (URL → PDF/Markdown, PDF → Mathpix OCR), the math extraction
  and MathML→LaTeX conversion, the reading queue, the reader, page templates.
- `render.py` — the headless-Chromium renderer: waits for network idle, then
  MathJax/KaTeX, then fonts, then captures.
- `static/` — `style.css` (the shared paper-and-ink look),
  `service-worker.js` (offline queue and saved pages), `manifest.json`,
  `icon.svg` + generated PNGs.
- `deploy/` — `gen_icons.py` (re-render PNGs from icon.svg), systemd unit,
  `install.sh` + `add-instance.sh` (Ubuntu, one instance per person),
  `paths.sh` (shared path guards), `make-constraints.sh` (version pins).
- `tests/` — `test_margin.py`: pytest, no network or browser.
  `test_browser.py`: Playwright against a real server on a loopback port, for
  what only exists in a browser; skips without chromium.
- `description.md` — architecture doc (endpoints, output format, the math
  pipeline, design decisions); `README.md` — full user-facing reference. Keep
  both in sync with behaviour changes.

## Commands

```bash
.venv/bin/python -m pytest            # run tests (browser suite skips w/o chromium)
.venv/bin/playwright install chromium # enable tests/test_browser.py
./start.sh                            # run server (port 8000)
.venv/bin/python deploy/gen_icons.py  # regenerate icons after editing SVG
bash deploy/make-constraints.sh       # regenerate pins — on the target host only
shellcheck -x deploy/*.sh             # -x, or it stops at the sourced paths.sh
```

## Conventions

- Single-app, personal-use philosophy: no database, files on disk are the
  state, the output directory is the product.
- AGPL header on every source file, like Footnote.
- Paper-and-ink UI palette shared with Footnote (see `static/style.css`
  `:root` — the two files are meant to stay diffable).
- The server's filesystem paths stay server-side: nothing in a page, an error
  or `/health` names the output directory.
- The output directory is a **synced folder other software writes to**. Never
  trust what is in it: resolve symlinks before serving a file, and treat front
  matter as data (a recorded `source_url` is a link only if its scheme is
  http, https or mailto).
- Errors a browser can reach must render as pages with a way back. On the
  home screen there is no browser chrome to navigate with.
- Nothing in the reader may navigate away to a file: fetch it and hand it over
  as a blob instead.
- The service worker never stores a URL carrying `?token=`, and deleting an
  item tells it so before the form navigates — offline, nothing else ever
  will.
