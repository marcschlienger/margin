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

On Ubuntu the server runs under a systemd template unit
(`deploy/margin@.service`) — one instance per person, each as that person's
own user with its own port, output folder, and token; `deploy/install.sh`
installs the shared platform and `deploy/add-instance.sh` creates instances.
On macOS it can run as a Launch Agent so it starts at login and restarts on
crash. Capture happens from iOS via two Apple
Shortcuts (one for URLs, one for PDFs), from desktop browsers via a
bookmarklet hitting `/save-page`, or from anything that can POST JSON.

---

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI server — endpoints, extraction and conversion logic |
| `render.py` | Headless-Chromium rendering (Playwright): rendered HTML + PDF export |
| `deploy/install.sh` | Ubuntu installer — shared code, venv, Chromium deps, template unit |
| `deploy/add-instance.sh` | Creates a per-person instance (`margin@<user>`) |
| `deploy/margin@.service` | systemd template unit for the Ubuntu deployment |
| `requirements.txt` | Python dependencies |
| `.env` | Config: Mathpix credentials, `OUTPUT_DIR`, `MARGIN_TOKEN` |
| `.env.example` | Template for `.env` — copy to `.env` and fill in |
| `start.sh` | Start wrapper (used by the macOS Launch Agent; runs `app.py`) |
| `shortcut_setup.md` | Step-by-step instructions for building the iOS Shortcuts |
| `LICENSE` | GNU AGPL v3.0 |
| `~/Library/LaunchAgents/…plist` | Launch Agent definition (macOS auto-start only; not in the repo) |

### Setting up Mathpix credentials

PDF-to-text conversion from `/save-pdf` and direct PDF URLs calls the Mathpix
API. To enable it:

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

If `.env` is missing or the values are blank, page capture keeps working and
PDFs are still stored; only their OCR-derived text formats are skipped, with a
warning in the response.

---

## Server Endpoints

> If the `MARGIN_TOKEN` environment variable is set, every endpoint except
> `GET /health` requires the token — via `Authorization: Bearer` header,
> `?token=` query parameter, or the browser cookie set after one
> authenticated visit. See the README's Authentication section.

### `POST /save`

**Input:** JSON body `{ "url": "https://...", "formats": [...] }` —
`formats` optional, any subset of `pdf`, `md`, `tex`, `org`, each selected
independently; may also be a comma-separated string. Omitted, it uses the
`DEFAULT_FORMATS` env setting (ships as `pdf,md,tex`), which every capture
path shares so a save yields the same files however it was triggered.

**Process:** the server probes once whether the URL serves a PDF directly
(content type or `.pdf` extension), then branches:

*If it's a PDF:*
1. `pdf` format → the file is downloaded and stored as-is (title from the
   arXiv abstract page or embedded PDF metadata).
2. `md`/`tex`/`org` → the PDF bytes are OCR'd via Mathpix into
   Markdown/LaTeX, written under the same filename stem so they group with
   the PDF. Without `MATHPIX_APP_ID`/`KEY` this step is skipped and a warning
   is added; the PDF is still saved.

*If it's a web page:*
3. `pdf` → loaded in headless Chromium (waits for DOM-content-loaded, network
   idle best-effort, MathJax 2/3 typesetting via JS promises/queues, and
   `document.fonts.ready`), exported as `YYYY-MM-DD-title-slug.pdf` (screen
   CSS, A4, backgrounds on). Bot-challenge interstitials and soft-404s are
   detected by title/status and reported as errors instead of saved.
4. `md`/`tex`/`org` → the HTML Markdown pipeline below.

Either branch yields the same requested formats, so a save is consistent
regardless of whether the URL was a page or a PDF.

**Returns:** `{ "status": "ok", "title": "...", "files": [...] }`,
or `{ "status": "error", "message": "..." }` — always HTTP 200.

---

### `POST /save-url`

**Input:** JSON body `{ "url": "https://..." }`

