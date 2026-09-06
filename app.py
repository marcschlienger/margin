# Margin — self-hosted read-later server that preserves JS/math rendering.
# Copyright (C) 2026 Marc Schlienger
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version. See the LICENSE file for details.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Margin — read-later server that saves web pages with JS/math rendering intact.

POST /save       { "url": "https://...", "formats": ["pdf", "md"] }
POST /save-url   { "url": "https://..." }   Markdown pipeline only
POST /save-pdf   multipart form, field "file" (Mathpix OCR → Markdown)
GET  /save-page  ?url=…&formats=pdf,md      bookmarklet target, HTML result
GET  /           reading queue UI  (with GET /files/{name}, POST /archive)
GET  /health     status and configuration check
POST /echo       debug: mirrors the request back

Output directory: --output-dir flag > OUTPUT_DIR env var > platform default.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import unicodedata
import zlib
import uuid
import secrets
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import date
from html import escape as _html_escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote as _url_quote, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup, NavigableString
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, field_validator

from render import (
    CHROME_UA,
    Renderer,
    RendererUnavailable,
    looks_blocked,
    looks_missing,
)

try:
    from pypdf import PdfReader
except ModuleNotFoundError:  # optional — only used for direct-PDF titles
    PdfReader = None

try:
    import markdown as _markdown
except ModuleNotFoundError:  # optional — reader falls back to raw text
    _markdown = None

load_dotenv()

MATHPIX_APP_ID  = os.getenv("MATHPIX_APP_ID", "")
MATHPIX_APP_KEY = os.getenv("MATHPIX_APP_KEY", "")
MAX_PDF_BYTES = 50 * 1024 * 1024  # 50 MB cap on PDF uploads

# Optional API token. Unset → open server (private-network use). Set → every
# endpoint except /health requires it: `Authorization: Bearer <token>` header,
# `?token=` query parameter, or the cookie set after one authenticated visit.
MARGIN_TOKEN = os.getenv("MARGIN_TOKEN", "").strip()

# Output formats. MD_FORMATS are the text formats the Markdown pipeline emits
# (tex/org derived from md via Pandoc). DEFAULT_FORMATS is what a save writes
# when the request doesn't specify — set via the DEFAULT_FORMATS env var
# (comma-separated subset of pdf/md/tex/org). Every capture path (/save,
# /save-page bookmarklet, /save-url iOS shortcut, the queue's pre-checked
# boxes) honors it, so a save produces the same files however it was made.
MD_FORMATS = ("md", "tex", "org")
_ALL_FORMATS = ("pdf",) + MD_FORMATS


def _parse_default_formats(raw: str) -> tuple[str, ...]:
    values = [f for f in re.split(r"[\s,]+", raw.lower().strip()) if f]
    unknown = set(values) - set(_ALL_FORMATS)
    if unknown:
        raise RuntimeError(
            f"DEFAULT_FORMATS contains {sorted(unknown)}; use pdf, md, tex, org"
        )
    if not values:
        raise RuntimeError("DEFAULT_FORMATS must name at least one format")
    return tuple(dict.fromkeys(values))


DEFAULT_FORMATS = _parse_default_formats(
    os.getenv("DEFAULT_FORMATS", "pdf,md,tex")
)
def _text_formats(formats: tuple[str, ...]) -> tuple[str, ...]:
    """The text-only slice of a format list, for the steps that write text
    and cannot write a PDF: /save-url's whole job, and /save-pdf's OCR.

    It is empty when nothing text was asked for, and that emptiness is
    honoured rather than papered over with a Markdown fallback: /save-pdf
    keeps the uploaded file and skips the OCR, and /save-url — which has
    nothing else it could write — says so instead of producing a format the
    operator excluded.
    """
    return tuple(f for f in formats if f in MD_FORMATS)


_DEFAULT_MD_FORMATS = _text_formats(DEFAULT_FORMATS)


def _default_output_dir() -> Path:
    if sys.platform == "darwin":  # original iCloud-inbox workflow
        return (
            Path.home()
            / "Library/Mobile Documents/com~apple~CloudDocs/ReadLater/inbox"
        )
    return Path.home() / "ReadLater" / "inbox"


# Where saved files land. Overridden by the --output-dir CLI flag (see bottom).
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR") or _default_output_dir()).expanduser()


def _warn_about_shared_stems() -> None:
    """Say so once at startup if two items share a name.

    Allocation spans both folders now, so this can only be the work of an
    older version — but the files are still there, and until they are renamed
    one item's URL can resolve to the other's document.
    """
    def stems(folder: Path) -> set[str]:
        if not folder.is_dir():
            return set()
        try:
            return {f.stem for f in folder.iterdir()
                    if f.is_file() and f.suffix.lower() in _SERVE_EXTS}
        except OSError:
            return set()

    shared = stems(OUTPUT_DIR) & stems(_archive_dir())
    if shared:
        _log("startup",
             f"{len(shared)} stem(s) held by both an inbox and an archived "
             f"item ({', '.join(sorted(shared)[:3])}"
             f"{', …' if len(shared) > 3 else ''}). One item's URL can "
             "resolve to the other's file until they are renamed: run "
             "deploy/unique-stems.py")


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.client = httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": CHROME_UA},
        # httpx invokes request hooks again for redirects. Checking here, at
        # the last boundary before I/O, means a public URL cannot redirect the
        # server into loopback, a LAN service, or cloud metadata.
        event_hooks={"request": [_validate_outbound_request]},
    )
    application.state.renderer = Renderer(url_allowed=_browser_url_allowed)
    _warn_about_shared_stems()
    yield
    await application.state.renderer.close()
    await application.state.client.aclose()


app = FastAPI(title="Margin", version="2.1.0", lifespan=lifespan)

# Cross-origin access is opt-in. A wildcard let any page you happen to be
# visiting read this instance's answers, and with MARGIN_TOKEN unset — the
# documented private-network default — that is every page on the web. Name
# the origins that need it, comma-separated, in MARGIN_CORS_ORIGINS; a
# browser extension or a fetch-based client of your own is what this is for.
MARGIN_CORS_ORIGINS = [o for o in re.split(r"[\s,]+",
                                           os.getenv("MARGIN_CORS_ORIGINS", ""))
                       if o]
if MARGIN_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=MARGIN_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# Optional token auth (MARGIN_TOKEN)
# ---------------------------------------------------------------------------

_TOKEN_COOKIE = "margin_token"
# /health for monitoring; icons/manifest so browser chrome (favicon requests,
# home-screen installs) works without credentials — none of them are sensitive.
_PUBLIC_PATHS = {
    "/health",
    "/service-worker.js",
    "/favicon.svg",
    "/favicon.ico",
    "/favicon-32.png",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    "/manifest.json",
}

# Shown on unauthenticated browser requests. The form matters for the
# home-screen web app: standalone mode has no URL bar to type ?token=, and
# its cookie storage is separate from Safari's, so the token must be
# enterable in-page (submits as GET /?token=…, which sets the cookie).
_UNAUTHORIZED_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Margin — unauthorized</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon-32.png" type="image/png" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
</head>
<body style="font: 16px/1.5 system-ui, sans-serif; max-width: 34rem;
             margin: 4rem auto; padding: 0 1rem;">
<h1 style="font-size:1.3rem; color:#c62828;">Token required</h1>
<p>This Margin server requires an API token. Enter it once — it is stored
in a browser cookie afterwards. (API clients: send an
<code>Authorization: Bearer</code> header instead.)</p>
<form method="get" action="/" style="display:flex; gap:.5rem;">
  <input type="password" name="token" placeholder="API token" required
         autocomplete="current-password"
         style="flex:1; font:inherit; padding:.45rem .6rem;
                border:1px solid #999; border-radius:6px;">
  <button type="submit" style="font:inherit; padding:.45rem .9rem;
          border:1px solid #999; border-radius:6px; cursor:pointer;">
    Unlock</button>
</form>
</body></html>"""


def _request_is_secure(request: Request) -> bool:
    """Whether the *browser's* connection is TLS, which is not ours.

    Behind `tailscale serve` — or any proxy that terminates TLS — the app
    sees plain HTTP on loopback while the browser is on https://…ts.net. So
    request.url.scheme says "http" and the cookie went out without Secure on
    a site the browser considers HTTPS. The forwarded header is what the
    proxy leaves behind to say otherwise.

    Trusting it costs nothing here: a cookie is only ever set on a request
    that already carried the right token, so nobody without it can provoke
    one. And where no proxy sets the header, this answers exactly what
    request.url.scheme answered before.
    """
    if request.url.scheme == "https":
        return True
    # A chain of proxies leaves a list; the client-facing one is first.
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def _request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query_params.get("token")
            or request.cookies.get(_TOKEN_COOKIE, ""))


def _token_matches(presented: str) -> bool:
    """A malformed non-ASCII query token is simply wrong, never a 500."""
    try:
        return secrets.compare_digest(
            presented.encode("utf-8", "surrogatepass"),
            MARGIN_TOKEN.encode("utf-8", "surrogatepass"),
        )
    except (AttributeError, UnicodeEncodeError):
        return False


# The multipart framing around a PDF upload, so a file exactly at the cap
# still fits. Anything past this is refused before a byte is stored.
MAX_BODY_BYTES = MAX_PDF_BYTES + 1024 * 1024


@app.middleware("http")
async def _limit_body(request: Request, call_next):
    """Refuse an oversized body before the framework reads it.

    Checking the size after reading is not a limit: Starlette parses the
    whole multipart body before the handler runs, so a 200 MB upload was
    spooled to disk in full and only then told it was too large — measured,
    with the error quoting all 209,715,209 bytes. On an instance with no
    token that is unauthenticated disk consumption.

    Declared before the token check so it runs *inside* it: an oversized
    request to a locked instance should be unauthorized rather than told its
    body is too large.

    Content-Length only. A chunked upload has none, and bounding that means
    counting the stream by hand, which is more machinery than a personal
    server on a private network needs — the handler's own check stays as the
    backstop for that case.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return JSONResponse(
            {"status": "error", "filename": "",
             "message": f"Request body too large (max {MAX_BODY_BYTES} bytes)",
             "summary": "Error: request body too large"},
            status_code=413)
    return await call_next(request)


@app.middleware("http")
async def _refuse_cross_site_writes(request: Request, call_next):
    """A page you happen to be visiting may not change anything here.

    CORS does not help: a plain HTML form posts cross-origin without a
    preflight, and the browser sends it whether or not the answer can be
    read. With MARGIN_TOKEN unset — the documented private-network default —
    any page could archive, restore or delete a saved item. Verified against
    a running instance before this existed: `Origin: https://evil.test` on a
    form POST to /archive moved the item and answered 303.

    Sec-Fetch-Site is sent by browsers and by nothing else, so curl, the iOS
    Shortcut and the RSS reader are unaffected. The bookmarklet's GET is
    allowed only as the top-level document navigation it actually makes.
    """
    cross_site = request.headers.get("sec-fetch-site") == "cross-site"
    mutating_request = request.method not in ("GET", "HEAD", "OPTIONS")
    # /save-page is deliberately a GET because the bookmarklet must navigate
    # from an HTTPS article to a commonly HTTP home server. It still changes
    # state, so accept only the top-level document navigation the bookmarklet
    # makes—not an image, script, iframe, or background fetch from a page.
    disguised_write = (
        request.url.path == "/save-page"
        and (request.headers.get("sec-fetch-mode") != "navigate"
             or request.headers.get("sec-fetch-dest") != "document")
    )
    # An origin named in MARGIN_CORS_ORIGINS is one you decided to trust;
    # refusing its POST after answering its preflight made the documented
    # browser-extension case impossible.
    invited = request.headers.get("origin", "") in MARGIN_CORS_ORIGINS
    if cross_site and not invited and (mutating_request or disguised_write):
        return JSONResponse(
            {"status": "error", "filename": "",
             "message": "Refused: a cross-site request may not change "
                        "anything here.",
             "summary": "Error: cross-site request refused"},
            status_code=403,
        )
    return await call_next(request)


@app.middleware("http")
async def _require_token(request: Request, call_next):
    if (
        not MARGIN_TOKEN
        or request.url.path in _PUBLIC_PATHS
        or request.url.path.startswith("/static/")
        or request.method == "OPTIONS"  # CORS preflight carries no credentials
    ):
        return await call_next(request)

    if not _token_matches(_request_token(request)):
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(_UNAUTHORIZED_HTML, status_code=401)
        return JSONResponse(
            {"status": "error", "filename": "",
             "message": "Unauthorized: missing or wrong token "
                        "(Authorization: Bearer header or ?token= parameter)",
             "summary": "Error: missing or wrong API token"},
            status_code=401,
        )

    if request.query_params.get("token"):
        # The cookie now carries it, so remove the token from a browser's
        # address bar and later history. The first request necessarily still
        # reaches the access log once. API clients receive their answer rather
        # than an unexpected redirect.
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            response = RedirectResponse(
                str(request.url.remove_query_params("token")), status_code=303
            )
        else:
            response = await call_next(request)
        response.set_cookie(
            _TOKEN_COOKIE, MARGIN_TOKEN, max_age=365 * 24 * 3600,
            httponly=True, samesite="strict",
            secure=_request_is_secure(request),
        )
        return response
    return await call_next(request)


# Margin renders text extracted from arbitrary sites and serves files from a
# synced folder. The allowlist sanitizer is the first line; these headers keep
# an overlooked tag or a mislabeled file from becoming executable app code.
# Inline script/style is needed by the small server-rendered UI and MathJax.
_SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' https://cdn.jsdelivr.net; img-src 'self' data:; "
        "connect-src 'self'; frame-src 'self'; worker-src 'self'; "
        "form-action 'self'; frame-ancestors 'self'; base-uri 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}
_REVALIDATE_PATHS = {"/", "/service-worker.js", "/manifest.json"}


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    path = request.url.path
    if path in _REVALIDATE_PATHS or path.startswith("/static/"):
        response.headers.setdefault("cache-control", "no-cache")
    return response


# ---------------------------------------------------------------------------
# Math-character / function tables — referenced by the MathML→LaTeX converter
# and by the Unicode wrapping pass, so they must be defined before either.
# ---------------------------------------------------------------------------

