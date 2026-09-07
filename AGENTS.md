# 仓库开发规则

## 功能开发分支

- 每次开始新的功能开发，都必须先获取远端 `origin/main` 的最新状态，并从最新 `main` 基线创建独立功能分支。
- 新功能不得以其他尚未合并的功能、修复或文档分支作为起点；已有工作的后续修改仍在其原功能分支内完成。
- 当前目录有未提交改动或存在多个 Codex 并行任务时，应从最新 `main` 创建独立 Git worktree 开发新功能，保留其他任务的分支和工作区。

## 提交与合并

- 面向 `main` 的 Pull Request 必须使用 Squash merge，使 PR 标题成为主分支提交标题。
- PR 标题必须使用 Conventional Commits：`<type>(<scope>)!: 中文描述`。`scope` 和 `!` 可省略。
- 可用类型仅限 `feat`、`fix`、`perf`、`docs`、`test`、`chore`、`ci`、`refactor`。
- 类型和可选 scope 使用小写英文；冒号后使用简明中文描述，例如 `feat(records): 新增报告导出功能`。
- 不兼容变更必须在类型或 scope 后加 `!`，例如 `feat(api)!: 调整报告接口格式`；需要补充说明时，可在 PR 正文加入 `BREAKING CHANGE:`。
- 提交或更新 PR 前运行 `python tools/check_conventional_commit.py "<PR 标题>" --body "<PR 正文>"`。

## 腾讯云部署资料（仅限本地）

- 腾讯云部署相关资料统一存放在本项目主工作区的
  `docs/deployment/local/tencent-cloud/`。使用 Git worktree 时，仍使用这一处目录，
  不在各 worktree 或用户 AppData 中另存一套。
- 该目录包含访问说明（`trial-access.txt`）、账号和凭据、环境文件、云资源配置、
  腾讯云专用部署脚本与适配补丁、CLI 配置和日志、部署文档、截图、验证证据及归档。
  不得提交或推送到任何远程分支，也不得上传到 PR/Issue 正文或附件。
- 必须保留对应的 Git 忽略规则；禁止使用 `git add -f` 绕过。提交前检查暂存区，
  推送前检查本次新增提交的历史，不能仅凭工作区已删除文件就认定历史安全。
- 已有部署资料迁入该目录时，先校验文件完整性并更新本地脚本中的路径，再移除旧副本。
  清理相关分支或 worktree 前，须先在该目录保留未合并的本地部署内容。
- 可复用应用源码、自动化测试和仓库规则按正常流程合并；其中不得包含实际云资源地址、
  标识、部署账号、凭据或本地部署证据。仅限本地的资料不进入文档登记表，
  提交到远程的文档和 CI 不得依赖这些私有文件存在。

## 自动版本与 Changelog

- 合并到 `main` 后，由 Release Please 自动计算版本、生成 Changelog、合并发布 PR，并创建 `v<版本号>` 标签和 GitHub Release。
- `fix`、`perf` 升 PATCH；`feat` 升 MINOR；带 `!` 或 `BREAKING CHANGE:` 升 MAJOR。
- `docs`、`test`、`chore`、`ci`、`refactor` 不触发版本发布，也不进入发布记录。
- 普通功能分支不得手工修改 `VERSION`、`.release-please-manifest.json`、`pyproject.toml`、`package.json`、`package-lock.json` 中的版本字段，也不得手工填写 `CHANGELOG.md` 的自动生成区域。
- 修改发布自动化后必须运行 `python tools/verify_release_automation.py` 和 `python tools/release_version.py check`。
- GitHub Release 不代表生产环境已放行；生产部署仍必须通过 `docs/verification/release-gate.md` 中的门禁。

## 文档管理（强制）

- 任何 AI 或自动化代理在本仓库执行任务时都必须遵守
  `docs/policies/document-governance.md`；查询文档现状时从 `docs/README.md` 和
  `docs/document-registry.json` 开始，不得通过文件日期或历史计划复选框猜测进度。
- 除 `README.md`、`CHANGELOG.md`、`AGENTS.md`、`LICENSE*`、`NOTICE*`，以及
  `.github/pull_request_template.md`、`.github/PULL_REQUEST_TEMPLATE/**/*.md` 和
  `.github/ISSUE_TEMPLATE/**/*.md` 平台模板外，Markdown 文档必须存放在 `docs/`。
- 新文档按用途进入 `docs/product/`、`docs/decisions/`、`docs/specs/`、`docs/plans/`、
  `docs/policies/`、`docs/releases/`、`docs/verification/`、`docs/deployment/`、
  `docs/licenses/` 或 `docs/archive/`；不得在根目录、源码、测试或 `deploy/` 中建立临时
  需求、设计、计划、报告或交接文档。
- 新规格、计划和决策记录必须使用 `YYYY-MM-DD-kebab-case.md`；版本清单固定使用
  `vMAJOR.MINOR.PATCH.md`；其他新文档必须使用小写英文 `kebab-case.md`，目录入口可使用
  `README.md`。只有治理规范列出的三个迁移文件可以保留历史文件名，不得新增例外。
- 新增、移动、取代、归档或删除文档时，必须同步更新 `docs/document-registry.json`、
  `docs/README.md` 和全部引用。若实际版本已经由 Release Please 确定，还要更新对应的
  `docs/releases/v<版本>.md`；版本未知时不得猜测。
- `lifecycle` 表示文档是否有效，`delivery` 表示功能交付状态。标记 `verified` 或
  `blocked` 必须提供真实证据；计划复选框只作为执行日志，不能覆盖登记表状态。
- 完成任何涉及文档、需求、版本、部署或验证证据的任务前，必须运行
  `python tools/verify_documentation.py`。若校验失败，不得声称任务完成。
