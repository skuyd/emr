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


def test_profile_script_keeps_no_js_form_and_click_only_permission_contract():
    script = (ROOT / "static/js/profile.js").read_text(encoding="utf-8")

    assert 'form.addEventListener("submit"' in script
    assert "Notification.requestPermission()" in script
    assert script.index('form.addEventListener("submit"') < script.index("Notification.requestPermission()")
    assert "innerHTML" not in script
    assert "data-notification-form" in (ROOT / "templates/patients/profile.html").read_text(encoding="utf-8")