_UNICODE_TO_LATEX = {
    "α": r"\alpha",   "β": r"\beta",    "γ": r"\gamma",   "δ": r"\delta",
    "ε": r"\epsilon", "ζ": r"\zeta",    "η": r"\eta",     "θ": r"\theta",
    "ι": r"\iota",    "κ": r"\kappa",   "λ": r"\lambda",  "μ": r"\mu",
    "ν": r"\nu",      "ξ": r"\xi",      "π": r"\pi",      "ρ": r"\rho",
    "σ": r"\sigma",   "τ": r"\tau",     "υ": r"\upsilon", "φ": r"\phi",
    "χ": r"\chi",     "ψ": r"\psi",     "ω": r"\omega",
    "Γ": r"\Gamma",   "Δ": r"\Delta",   "Θ": r"\Theta",   "Λ": r"\Lambda",
    "Ξ": r"\Xi",      "Π": r"\Pi",      "Σ": r"\Sigma",   "Υ": r"\Upsilon",
    "Φ": r"\Phi",     "Ψ": r"\Psi",     "Ω": r"\Omega",
    "ℓ": r"\ell",     "∞": r"\infty",   "∂": r"\partial", "∇": r"\nabla",
    "∑": r"\sum",     "∏": r"\prod",    "∫": r"\int",     "√": r"\sqrt",
    "≈": r"\approx",  "≠": r"\neq",     "≤": r"\leq",     "≥": r"\geq",
    "→": r"\to",      "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "±": r"\pm",      "×": r"\times",   "÷": r"\div",     "·": r"\cdot",
}
_MATHML_OP_MAP = {
    "−": "-", "×": r"\times", "÷": r"\div",
    "≈": r"\approx", "≤": r"\leq", "≥": r"\geq", "≠": r"\neq",
    "∞": r"\infty", "∑": r"\sum", "∫": r"\int", "∏": r"\prod",
    "⁡": "",  # invisible function application
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "±": r"\pm", "∓": r"\mp", "∂": r"\partial", "∇": r"\nabla",
    "…": r"\ldots", "⋯": r"\cdots", "·": r"\cdot",
    "%": r"\%",
}
_MATHML_FUNC = {
    "ln", "log", "sin", "cos", "tan", "cot", "sec", "csc",
    "arcsin", "arccos", "arctan", "exp", "lim", "max", "min",
    "det", "ker", "dim", "deg", "gcd", "sup", "inf",
}


# ---------------------------------------------------------------------------
# Title / filename / frontmatter helpers
# ---------------------------------------------------------------------------

# Strip a trailing " | Site Name", " — Site Name", " - Site Name" from titles.
_RE_TITLE_SUFFIX = re.compile(r"\s+[|—–\-]\s+[^|—–\-]+$")
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")
_TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _has_lone_surrogate(value: str) -> bool:
    return bool(_LONE_SURROGATE.search(value))


def _clean_text(value) -> str:
    """Text that is safe to log, encode as UTF-8, and write to a note."""
    text = value if isinstance(value, str) else str(value or "")
    text = _LONE_SURROGATE.sub("\ufffd", text)
    return _TEXT_CONTROLS.sub("", text)


def _clean_title(title: str) -> str:
    # A title becomes one YAML field, one queue row and part of a filename.
    # Provider metadata may contain newlines, controls, or broken Unicode.
    title = re.sub(r"\s+", " ", _clean_text(title)).strip()
    cleaned = _RE_TITLE_SUFFIX.sub("", title)
    return cleaned if cleaned else title


def _slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", _clean_text(text)).encode(
        "ascii", "ignore"
    ).decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text[:max_len] or "untitled"


def _filename(title: str, ext: str = "md") -> str:
    return f"{date.today().isoformat()}-{_slugify(title)}.{ext}"


def _unique_path(path: Path) -> Path:
    """Return `path` if free, else the first `<stem>-2.<ext>`, `-3.<ext>` etc."""
    if not path.exists():
        return path
    i = 2
    while (candidate := path.with_stem(f"{path.stem}-{i}")).exists():
        i += 1
    return candidate


def _yaml_quote(s: str) -> str:
    s = _clean_text(s)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _frontmatter(title: str, source_url: str | None = None,
                 has_math: bool = True) -> str:
    lines = ["---", f"title: {_yaml_quote(_clean_title(title))}"]
    if source_url:
        lines.append(f"source_url: {_yaml_quote(source_url)}")
    lines += [
        f"date_saved: {date.today().isoformat()}",
        f"tags: [readlater{', math' if has_math else ''}]",
        "---",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-format output (.md + Pandoc → .tex + .org)
# ---------------------------------------------------------------------------

# Pandoc's standalone LaTeX template is huge (KOMA, microtype, fontspec, ...) and
# its preamble breaks pdflatex on common Unicode (↩ footnote-back arrows, etc.).
# We generate a minimal article ourselves: produce just the body via `pandoc -t
# latex` and wrap it in this preamble.
_LATEX_PREAMBLE = r"""% !TEX TS-program = pdflatex
% !TEX encoding = UTF-8 Unicode
\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{hyperref}

% Pandoc emits \tightlist inside compact bullet lists; provide it so the
% body compiles without the standalone template.
\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}

\title{__TITLE__}
\date{__DATE__}

\begin{document}
\maketitle

__BODY__
\end{document}

%%% Local Variables:
%%% mode: latex
%%% TeX-master: t
%%% End:
"""

# Unicode characters that pdflatex cannot typeset and that carry no semantic
# weight in our context (footnote-back arrows, etc.). Stripped from the body
# before it reaches Pandoc.
_PDFLATEX_DROP = {
    "↩": "",  # ↩  leftwards arrow with hook
    "↪": "",  # ↪  rightwards arrow with hook
    "↺": "",  # ↺
    "↻": "",  # ↻
    "⏎": "",  # ⏎  return symbol
}


def _strip_pdflatex_unsafe(text: str) -> str:
    for ch, repl in _PDFLATEX_DROP.items():
        text = text.replace(ch, repl)
    return text


def _latex_escape_title(s: str) -> str:
    """Minimal escaping for a title that goes inside \\title{...}."""
    table = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
             "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
             "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(table.get(c, c) for c in s)


def _run_pandoc(args: list[str], label: str) -> tuple[int, str, str]:
    """Run pandoc, returning (returncode, stdout, stderr). -1 on missing/timeout."""
    try:
        r = subprocess.run(["pandoc", *args], capture_output=True, timeout=30, text=True)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"[pandoc] {label} skipped: {e}", file=sys.stderr, flush=True)
        return -1, "", str(e)
    if r.returncode != 0:
        print(f"[pandoc] {label} failed: {r.stderr.strip()}",
              file=sys.stderr, flush=True)
    return r.returncode, r.stdout, r.stderr


# Read source as GFM with YAML frontmatter — handles fenced code, tables, and
# our `---` metadata block more predictably than Pandoc's default flavour.
_PANDOC_FROM = ["-f", "gfm+yaml_metadata_block"]


def _write_tex(md_path: Path, tex_path: Path, title: str) -> None:
    rc, body, _ = _run_pandoc(
        [*_PANDOC_FROM, str(md_path), "-t", "latex"],
        f"latex {md_path.name}",
    )
    if rc != 0:
        return
    body = _strip_pdflatex_unsafe(body)
    tex = (_LATEX_PREAMBLE
           .replace("__TITLE__", _latex_escape_title(title))
           .replace("__DATE__", date.today().isoformat())
           .replace("__BODY__", body))
    _write_text_atomically(tex_path, tex)


