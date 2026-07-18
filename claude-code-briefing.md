# Claude Code Briefing: Math-Aware Read-It-Later Pipeline

## Background & Problem

I need a pipeline that captures math-heavy web articles and PDFs on iOS/iPadOS and converts them to clean Markdown (with LaTeX math preserved) for use on my Mac — ideally landing in an Obsidian vault or iCloud Drive folder.

**Key constraints:**
- Capture must work from iOS Share Sheet — triggered from Safari and RSS readers (NetNewsWire or Unread)
- Math blocks (MathJax/KaTeX rendered on web pages, and math in PDFs) must be preserved as LaTeX in the output Markdown
- Output should be saved to iCloud Drive, accessible on Mac
- On the Mac, content should be usable in Obsidian (and optionally exportable to LaTeX)

**Existing tools in the workflow:**
- Readwise Reader — for non-math content (works well, no changes needed)
- Zotero — for math PDFs and math-heavy web pages (good desktop experience)
- Mathpix API — best-in-class OCR for math, supports PDF and image to Markdown/LaTeX conversion
- Obsidian — target note-taking app on Mac (with iCloud sync)

---

## What to Build

Build **both** of the following. Start with Option A (Shortcut), and if it hits limitations, proceed to Option B (web app).

---

## Option A: Apple Shortcut

### Goal
A Shortcut that appears in the iOS Share Sheet, accepts a URL or PDF, converts it to math-aware Markdown, and saves the result to iCloud Drive.

### Flow
1. User shares a URL or PDF from Safari / RSS reader via Share Sheet
2. Shortcut detects input type (URL vs PDF file)
3. **For URLs:**
   - Fetch the page HTML
   - Send to Mathpix API (or alternative — see notes) for conversion to Markdown with LaTeX
4. **For PDFs:**
   - Send the PDF binary to Mathpix API for conversion to Markdown with LaTeX
5. Save resulting `.md` file to a specific iCloud Drive folder (e.g. `iCloud Drive/ReadLater/inbox/`)
6. Filename: sanitized page title or PDF filename + timestamp

### Mathpix API details
- Endpoint for PDF: `POST https://api.mathpix.com/v3/pdf`
- Endpoint for image/URL: `POST https://api.mathpix.com/v3/text`
- Auth headers: `app_id` and `app_key`
- Output format: Mathpix Markdown (MMD) — compatible with standard Markdown + LaTeX math delimiters (`$...$` and `$$...$$`)
- Docs: https://mathpix.com/docs/api/

### Notes & potential limitations
- Shortcuts can make HTTP requests via the "Get Contents of URL" action, including POST with headers and body — sufficient for Mathpix API calls
- PDF binary upload from Shortcuts may require base64 encoding — handle this
- iCloud Drive writing is natively supported in Shortcuts
- If Shortcuts cannot handle multipart form upload for PDFs reliably, flag this and recommend moving to Option B

---

## Option B: Small Web App

### Goal
A lightweight server app (Node.js or Python) that:
- Accepts a URL or PDF upload via a simple HTTP endpoint
- Converts to math-aware Markdown via Mathpix API
- Saves the `.md` file to an iCloud Drive folder on the Mac
- Can be triggered from iOS via a Shortcut (which just POSTs the URL or PDF to this local/hosted server)

### Architecture
- **Server:** Simple Express (Node) or FastAPI (Python) app
- **Hosting options:**
  - Locally on Mac (always-on or on-demand), exposed to iOS via local network or Tailscale
  - Or hosted on Cloudflare Workers / Railway / Render for always-available access
- **iCloud Drive writing:** Write directly to `~/Library/Mobile Documents/com~apple~CloudDocs/ReadLater/inbox/` on the Mac
- **iOS trigger:** A minimal Shortcut that just POSTs the URL or PDF to this server — no complex logic on iOS side

### Endpoints to implement
```
POST /save-url
  Body: { "url": "https://..." }
  Returns: { "status": "ok", "filename": "..." }

POST /save-pdf
  Body: multipart/form-data with PDF file
  Returns: { "status": "ok", "filename": "..." }
```

### Conversion pipeline (for both endpoints)
1. Receive URL or PDF
2. Call Mathpix API for conversion to Markdown
3. Clean up output (strip Mathpix-specific metadata if needed)
4. Write `.md` to iCloud Drive inbox folder
5. Return success response to iOS Shortcut

---

## Output Format Requirements

The resulting Markdown files should:
- Use standard `$...$` for inline math and `$$...$$` for block math
- Be compatible with Obsidian (renders via the built-in MathJax renderer)
- Include a YAML frontmatter block with: `title`, `source_url` (if from web), `date_saved`, `tags: [readlater, math]`
- Be saved with a filename like `YYYY-MM-DD-title-slug.md`

### Example output structure
```markdown
---
title: "Understanding the Fourier Transform"
source_url: "https://example.com/fourier"
date_saved: 2026-04-25
tags: [readlater, math]
---

# Understanding the Fourier Transform

The Fourier transform of a function $f(t)$ is defined as:

$$\hat{f}(\xi) = \int_{-\infty}^{\infty} f(t) e^{-2\pi i \xi t} dt$$

...
```

---

## Environment & Secrets

- Mathpix API key: to be provided by user (`MATHPIX_APP_ID` and `MATHPIX_APP_KEY`)
- iCloud Drive path on Mac: `~/Library/Mobile Documents/com~apple~CloudDocs/`
- Target inbox folder: `ReadLater/inbox/` (create if not exists)

---

## Suggested Build Order

1. Build Option A (Shortcut) first — provide an importable `.shortcut` file or step-by-step Shortcut configuration
2. Test URL capture and PDF capture separately
3. If Shortcut PDF upload hits limitations, build Option B web app
4. Provide a minimal companion Shortcut for Option B that just POSTs to the server

---

## Success Criteria

- Share a math-heavy arXiv page URL from Safari → `.md` file appears in iCloud Drive with LaTeX intact
- Share a math PDF from Safari/Files → `.md` file appears in iCloud Drive with equations as LaTeX
- Both work from NetNewsWire/Unread via Share Sheet
- Output renders correctly in Obsidian on Mac
