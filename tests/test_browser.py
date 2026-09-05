# Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Browser tests: the behaviour that only exists in a browser.

The rest of the suite reads the page templates as text, which cannot tell
whether Copy works where the Clipboard API does not exist, whether Download
navigates away, or whether Archive actually moves the card. Each of those has
been a real bug in this family of apps.

These run a real server on a loopback port and drive Chromium against it, so
they are slower than the rest and need a browser:

    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/playwright install chromium
    .venv/bin/python -m pytest tests/test_browser.py

Without the browser they skip rather than fail — the offline suite is what
gates a commit, and a missing tool is not a broken Margin.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STEM = "2026-09-04-riemann-hypothesis"
TITLE = "The Riemann hypothesis, explained"

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed").sync_playwright


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _fixture_queue(root: Path):
    """Two saved items and one archived one, as a save would have left them."""
    (root / "archive").mkdir(parents=True)
    (root / f"{STEM}.md").write_text(
        f'---\ntitle: "{TITLE}"\nsource_url: "https://example.test/riemann"\n'
        "---\n\n# " + TITLE + "\n\nAll non-trivial zeros have real part "
        "$1/2$, and that is the whole story.\n", encoding="utf-8")
    (root / f"{STEM}.pdf").write_bytes(b"%PDF-1.4\n%stub\n")
    (root / "2026-09-03-a-note-on-sheaves.md").write_text(
        '---\ntitle: "A note on sheaves"\n'
        'source_url: "javascript:alert(1)"\n---\n\nSections glue.\n',
        encoding="utf-8")
    (root / "archive" / "2026-08-30-older-piece.md").write_text(
        '---\ntitle: "An older piece"\n---\n\nBody.\n', encoding="utf-8")


# Function scope, not module: these tests press the app's real controls, and
# Archive and Delete move files. A server shared across the file would make
# one test's archiving permanent for every test after it.
@pytest.fixture
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("queue")
    _fixture_queue(root)
    port = _free_port()
    env = dict(os.environ, OUTPUT_DIR=str(root), HOST="127.0.0.1",
               PORT=str(port))
    env.pop("MARGIN_TOKEN", None)            # no auth: fewer moving parts
    process = subprocess.Popen([sys.executable, "app.py"], cwd=REPO, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for(base, process)
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


def _wait_for(base, process):
    import urllib.request
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(process.stdout.read().decode("utf-8", "replace"))
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1) as answer:
                if answer.status == 200:
                    return
        except Exception:                                  # noqa: BLE001
            time.sleep(0.1)
    raise RuntimeError("the server did not come up")


@pytest.fixture(scope="module")
def browser():
    # A missing browser is a missing tool and skips; anything else is a
    # failure. Catching every launch error would turn a sandbox problem, a
    # crashing binary or a CI regression into a screenful of green skips —
    # all the behavioural coverage there is, quietly gone.
    with sync_playwright() as play:
        if not Path(play.chromium.executable_path).exists():
            pytest.skip("chromium is not installed: playwright install chromium")
        engine = play.chromium.launch()
        yield engine
        engine.close()


@pytest.fixture
def page(browser, server):
    context = browser.new_context(viewport={"width": 375, "height": 812})
    sheet = context.new_page()
    yield sheet
    context.close()


def _queue(sheet, base):
    sheet.goto(base)
    sheet.wait_for_selector(".item")


def _poll(sheet, script, arg, want, seconds=10):
    """page.evaluate in a loop, because wait_for_function does not await.

    Playwright checks the predicate's return value for truthiness without
    awaiting it, so an async predicate hands it a Promise — always truthy,
    always an instant pass.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if sheet.evaluate(script, arg) == want:
            return True
        sheet.wait_for_timeout(100)
    return False


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

def test_the_queue_reads_as_margins_own_page(page, server):
    """Paper, ink and a serif wordmark — the same family as its sibling."""
    _queue(page, server)
    look = page.evaluate("""() => {
      const seen = (el, prop) => getComputedStyle(el)[prop];
      return {
        paper: seen(document.body, 'backgroundColor'),
        heading: seen(document.querySelector('h1'), 'fontFamily'),
        title: seen(document.querySelector('.item .title'), 'fontFamily'),
        icon: !!document.querySelector('header.masthead img'),
      };
    }""")
    assert look["paper"] == "rgb(245, 238, 220)"          # --paper
    assert "Iowan Old Style" in look["heading"]
    assert "Iowan Old Style" in look["title"]
    assert look["icon"]


def test_the_header_icon_lines_up_with_the_wordmark(page, server):
    """Centred against the whole block, an icon taller than the heading sits
    beside the gap under it and lines up with neither line."""
    _queue(page, server)
    boxes = page.evaluate("""() => {
      const box = (sel) => {
        const r = document.querySelector(sel).getBoundingClientRect();
        return {top: r.top, height: r.height};
      };
      return {icon: box('header.masthead img'), title: box('header.masthead h1')};
    }""")
    assert abs(boxes["icon"]["top"] - boxes["title"]["top"]) <= 2, boxes
    assert abs(boxes["icon"]["height"] - boxes["title"]["height"]) <= 2, boxes


def test_a_date_is_shown_the_way_the_reader_writes_them(page, server):
    """The ISO stamp is what the markup carries, so it is still right with
    no script; what is displayed is the reader's own order."""
    _queue(page, server)
    stamps = page.eval_on_selector_all(
        ".item time", "els => els.map((e) => "
        "({shown: e.textContent, iso: e.getAttribute('datetime')}))")
    assert stamps
    for stamp in stamps:
        assert stamp["iso"].startswith("2026-")
        assert stamp["shown"] != stamp["iso"], stamp
        assert any(c.isalpha() for c in stamp["shown"]), stamp


