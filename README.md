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
 │  POST /save  web page ─► headless Chromium ─► page.pdf() ──►  .pdf        │
 │              (waits: network idle → MathJax/KaTeX done → fonts ready)     │
 │              + HTML math extraction ─► trafilatura ──────►  .md/.tex/.org │
 │                                                                           │
 │  POST /save  PDF URL  ─► store as-is ──►  .pdf                            │
 │              + Mathpix OCR ─────────────►  .md/.tex/.org                  │
 │                                                                           │
 │  POST /save-pdf  upload ─► keep file .pdf  +  Mathpix OCR .md/.tex/.org   │
 │                                                                           │
 └────────────────────────────► OUTPUT_DIR ◄────────────────────────────────┘
                    (e.g. an iCloud/Nextcloud folder synced to your notes app)
```

Maths is detected from the page, not from a setting: the Markdown path
converts whatever formula markup a page ships (MathJax, KaTeX, MathML,
MediaWiki, `alt`-text images), and only pages that ship some get the extra
pass that turns stray Unicode symbols into LaTeX. An article about the
α-version keeps its α.

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

A save that reaches its handler answers HTTP 200 with a JSON body containing
`"status": "ok"` or `"status": "error"` — a failed save is in-band, so iOS
Shortcuts can display the message instead of failing silently. A request
turned away before the handler carries its own status: 401 without the token,
403 for a cross-site write, 413 for a body over the cap. Those three answer
the same JSON shape, so a client reading `summary` has something to show
either way; a payload that does not parse is the exception, answering 422
with FastAPI's `{"detail": …}`.

If `MARGIN_TOKEN` is set, every endpoint below except `GET /health` also
requires the token (`Authorization: Bearer <token>` header, `?token=`
parameter, or the browser cookie) and answers HTTP 401 without it — see
[Authentication](#authentication-optional).

### `POST /save` — save any page (the general endpoint)

```json
{ "url": "https://…", "formats": ["pdf", "md", "tex", "org"] }
```

`formats` is optional; any subset of `pdf`, `md`, `tex`, `org`, each selected
independently. Omitted, it uses the server default (`DEFAULT_FORMATS`,
shipping as `pdf,md,tex`) — the *same* default every capture path uses
(bookmarklet, iOS shortcut, the queue's pre-checked boxes), so a save
produces the same files however it's triggered. `formats` may also be a
comma-separated string (`"pdf,md"`), which iOS Shortcuts can send more easily
than a JSON array.

A URL whose PDF is larger than the 100 MB download limit is refused, and
that is the end of it: the refusal used to be read as "this is not a PDF",
after which the page was handed to Chromium, which loaded and printed the
file the limit had just turned down.

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
- **`md`** / **`tex`** / **`org`** — the text formats: `.md` (Markdown, math
  as LaTeX), `.tex` (compilable LaTeX article), `.org` (Emacs Org-mode);
  `tex`/`org` are Pandoc-derived and work without `md` selected. For a web
  page these come from the HTML pipeline below. **For a URL that serves a PDF
  directly, they come from Mathpix OCR of that PDF** — so an arXiv (or
  archive.org) PDF is saved as the PDF *and* converted to Markdown/LaTeX in
  one request. OCR needs `MATHPIX_APP_ID`/`KEY`; without them the PDF is
  still saved and the text formats are skipped with a warning in the
  response's `warnings` array.

Re-saving a URL that is already in the inbox or archive is skipped: the
response carries `"duplicate": true` plus the existing files, instead of
piling up `-2`/`-3` copies. Pass `"force": true` (or `&force=true` on
`/save-page`) to save again anyway. Saved URLs are tracked in a
`.saved-urls.json` index inside the output directory, with a fallback match
on `source_url` in Markdown frontmatter for files saved before the index
existed.

```json
{ "status": "ok", "title": "Fourier transform",
  "files": ["2026-07-18-fourier-transform.pdf",
            "2026-07-18-fourier-transform.md"] }
