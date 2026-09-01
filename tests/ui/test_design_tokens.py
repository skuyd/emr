import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOKENS_PATH = PROJECT_ROOT / "static" / "css" / "tokens.css"
COMPONENTS_PATH = PROJECT_ROOT / "static" / "css" / "components.css"

EXPECTED_TOKENS = {
    "--color-paper": "#f7f1e7",
    "--color-paper-deep": "#eee3d4",
    "--color-surface": "#fffcf7",
    "--color-ink": "#25322d",
    "--color-muted": "#6c766f",
    "--color-primary": "#b85c3f",
    "--color-primary-dark": "#8f402a",
    "--color-sage": "#47685b",
    "--color-sage-soft": "#dce7df",
    "--color-focus": "#8f402a",
    "--color-danger": "#8f2f24",
    "--color-warning-ink": "#674512",
    "--color-warning-bg": "#f8ebd3",
    "--color-success-ink": "#315f4c",
    "--color-success-bg": "#dce7df",
    "--font-display": 'Georgia, "Songti SC", "STSong", "SimSun", serif',
    "--font-body": '"Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
    "--space-1": ".5rem",
    "--space-2": "1rem",
    "--space-3": "1.5rem",
    "--space-4": "2rem",
    "--radius-control": "14px",
    "--radius-card": "22px",
    "--shadow-card": "0 18px 48px rgb(37 50 45 / 9%)",
    "--line": "rgb(49 64 58 / 14%)",
}


def _custom_properties(css):
    return {
        name.lower(): value.strip()
        for name, value in re.findall(
            r"(--[a-z0-9-]+)\s*:\s*([^;]+);",
            css,
            flags=re.IGNORECASE,
        )
    }


def _relative_luminance(color):
    channels = []
    for index in (1, 3, 5):
        channel = int(color[index:index + 2], 16) / 255
        channels.append(
            channel / 12.92
            if channel <= .04045
            else ((channel + .055) / 1.055) ** 2.4
        )
    return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]


def _contrast_ratio(foreground, background):
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (lighter + .05) / (darker + .05)


def _rule_declarations(css, selector):
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)}}", css, re.DOTALL)
    assert match, f"Missing CSS rule: {selector}"
    return dict(
        re.findall(r"([a-z-]+)\s*:\s*([^;]+);", match.group(1), re.IGNORECASE)
    )


def test_warm_design_tokens_are_exact_and_use_local_font_stacks():
    tokens = _custom_properties(TOKENS_PATH.read_text(encoding="utf-8"))

    assert {name: tokens.get(name) for name in EXPECTED_TOKENS} == EXPECTED_TOKENS

    source = TOKENS_PATH.read_text(encoding="utf-8").lower()
    assert "@import" not in source
    assert "url(" not in source


def test_shared_components_cover_controls_surfaces_states_and_accessibility_modes():
    css = COMPONENTS_PATH.read_text(encoding="utf-8").lower()

    for selector in (
        ".button",
        ".button--primary",
        ".button--secondary",
        ".button--text",
        ".button--danger",
        ".card",
        ".notice",
        ".field",
        ".error-summary",
        ".field-errors",
        ".empty-state",
        ".record-card",
        ".brand-lockup",
        ".visually-hidden",
    ):
        assert selector in css

    for state in ("processing", "saved", "organized", "original", "failed"):
        assert f".status-badge--{state}" in css

    assert re.search(r"\.button\s*\{[^}]*min-height:\s*44px", css, re.DOTALL)
    assert re.search(r"\.button\s*\{[^}]*min-width:\s*44px", css, re.DOTALL)
    assert re.search(r"\.field\s+:is\([^}]+min-height:\s*44px", css, re.DOTALL)
    assert ":focus-visible" in css
    assert "outline: 3px solid var(--color-focus)" in css
    assert "@media (forced-colors: active)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@import" not in css
    assert "url(" not in css


def test_small_secondary_copy_without_its_own_surface_meets_aa_on_paper():
    tokens = _custom_properties(TOKENS_PATH.read_text(encoding="utf-8"))
    css = COMPONENTS_PATH.read_text(encoding="utf-8")

    for selector in (".brand-lockup__tagline", ".field__help"):
        color_value = _rule_declarations(css, selector)["color"]
        token_name = re.fullmatch(r"var\((--[a-z-]+)\)", color_value).group(1)
        assert _contrast_ratio(tokens[token_name], tokens["--color-paper"]) >= 4.5
