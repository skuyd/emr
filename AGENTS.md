# 仓库开发规则

## 提交与合并

- 面向 `main` 的 Pull Request 必须使用 Squash merge，使 PR 标题成为主分支提交标题。
- PR 标题必须使用 Conventional Commits：`<type>(<scope>)!: 中文描述`。`scope` 和 `!` 可省略。
- 可用类型仅限 `feat`、`fix`、`perf`、`docs`、`test`、`chore`、`ci`、`refactor`。
- 类型和可选 scope 使用小写英文；冒号后使用简明中文描述，例如 `feat(records): 新增报告导出功能`。
- 不兼容变更必须在类型或 scope 后加 `!`，例如 `feat(api)!: 调整报告接口格式`；需要补充说明时，可在 PR 正文加入 `BREAKING CHANGE:`。
- 提交或更新 PR 前运行 `python tools/check_conventional_commit.py "<PR 标题>" --body "<PR 正文>"`。

## 自动版本与 Changelog

- 合并到 `main` 后，由 Release Please 自动计算版本、生成 Changelog、合并发布 PR，并创建 `v<版本号>` 标签和 GitHub Release。
- `fix`、`perf` 升 PATCH；`feat` 升 MINOR；带 `!` 或 `BREAKING CHANGE:` 升 MAJOR。
- `docs`、`test`、`chore`、`ci`、`refactor` 不触发版本发布，也不进入发布记录。
- 普通功能分支不得手工修改 `VERSION`、`.release-please-manifest.json`、`pyproject.toml`、`package.json`、`package-lock.json` 中的版本字段，也不得手工填写 `CHANGELOG.md` 的自动生成区域。
- 修改发布自动化后必须运行 `python tools/verify_release_automation.py` 和 `python tools/release_version.py check`。
- GitHub Release 不代表生产环境已放行；生产部署仍必须通过 `docs/verification/release-gate.md` 中的门禁。