def _write_bytes_atomically(path: Path, data: bytes) -> None:
    """Replace one output without ever exposing a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _write_text_atomically(path: Path, text: str) -> None:
    _write_bytes_atomically(path, _clean_text(text).encode("utf-8"))


# Held across choosing a stem and writing the files under it. Choosing looks
# at what exists and writing creates it, so two saves that landed on the same
# title used to be able to pick the same stem — and once the writing moved off
# the event loop (below) they could interleave inside the allocator itself.
_WRITE_LOCK = threading.Lock()


def _stem_taken(stem: str, exts: set[str], besides: frozenset = frozenset()) -> bool:
    """Whether any file of this stem exists in *either* folder.

    Both, because a stem is an item's identity everywhere else in the app —
    the URL index, the delete and archive forms, the service worker's
    forget-stem message — and not one of those carries a folder alongside it.
    Allocating per folder let the inbox and the archive hold two different
    items under one name, and then archiving one rewrote the other's index
    entry, so its URL resolved to a document it had nothing to do with.

    `besides` excludes files that are about to move: the family being
    archived is its own occupant until the rename lands.
    """
    for folder in (OUTPUT_DIR, _archive_dir()):
        for ext in exts:
            path = folder / f"{stem}{ext}"
            if path not in besides and path.exists():
                return True
    return False


def _family_path(path: Path, formats: tuple[str, ...], reuse_stem: bool = False,
                 besides: frozenset = frozenset()) -> Path:
    """Pick one stem that is free for the whole requested file family.

    Free in both folders, so a stem names one item for as long as it exists.
    Looking only at the Markdown name let a later TeX-only save overwrite an
    existing `.tex`. `reuse_stem` is for adding OCR text beside the PDF this
    same request has just written; requested targets still have to be absent.
    """
    stem, parent = path.stem, path.parent
    wanted = {f".{fmt}" for fmt in formats}
    i = 1
    while True:
        candidate = stem if i == 1 else f"{stem}-{i}"
        target_exists = _stem_taken(candidate, wanted, besides)
        family_exists = _stem_taken(candidate, set(_SERVE_EXTS), besides)
        if not target_exists and (reuse_stem or not family_exists):
            return parent / f"{candidate}{path.suffix}"
        i += 1


def _write_all_formats(filename: str, md_content: str, title: str,
                       formats: tuple[str, ...] = _DEFAULT_MD_FORMATS,
                       reuse_stem: bool = False) -> list[Path]:
    """Write the requested subset of .md/.tex/.org (collision-safe stem).

    Returns the files actually written — Pandoc-derived ones are skipped
    silently when Pandoc is unavailable, exactly as before.
    """
    with _WRITE_LOCK:
        return _write_all_formats_locked(filename, md_content, title,
                                         formats, reuse_stem)


def _write_all_formats_locked(filename: str, md_content: str, title: str,
                              formats: tuple[str, ...],
                              reuse_stem: bool) -> list[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    md_content = _clean_text(md_content)
    md_path = _family_path(OUTPUT_DIR / filename, formats, reuse_stem)
    written: list[Path] = []

    if "md" in formats:
        _write_text_atomically(md_path, md_content)
        written.append(md_path)
        pandoc_src = md_path
    else:
        # Pandoc still needs Markdown input — use a throwaway temp file.
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8")
        tmp.write(md_content)
        tmp.close()
        pandoc_src = Path(tmp.name)

    try:
        if "tex" in formats:
            tex_path = md_path.with_suffix(".tex")
            _write_tex(pandoc_src, tex_path, title)
            if tex_path.exists():
                written.append(tex_path)
        if "org" in formats:
            org_path = md_path.with_suffix(".org")
            fd, tmp_name = tempfile.mkstemp(
                dir=org_path.parent, prefix=f".{org_path.name}.", suffix=".tmp"
            )
            os.close(fd)
            try:
                _run_pandoc(
                    [*_PANDOC_FROM, str(pandoc_src), "-t", "org",
                     "-o", tmp_name],
                    f"org {org_path.name}",
                )
                if Path(tmp_name).stat().st_size:
                    os.replace(tmp_name, org_path)
                    written.append(org_path)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
    finally:
        if pandoc_src is not md_path:
            pandoc_src.unlink(missing_ok=True)

    return written


# ---------------------------------------------------------------------------
# URL → Markdown  (HTML-source math extraction, no OCR needed)
# ---------------------------------------------------------------------------

# Match \[...\] and \(...\) emitted by MathJax 3 / KaTeX into plain text
_RE_BLOCK  = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_RE_INLINE = re.compile(r"\\\((.+?)\\\)")
# Wikipedia wraps every formula in {\displaystyle ...} — strip the outer wrapper
_RE_DISPLAYSTYLE = re.compile(r"^\{\\displaystyle\s+(.*)\}\s*$", re.DOTALL)
_RE_LATEX_ALT = re.compile(r"\\[a-zA-Z]|[_^]\{")


def _unwrap_displaystyle(latex: str) -> str:
    m = _RE_DISPLAYSTYLE.match(latex.strip())
    return m.group(1).strip() if m else latex.strip()


def _math_replacement(latex: str, display: bool) -> str:
    latex = _unwrap_displaystyle(latex)
    return f"\n$$\n{latex}\n$$\n" if display else f"${latex}$"


def _mathml_kids(node):
    return [c for c in node.children
            if not (isinstance(c, NavigableString) and not str(c).strip())]


def _mathml_to_latex(node) -> str:
    """Recursively convert a MathML BeautifulSoup node to a LaTeX string."""
    if isinstance(node, NavigableString):
        return str(node)

    tag = node.name

    def render_all():
        return "".join(_mathml_to_latex(c) for c in node.children)

    def k():
        return _mathml_kids(node)

    if tag == "mi":
        text = node.get_text()
        if text in _MATHML_FUNC:
            return f"\\{text}"
        if text in _MATHML_OP_MAP:  # ellipsis sometimes appears in <mi>
            return _MATHML_OP_MAP[text]
        return _UNICODE_TO_LATEX.get(text, text)

    if tag == "mn":
        return node.get_text()

    if tag == "mo":
        return _MATHML_OP_MAP.get(node.get_text(), node.get_text())

    if tag in ("mrow", "mstyle", "math"):
        return render_all()

    if tag == "msup":
        kids = k()
        if len(kids) == 2:
            return f"{{{_mathml_to_latex(kids[0])}}}^{{{_mathml_to_latex(kids[1])}}}"
        return render_all()

    if tag == "msub":
        kids = k()
        if len(kids) == 2:
            return f"{{{_mathml_to_latex(kids[0])}}}_{{{_mathml_to_latex(kids[1])}}}"
        return render_all()

    if tag == "msubsup":
        kids = k()
        if len(kids) == 3:
            b, sub, sup = (_mathml_to_latex(kids[i]) for i in range(3))
            return f"{{{b}}}_{{{sub}}}^{{{sup}}}"
        return render_all()

    if tag == "mfrac":
        kids = k()
        if len(kids) == 2:
            return f"\\frac{{{_mathml_to_latex(kids[0])}}}{{{_mathml_to_latex(kids[1])}}}"
        return render_all()

    if tag == "msqrt":
        return f"\\sqrt{{{render_all()}}}"

    if tag == "mroot":
        kids = k()
        if len(kids) == 2:
            return f"\\sqrt[{_mathml_to_latex(kids[1])}]{{{_mathml_to_latex(kids[0])}}}"
        return render_all()

    if tag == "mspace":
        return r"\,"

    if tag == "mtext":
        return f"\\text{{{node.get_text()}}}"

    if tag == "mover":
        kids = k()
        if len(kids) == 2:
            base, acc = _mathml_to_latex(kids[0]), _mathml_to_latex(kids[1])
            acc_map = {"→": r"\vec", "˙": r"\dot", "¨": r"\ddot",
                       "^": r"\hat", "‾": r"\bar", "~": r"\tilde"}
            fn = acc_map.get(acc, r"\overset{" + acc + "}")
            return f"{fn}{{{base}}}"
        return render_all()

    if tag == "munder":
        kids = k()
        if len(kids) == 2:
            return f"\\underset{{{_mathml_to_latex(kids[1])}}}{{{_mathml_to_latex(kids[0])}}}"
        return render_all()

    if tag == "semantics":
        kids = k()
        return _mathml_to_latex(kids[0]) if kids else render_all()

    if tag == "annotation":
        return ""

    return render_all()


def _replace_math_elements(soup: BeautifulSoup) -> int:
    """Mutate soup in-place: replace every math element with $...$ / $$...$$ text.

    Returns how many elements were replaced. That count is the page saying
    what it is: prose does not ship MathML, KaTeX or MathJax, so a non-zero
    answer means a maths page with no guessing involved. Extraction uses it
    to decide whether the Unicode pass — which *is* a guess — runs at all.

    Strategies in priority order:
      1.  Wikipedia / MediaWiki  — <span class="mwe-math-element[-inline|-block]">
      1b. MathJax 3 rendered output — <mjx-container> (recover from assistive MathML)
      2.  General MathML w/ LaTeX annotation — <math> with <annotation encoding="application/x-tex">
      2b. Raw MathML            — <math> with no annotation (converted structurally)
      3.  KaTeX                  — <span class="katex"> with annotation inside
      4.  MathJax 2              — <script type="math/tex[; mode=display]">
      5.  Image math             — <img alt="LATEX"> (SVG/PNG rendered formulas)
    """
    found = 0

    # Every strategy replaces through here, so one added later cannot forget
    # to be counted.
    def swap(node, latex, display):
        nonlocal found
        found += 1
        node.replace_with(_math_replacement(latex, display))

    # 1. Wikipedia mwe-math-element spans
    for span in soup.find_all("span", class_="mwe-math-element"):
        ann = span.find("annotation", attrs={"encoding": "application/x-tex"})
        if not ann:
            continue
        classes = " ".join(span.get("class", []))
        display = "block" in classes and "inline" not in classes
        swap(span, ann.get_text(), display)

    # 1b. MathJax 3 rendered output. After client-side typesetting the TeX
    # source is gone from the DOM text; recover it from the assistive-MathML
    # copy inside the container. Must run before the generic <math> pass so
    # the whole container (including its SVG/CHTML duplicate) is replaced.
    for container in soup.find_all("mjx-container"):
        math = container.find("math")
        display = container.get("display") == "true" or (
            math is not None and math.get("display") == "block"
        )
        latex = ""
        if math is not None:
            ann = math.find("annotation", attrs={"encoding": "application/x-tex"})
            latex = (
                ann.get_text() if ann
                else (math.get("alttext") or _mathml_to_latex(math))
            )
        if latex.strip():
            swap(container, latex, display)
        else:
            container.decompose()

    # 2. Bare <math> elements — prefer embedded LaTeX annotation, then alttext,
    #    then structural MathML→LaTeX conversion as last resort
    for math in soup.find_all("math"):
        display = math.get("display") == "block"
        ann = math.find("annotation", attrs={"encoding": "application/x-tex"})
        if ann:
            swap(math, ann.get_text(), display)
        elif math.get("alttext"):
            swap(math, math["alttext"], display)
        else:
            latex = _mathml_to_latex(math)
            if latex.strip():
                swap(math, latex, display)

    # 3. KaTeX rendered spans  (.katex wraps both inline and display)
    for katex in soup.find_all(class_="katex"):
        ann = katex.find("annotation", attrs={"encoding": "application/x-tex"})
        if ann:
            display = bool(katex.find_parent(class_="katex-display"))
            swap(katex, ann.get_text(), display)

    # 4. MathJax 2 script tags — type is "math/tex", optionally with a
    # "; mode=display" suffix (spacing around ";" varies between sites).
    for tag in soup.find_all("script", attrs={"type": re.compile(r"^\s*math/tex")}):
        display = "display" in tag.get("type", "")
        swap(tag, tag.string or "", display)

    # 5. Image-rendered math: <img alt="LATEX ..."> (e.g. SVG formula images).
    for img in soup.find_all("img"):
        alt = img.get("alt", "")
        if not _RE_LATEX_ALT.search(alt):
            continue
        parent = img.parent
        sole = len([s for s in parent.children if str(s).strip()]) == 1
        display = sole and parent.name in ("p", "div", "figure", "td", "li")
        swap(img, alt, display)

    # Remove any leftover rendered MathJax output nodes (duplicates)
    for node in soup.find_all(True, class_=re.compile(r"MathJax|mjx-", re.I)):
        node.decompose()

    return found


def _is_footnote_marker(tag) -> bool:
    """Heuristic: does this <sub>/<sup> look like a footnote anchor (not math)?"""
    content = tag.get_text().strip()
    # Bracketed numbers ([1], [12]) and footnote symbols are always footnotes.
    if re.fullmatch(r"\[\d+\]|[*†‡§¶]", content):
        return True
    # Wrapped in or containing an <a> — clicking it jumps to a footnote.
    if tag.find_parent("a") or tag.find("a"):
        return True
    # Bare 1–3 digits AND not directly after a letter/digit (so x², H₂O survive).
    if tag.name == "sup" and re.fullmatch(r"\d{1,3}", content):
        prev = tag.previous_sibling
        if prev is None or not isinstance(prev, NavigableString):
            return True
        prev_text = str(prev).rstrip()
        if not prev_text or not prev_text[-1].isalnum():
            return True
    return False


def _preserve_sub_sup(soup: BeautifulSoup) -> None:
    """Convert <sub>/<sup> to LaTeX _{} / ^{} so trafilatura preserves them.

    Skips footnote markers — see `_is_footnote_marker`. Footnote `<sup>1</sup>`
    anchors would otherwise become spurious `^{1}` math superscripts in prose.
    """
    for tag in soup.find_all(["sub", "sup"]):
        content = tag.get_text()
        if not content:
            continue
        if _is_footnote_marker(tag):
            tag.decompose()  # drop entirely — no useful semantic in standalone doc
            continue
        notation = "_{%s}" if tag.name == "sub" else "^{%s}"
        tag.replace_with(notation % content)


# A display block or an inline span — used to protect existing math regions
# from the prose-wrapping pass below.
_RE_MATH_SPAN = re.compile(r"\$\$.*?\$\$|(?<!\$)\$(?!\$)[^$\n]+\$", re.DOTALL)
# Code fences and inline spans, so math can be looked for outside them.
# CommonMark, to the extent it matters here: a fence may be indented up to
# three spaces, and one that is never closed runs to the end of the document.
# Indented (four-space) code blocks are not stripped, and do not need to be —
# extraction emits fences for every <pre>, measured on a shell article.
_RE_FENCED_CODE = re.compile(
    r"^ {0,3}(?P<fence>```+|~~~+).*?(?:^ {0,3}(?P=fence)[ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`+[^`\n]*`+")


def _has_math_outside_code(body: str) -> bool:
    """Whether the text carries math anywhere it could be typeset.

    A shell article is full of $HOME, $PATH and {print $1, $3}, which look
    exactly like inline math and are not. MathJax skips <pre> and <code> —
    verified in a browser: a page with one real formula and two shell blocks
    typesets one thing and leaves the blocks byte-for-byte intact — so
    counting them meant tagging a page about shell scripts as maths and
    fetching a megabyte of JavaScript to typeset nothing.
    """
    outside = _RE_FENCED_CODE.sub("", body)
    outside = _RE_INLINE_CODE.sub("", outside)
    return bool(_RE_MATH_SPAN.search(outside))

# Isolated = not adjacent to any letter (Unicode-aware: `[^\W\d_]` is "letter"),
# so Greek prose like Πλάτων is left alone while a lone θ still gets wrapped.
_RE_UNICODE_SYM = re.compile(
    r"(?<![^\W\d_])(?<![$\\])("
    + "|".join(map(re.escape, _UNICODE_TO_LATEX))
    + r")(?![^\W\d_])(?!\$)"
)


def _apply_unicode_latex(body: str) -> str:
    """Convert Unicode math symbols to LaTeX.

    Inside existing math regions ($...$ and $$...$$) symbols become their LaTeX
    commands; in prose, isolated symbols are additionally wrapped in $...$.
    Processing the two separately keeps the prose pass from nesting a new $...$
    inside an existing block.
    """
    def wrap_symbol(m: re.Match) -> str:
        return f"${_UNICODE_TO_LATEX[m.group(0)]}$"

    def convert_in_math(math: str) -> str:
        for uni, latex in _UNICODE_TO_LATEX.items():
            if uni in math:
                # Trailing space so the command can't fuse with a following letter
                math = math.replace(uni, latex + " ")
        return math

    out, pos = [], 0
    for m in _RE_MATH_SPAN.finditer(body):
        out.append(_RE_UNICODE_SYM.sub(wrap_symbol, body[pos:m.start()]))
        out.append(convert_in_math(m.group(0)))
        pos = m.end()
    out.append(_RE_UNICODE_SYM.sub(wrap_symbol, body[pos:]))
    body = "".join(out)

    # Merge $X$_{n} / $X$^{n} → $X_{n}$ / $X^{n}$
    body = re.sub(r"\$([^$]+)\$([_^]\{[^}]+\})", r"$\1\2$", body)
    return body


def _escape_math_special(body: str) -> str:
    """Escape characters that are valid in MathJax but break LaTeX compilation."""
    def fix(m: re.Match) -> str:
        return re.sub(r"(?<!\\)%", r"\\%", m.group(0))

    body = re.sub(r"\$\$.+?\$\$", fix, body, flags=re.DOTALL)
    body = re.sub(r"(?<!\$)\$(?!\$)[^$\n]+\$", fix, body)
    return body


def _extract_url_content(html: str, url: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    og = soup.find("meta", property="og:title")
    raw_title = (og.get("content") if og else None) or (
        soup.title.get_text() if soup.title else url
    )
    title = _clean_title(raw_title)

    # The page's own answer to "is this maths": prose does not ship MathML,
    # KaTeX or MathJax.
    math_found = _replace_math_elements(soup)
    _preserve_sub_sup(soup)

    body = trafilatura.extract(
        str(soup),
        include_formatting=True,
        output_format="markdown",
        with_metadata=False,
        include_links=False,
    ) or ""

    body = _RE_BLOCK.sub(r"\n$$\n\1\n$$\n", body)
    body = _RE_INLINE.sub(r"$\1$", body)
    # Only on a page that already showed real math. The Unicode pass wraps
    # isolated Greek letters and symbols in $…$, which is right in a paper
    # where α is a variable and wrong everywhere else: measured on eight
    # ordinary sentences, seven came back rewritten — "The $\alpha$-version
    # shipped in March", "Costs rose $\approx$15%". A general read-later
    # queue is mostly those. What is lost is a maths post written in plain
    # Unicode with no markup, which is rare and still reads correctly.
    if math_found:
        body = _apply_unicode_latex(body)
    body = _escape_math_special(body)

    return title, body


# ---------------------------------------------------------------------------
# PDF → Markdown via Mathpix /v3/pdf  (async with polling)
# ---------------------------------------------------------------------------

# `.mmd` is the default output of /v3/pdf — it's always generated and fetchable
# at /v3/pdf/{pdf_id}.mmd. `conversion_formats` is only for *additional* outputs
# (docx, tex.zip, html); listing "mmd" there is now rejected as unknown.
_MATHPIX_PDF_OPTIONS = (
    '{"math_inline_delimiters":["$","$"],'
    '"math_display_delimiters":["$$","$$"],'
    '"rm_spaces":true}'
)


def _mathpix_headers() -> dict[str, str]:
    return {"app_id": MATHPIX_APP_ID, "app_key": MATHPIX_APP_KEY}


async def _mathpix_pdf(pdf_bytes: bytes) -> str:
    """Upload PDF, poll until done, return MMD text within one deadline."""
    deadline = time.monotonic() + 180

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise HTTPException(504, detail="Mathpix timed out (>3 min)")
        return left

    async def get_with_retry(client, url):
        delay = 1.0
        while True:
            try:
                answer = await client.get(
                    url, headers=_mathpix_headers(), timeout=remaining()
                )
                if answer.status_code not in (408, 429) and answer.status_code < 500:
                    answer.raise_for_status()
                    return answer
            except httpx.TransportError:
                pass
            wait = min(delay, remaining())
            await asyncio.sleep(wait)
            delay = min(delay * 2, 8)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.mathpix.com/v3/pdf",
            headers=_mathpix_headers(),
            files={"file": ("document.pdf", pdf_bytes, "application/pdf")},
            data={"options_json": _MATHPIX_PDF_OPTIONS},
            timeout=remaining(),
        )
        resp.raise_for_status()
        try:
            uploaded = resp.json()
        except ValueError:
            uploaded = None
        pdf_id = uploaded.get("pdf_id") if isinstance(uploaded, dict) else None
        if not isinstance(pdf_id, str) or not pdf_id:
            raise HTTPException(502, detail="Mathpix upload returned no PDF id")

        while True:
            await asyncio.sleep(min(3, remaining()))
            check = await get_with_retry(
                client, f"https://api.mathpix.com/v3/pdf/{pdf_id}"
            )
            try:
                progress = check.json()
            except ValueError:
                progress = None
            if not isinstance(progress, dict):
                raise HTTPException(502, detail="Mathpix status was not an object")
            status = progress.get("status", "")
            if not isinstance(status, str):
                raise HTTPException(502, detail="Mathpix status was not text")
            if status == "completed":
                break
            if status == "error":
                raise HTTPException(502, detail="Mathpix reported an error")

        mmd = await get_with_retry(
            client, f"https://api.mathpix.com/v3/pdf/{pdf_id}.mmd"
        )
        return _clean_text(mmd.text)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _err(msg: str) -> dict:
    """Uniform error shape — keeps HTTP 200 so iOS Shortcuts can surface the message.

    `summary` is a ready-made one-liner so a Shortcut needs a single
    Get-Dictionary-Value action for its notification.
    """
    return {"status": "error", "filename": "", "message": msg,
            "summary": f"Error: {msg}"}


def _clean_shortcut_url(v: str) -> str:
    """Repair URLs mangled by iOS Shortcuts.

    Shortcuts has two known issues:
    - Inserts whitespace inside long URLs when serialising to JSON.
    - Occasionally sends the URL twice separated by whitespace.

    Strategy: collapse all whitespace to nothing. If the result is exactly the
    same URL twice concatenated, return one copy. This preserves URLs that
    legitimately contained spaces in query strings (rare but legal).
    """
    joined = re.sub(r"\s+", "", v.strip())
    if joined and len(joined) % 2 == 0 and joined[: len(joined) // 2] == joined[len(joined) // 2 :]:
        return joined[: len(joined) // 2]
    return joined


# Addresses inside this machine or its network. Margin fetches whatever URL
# it is handed and then serves the result back through /read and /files, so
# without this a caller can read the server's own loopback services and its
# cloud metadata and collect the answer — and with MARGIN_TOKEN unset, the
# documented private-network default, that caller is anyone who can reach the
# port. Verified before this existed: POST /save-url with
# http://127.0.0.1:<port>/health saved the response into the output folder.
#
# Set MARGIN_ALLOW_PRIVATE_URLS=1 to turn it off — saving from an internal
# wiki on the same network is a real thing to want, and an operator who wants
# it should be able to say so.
ALLOW_PRIVATE_URLS = os.getenv("MARGIN_ALLOW_PRIVATE_URLS", "").strip().lower() \
    in ("1", "true", "yes", "on")
MAX_URL_CHARS = 8192

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")


def _ascii_host(url: str) -> str | None:
    """The hostname as a resolver will see it, or None if there is not one.

    One function, so the name that is *classified* is the name that will be
    *resolved*: IDNA folds "127。0。0。1" onto loopback, so checking the
    original and resolving the encoded form lets exactly that through.
    """
    text = str(url or "")
    if text != re.sub(r"[\x00-\x20]", "", text):
        return None                     # control characters inside the URL
    try:
        parsed = urlparse(text)
        host = parsed.hostname
        parsed.port                     # raises for an invalid port
    except ValueError:
        return None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)      # a literal address is a fine host
        return host
    except ValueError:
        pass
    try:
        # A final DNS root dot changes no destination (`localhost.` and
        # `127.0.0.1.` still resolve inward) but prevented literal/localhost
        # classification when it was left attached.
        encoded = host.encode("idna").decode("ascii").removesuffix(".")
    except (UnicodeError, ValueError):
        return None
    return encoded if _HOSTNAME_RE.fullmatch(encoded) else None


def _as_address(host: str):
    """The address this host denotes, including the resolver's shorthands.

    "127.1", "2130706433" and "0x7f000001" all reach 127.0.0.1 — inet_aton
    accepts them and so does every resolver, while ipaddress alone does not.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _address_is_public(address: str) -> bool:
    """The same test is_public_http_url applies, on a resolved address."""
    parsed = _as_address(address)
    if parsed is None:
        return False
    return (parsed.is_global and not parsed.is_multicast
            and not getattr(parsed, "is_site_local", False))


