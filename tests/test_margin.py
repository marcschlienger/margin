# Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the extraction pipeline and save helpers.

Run with:  python -m pytest
No network, browser, or Mathpix access required.
"""
import shutil

import pytest

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
    assert app._ok(tmp_path / "a.md", "T")["summary"] == "Saved: a.md"
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
    assert 'action="/delete"' not in client.get("/").text
    assert 'action="/delete"' in client.get("/?view=archive").text


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
    assert "← Inbox" in r.text and "mathjax" in r.text.lower()


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
