import re
from pathlib import Path

from django import forms
from django.core.exceptions import ValidationError
from django.template import engines
from django.template.loader import render_to_string


PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPONENTS_DIR = PROJECT_ROOT / "templates" / "components"


class ExampleForm(forms.Form):
    display_name = forms.CharField(
        label="患者称呼",
        help_text="填写自己或家人的称呼。",
        error_messages={"required": "请输入患者称呼。"},
    )

    def clean(self):
        cleaned_data = super().clean()
        raise ValidationError("请检查下面标出的内容。")


def test_icon_partial_only_renders_explicit_named_inline_svg_branches():
    for name in ("brand", "processing", "saved", "organized", "original", "failed", "empty", "document"):
        html = render_to_string("components/_icon.html", {"name": name})
        assert html.count("<svg") == 1
        assert 'aria-hidden="true"' in html
        assert "<path" in html or "<circle" in html

    rejected = render_to_string(
        "components/_icon.html",
        {"name": '<script src="https://example.invalid/icon.js"></script>'},
    )
    assert "<svg" not in rejected
    assert "<script" not in rejected
    assert "example.invalid" not in rejected


def test_brand_lockup_has_visible_name_and_decorative_inline_mark():
    html = render_to_string("components/_brand.html", {"brand_url": "/"})

    assert 'class="brand-lockup"' in html
    assert 'class="brand-lockup__mark" aria-hidden="true"' in html
    assert "暖笺" in html
    assert "家庭健康档案" in html
    assert "<svg" in html
    assert "<img" not in html


def test_status_badges_encode_every_state_with_icon_text_and_class_color_hook():
    labels = {
        "processing": "处理中",
        "saved": "已保存",
        "organized": "已整理",
        "original": "仅原件",
        "failed": "处理失败",
    }

    for status, label in labels.items():
        html = render_to_string(
            "components/_status_badge.html",
            {"status": status, "label": label},
        )
        assert f'status-badge--{status}' in html
        assert 'class="status-badge__icon" aria-hidden="true"' in html
        assert f'<span class="status-badge__label">{label}</span>' in html
        assert "<svg" in html

    assert "status-badge" not in render_to_string(
        "components/_status_badge.html",
        {"status": "unknown", "label": "未知"},
    )


def test_form_errors_render_focusable_summary_and_persistent_field_association():
    form = ExampleForm(data={})
    assert not form.is_valid()
    template = engines["django"].from_string(
        """
        {% include "components/_form_errors.html" with form=form summary_id="profile-errors" only %}
        <form>
          <label for="{{ form.display_name.id_for_label }}">{{ form.display_name.label }}</label>
          {{ form.display_name }}
          <span id="id_display_name_helptext">{{ form.display_name.help_text }}</span>
          {% include "components/_form_errors.html" with field=form.display_name only %}
        </form>
        """
    )

    html = template.render({"form": form})

    assert re.search(
        r'class="error-summary"\s+role="alert"\s+tabindex="-1"',
        html,
    )
    assert 'id="profile-errors"' in html
    assert 'aria-labelledby="profile-errors-title"' in html
    assert 'href="#id_display_name"' in html
    assert "请检查下面标出的内容。" in html
    assert "请输入患者称呼。" in html
    assert 'aria-invalid="true"' in html
    assert 'aria-describedby="id_display_name_helptext id_display_name_error"' in html
    assert 'class="field-errors" id="id_display_name_error"' in html
    assert html.count('id="id_display_name_error"') == 1


def test_empty_state_and_record_card_expose_generic_labelled_structures():
    empty_html = render_to_string(
        "components/_empty_state.html",
        {
            "heading_id": "recent-empty-title",
            "title": "暂时没有内容",
            "message": "内容出现后会显示在这里。",
            "action_url": "/create/",
            "action_label": "开始添加",
        },
    )
    assert 'class="empty-state" aria-labelledby="recent-empty-title"' in empty_html
    assert '<h2 class="empty-state__title" id="recent-empty-title">暂时没有内容</h2>' in empty_html
    assert 'href="/create/"' in empty_html
    assert "<svg" in empty_html

    card_html = render_to_string(
        "components/_record_card.html",
        {
            "heading_id": "record-42-title",
            "title": "一份资料",
            "url": "/item/42/",
            "date_value": "2026-08-22",
            "date_label": "2026年8月22日",
            "meta": "通用补充信息",
            "status": "saved",
            "status_label": "已保存",
            "action_label": "打开",
        },
    )
    assert 'class="record-card" aria-labelledby="record-42-title"' in card_html
    assert re.search(
        r'<time[^>]*datetime="2026-08-22"[^>]*>2026年8月22日</time>',
        card_html,
    )
    assert 'id="record-42-title"' in card_html
    assert 'href="/item/42/"' in card_html
    assert "通用补充信息" in card_html
    assert "status-badge--saved" in card_html
    assert "打开" in card_html


def test_component_templates_have_no_external_assets_or_emoji_icons():
    templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(COMPONENTS_DIR.glob("*.html"))
    )

    assert "http://" not in templates
    assert "https://" not in templates
    assert "<img" not in templates
    assert not any(0x1F300 <= ord(character) <= 0x1FAFF for character in templates)
