# Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the extraction pipeline and save helpers.

Run with:  python -m pytest
No network, browser, or Mathpix access required.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
from render import looks_blocked, looks_missing


# ---------------------------------------------------------------------------
# Unicode → LaTeX
# ---------------------------------------------------------------------------

def test_unicode_in_display_math_not_nested():
    out = app._apply_unicode_latex("$$\n\\alpha + β = γ\n$$")
    assert "\\beta" in out
    assert "$\\beta$" not in out  # no nested $...$ inside the block

def test_unicode_prose_symbol_wrapped():
    out = app._apply_unicode_latex("An isolated θ in prose.")
    assert "$\\theta$" in out

def test_unicode_inside_inline_math_converted():
    out = app._apply_unicode_latex("And $x + δ$ inline.")
    assert "\\delta" in out and "$x + \\delta $" in out

def test_unicode_part_of_word_untouched():
    assert app._apply_unicode_latex("The word Πλάτων stays.") == "The word Πλάτων stays."

def test_sub_sup_merge():
    assert app._apply_unicode_latex("$X$_{n}") == "$X_{n}$"


# ---------------------------------------------------------------------------
# Math element extraction from HTML
# ---------------------------------------------------------------------------

PAD = "Padding sentence so trafilatura keeps this paragraph. " * 20

def test_mjx_container_assistive_mathml():
    html = f"""<html><head><title>T</title></head><body><article><p>Einstein said
    <mjx-container class="MathJax"><mjx-math></mjx-math>
    <mjx-assistive-mml><math><mi>E</mi><mo>=</mo><mi>m</mi>
    <msup><mi>c</mi><mn>2</mn></msup></math></mjx-assistive-mml>
    </mjx-container> which is famous. {PAD}</p></article></body></html>"""
    _, body = app._extract_url_content(html, "http://x.test/")
    assert "E=m{c}^{2}" in body.replace(" ", "")

def test_mjx_container_prefers_tex_annotation():
    html = f"""<html><body><article><p>{PAD}</p>
    <mjx-container display="true"><mjx-assistive-mml>
    <math display="block"><semantics><mrow><mi>a</mi></mrow>
    <annotation encoding="application/x-tex">\\int_0^1 f(x)\\,dx</annotation>
    </semantics></math></mjx-assistive-mml></mjx-container>
    </article></body></html>"""
    _, body = app._extract_url_content(html, "http://x.test/")
    assert "\\int_0^1" in body

def test_mathjax2_script_tags_both_type_variants():
    html = f"""<html><body><article><p>Before. {PAD}
    <script type="math/tex">x^2</script> and
    <script type="math/tex;mode=display">\\sum_n 1/n^2</script>
    </p></article></body></html>"""
    _, body = app._extract_url_content(html, "http://x.test/")
    assert "$x^2$" in body
    assert "\\sum_n" in body and "$$" in body

def test_wikipedia_displaystyle_unwrapped():
    assert app._unwrap_displaystyle("{\\displaystyle x^2}") == "x^2"
    assert app._unwrap_displaystyle("x^2") == "x^2"

def test_mathml_structural_fraction():
    from bs4 import BeautifulSoup
    node = BeautifulSoup(
        "<math><mfrac><mi>a</mi><mi>b</mi></mfrac></math>", "html.parser"
    ).find("math")
    assert app._mathml_to_latex(node) == "\\frac{a}{b}"

def test_escape_percent_in_math():
    out = app._escape_math_special("$$ 50% of x $$ and $a%b$")
    assert "\\%" in out


# ---------------------------------------------------------------------------
# Titles, filenames, frontmatter
# ---------------------------------------------------------------------------

def test_clean_title_strips_site_suffix():
    assert app._clean_title("Article Name | Site") == "Article Name"
    assert app._clean_title("Article — Site") == "Article"

def test_slugify():
    assert app._slugify("Hello, Wörld!") == "hello-world"
    assert app._slugify("") == "untitled"

def test_unique_path(tmp_path):
    p = tmp_path / "a.md"
    assert app._unique_path(p) == p
    p.write_text("x")
    assert app._unique_path(p) == tmp_path / "a-2.md"

def test_frontmatter_tags():
    assert "tags: [readlater, math]" in app._frontmatter("T", has_math=True)
    assert "tags: [readlater]" in app._frontmatter("T", has_math=False)

def test_yaml_quote_escapes():
    assert app._yaml_quote('a "b" \\c') == '"a \\"b\\" \\\\c"'


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------

def test_formats_validation():
    p = app.SavePayload(url="https://a.test/x", formats=["PDF", ".tex", "org"])
    assert p.formats == ["pdf", "tex", "org"]
    with pytest.raises(ValueError):
        app.SavePayload(url="https://a.test/x", formats=["docx"])


def test_default_formats_shared_everywhere():
    # Ships as PDF + Markdown + TeX; Org opt-in. Text-only slice drops pdf.
    assert app.DEFAULT_FORMATS == ("pdf", "md", "tex")
    assert app._DEFAULT_MD_FORMATS == ("md", "tex")
    # An unspecified save uses the default; an empty list falls back to it too.
    assert app.SavePayload(url="https://a.test/x").formats == ["pdf", "md", "tex"]
    assert app.SavePayload(url="https://a.test/x", formats=[]).formats == \
        ["pdf", "md", "tex"]
    # Comma/space string accepted (iOS Shortcuts sends a text body, not JSON)
    assert app.SavePayload(url="https://a.test/x", formats="pdf, md").formats == \
        ["pdf", "md"]