def test_a_source_link_is_checked_and_opens_off_site(page, server):
    """Front matter comes out of a folder people and sync clients write to."""
    _queue(page, server)
    links = page.eval_on_selector_all(
        ".item a", "els => els.map((a) => "
        "({href: a.getAttribute('href'), target: a.target, rel: a.rel}))")
    assert not [a for a in links if (a["href"] or "").startswith("javascript:")]
    off_site = [a for a in links if (a["href"] or "").startswith("https://")]
    assert off_site, links
    for link in off_site:
        assert link["target"] == "_blank" and "noopener" in link["rel"]


def test_the_filter_hides_what_it_does_not_match(page, server):
    _queue(page, server)
    assert page.locator(".item:visible").count() == 2
    page.fill("#filter", "sheaves")
    page.wait_for_timeout(150)
    assert page.locator(".item:visible").count() == 1
    page.fill("#filter", "")
    page.wait_for_timeout(150)
    assert page.locator(".item:visible").count() == 2


def test_archiving_moves_the_card_out_of_the_inbox(page, server):
    """Driven through the real control, because the button is what a person
    presses."""
    _queue(page, server)
    before = page.locator(".item").count()
    page.get_by_role("button", name="archive").first.click()
    page.wait_for_selector(".item")
    assert page.locator(".item").count() == before - 1
    page.goto(f"{server}/?view=archive")
    page.wait_for_selector(".item")
    assert page.locator(".item").count() == 2          # the older one, plus it


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------

def test_the_reader_copies_where_the_clipboard_api_does_not_exist(page, server):
    """navigator.clipboard is a secure-context feature and Margin is normally
    reached over plain HTTP on a home network, so Copy used to be hidden
    there — the feature simply did not exist where it was needed.

    The API is removed by an init script, before the page's own script runs.
    Deleting it afterwards proves nothing: the tests reach the server on
    127.0.0.1, which *is* a secure context, so the button had already been
    shown and the first version of this test passed with the bug put back.
    """
    page.add_init_script("""
      Object.defineProperty(navigator, 'clipboard',
                            {value: undefined, configurable: true});
      window.__asked = null;
      document.execCommand = (what) => { window.__asked = what; return true; };
    """)
    page.goto(f"{server}/read/{STEM}.md")
    page.wait_for_selector(".reading")
    assert page.locator("#copy").is_visible()      # offered, not hidden away
    page.click("#copy")
    assert _poll(page, "() => window.__asked", None, "copy")
    assert page.locator("#copy").inner_text() == "Copied"


def test_the_reader_says_when_an_action_fails(page, server):
    """These pages have no flash area, and an empty catch is how Copy,
    Share and Download come to be indistinguishable from a button that does
    nothing."""
    page.goto(f"{server}/read/{STEM}.md")
    page.wait_for_selector(".reading")
    page.evaluate("""() => {
      Object.defineProperty(navigator, 'clipboard',
                            {value: undefined, configurable: true});
      document.execCommand = () => false;
    }""")
    page.click("#copy")
    note = page.wait_for_selector("#note:not([hidden])")
    assert "Could not copy text" in note.inner_text()


def test_downloading_never_leaves_the_reader(page, server):
    """A plain link would replace this page with the attachment URL — in the
    home-screen app, with no browser chrome, that strands the reader.

    What is asserted is that nothing asks for ?download=1: the file is
    fetched and handed over as a blob instead. page.url alone does not
    discriminate, because a navigation that turns into a download leaves the
    address bar where it was — the first version of this test passed with
    preventDefault removed.
    """
    asked = []
    page.on("request", lambda r: asked.append(r.url))
    page.goto(f"{server}/read/{STEM}.md")
    page.wait_for_selector(".reading")
    before = page.url

    with page.expect_download() as caught:
        page.click("#download")
    assert caught.value.suggested_filename == f"{STEM}.md"
    assert page.url == before
    assert not [u for u in asked if "download=1" in u], asked


def test_the_reader_sets_the_math_and_the_page_in_serif(page, server):
    page.goto(f"{server}/read/{STEM}.md")
    page.wait_for_selector(".reading")
    look = page.evaluate("""() => ({
      body: getComputedStyle(document.body).backgroundColor,
      text: getComputedStyle(document.querySelector('.reading')).fontFamily,
      back: document.querySelector('header.reader-bar .back').textContent,
    })""")
    assert look["body"] == "rgb(245, 238, 220)"
    assert "Iowan Old Style" in look["text"]
    assert look["back"] == "← Margin"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_an_error_page_has_a_way_back(page, server):
    """A stale bookmark is ordinary, and on the home screen there is not
    even a back button."""
    page.goto(f"{server}/read/nothing-here.md")
    assert page.locator("text=← Margin").count() == 1
    page.click("text=← Margin")
    page.wait_for_selector(".item")
    assert page.url.rstrip("/") == server.rstrip("/")