**Process:**
1. Cleans the URL — iOS Shortcuts sometimes serialises long URLs with
   whitespace or even duplicates the value with a newline between copies. The
   server removes the inserted whitespace and collapses an exact doubled URL.
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
1. Validates the upload is a PDF `<= 50 MB`.
2. If Mathpix is configured, OCRs the PDF (upload → poll `/v3/pdf/{pdf_id}`
   every 3 s to `completed`, ≤ 3 min → fetch MMD), taking the title from the
   first `#` heading. Without credentials this step is skipped with a warning.
3. Keeps the uploaded PDF file (when `pdf` is in `DEFAULT_FORMATS`) and writes
   the OCR'd text in the default text formats, all under one filename stem.

So an uploaded PDF produces the same PDF + Markdown + LaTeX as a URL that
points at a PDF — the iOS "Save PDF" shortcut and the desktop bookmarklet on
a PDF link converge on the same result.

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
items with Restore buttons. File links open in the built-in reader
(`GET /read/{name}`): back-to-queue navigation, native Share (Web Share API,
whole file attached), Copy, Download; Markdown rendered server-side through
an allowlist sanitizer with MathJax typesetting math client-side, PDFs
embedded in an iframe. Supporting endpoints: `GET /files/{name}` serves raw
saved files (`.pdf/.md/.tex/.org` only, traversal-safe; `?download=1` forces
attachment), `POST /archive` (form fields `stem`,
`action=archive|restore`) moves an item's files between the output directory
and its `archive/` subfolder, and `POST /delete` (form field `stem`)
permanently removes an item's files and its duplicate-index entry — exposed
in the UI only from the archive view, behind a confirmation prompt. The
queue is a live view of the folder: files deleted or moved externally simply
disappear from it.

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

## Deciding whether a page is maths

Margin is a general read-later service that happens to be very good at maths,
which means most of what it saves has no maths in it at all. Two of its steps
are only right on a maths page, and one of them was actively wrong everywhere
else.

The math strategies in `_replace_math_elements` — MathML, MathJax 2 and 3,
KaTeX, MediaWiki spans, `alt`-text formula images — are safe to run on
anything: they only fire when the page ships that markup, and prose does not.
The Unicode pass is different. It wraps isolated Greek letters and symbols in
`$…$`, which is right where α is a variable and wrong where it is a product
name. Measured on eight ordinary sentences, seven came back rewritten:

    The α-version shipped in March  →  The $\alpha$-version shipped in March
    Costs rose ≈15% year over year  →  Costs rose $\approx$15% year over year
    The Σ of small decisions        →  The $\Sigma$ of small decisions

So the page decides, and it decides with something it actually said rather
than with a guess about its prose: `_replace_math_elements` returns how many
elements it replaced, and the Unicode pass runs only when that count is
non-zero. Every strategy replaces through one `swap()` helper, so a strategy
added later cannot forget to be counted.

What this gives up is a maths post written in plain Unicode with no markup —
a blog saying "let α be a root" in a bare `<p>`. That case is rare, the text
still reads correctly as Unicode, and it is genuinely indistinguishable from
"the α-version shipped" without reading the article. If it ever matters, the
escape hatch is a parameter on the save rather than a checkbox to consider on
every one.

The same signal makes the reader honest: it loads MathJax only for documents
that contain `$…$`. It used to fetch a megabyte of JavaScript from a CDN for
every Markdown page, including articles about tooling — which is also why the
browser suite's runtime halved once this landed.

Mathpix output is unaffected either way: the PDF path emits `$…$` directly,
so the gate concerns only the HTML pipeline.

## Code, which is made of the same characters as maths

A general read-later queue is full of shell and C++, and both are written with
`$`, `_`, `^`, `<` and `>`. Three things had to be checked rather than assumed,
and all three hold:

- **Extraction leaves code alone.** `trafilatura` emits fenced blocks and
  inline spans, and nothing downstream touches them: `export PATH="$HOME/bin:$PATH"`,
  `awk '{print $1, $3}'` and `"${f%.md}.markdown"` come through byte for byte.
- **The reader leaves it alone too.** `_render_markdown` stashes math spans
  *before* Markdown conversion, and `$HOME/bin:$PATH` matches the inline-math
  pattern exactly — but it is restored as escaped text, so what lands in the
  page is what was written.
