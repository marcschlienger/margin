# Margin

> "I have discovered a truly marvelous proof of this, which this margin is
> too narrow to contain." — Pierre de Fermat, writing the original
> read-it-later note, c. 1637

Margin is a self-hosted read-it-later server. Give it a URL and it saves the
page to a folder you control — as a pixel-faithful **PDF** rendered in headless
Chromium, as clean **Markdown** with LaTeX math preserved, or both. It exists
because mainstream read-later apps (Readwise Reader, Matter, Instapaper, …)
parse the raw HTML of a page and silently strip anything rendered by
JavaScript — most painfully MathJax and KaTeX formulas. Margin renders the real
page first and captures it only after the JS, the math typesetting, and the web
fonts have finished.

It is a single small FastAPI app designed for personal use: run it on a Mac or
an Ubuntu server, point an iOS Shortcut, `curl`, or any HTTP client at it, and
collect the results in a synced folder (iCloud → Obsidian, Nextcloud, Syncthing
— anything that watches a directory).

## How it works

```
 iPhone Share Sheet / curl / RSS reader
        │  HTTP POST (LAN or Tailscale)
        ▼
 ┌─────────────────────── Margin (FastAPI, port 8000) ───────────────────────┐
 │                                                                           │
 │  POST /save ──► headless Chromium (Playwright)                            │
 │                   goto → network idle → MathJax/KaTeX done → fonts ready  │
 │                   └─► page.pdf()  ──────────────────────────►  .pdf       │
 │                                                                           │
 │  POST /save-url ─► fetch HTML ─► math extraction ─► trafilatura ─► .md    │
 │                    (falls back to the Chromium DOM     + .tex/.org        │
 │                     for SPAs and bot-walled fetches)   via Pandoc         │
 │                                                                           │
 │  POST /save-pdf ─► Mathpix OCR API ─► Mathpix Markdown ─►  .md            │
 │                                                                           │
 └────────────────────────────► OUTPUT_DIR ◄────────────────────────────────┘
                    (e.g. an iCloud/Nextcloud folder synced to your notes app)
```

The PDF path is the general-purpose one: it works on any page, including
client-side-rendered SPAs, and preserves exactly what a browser shows. The
Markdown path is the "clean text for my notes app" one: it extracts the
article body and converts every math representation it finds back to LaTeX
source (`$...$` / `$$...$$`), covering Wikipedia/MediaWiki annotations,
MathJax 2 script tags, MathJax 3 rendered containers (recovered from assistive
MathML), KaTeX annotations, raw presentation MathML (converted structurally),
`alt`-text formula images, and stray Unicode math in prose. The strategies are
documented in detail in [description.md](description.md).

## API

All save endpoints respond with HTTP 200 and a JSON body containing
`"status": "ok"` or `"status": "error"` — errors are in-band so that iOS
Shortcuts can display the message instead of failing silently.

### `POST /save` — save any page (the general endpoint)

```json
{ "url": "https://…", "formats": ["pdf", "md"] }
```

`formats` is optional and defaults to `["pdf"]`.

- **`pdf`** — renders the page in headless Chromium and exports A4 PDF with
  screen CSS and backgrounds. The renderer waits, in order: DOM content
  loaded (≤ 60 s) → network idle (best effort, ≤ 15 s) → MathJax 2/3
  typesetting finished via their JS promises/queues and
  `document.fonts.ready` (≤ 20 s) → 0.5 s settle. URLs that already point at
  a PDF (by content type or extension) are downloaded and stored as-is, with
  the title taken from the arXiv abstract page (for arXiv links) or the PDF's
  embedded metadata. Bot-challenge interstitials ("Just a moment…") and
  soft-404 pages are detected by title/status and reported as errors, never
  saved.
- **`md`** — additionally runs the Markdown pipeline below.

Re-saving a URL that is already in the inbox or archive is skipped: the
response carries `"duplicate": true` plus the existing files, instead of
piling up `-2`/`-3` copies. Pass `"force": true` (or `&force=true` on
`/save-page`) to save again anyway. Saved URLs are tracked in a
`.saved-urls.json` index inside the output directory, with a fallback match
on `source_url` in Markdown frontmatter for files saved before the index
existed.

