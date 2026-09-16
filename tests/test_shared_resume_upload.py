"""Regression tests for issue #15: one shared file-upload implementation.

Before this change JobrightScraper carried its own `_upload_documents_if_prompted`
while every other scraper used `BaseScraper._upload_resume_if_prompted`. The two
drifted: different accept-type matching, different label resolution, and only
Jobright handled cover letters. These tests pin the consolidated behavior onto
BaseScraper so a future edit cannot silently re-divide it.
"""
import inspect

import pytest

from src.sources.base import BaseScraper
from src.sources.jobright import JobrightScraper


def test_jobright_no_longer_overrides_the_shared_upload_helper():
    """The duplicate must be gone, not merely shadowed."""
    assert "_upload_documents_if_prompted" not in vars(JobrightScraper)


def test_single_definition_of_the_upload_helper():
    assert "_upload_documents_if_prompted" in vars(BaseScraper)
    assert inspect.iscoroutinefunction(BaseScraper._upload_documents_if_prompted)


def test_resume_wrapper_delegates_to_the_documents_helper():
    """`_upload_resume_if_prompted` is a thin wrapper, so calling it reaches the
    one real implementation rather than a second one."""
    import asyncio

    calls = []

    class _Scraper(BaseScraper):
        name = "probe"

        async def scrape(self, *a, **kw):
            return []

        async def apply(self, *a, **kw):
            return False

        async def _upload_documents_if_prompted(self, page, resume_path, cover_letter_path=""):
            calls.append((resume_path, cover_letter_path))
            return True

    asyncio.run(_Scraper({})._upload_resume_if_prompted(object(), "/tmp/resume.pdf"))

    assert calls == [("/tmp/resume.pdf", "")]


# ── DOM behavior (fresh subprocess so the real playwright import is unshadowed:
#    tests/conftest.py stubs playwright at session scope for in-process tests) ──

_CHILD_SCRIPT = r'''
import asyncio, json, sys

async def main():
    from playwright.async_api import async_playwright
    from src.sources.base import BaseScraper

    class Scraper(BaseScraper):
        name = "test"
        async def scrape(self, *a, **kw): return []
        async def apply(self, *a, **kw): return False

    case = json.loads(sys.stdin.read())
    html = case["html"]
    with_cover = case.get("with_cover", True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.route("**/*", lambda route: route.abort())
        await page.set_content(html)
        try:
            scraper = Scraper({"search_settings": {"delay_min_seconds": 0, "delay_max_seconds": 0}})
            fp = "/tmp/_issue15_resume.pdf"
            cl = "/tmp/_issue15_cover.pdf"
            for p in (fp, cl):
                with open(p, "wb") as fh:
                    fh.write(b"%PDF-1.4\n")
            uploaded = await scraper._upload_documents_if_prompted(
                page, fp, cl if with_cover else ""
            )
            files = await page.evaluate(
                "() => [...document.querySelectorAll('input[type=file]')]"
                ".map(i => (i.files[0] ? i.files[0].name : null))"
            )
            sys.stdout.write(json.dumps({"uploaded": uploaded, "files": files}))
        finally:
            await browser.close()

asyncio.run(main())
'''


def _run_dom(html: str, *, with_cover: bool = True) -> dict:
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = os.fspath(Path(__file__).resolve().parent.parent)
    proc = subprocess.run(  # noqa: S603 — args controlled, no shell
        [sys.executable, "-c", _CHILD_SCRIPT],
        input=json.dumps({"html": html, "with_cover": with_cover}),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        pytest.fail(f"upload DOM harness failed:\n{proc.stderr}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        pytest.fail(f"non-JSON harness output: {exc}\nstdout={proc.stdout!r}\nstderr={proc.stderr}")


def test_label_walk_finds_resume_input_and_uploads(tmp_path):
    """A resume input whose label is an ancestor <div> text (the Jobright case
    the shallow label lookup used to miss) must be selected."""
    html = """
    <form>
      <div>Upload your Resume <input type="file" name="doc"></div>
      <input type="file" name="other" accept=".txt">
    </form>
    """
    result = _run_dom(html)

    assert result["uploaded"] is True
    assert "_issue15_resume.pdf" in result["files"]


def test_cover_letter_input_is_not_given_the_resume(tmp_path):
    """Cover-letter inputs must receive the cover letter, not the resume."""
    html = """
    <form>
      <div>Resume <input type="file" id="r1" name="resume"></div>
      <div>Cover Letter <input type="file" id="c1" name="cover_letter"></div>
    </form>
    """
    result = _run_dom(html)

    assert result["uploaded"] is True
    assert result["files"] == ["_issue15_resume.pdf", "_issue15_cover.pdf"]


def test_accept_type_outside_pdf_word_is_skipped(tmp_path):
    """An input that only accepts images must not be handed the resume."""
    html = '<form><input type="file" name="avatar" accept="image/png"></form>'
    result = _run_dom(html)

    assert result["uploaded"] is False
    assert result["files"] == [None]


def test_unlabeled_input_falls_back_to_resume(tmp_path):
    """With no usable label hints, the first unlabeled input receives the resume."""
    html = '<form><input type="file" id="x"></form>'
    result = _run_dom(html)

    assert result["uploaded"] is True
    assert result["files"] == ["_issue15_resume.pdf"]


def test_aria_label_is_used_when_no_ancestor_text():
    """araia-label fallback (from the deeper Jobright walk) must survive."""
    html = '<form><input type="file" id="r" aria-label="Resume upload"></form>'
    result = _run_dom(html)

    assert result["uploaded"] is True


def test_generic_upload_input_still_receives_resume():
    """A plain 'Upload file' input has no resume/cv keyword; the unlabeled
    fallback must still deliver the resume (this replaces the old base helper's
    'upload'/'file' keyword match)."""
    html = '<form><input type="file" id="g" name="file" title="Upload file"></form>'
    result = _run_dom(html)

    assert result["uploaded"] is True
    assert result["files"] == ["_issue15_resume.pdf"]


def test_cover_letter_only_input_gets_nothing_when_no_cover_letter_supplied():
    """Resume-only callers (LinkedIn Easy Apply) must not leak the resume into a
    cover-letter field."""
    html = '<form><div>Cover Letter <input type="file" name="cover_letter"></div></form>'
    result = _run_dom(html, with_cover=False)

    assert result["uploaded"] is False
    assert result["files"] == [None]