```

On partial success a `"warnings"` array lists what failed; on total failure
`"status": "error"` with a `"message"`.

### `POST /save-url` — Markdown pipeline only

```json
{ "url": "https://…" }
```

The text-only path: no PDF, just the Markdown formats. Fetches the raw HTML
(fast — MathJax/KaTeX sites ship the LaTeX source in the initial HTML),
extracts the article with math converted to LaTeX, and writes the text-format
slice of the default (shipping as `md` + `tex`; Pandoc-derived, skipped if
Pandoc is absent). If the plain fetch is bot-blocked (401/403/406/429/503) or
the extracted body is nearly empty (client-side-rendered app), it
automatically retries from the fully rendered Chromium DOM. A page that is
still a bot-check after that retry is refused rather than filed: a challenge
page is long enough to pass for an article, and only its title gives it away.
Responds with `{"status": "ok", "filename": …, "files": […], "title": …,
"summary": …}`.

Set `DEFAULT_FORMATS` to PDF alone and this endpoint has nothing it can
write, so it refuses and says so, naming `POST /save` — it does not quietly
fall back to Markdown, which would produce exactly the format you excluded.

Prefer `POST /save` for new integrations — it's the unified endpoint that can
also produce the PDF. `/save-url` remains for the pure-text use case.

The URL cleaning tolerates iOS Shortcuts quirks (inserted whitespace,
duplicated URL); only `http(s)` URLs are accepted.

### `POST /save-pdf` — PDF upload → Markdown

Multipart form upload, field `file`, ≤ 50 MB (the iOS "Save PDF" shortcut).
**Keeps the uploaded PDF** (when `pdf` is in `DEFAULT_FORMATS`) *and* OCRs it
to the default text formats via the [Mathpix](https://mathpix.com) `/v3/pdf`
API (polls up to 3 minutes) — best-in-class for math PDFs. Needs
`MATHPIX_APP_ID`/`MATHPIX_APP_KEY`; without them the PDF is still saved and
the OCR step is skipped with a warning. So an uploaded PDF yields the same
PDF + Markdown + LaTeX as a URL save. With `DEFAULT_FORMATS` naming no text
format the OCR is skipped too, and no Mathpix credit is spent on text you
said you did not want.

### `GET /` — built-in reading queue

A minimal web UI over the output directory: every saved item with its date,
title (from the Markdown frontmatter when available), links to its files and
original source, a quick-save box (with per-format checkboxes — PDF,
Markdown, LaTeX, Org — that remember your last choice), a client-side title
filter, and an
**Archive** button that moves an item's files into an `archive/` subfolder
(with a restore view at `/?view=archive`). The archive view also offers
permanent **Delete** (confirmation prompt; removes all of an item's files
and its duplicate-index entry) — deletion is deliberately two-step: inbox →
archive → delete. Follows the system light/dark theme. With this, any
browser is a functional read-later front end — no notes app or third-party
service required.

The queue is a **live view of the folder** — there is no separate database.
Deleting, moving, or adding files by hand (file manager, `rm`, a sync
client) is equally valid: the queue reflects it on the next reload, and the
duplicate index ignores entries whose files are gone.

File links open in a built-in **reader** (`GET /read/{name}`): a page with
back-to-queue navigation, a **Share** button (native share sheet with the
whole file attached, via the Web Share API — works in the home-screen app),
**Copy** and **Download**. Markdown is rendered server-side (sanitized) with
MathJax typesetting the math client-side (CDN — without internet the math
shows as `$...$` source); PDFs embed inline. This matters most in the
home-screen web app, which has no browser chrome to navigate back with.

Supporting endpoints: `GET /files/{name}` serves raw saved files (inbox or
archive; only `.pdf/.md/.tex/.org`, no path traversal; `?download=1` forces
an attachment), `POST /archive` (form fields `stem` and `action`) moves
items, and `POST /delete` (form fields `stem` and `view`) removes one.

Both of those fields are **required, and are checked against the two values
they accept** — `archive`/`restore` and `inbox`/`archive`. Neither has a
default: "anything that is not `archive` means the inbox" turned a typo into
a different, permanent operation, and an omitted field took the safe-looking
default silently. Anything else answers 422 and changes nothing.

### `GET /save-page` — bookmarklet target

Same pipeline as `POST /save`, but GET with query parameters
(`?url=…&formats=pdf,md`) and an HTML result page instead of JSON — made to
be opened as a browser tab by the desktop bookmarklet (see below).

### `GET /health`

```json
{ "status": "ok", "output_dir_exists": true, "output_dir_writable": true,
  "saved_md_count": 12, "saved_pdf_count": 34, "pandoc_available": true,
  "playwright_available": true, "chromium_installed": true,
  "mathpix_configured": false, "auth_required": false }