_RESOLVE_TTL_S = 60.0
_RESOLVE_MAX = 256
_resolved: dict = {}


async def _host_resolves_public(host: str, port: int) -> bool:
    """Whether every address this name resolves to is on the open internet.

    is_public_http_url answers "a name; the resolver decides", and nothing
    ever asked the resolver — so the address checks only ever stopped literal
    IPs, which is the one form nobody has to use. Not a rebinding race, a
    plain bypass: localtest.me is a free public service resolving to
    127.0.0.1, and POST /save-url with http://localtest.me:<port>/health
    fetched this server's own health endpoint and filed the answer in the
    output folder where the queue reads it.

    Verdicts are kept for a minute so an image-heavy page does not resolve
    one host a hundred times. That leaves the genuine rebinding race open — a
    name that answers publicly now and inward a moment later — which closing
    properly means connecting to the address that was checked rather than to
    the name, with the Host header and TLS SNI carried across.
    """
    key = (host, port)
    now = time.monotonic()
    seen = _resolved.get(key)
    if seen is not None and now - seen[1] < _RESOLVE_TTL_S:
        return seen[0]
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port or None, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError):
        return False            # unresolvable is unreachable; refusing is free
    verdict = bool(infos) and all(
        _address_is_public(info[4][0]) for info in infos)
    _resolved[key] = (verdict, now)
    while len(_resolved) > _RESOLVE_MAX:
        _resolved.pop(next(iter(_resolved)))
    return verdict


def _default_port(scheme: str) -> int:
    return 443 if scheme.lower() == "https" else 80


async def _url_points_outward(url: str) -> bool:
    """The full policy: the URL's own form, and where its name resolves."""
    if ALLOW_PRIVATE_URLS:
        return True
    try:
        _validated_url(url)
    except (TypeError, ValueError):
        return False
    host = _ascii_host(url)
    if not host:
        return False
    parsed = urlparse(url)
    try:
        port = parsed.port or _default_port(parsed.scheme)
    except ValueError:
        return False
    return await _host_resolves_public(host, port)