def test_queue_checkboxes_reflect_default(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    html = TestClient(app.app).get("/").text
    assert 'value="pdf" checked' in html
    assert 'value="md" checked' in html
    assert 'value="tex" checked' in html
    assert 'value="org" checked' not in html   # Org opt-in


def test_source_link_from_index_for_pdf_only(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-p.pdf").write_bytes(b"%PDF")  # no .md, no frontmatter
    app._record_url("https://a.test/paper", "2026-07-19-p")
    (item,) = app._list_items(tmp_path)
    assert item["source"] == app._norm_url("https://a.test/paper")


def test_write_all_formats_md_only(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    written = app._write_all_formats("2026-07-19-t.md", "# T\n\nbody", "T", ("md",))
    assert [p.suffix for p in written] == [".md"]
    assert not (tmp_path / "2026-07-19-t.tex").exists()
    assert not (tmp_path / "2026-07-19-t.org").exists()


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
def test_write_all_formats_tex_without_md(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    written = app._write_all_formats("2026-07-19-t.md", "# T\n\nbody", "T", ("tex",))
    assert [p.suffix for p in written] == [".tex"]
    assert not (tmp_path / "2026-07-19-t.md").exists()


# ---------------------------------------------------------------------------
# PDF → PDF + Markdown/LaTeX (OCR), consistency across sources
# ---------------------------------------------------------------------------

def _patch_direct_pdf(monkeypatch, tmp_path, mathpix=True):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)

    async def fake_fetch(client, url):
        return b"%PDF-1.4 fake bytes"

    async def fake_title(client, url, data):
        return "Fourier Notes"

    async def fake_mathpix(data):
        return "# Fourier Notes\n\nInline $a_i + b_j$ math.\n"

    monkeypatch.setattr(app, "_fetch_pdf_bytes", fake_fetch)
    monkeypatch.setattr(app, "_direct_pdf_title", fake_title)
    monkeypatch.setattr(app, "_mathpix_pdf", fake_mathpix)
    monkeypatch.setattr(app, "MATHPIX_APP_ID", "id" if mathpix else "")
    monkeypatch.setattr(app, "MATHPIX_APP_KEY", "key" if mathpix else "")


def test_save_direct_pdf_with_ocr(tmp_path, monkeypatch):
    _patch_direct_pdf(monkeypatch, tmp_path, mathpix=True)
    with TestClient(app.app) as client:
        r = client.post("/save", json={"url": "https://a.test/doc.pdf",
                                       "formats": ["pdf", "md"]})
    d = r.json()
    assert d["status"] == "ok"
    exts = {f.rsplit(".", 1)[1] for f in d["files"]}
    assert "pdf" in exts and "md" in exts          # PDF kept + OCR'd to Markdown
    md = next(p for p in tmp_path.iterdir() if p.suffix == ".md")
    assert "$a_i + b_j$" in md.read_text()
    # PDF and Markdown share a stem so they group in the queue
    stems = {p.stem for p in tmp_path.iterdir() if p.suffix in (".pdf", ".md")}
    assert len(stems) == 1


def test_save_direct_pdf_without_mathpix_warns(tmp_path, monkeypatch):
    _patch_direct_pdf(monkeypatch, tmp_path, mathpix=False)
    with TestClient(app.app) as client:
        r = client.post("/save", json={"url": "https://a.test/doc.pdf",
                                       "formats": ["pdf", "md"]})
    d = r.json()
    assert d["status"] == "ok"
    assert any(f.endswith(".pdf") for f in d["files"])      # PDF still saved
    assert not any(f.endswith(".md") for f in d["files"])   # OCR skipped
    assert any("Mathpix" in w for w in d.get("warnings", []))


def test_save_pdf_upload_keeps_file_and_ocrs(tmp_path, monkeypatch):
    _patch_direct_pdf(monkeypatch, tmp_path, mathpix=True)
    with TestClient(app.app) as client:
        r = client.post("/save-pdf",
                        files={"file": ("doc.pdf", b"%PDF fake", "application/pdf")})
    d = r.json()
    assert d["status"] == "ok"
    assert any(f.endswith(".pdf") for f in d["files"])   # uploaded PDF kept
    assert any(f.endswith(".md") for f in d["files"])    # + OCR text


def test_save_pdf_upload_without_mathpix_keeps_pdf(tmp_path, monkeypatch):
    _patch_direct_pdf(monkeypatch, tmp_path, mathpix=False)
    with TestClient(app.app) as client:
        r = client.post("/save-pdf",
                        files={"file": ("doc.pdf", b"%PDF fake", "application/pdf")})
    d = r.json()
    assert d["status"] == "ok"
    assert any(f.endswith(".pdf") for f in d["files"])
    assert not any(f.endswith(".md") for f in d["files"])
    assert any("Mathpix" in w for w in d.get("warnings", []))


# ---------------------------------------------------------------------------
# URL validation and cleaning
# ---------------------------------------------------------------------------

def test_url_scheme_validation():
    with pytest.raises(ValueError):
        app._validated_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        app._validated_url("javascript:alert(1)")
    assert app._validated_url(" https://a.test/b ") == "https://a.test/b"

def test_shortcut_url_deduplication():
    doubled = "https://a.test/x\nhttps://a.test/x"
    assert app._clean_shortcut_url(doubled) == "https://a.test/x"
    spaced = "https://a.test/very long path"
    assert app._clean_shortcut_url(spaced) == "https://a.test/verylongpath"

def test_norm_url():
    assert app._norm_url("HTTPS://A.Test/x/") == app._norm_url("https://a.test/x")
    assert app._norm_url("https://a.test/x#frag") == app._norm_url("https://a.test/x")
    assert app._norm_url("https://a.test/x?q=1") != app._norm_url("https://a.test/x")


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def test_find_existing_via_index(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-18-x.pdf").write_bytes(b"%PDF")
    app._record_url("https://a.test/article", "2026-07-18-x")
    hit = app._find_existing("https://a.test/article/")  # trailing slash normed
    assert hit and hit["files"] == ["2026-07-18-x.pdf"]

def test_find_existing_stale_index_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    app._record_url("https://a.test/gone", "2026-07-18-gone")
    assert app._find_existing("https://a.test/gone") is None  # file deleted

def test_find_existing_via_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-18-y.md").write_text(
        '---\ntitle: "Y"\nsource_url: "https://a.test/y"\n---\nbody',
        encoding="utf-8",
    )
    hit = app._find_existing("https://a.test/y")
    assert hit and hit["title"] == "Y"

def test_find_existing_checks_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    app._record_url("https://a.test/z", "2026-07-18-z")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "2026-07-18-z.pdf").write_bytes(b"%PDF")
    assert app._find_existing("https://a.test/z") is not None


# ---------------------------------------------------------------------------
# Reading queue helpers
# ---------------------------------------------------------------------------

def test_list_items_groups_and_titles(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-18-a.pdf").write_bytes(b"%PDF")
    (tmp_path / "2026-07-18-a.md").write_text(
        '---\ntitle: "Real Title"\n---\n', encoding="utf-8")
    (tmp_path / "2026-07-17-b.pdf").write_bytes(b"%PDF")
    items = app._list_items(tmp_path)
    assert [i["stem"] for i in items] == ["2026-07-18-a", "2026-07-17-b"]
    assert items[0]["title"] == "Real Title"
    assert items[1]["title"] == "b"
    assert [f.suffix for f in items[0]["files"]] == [".pdf", ".md"]


# ---------------------------------------------------------------------------
# Token auth (MARGIN_TOKEN)
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient


def test_responses_carry_notification_summary(tmp_path, monkeypatch):
    assert app._err("boom")["summary"] == "Error: boom"
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-x.pdf").write_bytes(b"%PDF")
    app._record_url("https://a.test/x", "2026-07-19-x")
    dup = app._duplicate_response(app._find_existing("https://a.test/x"))
    assert dup["summary"] == "Already saved: 2026-07-19-x.pdf"


def test_unauthorized_page_has_token_form(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MARGIN_TOKEN", "s3cret")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    r = TestClient(app.app).get("/", headers={"accept": "text/html"})
    assert r.status_code == 401
    assert 'name="token"' in r.text and 'action="/"' in r.text


def test_auth_disabled_when_no_token(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MARGIN_TOKEN", "")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    assert TestClient(app.app).get("/").status_code == 200


def test_auth_enforced_and_cookie_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MARGIN_TOKEN", "s3cret")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    client = TestClient(app.app)

    # /health stays open; everything else is rejected without the token
    assert client.get("/health/../").status_code in (401, 404)
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 401 and "Token required" in r.text
    assert client.post(
        "/save-url", json={"url": "https://a.test/x"}
    ).status_code == 401
    assert client.get("/files/x.pdf").status_code == 401

    # Bearer header works
    assert client.get(
        "/", headers={"Authorization": "Bearer s3cret"}
    ).status_code == 200
    # Wrong token still rejected
    assert client.get(
        "/", headers={"Authorization": "Bearer nope"}
    ).status_code == 401

    # Query token works and sets the SameSite=Strict cookie...
    r = client.get("/?token=s3cret")
    assert r.status_code == 200
    assert "margin_token" in r.cookies or "margin_token" in client.cookies
    # ...after which plain browsing works via the cookie
    assert client.get("/").status_code == 200


def test_icons_public_even_with_token(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "MARGIN_TOKEN", "s3cret")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    client = TestClient(app.app)
    for path, ctype in [
        ("/favicon.svg", "image/svg+xml"),
        ("/favicon.ico", "image/x-icon"),  # real ICO for Safari
        ("/favicon-32.png", "image/png"),
        ("/apple-touch-icon.png", "image/png"),
        ("/apple-touch-icon-precomposed.png", "image/png"),
        ("/static/icon-512.png", "image/png"),
        ("/static/icon-192.png", "image/png"),
        ("/static/icon-512-maskable.png", "image/png"),
        ("/manifest.json", "application/manifest+json"),
    ]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith(ctype), path


def test_health_open_and_reports_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "MARGIN_TOKEN", "s3cret")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    with TestClient(app.app) as client:  # lifespan: /health touches app.state
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["auth_required"] is True


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def test_delete_removes_files_and_index_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-z.md").write_text("x", encoding="utf-8")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "2026-07-19-z.pdf").write_bytes(b"%PDF")
    app._record_url("https://a.test/z", "2026-07-19-z")

    client = TestClient(app.app)
    r = client.post("/delete", data={"stem": "2026-07-19-z"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert not (tmp_path / "2026-07-19-z.md").exists()
    assert not (tmp_path / "archive" / "2026-07-19-z.pdf").exists()
    assert app._find_existing("https://a.test/z") is None
    assert app._load_url_index() == {}          # index entry cleaned up

    assert client.post("/delete", data={"stem": "../evil"}).status_code == 400
    assert client.post("/delete", data={"stem": "2026-07-19-z"}).status_code == 404


def test_delete_button_only_in_archive_view(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-a.pdf").write_bytes(b"%PDF")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "2026-07-19-b.pdf").write_bytes(b"%PDF")
    client = TestClient(app.app)
    # The form, not the attribute: the page's own script names that selector
    # too, so the bare string is in every view whether a button is or not.
    form = '<form method="post" action="/delete"'
    assert form not in client.get("/").text
    assert form in client.get("/?view=archive").text


# ---------------------------------------------------------------------------
# Reader (/read/{name})
# ---------------------------------------------------------------------------

def test_sanitizer_strips_scripts_and_attrs():
    dirty = ('<p onclick="evil()">hi</p><script>steal()</script>'
             '<a href="javascript:x">l</a><a href="https://a.test/">ok</a>')
    clean = app._sanitize_html(dirty)
    assert "<p>hi</p>" in clean
    assert "steal" not in clean and "onclick" not in clean
    assert 'href="https://a.test/"' in clean and "javascript:" not in clean


def test_reader_renders_markdown_with_math(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-r.md").write_text(
        '---\ntitle: "R"\n---\n# Heading\n\nInline $a_i + b_j$ math.\n'
        "<script>alert(1)</script>\n", encoding="utf-8")
    r = TestClient(app.app).get("/read/2026-07-19-r.md")
    assert r.status_code == 200
    assert "<h1>Heading</h1>" in r.text
    assert "$a_i + b_j$" in r.text          # math untouched (no <em> mangling)
    assert "alert(1)" not in r.text
    # Download must be JS-driven (no page navigation in the standalone app)
    assert 'id="download"' in r.text and "preventDefault" in r.text
    # "← Margin", not "← Inbox": the reader is reached from the archive
    # too, and the wordmark is what the sibling app puts there.
    assert "← Margin" in r.text and "mathjax" in r.text.lower()


def test_reader_pdf_wraps_iframe(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-p.pdf").write_bytes(b"%PDF")
    r = TestClient(app.app).get("/read/2026-07-19-p.pdf")
    assert '<iframe class="pdf" src="/files/2026-07-19-p.pdf">' in r.text


def test_files_download_disposition(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-d.md").write_text("x", encoding="utf-8")
    client = TestClient(app.app)
    plain = client.get("/files/2026-07-19-d.md")
    forced = client.get("/files/2026-07-19-d.md?download=1")
    assert "attachment" not in plain.headers.get("content-disposition", "")
    assert "attachment" in forced.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# Renderer heuristics
# ---------------------------------------------------------------------------

def test_looks_blocked():
    assert looks_blocked("Just a moment...")
    assert looks_blocked("Attention Required! | Cloudflare")
    assert not looks_blocked("Attention mechanisms in transformers")

def test_looks_missing():
    assert looks_missing("Page not found | Blog", 403)
    assert looks_missing("Anything", 404)
    assert not looks_missing("A fine article", 200)


# ---------------------------------------------------------------------------
# What the outside world is told, and what it may do
# ---------------------------------------------------------------------------

def test_health_does_not_name_the_output_directory(tmp_path, monkeypatch):
    """/health is public — it has to be, for a probe that carries no
    credentials — and on a real install the path names the account it runs
    as. Whether the folder works is the useful part; where it is, is not."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    with TestClient(app.app) as client:   # lifespan: /health touches app.state
        body = client.get("/health").json()
    assert body["output_dir_exists"] is True
    assert "output_dir" not in body
    assert not any(str(tmp_path) in str(v) for v in body.values())


def test_a_cross_site_request_may_not_change_anything(tmp_path, monkeypatch):
    """CORS does not help here: a plain HTML form posts cross-origin without
    a preflight, and the browser sends it whether or not the answer can be
    read. With MARGIN_TOKEN unset — the documented private-network default —
    any page you visited could archive, restore or delete a saved item.
    Reproduced against a running instance: Origin: https://evil.test on a
    form POST to /archive moved the item and answered 303."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-x.md").write_text("---\ntitle: \"X\"\n---\nbody\n",
                                             encoding="utf-8")
    client = TestClient(app.app)
    form = {"stem": "2026-07-19-x", "action": "archive"}

    hostile = client.post("/archive", data=form,
                          headers={"Origin": "https://evil.test",
                                   "Sec-Fetch-Site": "cross-site"},
                          follow_redirects=False)
    assert hostile.status_code == 403
    assert (tmp_path / "2026-07-19-x.md").is_file()      # nothing moved

    # The app's own form, and clients that send no Sec-Fetch-Site at all
    # (curl, the Shortcut, an RSS reader), are unaffected.
    same = client.post("/archive", data=form,
                       headers={"Sec-Fetch-Site": "same-origin"},
                       follow_redirects=False)
    assert same.status_code == 303
    assert (tmp_path / "archive" / "2026-07-19-x.md").is_file()


def test_reading_is_still_allowed_across_sites():
    """GET stays open: the bookmarklet's whole job is a cross-site
    navigation to /save-page."""
    answer = TestClient(app.app).get(
        "/health", headers={"Sec-Fetch-Site": "cross-site"})
    assert answer.status_code == 200


def test_cross_origin_access_is_opt_in():
    """A wildcard let any page you happen to be visiting read this
    instance's answers."""
    source = Path(app.__file__).read_text()
    assert 'allow_origins=["*"]' not in source
    assert "MARGIN_CORS_ORIGINS" in source


# ---------------------------------------------------------------------------
# Errors a browser can get out of
# ---------------------------------------------------------------------------

def test_a_browser_gets_a_page_back_from_an_error(tmp_path, monkeypatch):
    """A stale bookmark or a file moved in the synced folder is ordinary,
    and a bare JSON body leaves the reader with no way back — on the home
    screen there is not even a back button."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    client = TestClient(app.app)
    for path in ("/read/nope.md", "/no-such-page", "/files/nope.md"):
        page = client.get(path, headers={"Accept": "text/html"})
        assert page.status_code == 404, path
        assert "text/html" in page.headers["content-type"], path
        assert "← Margin" in page.text, path
    # And a client that asked for JSON still gets JSON.
    api = client.get("/read/nope.md")
    assert api.status_code == 404
    assert api.json()["detail"]


# ---------------------------------------------------------------------------
# What is served, and what is linked
# ---------------------------------------------------------------------------

def test_a_link_out_of_the_folder_is_not_a_saved_file(tmp_path, monkeypatch):
    """The output directory is a synced folder that people and sync clients
    write to, so a name inside it can be a link to anything the account can
    read."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    secret = tmp_path.parent / "elsewhere.md"
    secret.write_text("not yours\n", encoding="utf-8")
    (tmp_path / "2026-07-19-linked.md").symlink_to(secret)
    (tmp_path / "2026-07-19-real.md").write_text("---\ntitle: \"R\"\n---\nok\n",
                                                encoding="utf-8")
    client = TestClient(app.app)
    assert client.get("/files/2026-07-19-linked.md").status_code == 404
    assert client.get("/read/2026-07-19-linked.md").status_code == 404
    assert client.get("/files/2026-07-19-real.md").status_code == 200


def test_a_source_link_is_a_url_we_would_follow(tmp_path, monkeypatch):
    """Front matter comes out of a folder people and sync clients write to.
    A "source" recorded there was rendered as a link whatever it said."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-bad.md").write_text(
        '---\ntitle: "Bad"\nsource_url: "javascript:alert(1)"\n---\nbody\n',
        encoding="utf-8")
    (tmp_path / "2026-07-19-good.md").write_text(
        '---\ntitle: "Good"\nsource_url: "https://example.test/a"\n---\nbody\n',
        encoding="utf-8")
    page = TestClient(app.app).get("/").text
    assert "javascript:alert(1)" not in page
    assert 'href="https://example.test/a"' in page
    # Off-site, so it opens off-site rather than replacing the queue.
    assert 'rel="noopener noreferrer"' in page


def test_the_two_apps_share_one_palette():
    """Margin and Footnote are siblings; the :root block is the shared part
    and the two stylesheets are meant to stay diffable."""
    css = (Path(app.__file__).parent / "static" / "style.css").read_text()
    for token in ("--paper: #F5EEDC", "--ink: #33394A", "--rule-red: #C43D33",
                  "--pen-blue: #3D6BB3", "--card: #FBF7EB",
                  '--serif: "Iowan Old Style"'):
        assert token in css, token
    # Every page links it rather than carrying its own copy.
    source = Path(app.__file__).read_text()
    assert source.count("<style>") == 0
    assert source.count('href="/static/style.css"') == 1


def test_the_reader_can_copy_without_a_secure_context(tmp_path, monkeypatch):
    """The Clipboard API is secure-context only, and Margin is normally
    reached over plain HTTP on a home network — so Copy was hidden there
    rather than offered."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-c.md").write_text("---\ntitle: \"C\"\n---\nbody\n",
                                             encoding="utf-8")
    page = TestClient(app.app).get("/read/2026-07-19-c.md").text
    assert "document.execCommand('copy')" in page
    # Offered whenever the file is text, not only where the API exists.
    assert "if (IS_TEXT) {" in page
    # And a failure says so, rather than looking like a button that does
    # nothing: these pages have no flash area.
    assert "function say(" in page and "Could not " in page


# ---------------------------------------------------------------------------
# Deployment — both scripts run as root and both have a destructive edge
# ---------------------------------------------------------------------------

_DEPLOY = Path(app.__file__).resolve().parent / "deploy"


def _shell(body: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "run.sh"
        runner.write_text(f'. "{_DEPLOY / "paths.sh"}"\n{body}\n')
        return subprocess.run(["bash", str(runner)], capture_output=True,
                              text=True)


def _extract(script: str, name: str) -> str:
    text = (_DEPLOY / script).read_text()
    return name + "() {" + text.split(name + "() {")[1].split("\n}")[0] + "\n}"


def _run_app_dir_check(app_dir, repo_dir=""):
    body = ('APP_MARKER=".margin-install"\n'
            + _extract("install.sh", "check_app_dir")
            + f'\ncheck_app_dir "{app_dir}" "{repo_dir}"')
    return _shell(body).returncode


def test_an_instance_directory_is_never_a_system_directory(tmp_path):
    """The output directory used not to be checked at all, so "/" was a
    legal argument — and the script would then run
    install -d -o <user> -m 700 on the root of the filesystem, as root."""
    def check(path):
        return _shell(f'check_target_dir output-dir "{path}"').returncode

    for system in ("/", "/home", "/srv", "/var", "/etc", "/opt", "/tmp"):
        assert check(system) != 0, system
    assert check("/opt/") != 0                 # the same directory, spelled on
    assert check("/srv/../etc") != 0           # …and reached by another route
    assert check("pages") != 0                 # not absolute at all
    assert check("/home/someone/ReadLater/inbox") == 0
    assert check(str(tmp_path / "inbox")) == 0

    link = tmp_path / "shortcut"
    link.symlink_to("/", target_is_directory=True)
    assert check(str(link)) != 0


def test_an_env_value_that_cannot_be_read_back_is_refused():
    """A path with a $ or a backslash is written verbatim on the first run
    and refused by the parser on the next, so the run that is supposed to be
    idempotent stops instead. Caught before anything is written."""
    def check(value):
        # Passed as an argument, not spliced into the script: the point of
        # the test is characters that mean something to a shell.
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / "run.sh"
            runner.write_text(f'. "{_DEPLOY / "paths.sh"}"\n'
                              'check_storable output-dir "$1"\n')
            return subprocess.run(["bash", str(runner), value],
                                  capture_output=True, text=True).returncode

    assert check("/home/me/ReadLater") == 0
    assert check("/srv/My Pages") == 0                  # a space is fine
    for bad in ("/home/me/$research", "/home/me/back\\slash",
                '/home/me/qu"ote', "/home/me/apos'trophe", "/home/me/two\nlines"):
        assert check(bad) != 0, bad
    add = (_DEPLOY / "add-instance.sh").read_text()
    assert add.index("check_storable output-dir") < add.index('cat > "$ENV_FILE"')


def test_the_installer_refuses_to_empty_a_directory_it_does_not_own(tmp_path):
    """APP_DIR is an override, and the copy is rsync --delete as root.
    Aimed at /opt or any populated directory, "install" means "delete what
    is there"."""
    for system in ("/", "/opt", "/usr", "/var"):
        assert _run_app_dir_check(system) != 0, system

    empty = tmp_path / "fresh"
    empty.mkdir()
    assert _run_app_dir_check(str(empty)) == 0
    assert _run_app_dir_check(str(tmp_path / "not-there-yet")) == 0

    occupied = tmp_path / "someone-elses"
    occupied.mkdir()
    (occupied / "important.conf").write_text("keep me")
    assert _run_app_dir_check(str(occupied)) != 0

    # The upgrade path this script exists to be: an installation made before
    # the marker existed is recognised, not refused.
    (occupied / "app.py").write_text("")
    (occupied / "render.py").write_text("")
    (occupied / "static").mkdir()
    assert _run_app_dir_check(str(occupied)) == 0


def test_the_installer_and_the_checkout_stay_apart(tmp_path):
    """rsync --delete reading from a directory it is emptying leaves neither
    a checkout nor a finished install."""
    app_dir = tmp_path / "opt-margin"
    (app_dir / "src" / "static").mkdir(parents=True)
    (app_dir / ".margin-install").write_text("")
    for name in ("app.py", "render.py"):
        (app_dir / name).write_text("")
        (app_dir / "src" / name).write_text("")
    (app_dir / "static").mkdir()
    assert _run_app_dir_check(str(app_dir), str(app_dir / "src")) != 0
    assert _run_app_dir_check(str(app_dir / "inner"), str(app_dir)) != 0
    beside = tmp_path / "checkout"
    beside.mkdir()
    assert _run_app_dir_check(str(app_dir), str(beside)) == 0


def test_the_instance_script_validates_before_it_writes():
    add = (_DEPLOY / "add-instance.sh").read_text()
    assert add.index("check_target_dir output-dir") < add.index('cat > "$ENV_FILE"')
    assert add.index("port must be between 1 and 65535") < add.index('cat > "$ENV_FILE"')
    assert "-m 700 -- " in add                 # install(1) reads a leading dash
    assert "is-active --quiet" in add          # do not claim success on failure
    assert "umask 077" in add                  # no world-readable token, ever


def test_an_existing_instance_keeps_its_stored_settings():
    """systemd reads the env file, not the command line. Re-running with a
    different port changed nothing and then printed the new number."""
    def stored(text):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "instance.env"
            env.write_text(text)
            body = (f'ENV_FILE={env}\n'
                    + "\n".join(_extract("add-instance.sh", n) for n in
                                ("read_env_value", "read_env_path",
                                 "read_env_port", "check_stored_env"))
                    + '\ncheck_stored_env && echo "$STORED_PORT $STORED_OUT"')
            return _shell(body)

    good = stored("PORT=8000\nOUTPUT_DIR=/srv/pages\n")
    assert good.returncode == 0 and good.stdout.strip() == "8000 /srv/pages"
    for broken in ("OUTPUT_DIR=/srv/pages\n", "PORT=eighty\nOUTPUT_DIR=/srv/p\n",
                   "PORT=99999\nOUTPUT_DIR=/srv/p\n", "PORT=8000\n",
                   "PORT=8000\nOUTPUT_DIR=/\n",
                   "PORT=8000\nOUTPUT_DIR=pages\n"):
        assert stored(broken).returncode != 0, broken


def test_the_installer_rebuilds_a_venv_from_another_interpreter():
    """"New enough" would keep a 3.10 environment on a run that said
    PYTHON=/usr/bin/python3.12, so the override silently did nothing."""
    install = (_DEPLOY / "install.sh").read_text()
    assert "PY_IDENTITY=" in install
    assert "base_prefix" in install            # version alone is not identity
    assert '"$HAVE_PY" != "$WANT_PY"' in install
    # The floor the README states is the floor the installer enforces.
    assert "sys.version_info >= (3, 10)" in install
    assert "22.04" in install and "20.04" in install
    # And a constraints file resolved elsewhere is refused rather than used.
    assert "was not resolved for" in install
    generator = (_DEPLOY / "make-constraints.sh").read_text()
    assert '"# Resolved from requirements.txt on {where}."' in generator


def test_the_shared_credentials_are_never_briefly_world_readable():
    install = (_DEPLOY / "install.sh").read_text()
    assert "(umask 077 && cp" in install
    # The recursive pass steps over the env file rather than widening it and
    # putting it back — before this, nothing narrowed it at all.
    assert 'find "$APP_DIR" -path "$APP_DIR/.env" -prune' in install
    assert "chmod -R a+rX" not in install
    assert 'chmod 640 "$APP_DIR/.env"' in install
    unit = (_DEPLOY / "margin@.service").read_text()
    assert "UMask=0077" in unit


def test_the_shell_scripts_declare_their_dialect():
    """paths.sh has no shebang because it is sourced, and ShellCheck stops
    at SC2148 without knowing what to assume."""
    assert "# shellcheck shell=bash" in (_DEPLOY / "paths.sh").read_text()
    for name in ("install.sh", "add-instance.sh", "make-constraints.sh"):
        assert (_DEPLOY / name).read_text().startswith("#!/usr/bin/env bash")


# ---------------------------------------------------------------------------
# Limits that limit, and state that survives being interrupted
# ---------------------------------------------------------------------------

def test_an_oversized_upload_is_refused_before_it_is_stored():
    """Checking the size after reading is not a limit: Starlette parses the
    whole multipart body before the handler runs, so a 200 MB upload was
    spooled to disk in full and only then told it was too large — measured
    against a running instance, with the error quoting all 209,715,209
    bytes. On an instance with no token that is unauthenticated disk use."""
    client = TestClient(app.app)
    answer = client.post(
        "/save-pdf",
        headers={"Content-Length": str(app.MAX_BODY_BYTES + 1)},
        content=b"x" * 32)
    assert answer.status_code == 413
    assert "too large" in answer.json()["message"]
    # The cap leaves room for the multipart framing around a PDF at the cap.
    assert app.MAX_BODY_BYTES > app.MAX_PDF_BYTES


def test_a_small_upload_still_reaches_the_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "DEFAULT_FORMATS", ("pdf",))
    monkeypatch.setattr(app, "_DEFAULT_MD_FORMATS", ())
    answer = TestClient(app.app).post(
        "/save-pdf", files={"file": ("small.pdf", b"%PDF-1.4\n%small\n",
                                     "application/pdf")})
    assert answer.status_code == 200
    assert answer.json()["status"] == "ok"


def test_no_response_names_the_output_directory(tmp_path, monkeypatch):
    """A client addresses a save by its filename. The folder's path names the
    account the service runs as, and nothing consumed it — the iOS Shortcut
    reads "summary"."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "DEFAULT_FORMATS", ("pdf",))
    monkeypatch.setattr(app, "_DEFAULT_MD_FORMATS", ())
    with TestClient(app.app) as client:   # lifespan: /health touches app.state
        saved = client.post("/save-pdf",
                            files={"file": ("s.pdf", b"%PDF-1.4\n%s\n",
                                            "application/pdf")}).json()
        health = client.get("/health").json()
    assert "path" not in saved
    assert str(tmp_path) not in json_dumps(saved)
    assert str(tmp_path) not in json_dumps(health)
    assert saved["files"] == [Path(saved["files"][0]).name]     # names, not paths


def json_dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj)


def test_the_url_index_is_written_atomically(tmp_path, monkeypatch):
    """It lives in a folder a sync client watches, and write_text truncates
    before it writes. A half file cannot be parsed, and an unparsable index
    is answered with an empty one — so every recorded URL is silently
    forgotten and every page looks new again."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    app._record_url("https://example.test/a", "2026-07-19-a")
    assert app._load_url_index() == {"https://example.test/a": ["2026-07-19-a"]}

    # Nothing but the index is left behind: the temp file is renamed over it.
    assert [f.name for f in tmp_path.iterdir()] == [".saved-urls.json"]

    source = Path(app.__file__).read_text()
    assert "os.replace(" in source
    writer = source[source.index("def _record_url("):]
    writer = writer[:writer.index("\n\n\n")]
    # Comments are allowed to name the thing they replaced; code is not.
    code = "\n".join(line for line in writer.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "write_text" not in code


def test_an_unreadable_index_says_so(tmp_path, monkeypatch, capsys):
    """Duplicate detection quietly stops working, which is worth one line."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / ".saved-urls.json").write_text("{half a fi", encoding="utf-8")
    assert app._load_url_index() == {}
    assert "unreadable" in capsys.readouterr().err      # _log writes to stderr


# ---------------------------------------------------------------------------
# Where the server may be sent
# ---------------------------------------------------------------------------

def test_the_server_will_not_fetch_its_own_network():
    """Margin fetches whatever URL it is handed and then serves the result
    back through /read and /files, so an address inside this machine is a
    server-side request forgery with the answer included. Verified before the
    guard existed: POST /save-url with http://127.0.0.1:<port>/health saved
    the response into the output folder."""
    public = ["https://example.com/a", "http://example.com",
              "https://b\u00fccher.example/a"]          # IDNA names still pass
    private = ["http://127.0.0.1:8000/health", "http://localhost/x",
               "http://127.1/x", "http://2130706433/x", "http://0x7f000001/x",
               "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/x",
               "http://192.168.1.1/x", "http://[::1]/x",
               "http://100.64.0.1/x",                    # carrier-grade NAT
               "http://127\u30020\u30020\u30021/x"]   # IDNA folds these onto loopback
    for url in public:
        assert app.is_public_http_url(url) is True, url
    for url in private:
        assert app.is_public_http_url(url) is False, url


def test_a_private_address_is_refused_by_the_validator(monkeypatch):
    monkeypatch.setattr(app, "ALLOW_PRIVATE_URLS", False)
    assert app._validated_url("https://example.com/a") == "https://example.com/a"
    with pytest.raises(ValueError, match="inside this machine"):
        app._validated_url("http://127.0.0.1:8000/health")


def test_an_operator_can_say_they_want_private_addresses(monkeypatch):
    """Saving from an internal wiki on the same network is a real thing to
    want, and the operator is the one who gets to decide."""
    monkeypatch.setattr(app, "ALLOW_PRIVATE_URLS", True)
    assert app._validated_url("http://127.0.0.1:8000/health").startswith("http://")


# ---------------------------------------------------------------------------
# Offline reading — the wiring; the behaviour is in tests/test_browser.py
# ---------------------------------------------------------------------------

def test_the_worker_is_served_from_the_root():
    """A worker's scope is the directory it is served from, and one under
    /static/ could not answer for "/" — which is the queue."""
    with TestClient(app.app) as client:
        answer = client.get("/service-worker.js")
    assert answer.status_code == 200
    assert "javascript" in answer.headers["content-type"]
    # Never from a cache without asking: a deployed fix that does not arrive
    # is the worst kind, and this file decides what everything else does.
    assert answer.headers.get("cache-control") == "no-cache"


def test_the_worker_registers_before_any_token_exists(monkeypatch, tmp_path):
    """Registration happens on the first visit, which is the visit that has
    not authenticated yet — the same reason the icons are public."""
    monkeypatch.setattr(app, "MARGIN_TOKEN", "s3cret")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    with TestClient(app.app) as client:
        assert client.get("/service-worker.js").status_code == 200
        assert client.get("/").status_code == 401       # everything else is not


def test_the_queue_leaves_room_for_the_offline_line(tmp_path, monkeypatch):
    """The worker fills the marker in rather than parsing the page."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    page = TestClient(app.app).get("/").text
    assert "<!--offline-notice-->" in page
    assert "navigator.serviceWorker.register('/service-worker.js')" in page
    worker = (Path(app.__file__).parent / "static" / "service-worker.js").read_text()
    assert "<!--offline-notice-->" in worker             # both ends of the deal
    css = (Path(app.__file__).parent / "static" / "style.css").read_text()
    assert ".offline {" in css


def test_deleting_tells_the_worker_before_it_navigates(tmp_path, monkeypatch):
    """The 404-driven cleanup only fires if someone asks for the file again,
    and offline that may never happen — so a deleted page would stay
    readable for ever."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "2026-07-19-a.pdf").write_bytes(b"%PDF")
    page = TestClient(app.app).get("/?view=archive").text
    assert "forget-stem" in page
    worker = (Path(app.__file__).parent / "static" / "service-worker.js").read_text()
    assert '"forget-stem"' in worker
