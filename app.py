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
import io
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import unicodedata
import secrets
import tempfile
from contextlib import asynccontextmanager
from datetime import date
from html import escape as _html_escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup, NavigableString
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
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


def _default_output_dir() -> Path:
    if sys.platform == "darwin":  # original iCloud-inbox workflow
        return (
            Path.home()
            / "Library/Mobile Documents/com~apple~CloudDocs/ReadLater/inbox"
        )
    return Path.home() / "ReadLater" / "inbox"


# Where saved files land. Overridden by the --output-dir CLI flag (see bottom).
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR") or _default_output_dir()).expanduser()


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.client = httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers={"User-Agent": CHROME_UA}
    )
    application.state.renderer = Renderer()
    yield
    await application.state.renderer.close()
    await application.state.client.aclose()


app = FastAPI(title="Margin", version="2.1.0", lifespan=lifespan)

# Allow cross-origin calls (browser extensions, fetch-based clients). Origins
# are not restricted — the server is either on a private network or protected
# by MARGIN_TOKEN; the auth cookie is SameSite=Strict and never sent
# cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


def _request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query_params.get("token")
            or request.cookies.get(_TOKEN_COOKIE, ""))


@app.middleware("http")
async def _require_token(request: Request, call_next):
    if (
        not MARGIN_TOKEN
        or request.url.path in _PUBLIC_PATHS
        or request.url.path.startswith("/static/")
        or request.method == "OPTIONS"  # CORS preflight carries no credentials
    ):
        return await call_next(request)

    if not secrets.compare_digest(_request_token(request), MARGIN_TOKEN):
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(_UNAUTHORIZED_HTML, status_code=401)
        return JSONResponse(
            {"status": "error", "filename": "",
             "message": "Unauthorized: missing or wrong token "
                        "(Authorization: Bearer header or ?token= parameter)",
             "summary": "Error: missing or wrong API token"},
            status_code=401,
        )

    response = await call_next(request)
    if request.query_params.get("token"):
        # Remember a query-supplied token so plain browsing works afterwards.
        # SameSite=Strict: never sent on cross-site requests, so third-party
        # pages cannot ride this cookie (no CSRF).
        response.set_cookie(
            _TOKEN_COOKIE, MARGIN_TOKEN, max_age=365 * 24 * 3600,
            httponly=True, samesite="strict",
        )
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


def _clean_title(title: str) -> str:
    title = title.strip()
    cleaned = _RE_TITLE_SUFFIX.sub("", title)
    return cleaned if cleaned else title


def _slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
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
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _frontmatter(title: str, source_url: str | None = None,
                 has_math: bool = True) -> str:
    lines = ["---", f"title: {_yaml_quote(title)}"]
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
    tex_path.write_text(tex, encoding="utf-8")


# Text formats the Markdown pipeline can emit. "tex" and "org" are derived
# from the Markdown via Pandoc and are selectable independently of "md".
MD_FORMATS = ("md", "tex", "org")


