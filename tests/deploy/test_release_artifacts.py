import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_playwright_package_is_locked_and_distinguishes_webkit_from_real_safari():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    config = (ROOT / "playwright.config.ts").read_text(encoding="utf-8")

    assert package["private"] is True
    assert package["devDependencies"]["@playwright/test"] == "1.62.1"
    assert lock["packages"]["node_modules/@playwright/test"]["version"] == "1.62.1"
    assert 'channel: "chrome"' in config
    assert 'channel: "msedge"' in config
    assert 'name: "webkit-reference"' in config
    assert "never accepted as real" in config


def test_full_e2e_contract_covers_pages_viewports_runtime_failures_and_destructive_confirmation():
    source = (ROOT / "tests" / "e2e" / "phr-v1.spec.ts").read_text(encoding="utf-8")

    for route in (
        '"/login/"',
        '"/onboarding/"',
        '"/"',
        '"/uploads/new/"',
        '"/records/"',
        '"/me/"',
        "/viewer/",
        "/trends/",
        "/delete/",
    ):
        assert route in source
    assert "1280" in source and "720" in source
    assert "1440" in source and "900" in source
    assert "scrollWidth <=" in source
    assert 'message.type() === "error"' in source
    assert "response.status() >= 400" in source
    assert "domInteractive" in source and "3000" in source
    assert "PHR_E2E_DELETE_DOCUMENT_ID" in source
    assert "expect(removed.status()).toBe(404)" in source
    assert "test.skip" not in source
    assert "release browser checks must not be skipped" in source


def test_k6_contract_enforces_fixed_scale_percentiles_zero_failures_and_first_result_limit():
    source = (ROOT / "tests" / "performance" / "k6-upload-search.js").read_text(encoding="utf-8")

    assert "vus: 100" in source
    assert "vus: 10" in source
    assert "session.document_count >= 300" in source
    assert "totalPages / totalFiles === 3" in source
    assert "batch.declaredPages === 60" in source
    assert 'request_failures: ["rate==0"]' in source
    assert 'first_result_failures: ["rate==0"]' in source
    assert 'search_duration: ["p(95)<2000"]' in source
    assert 'viewer_first_page_duration: ["p(95)<3000"]' in source
    assert '"min", "med", "p(95)", "p(99)", "max"' in source
    assert "300000" in source


def test_release_documents_keep_unexecuted_external_checks_blocked():
    for relative in (
        "docs/deployment/production-runbook.md",
        "docs/verification/browser-accessibility.md",
        "docs/verification/performance.md",
        "docs/verification/backup-restore.md",
        "docs/verification/release-gate.md",
    ):
        assert (ROOT / relative).is_file()
    performance = (ROOT / "docs/verification/performance.md").read_text(encoding="utf-8")
    backup = (ROOT / "docs/verification/backup-restore.md").read_text(encoding="utf-8")
    gate = (ROOT / "docs/verification/release-gate.md").read_text(encoding="utf-8")

    assert "没有 P50/P95/P99 数字" in performance
    assert "尚未执行一次端到端" in backup
    assert "**结论：BLOCKED**" in gate
