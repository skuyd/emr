from pathlib import Path


E2E_SOURCE = Path("tests/e2e/health-home-warm-ui.spec.ts")


def test_expected_bad_request_navigation_scopes_console_filter_to_known_stages():
    source = E2E_SOURCE.read_text(encoding="utf-8")

    assert "gotoExpectedBadRequest" in source
    assert "expected400ConsoleBudget" in source
    assert "status of 400" in source
    assert "state.expected400ConsoleBudget -= 1" in source
    assert "gotoExpectedBadRequest(page, path, failures)" in source
    assert 'gotoExpectedBadRequest(page, "/login/forgot-password/new-password/", failures)' in source
    for path in (
        '"/login/first-use/verify/"',
        '"/login/first-use/password/"',
        '"/login/forgot-password/new-password/"',
    ):
        assert path in source