def is_public_http_url(url: str) -> bool:
    """An http(s) URL that points at the open internet."""
    host = (_ascii_host(url) or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    address = _as_address(host)
    if address is None:
        return True                     # a name; the resolver decides
    # is_global covers carrier-grade NAT (100.64/10), which is neither
    # private nor reserved — but Python also calls multicast and IPv6
    # site-local addresses global, so those are excluded by name.
    return (address.is_global and not address.is_multicast
            and not getattr(address, "is_site_local", False))


def _validated_url(v: str) -> str:
    if _has_lone_surrogate(v) or _TEXT_CONTROLS.search(v):
        raise ValueError("URL contains invalid control or Unicode characters")
    url = _clean_shortcut_url(v)
    if len(url) > MAX_URL_CHARS:
        raise ValueError(f"URL is too long ({MAX_URL_CHARS} characters max)")
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    # Only web URLs — keeps the fetcher/headless browser away from file:// etc.
    if scheme not in ("http", "https"):
        raise ValueError("Only http(s) URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing credentials are not supported")
    if not ALLOW_PRIVATE_URLS and not is_public_http_url(url):
        raise ValueError(
            "That address is inside this machine or its network, and Margin "
            "would fetch it and then serve the result back. Set "
            "MARGIN_ALLOW_PRIVATE_URLS=1 if that is what you want.")
    return url


async def _validate_outbound_request(request: httpx.Request) -> None:
    """Apply the URL policy to every HTTPX request, including redirects.

    Both halves: the URL's own form, and the addresses its name resolves to.
    The first alone let any hostname through, which is the whole of the
    bypass — a name is exactly how you reach 127.0.0.1 without writing it.
    """
    url = str(request.url)
    try:
        _validated_url(url)
    except (TypeError, ValueError) as exc:
        raise httpx.RequestError(
            f"outbound URL violates Margin's policy: {exc}",
            request=request,
        ) from None
    if not await _url_points_outward(url):
        raise httpx.RequestError(
            "outbound URL resolves inside this machine or its network: "
            f"{request.url.host}",
            request=request,
        ) from None


async def _browser_url_allowed(url: str) -> bool:
    """The same policy for everything Chromium fetches, redirects included.

    Advisory for the address itself — Chromium does its own DNS, so this
    cannot pin what it connects to — but it is what stops a public page from
    naming a host that resolves inward and using the browser as the bridge.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme in ("data", "blob", "about"):
        return True
    if scheme not in ("http", "https"):
        return False
    return await _url_points_outward(url)


class URLPayload(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def strip_url(cls, v: str) -> str:
        return _validated_url(v)


class SavePayload(BaseModel):
    url: str
    formats: list[str] = list(DEFAULT_FORMATS)
    force: bool = False  # save again even if this URL was saved before

    @field_validator("url")
    @classmethod
    def strip_url(cls, v: str) -> str:
        return _validated_url(v)

    @field_validator("formats", mode="before")
    @classmethod
    def check_formats(cls, v) -> list[str]:
        # Accept a comma/space-separated string too — iOS Shortcuts can send a
        # plain text body far more easily than a JSON array.
        if isinstance(v, str):
            v = re.split(r"[\s,]+", v)
        if not isinstance(v, (list, tuple)):
            raise ValueError("formats must be a list or comma-separated text")
        if any(not isinstance(f, str) for f in v):
            raise ValueError("every format must be text")
        normalized = [f.lower().lstrip(".") for f in v if f]
        normalized = list(dict.fromkeys(normalized)) or list(DEFAULT_FORMATS)
        unknown = set(normalized) - set(_ALL_FORMATS)
        if unknown:
            raise ValueError(
                f"Unknown formats {sorted(unknown)}; "
                "use any of 'pdf', 'md', 'tex', 'org'"
            )
        return normalized


@app.post("/echo")
async def echo(request: Request):
    """Debug endpoint — returns whatever the client sent, as JSON."""
    body_bytes = await request.body()
    try:
        body = await request.json()
    except Exception:
        body = body_bytes.decode(errors="replace")
    headers = dict(request.headers)
    for sensitive in ("authorization", "cookie"):
        if sensitive in headers:
            headers[sensitive] = "[redacted]"
    return {"method": request.method, "headers": headers, "body": body}


def _log(label: str, msg: str) -> None:
    print(f"[{_clean_text(label)}] {_clean_text(msg)}", file=sys.stderr, flush=True)


# Bodies shorter than this suggest a client-side-rendered page (the article is
# assembled in the browser) or a bot wall — worth retrying in headless Chromium.
_THIN_BODY_CHARS = 200


# Cap for PDFs downloaded from a URL (uploads have their own MAX_PDF_BYTES).
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
# HTML is read into a string and then parsed, so the page costs several times
# its own size: measured, a 200 MiB body peaked at 1001.1 MiB before anything
# was written. An article is tens of kilobytes and the largest page worth
# extracting from is a few megabytes, so this is generous and still bounded.
MAX_HTML_BYTES = 8 * 1024 * 1024


def _looks_like_pdf(data: bytes) -> bool:
    # ISO 32000 permits leading bytes before the header, but readers are only
    # required to find `%PDF-` within the first 1024 bytes.
    return b"%PDF-" in data[:1024]


def _inflate(data: bytes, encoding: str, limit: int) -> bytes | None:
    """At most `limit` bytes of `data` decompressed, or None if we cannot.

    zlib's decompress() takes a maximum length, which is the difference
    between bounding the output and hoping it is small. Anything we did not
    ask for — brotli, zstd — is simply not read.
    """
    windows = {"gzip": (16 + zlib.MAX_WBITS,), "x-gzip": (16 + zlib.MAX_WBITS,),
               "deflate": (zlib.MAX_WBITS, -zlib.MAX_WBITS)}.get(encoding)
    if windows is None:
        return None
    for wbits in windows:                 # "deflate" is raw as often as not
        try:
            return zlib.decompressobj(wbits).decompress(data, limit)
        except zlib.error:
            continue
    return None


def _too_large(url: str) -> str:
    """One phrasing for the download cap, in a unit that survives small caps."""
    cap = MAX_DOWNLOAD_BYTES
    size = (f"{cap // 2**20} MB" if cap >= 2**20 else
            f"{cap // 1024} kB" if cap >= 1024 else f"{cap} bytes")
    return f"The PDF at {url} is larger than the {size} download limit"


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    """The page at `url` as text, refusing to read more than MAX_HTML_BYTES.

    `client.get(...).text` reads whatever the server sends, and how much that
    is belongs to the server. Read the wire raw under a bound and expand any
    coding under the same one, as the PDF path does — decoding first would
    put the size back in the sender's hands.
    """
    async with client.stream(
            "GET", url,
            headers={"Accept-Encoding": "identity, gzip, deflate"}) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        encoding = resp.headers.get("content-encoding", "").strip().lower()
        chunks, size = [], 0
        async for chunk in resp.aiter_raw():
            size += len(chunk)
            if size > MAX_HTML_BYTES:
                raise _PageTooLarge(
                    f"{url} sent more than "
                    f"{MAX_HTML_BYTES // 2**20} MB of HTML")
            chunks.append(chunk)
        raw = b"".join(chunks)
    if encoding and encoding != "identity":
        expanded = _inflate(raw, encoding, MAX_HTML_BYTES + 1)
        if expanded is None:
            raise _PageTooLarge(f"{url} answered in a content coding "
                                f"({encoding}) this server did not ask for")
        if len(expanded) > MAX_HTML_BYTES:
            raise _PageTooLarge(
                f"{url} sent more than {MAX_HTML_BYTES // 2**20} MB of HTML "
                "once decompressed")
        raw = expanded
    # httpx picks the charset from the header and the body the same way
    # Response.text does; handing it the bytes keeps that judgement intact.
    return httpx.Response(200, headers={"content-type": content_type},
                          content=raw).text


class _TooLarge(Exception):
    """Something the size limits refused, with a reason a client can be told."""


class _PageTooLarge(_TooLarge):
    """An HTML body over MAX_HTML_BYTES."""


class _DownloadTooLarge(_TooLarge):
    """A direct PDF over MAX_DOWNLOAD_BYTES."""


async def _fetch_pdf_bytes(client: httpx.AsyncClient, url: str) -> bytes | None:
    """Return the raw bytes if `url` serves a PDF directly, else None."""
    ctype, answered = "", False
    try:
        head = await client.head(url)
        # 405 and 403 are how servers say "not this method", not "not a PDF".
        if head.status_code < 400:
            ctype = head.headers.get("content-type", "").lower()
            answered = True
    except httpx.HTTPError:
        pass  # many servers reject HEAD outright — the GET can still answer
    looks_pdf = ("application/pdf" in ctype
                 or urlparse(url).path.lower().endswith(".pdf"))
    # A URL with no .pdf used to end it here whenever HEAD had not said
    # otherwise — including when HEAD had failed and said nothing at all, so
    # an extensionless PDF behind a HEAD-hostile server was never recognised
    # and its GET's content type never looked at. Give up early only when
    # HEAD answered and named something else.
    if not looks_pdf and answered:
        return None
    try:
        # A PDF is already compressed, so a content coding on top of one buys
        # nothing — but servers send them anyway, and httpx's aiter_bytes
        # hands over what the decoder produced. A cap counted there is a cap
        # on bytes that already exist, and how many that is belongs to whoever
        # compressed them: measured against a 300 MB body in 299 kB of gzip,
        # a 10 MB cap peaked at 142.9 MiB. Reading the wire raw and expanding
        # under a bound puts the same case at 21.2 MiB — twice the cap, and
        # proportional to it rather than to the compression ratio.
        async with client.stream(
                "GET", url,
                headers={"Accept-Encoding": "identity, gzip, deflate"}) as resp:
            resp.raise_for_status()
            if "application/pdf" not in resp.headers.get("content-type", "").lower():
                return None
            chunks, size = [], 0
            async for chunk in resp.aiter_raw():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise _DownloadTooLarge(_too_large(url))
                chunks.append(chunk)
            data = b"".join(chunks)
            encoding = resp.headers.get("content-encoding", "").strip().lower()
        if encoding and encoding != "identity":
            data = _inflate(data, encoding, MAX_DOWNLOAD_BYTES + 1)
            if data is None:
                return None                 # a coding we did not ask for
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise _DownloadTooLarge(_too_large(url) + " once decompressed"
                )
        return data if _looks_like_pdf(data) else None
    except httpx.HTTPError:
        return None


async def _write_binary_async(filename: str, data: bytes,
                              reuse_stem: bool = False) -> Path:
    """Off the loop: a PDF is megabytes, and the write is followed by fsync,
    an atomic replace and a lock. Measured with a 400ms write, an async
    heartbeat stopped entirely — the same reason the multi-format writer was
    moved off the loop."""
    return await asyncio.to_thread(_write_binary, filename, data, reuse_stem)


def _write_binary(filename: str, data: bytes, reuse_stem: bool = False) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        path = _family_path(OUTPUT_DIR / filename,
                            (Path(filename).suffix.lstrip("."),),
                            reuse_stem=reuse_stem)
        _write_bytes_atomically(path, data)
    return path


# --- Duplicate detection -----------------------------------------------------
# Saved URLs are recorded in a small JSON index (url → file stems) inside the
# output dir. On a repeat save the request is skipped with a note instead of
# piling up -2/-3 copies; `"force": true` bypasses the check.

def _norm_url(url: str) -> str:
    """Canonical form for duplicate comparison: drop fragment, trailing '/'."""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}" + (
        f"?{p.query}" if p.query else ""
    )


def _url_index_path() -> Path:
    return OUTPUT_DIR / ".saved-urls.json"


def _quarantine_url_index(path: Path, reason: str) -> None:
    spoiled = path.with_name(
        f"{path.stem}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:6]}{path.suffix}"
    )
    try:
        path.rename(spoiled)
        _log("index", f"URL index unusable ({reason}); kept as {spoiled.name}")
    except OSError as move_error:
        _log("index", f"URL index unusable ({reason}); could not quarantine it: {move_error}")


def _load_url_index() -> dict[str, list[str]]:
    path = _url_index_path()
    if path.is_symlink() and not _inside(path, OUTPUT_DIR):
        _log("index", "URL index is a link outside the output directory; ignoring it")
        return {}
    try:
        idx = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as e:
        _log("index", f"URL index unreadable ({e}); duplicate detection disabled")
        return {}
    except (ValueError, UnicodeError) as e:
        # Preserve the evidence and make the next atomic save create a clean
        # index rather than silently overwriting the only copy.
        _quarantine_url_index(path, str(e))
        return {}
    if not isinstance(idx, dict):
        _quarantine_url_index(path, f"expected an object, found {type(idx).__name__}")
        return {}
    clean: dict[str, list[str]] = {}
    for url, stems in idx.items():
        if (not isinstance(url, str) or _has_lone_surrogate(url)
                or re.search(r"[\x00-\x20\x7f]", url)
                or not isinstance(stems, list)):
            continue
        valid = [s for s in stems if isinstance(s, str)
                 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]*", s)]
        if valid:
            clean[url] = list(dict.fromkeys(valid))
    return clean


def _record_url(url: str, stem: str) -> None:
    idx = _load_url_index()
    stems = idx.setdefault(_norm_url(url), [])
    if stem not in stems:
        stems.append(stem)
    # Written atomically: this file lives in a folder a sync client watches,
    # and write_text truncates before it writes. A crash or a mid-write read
    # leaves a half file, which _load_url_index cannot parse — and it answers
    # an unparsable index with an empty one, so every recorded URL is
    # silently forgotten and every page looks new again.
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _write_json_atomically(_url_index_path(), idx)
    except OSError as e:
        _log("index", f"could not write URL index: {e}")


def _write_json_atomically(path: Path, data) -> None:
    """Write to a temp file in the same directory, then rename over the top.

    rename(2) within a directory is atomic, so a reader sees either the old
    file or the new one — never the half-written state a truncating write
    leaves behind whenever it is interrupted.
    """
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent),
        prefix=f".{path.name}.", suffix=".tmp", delete=False)
    try:
        with tmp as fh:
            json.dump(data, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp.name, path)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def _files_for_stems(stems: set[str]) -> list[Path]:
    files = []
    for folder in (OUTPUT_DIR, _archive_dir()):
        if folder.is_dir() and _storage_folder_safe(folder):
            files += [f for f in folder.iterdir()
                      if f.is_file() and _inside(f, folder) and f.stem in stems
                      and f.suffix.lower() in _SERVE_EXTS]
    return files


def _covers_formats(existing: dict, formats: tuple[str, ...]) -> bool:
    """Whether an earlier save already produced every format now asked for.

    "Already saved" used to mean "some file exists for this URL", so a
    capture whose Mathpix step had failed could never be completed: asking
    for the missing Markdown was answered with the PDF that was already
    there, and --force was the only way through.
    """
    have = {Path(name).suffix.lstrip(".").lower() for name in existing["files"]}
    return set(formats).issubset(have)


def _find_existing(url: str) -> dict | None:
    """Return {'files', 'title'} for a still-present earlier save of `url`."""
    norm = _norm_url(url)
    files = _files_for_stems(set(_load_url_index().get(norm, [])))
    if not files:
        # Saves that predate the index: match source_url in Markdown frontmatter
        for folder in (OUTPUT_DIR, _archive_dir()):
            if not folder.is_dir() or not _storage_folder_safe(folder):
                continue
            for md in folder.glob("*.md"):
                if not _inside(md, folder):
                    continue
                src = _read_frontmatter_field(md, "source_url")
                if src and _norm_url(src) == norm:
                    files = _files_for_stems({md.stem})
                    break
            if files:
                break
    if not files:
        return None
    files.sort(key=lambda f: _SERVE_EXTS.index(f.suffix.lower()))
    md = next((f for f in files if f.suffix.lower() == ".md"), None)
    title = (_read_frontmatter_field(md, "title") if md else None) or _pretty_stem(
        files[0].stem
    )
    return {"files": [f.name for f in files], "title": title,
            "stem": files[0].stem,
            # Which folder it is in decides whether a missing format can be
            # added to it: writing into the inbox beside an archived family
            # would leave one item spread across both.
            "archived": files[0].parent != OUTPUT_DIR}


# URLs being captured right now. The duplicate check runs before any file
# exists, so two requests for one URL both passed it and both rendered the
# page and spent Mathpix credits on it.
_in_flight: set = set()


@contextlib.asynccontextmanager
async def _reserve_url(url: str):
    """Hold a URL for the length of one capture; yields False if it is taken."""
    key = _norm_url(url)
    if key in _in_flight:
        yield False
        return
    _in_flight.add(key)
    try:
        yield True
    finally:
        _in_flight.discard(key)


def _duplicate_response(existing: dict) -> dict:
    return {
        "status": "ok",
        "duplicate": True,
        "title": existing["title"],
        "files": existing["files"],
        "filename": existing["files"][0],
        "message": f"Already saved ({existing['files'][0]}) — "
                   'send "force": true to save again',
        "summary": f"Already saved: {existing['files'][0]}",
    }


# --- Direct-PDF titles -------------------------------------------------------

_RE_ARXIV_PDF = re.compile(
    r"^https?://arxiv\.org/pdf/(?P<id>[^?#]+?)(?:\.pdf)?/?$", re.I
)


def _pdf_metadata_title(data: bytes) -> str | None:
    if PdfReader is None:
        return None
    try:
        meta = PdfReader(io.BytesIO(data)).metadata
        title = str(meta.title).strip() if meta and meta.title else ""
        return title or None
    except Exception:
        return None


async def _direct_pdf_title(client: httpx.AsyncClient, url: str,
                            data: bytes) -> str:
    """Best-effort title for a directly-downloaded PDF.

    arXiv abstract page > embedded PDF metadata > URL path stem.
    """
    m = _RE_ARXIV_PDF.match(url)
    if m:
        try:
            abs_page = await client.get(f"https://arxiv.org/abs/{m.group('id')}")
            abs_page.raise_for_status()
            soup = BeautifulSoup(abs_page.text, "html.parser")
            if soup.title:
                # arXiv titles look like "[1706.03762] Attention Is All You Need"
                title = re.sub(r"^\[[^\]]+\]\s*", "", soup.title.get_text().strip())
                if title:
                    return title
        except httpx.HTTPError as e:
            _log("save", f"arXiv abs lookup failed: {e}")
    return (_pdf_metadata_title(data)
            or Path(urlparse(url).path).stem
            or "document")


def _no_text_format_error() -> dict:
    """The one refusal DEFAULT_FORMATS can produce, in words that name the fix."""
    return _err(
        f"DEFAULT_FORMATS is {','.join(DEFAULT_FORMATS)}, which asks for no "
        "text format, and this endpoint writes text and nothing else. "
        'POST /save with {"formats": ["pdf"]} to capture a page instead, or '
        "add md, tex or org to DEFAULT_FORMATS.")


async def _run_markdown_save(
    url: str, request: Request, formats: tuple[str, ...] = _DEFAULT_MD_FORMATS,
    preferred_stem: str | None = None,
) -> dict:
    """Markdown pipeline: fetch, extract content + math, write the requested
    subset of .md/.tex/.org."""
    if not formats:
        return _no_text_format_error()
    _log("save-url", f"fetching {url}")
    client: httpx.AsyncClient = request.app.state.client
    renderer: Renderer = request.app.state.renderer

    html, fetch_err, retry_render = None, "", True
    try:
        html = await _fetch_html(client, url)
    except _PageTooLarge as e:
        # Not a bot wall and not a thin page: rendering it in Chromium would
        # read the same bytes again, with a browser on top.
        fetch_err, retry_render = str(e), False
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        fetch_err = f"Site returned {code}: {url}"
        # Only bot-wall-ish statuses are worth retrying in a real browser —
        # a 404/410 is a genuine miss and would just save the error page.
        retry_render = code in (401, 403, 406, 429, 503)
    except httpx.RequestError as e:
        fetch_err = f"Could not reach {url}: {e}"

    title, body, challenged = "", "", ""
    if html is not None:
        try:
            title, body = _extract_url_content(html, url)
        except Exception as e:
            _log("save-url", f"extract failed: {e}\n{traceback.format_exc()}")
            return _err(f"Extraction failed: {e}")
        # A bot wall can answer 200 with a page long enough to look like an
        # article — "Just a moment…" plus a paragraph of explanation
        # extracts as ~2000 characters. The status and the length both say
        # yes; only the title says what it is, and looks_blocked already
        # knows. Treat it as no content, which is what it is, so the render
        # retry below gets its turn.
        if looks_blocked(title):
            _log("save-url", f"challenge page from plain fetch: {title!r}")
            challenged = title
            title, body = "", ""
            retry_render = True

    # Plain fetch blocked or page is client-side rendered → retry in headless
    # Chromium, which executes the JS before we extract.
    if (
        (html is None or len(body.strip()) < _THIN_BODY_CHARS)
        and retry_render
        and renderer.available
    ):
        reason = fetch_err or f"thin body ({len(body.strip())} chars)"
        _log("save-url", f"{reason} — retrying with headless render")
        try:
            rendered = await renderer.render(url)
            r_title, r_body = _extract_url_content(rendered.html, url)
            if looks_blocked(rendered.title):
                _log("save-url", f"render fallback blocked: {rendered.title!r}")
            elif len(r_body.strip()) > len(body.strip()):
                title = _clean_title(rendered.title) or r_title or title
                body = r_body
        except Exception as e:
            _log("save-url", f"render fallback failed: {e}")

    if looks_blocked(title) or (challenged and not body.strip()):
        seen = title or challenged
        return _err(f"{url} answered with a bot-check page, not an article "
                    f"({seen!r}) — try POST /save with "
                    '{"formats": ["pdf"]} once you can reach it in a browser')
    if not body.strip():
        return _err(
            fetch_err
            or f"No article content could be extracted from {url} — "
               'try POST /save with {"formats": ["pdf"]} instead'
        )
    title = title or url

    md = _frontmatter(title, url, has_math=_has_math_outside_code(body)) + "\n" + body
    try:
        filename = f"{preferred_stem}.md" if preferred_stem else _filename(title)
        # Off the loop: Pandoc is subprocess.run(timeout=30) and Margin asks
        # for .tex and .org, so a save froze the whole server for as long as
        # it took — measured, a 3.0s write let the loop run 4 ticks where it
        # should have run about 64, so /health, the queue and every other
        # save waited it out.
        written = await asyncio.to_thread(
            _write_all_formats,
            filename, md, title, formats, preferred_stem is not None
        )
    except Exception as e:
        _log("save-url", f"write failed: {e}\n{traceback.format_exc()}")
        return _err(f"Could not write file: {e}")
    if not written:
        # Only possible when just tex/org were requested and Pandoc is absent
        return _err("Pandoc is required for .tex/.org output but is not "
                    "installed on the server")
    _log("save-url", f"saved → {', '.join(p.name for p in written)}")
    _record_url(url, written[0].stem)
    return {
        "status": "ok",
        "filename": written[0].name,
        "files": [p.name for p in written],
        "title": title,
        "summary": f"Saved: {written[0].name}",
    }


@app.post("/save-url")
async def save_url(payload: URLPayload, request: Request):
    """Fetch a web page, extract content + math, save Markdown to the output dir."""
    if not _DEFAULT_MD_FORMATS:
        return _no_text_format_error()
    existing = _find_existing(payload.url)
    if existing and _covers_formats(existing, _DEFAULT_MD_FORMATS):
        _log("save-url", f"duplicate of {existing['files'][0]}: {payload.url}")
        return _duplicate_response(existing)
    async with _reserve_url(payload.url) as reserved:
        if not reserved:
            return _err(f"Already saving {payload.url} — try again in a moment")
        return await _run_markdown_save(payload.url, request)


def _mathpix_missing_warning(md_formats: tuple[str, ...]) -> str:
    exts = "/".join("." + f for f in md_formats)
    return (f"PDF → {exts} needs Mathpix credentials "
            "(MATHPIX_APP_ID / MATHPIX_APP_KEY); text formats skipped")


@app.post("/save")
async def save(payload: SavePayload, request: Request):
    """One capture of a URL at a time; the work is in _save_reserved."""
    async with _reserve_url(payload.url) as reserved:
        if not reserved:
            return _err(f"Already saving {payload.url} — try again in a moment")
        return await _save_reserved(payload, request)


async def _save_reserved(payload: SavePayload, request: Request):
    """Save any page into the requested formats (default: DEFAULT_FORMATS).

    A web page → PDF via headless Chromium + Markdown via the HTML pipeline.
    A URL that serves a PDF directly → the PDF stored as-is + Markdown/LaTeX
    via Mathpix OCR (skipped with a warning when Mathpix isn't configured).
    Either way the same formats are produced, so a save is consistent
    regardless of the source."""
    _log("save", f"{payload.formats} {payload.url}")
    client: httpx.AsyncClient = request.app.state.client
    renderer: Renderer = request.app.state.renderer

    existing = None
    if not payload.force:
        existing = _find_existing(payload.url)
        if existing and _covers_formats(existing, payload.formats):
            _log("save", f"duplicate of {existing['files'][0]}: {payload.url}")
            return _duplicate_response(existing)

    # An earlier save that produced only some of these formats — a capture
    # whose Mathpix or render step failed — is completed in place. Repeating
    # the whole set instead allocated a fresh stem for the formats that
    # already existed, so one URL became two queue entries and the PDF was
    # written twice.
    formats = tuple(payload.formats)
    reuse_stem = None
    if existing:
        if existing["archived"]:
            answer = _duplicate_response(existing)
            answer["message"] = (
                f"Already saved and archived ({existing['files'][0]}); "
                "restore it before adding formats, or send \"force\": true "
                "to save a second copy")
            return answer
        have = {Path(name).suffix.lstrip(".").lower()
                for name in existing["files"]}
        formats = tuple(f for f in payload.formats if f not in have)
        reuse_stem = existing["stem"]
        _log("save", f"completing {reuse_stem} with {list(formats)}")

    saved: list[str] = []
    errors: list[str] = []
    title = ""
    want_pdf = "pdf" in formats
    md_formats = tuple(f for f in formats if f in MD_FORMATS)

    # Probe once: does the URL serve a PDF directly? (One HEAD for web pages.)
    try:
        pdf_bytes = await _fetch_pdf_bytes(client, payload.url)
    except _DownloadTooLarge as e:
        # Not "this is not a PDF". Falling through to the web-page branch
        # handed the same URL to Chromium, which loaded and printed the file
        # the download limit had just refused — and answered "ok".
        _log("save", f"refused: {e}")
        return _err(str(e))
    except Exception as e:
        _log("save", f"direct-pdf probe failed: {e}")
        pdf_bytes = None

    if pdf_bytes is not None:
        # ---- Source is a PDF: store it as-is, OCR it for the text formats ----
        title = _clean_title(await _direct_pdf_title(client, payload.url, pdf_bytes))
        stem = reuse_stem
        if want_pdf:
            # Inside the error handling like every other write. Outside it, a
            # full disk or a read-only folder left this branch answering HTTP
            # 500 instead of the JSON error result the API documents.
            try:
                pdf_path = await _write_binary_async(
                    f"{reuse_stem}.pdf" if reuse_stem else _filename(title, "pdf"),
                    pdf_bytes, reuse_stem=reuse_stem is not None)
            except OSError as e:
                _log("save", f"direct-PDF write failed: {e}")
                return _err(f"Could not write the PDF: {e}")
            saved.append(pdf_path.name)
            stem = pdf_path.stem
            _record_url(payload.url, stem)
            _log("save", f"saved direct PDF → {pdf_path.name}")
        if md_formats and not (MATHPIX_APP_ID and MATHPIX_APP_KEY):
            errors.append(_mathpix_missing_warning(md_formats))
        elif md_formats:
            try:
                mmd = await _mathpix_pdf(pdf_bytes)
                md = _frontmatter(title, payload.url, has_math=_has_math_outside_code(mmd)) + "\n" + mmd
                written = await asyncio.to_thread(
                    _write_all_formats,
                    f"{stem}.md" if stem else _filename(title), md, title,
                    md_formats, stem is not None,
                )
                saved.extend(p.name for p in written)
                if written and stem is None:
                    _record_url(payload.url, written[0].stem)
                _log("save", f"OCR'd PDF → {', '.join(p.name for p in written)}")
            except HTTPException as e:
                errors.append(f"Mathpix: {e.detail}")
            except Exception as e:
                _log("save", f"PDF OCR failed: {e}\n{traceback.format_exc()}")
                errors.append(f"PDF OCR failed: {e}")
    else:
        # ---- Source is a web page: render for PDF, extract HTML for text ----
        web_stem = reuse_stem
        if want_pdf:
            try:
                rendered = await renderer.render(payload.url)
                if looks_blocked(rendered.title):
                    raise RuntimeError(
                        f"site blocked headless access ({rendered.title!r})"
                    )
                if looks_missing(rendered.title, rendered.status):
                    raise RuntimeError(
                        f"page looks like an error page "
                        f"(status {rendered.status}, title {rendered.title!r})"
                    )
                title = _clean_title(rendered.title) or payload.url
                pdf_path = await _write_binary_async(
                    f"{reuse_stem}.pdf" if reuse_stem else _filename(title, "pdf"),
                    rendered.pdf, reuse_stem=reuse_stem is not None)
                saved.append(pdf_path.name)
                web_stem = pdf_path.stem
                if reuse_stem is None:
                    _record_url(payload.url, pdf_path.stem)
                _log("save", f"saved → {pdf_path.name}")
            except RendererUnavailable as e:
                errors.append(str(e))
            except Exception as e:
                _log("save", f"render failed: {e}\n{traceback.format_exc()}")
                errors.append(f"Could not render {payload.url}: {e}")

        if md_formats:
            # Skip the endpoint's duplicate check — this request already passed
            # it (and the PDF above would otherwise count as a duplicate).
            md_result = await _run_markdown_save(
                payload.url, request, md_formats, preferred_stem=web_stem
            )
            if md_result.get("status") == "ok":
                saved.extend(md_result["files"])
                title = title or md_result["title"]
            else:
                errors.append(md_result.get("message", "markdown save failed"))

    if not saved:
        return _err("; ".join(errors) or "nothing saved")
    return {
        "status": "ok",
        "title": title,
        "files": saved,
        "summary": f"Saved: {', '.join(saved)}",
        **({"warnings": errors} if errors else {}),
    }


@app.post("/save-pdf")
async def save_pdf(file: UploadFile = File(...)):
    """Save an uploaded PDF: keep the file (if 'pdf' is a default format) and,
    with Mathpix configured, OCR it to the default text formats. Without
    Mathpix the PDF is still kept and the text step is skipped with a warning."""
    _log("save-pdf", f"received {file.filename}")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return _err("Expected a .pdf file")

    # The backstop: _limit_body refuses anything that declares an oversized
    # Content-Length, which is every ordinary client. A chunked upload
    # declares nothing, and reaches here.
    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_PDF_BYTES:
        return _err(f"PDF too large: {len(pdf_bytes)} bytes (max {MAX_PDF_BYTES})")
    if not _looks_like_pdf(pdf_bytes):
        return _err("The uploaded file is not a PDF")

    saved: list[str] = []
    errors: list[str] = []
    title = _clean_title(Path(file.filename).stem)

    # OCR first (it yields a better title from the document's first heading),
    # then write files under that title so the PDF and text share a stem.
    mmd = None
    if _DEFAULT_MD_FORMATS and not (MATHPIX_APP_ID and MATHPIX_APP_KEY):
        errors.append(_mathpix_missing_warning(_DEFAULT_MD_FORMATS))
    elif _DEFAULT_MD_FORMATS:
        try:
            mmd = await _mathpix_pdf(pdf_bytes)
            m = re.search(r"^#\s+(.+)$", mmd, re.MULTILINE)
            if m:
                title = _clean_title(m.group(1).strip())
        except HTTPException as e:
            errors.append(f"Mathpix: {e.detail}")
        except Exception as e:
            _log("save-pdf", f"mathpix crash: {e}\n{traceback.format_exc()}")
            errors.append(f"Mathpix call failed: {e}")

    stem = None
    if "pdf" in DEFAULT_FORMATS:
        try:
            pdf_path = await _write_binary_async(
                _filename(title, "pdf"), pdf_bytes)
            saved.append(pdf_path.name)
            stem = pdf_path.stem
        except Exception as e:
            _log("save-pdf", f"write failed: {e}\n{traceback.format_exc()}")
            errors.append(f"Could not write PDF: {e}")

    if mmd is not None:
        md = _frontmatter(title, has_math=_has_math_outside_code(mmd)) + "\n" + mmd
        try:
            written = await asyncio.to_thread(
                _write_all_formats,
                f"{stem}.md" if stem else _filename(title), md, title,
                _DEFAULT_MD_FORMATS, stem is not None,
            )
            saved.extend(p.name for p in written)
        except Exception as e:
            _log("save-pdf", f"write failed: {e}\n{traceback.format_exc()}")
            errors.append(f"Could not write text formats: {e}")

    if not saved:
        return _err("; ".join(errors) or "nothing saved")
    _log("save-pdf", f"saved → {', '.join(saved)}")
    return {
        "status": "ok",
        "filename": saved[0],
        "files": saved,
        "title": title,
        "summary": f"Saved: {', '.join(saved)}",
        **({"warnings": errors} if errors else {}),
    }


# ---------------------------------------------------------------------------
# Icon / manifest — served from static/ (public: favicon requests and
# home-screen installs don't carry credentials)
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# SVG first for browsers that support it; 32px PNG fallback for Safari,
# which ignores SVG favicons.
_HEAD = """<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon-32.png" type="image/png" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#EDE2C9">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="stylesheet" href="/static/style.css">"""


@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg():
    return FileResponse(_STATIC_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/favicon-32.png", include_in_schema=False)
async def favicon_png():
    return FileResponse(_STATIC_DIR / "favicon-32.png", media_type="image/png")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    # A genuine ICO container (16+32) — Safari won't accept anything else here.
    return FileResponse(_STATIC_DIR / "favicon.ico",
                        media_type="image/x-icon")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)  # legacy Safari probe
async def apple_touch_icon():
    return FileResponse(_STATIC_DIR / "apple-touch-icon.png")


@app.get("/static/{name}", include_in_schema=False)
async def static_file(name: str):
    path = _STATIC_DIR / name
    if "/" in name or "\\" in name or name.startswith(".") or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/service-worker.js", include_in_schema=False)
async def service_worker():
    # From the root, not /static/: a worker's scope is the directory it is
    # served from, and one under /static/ could not answer for "/".
    return FileResponse(_STATIC_DIR / "service-worker.js",
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    return FileResponse(_STATIC_DIR / "manifest.json",
                        media_type="application/manifest+json")


# Result page for the /save-page bookmarklet flow. Doubled braces are literal
# CSS braces (str.format).
_SAVE_PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Margin</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
__HEAD__
</head>
<body>
<main>
  <div class="notice {cls}">
    <h1>{heading}</h1>
    <p class="detail">{detail}</p>
  </div>
  {autoclose}
  <p><a href="/">← Margin</a></p>
</main>
</body></html>"""


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    """An error a browser can get out of.

    A stale bookmark, a file moved in the synced folder, a mistyped address —
    all ordinary, and a bare JSON body leaves the reader on a page with no
    way back. On the home screen there is not even a back button. Clients
    that asked for JSON still get JSON.
    """
    wants_html = ("text/html" in request.headers.get("accept", "")
                  and request.method == "GET")
    if not wants_html:
        return JSONResponse({"detail": exc.detail},
                            status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    detail = _html_escape(str(exc.detail or "Not here"))
    return HTMLResponse(
        _SAVE_PAGE_HTML.replace("__HEAD__", _HEAD).format(
            cls="bad", heading=f"{exc.status_code} — not here",
            detail=detail, autoclose=""),
        status_code=exc.status_code)


# Responses name files, never the folder they are in: the output directory's
# path names the account the service runs as, and a client addresses a save by
# its filename. The iOS Shortcut reads "summary"; nothing consumed "path".


def _save_page_response(ok: bool, heading: str, detail: str) -> HTMLResponse:
    autoclose = (
        "<p>This tab will close by itself.</p>"
        "<script>setTimeout(function () { window.close(); }, 2500)</script>"
        if ok else ""
    )
    return HTMLResponse(_SAVE_PAGE_HTML.replace("__HEAD__", _HEAD).format(
        cls="" if ok else "bad",
        heading=_html_escape(heading),
        detail=_html_escape(detail),
        autoclose=autoclose,
    ))


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """Return a bounded 422 even when the rejected input cannot be encoded."""
    detail = [{k: v for k, v in error.items() if k not in ("input", "ctx")}
              for error in exc.errors()]
    body = json.dumps({"detail": detail}, ensure_ascii=True, default=str)
    return Response(body, media_type="application/json", status_code=422)


@app.get("/save-page", response_class=HTMLResponse)
async def save_page(request: Request, url: str = "",
                    formats: list[str] = Query(default=[]),
                    force: bool = False):
    """Desktop-browser capture: open from a bookmarklet, get an HTML result.

    A bookmarklet opens this in a new tab (`window.open`). A top-level
    navigation is used instead of fetch() because browsers block fetch from
    an https page to a plain-http LAN server (mixed content), but allow
    navigating to it. `formats` accepts repeated parameters
    (?formats=pdf&formats=md — what the queue form sends) and/or
    comma-separated values (?formats=pdf,md — bookmarklet style).
    """
    fmt_list = [f for part in formats for f in re.split(r"[\s,]+", part) if f]
    try:
        payload = SavePayload(url=url, formats=fmt_list or list(DEFAULT_FORMATS),
                              force=force)
    except ValueError as e:
        return _save_page_response(False, "Invalid request", str(e))

    result = await save(payload, request)
    if result.get("status") == "ok":
        files = ", ".join(result.get("files", []))
        heading = "Already saved" if result.get("duplicate") else "Saved"
        return _save_page_response(True, heading,
                                   f"{result.get('title', '')} → {files}")
    return _save_page_response(False, "Save failed",
                               result.get("message", "unknown error"))


# ---------------------------------------------------------------------------
# Reading queue — GET / lists the inbox, POST /archive moves items, and
# GET /files/{name} serves the saved files. Together they make any browser a
# minimal read-later front end (no notes app or service required).
# ---------------------------------------------------------------------------

_SERVE_EXTS = (".pdf", ".md", ".tex", ".org")  # order = display order
_ARCHIVE_SUBDIR = "archive"
# A stem names files inside one folder, so what makes it safe is that it
# cannot leave that folder — not that it is ASCII. The old rule was
# ^[A-Za-z0-9][A-Za-z0-9._ -]*$, which listed "Über den Rand.md" in the queue
# and then answered 400 to both Archive and Delete, in a folder the README
# invites you to drop files into by hand.
_RE_UNSAFE_STEM = re.compile(r"[/\\]|[\x00-\x1f\x7f]")


def _safe_stem(stem: str) -> bool:
    return bool(stem) and stem not in (".", "..") and not stem.startswith(".") \
        and _RE_UNSAFE_STEM.search(stem) is None

# Placeholders (__ROWS__ etc.) are substituted with str.replace, so the CSS
# and JS braces below need no escaping.
_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Margin</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
__HEAD__
</head>
<body>
<main>
  <header class="masthead">
    <img src="/favicon.svg" alt="" width="30" height="30">
    <div>
      <h1><a href="/">Margin</a></h1>
      <p class="tagline">Save a page — read it later, in your own folder.</p>
    </div>
  </header>

  <form class="saver" method="get" action="/save-page">
    <div class="row">
      <input type="url" name="url" placeholder="https://…  save a page" required>
      <button type="submit">Save</button>
    </div>
    <details class="formats">
      <summary>Formats: <b id="fmt-summary"></b></summary>
      <div class="fmt-list">__FORMAT_CHECKBOXES__</div>
    </details>
  </form>

  <div class="queue-head">__TABS__</div>
  <!--offline-notice-->
  <input id="filter" type="search" placeholder="Filter by title…">
  __ROWS__
</main>
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/service-worker.js')
    .catch((e) => console.warn('SW registration failed', e));
}

// Tell the worker before the form navigates away: the 404-driven cleanup
// only fires if someone asks for the file again, and offline that may never
// happen — so a deleted page would stay readable for ever.
document.querySelectorAll('form[action="/delete"]').forEach(function (form) {
  form.addEventListener('submit', function (event) {
    // One listener owns both halves. With confirm() in an onsubmit attribute
    // and the message in a listener of its own, cancelling stopped the
    // navigation and sent forget-stem anyway: the server kept the item and
    // its offline copy vanished.
    if (!window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    const stem = form.querySelector('input[name=stem]').value;
    if (navigator.serviceWorker && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage(
        {type: 'forget-stem', stem: stem});
    }
  });
});

document.getElementById('filter').addEventListener('input', function () {
  const q = this.value.toLowerCase();
  document.querySelectorAll('.item').forEach(function (el) {
    el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
});

// The date is written as an ISO stamp so it is still right without script;
// this reads it back in whatever order the reader's locale puts it.
document.querySelectorAll('time[datetime]').forEach(function (el) {
  const when = new Date(el.getAttribute('datetime') + 'T12:00:00');
  if (isNaN(when.getTime())) return;
  const parts = { month: 'short', day: 'numeric' };
  if (when.getFullYear() !== new Date().getFullYear()) parts.year = 'numeric';
  el.textContent = when.toLocaleDateString([], parts);
});

// Format checkboxes: restore last choice, keep the summary line current.
(function () {
  const NAMES = { pdf: 'PDF', md: 'Markdown', tex: 'LaTeX', org: 'Org' };
  const boxes = Array.from(
    document.querySelectorAll('.fmt-list input[type=checkbox]'));
  let saved = null;
  try { saved = localStorage.getItem('margin-formats'); } catch (e) { /* private mode */ }
  if (saved !== null) {
    const picked = saved.split(',');
    boxes.forEach(b => { b.checked = picked.includes(b.value); });
  }
  // An empty form field is simply absent from a GET, and /save-page falls
  // back to DEFAULT_FORMATS rather than saving nothing. Saying "none" was a
  // lie about a save that was about to happen anyway.
  const fallback = boxes.filter(b => b.dataset.default !== undefined)
                        .map(b => NAMES[b.value]).join(', ');
  function update() {
    const picked = boxes.filter(b => b.checked).map(b => b.value);
    document.getElementById('fmt-summary').textContent =
      picked.length ? picked.map(v => NAMES[v]).join(', ')
                    : 'server default — ' + fallback;
    try { localStorage.setItem('margin-formats', picked.join(',')); }
    catch (e) { /* nothing to remember it with */ }
  }
  boxes.forEach(b => b.addEventListener('change', update));
  update();
})();
</script>
</body></html>"""


def _archive_dir() -> Path:
    return OUTPUT_DIR / _ARCHIVE_SUBDIR


def _storage_folder_safe(folder: Path) -> bool:
    """The configured root is trusted; its archive child may not escape it."""
    if folder == OUTPUT_DIR:
        return True
    return (folder == _archive_dir() and not folder.is_symlink()
            and _inside(folder, OUTPUT_DIR))


# Quick-save format checkboxes; pre-checked to match DEFAULT_FORMATS.
_FORMAT_LABELS = [
    ("pdf", "PDF", "the page exactly as rendered"),
    ("md", "Markdown", "article text, math as LaTeX (.md)"),
    ("tex", "LaTeX", "compilable article (.tex)"),
    ("org", "Org", "Emacs Org-mode (.org)"),
]


def _format_checkboxes() -> str:
    """Boxes for the quick-save form, pre-checked to match DEFAULT_FORMATS.

    data-default marks the same set for the script: clearing every box does
    not save nothing, it saves the server default, and the summary line has
    to be able to say which formats those are.
    """
    rows = []
    for val, name, desc in _FORMAT_LABELS:
        default = ' data-default checked' if val in DEFAULT_FORMATS else ''
        rows.append(
            f'<label><input type="checkbox" name="formats" value="{val}"{default}> '
            f'{name} <small>— {desc}</small></label>'
        )
    return "\n      ".join(rows)


def _url_by_stem() -> dict[str, str]:
    """Reverse of the saved-URL index: stem → source URL. Lets the queue show
    a source link for PDF-only items, which have no Markdown frontmatter."""
    out: dict[str, str] = {}
    for url, stems in _load_url_index().items():
        for s in stems:
            out.setdefault(s, url)
    return out


def _pretty_stem(stem: str) -> str:
    """Fallback display title from a filename stem: drop date, de-hyphenate."""
    return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem).replace("-", " ") or stem


_RE_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)^---[ \t]*\r?$",
                             re.DOTALL | re.MULTILINE)


def _read_frontmatter_field(md_path: Path, field: str) -> str | None:
    try:
        chunks: list[str] = []
        with md_path.open(encoding="utf-8", errors="replace") as handle:
            while sum(map(len, chunks)) < 64 * 1024:
                chunk = handle.read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                joined = "".join(chunks)
                if "\n---" in joined[3:]:
                    break
        head = "".join(chunks)
    except OSError:
        return None
    # Only the block between the delimiters. Reading stopped at the chunk
    # that contained the closing "---" and then searched the whole chunk, so
    # a "source_url:" line in the body was read as front matter — and the
    # body of a saved page is text from somebody else's website.
    block = _RE_FRONTMATTER.match(head)
    if block is None:
        return None
    m = re.search(rf'^{field}:\s*"?(.*?)"?\s*$', block.group(1), re.MULTILINE)
    if not m:
        return None
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\")


def _list_items(folder: Path) -> list[dict]:
    """Group saved files by stem → [{stem, title, date, source, files}]."""
    groups: dict[str, list[Path]] = {}
    if folder.is_dir() and _storage_folder_safe(folder):
        for f in folder.iterdir():
            if (f.is_file() and _inside(f, folder)
                    and not _has_lone_surrogate(f.name)
                    and f.suffix.lower() in _SERVE_EXTS):
                groups.setdefault(f.stem, []).append(f)

    url_by_stem = _url_by_stem()
    items = []
    for stem, files in groups.items():
        files.sort(key=lambda f: _SERVE_EXTS.index(f.suffix.lower()))
        md = next((f for f in files if f.suffix.lower() == ".md"), None)
        title = (_read_frontmatter_field(md, "title") if md else None) or _pretty_stem(stem)
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", stem)
        date_str = (m.group(1) if m
                    else date.fromtimestamp(files[0].stat().st_mtime).isoformat())
        # Prefer the exact URL from Markdown frontmatter; fall back to the
        # saved-URL index so PDF-only items get a source link too.
        source = ((_read_frontmatter_field(md, "source_url") if md else None)
                  or url_by_stem.get(stem))
        items.append({"stem": stem, "title": title, "date": date_str,
                      "source": source, "files": files})
    items.sort(key=lambda i: (i["date"], i["stem"]), reverse=True)
    return items


def _item_row(item: dict, view: str) -> str:
    """One saved item as a card. Links go through /read/, not the raw file:
    on the home screen there is no browser chrome to come back with."""
    file_links = " ".join(
        f'<a class="linkish" href="/read/{_url_quote(f.name, safe="")}">'
        f'{f.suffix[1:]}</a>'
        for f in item["files"]
    )
    # The source comes out of front matter in a folder people and sync
    # clients write to, so it is a URL only if it is one we would follow.
    source = ""
    if _safe_url(item["source"], relative_ok=False):
        source = (f'<a class="linkish" href="{_html_escape(item["source"], True)}"'
                  f' target="_blank" rel="noopener noreferrer">source ↗</a>')
    action = "restore" if view == "archive" else "archive"
    # Permanent deletion only from the archive view: inbox → archive → delete
    # is a deliberate two-step, and the confirm() guards against slips.
    delete_form = "" if view != "archive" else f"""
      <form method="post" action="/delete"
            data-confirm="Delete permanently? This cannot be undone.">
        <input type="hidden" name="stem" value="{_html_escape(item["stem"], True)}">
        <input type="hidden" name="view" value="{_html_escape(view, True)}">
        <button type="submit" class="linkish danger">delete</button>
      </form>"""
    return f"""<article class="item">
  <a class="title" href="/read/{_url_quote(item["files"][0].name, safe="")}">{_html_escape(_clean_text(item["title"]))}</a>
  <div class="meta">
    <time datetime="{_html_escape(item["date"], True)}">{_html_escape(item["date"])}</time>
    {file_links}
    {source}
    <form class="spacer" method="post" action="/archive">
      <input type="hidden" name="stem" value="{_html_escape(item["stem"], True)}">
      <input type="hidden" name="action" value="{action}">
      <button type="submit" class="linkish">{action}</button>
    </form>{delete_form}
  </div>
</article>"""


def _tabs(view: str, inbox_n: int, archive_n: int) -> str:
    """The two views, with the one you are in named rather than linked."""
    def entry(name, label, count):
        inside = (f'{label} <span class="count">{count}</span>')
        if view == name:
            return f'<span class="here">{inside}</span>'
        target = "/" if name == "inbox" else "/?view=archive"
        return f'<a href="{target}">{inside}</a>'

    return (entry("inbox", "Inbox", inbox_n)
            + entry("archive", "Archive", archive_n))


@app.get("/", response_class=HTMLResponse)
async def index(view: str = "inbox"):
    """Minimal reading-queue UI: saved items with file links and archive."""
    view = "archive" if view == "archive" else "inbox"
    folder = _archive_dir() if view == "archive" else OUTPUT_DIR
    items = _list_items(folder)
    rows = "\n".join(_item_row(i, view) for i in items) or (
        '<p class="empty">Nothing here yet.</p>'
        if view == "inbox" else '<p class="empty">Nothing archived yet.</p>'
    )
    inbox_n = len(_list_items(OUTPUT_DIR)) if view == "archive" else len(items)
    archive_n = len(items) if view == "archive" else len(_list_items(_archive_dir()))
    html = (_INDEX_HTML
            .replace("__HEAD__", _HEAD)
            .replace("__FORMAT_CHECKBOXES__", _format_checkboxes())
            .replace("__TABS__", _tabs(view, inbox_n, archive_n))
            .replace("__ROWS__", rows))
    return HTMLResponse(html)


# The schemes a link in Margin's own pages may carry. Front matter comes out
# of a folder that people and sync clients write to, so a "source" recorded
# there is a URL only if it is one we would be willing to follow — the same
# allowlist the reader's sanitizer uses.
_SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")


def _safe_url(url: str | None, relative_ok: bool = True) -> bool:
    if not url:
        return False
    # Browsers ignore control characters inside a scheme ("java\tscript:").
    match = _SCHEME_RE.match(re.sub(r"[\x00-\x20]", "", str(url)))
    if match is None:
        return relative_ok
    return match.group(1).lower() in _SAFE_SCHEMES


def _inside(path: Path, folder: Path) -> bool:
    """Whether path really lives under folder, symlinks resolved.

    The output directory is a synced folder that people and sync clients
    write to, so a name inside it can be a link to anything the account can
    read. Serving that would turn "read my saved page" into "read that".
    """
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (OSError, ValueError):
        return False


def _resolve_saved_file(name: str) -> Path:
    """Locate `name` in the output dir or archive/; 404 on miss/unsafe names."""
    if "/" in name or "\\" in name or name.startswith(".") or \
            Path(name).suffix.lower() not in _SERVE_EXTS:
        raise HTTPException(404, "No such file")
    for folder in (OUTPUT_DIR, _archive_dir()):
        path = folder / name
        if (_storage_folder_safe(folder) and path.is_file()
                and _inside(path, folder) and _inside(path, OUTPUT_DIR)):
            return path
    raise HTTPException(404, "No such file")


@app.get("/files/{name}")
async def get_file(name: str, download: bool = False):
    """Serve a saved file; ?download=1 forces a download (attachment)."""
    path = _resolve_saved_file(name)
    if download:
        return FileResponse(path, filename=name)
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Reader — /read/{name} wraps a saved file in a page with back-navigation and
# native share/copy/download. Needed most in the home-screen web app, where
# navigating to a raw file would strand the user (no browser chrome).
# ---------------------------------------------------------------------------

_ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "em", "strong", "b", "i", "u",
    "s", "del", "code", "pre", "ul", "ol", "li", "blockquote", "hr", "br",
    "a", "sup", "sub", "table", "thead", "tbody", "tr", "th", "td",
}


class _HTMLSanitizer(HTMLParser):
    """Allowlist filter for rendered Markdown: keeps structural tags, drops
    scripts/styles (including their content), event handlers, and every
    attribute except http(s)/# hrefs. Saved pages come from arbitrary
    websites, so their Markdown must not inject markup into the reader."""

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._skip: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip.append(tag)
            return
        if self._skip or tag not in _ALLOWED_TAGS:
            return
        if tag == "a":
            href = next((v for k, v in attrs if k == "href"), "") or ""
            if _safe_url(href):
                external = bool(_SCHEME_RE.match(href))
                attrs_out = ' rel="noopener noreferrer" target="_blank"' if external else ""
                self.out.append(
                    f'<a href="{_html_escape(href, quote=True)}"{attrs_out}>'
                )
                return
        self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self._skip and tag == self._skip[-1]:
            self._skip.pop()
        elif not self._skip and tag in _ALLOWED_TAGS and tag not in ("br", "hr"):
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(_html_escape(data))


def _sanitize_html(html: str) -> str:
    s = _HTMLSanitizer()
    s.feed(html)
    s.close()
    return "".join(s.out)


def _render_markdown(text: str) -> str:
    """Markdown → sanitized HTML. Math spans are stashed before conversion so
    Markdown can't mangle them (e.g. underscores → <em>), then restored as
    escaped text for MathJax to typeset client-side."""
    stash: list[str] = []
    marker = f"MARGINMATH{secrets.token_hex(12)}"
    while marker in text:
        marker = f"MARGINMATH{secrets.token_hex(12)}"

    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"{marker}{len(stash) - 1}END"

    text = _RE_MATH_SPAN.sub(_stash, text)
    html = _sanitize_html(_markdown.markdown(text, extensions=["extra"]))
    for i, m in enumerate(stash):
        html = html.replace(f"{marker}{i}END", _html_escape(m))
    return html


# MathJax from CDN — only loaded on .md reader pages; without internet the
# page still works, math just stays as $...$ source.
_MATHJAX_SNIPPET = """<script>
MathJax = { tex: { inlineMath: [['$','$']], displayMath: [['$$','$$']] } };
</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>"""

_READ_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>__TITLE__ — Margin</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
__HEAD__
</head>
<body class="reader">
<main>
  <header class="reader-bar">
    <a class="back" href="/">← Margin</a>
    <button class="linkish" id="copy" hidden>Copy text</button>
    <button class="linkish" id="share" hidden>Share</button>
    <a class="linkish" id="download" href="/files/__NAME__?download=1">Download</a>
    <span class="name">__TITLE__</span>
    <span class="note" id="note" hidden></span>
  </header>
  <div class="reading">__CONTENT__</div>
</main>
<script>
const NAME = __NAME_JSON__;
const FILE_URL = '/files/' + encodeURIComponent(NAME);
const IS_TEXT = __IS_TEXT__;
const note = document.getElementById('note');

// These pages have no flash area, and an empty catch is how Copy, Share and
// Download come to be indistinguishable from a button that does nothing.
function say(message) {
  note.textContent = message;
  note.hidden = false;
}
function action(el, run) {
  el.addEventListener('click', (event) => {
    event.preventDefault();
    Promise.resolve().then(run).catch(
      (e) => say('Could not ' + el.textContent.toLowerCase() + ': ' + e.message));
  });
}
async function fileBlob() {
  const answer = await fetch(FILE_URL);
  if (!answer.ok) throw new Error(answer.statusText || ('HTTP ' + answer.status));
  return await answer.blob();
}

const shareBtn = document.getElementById('share');
if (navigator.canShare) {
  shareBtn.hidden = false;
  action(shareBtn, async () => {
    const blob = await fileBlob();
    const file = new File([blob], NAME, { type: blob.type });
    try {
      if (navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: NAME });
      } else {
        await navigator.share({ title: NAME, url: location.href });
      }
    } catch (e) { /* the sheet was dismissed */ }
  });
}

// Download without navigating: a plain link would replace this page with the
// attachment URL — in the home-screen app, with no browser chrome, that
// strands the reader. Fetch → blob → synthetic <a download> keeps the page;
// the href stays as the no-script fallback.
action(document.getElementById('download'), async () => {
  const blob = await fileBlob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = NAME;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
});

// The Clipboard API is a secure-context feature and Margin is normally
// reached over plain HTTP on a home network, where navigator.clipboard does
// not exist at all — so Copy used to be hidden rather than offered. The
// selection route is deprecated but is what still works there.
async function copyText(body) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(body); return true; }
    catch (e) { /* denied on this origin */ }
  }
  const box = document.createElement('textarea');
  box.value = body;
  box.readOnly = true;
  box.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
  document.body.appendChild(box);
  box.select();
  box.setSelectionRange(0, body.length);
  let copied = false;
  try { copied = document.execCommand('copy'); } catch (e) { copied = false; }
  box.remove();
  return copied;
}

const copyBtn = document.getElementById('copy');
if (IS_TEXT) {
  copyBtn.hidden = false;
  action(copyBtn, async () => {
    const body = await (await fileBlob()).text();
    if (!await copyText(body)) {
      throw new Error('this browser would not let the page do it — ' +
                      'select the text and copy it yourself');
    }
    copyBtn.textContent = 'Copied';
    setTimeout(() => { copyBtn.textContent = 'Copy text'; }, 1500);
  });
}
</script>
__MATHJAX__
</body></html>"""


@app.get("/read/{name}", response_class=HTMLResponse)
async def read_file(name: str):
    """Reader page: back link, Share (native sheet via Web Share API), Copy,
    Download; Markdown rendered server-side with MathJax typesetting."""
    path = _resolve_saved_file(name)
    ext = path.suffix.lower()
    mathjax = ""

    if ext == ".pdf":
        file_url = "/files/" + _url_quote(name, safe="")
        content = (
            f'<iframe class="pdf" src="{file_url}"></iframe>'
            '<p class="note">If only the first page shows (an iOS iframe '
            'limitation), use Share or Download for the full document.</p>'
        )
        is_text = "false"
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
        body = re.sub(r"\A---\n.*?\n---\n", "", raw, flags=re.DOTALL)
        if ext == ".md" and _markdown is not None:
            content = _render_markdown(body)
            # Only where there is something to typeset. A megabyte of
            # JavaScript from a CDN is a strange thing to fetch for an
            # article about tooling, and most of a read-later queue is that.
            if _has_math_outside_code(body):
                mathjax = _MATHJAX_SNIPPET
        else:
            content = f"<pre>{_html_escape(body)}</pre>"
        is_text = "true"

    page = (_READ_HTML
            .replace("__HEAD__", _HEAD)
            .replace("__NAME_JSON__", json.dumps(name))  # before __NAME__!
            .replace("__NAME__", _url_quote(name, safe=""))
            .replace("__TITLE__", _html_escape(_clean_text(name)))
            .replace("__IS_TEXT__", is_text)
            .replace("__CONTENT__", content)
            .replace("__MATHJAX__", mathjax))
    return HTMLResponse(page)


def _rename_stem(old: str, new: str) -> None:
    """Follow a family that a collision moved to a different stem.

    Every URL carrying the old stem is rewritten, which is right exactly
    because a stem names one item: see _stem_taken. It was not always so, and
    a shared stem meant archiving one item silently repointed the other's URL
    at it.
    """
    idx = _load_url_index()
    changed = False
    for url, stems in idx.items():
        if old in stems:
            idx[url] = [new if s == old else s for s in stems]
            changed = True
    if changed:
        try:
            _write_json_atomically(_url_index_path(), idx)
        except OSError as e:
            _log("index", f"could not write URL index: {e}")


def _forget_stem(stem: str) -> None:
    """Drop a deleted item's stem from the duplicate-URL index.

    Only once no file carries it any more: the two folders name items
    independently, so deleting the archived copy of "same" can leave an
    unrelated inbox item of that name still standing.
    """
    if _files_for_stems({stem}):
        return
    idx = _load_url_index()
    changed = False
    for url in list(idx):
        if stem in idx[url]:
            idx[url] = [s for s in idx[url] if s != stem]
            changed = True
            if not idx[url]:
                del idx[url]
    if changed:
        try:
            _write_json_atomically(_url_index_path(), idx)
        except OSError as e:
            _log("index", f"could not write URL index: {e}")


@app.post("/delete")
async def delete_item(stem: str = Form(...), view: str = Form(...)):
    """Permanently delete one saved item, in one folder.

    One folder, because the inbox and the archive allocate names
    independently and can both hold a different item under the same stem —
    two captures of pages with the same title on the same day. Deleting "the
    stem wherever it lives" then took an unrelated inbox item with it, which
    is what the archive-only, confirm-prompted UI was meant to prevent.
    """
    if not _safe_stem(stem):
        raise HTTPException(400, detail="invalid stem")
    # Named, not defaulted, and required rather than optional: "anything that
    # is not 'archive' means the inbox" turned a typo into a different,
    # permanent operation, and an omitted field took the default silently.
    # Both forms in the UI post the field, so nothing here is guessing.
    if view not in ("inbox", "archive"):
        raise HTTPException(422, detail="view must be 'inbox' or 'archive'")
    folder = _archive_dir() if view == "archive" else OUTPUT_DIR
    if not _storage_folder_safe(folder):
        raise HTTPException(409, detail="storage path is not a safe directory")
    removed = 0
    if folder.is_dir():
        for f in list(folder.iterdir()):
            if (f.is_file() and _inside(f, folder) and f.stem == stem
                    and f.suffix.lower() in _SERVE_EXTS):
                f.unlink()
                removed += 1
    if not removed:
        raise HTTPException(404, detail=f"no files found for {stem!r}")
    _forget_stem(stem)
    _log("delete", f"deleted {stem} ({removed} files)")
    return RedirectResponse(url="/?view=archive", status_code=303)


@app.post("/archive")
async def archive(stem: str = Form(...), action: str = Form(...)):
    """Move all files of one saved item between the inbox and archive/."""
    if not _safe_stem(stem):
        raise HTTPException(400, detail="invalid stem")
    if action not in ("archive", "restore"):
        raise HTTPException(422, detail="action must be 'archive' or 'restore'")
    if action == "restore":
        src, dst, back = _archive_dir(), OUTPUT_DIR, "/?view=archive"
    else:
        src, dst, back = OUTPUT_DIR, _archive_dir(), "/"
    if not _storage_folder_safe(src) or not _storage_folder_safe(dst):
        raise HTTPException(409, detail="archive path is not a safe directory")
    moved = 0
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        files = [f for f in src.iterdir()
                 if (f.is_file() and _inside(f, src) and f.stem == stem
                     and f.suffix.lower() in _SERVE_EXTS)]
        if files:
            formats = tuple(f.suffix.lstrip(".").lower() for f in files)
            # The family being moved does not count as the occupant of its
            # own name: stems are unique across both folders, so a move keeps
            # its name unless something arrived in the destination out of
            # band — the output directory is synced and written to by other
            # software.
            target = _family_path(dst / f"{stem}.md", formats,
                                  besides=frozenset(files))
            # One item is several files, and a rename can fail part way — a
            # full disk, a permission, the sync client holding one open. A
            # bare loop leaves the item split across both folders, listed in
            # neither view completely. Put back what moved, and if even that
            # fails, say exactly where the pieces are.
            done: list[tuple[Path, Path]] = []
            try:
                for f in files:
                    landed = dst / f"{target.stem}{f.suffix.lower()}"
                    f.rename(landed)
                    done.append((f, landed))
            except OSError as e:
                stranded = []
                for was, now in reversed(done):
                    try:
                        now.rename(was)
                    except OSError:
                        stranded.append(now.name)
                _log("archive", f"{action} {stem} failed after "
                                f"{len(done)} of {len(files)}: {e}")
                detail = f"Could not {action} {stem}: {e}"
                if stranded:
                    detail += ("; these are now in the other folder and were "
                               "not put back: " + ", ".join(sorted(stranded)))
                raise HTTPException(500, detail=detail) from e
            moved = len(done)
            # The destination may already hold that name, in which case the
            # family lands on "<stem>-2" — and the URL index still pointed at
            # "<stem>", which by then is a different item's files. A PDF-only
            # capture has no front matter to fall back on, so the wrong
            # document would answer for the URL for good.
            if target.stem != stem:
                _rename_stem(stem, target.stem)
    if not moved:
        raise HTTPException(404, detail=f"no files found for {stem!r}")
    _log("archive", f"{action} {stem} ({moved} files)")
    return RedirectResponse(url=back, status_code=303)


@app.get("/health")
async def health(request: Request):
    # is_dir, not exists: pointed at a regular file, health reported ok,
    # exists and writable while every save failed.
    exists = OUTPUT_DIR.is_dir()
    # The path itself is not reported: /health is public (it has to be, for
    # a probe that carries no credentials), and on a real install the path
    # names the account it runs as. Whether it works is the useful part.
    return {
        "status": "ok",
        "output_dir_exists": exists,
        "output_dir_writable": exists and os.access(OUTPUT_DIR, os.W_OK),
        "saved_md_count": len(list(OUTPUT_DIR.glob("*.md"))) if exists else 0,
        "saved_pdf_count": len(list(OUTPUT_DIR.glob("*.pdf"))) if exists else 0,
        "pandoc_available": shutil.which("pandoc") is not None,
        # Two facts, because they come apart: pip installs the package and
        # `playwright install chromium` installs the browser. With only the
        # first, this reported a working renderer while every PDF save
        # failed on launch.
        "playwright_available": request.app.state.renderer.available,
        "chromium_installed":
            await request.app.state.renderer.chromium_installed(),
        "mathpix_configured": bool(MATHPIX_APP_ID and MATHPIX_APP_KEY),
        "auth_required": bool(MARGIN_TOKEN),
    }


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Margin — read-later server")
    parser.add_argument(
        "--output-dir",
        help="Directory for saved files (overrides the OUTPUT_DIR env var)",
    )
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    if args.output_dir:
        OUTPUT_DIR = Path(args.output_dir).expanduser()
    print(f"[startup] output dir: {OUTPUT_DIR}", file=sys.stderr, flush=True)
    uvicorn.run(app, host=args.host, port=args.port)