```

The output directory's path is not in there: `/health` is the one endpoint a
token does not gate, and on a real install the path names the account the
service runs as. Whether it exists and is writable is the useful half.

`playwright_available` and `chromium_installed` are two different facts. `pip
install playwright` gives you the first; `playwright install chromium` gives
you the second, and PDF rendering needs both — with only the package, every
render fails at launch.

### `POST /echo`

Debug helper: echoes method, headers, and parsed body of the request back.

## Saved files

- Filenames: `YYYY-MM-DD-title-slug.{pdf,md,tex,org}`. Titles come from the
  page `<title>`/`og:title` (site-name suffixes like `… | Site` stripped);
  name collisions get `-2`, `-3`, … suffixes — nothing is ever overwritten.
- **A stem names one item.** The part before the extension is how everything
  addresses a saved item — the URL index, the archive and delete forms, the
  offline cache — and none of those carry a folder with it, so a stem free in
  the inbox but taken in `archive/` is not free. If an older version left you
  two items sharing one, `deploy/unique-stems.py` renames the archived side
  and follows it in the index; the server says so at startup when it finds
  any.
- **A save that produced only some formats is completed in place.** Ask again
  for `["pdf", "md"]` after the Markdown half failed and only the `.md` is
  written, beside the PDF that is already there — not a second copy of the
  whole family under `-2`.
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
| Default formats | `DEFAULT_FORMATS` in `.env` (subset of `pdf,md,tex,org`; an invalid value stops startup; naming no text format leaves `/save-url` nothing to write, and it says so) | `pdf,md,tex` |
| Mathpix credentials | `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` in `.env` | unset — PDFs are kept, but PDF-to-text OCR is skipped |
| API token | `MARGIN_TOKEN` in `.env` | unset — no authentication |
| Cross-origin access | `MARGIN_CORS_ORIGINS` in `.env` (comma-separated origins) | unset — no cross-origin access |
| Saves to private addresses | `MARGIN_ALLOW_PRIVATE_URLS` in `.env` | unset — loopback, LAN and metadata addresses refused |

Three limits are fixed in the source rather than configured, because the
numbers only matter when something has gone wrong: an upload is at most
50 MB, a downloaded PDF at most 100 MB, and a page's HTML at most 8 MB. The
last one is smaller than it sounds — an article is tens of kilobytes, and
the text is parsed as well as held, so a body costs several times its own
size while it is being read.

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
  bookmarklet. A browser is redirected immediately to the same URL without
  the token, keeping it out of the address bar and later history; the first
  request necessarily still appears once in the server's access log.
- **Browser cookie** — open `http://YOUR-SERVER:8000/?token=<token>` once and
  the token is stored in an `HttpOnly`, `SameSite=Strict` cookie (1 year);
  after that the queue UI, file links, and archive buttons work with no
  decoration. `Strict` keeps other *sites* from riding it; a page served from
  the same site — the same host on another port — still gets it, which is why
  the cross-site guard below is not the whole story.

With a token set, the bookmarklet becomes:

```
javascript:window.open('http://YOUR-SERVER:8000/save-page?token=YOUR-TOKEN&url='+encodeURIComponent(location.href));
```

`GET /health` stays open (it reports `"auth_required": true` so clients can
detect the requirement) and CORS preflights pass through, as they carry no
credentials. It does **not** report where the output directory is: the path
names the account the service runs as, and whether the folder works is the
useful half.

**Margin will not fetch its own network.** Names are resolved and checked,
not just addresses — `localtest.me` resolves to `127.0.0.1`, so checking the
spelling alone stopped nothing. It saves whatever URL it is given
and then serves the result back through `/read`, so an address inside the
machine — loopback, `10.x`, `192.168.x`, link-local, cloud metadata — would
be a way to read those services *through* Margin, and with no token that is
open to anyone who can reach the port. Such addresses are refused, including
the shorthands a resolver accepts (`127.1`, `2130706433`, `0x7f000001`) and
names that IDNA folds onto them. The check is repeated after every redirect
and for every Chromium subrequest. A DNS name is still resolved by the HTTP
client or browser, not resolved and pinned by Margin, so a hostile name that
changes or resolves to an internal address remains outside this guarantee.
Set `MARGIN_ALLOW_PRIVATE_URLS=1` if you save from an internal wiki and want
it back.

