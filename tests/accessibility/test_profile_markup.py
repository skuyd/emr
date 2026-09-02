from pathlib import Path
import re

import pytest

from tests.documents.test_detail_viewer import _patient


pytestmark = pytest.mark.django_db
ROOT = Path(__file__).parents[2]


def test_profile_markup_has_one_heading_and_named_sections(django_user_model):
    client, _patient_record = _patient(django_user_model, "markup")
    content = client.get("/me/").content.decode()

    assert content.count("<h1") == 1
    for heading_id in (
        "profile-title",
        "profile-archive-title",
        "profile-preferences-title",
        "notifications-title",
        "profile-privacy-title",
        "logout-title",
        "account-data-title",
    ):
        assert f'id="{heading_id}"' in content
    assert 'class="profile-danger-zone"' in content
    assert 'aria-labelledby="account-data-title"' in content
    assert 'method="post" action="/logout/"' in content


def test_profile_styles_cover_touch_focus_forced_colors_and_narrow_containment():
    css = (ROOT / "static/css/profile.css").read_text(encoding="utf-8")

    assert re.search(r"\.profile-card[^\{]*\{[^}]*min-width:\s*0", css)
    assert "var(--color-surface)" in css
    assert "var(--color-primary)" in css
    assert "var(--color-danger)" in css
    assert re.search(r"\.profile-card button\s*\{[^}]*min-height:\s*44px", css)
    assert re.search(r"\.profile-card input[^}]*min-height:\s*44px", css)
    assert re.search(r"\.profile-danger-zone[^\{]*\{[^}]*min-width:\s*0", css)
    assert ":focus-visible" in css and "outline: 3px solid var(--color-focus)" in css
    assert "@media (forced-colors: active)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "overflow-wrap: anywhere" in css


def _profile_contrast_tokens():
    tokens = (ROOT / "static/css/tokens.css").read_text(encoding="utf-8")
    return dict(re.findall(r"(--color-(?:paper|surface|ink|primary)):\s*(#[0-9a-fA-F]{6})", tokens))


def _contrast(first, second):
    def relative_luminance(hex_color):
        channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [channel / 12.92 if channel <= .03928 else ((channel + .055) / 1.055) ** 2.4 for channel in channels]
        return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]

    first_lum, second_lum = relative_luminance(first), relative_luminance(second)
    return (max(first_lum, second_lum) + .05) / (min(first_lum, second_lum) + .05)


def test_profile_heading_text_keeps_aa_safe_color_contract():
    css = (ROOT / "static/css/profile.css").read_text(encoding="utf-8")

    heading_rule = re.search(r"\.profile-heading p\s*\{([^}]*)\}", css)
    assert heading_rule and "color: var(--color-ink)" in heading_rule.group(1)

    colors = _profile_contrast_tokens()
    assert _contrast(colors["--color-ink"], colors["--color-paper"]) >= 4.5


def test_profile_primary_action_keeps_aa_safe_global_text_color():
    css = (ROOT / "static/css/profile.css").read_text(encoding="utf-8")
    components = (ROOT / "static/css/components.css").read_text(encoding="utf-8")

    primary_rule = re.search(r"\.profile-card \.button--primary\s*\{([^}]*)\}", css)
    assert primary_rule and not re.search(r"(?:^|;)\s*color\s*:", primary_rule.group(1))
    global_primary = re.search(r"\.button--primary\s*\{([^}]*)\}", components)
    assert global_primary and "color: #fff" in global_primary.group(1)

    colors = _profile_contrast_tokens()
    assert _contrast("#ffffff", colors["--color-primary"]) >= 4.5


def test_profile_script_keeps_no_js_form_and_click_only_permission_contract():
    script = (ROOT / "static/js/profile.js").read_text(encoding="utf-8")

    assert 'form.addEventListener("submit"' in script
    assert "Notification.requestPermission()" in script
    assert script.index('form.addEventListener("submit"') < script.index("Notification.requestPermission()")
    assert "innerHTML" not in script
    assert "data-notification-form" in (ROOT / "templates/patients/profile.html").read_text(encoding="utf-8")
