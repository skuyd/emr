from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "check_conventional_commit.py"


def run_check(title, body=""):
    return subprocess.run(
        [sys.executable, str(SCRIPT), title, "--body", body],
        capture_output=True,
        text=True,
        check=False,
    )


def test_feature_title_with_chinese_description_is_accepted():
    result = run_check("feat: 新增报告导出功能")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Conventional commit is valid: feat (minor)\n"


@pytest.mark.parametrize(
    ("title", "body", "expected_type", "expected_bump"),
    [
        ("fix(records): 修复报告排序问题", "", "fix", "patch"),
        ("perf: 缩短病历检索时间", "", "perf", "patch"),
        ("feat(auth)!: 调整登录凭据格式", "", "feat", "major"),
        (
            "refactor: 调整病历解析器",
            "BREAKING CHANGE: 不再接受旧格式。",
            "refactor",
            "major",
        ),
        ("docs: 更新部署说明", "", "docs", "none"),
        ("test: 补充上传回归用例", "", "test", "none"),
        ("chore: 更新开发工具", "", "chore", "none"),
        ("ci: 调整持续集成缓存", "", "ci", "none"),
        ("refactor: 整理病历解析器", "", "refactor", "none"),
    ],
)
def test_supported_titles_report_the_release_impact(
    title,
    body,
    expected_type,
    expected_bump,
):
    result = run_check(title, body)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"Conventional commit is valid: {expected_type} ({expected_bump})\n"
    )


def test_breaking_footer_requires_the_spec_separator():
    result = run_check(
        "docs: 更新接口说明",
        "BREAKINGCHANGE: 这不是有效的 Conventional Commits footer。",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Conventional commit is valid: docs (none)\n"


@pytest.mark.parametrize(
    "title",
    [
        "feature: 新增报告导出功能",
        "feat 新增报告导出功能",
        "feat: add report export",
        "Feat: 新增报告导出功能",
        "feat(): 新增报告导出功能",
        "feat:  新增报告导出功能",
        "feat: 新增报告导出功能 ",
    ],
)
def test_invalid_titles_fail_with_actionable_guidance(title):
    result = run_check(title)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "ERROR: PR title must match '<type>(<scope>)!: 中文描述'; "
        "allowed types: feat, fix, perf, docs, test, chore, ci, refactor\n"
    )
    assert "Traceback" not in result.stderr