- **MathJax skips it.** Its default `skipHtmlTags` covers `<pre>` and
  `<code>`. Verified in a browser on a page with one real formula and two
  shell blocks: one `mjx-container`, none inside code, and the blocks intact.

What did *not* hold was the detection. Both the front-matter `math` tag and
the reader's decision to load MathJax looked for `$…$` anywhere in the body,
so an article about shell scripting was tagged as maths and fetched a
megabyte of JavaScript to typeset nothing. `_has_math_outside_code` removes
fenced blocks and inline spans first, then looks. Inline spans matter as much
as fences: in "Use `$HOME` and `$PATH` together" the text between the two
dollars carries neither a dollar nor a newline, so the inline-math pattern
matches straight across them.

A page with both — a formula in prose and a shell block — still counts as
maths, which is right: there is something to typeset, and MathJax will leave
the block alone.

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

## Staying responsive, and staying inside the caps

Two things a personal server gets wrong quietly, because with one user
nothing visibly breaks until the day it does.

**A save must not freeze the server.** Pandoc is `subprocess.run` with a
thirty-second timeout and Margin asks for `.tex` and `.org`, so writing a
save held the event loop for as long as Pandoc took. Measured with a stubbed
three-second Pandoc: the loop ran 4 ticks where it should have run about 64 —
no `/health`, no queue, no second save, for the duration. The write phase runs
on a worker thread now (63 of 64 ticks in the same measurement). One lock
covers choosing a stem and writing the files under it, because choosing looks
at what exists and writing creates it: two saves that landed on the same title
could otherwise pick the same stem, and moving the write off the loop is what
made that window real.

**A cap has to bound what is allocated, not what arrives.** `httpx`'s
`aiter_bytes` hands over what the decoder produced, so a limit counted there
is a limit on bytes that already exist — and how many that is belongs to
whoever compressed them. Measured against 300 MB of zeros in 299 kB of gzip:
a 10 MB cap peaked at 142.9 MiB. The wire is read raw and capped there, and a
content coding is expanded with `zlib`'s own output limit, which puts the same
case at 21.2 MiB — twice the cap, and proportional to it rather than to the
compression ratio. Codings are still accepted, because refusing them would
send a real PDF URL down the render path and produce a picture of a PDF
viewer.

Extraction and the reader's Markdown rendering stay on the loop: measured,
`trafilatura` on a 413 kB page costs 0.22s and rendering a 160 kB saved
document costs 0.48s. That is an order of magnitude below the Pandoc case and
not worth the machinery.

## Offline reading

The point of a read-later queue is the train, and until there was a service
worker the home-screen app was a blank page without a signal. Two kinds of
response age quite differently, so `static/service-worker.js` gives each its
own policy:

- **The queue (`/`) and the shell** (`static/style.css`, the manifest, the
  icons) are network-first. Items are added and archived constantly, so a
  stale list is wrong while online. Offline the cached copy is served with a
  line saying what it is — the template leaves an `<!--offline-notice-->`
  comment where the banner belongs, so the worker fills it in rather than
  parsing the page. A list that is quietly hours old is worse than one that
  admits it.
- **A saved page** (`/read/…` and `/files/…`) is cache-first with a refresh
  behind it. These files are written once and not edited, so a page opened
  once stays readable with no network at all. `SAVED_MAX` bounds it in
  entries rather than bytes, and modestly: a saved PDF can be megabytes, and
  a browser evicts the whole origin when it runs out of room.

Everything else — saving, archiving, deleting — is network only. They change
what is on the server, and pretending to do that offline is a lie the queue
then has to un-tell.

Three details are load-bearing. A URL carrying `?token=` is never stored: the
cache key is the whole URL, so caching it would write the token into Cache
Storage and leave it there long after the cookie made it unnecessary — and a
401 empties both caches, because a revoked token should not leave a readable
copy behind. Deleting an item tells the worker before the form navigates
away, since the 404-driven cleanup only fires if someone asks for the file
again, which offline may be never. And every cache write carries the stem's
generation from the moment its fetch began: deleting increments it, so a
refresh already in flight cannot put the page back afterwards, while a
genuinely new save with the same stem can still be cached.

