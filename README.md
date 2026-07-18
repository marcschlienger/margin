# Margin

> "I have discovered a truly marvelous proof of this, which this margin is
> too narrow to contain." — the original read-it-later note

Self-hosted read-it-later server that saves **any** web page with client-side
rendering — including MathJax / KaTeX formulas — intact. Pages are rendered in
headless Chromium (Playwright), which waits for JS and math typesetting to
finish before exporting a **PDF**; a Markdown-extraction pipeline (with LaTeX
math preserved) and Mathpix-based PDF→Markdown conversion are also available.

Why: mainstream read-later apps (Readwise Reader, Matter, Instapaper) parse the
raw HTML and strip JS-rendered math. Rendering the real page first sidesteps
that entirely.

## Endpoints

| Endpoint | Input | Output |
|---|---|---|
| `POST /save` | `{"url": "…", "formats": ["pdf", "md"]}` (formats optional, default `["pdf"]`) | Renders the page in headless Chromium, saves `YYYY-MM-DD-title-slug.pdf`; `"md"` additionally runs the Markdown pipeline. URLs that point directly at a PDF are stored as-is. |
| `POST /save-url` | `{"url": "…"}` | Markdown pipeline only: extracts article + math to `.md` (+ `.tex`/`.org` via Pandoc). Falls back to headless rendering when the page is client-side rendered or the plain fetch is blocked. |
| `POST /save-pdf` | multipart form, field `file` | PDF → Markdown via the Mathpix OCR API (needs `MATHPIX_*` credentials). |
| `GET /health` | – | Status: output dir, Pandoc/Playwright/Mathpix availability, saved-file counts. |
| `POST /echo` | anything | Debug: echoes the request back. |

Save responses are always HTTP 200 with `{"status": "ok"|"error", …}` so iOS
Shortcuts can surface the message. Example:

```bash
curl -X POST http://localhost:8000/save \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://en.wikipedia.org/wiki/Fourier_transform", "formats": ["pdf", "md"]}'
```

## Output directory

Priority: `--output-dir` flag → `OUTPUT_DIR` env var (also read from `.env`) →
platform default (`~/Library/Mobile Documents/com~apple~CloudDocs/ReadLater/inbox`
on macOS for the iCloud→Obsidian workflow, `~/ReadLater/inbox` elsewhere).

## Quickstart (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # headless browser for /save
cp .env.example .env               # optional: OUTPUT_DIR, Mathpix credentials

python app.py --output-dir ~/ReadLater/inbox   # defaults: 0.0.0.0:8000
curl http://localhost:8000/health
```

`pandoc` on the PATH is optional — it adds companion `.tex`/`.org` files to
every Markdown save. Without Playwright the server still runs; `/save` (PDF)
returns an error and `/save-url` loses its rendered-page fallback.

## Deploy on Ubuntu (systemd)

On the server, from a checkout of this repo:

```bash
sudo bash deploy/install.sh
```

This installs the app to `/opt/margin`, creates a `margin` system user, sets
up the venv, installs headless Chromium with its system dependencies, writes
files to `/var/lib/margin/inbox`, and enables the `margin` systemd service.
Customize via env vars:

```bash
sudo APP_DIR=/opt/margin OUTPUT_DIR=/srv/margin/inbox bash deploy/install.sh
```

Operate it with:

```bash
systemctl status margin
journalctl -u margin -f
curl http://localhost:8000/health
```

Edit `/opt/margin/.env` for Mathpix credentials, then
`sudo systemctl restart margin`. The unit file lives at
[deploy/margin.service](deploy/margin.service).

## macOS auto-start (legacy iCloud workflow)

```bash
launchctl load ~/Library/LaunchAgents/com.marc.math-readlater.plist
```

If launchd reports a permission error on `.venv/pyvenv.cfg`, grant Full Disk
Access to `/bin/bash` in System Settings → Privacy & Security.

## Day-to-day use

- iPhone → Share Sheet → **Math Inbox — URL** / **Math Inbox — PDF** shortcuts
  (see [shortcut_setup.md](shortcut_setup.md)); they POST to `/save-url` and
  `/save-pdf`.
- Bookmarklet / browser extension: `POST /save` with the current tab's URL.
- Failed saves return the error message in the response body; server logs go
  to stderr (journald on Ubuntu, `server.log` under launchd).

For architecture and the math-extraction strategies, see
[description.md](description.md).
