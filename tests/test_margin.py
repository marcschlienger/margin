# Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for the extraction pipeline and save helpers.

Run with:  python -m pytest
No network, browser, or Mathpix access required.
"""
import asyncio
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import httpx
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

def test_titles_are_one_safe_line():
    title = app._clean_title(" First\nsecond\x00\ud800 | Site ")
    assert title == "First second\ufffd"
    title.encode("utf-8")

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
    for malformed in (None, 5, [5], [{}], [None]):
        with pytest.raises(ValueError):
            app.SavePayload(url="https://a.test/x", formats=malformed)


def test_default_formats_fail_loudly_on_a_typo():
    with pytest.raises(RuntimeError, match="DEFAULT_FORMATS"):
        app._parse_default_formats("pdf,markdonw")


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


def test_a_text_only_save_reserves_the_whole_file_family(tmp_path, monkeypatch):
    """Checking only the absent `.md` let a TeX-only save overwrite `.tex`."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    original = tmp_path / "2026-07-19-t.tex"
    original.write_text("keep me", encoding="utf-8")

    def fake_tex(source, target, title):
        target.write_text("new", encoding="utf-8")

    monkeypatch.setattr(app, "_write_tex", fake_tex)
    written = app._write_all_formats(
        "2026-07-19-t.md", "# T", "T", ("tex",)
    )
    assert original.read_text() == "keep me"
    assert [p.name for p in written] == ["2026-07-19-t-2.tex"]


def test_atomic_org_output_names_its_writer_explicitly(tmp_path, monkeypatch):
    """The atomic temp ends in `.tmp`, so Pandoc cannot infer Org from it."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    seen = []

    def fake_pandoc(args, label):
        seen.extend(args)
        Path(args[args.index("-o") + 1]).write_text("* T", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(app, "_run_pandoc", fake_pandoc)
    written = app._write_all_formats(
        "2026-07-19-t.md", "# T", "T", ("org",)
    )
    assert [p.name for p in written] == ["2026-07-19-t.org"]
    assert seen[seen.index("-t") + 1] == "org"


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
                        files={"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")})
    d = r.json()
    assert d["status"] == "ok"
    assert any(f.endswith(".pdf") for f in d["files"])   # uploaded PDF kept
    assert any(f.endswith(".md") for f in d["files"])    # + OCR text


def test_save_pdf_upload_without_mathpix_keeps_pdf(tmp_path, monkeypatch):
    _patch_direct_pdf(monkeypatch, tmp_path, mathpix=False)
    with TestClient(app.app) as client:
        r = client.post("/save-pdf",
                        files={"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")})
    d = r.json()
    assert d["status"] == "ok"
    assert any(f.endswith(".pdf") for f in d["files"])
    assert not any(f.endswith(".md") for f in d["files"])
    assert any("Mathpix" in w for w in d.get("warnings", []))


def test_a_non_pdf_is_rejected_before_paid_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "MATHPIX_APP_ID", "id")
    monkeypatch.setattr(app, "MATHPIX_APP_KEY", "key")
    called = False

    async def fake_mathpix(data):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(app, "_mathpix_pdf", fake_mathpix)
    answer = TestClient(app.app).post(
        "/save-pdf", files={"file": ("wrong.pdf", b"plain text", "application/pdf")}
    ).json()
    assert answer["status"] == "error"
    assert called is False


# ---------------------------------------------------------------------------
# URL validation and cleaning
# ---------------------------------------------------------------------------

def test_url_scheme_validation():
    with pytest.raises(ValueError):
        app._validated_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        app._validated_url("javascript:alert(1)")
    assert app._validated_url(" https://a.test/b ") == "https://a.test/b"
    with pytest.raises(ValueError, match="Unicode"):
        app._validated_url("https://a.test/\ud800")
    with pytest.raises(ValueError, match="credentials"):
        app._validated_url("https://user:secret@example.com/article")
    with pytest.raises(ValueError, match="too long"):
        app._validated_url("https://example.com/" + "x" * app.MAX_URL_CHARS)

def test_shortcut_url_deduplication():
    doubled = "https://a.test/x\nhttps://a.test/x"
    assert app._clean_shortcut_url(doubled) == "https://a.test/x"
    spaced = "https://a.test/very long path"
    assert app._clean_shortcut_url(spaced) == "https://a.test/verylongpath"

def test_norm_url():
    assert app._norm_url("HTTPS://A.Test/x/") == app._norm_url("https://a.test/x")
    assert app._norm_url("https://a.test/x#frag") == app._norm_url("https://a.test/x")
    assert app._norm_url("https://a.test/x?q=1") != app._norm_url("https://a.test/x")


def test_http_redirects_are_checked_before_the_next_request(monkeypatch):
    monkeypatch.setattr(app, "ALLOW_PRIVATE_URLS", False)
    seen = []

    def answer(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(answer), follow_redirects=True,
            event_hooks={"request": [app._validate_outbound_request]},
        ) as client:
            with pytest.raises(httpx.RequestError, match="policy"):
                await client.get("https://example.com/start")

    asyncio.run(run())
    assert seen == ["https://example.com/start"]


def test_browser_policy_checks_redirects_and_subresources(monkeypatch):
    """The syntactic half. Resolution is stubbed so the test does not need a
    resolver; it has a test of its own below."""
    import asyncio

    monkeypatch.setattr(app, "ALLOW_PRIVATE_URLS", False)

    async def resolves(host, port):
        return True

    monkeypatch.setattr(app, "_host_resolves_public", resolves)
    allowed = lambda url: asyncio.run(app._browser_url_allowed(url))  # noqa: E731
    assert allowed("https://example.com/page")
    assert allowed("data:text/plain,ok")
    assert not allowed("http://127.0.0.1/private")
    assert not allowed("file:///etc/passwd")


def _with_resolver(monkeypatch, mapping):
    """Answer getaddrinfo from a dict, so no test needs a resolver."""
    import socket as _socket

    async def fake(host, port, *args, **kwargs):
        if host not in mapping:
            raise OSError("name or service not known")
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (addr, port or 80))
                for addr in mapping[host]]

    class Loop:
        getaddrinfo = staticmethod(fake)

    monkeypatch.setattr(app.asyncio, "get_running_loop", lambda: Loop())
    app._resolved.clear()


def test_a_name_that_resolves_inward_is_refused(monkeypatch):
    """is_public_http_url answers "a name; the resolver decides", and nothing
    asked the resolver — so the address checks only ever stopped literal IPs,
    which is the one form nobody has to use. Demonstrated against a running
    instance with localtest.me, a free public service resolving to 127.0.0.1:
    POST /save-url fetched this server's own /health and filed the answer."""
    import asyncio

    monkeypatch.setattr(app, "ALLOW_PRIVATE_URLS", False)
    _with_resolver(monkeypatch, {
        "localtest.me": ["127.0.0.1"],
        "sneaky.test": ["93.184.216.34", "10.0.0.5"],   # one of each
        "tailnet.test": ["100.64.0.1"],                 # carrier-grade NAT
        "example.com": ["93.184.216.34"],
        "v6.test": ["::1"],
    })
    outward = lambda url: asyncio.run(app._url_points_outward(url))  # noqa: E731

    assert outward("https://example.com/a") is True
    assert outward("http://localtest.me:8060/health") is False
    assert outward("http://sneaky.test/x") is False     # every address must pass
    assert outward("http://tailnet.test/x") is False
    assert outward("http://nowhere.test/x") is False    # unresolvable
    # And an operator who says they want private addresses still gets them.
    monkeypatch.setattr(app, "ALLOW_PRIVATE_URLS", True)
    assert outward("http://localtest.me:8060/health") is True