## What the client is not told, and what it may do

- **Every outbound hop is checked.** Literal loopback, private, link-local,
  reserved and metadata addresses are refused on the initial URL, after HTTP
  redirects, and on Chromium subrequests. DNS names are not resolved and
  pinned by Margin, so a hostile name that resolves inward remains a known
  residual; `MARGIN_ALLOW_PRIVATE_URLS=1` deliberately removes the boundary
  for an operator saving an internal wiki.
- **The output directory's path stays server-side.** `/health` is public — it
  has to be, for a probe that carries no credentials — and on a real install
  the path names the account the service runs as. It reports whether the
  folder exists and is writable, which is the useful half.
- **A page you are visiting may not change anything.** CORS does not help:
  a plain HTML form posts cross-origin without a preflight, and the browser
  sends it whether or not the answer can be read. With `MARGIN_TOKEN` unset —
  the documented private-network default — any page could archive, restore or
  delete a saved item; verified against a running instance before the guard
  existed. Requests that change something are refused when the browser marks
  them `Sec-Fetch-Site: cross-site`. The bookmarklet's state-changing GET is
  accepted cross-site only as a top-level document navigation, not as a
  subresource or background fetch. Browsers send those headers and ordinary
  API clients do not, so `curl`, the Shortcut and RSS readers are unaffected.
  Cross-origin *reading* is opt-in through `MARGIN_CORS_ORIGINS`; the
  wildcard that used to be the default let any page read the answers.
- **A saved file is a file inside the folder.** `/files` and `/read` resolve
  a name only within the output directory or its `archive/`, symlinks
  resolved — the folder is synced and written to by other software, so a name
  in it can be a link to anything the account can read.
- **A recorded source is a link only if we would follow it.** Front matter
  comes out of that same folder, so `source_url` is rendered as a link only
  when its scheme is http, https or mailto, and off-site links open off-site
  (`target=_blank`, `rel=noopener noreferrer`) rather than replacing the queue.
- **An error is a page a browser can get out of.** A stale bookmark or a file
  moved in the synced folder is ordinary, and a bare JSON body leaves the
  reader with no way back — on the home screen there is not even a back
  button. Clients that asked for JSON still get JSON.

## The look, and why it is shared

Margin and Footnote are siblings and are meant to look it: one palette (cream
paper, slate ink, a red rule, blue for the app's own marks), one serif for
anything you read and one sans for anything you operate. `static/style.css`
holds it, the `:root` block is the shared part, and the two files are meant to
stay diffable — the pages used to carry three separate `<style>` blocks that
had drifted from each other and from Footnote entirely.

Two details are worth naming because they were wrong for a while. The header
icon is sized to the h1's line box and set flush with its top: centred
against the whole two-line block it lined up with the gap between the title
and the tagline and with neither line. And the queue's dates are written as
ISO stamps in `<time datetime=…>`, so they are still right with no script,
and rendered in the reader's own locale order by a few lines that read the
attribute back.

The reader's Copy button is offered whenever the file is text, not only where
`navigator.clipboard` exists. The Clipboard API is a secure-context feature
and Margin is normally reached over plain HTTP on a home network, so the old
condition hid the button exactly where it was needed; the deprecated
selection route is the fallback, and a refusal says so rather than looking
like a button that does nothing.

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

**Ubuntu — systemd template** (installed by `deploy/install.sh` +
`deploy/add-instance.sh <user> <port>`):
```bash
systemctl status margin@<user>
journalctl -u margin@<user> -f   # logs
```

**macOS — Launch Agent (auto-starts at login):**
```bash
launchctl load ~/Library/LaunchAgents/<label>.plist
tail -f server.log               # logs, in the app directory
```

**Convert a saved Markdown file to PDF** (requires Pandoc + BasicTeX or TeX Live):
```bash
pandoc input.md -o output.pdf --pdf-engine=xelatex -V geometry:margin=1in
```