def _write_all_formats(filename: str, md_content: str, title: str,
                       formats: tuple[str, ...] = MD_FORMATS) -> list[Path]:
    """Write the requested subset of .md/.tex/.org (collision-safe stem).

    Returns the files actually written — Pandoc-derived ones are skipped
    silently when Pandoc is unavailable, exactly as before.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = _unique_path(OUTPUT_DIR / filename)
    written: list[Path] = []

    if "md" in formats:
        md_path.write_text(md_content, encoding="utf-8")
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
            _run_pandoc(
                [*_PANDOC_FROM, str(pandoc_src), "-o", str(org_path)],
                f"org {org_path.name}",
            )
            if org_path.exists():
                written.append(org_path)
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


def _replace_math_elements(soup: BeautifulSoup) -> None:
    """Mutate soup in-place: replace every math element with $...$ / $$...$$ text.

    Strategies in priority order:
      1.  Wikipedia / MediaWiki  — <span class="mwe-math-element[-inline|-block]">
      1b. MathJax 3 rendered output — <mjx-container> (recover from assistive MathML)
      2.  General MathML w/ LaTeX annotation — <math> with <annotation encoding="application/x-tex">
      2b. Raw MathML            — <math> with no annotation (converted structurally)
      3.  KaTeX                  — <span class="katex"> with annotation inside
      4.  MathJax 2              — <script type="math/tex[; mode=display]">
      5.  Image math             — <img alt="LATEX"> (SVG/PNG rendered formulas)
    """
    # 1. Wikipedia mwe-math-element spans
    for span in soup.find_all("span", class_="mwe-math-element"):
        ann = span.find("annotation", attrs={"encoding": "application/x-tex"})
        if not ann:
            continue
        classes = " ".join(span.get("class", []))
        display = "block" in classes and "inline" not in classes
        span.replace_with(_math_replacement(ann.get_text(), display))

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
            container.replace_with(_math_replacement(latex, display))
        else:
            container.decompose()

    # 2. Bare <math> elements — prefer embedded LaTeX annotation, then alttext,
    #    then structural MathML→LaTeX conversion as last resort
    for math in soup.find_all("math"):
        display = math.get("display") == "block"
        ann = math.find("annotation", attrs={"encoding": "application/x-tex"})
        if ann:
            math.replace_with(_math_replacement(ann.get_text(), display))
        elif math.get("alttext"):
            math.replace_with(_math_replacement(math["alttext"], display))
        else:
            latex = _mathml_to_latex(math)
            if latex.strip():
                math.replace_with(_math_replacement(latex, display))

    # 3. KaTeX rendered spans  (.katex wraps both inline and display)
    for katex in soup.find_all(class_="katex"):
        ann = katex.find("annotation", attrs={"encoding": "application/x-tex"})
        if ann:
            display = bool(katex.find_parent(class_="katex-display"))
            katex.replace_with(_math_replacement(ann.get_text(), display))

    # 4. MathJax 2 script tags — type is "math/tex", optionally with a
    # "; mode=display" suffix (spacing around ";" varies between sites).
    for tag in soup.find_all("script", attrs={"type": re.compile(r"^\s*math/tex")}):
        display = "display" in tag.get("type", "")
        tag.replace_with(_math_replacement(tag.string or "", display))

    # 5. Image-rendered math: <img alt="LATEX ..."> (e.g. SVG formula images).
    for img in soup.find_all("img"):
        alt = img.get("alt", "")
        if not _RE_LATEX_ALT.search(alt):
            continue
        parent = img.parent
        sole = len([s for s in parent.children if str(s).strip()]) == 1
        display = sole and parent.name in ("p", "div", "figure", "td", "li")
        img.replace_with(_math_replacement(alt, display))

    # Remove any leftover rendered MathJax output nodes (duplicates)
    for node in soup.find_all(True, class_=re.compile(r"MathJax|mjx-", re.I)):
        node.decompose()


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

    _replace_math_elements(soup)
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
    """Upload PDF, poll until done, return MMD text."""
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            "https://api.mathpix.com/v3/pdf",
            headers=_mathpix_headers(),
            files={"file": ("document.pdf", pdf_bytes, "application/pdf")},
            data={"options_json": _MATHPIX_PDF_OPTIONS},
        )
        resp.raise_for_status()
        pdf_id = resp.json().get("pdf_id")
        if not pdf_id:
            raise HTTPException(502, detail=f"Mathpix upload failed: {resp.text}")

        for _ in range(60):  # ~3 min @ 3s polls
            await asyncio.sleep(3)
            check = await client.get(
                f"https://api.mathpix.com/v3/pdf/{pdf_id}",
                headers=_mathpix_headers(),
            )
            check.raise_for_status()
            status = check.json().get("status", "")
            if status == "completed":
                break
            if status == "error":
                raise HTTPException(502, detail=f"Mathpix error: {check.text}")
        else:
            raise HTTPException(504, detail="Mathpix timed out (>3 min)")

        mmd = await client.get(
            f"https://api.mathpix.com/v3/pdf/{pdf_id}.mmd",
            headers=_mathpix_headers(),
        )
        mmd.raise_for_status()
        return mmd.text


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


def _validated_url(v: str) -> str:
    url = _clean_shortcut_url(v)
    scheme = urlparse(url).scheme.lower()
    # Only web URLs — keeps the fetcher/headless browser away from file:// etc.
    if scheme not in ("http", "https"):
        raise ValueError(f"Only http(s) URLs are supported, got: {url[:100]}")
    return url


class URLPayload(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def strip_url(cls, v: str) -> str:
        return _validated_url(v)


class SavePayload(BaseModel):
    url: str
    formats: list[str] = ["pdf"]
    force: bool = False  # save again even if this URL was saved before

    @field_validator("url")
    @classmethod
    def strip_url(cls, v: str) -> str:
        return _validated_url(v)

    @field_validator("formats")
    @classmethod
    def check_formats(cls, v: list[str]) -> list[str]:
        v = [f.lower().lstrip(".") for f in v] or ["pdf"]
        unknown = set(v) - {"pdf", *MD_FORMATS}
        if unknown:
            raise ValueError(
                f"Unknown formats {sorted(unknown)}; "
                "use any of 'pdf', 'md', 'tex', 'org'"
            )
        return v


@app.post("/echo")
async def echo(request: Request):
    """Debug endpoint — returns whatever the client sent, as JSON."""
    body_bytes = await request.body()
    try:
        body = await request.json()
    except Exception:
        body = body_bytes.decode(errors="replace")
    return {"method": request.method, "headers": dict(request.headers), "body": body}


def _ok(md_path: Path, title: str) -> dict:
    return {
        "status": "ok",
        "filename": md_path.name,
        "title": title,
        "path": str(md_path),
        "summary": f"Saved: {md_path.name}",
    }


def _log(label: str, msg: str) -> None:
    print(f"[{label}] {msg}", file=sys.stderr, flush=True)


# Bodies shorter than this suggest a client-side-rendered page (the article is
# assembled in the browser) or a bot wall — worth retrying in headless Chromium.
_THIN_BODY_CHARS = 200


# Cap for PDFs downloaded from a URL (uploads have their own MAX_PDF_BYTES).
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


async def _fetch_pdf_bytes(client: httpx.AsyncClient, url: str) -> bytes | None:
    """Return the raw bytes if `url` serves a PDF directly, else None."""
    ctype = ""
    try:
        head = await client.head(url)
        ctype = head.headers.get("content-type", "").lower()
    except httpx.HTTPError:
        pass  # many servers reject HEAD — fall back to the extension check
    if "application/pdf" not in ctype and not urlparse(url).path.lower().endswith(".pdf"):
        return None
    try:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            if "application/pdf" not in resp.headers.get("content-type", "").lower():
                return None
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"PDF at {url} exceeds {MAX_DOWNLOAD_BYTES // 2**20} MB"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError:
        return None


def _write_binary(filename: str, data: bytes) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _unique_path(OUTPUT_DIR / filename)
    path.write_bytes(data)
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


def _load_url_index() -> dict[str, list[str]]:
    try:
        idx = json.loads(_url_index_path().read_text(encoding="utf-8"))
        return idx if isinstance(idx, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_url(url: str, stem: str) -> None:
    idx = _load_url_index()
    stems = idx.setdefault(_norm_url(url), [])
    if stem not in stems:
        stems.append(stem)
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _url_index_path().write_text(json.dumps(idx, indent=1), encoding="utf-8")
    except OSError as e:
        _log("index", f"could not write URL index: {e}")


def _files_for_stems(stems: set[str]) -> list[Path]:
    files = []
    for folder in (OUTPUT_DIR, _archive_dir()):
        if folder.is_dir():
            files += [f for f in folder.iterdir()
                      if f.is_file() and f.stem in stems
                      and f.suffix.lower() in _SERVE_EXTS]
    return files


def _find_existing(url: str) -> dict | None:
    """Return {'files', 'title'} for a still-present earlier save of `url`."""
    norm = _norm_url(url)
    files = _files_for_stems(set(_load_url_index().get(norm, [])))
    if not files:
        # Saves that predate the index: match source_url in Markdown frontmatter
        for folder in (OUTPUT_DIR, _archive_dir()):
            if not folder.is_dir():
                continue
            for md in folder.glob("*.md"):
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
    return {"files": [f.name for f in files], "title": title}


def _duplicate_response(existing: dict) -> dict:
    return {
        "status": "ok",
        "duplicate": True,
        "title": existing["title"],
        "files": existing["files"],
        "filename": existing["files"][0],
        "path": str(OUTPUT_DIR),
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


async def _run_markdown_save(
    url: str, request: Request, formats: tuple[str, ...] = MD_FORMATS
) -> dict:
    """Markdown pipeline: fetch, extract content + math, write the requested
    subset of .md/.tex/.org."""
    _log("save-url", f"fetching {url}")
    client: httpx.AsyncClient = request.app.state.client
    renderer: Renderer = request.app.state.renderer

    html, fetch_err, retry_render = None, "", True
    try:
        page = await client.get(url)
        page.raise_for_status()
        html = page.text
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        fetch_err = f"Site returned {code}: {url}"
        # Only bot-wall-ish statuses are worth retrying in a real browser —
        # a 404/410 is a genuine miss and would just save the error page.
        retry_render = code in (401, 403, 406, 429, 503)
    except httpx.RequestError as e:
        fetch_err = f"Could not reach {url}: {e}"

    title, body = "", ""
    if html is not None:
        try:
            title, body = _extract_url_content(html, url)
        except Exception as e:
            _log("save-url", f"extract failed: {e}\n{traceback.format_exc()}")
            return _err(f"Extraction failed: {e}")

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

    if not body.strip():
        return _err(
            fetch_err
            or f"No article content could be extracted from {url} — "
               'try POST /save with {"formats": ["pdf"]} instead'
        )
    title = title or url

    md = _frontmatter(title, url, has_math="$" in body) + "\n" + body
    try:
        written = _write_all_formats(_filename(title), md, title, formats)
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
        "path": str(written[0]),
        "summary": f"Saved: {written[0].name}",
    }


@app.post("/save-url")
async def save_url(payload: URLPayload, request: Request):
    """Fetch a web page, extract content + math, save Markdown to the output dir."""
    existing = _find_existing(payload.url)
    if existing:
        _log("save-url", f"duplicate of {existing['files'][0]}: {payload.url}")
        return _duplicate_response(existing)
    return await _run_markdown_save(payload.url, request)


@app.post("/save")
async def save(payload: SavePayload, request: Request):
    """Save any web page. Format "pdf" renders it in headless Chromium (waits
    for JS/MathJax/KaTeX to finish) and exports a PDF; "md" runs the
    Markdown-extraction pipeline. Default is ["pdf"]."""
    _log("save", f"{payload.formats} {payload.url}")
    client: httpx.AsyncClient = request.app.state.client
    renderer: Renderer = request.app.state.renderer

    if not payload.force:
        existing = _find_existing(payload.url)
        if existing:
            _log("save", f"duplicate of {existing['files'][0]}: {payload.url}")
            return _duplicate_response(existing)

    saved: list[str] = []
    errors: list[str] = []
    title = ""

    if "pdf" in payload.formats:
        try:
            direct = await _fetch_pdf_bytes(client, payload.url)
        except Exception as e:
            _log("save", f"direct-pdf probe failed: {e}")
            direct = None
        if direct is not None:
            # The URL already serves a PDF — store it as-is.
            title = _clean_title(await _direct_pdf_title(client, payload.url, direct))
            pdf_path = _write_binary(_filename(title, "pdf"), direct)
            saved.append(pdf_path.name)
            _record_url(payload.url, pdf_path.stem)
            _log("save", f"saved direct PDF → {pdf_path.name}")
        else:
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
                pdf_path = _write_binary(_filename(title, "pdf"), rendered.pdf)
                saved.append(pdf_path.name)
                _record_url(payload.url, pdf_path.stem)
                _log("save", f"saved → {pdf_path.name}")
            except RendererUnavailable as e:
                errors.append(str(e))
            except Exception as e:
                _log("save", f"render failed: {e}\n{traceback.format_exc()}")
                errors.append(f"Could not render {payload.url}: {e}")

    md_formats = tuple(f for f in payload.formats if f in MD_FORMATS)
    if md_formats:
        # Skip the endpoint's duplicate check — this request already passed it
        # (and the PDF just written above would otherwise count as a duplicate).
        md_result = await _run_markdown_save(payload.url, request, md_formats)
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
        "path": str(OUTPUT_DIR),
        "summary": f"Saved: {', '.join(saved)}",
        **({"warnings": errors} if errors else {}),
    }


@app.post("/save-pdf")
async def save_pdf(file: UploadFile = File(...)):
    """Convert PDF via Mathpix, save the resulting Markdown to the output dir."""
    _log("save-pdf", f"received {file.filename}")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return _err("Expected a .pdf file")
    if not MATHPIX_APP_ID or not MATHPIX_APP_KEY:
        return _err("MATHPIX_APP_ID / MATHPIX_APP_KEY not set in .env")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_PDF_BYTES:
        return _err(f"PDF too large: {len(pdf_bytes)} bytes (max {MAX_PDF_BYTES})")

    try:
        mmd = await _mathpix_pdf(pdf_bytes)
    except HTTPException as e:
        _log("save-pdf", f"mathpix error: {e.detail}")
        return _err(str(e.detail))
    except Exception as e:
        _log("save-pdf", f"mathpix crash: {e}\n{traceback.format_exc()}")
        return _err(f"Mathpix call failed: {e}")

    m = re.search(r"^#\s+(.+)$", mmd, re.MULTILINE)
    title = _clean_title(m.group(1).strip() if m else Path(file.filename).stem)

    md = _frontmatter(title, has_math="$" in mmd) + "\n" + mmd
    try:
        written = _write_all_formats(_filename(title), md, title)
    except Exception as e:
        _log("save-pdf", f"write failed: {e}\n{traceback.format_exc()}")
        return _err(f"Could not write file: {e}")
    _log("save-pdf", f"saved → {written[0].name}")
    return _ok(written[0], title)


# ---------------------------------------------------------------------------
# Icon / manifest — served from static/ (public: favicon requests and
# home-screen installs don't carry credentials)
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# SVG first for browsers that support it; 32px PNG fallback for Safari,
# which ignores SVG favicons.
_HEAD_ICONS = """<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon-32.png" type="image/png" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.json">"""


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


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    return FileResponse(_STATIC_DIR / "manifest.json",
                        media_type="application/manifest+json")


# Result page for the /save-page bookmarklet flow. Doubled braces are literal
# CSS braces (str.format).
_SAVE_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Margin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
__HEAD_ICONS__
<style>
  body {{ font: 16px/1.5 -apple-system, system-ui, sans-serif;
          max-width: 34rem; margin: 4rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; }}
  .ok {{ color: #2e7d32; }} .err {{ color: #c62828; }}
</style></head>
<body>
<h1 class="{cls}">{heading}</h1>
<p>{detail}</p>
{autoclose}
<p><a href="/">← Margin inbox</a></p>
</body></html>"""


