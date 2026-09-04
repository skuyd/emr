# 自动版本号与 Changelog 流程

项目使用统一产品版本和三段式 SemVer：`MAJOR.MINOR.PATCH`。合并到 `main` 后，
[Release Please](https://github.com/googleapis/release-please) 会根据 Conventional Commits
自动计算下一版本、生成 Changelog、同步版本文件并创建 GitHub Release。

根目录 [`VERSION`](../VERSION) 是应用读取的规范版本来源；其中的
`x-release-please-version` 注释是自动更新标记。请通过以下命令读取版本，不要直接解析文件文本：

```powershell
python tools/release_version.py show
```

## Pull Request 标题规则

面向 `main` 的 PR 使用以下格式，并以 Squash merge 合并：

```text
<type>(<scope>)!: 中文描述
```

`scope` 和 `!` 可省略。例如：

```text
feat(records): 新增报告导出功能
fix(viewer): 修复影像翻页状态丢失
feat(api)!: 调整报告接口格式
docs: 更新部署说明
```

| 提交类型 | 版本影响 | Changelog |
| --- | --- | --- |
| `fix`、`perf` | PATCH | 分别进入“修复”“性能” |
| `feat` | MINOR | 进入“新增” |
| 任意允许类型带 `!` 或正文包含 `BREAKING CHANGE:` | MAJOR | 标记为不兼容变更 |
| `docs`、`test`、`chore`、`ci`、`refactor` | 不发布 | 隐藏 |

类型与 scope 使用小写英文，冒号后的描述至少包含中文。可在本地验证 PR 标题：

```powershell
python tools/check_conventional_commit.py "feat(records): 新增报告导出功能"
```

不兼容变更可以同时传入 PR 正文验证：

```powershell
python tools/check_conventional_commit.py `
  "refactor(records): 调整报告解析接口" `
  --body "BREAKING CHANGE: 不再接受旧版报告结构。"
```

## 自动发布过程

1. PR 合并到 `main` 后，`.github/workflows/release.yml` 启动 Release Please。
2. Release Please 解析自上次版本以来的提交。只有 `feat`、`fix`、`perf` 或不兼容变更会触发新版本。
3. 机器人创建或更新发布 PR，并同步以下文件：
   - `VERSION`
   - `.release-please-manifest.json`
   - `pyproject.toml`
   - `package.json`
   - `package-lock.json`
   - `CHANGELOG.md`
4. 发布 PR 的必需 CI 检查通过后，GitHub 自动以 Squash merge 合并。
5. 合并产生的新一次 `main` 推送会创建 `v<版本号>` 标签和 GitHub Release。

该内部发布 PR 不需要人工确认。CI 失败、版本字段漂移、配置无效或自动合并未启用时，
流程会停在发布 PR，不会创建标签。

## 本地检查

普通开发不需要填写 Changelog，也不应手工修改任何版本字段。修改发布配置或提交 PR 前运行：

```powershell
python tools/verify_release_automation.py
python tools/release_version.py check
python -m pytest tests/tools/test_conventional_commit.py `
  tests/tools/test_release_automation.py `
  tests/tools/test_release_version.py -q
```

`verify_release_automation.py` 会验证 Release Please 配置、manifest、Action 固定提交、自动合并、
CI 入口和全部版本副本；`release_version.py check` 会验证当前版本与 Changelog 条目一致。

## GitHub 仓库一次性设置

自动发布需要仓库管理员完成以下设置：

1. 创建能够访问本仓库的 fine-grained Personal Access Token，至少授予 Contents、Issues 和
   Pull requests 读写权限，并保存为 Actions secret `RELEASE_PLEASE_TOKEN`。
2. 在 **Settings → General → Pull Requests** 启用 **Allow auto-merge**。
3. 在 `main` 分支保护规则中要求发布 PR 通过 `CI / conventional-title` 与 `CI / test`，并启用
   **Require branches to be up to date before merging**（或使用 merge queue）。这会让 Release Please
   在其他 PR 先合并后更新过期的发布 PR、重新计算版本和 Changelog，并重新运行 CI，避免旧候选版本
   被合并后给未纳入版本计算的新代码打标签。若现有规则要求人工审核，需要取消该要求，否则全自动
   发布会停在发布 PR。
4. 仅启用 Squash merging，并将默认 Squash commit message 设为 **Pull request title and description**，
   确保标题和 `BREAKING CHANGE:` 正文进入 `main`。
5. 在 **Settings → Actions → General** 允许 Actions 创建 Pull Request。

首次运行使用 `release-please-config.json` 中的 `bootstrap-sha` 作为已有 `0.1.0` 基线；首次
自动版本生成后，Release Please 将使用已创建的版本标签继续计算，无需人工维护该 SHA。

GitHub Release 只表示源代码版本已经生成，不会自动部署。生产环境仍须先通过
[`docs/verification/release-gate.md`](../verification/release-gate.md)，部署时使用与 `VERSION`
一致的 `APP_IMAGE_TAG`。
