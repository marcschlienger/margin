# Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the extraction pipeline and save helpers.

Run with:  python -m pytest
No network, browser, or Mathpix access required.
"""
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