def test_the_outbound_hook_refuses_a_name_that_points_inward(monkeypatch):
    """Every request the client makes goes through the hook, redirects
    included — so this is where the answer has to be enforced, not only in
    the validator that runs once on the way in."""
    import asyncio
    import httpx

    monkeypatch.setattr(app, "ALLOW_PRIVATE_URLS", False)
    _with_resolver(monkeypatch, {"localtest.me": ["127.0.0.1"],
                                 "example.com": ["93.184.216.34"]})

    async def run(url):
        request = httpx.Request("GET", url)
        await app._validate_outbound_request(request)

    asyncio.run(run("https://example.com/a"))          # no exception
    with pytest.raises(httpx.RequestError, match="resolves inside"):
        asyncio.run(run("http://localtest.me:8060/health"))


def test_a_resolved_verdict_is_remembered_briefly(monkeypatch):
    """An image-heavy page must not resolve one host a hundred times."""
    import asyncio
    import socket as _socket

    calls = []

    async def fake(host, port, *args, **kwargs):
        calls.append(host)
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "",
                 ("93.184.216.34", port or 80))]

    class Loop:
        getaddrinfo = staticmethod(fake)

    monkeypatch.setattr(app.asyncio, "get_running_loop", lambda: Loop())
    app._resolved.clear()

    async def run():
        for _ in range(5):
            assert await app._host_resolves_public("example.com", 443) is True

    asyncio.run(run())
    assert calls == ["example.com"], calls
    app._resolved.clear()


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