```json
{ "status": "ok", "title": "Fourier transform",
  "files": ["2026-07-18-fourier-transform.pdf", "2026-07-18-fourier-transform.md"],
  "path": "/var/lib/margin/inbox" }
```

On partial success a `"warnings"` array lists what failed; on total failure
`"status": "error"` with a `"message"`.

### `POST /save-url` — Markdown pipeline

```json
{ "url": "https://…" }
```

Fetches the raw HTML (fast — MathJax/KaTeX sites ship the LaTeX source in the
initial HTML), extracts the article with math converted to LaTeX, and writes
`.md` plus companion `.tex` and `.org` files (via Pandoc, if installed). If
the plain fetch is bot-blocked (401/403/406/429/503) or the extracted body is
nearly empty (client-side-rendered app), it automatically retries from the
fully rendered Chromium DOM. Responds with
`{"status": "ok", "filename": …, "title": …, "path": …}`.

The URL cleaning tolerates iOS Shortcuts quirks (inserted whitespace,
duplicated URL); only `http(s)` URLs are accepted.

### `POST /save-pdf` — PDF upload → Markdown

Multipart form upload, field `file`, ≤ 50 MB. Converts via the
[Mathpix](https://mathpix.com) `/v3/pdf` OCR API (polls up to 3 minutes) —
best-in-class for math PDFs, requires `MATHPIX_APP_ID`/`MATHPIX_APP_KEY`.
Without credentials the rest of the server works; only this endpoint errors.

### `GET /` — built-in reading queue

A minimal web UI over the output directory: every saved item with its date,
title (from the Markdown frontmatter when available), links to its files and
original source, a quick-save box, a client-side title filter, and an
**Archive** button that moves an item's files into an `archive/` subfolder
(with a restore view at `/?view=archive`). Follows the system light/dark
theme. With this, any browser is a functional read-later front end — no notes
app or third-party service required.

Supporting endpoints: `GET /files/{name}` serves saved files (inbox or
archive; only `.pdf/.md/.tex/.org`, no path traversal), and `POST /archive`
(form fields `stem`, `action=archive|restore`) moves items.

### `GET /save-page` — bookmarklet target

Same pipeline as `POST /save`, but GET with query parameters
(`?url=…&formats=pdf,md`) and an HTML result page instead of JSON — made to
be opened as a browser tab by the desktop bookmarklet (see below).

### `GET /health`

```json
{ "status": "ok", "output_dir": "…", "output_dir_exists": true,
  "output_dir_writable": true, "saved_md_count": 12, "saved_pdf_count": 34,
  "pandoc_available": true, "playwright_available": true,
  "mathpix_configured": false }
```

### `POST /echo`

Debug helper: echoes method, headers, and parsed body of the request back.

## Saved files

- Filenames: `YYYY-MM-DD-title-slug.{pdf,md,tex,org}`. Titles come from the
  page `<title>`/`og:title` (site-name suffixes like `… | Site` stripped);
  name collisions get `-2`, `-3`, … suffixes — nothing is ever overwritten.
- Markdown files start with YAML frontmatter:

  ```markdown
  ---
  title: "Understanding the Fourier Transform"
  source_url: "https://example.com/fourier"
  date_saved: 2026-07-18
  tags: [readlater, math]
  ---
  ```

  (the `math` tag only appears when the page actually contains math). Math
  uses `$...$` / `$$...$$`, which Obsidian renders natively.
- `.tex` companions are minimal, self-contained `pdflatex`-compilable articles;
  `.org` companions are for Emacs Org-mode. Both are derived from the `.md`
  via Pandoc and skipped gracefully when Pandoc is absent.

## Configuration

| Setting | How | Default |
|---|---|---|
| Output directory | `--output-dir` flag > `OUTPUT_DIR` env var (both also read from `.env`) | iCloud `ReadLater/inbox` on macOS, `~/ReadLater/inbox` elsewhere |
| Bind address / port | `--host` / `--port` flags, or `HOST` / `PORT` env vars | `0.0.0.0` / `8000` |
| Mathpix credentials | `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` in `.env` | unset — `/save-pdf` disabled |
| API token | `MARGIN_TOKEN` in `.env` | unset — no authentication |

## Authentication (optional)

Set `MARGIN_TOKEN` and every endpoint except `GET /health` requires it:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"  # generate one
echo "MARGIN_TOKEN=paste-it-here" >> .env                      # then restart
```

Clients can present the token three ways:

- **`Authorization: Bearer <token>` header** — preferred for curl and the iOS
  Shortcuts (one extra header field; see
  [shortcut_setup.md](shortcut_setup.md)). Keeps the token out of access logs.
- **`?token=<token>` query parameter** — for URL-only contexts like the
  bookmarklet. Query strings do appear in the server's access log.
- **Browser cookie** — open `http://YOUR-SERVER:8000/?token=<token>` once and
  the token is stored in an `HttpOnly`, `SameSite=Strict` cookie (1 year);
  after that the queue UI, file links, and archive buttons work with no
  decoration. Because the cookie is `Strict`, other websites can never ride
  it — enabling the token also closes the drive-by CSRF window that an open
  LAN server inherently has.

With a token set, the bookmarklet becomes:

```
javascript:window.open('http://YOUR-SERVER:8000/save-page?token=YOUR-TOKEN&url='+encodeURIComponent(location.href));
```

`GET /health` stays open (it reports `"auth_required": true` so clients can
detect the requirement) and CORS preflights pass through, as they carry no
credentials.

## Remote access via Tailscale

[Tailscale](https://tailscale.com) is the easiest way to use Margin away from
home without exposing it to the internet: every device gets a stable private
`100.x.y.z` address inside an encrypted WireGuard mesh, and nothing is
reachable from the public internet.

**1. Install it on the server and your devices.**

```bash
# Ubuntu server
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

iPhone/iPad/Mac: install the Tailscale app and sign in to the same tailnet.

**2. Find the server's tailnet address.**

```bash
tailscale ip -4        # e.g. 100.101.102.103
tailscale status       # shows the machine name, e.g. "margin-box"
```

With MagicDNS (on by default for new tailnets) the name works too:
`http://margin-box:8000`.

**3. Point every client at it.** Use the tailnet address instead of the LAN
IP in the iOS Shortcuts (`http://100.101.102.103:8000/save-url`), the
bookmarklet, and the queue page. It works identically at home and away — no
special-casing, no port forwarding, no dynamic DNS.

**4. Choose how tight to lock it down.** Two good setups:

- **Simple (recommended):** keep the default bind (`0.0.0.0`), set
  `MARGIN_TOKEN`. Reachable on LAN and tailnet, but every request needs the
  token. On a tailnet you share with others, also restrict port 8000 to your
  own devices in the Tailscale admin ACLs.
- **Tailnet-only + HTTPS:** bind Margin to loopback and let Tailscale proxy
  it with a real TLS certificate:

  ```bash
  # in /etc/systemd/system/margin.service: ExecStart ... --host 127.0.0.1
  sudo systemctl daemon-reload && sudo systemctl restart margin
  sudo tailscale serve --bg 8000
  ```

  Margin is now at `https://margin-box.<your-tailnet>.ts.net` (no port, note
  the **https**) and unreachable from the LAN or anywhere outside the
  tailnet. The HTTPS URL has a bonus: browsers no longer treat the server as
  mixed content, so `fetch()`-based clients and extensions work from https
  pages too. `MARGIN_TOKEN` is still worth setting on shared tailnets.

The iOS Shortcuts, the bookmarklet, and `curl` all work unchanged over
Tailscale — only the address (and with `tailscale serve`, the scheme)
changes.

## Install

Requirements: Python ≥ 3.10. Optional: `pandoc` (for `.tex`/`.org`
companions), Mathpix credentials (for `/save-pdf`).

### Local / macOS

```bash
git clone https://codeberg.org/blutlauge/margin.git && cd margin
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # headless browser for /save
cp .env.example .env                 # optional: OUTPUT_DIR, Mathpix keys

python app.py --output-dir ~/ReadLater/inbox
curl http://localhost:8000/health
```

Without Playwright installed the server still runs: `/save` (PDF) returns an
instructive error and `/save-url` loses only its rendered-DOM fallback.

For auto-start at login on macOS, `start.sh` is a launchd-friendly wrapper
(see the LaunchAgent notes in [description.md](description.md)).

### Ubuntu server (systemd)

From a checkout on the server:

```bash
sudo bash deploy/install.sh
```

The installer is idempotent and: installs `python3-venv`/`pandoc`, creates a
`margin` system user, copies the app to `/opt/margin`, builds the venv,
installs headless Chromium **with its system dependencies**, writes output to
`/var/lib/margin/inbox`, and enables the `margin` systemd service
([deploy/margin.service](deploy/margin.service)). Defaults are overridable:

```bash
sudo APP_DIR=/opt/margin OUTPUT_DIR=/srv/margin/inbox SERVICE_USER=margin \
  bash deploy/install.sh
```

Day-2 operations:

```bash
systemctl status margin
journalctl -u margin -f
sudo systemctl restart margin        # e.g. after editing /opt/margin/.env
```

The service listens on all interfaces; run it on a private network (LAN,
Tailscale, WireGuard) and/or set `MARGIN_TOKEN` — see
[Authentication](#authentication-optional) and
[Remote access via Tailscale](#remote-access-via-tailscale) above.

## Capture clients

- **iOS Share Sheet** — two small Shortcuts ("Save to Margin" → `/save-url`,
  "Save PDF to Margin" → `/save-pdf`); build instructions in
  [shortcut_setup.md](shortcut_setup.md).
- **Desktop browser (macOS / Linux / Windows)** — a bookmarklet. Create a new
  bookmark in any browser and set its URL to (replace `YOUR-SERVER`):

  ```
  javascript:window.open('http://YOUR-SERVER:8000/save-page?url='+encodeURIComponent(location.href));
  ```

  Clicking it while reading any page opens a small tab that saves the page as
  PDF, reports the result, and closes itself. Append `&formats=pdf,md` inside
  the quoted URL to also get Markdown. (The bookmarklet navigates instead of
  using `fetch()` because browsers block mixed-content requests from https
  pages to a plain-http LAN server; opening a tab is always allowed. The
  server also sends permissive CORS headers, so extension-based or
  `fetch`-based clients work too wherever mixed content isn't an issue.)
- **The built-in queue page** — open `http://YOUR-SERVER:8000/` in any
  browser: paste a URL to save, read via the file links, archive when done.
- **Anything that speaks HTTP** — `curl`, RSS-reader automations, Raycast, a
  cron job:

  ```bash
  curl -X POST http://server:8000/save \
    -H 'Content-Type: application/json' \
    -d '{"url": "https://en.wikipedia.org/wiki/Fourier_transform"}'
  ```

## Limitations & roadmap

- Sites behind aggressive bot protection (e.g. Cloudflare-challenged domains)
  may refuse the headless browser; Margin detects this and returns a clear
  error instead of saving the challenge page. Workaround: print the page to
  PDF on-device and use `/save-pdf`.
- Authentication is optional (`MARGIN_TOKEN`) and coarse — one shared token,
  no users or rate limiting. Keep the server on a private network (LAN,
  Tailscale, WireGuard) regardless.
- Planned: optional upload of saved PDFs to Readwise Reader via its API.

## Repository layout

| Path | Purpose |
|---|---|
| `app.py` | FastAPI server: endpoints, math extraction, Markdown pipeline |
| `render.py` | Playwright wrapper: rendered HTML + PDF export, wait logic |
| `tests/` | Unit tests (`pip install -r requirements-dev.txt && python -m pytest`) |
| `deploy/` | Ubuntu installer and systemd unit |
| `description.md` | Architecture and the seven math-extraction strategies |
| `shortcut_setup.md` | Step-by-step iOS Shortcut construction |
| `start.sh` | launchd-friendly start wrapper (macOS) |

## License

Margin is free software, licensed under the
[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-or-later).
You may run, study, modify, and share it; if you offer a modified version
as a network service, you must make your modified source available to its
users. Copyright © 2026 Marc Schlienger.