**A page on another site cannot change anything here.** A plain HTML form
posts cross-origin without a preflight and the browser sends it whether or
not the answer can be read, so with `MARGIN_TOKEN` unset — the private-network
default — any site could have archived, restored or deleted your saved items.
Requests that change something are refused when the browser marks them
cross-site (`Sec-Fetch-Site: cross-site`). The bookmarklet's GET is the one
state-changing exception, and is accepted cross-site only as a top-level
document navigation—not as an image, iframe, script or background request.
`curl`, the iOS Shortcut and RSS readers send no such header and are
unaffected.

Three things that guard deliberately does not cover, because "another site"
is narrower than "another origin":

- **The same site on another port.** A *site* is the scheme plus the
  registrable domain; the port is not part of it ([HTML][site-def]), so a
  second service on the same machine — `http://your-server:9000` next to
  Margin on `:8000` — is same-site, and a page it serves can post here. The
  `SameSite=Strict` cookie is attached to those requests too. On a tailnet
  the registrable domain is your tailnet (`ts.net` is a public suffix), so
  another machine of yours is same-site and another tailnet is not.
- **An origin you listed in `MARGIN_CORS_ORIGINS`.** That setting exists to
  let a browser extension post, so its origins skip the guard. Keep it empty
  unless you are running one.
- **A browser too old to send `Sec-Fetch-Site`.** The header is the whole
  mechanism; a client that omits it is treated as not-a-browser, which is
  what makes `curl` and the Shortcut work.

`MARGIN_TOKEN` closes the first gap only for clients that have to *supply*
the token — the header and the `?token=` form. It does not close it for the
browser you read in: the cookie is `SameSite=Strict`, and Strict is about the
site, so a same-site page's request carries the cookie just as your own tabs
do. What actually covers that case is not serving untrusted pages from the
machine or the tailnet Margin runs on.

[site-def]: https://html.spec.whatwg.org/multipage/browsers.html#site

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

- **Simple:** keep the default bind (`0.0.0.0`), set `MARGIN_TOKEN`.
  Reachable on LAN and tailnet, but every request needs the token. On a
  tailnet you share with others, also restrict port 8000 to your own devices
  in the Tailscale admin ACLs. Note what this costs: a plain-`http://`
  address that is not `localhost` is not a **secure context**, and browsers
  withhold whole APIs there. Measured in Chromium against
  `http://192.168.1.42:8000`, `'serviceWorker' in navigator` is `false` —
  not refused, absent — so **offline reading never activates**, and
  `navigator.clipboard` and `navigator.canShare` are undefined too. Margin
  copes (Copy falls back to the selection route, Share hides itself), but
  the queue and your saved pages are unavailable without a signal.