def test_frontmatter_is_read_to_its_delimiter(tmp_path):
    note = tmp_path / "long.md"
    title = "A" * 3000
    note.write_text(f'---\ntitle: "{title}"\nsource_url: "https://example.test/x"\n---\nbody')
    assert app._read_frontmatter_field(note, "source_url") == "https://example.test/x"


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
    r = client.get("/?token=s3cret", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert r.history and r.history[0].status_code == 303
    assert "token=" not in str(r.url)
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


def test_validation_errors_do_not_echo_unencodable_input(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    client = TestClient(app.app)
    raw = b'{"url":"https://example.com/\\ud800","formats":["md"]}'
    answer = client.post("/save", content=raw,
                         headers={"content-type": "application/json"})
    assert answer.status_code == 422
    answer.content.decode("utf-8")
    assert client.get("/health").status_code == 200


def test_echo_redacts_credentials(monkeypatch):
    monkeypatch.setattr(app, "MARGIN_TOKEN", "s3cret")
    client = TestClient(app.app)
    client.cookies.set("margin_token", "s3cret")
    answer = client.post(
        "/echo", headers={"Authorization": "Bearer s3cret"},
        content=b"hello",
    ).json()
    assert answer["headers"]["authorization"] == "[redacted]"
    assert answer["headers"]["cookie"] == "[redacted]"


def test_health_open_and_reports_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "MARGIN_TOKEN", "s3cret")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    with TestClient(app.app) as client:  # lifespan: /health touches app.state
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["auth_required"] is True


def test_pages_and_files_carry_the_sibling_security_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-a.md").write_text("body", encoding="utf-8")
    client = TestClient(app.app)
    for url in ("/", "/read/2026-07-19-a.md", "/files/2026-07-19-a.md",
                "/service-worker.js"):
        headers = client.get(url).headers
        assert headers["x-content-type-options"] == "nosniff", url
        assert headers["referrer-policy"] == "no-referrer", url
        csp = headers["content-security-policy"]
        assert "form-action 'self'" in csp and "base-uri 'none'" in csp, url
    assert client.get("/").headers["cache-control"] == "no-cache"


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def test_delete_removes_files_and_index_entry(tmp_path, monkeypatch):
    """An item lives in one folder — archiving moves its whole family — so
    deletion happens in one folder too."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "2026-07-19-z.md").write_text("x", encoding="utf-8")
    (tmp_path / "archive" / "2026-07-19-z.pdf").write_bytes(b"%PDF")
    app._record_url("https://a.test/z", "2026-07-19-z")

    client = TestClient(app.app)
    r = client.post("/delete", data={"stem": "2026-07-19-z"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert not (tmp_path / "archive" / "2026-07-19-z.md").exists()
    assert not (tmp_path / "archive" / "2026-07-19-z.pdf").exists()
    assert app._find_existing("https://a.test/z") is None
    assert app._load_url_index() == {}          # index entry cleaned up

    assert client.post("/delete", data={"stem": "../evil"}).status_code == 400
    assert client.post("/delete", data={"stem": "2026-07-19-z"}).status_code == 404


def test_deleting_one_item_leaves_its_namesake_alone(tmp_path, monkeypatch):
    """The inbox and the archive allocate names independently, so both can
    hold a *different* item under one stem — two pages with the same title
    saved on the same day. Deleting "the stem wherever it lives" took the
    unrelated inbox item with it, which is what the archive-only,
    confirm-prompted UI exists to prevent."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "archive").mkdir()
    (tmp_path / "2026-07-19-same.md").write_text(
        '---\ntitle: "The inbox one"\n---\nkeep me\n', encoding="utf-8")
    (tmp_path / "archive" / "2026-07-19-same.md").write_text(
        '---\ntitle: "The archived one"\n---\ndelete me\n', encoding="utf-8")
    app._record_url("https://a.test/inbox", "2026-07-19-same")

    client = TestClient(app.app)
    assert client.post("/delete",
                       data={"stem": "2026-07-19-same", "view": "archive"},
                       follow_redirects=False).status_code == 303
    assert (tmp_path / "2026-07-19-same.md").is_file()          # untouched
    assert not (tmp_path / "archive" / "2026-07-19-same.md").exists()
    # And the surviving item keeps its place in the duplicate index.
    assert app._load_url_index() == {"https://a.test/inbox": ["2026-07-19-same"]}

    # Asked for the inbox one, it goes.
    assert client.post("/delete",
                       data={"stem": "2026-07-19-same", "view": "inbox"},
                       follow_redirects=False).status_code == 303
    assert not (tmp_path / "2026-07-19-same.md").exists()
    assert app._load_url_index() == {}


def test_an_archived_family_that_collides_takes_its_index_with_it(tmp_path,
                                                                  monkeypatch):
    """Archiving onto an occupied name lands the family on "<stem>-2". The
    index still said "<stem>", which by then is a different item's files —
    and a PDF-only capture has no front matter to fall back on, so the wrong
    document would answer for that URL for good."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "archive").mkdir()
    (tmp_path / "2026-07-19-same.pdf").write_bytes(b"%PDF mine")
    (tmp_path / "archive" / "2026-07-19-same.pdf").write_bytes(b"%PDF someone else")
    app._record_url("https://a.test/mine", "2026-07-19-same")

    client = TestClient(app.app)
    assert client.post("/archive",
                       data={"stem": "2026-07-19-same", "action": "archive"},
                       follow_redirects=False).status_code == 303
    assert (tmp_path / "archive" / "2026-07-19-same-2.pdf").is_file()
    assert app._load_url_index() == {"https://a.test/mine": ["2026-07-19-same-2"]}
    hit = app._find_existing("https://a.test/mine")
    assert hit and hit["files"] == ["2026-07-19-same-2.pdf"]


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
    """Read-only GETs stay open; the bookmarklet has its narrower check."""
    answer = TestClient(app.app).get(
        "/health", headers={"Sec-Fetch-Site": "cross-site"})
    assert answer.status_code == 200


def test_cross_site_save_page_must_be_a_real_bookmarklet_navigation(monkeypatch):
    """The GET endpoint writes; an image/background request must not save."""
    called = False

    async def fake_save(payload, request):
        nonlocal called
        called = True
        return {"status": "ok", "title": "T", "files": ["t.pdf"]}

    monkeypatch.setattr(app, "save", fake_save)
    client = TestClient(app.app)
    hostile = client.get(
        "/save-page?url=https://example.com",
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors",
                 "Sec-Fetch-Dest": "image"},
    )
    assert hostile.status_code == 403
    assert called is False

    bookmarklet = client.get(
        "/save-page?url=https://example.com",
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                 "Sec-Fetch-Dest": "document", "Accept": "text/html"},
    )
    assert bookmarklet.status_code == 200
    assert "Saved" in bookmarklet.text and "T" in bookmarklet.text
    assert called is True


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


def test_a_link_out_of_the_folder_is_not_read_for_the_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    secret = tmp_path.parent / "private-frontmatter.md"
    secret.write_text('---\ntitle: "SECRET CONTENT"\n---\n', encoding="utf-8")
    (tmp_path / "2026-07-19-linked.md").symlink_to(secret)
    page = TestClient(app.app).get("/").text
    assert "SECRET CONTENT" not in page


def test_an_archive_symlink_cannot_read_or_delete_outside_the_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    elsewhere = tmp_path.parent / "not-an-archive"
    elsewhere.mkdir()
    victim = elsewhere / "2026-07-19-victim.md"
    victim.write_text('---\ntitle: "Outside"\n---\nbody', encoding="utf-8")
    (tmp_path / "archive").symlink_to(elsewhere, target_is_directory=True)
    client = TestClient(app.app)
    assert "Outside" not in client.get("/?view=archive").text
    # 409, not 404: "your archive path is a symlink" is the useful answer,
    # and it is given before anything is looked at, let alone unlinked.
    assert client.post("/delete", data={"stem": victim.stem}).status_code == 409
    assert victim.is_file()


def test_queue_links_quote_real_filename_characters(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-why?.md").write_text("body", encoding="utf-8")
    page = TestClient(app.app).get("/").text
    assert "/read/2026-07-19-why%3F.md" in page


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
    assert "unusable" in capsys.readouterr().err      # _log writes to stderr
    assert not (tmp_path / ".saved-urls.json").exists()
    assert len(list(tmp_path.glob(".saved-urls.corrupt-*.json"))) == 1


def test_malformed_index_records_do_not_break_the_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / ".saved-urls.json").write_text(
        '{"https://good.test": ["2026-07-19-good", 5], '
        '"https://bad.test": 5}', encoding="utf-8")
    assert app._load_url_index() == {
        "https://good.test": ["2026-07-19-good"]
    }
    assert TestClient(app.app).get("/").status_code == 200


def test_a_non_object_index_is_quarantined(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / ".saved-urls.json").write_text("[]", encoding="utf-8")
    assert app._load_url_index() == {}
    assert not (tmp_path / ".saved-urls.json").exists()
    assert len(list(tmp_path.glob(".saved-urls.corrupt-*.json"))) == 1


def test_the_url_index_cannot_be_a_link_to_another_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    outside = tmp_path.parent / "outside-index.json"
    outside.write_text('{"https://secret.test": ["2026-07-19-secret"]}')
    (tmp_path / ".saved-urls.json").symlink_to(outside)
    assert app._load_url_index() == {}


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
               "http://127.0.0.1./x", "http://localhost./x",
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


def test_a_compressed_pdf_cannot_outgrow_its_cap(monkeypatch):
    """httpx's aiter_bytes hands over what the decoder produced, so a cap
    counted there bounds bytes that already exist — and how many belongs to
    whoever compressed them. Measured against 300 MB of zeros in 299 kB of
    gzip: a 10 MB cap peaked at 142.9 MiB reading it the old way, and 21.2
    MiB reading the wire raw and expanding under a bound."""
    import asyncio
    import gzip
    import httpx

    cap = 1 * 1024 * 1024
    monkeypatch.setattr(app, "MAX_DOWNLOAD_BYTES", cap)
    body = gzip.compress(b"%PDF-1.4\n" + b"0" * (40 * cap))

    async def run():
        async def serve(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n"
                         b"Content-Encoding: gzip\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                         % len(body))
            writer.write(body)
            try:
                await writer.drain()
            except Exception:                                  # noqa: BLE001
                return

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                return await app._fetch_pdf_bytes(
                    client, f"http://127.0.0.1:{port}/x.pdf")
            finally:
                server.close()

    import gc
    import tracemalloc

    gc.collect()
    tracemalloc.start()
    with pytest.raises(RuntimeError, match="exceeds"):
        asyncio.run(run())
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    # The assertion that matters is the *size* of the refusal, not the
    # refusal: reading the decoded stream also raises, having already built
    # the megabytes. Bounded, the peak follows the cap; unbounded, it follows
    # whoever did the compressing.
    assert peak < 6 * cap, f"{peak / 2 ** 20:.1f} MiB against a {cap} byte cap"

    # The wire is read raw, and anything encoded is expanded with a limit —
    # not handed to a decoder that decides how much to produce.
    source = Path(app.__file__).read_text()
    fetch = source[source.index("async def _fetch_pdf_bytes("):]
    fetch = fetch[:fetch.index("\n\n\n")]
    assert "aiter_raw()" in fetch and "aiter_bytes()" not in fetch
    assert "_inflate(" in fetch


def test_an_uncompressed_pdf_still_downloads(monkeypatch):
    import asyncio
    import httpx

    monkeypatch.setattr(app, "MAX_DOWNLOAD_BYTES", 8 * 1024 * 1024)
    body = b"%PDF-1.4\n" + b"0" * (64 * 1024)

    async def run():
        async def serve(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                         % len(body))
            writer.write(body)
            try:
                await writer.drain()
            except Exception:                                  # noqa: BLE001
                return

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                return await app._fetch_pdf_bytes(
                    client, f"http://127.0.0.1:{port}/x.pdf")
            finally:
                server.close()

    assert asyncio.run(run()) == body


def test_a_gzipped_pdf_within_the_cap_still_arrives(monkeypatch):
    """Refusing every coding would send a real PDF URL down the render path,
    which produces a picture of a PDF viewer. So they are expanded, bounded."""
    import asyncio
    import gzip
    import httpx

    monkeypatch.setattr(app, "MAX_DOWNLOAD_BYTES", 8 * 1024 * 1024)
    plain = b"%PDF-1.4\n" + b"0" * (64 * 1024)
    body = gzip.compress(plain)

    async def run():
        async def serve(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n"
                         b"Content-Encoding: gzip\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                         % len(body))
            writer.write(body)
            try:
                await writer.drain()
            except Exception:                                  # noqa: BLE001
                return

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                return await app._fetch_pdf_bytes(
                    client, f"http://127.0.0.1:{port}/x.pdf")
            finally:
                server.close()

    assert asyncio.run(run()) == plain


def test_a_save_does_not_freeze_the_server(tmp_path, monkeypatch):
    """Pandoc is subprocess.run(timeout=30) and Margin asks for .tex and
    .org, so writing a save used to hold the event loop for as long as it
    took — no /health, no queue, no second save. Measured before: a 3.0s
    write let the loop run 4 ticks where it should have run about 64."""
    import asyncio
    import time as _time

    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "_run_pandoc",
                        lambda args, label: (_time.sleep(0.6), (-1, "", "stub"))[1])

    async def run():
        ticks = 0
        stop = asyncio.Event()

        async def heartbeat():
            nonlocal ticks
            while not stop.is_set():
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        started = _time.monotonic()
        # Exactly how the handlers call it.
        await asyncio.to_thread(app._write_all_formats, "2026-07-19-x.md",
                                "# Body\n\ntext\n", "X", ("md", "tex", "org"))
        took = _time.monotonic() - started
        stop.set()
        await beat
        return ticks, took

    ticks, took = asyncio.run(run())
    assert took > 0.5, took                  # the stub really did block
    # Most of the ticks it could have had, not a handful.
    assert ticks > took / 0.02 * 0.6, (ticks, took)

    # And the handlers do call it that way.
    source = Path(app.__file__).read_text()
    assert source.count("asyncio.to_thread(\n                _write_all_formats") \
        + source.count("asyncio.to_thread(\n            _write_all_formats") \
        + source.count("asyncio.to_thread(\n                    _write_all_formats") == 3
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("written = _write_all_formats("):
            raise AssertionError("a handler still writes on the event loop")


def test_two_saves_cannot_claim_the_same_stem(tmp_path, monkeypatch):
    """Choosing a stem looks at what exists and writing creates it. Once the
    writing moved off the event loop those two steps could interleave."""
    import threading

    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "_run_pandoc", lambda args, label: (-1, "", ""))
    results = []

    def save(n):
        results.append(app._write_all_formats(
            "2026-07-19-same.md", f"# Body {n}\n", "Same", ("md",)))

    threads = [threading.Thread(target=save, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    written = [p for group in results for p in group]
    assert len(written) == 6
    assert len({p.name for p in written}) == 6, [p.name for p in written]
    assert all(p.is_file() for p in written)


# ---------------------------------------------------------------------------
# Deciding whether a page is maths, without asking
# ---------------------------------------------------------------------------

_ARTICLE = """<html><head><title>The alpha release</title></head><body><article>
<p>The \u03b1-version shipped in March, and the \u03b2 followed in May. Costs rose
\u224815% year over year \u2192 margins fell. The \u03a3 of small decisions is a culture,
and the \u03c0-day tradition started as a joke. Temperatures above 30\u00b0C are now
normal; \u0394 from the 1990s is stark. He wrote in \u03a9 magazine about the \u221e
possibilities of the format, which is a long paragraph so that the extractor
treats this as an article body rather than boilerplate to be discarded.</p>
<p>A second paragraph, also of reasonable length, discussing the \u22642\u00d7 spread
in prices and the way teams talk about \u03b1 testing without meaning anything
mathematical by it at all. This should read exactly as written.</p>
</article></body></html>"""

_MATHS = """<html><head><title>On zeta</title></head><body><article>
<p>Let <span class="katex"><span class="katex-mathml"><math><semantics>
<annotation encoding="application/x-tex">\\alpha</annotation></semantics></math>
</span></span> be a root. Then the sum over \u03b1 and \u03b2 converges, and we write
\u2248 for asymptotic equality throughout this fairly long paragraph so that the
extractor keeps it as the article body rather than discarding it as chrome.</p>
<p>A second paragraph of similar length, in which \u221e appears and the \u2264 sign is
used in the ordinary mathematical way, so the Unicode pass has something to
do here and is right to do it.</p>
</article></body></html>"""


def test_prose_is_not_turned_into_latex():
    """The Unicode pass wraps isolated Greek letters and symbols in $…$,
    which is right where \u03b1 is a variable and wrong everywhere else. Measured
    on eight ordinary sentences before it was gated, seven came back
    rewritten — "The $\\alpha$-version shipped in March", "Costs rose
    $\\approx$15%" — and a general read-later queue is mostly those."""
    _, body = app._extract_url_content(_ARTICLE, "https://example.test/x")
    assert body.strip(), "the fixture must extract as an article"
    assert "$" not in body, body[:200]
    assert "\u03b1-version" in body and "\u224815%" in body      # left exactly as written


def test_a_page_that_ships_math_still_gets_it():
    """The page's own markup is the signal: prose does not carry KaTeX."""
    _, body = app._extract_url_content(_MATHS, "https://example.test/x")
    assert "$\\alpha$" in body                      # the structural one
    assert "$\\beta$" in body and "$\\approx$" in body   # and the Unicode pass ran


def test_the_math_count_is_what_decides():
    """Counted, not guessed — and every strategy replaces through one place,
    so one added later cannot forget to be counted."""
    from bs4 import BeautifulSoup
    plain = BeautifulSoup("<p>An \u03b1-version and a \u03b2 test.</p>", "html.parser")
    assert app._replace_math_elements(plain) == 0
    katex = BeautifulSoup(
        '<p><span class="katex"><span class="katex-mathml"><math><semantics>'
        '<annotation encoding="application/x-tex">x^2</annotation>'
        "</semantics></math></span></span></p>", "html.parser")
    assert app._replace_math_elements(katex) == 1
    source = Path(app.__file__).read_text()
    body = source[source.index("def _replace_math_elements("):]
    body = body[:body.index("\n\n\ndef ")]
    # One call, inside the helper that counts.
    assert body.count("replace_with(_math_replacement") == 1
    assert body.count("swap(") == 9                  # the def plus eight uses


def test_the_reader_loads_mathjax_only_where_there_is_math(tmp_path, monkeypatch):
    """A megabyte of JavaScript from a CDN is a strange thing to fetch for an
    article about tooling, and most of a read-later queue is that."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-prose.md").write_text(
        '---\ntitle: "Prose"\n---\n\nNo formulas here, only words.\n',
        encoding="utf-8")
    (tmp_path / "2026-07-19-maths.md").write_text(
        '---\ntitle: "Maths"\n---\n\nAll zeros have real part $1/2$.\n',
        encoding="utf-8")
    client = TestClient(app.app)
    prose = client.get("/read/2026-07-19-prose.md").text
    maths = client.get("/read/2026-07-19-maths.md").text
    assert "mathjax" not in prose.lower(), "MathJax fetched for a page with no math"
    assert "mathjax" in maths.lower()


# ---------------------------------------------------------------------------
# Code, which competes with maths for the same characters
# ---------------------------------------------------------------------------

_CODE_ARTICLE = """<html><head><title>Shell tricks</title></head><body><article>
<p>A short intro paragraph that is long enough for the extractor to keep the
article body rather than treating the whole thing as boilerplate chrome, and
which mentions nothing mathematical at all.</p>
<pre><code>export PATH="$HOME/bin:$PATH"
awk '{print $1, $3}' access.log | sort | uniq -c
for f in *.md; do mv "$f" "${f%.md}.markdown"; done
</code></pre>
<p>Inline you would write <code>$PATH</code> or <code>a_b_c</code> and expect
them to survive, and this paragraph is padded so the extractor keeps it too.</p>
</article></body></html>"""


def test_code_survives_extraction_unchanged():
    """A general read-later queue is full of shell and C++, and both are made
    of the characters maths is made of."""
    _, body = app._extract_url_content(_CODE_ARTICLE, "https://example.test/x")
    for snippet in ('export PATH="$HOME/bin:$PATH"', "awk '{print $1, $3}'",
                    '"${f%.md}.markdown"', "`$PATH`", "`a_b_c`"):
        assert snippet in body, snippet
    assert "\\alpha" not in body and "$\\" not in body


def test_code_survives_the_reader_unchanged():
    """The reader stashes math spans before Markdown conversion, and
    "$HOME/bin:$PATH" matches the inline-math pattern exactly."""
    md = ('# Shell\n\n```\nexport PATH="$HOME/bin:$PATH"\n'
          "awk '{print $1, $3}' access.log\n```\n\n"
          "Inline `$PATH` and `a_b_c` in prose.\n\n"
          "And real maths: $E = mc^2$.\n")
    out = app._render_markdown(md)
    assert "$HOME/bin:$PATH" in out
    assert "{print $1, $3}" in out
    assert "<code>$PATH</code>" in out
    assert "$E = mc^2$" in out            # left for MathJax, which skips code


def test_shell_variables_are_not_mistaken_for_maths():
    """MathJax skips <pre> and <code> — verified in a browser — so a page
    whose only dollars are in code has nothing to typeset. Counting them
    tagged an article about shell scripts as maths and fetched a megabyte of
    JavaScript to do nothing with."""
    shell = ('# Shell\n\n```\nexport PATH="$HOME/bin:$PATH"\n'
             "awk '{print $1, $3}' log\n```\n\nInline `$PATH` in prose.\n")
    assert app._has_math_outside_code(shell) is False
    assert app._has_math_outside_code("# T\n\n~~~\ncost=$1\n~~~\n") is False
    assert app._has_math_outside_code("Just words, no dollars.") is False
    # Two inline spans in one sentence: the text between them carries no
    # dollar and no newline, so the inline-math pattern matches straight
    # across them unless the spans are taken out first.
    assert app._has_math_outside_code(
        "Use `$HOME` and `$PATH` together in the profile.") is False
    # Real maths still counts, including alongside code.
    assert app._has_math_outside_code("Zeros at $1/2$.") is True
    assert app._has_math_outside_code("$$\n\\zeta(s)\n$$") is True
    assert app._has_math_outside_code(
        '$E = mc^2$ and\n\n```\necho "$HOME"\n```\n') is True


def test_a_shell_article_is_not_tagged_as_maths(tmp_path, monkeypatch):
    """Both the front-matter tag and the reader ask the same question."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "2026-07-19-shell.md").write_text(
        '---\ntitle: "Shell"\n---\n\n```\nexport PATH="$HOME/bin:$PATH"\n```\n',
        encoding="utf-8")
    (tmp_path / "2026-07-19-maths.md").write_text(
        '---\ntitle: "Maths"\n---\n\nZeros at $1/2$.\n', encoding="utf-8")
    client = TestClient(app.app)
    assert "mathjax" not in client.get("/read/2026-07-19-shell.md").text.lower()
    assert "mathjax" in client.get("/read/2026-07-19-maths.md").text.lower()

    front = app._frontmatter("Shell", "https://example.test/x",
                             has_math=app._has_math_outside_code(
                                 '```\nexport PATH="$HOME"\n```\n'))
    assert "math" not in front.split("tags:")[1].split("\n")[0]


