"""Deterministic browser regressions using real templates, CSS and upload JS.

All requests are fulfilled from repository assets or synthetic API responses;
the browser never connects to an application, object store or external host.
"""

from contextlib import contextmanager
import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

from django.template.loader import render_to_string
from django.test import RequestFactory
import pytest

from tests.browser.test_ac02_upload_browser import _browser_executable, _png_bytes


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://upload.test"
BATCH_ID = "00000000-0000-4000-8000-000000000001"
ITEM_ID = "00000000-0000-4000-8000-000000000002"


@contextmanager
def _upload_browser():
    if importlib.util.find_spec("playwright") is None:
        pytest.skip("Playwright is not installed")
    executable = _browser_executable()
    if executable is None:
        pytest.skip("No supported local Chromium browser was found")
    from playwright.sync_api import sync_playwright

    markup = render_to_string(
        "documents/upload.html", {"current_section": "home"},
        request=RequestFactory().get("/uploads/new/"),
    )
    assets = {
        f"/static/{path.relative_to(ROOT / 'static').as_posix()}": path
        for folder in ("css", "js")
        for path in (ROOT / "static" / folder).iterdir()
        if path.is_file()
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            url = urlsplit(route.request.url)
            if f"{url.scheme}://{url.netloc}" != ORIGIN:
                route.abort()
            elif url.path == "/uploads/new/":
                route.fulfill(content_type="text/html", body=markup)
            elif url.path in assets:
                path = assets[url.path]
                content_type = "text/css" if path.suffix == ".css" else "text/javascript"
                route.fulfill(content_type=content_type, body=path.read_bytes())
            elif url.path == "/api/notifications/":
                route.fulfill(json={"notifications": [], "unread_count": 0})
            else:
                route.abort()

        page.route("**/*", serve)
        try:
            page.goto(f"{ORIGIN}/uploads/new/", wait_until="load")
            yield page
            assert errors == []
        finally:
            browser.close()


def _select_image(page):
    page.locator("[data-file-input]").set_input_files(
        {"name": "synthetic.png", "mimeType": "image/png", "buffer": _png_bytes()}
    )


def _accept_batch(route):
    route.fulfill(json={
        "batch_id": BATCH_ID,
        "items": [{"ordinal": 1, "item_id": ITEM_ID, "accepted": True, "error_code": None}],
    })


def test_upload_hides_inapplicable_actions_and_status_icons():
    from playwright.sync_api import expect

    with _upload_browser() as page:
        _select_image(page)
        expect(page.get_by_role("button", name="重试", exact=True)).to_have_count(0)
        expect(page.locator("[data-retry-file]")).to_be_hidden()
        expect(page.locator("[data-existing-link]")).to_be_hidden()
        expect(page.locator("[data-processing-failure-link]")).to_be_hidden()
        expect(page.locator("[data-status-icon]:visible")).to_have_count(1)
        expect(page.locator("[data-status-icon=processing]")).to_be_visible()


def test_retry_ignores_unreserved_pending_and_inflight_rows():
    from playwright.sync_api import expect

    with _upload_browser() as page:
        # Dispatch bypasses visual hiding deliberately: the state machine must
        # reject stale queued clicks too, even when CSS is working correctly.
        _select_image(page)
        sent = []
        page.route("**/content/", lambda route: (sent.append(route.request.url), route.abort()))
        page.locator("[data-retry-file]").dispatch_event("click")
        expect(page.locator("[data-file-status]")).to_have_attribute("data-state", "PENDING")
        assert sent == []

        page.route("**/api/upload-batches/", _accept_batch)
        pending_uploads = []
        page.route("**/content/", lambda route: pending_uploads.append(route))
        page.locator("[data-start-upload]").click()
        expect(page.locator("[data-file-status]")).to_have_attribute("data-state", "UPLOADING")
        page.locator("[data-retry-file]").dispatch_event("click")
        assert len(pending_uploads) == 1
        pending_uploads[0].fulfill(json={"saved": True, "document_id": ITEM_ID, "status": "ORGANIZED"})
        expect(page.locator("[data-file-status]")).to_have_attribute("data-state", "ORGANIZED")
        page.locator("[data-retry-file]").dispatch_event("click")
        expect(page.locator("[data-file-status]")).to_have_attribute("data-state", "ORGANIZED")
        assert len(pending_uploads) == 1


def test_slow_batch_creation_freezes_removal_and_keeps_file_results_matched():
    from playwright.sync_api import expect

    with _upload_browser() as page:
        page.locator("[data-file-input]").set_input_files([
            {"name": "unsupported.txt", "mimeType": "text/plain", "buffer": b"synthetic"},
            {"name": "synthetic.png", "mimeType": "image/png", "buffer": _png_bytes()},
        ])
        pending_batches = []
        uploads = []
        page.route("**/api/upload-batches/", lambda route: pending_batches.append(route))

        def save_image(route):
            uploads.append(route.request)
            route.fulfill(json={"saved": True, "document_id": ITEM_ID, "status": "ORGANIZED"})

        page.route("**/content/", save_image)
        page.locator("[data-start-upload]").click()
        expect(page.locator("[data-remove-file]").first).to_be_disabled()
        page.locator("[data-remove-file]").first.dispatch_event("click")
        expect(page.locator("[data-file-row]")).to_have_count(2)
        pending_batches[0].fulfill(json={
            "batch_id": BATCH_ID,
            "items": [
                {"ordinal": 1, "item_id": "rejected", "accepted": False, "error_code": "unsupported_file"},
                {"ordinal": 2, "item_id": ITEM_ID, "accepted": True, "error_code": None},
            ],
        })
        expect(page.locator("[data-file-status]").nth(1)).to_have_attribute("data-state", "ORGANIZED")
        expect(page.locator("[data-file-name]").nth(1)).to_have_text("synthetic.png")
        assert len(uploads) == 1
        assert f"/items/{ITEM_ID}/content/" in uploads[0].url
        assert b'filename="synthetic.png"' in uploads[0].post_data_buffer


def test_upload_picker_has_a_visible_keyboard_focus_indicator():
    with _upload_browser() as page:
        page.locator("[data-file-input]").focus()
        style = page.locator("[data-dropzone]").evaluate("""element => {
            const style = getComputedStyle(element);
            return { outline: style.outlineStyle, width: parseFloat(style.outlineWidth) };
        }""")
        assert style["outline"] == "solid"
        assert style["width"] >= 3


def test_failed_batch_creation_does_not_enable_unreserved_item_retry():
    from playwright.sync_api import expect

    with _upload_browser() as page:
        _select_image(page)
        page.route("**/api/upload-batches/", lambda route: route.fulfill(
            status=503, json={"error": {"code": "upload_service_unavailable"}},
        ))
        page.locator("[data-start-upload]").click()
        expect(page.locator("[data-start-upload]")).to_be_enabled()
        expect(page.locator("[data-retry-file]")).to_be_hidden()
        page.locator("[data-retry-file]").dispatch_event("click")
        expect(page.locator("[data-file-status]")).to_have_attribute("data-state", "UPLOAD_FAILED")
        expect(page.locator("[data-remove-file]")).to_be_enabled()


def test_transient_upload_failure_can_retry_once_and_finish():
    from playwright.sync_api import expect

    with _upload_browser() as page:
        _select_image(page)
        page.route("**/api/upload-batches/", _accept_batch)
        attempts = []

        def upload(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                route.fulfill(status=503, json={"error": {"code": "storage_unavailable"}})
            else:
                route.fulfill(json={"saved": True, "document_id": ITEM_ID, "status": "ORGANIZED"})

        page.route("**/content/", upload)
        page.locator("[data-start-upload]").click()
        expect(page.locator("[data-file-status]")).to_have_attribute("data-state", "UPLOAD_FAILED")
        page.get_by_role("button", name="重试", exact=True).click()
        expect(page.locator("[data-file-status]")).to_have_attribute("data-state", "ORGANIZED")
        expect(page.locator("[data-retry-file]")).to_be_hidden()
        assert len(attempts) == 2
        assert attempts[0] == attempts[1]
