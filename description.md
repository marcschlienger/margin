# Margin — App Description

## Purpose

Margin is a self-hosted read-later server (macOS or Ubuntu) that captures web articles
and PDFs — including math-heavy ones — and saves them into a configurable
output folder (`OUTPUT_DIR` / `--output-dir`; defaults to an iCloud Drive
folder on macOS that syncs to Obsidian).

Standard read-it-later tools (Readwise, Pocket, Instapaper) strip or mangle
JS-rendered content and mathematical notation. This pipeline preserves both:
`POST /save` renders the page in headless Chromium (Playwright), waits for
JS/MathJax/KaTeX typesetting to finish, and exports a pixel-faithful PDF;
`POST /save-url` extracts a clean `.md` with LaTeX math preserved.

---

## Architecture

```
iPhone Share Sheet / desktop bookmarklet / curl
      │
      │  HTTP (Tailscale or local network)
      ▼
Margin — FastAPI server, port 8000  (Ubuntu, macOS, or any Linux)
      │
      ├── /save     ──► headless Chromium ──► JS+math rendered ──► .pdf
      ├── /save-url ──► fetch HTML ──► extract content + math ──► .md (+.tex/.org)
      ├── /save-pdf ──► Mathpix API ──► poll for MMD ──► .md
      │
      └── /  (reading queue UI: list, read, archive)
                                              │
                                              ▼
                                        OUTPUT_DIR
                          (a plain folder — sync it with iCloud,
                           Nextcloud, or Syncthing, or read via /)
```

On Ubuntu the server runs as a systemd service (`deploy/margin.service`,
installed by `deploy/install.sh`); on macOS it can run as a Launch Agent so it
starts at login and restarts on crash. Capture happens from iOS via two Apple
Shortcuts (one for URLs, one for PDFs), from desktop browsers via a
bookmarklet hitting `/save-page`, or from anything that can POST JSON.

---

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI server — endpoints, extraction and conversion logic |
| `render.py` | Headless-Chromium rendering (Playwright): rendered HTML + PDF export |
| `deploy/install.sh` | Ubuntu installer — venv, Chromium, systemd service |
| `deploy/margin.service` | systemd unit for the Ubuntu deployment |
| `requirements.txt` | Python dependencies |
| `.env` | Config: Mathpix credentials, `OUTPUT_DIR`, `MARGIN_TOKEN` |
| `.env.example` | Template for `.env` — copy to `.env` and fill in |
| `start.sh` | Start wrapper (used by the macOS Launch Agent; runs `app.py`) |
| `shortcut_setup.md` | Step-by-step instructions for building the iOS Shortcuts |
| `LICENSE` | GNU AGPL v3.0 |
| `~/Library/LaunchAgents/…plist` | Launch Agent definition (macOS auto-start only; not in the repo) |

### Setting up Mathpix credentials

The PDF endpoint (`/save-pdf`) calls the Mathpix API. To enable it:

1. Sign up at [mathpix.com](https://mathpix.com) → API Console.
2. Create an organisation and grab the `app_id` and `app_key` from the
   "API Keys" section.
3. Copy `.env.example` to `.env` and paste the values:
   ```
   MATHPIX_APP_ID=your_app_id_here
   MATHPIX_APP_KEY=your_app_key_here
   ```
4. Restart the server. `GET /health` will now show
   `"mathpix_configured": true`.

If `.env` is missing or the values are blank, `/save-url` keeps working —
only `/save-pdf` returns an error.

---

## Server Endpoints

> If the `MARGIN_TOKEN` environment variable is set, every endpoint except
> `GET /health` requires the token — via `Authorization: Bearer` header,
> `?token=` query parameter, or the browser cookie set after one
> authenticated visit. See the README's Authentication section.

### `POST /save`

**Input:** JSON body `{ "url": "https://...", "formats": [...] }` —
`formats` optional (default `["pdf"]`), any subset of `pdf`, `md`, `tex`,
`org`, each selected independently.

**Process:**
1. If the URL serves a PDF directly (content type or `.pdf` extension), the
   file is downloaded and stored as-is.
2. Otherwise the page is loaded in headless Chromium: waits for
   DOM-content-loaded, then network idle (best effort), then MathJax 2/3
   typesetting via their JS promises/queues and `document.fonts.ready`.
3. Bot-challenge interstitials ("Just a moment…" etc.) are detected by title
   and reported as errors instead of being saved.
4. The rendered page is exported as `YYYY-MM-DD-title-slug.pdf` (screen CSS,
   A4, backgrounds on) into the output directory.
5. With any of `"md"`, `"tex"`, `"org"` in `formats`, the Markdown pipeline
   below also runs and writes exactly the selected text formats (`tex`/`org`
   are derived via Pandoc and don't require `md`).

**Returns:** `{ "status": "ok", "title": "...", "files": [...], "path": "..." }`,
or `{ "status": "error", "message": "..." }` — always HTTP 200.

---

### `POST /save-url`

**Input:** JSON body `{ "url": "https://..." }`

**Process:**
1. Cleans the URL — iOS Shortcuts sometimes serialises long URLs with
   whitespace or even duplicates the value with a newline between copies. The
   server splits on whitespace and keeps only the first token.
2. Fetches the page HTML using a Chrome user-agent string (avoids bot blocking).
3. Passes the HTML through the math extraction pipeline (see below).
4. Writes the `.md` file to the output directory (see `OUTPUT_DIR` /
   `--output-dir`), also generating companion `.tex` and `.org` files via
   Pandoc.

**Returns (success):** `{ "status": "ok", "filename": "...", "title": "..." }`

**Returns (error):** `{ "status": "error", "filename": "", "message": "..." }`
— always HTTP 200, so iOS Shortcuts can read the body and surface the error in
a notification. (HTTP 4xx/5xx would otherwise abort the Shortcut silently.)

---

### `POST /save-pdf`

**Input:** multipart form upload, field name `file`, containing a PDF.

**Process:**
1. Validates Mathpix credentials are configured and the upload is `<= 50 MB`.
2. Uploads the PDF to `https://api.mathpix.com/v3/pdf`.
3. Polls `GET /v3/pdf/{pdf_id}` every 3 seconds until status is `completed`
   (timeout: 3 minutes / 60 polls).
4. Downloads the result as Mathpix Markdown (MMD) from `/v3/pdf/{pdf_id}.mmd`.
5. Extracts the document title from the first `#` heading, or falls back to the
   PDF filename stem (with site-suffix stripping applied).
6. Writes the `.md` file to the output directory plus companion `.tex` and `.org`.

**Mathpix options used:**
- Output format: MMD (Mathpix Markdown — a superset of standard Markdown)
- Inline math delimiters: `$...$`
- Display math delimiters: `$$...$$`
- `rm_spaces: true` (cleaner output)

**Returns:** same shape as `/save-url` — `{"status":"ok",…}` on success, or
`{"status":"error","filename":"","message":"..."}` on any failure.

---

### `GET /save-page`

Bookmarklet-facing variant of `POST /save`: query parameters
(`?url=…&formats=pdf,md`) instead of JSON, and an HTML result page instead of
a JSON body. Desktop bookmarklets open it in a new tab because browsers block
`fetch()` from https pages to a plain-http LAN server (mixed content), while
top-level navigation is always allowed. The tab closes itself on success.

---

### `GET /` — reading queue

Lists every saved item in the output directory: date, title (from Markdown
frontmatter when present), links to each file and the original source, a
quick-save form, and per-item Archive buttons. `/?view=archive` shows archived
items with Restore buttons. Supporting endpoints: `GET /files/{name}` serves
saved files (`.pdf/.md/.tex/.org` only, traversal-safe), and `POST /archive`
(form fields `stem`, `action=archive|restore`) moves an item's files between
the output directory and its `archive/` subfolder.

---

### `GET /health`

Returns server status, the output directory (path / exists / writable),
saved-file counts, and whether Pandoc, Playwright, and Mathpix are available.
Used to verify the server is running before testing Shortcuts.

---

### `POST /echo`

Debug endpoint. Returns the method, headers, and parsed body of whatever request
was sent. Used to verify that an iOS Shortcut is sending the correct payload before
pointing it at `/save-url` or `/save-pdf`.

---

## Output Format

Every saved file is a Markdown document with YAML frontmatter:

```markdown
---
title: "Understanding the Fourier Transform"
source_url: "https://example.com/fourier"
date_saved: 2026-04-26
tags: [readlater, math]
---

# Understanding the Fourier Transform

The Fourier transform of $f(t)$ is:

$$
\hat{f}(\xi) = \int_{-\infty}^{\infty} f(t)\, e^{-2\pi i \xi t}\, dt
$$
```

Filenames follow the pattern `YYYY-MM-DD-title-slug.md`, e.g.
`2026-04-26-understanding-the-fourier-transform.md`. If a file with the same
name already exists in the inbox, a `-2`, `-3`, ... suffix is appended so older
saves are never overwritten. Titles are de-suffixed (` | Site Name`,
` — Site Name`, ` - Site Name` are stripped) before slugifying.

Math uses standard LaTeX delimiters (`$...$` inline, `$$...$$` display) which
render natively in Obsidian via its built-in MathJax renderer, and are also
compatible with Pandoc for PDF export.

### Companion `.tex` and `.org` files

Each save also produces two derived files alongside the `.md`:

- **`.tex`** — a minimal LaTeX article. The body is generated via
  `pandoc -f gfm+yaml_metadata_block -t latex` (no `--standalone`) and wrapped
  in a hand-rolled 12-line preamble (`amsmath`, `amssymb`, `geometry`,
  `hyperref`, `lmodern`). This avoids Pandoc's 50+-line standalone template
  which pulls in `microtype`, `fontspec`/`unicode-math`, and KOMA settings
  that fail under plain `pdflatex`. Footnote-arrow Unicode characters
  (`↩ ↪ ↺ ↻ ⏎`) are stripped before insertion. Inside math, `%` is escaped
  to `\%` (it would otherwise act as a LaTeX comment character and break the
  document). Editor magic comments — `% !TEX TS-program = pdflatex` for
  TeXShop, `%%% TeX-master: t` for AUCTeX — are included so the file
  compiles in any common Mac LaTeX editor without configuration.
- **`.org`** — an Org-mode document, generated via the same Pandoc flavour
  with no `--standalone` (the Org writer produces a complete file already).

If `pandoc` is not on the `PATH`, conversion is skipped silently and the
`.md` is still saved. Pandoc errors and timeouts are logged to the server
log so silent corruption is detectable.

---

## Math Extraction Pipeline (URLs)

Web pages use many different technologies to render math. The pipeline handles them
in priority order, all in a single HTML pre-processing pass before content
extraction.

> `/save-url` first fetches the raw HTML (fast; KaTeX/MathJax sites ship the
> LaTeX source as text in the initial HTML). If the fetch is blocked or the
> extracted body is nearly empty — a client-side-rendered single-page app —
> it automatically retries with the headless-Chromium renderer and extracts
> from the fully rendered DOM instead. For a pixel-faithful copy of any page,
> use `POST /save` (PDF export).

### Strategy 1 — Wikipedia / MediaWiki
**Markup:** `<span class="mwe-math-element-inline">` or `mwe-math-element-block`
containing `<annotation encoding="application/x-tex">LATEX</annotation>`

Wikipedia stores the original LaTeX source inside a hidden `<annotation>` element
alongside the rendered MathML. The pipeline extracts this, strips the
`{\displaystyle ...}` wrapper that Wikipedia adds to every formula, and replaces
the entire span with `$LATEX$` (inline) or `$$LATEX$$` (display).

### Strategy 1b — MathJax 3 rendered output
**Markup:** `<mjx-container>` elements produced by client-side MathJax 3
typesetting, containing an assistive-MathML copy (`<mjx-assistive-mml>`).

Relevant when extracting from the *rendered* DOM (the headless-Chromium
fallback): after MathJax 3 has typeset a page, the original TeX is no longer
present as text. The pipeline recovers it from the assistive MathML inside the
container — preferring an embedded `x-tex` annotation, then `alttext`, then
structural MathML→LaTeX conversion — and replaces the whole container
(including its SVG/CHTML rendering) with the LaTeX. Runs before Strategy 2 so
the generic `<math>` pass doesn't leave duplicate renderings behind.

### Strategy 2 — General MathML (with embedded LaTeX)
**Markup:** `<math>` element with `<annotation encoding="application/x-tex">` or
`alttext` attribute.

Used by sites that serve MathML directly without the Wikipedia wrapper. Same
extraction as above; inline vs display determined by the `display` attribute on
the `<math>` element.

### Strategy 2b — Raw MathML (structural conversion)
**Markup:** `<math>` element with **no** annotation and no `alttext` — pure
presentation MathML (`<mfrac>`, `<msup>`, `<msub>`, `<mrow>`, `<mi>`, `<mn>`,
`<mo>`, `<msqrt>`, `<mover>`, etc.).

When neither LaTeX source nor alttext is available, the pipeline walks the
MathML tree recursively and emits LaTeX directly: `<mfrac>` → `\frac{...}{...}`,
`<msup>` → `{...}^{...}`, `<mi>` containing a function name (`ln`, `sin`, …) →
`\ln`, `\sin`, etc. Greek letters and other Unicode math symbols are mapped via
the same table used by Strategy 7.

### Strategy 3 — KaTeX
**Markup:** `<span class="katex">` containing
`<annotation encoding="application/x-tex">`.

KaTeX (used by many modern math blogs and documentation sites) also embeds the
LaTeX source in an annotation element. Inline vs display is determined by whether
the span has a `.katex-display` ancestor.

### Strategy 4 — MathJax 2
**Markup:** `<script type="math/tex">` (inline) and
`<script type="math/tex; mode=display">` (display).

The legacy MathJax 2 renderer stores LaTeX in hidden `<script>` tags in the DOM.
Display blocks use the more specific `mode=display` type string and are processed
first to avoid misclassification.

### Strategy 5 — Image-rendered math
**Markup:** `<img src="formula.svg" alt="LATEX">` (or PNG)

Used by sites that pre-render formulas as SVG or PNG images and store the original
LaTeX in the image's `alt` attribute. Detected by checking whether the `alt` text
contains LaTeX command patterns (`\command` or `_{`/`^{`). Classified as display
math when the image is the sole content of its parent block element (`<p>`,
`<div>`, `<figure>`, etc.).

### Strategy 6 — MathJax 3 / KaTeX raw delimiters
**Markup:** `\(...\)` and `\[...\]` appearing as plain text in the extracted body.

Some sites configure MathJax 3 or KaTeX to leave the raw delimiters in the HTML
text rather than rendering them server-side. These are normalised to `$...$` and
`$$...$$` after trafilatura extraction via regex substitution.

### Strategy 7 — Unicode math symbols in prose
**Markup:** Plain Unicode characters (`θ`, `ℓ`, `π`, `≈`, `∫`, etc.) used directly
in text by authors who do not use a math renderer for inline variables.

A 56-symbol mapping table converts Greek letters and common math operators to their
LaTeX equivalents. Symbols are wrapped in `$...$` when they appear as isolated
tokens (not part of a word). `<sub>` and `<sup>` elements are pre-converted to
`_{...}` and `^{...}` LaTeX notation before extraction so subscripts and
superscripts survive the trafilatura pass. After wrapping, adjacent `$X$_{n}`
patterns are merged into `$X_{n}$`.

---

## Content Extraction

After the math pre-processing pass, the modified HTML is passed to
[trafilatura](https://trafilatura.readthedocs.io/) for main-content extraction.
Trafilatura identifies the article body, removes navigation, ads, headers, and
footers, and outputs Markdown with headings, bold, italic, and lists preserved.

---

## iOS Shortcuts

Two shortcuts connect the iOS Share Sheet to the server (see
`shortcut_setup.md` for full build instructions). Each is a thin client: it
POSTs the URL or PDF to the server — wherever it runs — and shows a
notification with the saved filename. All conversion logic stays server-side.

- **Save to Margin**: Share Sheet type = URLs. POSTs `{"url": "..."}` to
  `/save-url` with an `Accept: application/json` header.
- **Save PDF to Margin**: Share Sheet type = PDFs. POSTs the file as multipart
  form data to `/save-pdf`.

Both include an explicit "Get Dictionary from Input" step before "Get Dictionary
Value" to prevent the *"couldn't convert from Text to Dictionary"* error that
occurs when Shortcuts receives a JSON response but hasn't been told to parse it.

`shortcut_setup.md` also has an appendix with standalone variants ("… (no
server)") that call the Mathpix API directly from iOS — a fallback for when no
server is reachable. They are weaker (OCR-based URL conversion, 100 s PDF
timeout, plain-text credentials inside the shortcut) and only worth building
if you cannot run the server.

---

## Dependencies

| Package | Purpose |
|---|---|
| `fastapi` | HTTP server framework |
| `uvicorn` | ASGI server |
| `httpx` | Async HTTP client (page fetching, Mathpix API) |
| `beautifulsoup4` + `lxml` | HTML parsing and DOM manipulation |
| `trafilatura` | Main-content extraction from HTML |
| `python-dotenv` | Load `.env` credentials |
| `python-multipart` | Multipart form parsing for PDF upload |
| `pydantic` | Request validation and URL sanitisation |
| `playwright` | Headless Chromium: page rendering and PDF export |

---

## Running the Server

**Start manually** (any platform):
```bash
.venv/bin/python app.py --host 0.0.0.0 --port 8000 [--output-dir DIR]
```

**Ubuntu — systemd service** (installed by `deploy/install.sh`):
```bash
systemctl status margin
journalctl -u margin -f          # logs
```

**macOS — Launch Agent (auto-starts at login):**
```bash
launchctl load ~/Library/LaunchAgents/com.marc.math-readlater.plist
tail -f server.log               # logs, in the app directory
```

**Convert a saved Markdown file to PDF** (requires Pandoc + BasicTeX or TeX Live):
```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex -V geometry:margin=1in
```