def _save_page_response(ok: bool, heading: str, detail: str) -> HTMLResponse:
    autoclose = (
        "<p>This tab will close by itself.</p>"
        "<script>setTimeout(function () { window.close(); }, 2500)</script>"
        if ok else ""
    )
    return HTMLResponse(_SAVE_PAGE_HTML.replace("__HEAD_ICONS__", _HEAD_ICONS).format(
        cls="ok" if ok else "err",
        heading=_html_escape(heading),
        detail=_html_escape(detail),
        autoclose=autoclose,
    ))


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
        payload = SavePayload(url=url, formats=fmt_list or ["pdf"], force=force)
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
_RE_SAFE_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")

# Placeholders (__ROWS__ etc.) are substituted with str.replace, so the CSS
# and JS braces below need no escaping.
_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Margin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
__HEAD_ICONS__
<style>
  :root { color-scheme: light dark;
          --muted: #8a8a8e; --line: #88888833; --btn: #88888818; }
  body { font: 16px/1.55 -apple-system, system-ui, sans-serif;
         max-width: 46rem; margin: 3rem auto; padding: 0 1rem; }
  h1 { font-size: 1.4rem; }
  h1 a { color: inherit; text-decoration: none; }
  form.saver { margin: 1rem 0 .8rem; }
  form.saver .row { display: flex; gap: .6rem; align-items: center; }
  form.saver input[type=url], #filter {
    flex: 1; min-width: 14rem; font: inherit; padding: .45rem .6rem;
    border: 1px solid var(--muted); border-radius: 6px;
    background: transparent; color: inherit; }
  details.formats { margin-top: .5rem; font-size: .85rem; }
  details.formats summary { cursor: pointer; color: var(--muted); }
  details.formats summary b { color: inherit; font-weight: 500; }
  .fmt-list { display: flex; flex-direction: column; gap: .35rem;
              margin: .6rem 0 .2rem .2rem; }
  .fmt-list label { cursor: pointer; }
  .fmt-list small { color: var(--muted); }
  #filter { margin-bottom: 1rem; display: block; width: 100%;
            box-sizing: border-box; }
  .tabs { margin-bottom: .8rem; font-size: .95rem; }
  .tabs a { margin-right: 1.2rem; }
  .item { padding: .7rem 0; border-bottom: 1px solid var(--line);
          display: flex; gap: 1rem; align-items: baseline; }
  .date { color: var(--muted); font-size: .82rem; white-space: nowrap; }
  .main { flex: 1; }
  .main a.title { font-weight: 600; text-decoration: none; }
  .links { font-size: .82rem; margin-top: .15rem; }
  .links a { margin-right: .7rem; }
  button { font: inherit; font-size: .82rem; padding: .25rem .7rem;
           border: 1px solid var(--muted); border-radius: 6px;
           background: var(--btn); color: inherit; cursor: pointer; }
  button.danger { color: #c43d33; border-color: #c43d3366; }
  .empty { color: var(--muted); margin-top: 2rem; }
</style></head>
<body>
<h1><a href="/">Margin</a></h1>
<form class="saver" method="get" action="/save-page">
  <div class="row">
    <input type="url" name="url" placeholder="https://…  save a page" required>
    <button type="submit">Save</button>
  </div>
  <details class="formats">
    <summary>Formats: <b id="fmt-summary"></b></summary>
    <div class="fmt-list">
      <label><input type="checkbox" name="formats" value="pdf" checked>
        PDF <small>— the page exactly as rendered</small></label>
      <label><input type="checkbox" name="formats" value="md" checked>
        Markdown <small>— article text, math as LaTeX (.md)</small></label>
      <label><input type="checkbox" name="formats" value="tex">
        LaTeX <small>— compilable article (.tex)</small></label>
      <label><input type="checkbox" name="formats" value="org">
        Org <small>— Emacs Org-mode (.org)</small></label>
    </div>
  </details>
</form>
<div class="tabs">
  <a href="/">Inbox (__INBOX_COUNT__)</a>
  <a href="/?view=archive">Archive (__ARCHIVE_COUNT__)</a>
</div>
<input id="filter" type="search" placeholder="Filter by title…">
__ROWS__
<script>
document.getElementById('filter').addEventListener('input', function () {
  const q = this.value.toLowerCase();
  document.querySelectorAll('.item').forEach(function (el) {
    el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
});

// Format checkboxes: restore last choice, keep the summary line current.
(function () {
  const NAMES = { pdf: 'PDF', md: 'Markdown', tex: 'LaTeX', org: 'Org' };
  const boxes = Array.from(
    document.querySelectorAll('.fmt-list input[type=checkbox]'));
  const saved = localStorage.getItem('margin-formats');
  if (saved !== null) {
    const picked = saved.split(',');
    boxes.forEach(b => { b.checked = picked.includes(b.value); });
  }
  function update() {
    const picked = boxes.filter(b => b.checked).map(b => b.value);
    document.getElementById('fmt-summary').textContent =
      picked.length ? picked.map(v => NAMES[v]).join(', ') : 'PDF (default)';
    localStorage.setItem('margin-formats', picked.join(','));
  }
  boxes.forEach(b => b.addEventListener('change', update));
  update();
})();
</script>
</body></html>"""


def _archive_dir() -> Path:
    return OUTPUT_DIR / _ARCHIVE_SUBDIR


def _pretty_stem(stem: str) -> str:
    """Fallback display title from a filename stem: drop date, de-hyphenate."""
    return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem).replace("-", " ") or stem


def _read_frontmatter_field(md_path: Path, field: str) -> str | None:
    try:
        head = md_path.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        return None
    m = re.search(rf'^{field}:\s*"?(.*?)"?\s*$', head, re.MULTILINE)
    if not m:
        return None
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\")


def _list_items(folder: Path) -> list[dict]:
    """Group saved files by stem → [{stem, title, date, source, files}]."""
    groups: dict[str, list[Path]] = {}
    if folder.is_dir():
        for f in folder.iterdir():
            if f.is_file() and f.suffix.lower() in _SERVE_EXTS:
                groups.setdefault(f.stem, []).append(f)

    items = []
    for stem, files in groups.items():
        files.sort(key=lambda f: _SERVE_EXTS.index(f.suffix.lower()))
        md = next((f for f in files if f.suffix.lower() == ".md"), None)
        title = (_read_frontmatter_field(md, "title") if md else None) or _pretty_stem(stem)
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", stem)
        date_str = (m.group(1) if m
                    else date.fromtimestamp(files[0].stat().st_mtime).isoformat())
        source = _read_frontmatter_field(md, "source_url") if md else None
        items.append({"stem": stem, "title": title, "date": date_str,
                      "source": source, "files": files})
    items.sort(key=lambda i: (i["date"], i["stem"]), reverse=True)
    return items


def _item_row(item: dict, view: str) -> str:
    # Links go through /read/ (reader page with back-nav and share), not the
    # raw file — essential in the home-screen web app, which has no browser
    # chrome to navigate back with.
    file_links = "".join(
        f'<a href="/read/{_html_escape(f.name)}">{f.suffix[1:]}</a> '
        for f in item["files"]
    )
    source = (
        f'<a href="{_html_escape(item["source"])}">source</a>'
        if item["source"] else ""
    )
    action = "restore" if view == "archive" else "archive"
    # Permanent deletion only from the archive view: inbox → archive → delete
    # is a deliberate two-step, and the confirm() guards against slips.
    delete_form = "" if view != "archive" else f"""
  <form method="post" action="/delete"
        onsubmit="return confirm('Delete permanently? This cannot be undone.')">
    <input type="hidden" name="stem" value="{_html_escape(item["stem"])}">
    <button type="submit" class="danger">Delete</button>
  </form>"""
    return f"""<div class="item">
  <span class="date">{item["date"]}</span>
  <div class="main">
    <a class="title" href="/read/{_html_escape(item["files"][0].name)}">{_html_escape(item["title"])}</a>
    <div class="links">{file_links}{source}</div>
  </div>
  <form method="post" action="/archive">
    <input type="hidden" name="stem" value="{_html_escape(item["stem"])}">
    <input type="hidden" name="action" value="{action}">
    <button type="submit">{action.capitalize()}</button>
  </form>{delete_form}
</div>"""


@app.get("/", response_class=HTMLResponse)
async def index(view: str = "inbox"):
    """Minimal reading-queue UI: saved items with file links and archive."""
    view = "archive" if view == "archive" else "inbox"
    folder = _archive_dir() if view == "archive" else OUTPUT_DIR
    items = _list_items(folder)
    rows = "\n".join(_item_row(i, view) for i in items) or (
        '<p class="empty">Nothing here yet.</p>'
    )
    inbox_n = len(_list_items(OUTPUT_DIR)) if view == "archive" else len(items)
    archive_n = len(items) if view == "archive" else len(_list_items(_archive_dir()))
    html = (_INDEX_HTML
            .replace("__HEAD_ICONS__", _HEAD_ICONS)
            .replace("__INBOX_COUNT__", str(inbox_n))
            .replace("__ARCHIVE_COUNT__", str(archive_n))
            .replace("__ROWS__", rows))
    return HTMLResponse(html)


def _resolve_saved_file(name: str) -> Path:
    """Locate `name` in the output dir or archive/; 404 on miss/unsafe names."""
    if "/" in name or "\\" in name or name.startswith(".") or \
            Path(name).suffix.lower() not in _SERVE_EXTS:
        raise HTTPException(404)
    path = OUTPUT_DIR / name
    if not path.is_file():
        path = _archive_dir() / name
    if not path.is_file():
        raise HTTPException(404)
    return path


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
        self._skip: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = tag
            return
        if self._skip or tag not in _ALLOWED_TAGS:
            return
        if tag == "a":
            href = next((v for k, v in attrs if k == "href"), "") or ""
            if href.startswith(("http://", "https://", "#")):
                self.out.append(f'<a href="{_html_escape(href)}" rel="noopener">')
                return
        self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == self._skip:
            self._skip = None
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

    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"§MATH{len(stash) - 1}§"

    text = _RE_MATH_SPAN.sub(_stash, text)
    html = _sanitize_html(_markdown.markdown(text, extensions=["extra"]))
    for i, m in enumerate(stash):
        html = html.replace(f"§MATH{i}§", _html_escape(m))
    return html


# MathJax from CDN — only loaded on .md reader pages; without internet the
# page still works, math just stays as $...$ source.
_MATHJAX_SNIPPET = """<script>
MathJax = { tex: { inlineMath: [['$','$']], displayMath: [['$$','$$']] } };
</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>"""

_READ_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__ — Margin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
__HEAD_ICONS__
<style>
  :root { color-scheme: light dark; --muted: #8a8a8e; --line: #88888833; }
  body { font: 17px/1.6 -apple-system, system-ui, sans-serif;
         max-width: 44rem; margin: 0 auto; padding: 0 1rem 3rem; }
  header { display: flex; gap: .7rem; align-items: center;
           padding: .7rem 0; border-bottom: 1px solid var(--line);
           position: sticky; top: 0; background: Canvas; }
  header a.back { text-decoration: none; white-space: nowrap; }
  header .name { flex: 1; font-size: .82rem; color: var(--muted);
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  header button, header a.btn {
    font: inherit; font-size: .82rem; padding: .3rem .7rem;
    border: 1px solid var(--muted); border-radius: 6px;
    background: transparent; color: inherit; cursor: pointer;
    text-decoration: none; white-space: nowrap; }
  main { margin-top: 1.2rem; }
  main pre { white-space: pre-wrap; word-break: break-word; font-size: .85em; }
  main code { background: #88888822; padding: .1em .3em; border-radius: 4px; }
  main pre code { background: none; padding: 0; }
  main blockquote { border-left: 3px solid var(--line); margin-left: 0;
                    padding-left: 1rem; color: var(--muted); }
  iframe.pdf { width: 100%; height: 85vh; border: 1px solid var(--line);
               border-radius: 6px; }
  .note { color: var(--muted); font-size: .85rem; }
</style></head>
<body>
<header>
  <a class="back" href="/">← Inbox</a>
  <span class="name">__TITLE__</span>
  <button id="share" hidden>Share</button>
  <button id="copy" hidden>Copy</button>
  <a class="btn" id="download" href="/files/__NAME__?download=1">Download</a>
</header>
<main>__CONTENT__</main>
<script>
const NAME = __NAME_JSON__;
const FILE_URL = '/files/' + encodeURIComponent(NAME);
const IS_TEXT = __IS_TEXT__;
async function fileBlob() { return await (await fetch(FILE_URL)).blob(); }

const shareBtn = document.getElementById('share');
if (navigator.canShare) {
  shareBtn.hidden = false;
  shareBtn.onclick = async () => {
    const blob = await fileBlob();
    const file = new File([blob], NAME, { type: blob.type });
    try {
      if (navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: NAME });
      } else {
        await navigator.share({ title: NAME, url: location.href });
      }
    } catch (e) { /* user cancelled */ }
  };
}
// Download without navigating: a plain link would replace this page with
// the attachment URL — in the home-screen app (no browser chrome) that
// strands the user with no way back. Fetch → blob → synthetic <a download>
// keeps the reader in place; the href stays as a no-JS fallback.
document.getElementById('download').addEventListener('click', async (e) => {
  e.preventDefault();
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

const copyBtn = document.getElementById('copy');
if (IS_TEXT && navigator.clipboard) {
  copyBtn.hidden = false;
  copyBtn.onclick = async () => {
    const blob = await fileBlob();
    await navigator.clipboard.writeText(await blob.text());
    copyBtn.textContent = 'Copied!';
    setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
  };
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
        content = (
            f'<iframe class="pdf" src="/files/{_html_escape(name)}"></iframe>'
            '<p class="note">If only the first page shows (an iOS iframe '
            'limitation), use Share or Download for the full document.</p>'
        )
        is_text = "false"
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
        body = re.sub(r"\A---\n.*?\n---\n", "", raw, flags=re.DOTALL)
        if ext == ".md" and _markdown is not None:
            content = _render_markdown(body)
            mathjax = _MATHJAX_SNIPPET
        else:
            content = f"<pre>{_html_escape(body)}</pre>"
        is_text = "true"

    page = (_READ_HTML
            .replace("__HEAD_ICONS__", _HEAD_ICONS)
            .replace("__NAME_JSON__", json.dumps(name))  # before __NAME__!
            .replace("__NAME__", _html_escape(name))
            .replace("__TITLE__", _html_escape(name))
            .replace("__IS_TEXT__", is_text)
            .replace("__CONTENT__", content)
            .replace("__MATHJAX__", mathjax))
    return HTMLResponse(page)


def _forget_stem(stem: str) -> None:
    """Drop a deleted item's stem from the duplicate-URL index."""
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
            _url_index_path().write_text(json.dumps(idx, indent=1),
                                         encoding="utf-8")
        except OSError as e:
            _log("index", f"could not write URL index: {e}")


@app.post("/delete")
async def delete_item(stem: str = Form(...)):
    """Permanently delete all files of one saved item (inbox and archive/).

    The queue UI only offers this from the archive view (inbox → archive →
    delete, with a confirm prompt), but the endpoint itself removes the stem
    wherever it lives.
    """
    if not _RE_SAFE_STEM.match(stem):
        raise HTTPException(400, detail="invalid stem")
    removed = 0
    for folder in (OUTPUT_DIR, _archive_dir()):
        if folder.is_dir():
            for f in list(folder.iterdir()):
                if f.is_file() and f.stem == stem and f.suffix.lower() in _SERVE_EXTS:
                    f.unlink()
                    removed += 1
    if not removed:
        raise HTTPException(404, detail=f"no files found for {stem!r}")
    _forget_stem(stem)
    _log("delete", f"deleted {stem} ({removed} files)")
    return RedirectResponse(url="/?view=archive", status_code=303)


@app.post("/archive")
async def archive(stem: str = Form(...), action: str = Form("archive")):
    """Move all files of one saved item between the inbox and archive/."""
    if not _RE_SAFE_STEM.match(stem):
        raise HTTPException(400, detail="invalid stem")
    if action == "restore":
        src, dst, back = _archive_dir(), OUTPUT_DIR, "/?view=archive"
    else:
        src, dst, back = OUTPUT_DIR, _archive_dir(), "/"
    moved = 0
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.is_file() and f.stem == stem and f.suffix.lower() in _SERVE_EXTS:
                f.rename(_unique_path(dst / f.name))
                moved += 1
    if not moved:
        raise HTTPException(404, detail=f"no files found for {stem!r}")
    _log("archive", f"{action} {stem} ({moved} files)")
    return RedirectResponse(url=back, status_code=303)


@app.get("/health")
async def health(request: Request):
    exists = OUTPUT_DIR.exists()
    return {
        "status": "ok",
        "output_dir": str(OUTPUT_DIR),
        "output_dir_exists": exists,
        "output_dir_writable": exists and os.access(OUTPUT_DIR, os.W_OK),
        "saved_md_count": len(list(OUTPUT_DIR.glob("*.md"))) if exists else 0,
        "saved_pdf_count": len(list(OUTPUT_DIR.glob("*.pdf"))) if exists else 0,
        "pandoc_available": shutil.which("pandoc") is not None,
        "playwright_available": request.app.state.renderer.available,
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
