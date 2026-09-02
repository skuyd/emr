import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _rule_declarations(css, selector):
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)}}", css, re.DOTALL)
    assert match, f"Missing CSS rule: {selector}"
    return dict(re.findall(r"([a-z-]+)\s*:\s*([^;]+);", match.group(1), re.IGNORECASE))


def test_records_markup_has_labeled_search_date_filters_and_server_actions():
    template = (PROJECT_ROOT / "templates" / "documents" / "records.html").read_text(encoding="utf-8")
    card = (PROJECT_ROOT / "templates" / "components" / "_record_card.html").read_text(encoding="utf-8")

    assert "收好的健康资料" in template
    assert '<section class="records-page" aria-labelledby="records-title">' in template
    assert '<h1 id="records-title">收好的健康资料</h1>' in template
    assert '<form class="records-search" method="get" role="search">' in template
    for field, label in (
        ("records-query", "搜索资料"),
        ("records-type", "资料类型"),
        ("records-status", "整理状态"),
        ("records-year", "年份"),
        ("records-month", "月份"),
    ):
        assert f'for="{field}"' in template
        assert f'id="{field}"' in template
        assert label in template
    assert 'name="q"' in template
    assert 'name="type"' in template
    assert 'name="status"' in template
    assert 'name="year"' in template
    assert 'name="month"' in template
    assert '{% include "components/_record_card.html"' in template
    assert "打开原件" in template
    assert "原件已保存" in template
    assert 'method="post"' in card
    assert "document_reprocess" in template
    assert "csrf_token" in card
    assert "没有找到相关资料，换个关键词试试。" in template
    assert 'class="records-upload button button--primary"' in template

    assert 'class="record-card"' in card
    assert 'include "components/_status_badge.html"' in card
    assert 'class="record-card__actions"' in card
    assert "status == reprocess_status" in card
    assert "upload_date|date:" in card


def test_records_styles_keep_tokens_focus_targets_forced_colors_and_narrow_overflow():
    css = (PROJECT_ROOT / "static" / "css" / "records.css").read_text(encoding="utf-8")

    page = _rule_declarations(css, ".records-page")
    assert page.get("min-width") == "0"
    assert "var(--radius-card)" in css
    assert "min-height: 44px" in css
    assert ".records-search" in css
    assert ".record-card__actions" in css
    assert ".record-card__link" in css
    assert ":focus-visible" in css
    assert "overflow-wrap: anywhere" in css
    assert "min-width: 0" in css
    assert "@media (max-width: 64rem)" in css
    assert "@media (max-width: 40rem)" in css
    assert "@media (forced-colors: active)" in css
    assert not re.search(r"#[0-9a-f]{3,8}\b", css, re.IGNORECASE)
