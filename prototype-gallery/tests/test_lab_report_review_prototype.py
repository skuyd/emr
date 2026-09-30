"""Browser checks for the stand-alone report review prototype."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from threading import Thread

import pytest
from playwright.sync_api import expect, sync_playwright


PROTOTYPE = Path(__file__).resolve().parents[1] / "lab-report-review" / "index.html"


@pytest.fixture(scope="module")
def prototype_url():
    handler = partial(SimpleHTTPRequestHandler, directory=str(PROTOTYPE.parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        executable = os.environ.get("PHR_BROWSER_EXECUTABLE")
        if not executable and os.name == "nt":
            for candidate in (Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                              Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")):
                if candidate.is_file():
                    executable = str(candidate)
                    break
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        try:
            page = context.new_page()
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(30000)
            yield page
        finally:
            context.close()
            browser.close()


def test_save_keeps_report_pending_until_explicit_confirmation(page, prototype_url):
    assert PROTOTYPE.exists(), "The clickable prototype must exist."
    page.goto(prototype_url)
    expect(page.get_by_role("heading", name="报告核对", exact=True)).to_be_visible()
    result = page.locator('[data-row-id="wbc"] [name="raw_value"]')
    result.fill("6.30")
    page.get_by_role("button", name="保存修改", exact=True).click()
    expect(page.locator("[data-review-state]")).to_have_text("待核对")
    page.reload()
    expect(page.locator('[data-row-id="wbc"] [name="raw_value"]')).to_have_value("6.30")
    page.get_by_role("button", name="确认本报告", exact=True).click()
    expect(page.locator("[data-review-state]")).to_have_text("已确认")


def test_incomplete_manual_row_stays_visible_and_focuses_invalid_field(page, prototype_url):
    assert PROTOTYPE.exists(), "The clickable prototype must exist."
    page.goto(prototype_url)
    page.get_by_role("button", name="添加遗漏指标", exact=True).click()
    page.get_by_role("button", name="保存修改", exact=True).click()
    name = page.locator('[data-new-row] [name="raw_name"]')
    expect(name).to_be_focused()
    expect(page.locator('[data-new-row] [name="raw_name"] + [data-field-error]')).to_be_visible()
    expect(page.locator("[data-review-state]")).to_have_text("待核对")


def test_exclusion_needs_a_reason_before_it_can_be_saved(page, prototype_url):
    page.goto(prototype_url)
    page.locator('[data-row-id="wbc"] [data-exclude]').click()
    page.get_by_role("button", name="保存修改", exact=True).click()
    expect(page.locator('[data-row-id="wbc"] [name="exclusion_reason"]')).to_be_focused()
    expect(page.locator("[data-review-state]")).to_have_text("待核对")


def test_confirm_and_next_does_not_confirm_the_next_report(page, prototype_url):
    page.goto(prototype_url)
    page.get_by_role("button", name="确认并下一份", exact=True).click()
    expect(page.locator("#summary-number")).to_have_text("DEMO-LAB-2609-019")
    expect(page.locator("[data-review-state]")).to_have_text("待核对")
    page.get_by_role("button", name="上一份").click()
    expect(page.locator("[data-review-state]")).to_have_text("已确认")


def test_a_second_manual_row_does_not_replace_the_first_after_reload(page, prototype_url):
    page.goto(prototype_url)
    page.get_by_role("button", name="添加遗漏指标", exact=True).click()
    first = page.locator("[data-new-row]").first
    first.locator('[name="raw_name"]').fill("补录项目甲")
    first.locator('[name="raw_value"]').fill("阳性")
    page.get_by_role("button", name="保存修改", exact=True).click()
    page.reload()
    page.get_by_role("button", name="添加遗漏指标", exact=True).click()
    second = page.locator("[data-new-row]").last
    second.locator('[name="raw_name"]').fill("补录项目乙")
    second.locator('[name="raw_value"]').fill("阴性")
    page.get_by_role("button", name="保存修改", exact=True).click()
    page.reload()
    expect(page.locator('[data-new-row] [name="raw_name"]')).to_have_count(2)
    expect(page.locator('[data-new-row] [name="raw_name"]').first).to_have_value("补录项目甲")
    expect(page.locator('[data-new-row] [name="raw_name"]').last).to_have_value("补录项目乙")


def test_source_hint_distinguishes_initial_view_from_page_only_location(page, prototype_url):
    page.goto(prototype_url)
    expect(page.locator("#source-location")).to_contain_text("选择右侧指标")
    page.get_by_role("button", name="添加遗漏指标", exact=True).click()
    page.locator('[data-new-row] [data-locate]').click()
    expect(page.locator("#source-location")).to_contain_text("没有可靠的精确坐标")
    expect(page.locator("#source-highlight")).to_be_hidden()


def test_confirmed_report_stays_confirmed_when_saved_without_new_edits(page, prototype_url):
    page.goto(prototype_url)
    page.get_by_role("button", name="确认本报告", exact=True).click()
    page.get_by_role("button", name="保存修改", exact=True).click()
    expect(page.locator("[data-review-state]")).to_have_text("已确认")
    expect(page.locator("#action-feedback")).not_to_contain_text("仍待核对")


def test_new_conflict_invalidates_the_visible_confirmation_until_resolved(page, prototype_url):
    page.goto(prototype_url)
    page.get_by_role("button", name="确认本报告", exact=True).click()
    page.locator("#demo-scenario").select_option("conflict")
    expect(page.locator("[data-review-state]")).to_have_text("待核对")
    page.get_by_role("button", name="确认本报告", exact=True).click()
    expect(page.locator("#action-feedback")).to_contain_text("先处理")
    page.get_by_label("保留人工核对值 5.2 mg/L").check()
    page.get_by_role("button", name="确认本报告", exact=True).click()
    expect(page.locator("[data-review-state]")).to_have_text("已确认")


def test_unsaved_report_switch_requires_an_explicit_choice(page, prototype_url):
    page.goto(prototype_url)
    page.locator('[data-row-id="wbc"] [name="raw_value"]').fill("6.30")
    page.locator("#next-report").click()
    expect(page.get_by_role("dialog", name="有未保存的修改")).to_be_visible()
    page.get_by_role("button", name="继续编辑").click()
    expect(page.locator("#summary-number")).to_have_text("DEMO-LAB-2609-018")
    page.locator("#next-report").click()
    page.get_by_role("button", name="放弃修改").click()
    expect(page.locator("#summary-number")).to_have_text("DEMO-LAB-2609-019")


def test_conflict_demo_opens_its_actual_source_report(page, prototype_url):
    page.goto(prototype_url)
    page.locator("#next-report").click()
    page.locator("#demo-scenario").select_option("conflict")
    expect(page.locator("#summary-number")).to_have_text("DEMO-LAB-2609-018")
    expect(page.locator("#source-tabs button")).to_have_count(2)


def test_last_report_does_not_claim_all_done_while_an_earlier_one_is_pending(page, prototype_url):
    page.goto(prototype_url)
    page.locator("#next-report").click()
    page.get_by_role("button", name="确认并下一份", exact=True).click()
    expect(page.locator("#action-feedback")).not_to_contain_text("所有已列出的报告均已核对")


def test_conflict_choice_updates_the_effective_result_before_confirmation(page, prototype_url):
    page.goto(prototype_url)
    page.locator("#demo-scenario").select_option("conflict")
    page.get_by_label("采用本次识别值 52 mg/L").check()
    page.get_by_role("button", name="确认本报告", exact=True).click()
    expect(page.locator('[data-row-id="crp"] [name="raw_value"]')).to_have_value("52")
    page.reload()
    page.locator("#demo-scenario").select_option("conflict")
    expect(page.locator('[data-row-id="crp"] [name="raw_value"]')).to_have_value("52")


def test_editing_conflict_result_reopens_the_unresolved_choice(page, prototype_url):
    page.goto(prototype_url)
    page.locator("#demo-scenario").select_option("conflict")
    page.get_by_label("采用本次识别值 52 mg/L").check()
    page.locator('[data-row-id="crp"] [name="raw_value"]').fill("5.3")
    page.get_by_role("button", name="确认本报告", exact=True).click()
    expect(page.locator("#action-feedback")).to_contain_text("识别冲突")
    expect(page.locator("[data-review-state]")).to_have_text("待核对")


def test_skipping_a_conflict_does_not_attach_it_to_the_next_report(page, prototype_url):
    page.goto(prototype_url)
    page.locator("#demo-scenario").select_option("conflict")
    page.locator("#next-report").click()
    expect(page.locator("#summary-number")).to_have_text("DEMO-LAB-2609-019")
    expect(page.locator("#conflict-section")).to_be_hidden()
    expect(page.locator("#review-counts")).to_contain_text("待处理 0 项")


def test_skipped_conflict_is_still_blocking_when_returning_to_its_report(page, prototype_url):
    page.goto(prototype_url)
    page.locator("#demo-scenario").select_option("conflict")
    page.locator("#next-report").click()
    page.locator("#previous-report").click()
    expect(page.locator("#conflict-section")).to_be_visible()
    expect(page.locator("#review-counts")).to_contain_text("待处理 1 项")
    page.get_by_role("button", name="确认本报告", exact=True).click()
    expect(page.locator("[data-review-state]")).to_have_text("待核对")


def test_zoomed_location_scrolls_the_source_to_show_the_highlight(page, prototype_url):
    page.goto(prototype_url)
    for _ in range(4):
        page.locator("#zoom-in").click()
    page.locator('[data-row-id="plt"] [data-locate]').click()
    assert page.locator("#source-viewport").evaluate("element => element.scrollTop") > 0


def test_rotated_source_remains_reachable_in_the_mobile_viewport(page, prototype_url):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(prototype_url)
    page.locator("#rotate-image").click()
    image = page.locator("#source-image").bounding_box()
    viewport = page.locator("#source-viewport").bounding_box()
    assert image["x"] >= viewport["x"] - 1
    assert page.locator("#source-viewport").evaluate(
        "element => element.scrollWidth > element.clientWidth"
    )


def test_editing_a_confirmed_report_shows_that_it_needs_reconfirmation(page, prototype_url):
    page.goto(prototype_url)
    page.get_by_role("button", name="确认本报告", exact=True).click()
    page.locator('[data-row-id="wbc"] [name="raw_value"]').fill("6.30")
    expect(page.locator("[data-review-state]")).to_have_text("待重新确认")


def test_reopen_prefers_the_next_pending_report(page, prototype_url):
    page.goto(prototype_url)
    page.get_by_role("button", name="确认并下一份", exact=True).click()
    page.reload()
    expect(page.locator("#summary-number")).to_have_text("DEMO-LAB-2609-019")


def test_all_confirmed_reports_show_completion_and_remain_viewable(page, prototype_url):
    page.goto(prototype_url)
    page.get_by_role("button", name="确认并下一份", exact=True).click()
    page.get_by_role("button", name="确认本报告", exact=True).click()
    page.reload()
    expect(page.locator("#completion-note")).to_be_visible()
    page.locator("#next-report").click()
    expect(page.locator("#summary-number")).to_have_text("DEMO-LAB-2609-019")


def test_correcting_the_original_name_clears_the_stale_catalog_match(page, prototype_url):
    page.goto(prototype_url)
    card = page.locator('[data-row-id="wbc"]')
    card.locator("summary").click()
    card.locator('[name="raw_name"]').fill("目录外项目甲")
    expect(card.locator("[data-source-detail]")).to_contain_text("待重新匹配")
    expect(card.locator("[data-source-detail]")).not_to_contain_text("目录匹配：白细胞计数")


def test_unreadable_original_result_can_be_saved_as_pending_but_not_confirmed(page, prototype_url):
    page.goto(prototype_url)
    page.get_by_role("button", name="添加遗漏指标", exact=True).click()
    added = page.locator("[data-new-row]")
    added.locator('[name="raw_name"]').fill("原件项目甲")
    added.get_by_label("原件结果为空或无法辨认，留待处理").check()
    page.get_by_role("button", name="保存修改", exact=True).click()
    expect(page.locator("#action-feedback")).to_contain_text("已保存")
    page.get_by_role("button", name="确认本报告", exact=True).click()
    expect(page.locator("[data-review-state]")).to_have_text("待核对")
    expect(page.locator("#action-feedback")).to_contain_text("待处理")


def test_recognized_result_can_be_marked_unreadable_and_saved_as_pending(page, prototype_url):
    page.goto(prototype_url)
    recognized = page.locator('[data-row-id="wbc"]')
    recognized.locator('[name="raw_value"]').fill("")
    recognized.get_by_label("原件结果为空或无法辨认，留待处理").check()
    page.get_by_role("button", name="保存修改", exact=True).click()
    expect(page.locator("#action-feedback")).to_contain_text("已保存")
    expect(page.locator("#review-counts")).to_contain_text("待处理 1 项")
    page.get_by_role("button", name="确认本报告", exact=True).click()
    expect(page.locator("[data-review-state]")).to_have_text("待核对")
    expect(page.locator("#action-feedback")).to_contain_text("待处理")
    page.reload()
    recognized = page.locator('[data-row-id="wbc"]')
    expect(recognized.get_by_label("原件结果为空或无法辨认，留待处理")).to_be_checked()
    recognized.locator('[name="raw_value"]').fill("6.20")
    recognized.get_by_label("原件结果为空或无法辨认，留待处理").uncheck()
    page.get_by_role("button", name="确认本报告", exact=True).click()
    expect(page.locator("[data-review-state]")).to_have_text("已确认")


def test_blank_recognized_result_requires_pending_mark(page, prototype_url):
    page.goto(prototype_url)
    recognized = page.locator('[data-row-id="wbc"]')
    recognized.locator('[name="raw_value"]').fill("")
    page.get_by_role("button", name="保存修改", exact=True).click()
    expect(recognized.locator('[name="raw_value"]')).to_have_attribute("aria-invalid", "true")
    expect(recognized.locator("[data-field-error]").nth(1)).to_contain_text("请标记待处理")


def test_no_report_scenario_shows_an_empty_state_without_confirm_actions(page, prototype_url):
    page.goto(prototype_url)
    page.locator("#demo-scenario").select_option("none")
    expect(page.locator("#empty-title")).to_have_text("尚无可核对的报告范围")
    expect(page.locator("#report-view")).to_be_hidden()
    expect(page.locator("#empty-next")).to_be_hidden()
