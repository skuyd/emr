from contextlib import contextmanager
import io
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.template.loader import render_to_string
from django.test import RequestFactory
from PIL import Image
import pytest

from tests.browser.test_ac02_upload_browser import _browser_executable


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://viewer.test"
DOCUMENT_ID = "00000000-0000-4000-8000-000000000001"
DETAIL_PATH = f"/records/{DOCUMENT_ID}/"
VIEWER_PATH = f"{DETAIL_PATH}viewer/"


@contextmanager
def _viewer_browser(*, page_count=3, viewport=None):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    executable = _browser_executable()
    if executable is None:
        pytest.skip("No supported local Chromium browser was found")
    context = {
        "document": SimpleNamespace(pk=DOCUMENT_ID, page_count=page_count, display_filename="scan.pdf"),
        "document_title": "血常规",
        "initial_page": 1,
        "initial_page_url": "/pages/1/image/",
        "page_url_template": "/pages/{page}/image/",
        "page_numbers": range(1, page_count + 1),
        "highlight_page": 1,
        "highlight_rect": "10,20,50,10",
    }
    request = RequestFactory().get(DETAIL_PATH)
    markup = {
        "detail": render_to_string("documents/detail.html", context, request=request),
        "viewer": render_to_string("documents/viewer.html", context, request=request),
        "embed": render_to_string("documents/viewer_embed.html", context, request=request),
    }
    images = {}
    for number, size in enumerate(((1000, 1400), (1400, 900), (1000, 3000)), 1):
        buffer = io.BytesIO()
        Image.new("RGB", size, "white").save(buffer, format="PNG")
        images[f"/pages/{number}/image/"] = buffer.getvalue()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
        page = browser.new_page(viewport=viewport or {"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            url = urlsplit(route.request.url)
            if f"{url.scheme}://{url.netloc}" != ORIGIN:
                route.abort()
            elif url.path == DETAIL_PATH:
                route.fulfill(content_type="text/html", body=markup["detail"])
            elif url.path == VIEWER_PATH:
                route.fulfill(content_type="text/html", body=markup["embed" if "embed=1" in url.query else "viewer"])
            elif url.path in images:
                route.fulfill(content_type="image/png", body=images[url.path])
            elif url.path.startswith("/static/"):
                path = ROOT / url.path.lstrip("/")
                if path.is_file():
                    route.fulfill(path=path)
                else:
                    route.fulfill(status=404)
            elif url.path == "/api/notifications/":
                route.fulfill(json={"unread_count": 0, "notifications": []})
            else:
                route.fulfill(status=204)

        page.route("**/*", serve)
        try:
            yield page
            assert errors == []
        finally:
            browser.close()


def _image_bounds(image):
    return image.evaluate("""async image => {
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const bounds = element => {
            const r = element.getBoundingClientRect();
            return { x: r.x, y: r.y, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
        };
        return { image: bounds(image), stage: bounds(image.closest('[data-viewer-stage]')) };
    }""")


def _assert_whole_page(image, *, fills_width=True):
    metrics = _image_bounds(image)
    sheet, stage = metrics["image"], metrics["stage"]
    assert sheet["width"] > 0 and sheet["height"] > 0
    assert sheet["x"] >= stage["x"] and sheet["right"] <= stage["right"] + 1
    assert sheet["y"] >= stage["y"] and sheet["bottom"] <= stage["bottom"] + 1
    if fills_width:
        assert sheet["width"] >= min(1000, stage["width"] - 32) - 2
    return metrics


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_embedded_preview_fits_page_and_adapts_to_page_shape_and_window(width):
    from playwright.sync_api import expect

    with _viewer_browser(viewport={"width": width, "height": 900}) as page:
        page.goto(f"{ORIGIN}{DETAIL_PATH}", wait_until="networkidle")
        frame = page.frame_locator('iframe[name="document-preview"]')
        image = frame.locator("[data-viewer-image]")
        expect(image).to_have_js_property("naturalHeight", 1400)
        portrait = _assert_whole_page(image)
        frame.get_by_role("button", name="下一页", exact=True).click()
        expect(image).to_have_js_property("naturalHeight", 900)
        landscape = _assert_whole_page(image)
        assert landscape["stage"]["height"] < portrait["stage"]["height"]
        frame.get_by_role("button", name="下一页", exact=True).click()
        expect(image).to_have_js_property("naturalHeight", 3000)
        _assert_whole_page(image)
        page.set_viewport_size({"width": 760, "height": 600})
        _assert_whole_page(image)
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")


def test_viewer_fit_reset_rotation_fullscreen_and_evidence_remain_usable():
    from playwright.sync_api import expect

    with _viewer_browser(page_count=1) as page:
        page.goto(f"{ORIGIN}{VIEWER_PATH}", wait_until="networkidle")
        image = page.locator("[data-viewer-image]")
        expect(image).to_have_js_property("naturalHeight", 1400)
        _assert_whole_page(image)
        page.get_by_role("button", name="放大", exact=True).click()
        expect(page.locator("[data-viewer-zoom-status]")).to_have_text("125%")
        page.get_by_role("button", name="适合整页", exact=True).click()
        expect(page.locator("[data-viewer-zoom-status]")).to_have_text("100%")
        normal = _assert_whole_page(image)
        highlight = page.locator("[data-viewer-highlight]").bounding_box()
        assert highlight["width"] == pytest.approx(normal["image"]["width"] * .5, abs=1)
        page.get_by_role("button", name="顺时针旋转", exact=True).click()
        _assert_whole_page(image)
        page.get_by_role("button", name="全屏查看", exact=True).click()
        page.wait_for_function("document.fullscreenElement !== null")
        _assert_whole_page(image, fills_width=False)
        page.evaluate("document.exitFullscreen()")
        page.wait_for_function("document.fullscreenElement === null")
        _assert_whole_page(image)