- **Tailnet-only + HTTPS (recommended):** bind Margin to loopback and let
  Tailscale proxy it with a real TLS certificate. This is what turns offline
  reading on:

  ```bash
  # in /etc/margin/<user>.env: HOST=127.0.0.1
  sudo systemctl daemon-reload && sudo systemctl restart margin@<user>
  sudo tailscale serve --bg --https=443 8000
  tailscale serve status
  ```

  Enable HTTPS certificates for the tailnet once first (admin console → DNS →
  HTTPS Certificates); `serve` cannot get a certificate without it. Then
  `tailscale serve status` should show the mapping, and Margin is at
  `https://<machine>.<your-tailnet>.ts.net` (no port, note the **https**),
  unreachable from the LAN or anywhere outside the tailnet. `serve` is not
  `funnel`: nothing is exposed to the internet.

  That URL is a secure context, so the service worker registers and a page
  you have opened once is readable with no network at all. It also stops
  browsers treating the server as mixed content, so `fetch()`-based clients
  and extensions work from https pages too. `MARGIN_TOKEN` is still worth
  setting on shared tailnets.

  Tailscale terminates TLS and speaks plain HTTP to Margin on loopback, so
  the app cannot see that the browser is on HTTPS. It reads
  `X-Forwarded-Proto` for that, which is what lets the auth cookie carry
  `Secure` on a site the browser considers secure.

  **Running Footnote on the same machine?** Give each its own HTTPS port —
  `--https=443` for one, `--https=8443` for the other (Tailscale allows 443,
  8443 and 10000):

  ```bash
  sudo tailscale serve --bg --https=443  8000   # Margin
  sudo tailscale serve --bg --https=8443 8010   # Footnote
  ```

  Do **not** put them on one name under different paths with `--set-path`:
  both apps address everything from the root (`/static/style.css`,
  `/manifest.json`, `/read/…`, `/files/…`, the `/archive` and `/delete`
  forms), and a service worker's scope is the directory it is served from, so
  one served under `/margin/` could not control its own pages.

  **Then, on each device** — easy to miss, and none of it is optional:

  1. Open the new URL once with `?token=…` to store the cookie.
  2. **Re-add the home-screen app from the HTTPS URL and delete the old one.**
     A PWA keeps the origin it was installed from, and service workers and
     caches are per-origin: the existing install will never read offline.
  3. Update the iOS Shortcuts and the bookmarklet to the HTTPS URL.

  **Can you still reach it over plain HTTP?** Yes — leave `HOST=0.0.0.0`
  (the default) instead of binding to loopback, and `serve` will proxy to the
  same port while the tailnet and LAN addresses keep working. Two things
  follow. The two addresses are two *origins*, so each has its own service
  worker cache, PWA install, cookie and `localStorage` (which is where the
  format checkboxes remember themselves); only the HTTPS one reads offline.
  And do not use the **same hostname** over both schemes: a `Secure` cookie
  is only sent to a URI whose scheme is secure
  ([RFC 6265 §5.4](https://www.rfc-editor.org/rfc/rfc6265#section-5.4)), so
  the HTTPS session's cookie is never sent over `http://` to that host and
  you would be re-authenticating constantly. Use the machine name for HTTPS
  and the tailnet IP for HTTP, and they stay independent.

The iOS Shortcuts, the bookmarklet, and `curl` all work unchanged over
Tailscale — only the address (and with `tailscale serve`, the scheme)
changes.

## Behind a reverse proxy (Caddy)

If a proxy already terminates TLS for other services on this machine, that is
the tidier home for Margin too: one thing owns certificates and renewal, the
config sits in a file beside everything else's, and each app gets a hostname
you chose instead of a port number.

**None of this has to be public.** Point the DNS record at the server's
*tailnet* address rather than its public one:

```
margin.example.com.    A    100.101.102.103      # tailscale ip -4
```

The name then resolves for anyone and connects for nobody outside the
tailnet. `100.64.0.0/10` is the Shared Address Space of
[RFC 6598][rfc6598] — reserved for carrier-grade NAT, which is to say for
traffic that needs another layer of translation before it reaches the
internet, and which nothing routes across it. Tailscale hands its node
addresses out of that range for exactly that reason.

`tailscale ip` prints yours: one from that range, and one from
`fd7a:115c:a1e0::/48`. That second prefix is not per-tailnet — it is a
constant compiled into the client, `TailscaleULARange()` in
[net/tsaddr/tsaddr.go][tsaddr], the IPv6 counterpart of `100.64.0.0/10`. It
is an RFC 4193 Unique Local Address prefix: `fd` for a locally assigned ULA,
then the 40-bit global ID Tailscale picked, `7a:115c:a1e0`. The same file
puts MagicDNS's own resolver at `fd7a:115c:a1e0::53`, the twin of
`100.100.100.100`, which is the giveaway that these are fixed for everyone
rather than issued per tailnet. Check it against your own with
`tailscale ip -6` before trusting it. You get
the ordinary reverse-proxy arrangement without putting a headless browser
that fetches arbitrary URLs on the open web.

The one thing that follows: Let's Encrypt cannot reach that address either,
so the certificate has to come from a **DNS-01 challenge**, which needs a DNS
provider plugin.

```bash
sudo caddy add-package github.com/caddy-dns/cloudflare   # your provider
sudo systemctl restart caddy
```

`add-package` is marked experimental and replaces the binary in place, so a
later `apt upgrade caddy` puts the stock one back and certificates stop
renewing. Pin the package, or build with `xcaddy` and install outside apt's
reach.

```caddyfile
margin.example.com {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	# Both address families: a node has one of each, and a request over
	# the v6 one would fail a v4-only match. See below — this line does
	# more work than it looks like.
	@outside not remote_ip 100.64.0.0/10 fd7a:115c:a1e0::/48
	respond @outside 403
	reverse_proxy 127.0.0.1:8000
}
```

**That `remote_ip` line is the enforcing control, not decoration.** If this
proxy already serves another site, it is listening on every interface, and a
vhost answers to whatever name is asked for — a client that connects to the
public address and sends this hostname as SNI reaches the site, whatever the
DNS record says. The record keeps the name from resolving anywhere useful;
the matcher is what refuses the connection. Note its limit: `100.64.0.0/10`
is also where ISPs put their own CGNAT customers, so it identifies "arrived
over something CGNAT-shaped", not "arrived over your tailnet".

The airtight version is `bind <tailnet-address>` in the site block, which
stops anything from listening publicly for these names at all. Two costs
before you reach for it: Caddy does not share vhosts across listeners, so
every *other* site on this proxy stops answering on that address, which
breaks anything else you reach over the tailnet; and the listener needs the
interface to exist when Caddy starts, which means ordering the unit after
`tailscaled`.

Three Caddy defaults are already what Margin needs, so there is nothing to
tune:

- It "sets the `X-Forwarded-Proto` header field"
  ([reverse_proxy][caddy-rp]) — which is exactly what the auth cookie reads
  to decide it may carry `Secure`.
- There is no default request-body limit (`request_body max_size` is opt-in),
  so 50 MB PDF uploads pass through untouched.
- `response_header_timeout` defaults to "No timeout", so a page that takes
  the full 60-second render deadline is not cut off mid-capture.

Then bind Margin to loopback, or the plain-HTTP port stays open on the
tailnet alongside the HTTPS name — two origins for one app, only one of which
reads offline:

```bash
# /etc/margin/<user>.env
HOST=127.0.0.1
sudo systemctl restart margin@<user>
ss -ltnp | grep 8000        # expect 127.0.0.1:8000, not 0.0.0.0:8000
```

**A consequence of sharing a domain.** The cross-site guard refuses writes
the browser marks `cross-site`, and "site" means the registrable domain — so
`margin.example.com` and any other service you host under `example.com` are
*same-site*, and a page served by one can post to the other with the
`SameSite=Strict` cookie attached. That is fine for software you trust; if
one of those services renders HTML that other people supply, give Margin a
different registrable domain instead.

**If you point the record at a public address**, you are trading that away:
what stands between the internet and a browser-as-a-service is one shared
bearer token with no rate limiting and no lockout. Add a second gate at the
proxy — `basic_auth`, mTLS, or an IP allowlist — rather than relying on
`MARGIN_TOKEN` alone.

### Switching an install that is already running

Order matters: prove the new path works before closing the old one.

1. Add the DNS record, add the Caddy block, `caddy validate` and reload.
2. Watch `journalctl -u caddy -f` for `certificate obtained successfully`.
3. Open `https://margin.example.com/?token=<token>` on one device. Only when
   that works, set `HOST=127.0.0.1` and restart the service.
4. On **every** device: open the URL once with `?token=…`, delete the old
   home-screen app and re-add it from the new address, and update the iOS
   Shortcuts and the bookmarklet. A PWA keeps the origin it was installed
   from, and service workers, caches and cookies are per-origin — the old
   install will never see the new server.
5. The old origin's cached pages stay in the browser until its site data is
   cleared (iOS: Settings → Safari → Advanced → Website Data).

[caddy-rp]: https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
[rfc6598]: https://www.rfc-editor.org/rfc/rfc6598#section-7
[tsaddr]: https://github.com/tailscale/tailscale/blob/main/net/tsaddr/tsaddr.go

## Install

Requirements: Python ≥ 3.10 (the installer refuses anything older rather
than starting a service that fails inside a save). Optional: `pandoc` (for
`.tex`/`.org` companions), Mathpix credentials (for PDF-to-text OCR from
uploads and direct PDF URLs).

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

Margin is single-user by design, so the deployment model is **one instance
per person**: each instance runs as that person's own Unix account, saves
into that person's own (synced) folder, and has its own port, token, reading
queue, and duplicate index. From a checkout on the server:

```bash
sudo bash deploy/install.sh                       # shared platform (once)
sudo bash deploy/add-instance.sh <user> 8000     # one line per person
sudo bash deploy/add-instance.sh <other-user> 8001
```

`install.sh` sets up the shared parts — code and venv in `/opt/margin`,
Chromium system libraries, and the [margin@.service](deploy/margin@.service)
systemd template. `add-instance.sh <user> <port> [output-dir]` writes
`/etc/margin/<user>.env` (output dir defaults to
`/home/<user>/ReadLater/inbox`; a `MARGIN_TOKEN` is generated and printed),
installs headless Chromium into that user's cache, and enables
`margin@<user>`. Both are idempotent, and both refuse a destination that is
not theirs: `/`, a top-level system directory, or a populated directory that
is not a Margin installation. Both run as root and turn a path into the
target of `chown`, a recursive `chmod` and — for the application directory —
`rsync --delete`, so a mistyped one is not a misconfiguration to correct on
the next run. `add-instance.sh` also stops rather than guessing when an
existing `/etc/margin/<user>.env` does not say what the instance runs as:
systemd reads that file, not the command line.

Saved pages are private by default: the output directory is created `0700`
and the unit runs with `UMask=0077`. The shared `/opt/margin/.env` is `0640`
and readable through a `margin` group each instance user joins, rather than
by every local account.

Versions are lower bounds in `requirements.txt`, so an install resolves them
afresh and two installs a month apart are not the same software. Generate
`deploy/constraints.txt` on the server, test it, commit it, and every later
install gets exactly those versions:

```bash
bash deploy/make-constraints.sh python3   # on the server, not on a laptop
.venv/bin/python -m pytest
```

Day-2 operations:

```bash
systemctl status margin@<user>
journalctl -u margin@<user> -f
sudoedit /etc/margin/<user>.env && sudo systemctl restart margin@<user>
sudo bash deploy/install.sh && sudo systemctl restart 'margin@*'   # upgrade
```

Shared, instance-independent secrets (Mathpix credentials) go in
`/opt/margin/.env`; everything per-person (port, output dir, token) lives in
`/etc/margin/<user>.env`.

> **Upgrading from the pre-template layout** (single `margin.service` with a
> dedicated `margin` user): stop and remove the old unit
> (`sudo systemctl disable --now margin && sudo rm
> /etc/systemd/system/margin.service`), run the two commands above, move the
> contents of the old inbox — including `archive/` and the hidden
> `.saved-urls.json` — into the new per-user output dir, and `chown -R` them
> to that user.

The service listens on all interfaces; run it on a private network (LAN,
Tailscale, WireGuard) and/or use the per-instance `MARGIN_TOKEN` — see
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
  Cross-origin `fetch` clients — a browser extension of your own — need
  their origin named in `MARGIN_CORS_ORIGINS`; a wildcard used to be the
  default, which let any page you happened to be visiting read this
  instance's answers.)
- **The built-in queue page** — open `http://YOUR-SERVER:8000/` in any
  browser: paste a URL to save, read via the file links, archive when done.
  A page you have opened once stays readable with no network at all, and the
  queue itself falls back to the last list it saw, saying so when it does —
  this needs a **secure context**, so `https://` or `localhost`; over a
  plain-`http://` LAN or tailnet address browsers do not offer service
  workers at all. See the Tailscale section for the one-line way to get one.
  On iPhone/iPad, Safari's Share → **Add to Home Screen** installs it as a
  full-screen app with the Margin icon; with `MARGIN_TOKEN` set, the
  installed app prompts for the token once on first launch (its cookie
  storage is separate from Safari's).
- **Anything that speaks HTTP** — `curl`, RSS-reader automations, Raycast, a
  cron job:

  ```bash
  curl -X POST http://server:8000/save \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $MARGIN_TOKEN" \
    -d '{"url": "https://en.wikipedia.org/wiki/Fourier_transform"}'
  ```

  (drop the `Authorization` header if you haven't set `MARGIN_TOKEN`)

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
| `deploy/` | Ubuntu installer, per-person instance script, systemd template, icon regeneration, `unique-stems.py` |
| `static/` | App icon (SVG master + generated PNGs) and web manifest |
| `description.md` | Architecture and the seven math-extraction strategies |
| `shortcut_setup.md` | Step-by-step iOS Shortcut construction |
| `start.sh` | launchd-friendly start wrapper (macOS) |

## License

Margin is free software, licensed under the
[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-or-later).
You may run, study, modify, and share it; if you offer a modified version
as a network service, you must make your modified source available to its
users. Copyright © 2026 Marc Schlienger.