def test_the_cookie_follows_the_browsers_scheme_not_ours(monkeypatch, tmp_path):
    """Behind `tailscale serve` the app sees plain HTTP on loopback while the
    browser is on https://…ts.net, so the scheme we see is exactly the one
    that cannot answer the question. The proxy leaves a header that can."""
    monkeypatch.setattr(app, "MARGIN_TOKEN", "s3cret")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    client = TestClient(app.app)

    plain = client.get("/?token=s3cret", follow_redirects=False)
    assert "margin_token=" in plain.headers.get("set-cookie", "")
    assert "Secure" not in plain.headers["set-cookie"]      # a plain-HTTP LAN

    behind = client.get("/?token=s3cret", follow_redirects=False,
                        headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in behind.headers["set-cookie"]

    # A chain of proxies leaves a list; the client-facing one is first.
    chained = client.get("/?token=s3cret", follow_redirects=False,
                         headers={"X-Forwarded-Proto": "https, http"})
    assert "Secure" in chained.headers["set-cookie"]
    # And nothing is invented from a header that does not say https.
    spoofed = client.get("/?token=s3cret", follow_redirects=False,
                         headers={"X-Forwarded-Proto": "gopher"})
    assert "Secure" not in spoofed.headers["set-cookie"]


# ---------------------------------------------------------------------------
# A review round's findings, each reproduced before it was fixed
# ---------------------------------------------------------------------------

def test_an_invited_origin_may_post(tmp_path, monkeypatch):
    """It gets a successful preflight and then a 403 on the real request:
    the cross-site guard never consulted the list of origins you allowed, so
    the documented browser-extension case could not work."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "MARGIN_CORS_ORIGINS", ["https://ext.test"])
    with TestClient(app.app) as client:
        invited = client.post(
            "/save-url", json={"url": "http://localtest.me/x"},
            headers={"Origin": "https://ext.test", "Sec-Fetch-Site": "cross-site"})
        assert invited.status_code == 200          # refused later, on its merits
        uninvited = client.post(
            "/save-url", json={"url": "http://localtest.me/x"},
            headers={"Origin": "https://evil.test", "Sec-Fetch-Site": "cross-site"})
        assert uninvited.status_code == 403


def test_frontmatter_stops_at_the_closing_delimiter(tmp_path, monkeypatch):
    """Reading stopped at the chunk holding the closing "---" and then
    searched the whole chunk, so a source_url: line in the body was read as
    front matter — and the body is text from somebody else's website."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    path = tmp_path / "2026-07-19-x.md"
    path.write_text('---\ntitle: "Real"\n---\n\nBody.\n'
                    'source_url: "https://evil.test/injected"\n', encoding="utf-8")
    assert app._read_frontmatter_field(path, "title") == "Real"
    assert app._read_frontmatter_field(path, "source_url") is None
    # A real one is still read.
    path.write_text('---\ntitle: "Real"\nsource_url: "https://ok.test/a"\n---\nBody\n',
                    encoding="utf-8")
    assert app._read_frontmatter_field(path, "source_url") == "https://ok.test/a"


def test_a_hand_added_unicode_file_can_be_archived(tmp_path, monkeypatch):
    """The stem rule was ASCII-only, so "Über den Rand.md" was listed in the
    queue and then answered 400 to both Archive and Delete — in a folder the
    README invites you to drop files into by hand. What makes a stem safe is
    that it cannot leave the folder, not that it is ASCII."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "archive").mkdir()
    (tmp_path / "Über den Rand.md").write_text(
        '---\ntitle: "Über den Rand"\n---\nbody\n', encoding="utf-8")
    client = TestClient(app.app)
    assert [i["stem"] for i in app._list_items(tmp_path)] == ["Über den Rand"]
    assert client.post("/archive",
                       data={"stem": "Über den Rand", "action": "archive"},
                       follow_redirects=False).status_code == 303
    assert (tmp_path / "archive" / "Über den Rand.md").is_file()
    # And nothing that could leave the folder is accepted.
    for bad in ("../evil", "a/b", "", ".", "..", ".hidden", "with\x00null"):
        assert not app._safe_stem(bad), bad
    for good in ("Über den Rand", "2026-07-19-x", "a b.c"):
        assert app._safe_stem(good), good


def test_health_wants_a_directory(tmp_path, monkeypatch):
    """Pointed at a regular file it reported ok, exists and writable while
    every save failed."""
    target = tmp_path / "not-a-dir"
    target.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(app, "OUTPUT_DIR", target)
    with TestClient(app.app) as client:
        body = client.get("/health").json()
    assert body["output_dir_exists"] is False
    assert body["output_dir_writable"] is False


def test_a_binary_write_does_not_freeze_the_server(tmp_path, monkeypatch):
    """A PDF is megabytes, and the write is followed by fsync, an atomic
    replace and a lock. On the loop, a 400ms write stopped an async
    heartbeat entirely."""
    import asyncio
    import time as _time

    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    real = app._write_bytes_atomically
    monkeypatch.setattr(app, "_write_bytes_atomically",
                        lambda path, data: (_time.sleep(0.4), real(path, data))[1])

    async def run():
        ticks = 0
        stop = asyncio.Event()

        async def beat():
            nonlocal ticks
            while not stop.is_set():
                await asyncio.sleep(0.02)
                ticks += 1

        heart = asyncio.create_task(beat())
        await asyncio.sleep(0.05)
        started = _time.monotonic()
        await app._write_binary_async("2026-07-19-x.pdf", b"%PDF" * 500)
        took = _time.monotonic() - started
        stop.set()
        await heart
        return ticks, took

    ticks, took = asyncio.run(run())
    assert took > 0.3, took
    assert ticks > took / 0.02 * 0.6, (ticks, took)
    source = Path(app.__file__).read_text()
    for line in source.splitlines():
        assert not line.strip().startswith("pdf_path = _write_binary("), line


def test_a_failed_pdf_write_is_a_json_error(tmp_path, monkeypatch):
    """Outside the branch's error handling, a full disk or a read-only
    folder left the direct-PDF path answering HTTP 500 instead of the JSON
    error result the API documents."""
    source = Path(app.__file__).read_text()
    branch = source[source.index("if pdf_bytes is not None:"):]
    branch = branch[:branch.index("if md_formats")]
    assert "except OSError" in branch
    assert "_err(" in branch


def test_cancelling_the_delete_prompt_keeps_the_offline_copy():
    """confirm() in an onsubmit attribute cancelled the navigation while a
    separate submit listener still sent forget-stem: the server kept the
    item and its offline copy vanished. One listener owns both halves."""
    source = Path(app.__file__).read_text()
    assert "onsubmit=" not in source
    assert 'data-confirm="Delete permanently' in source
    listener = source[source.index("form[action=\"/delete\"]"):]
    listener = listener[:listener.index("});\n});") + 8]
    assert "window.confirm(form.dataset.confirm)" in listener
    assert "event.preventDefault()" in listener
    assert "forget-stem" in listener
    # …and the form says which folder it is deleting from.
    assert 'name="view"' in source
