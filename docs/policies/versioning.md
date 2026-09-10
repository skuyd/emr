# 自动版本号与 Changelog 流程

项目使用统一产品版本和三段式 SemVer：`MAJOR.MINOR.PATCH`。合并到 `main` 后，
[Release Please](https://github.com/googleapis/release-please) 会根据 Conventional Commits
自动计算下一版本、生成 Changelog、同步版本文件并创建 GitHub Release。

根目录 [`VERSION`](../../VERSION) 是应用读取的规范版本来源；其中的
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
4. 工作流使用 `tools/merge_release_pr.py` 等待发布 PR 当前提交的最新 CI，确认标题、测试、
   PostgreSQL 并发和镜像构建四项均已实际通过，再以 Squash merge 合并，提交标题与 PR 一致。
5. 合并产生的新一次 `main` 推送会创建 `v<版本号>` 标签和 GitHub Release。

该内部发布 PR 不需要人工确认。检查尚未注册或仍在运行时继续等待，最多等待 90 分钟；
发布工作流总时限为 100 分钟，为创建候选与准备环境保留时间。CI 失败、
取消、超时、必跑任务缺失或跳过时均阻止合并。等待期间和合并请求前重新确认 PR 内容、
候选提交与 `main`：发生变化时停止本轮，由下一次主分支推送触发 Release Please 刷新候选。
临时网络失败或检查超时后，可在检查恢复后重新运行 Automatic release 工作流。

`release-please-config.json` 根级启用 `always-update`，即使新提交只修改文档或 CI、发布
说明未变化，也刷新已有候选及工作流输出。门禁确认候选提交真正包含当前 `main` 基线，
避免使用基于旧主分支的成功 CI；没有待发布功能时，该选项不会单独创建新版本。

读取检查使用工作流自带 token 的 `actions: read` 权限，创建发布 PR 与合并使用
`RELEASE_PLEASE_TOKEN`。执行门禁的源码固定为触发工作流的主分支提交，不检出待合并 PR。
发布 PR 合并时携带已验证的 head SHA，避免检查期间新增提交被直接合并。

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
3. 套餐支持时，在 `main` 分支保护规则中要求发布 PR 通过 `CI / conventional-title`、`CI / test`、
   `CI / postgres-concurrency` 与 `CI / container-build`，并启用
   **Require branches to be up to date before merging**（或使用 merge queue）。这会让 Release Please
   在其他 PR 先合并后更新过期的发布 PR、重新计算版本和 Changelog，并重新运行 CI，避免旧候选版本
   被合并后给未纳入版本计算的新代码打标签。若现有规则要求人工审核，需要取消该要求，否则全自动
   发布会停在发布 PR。当前私有仓库的分支保护 API 返回套餐限制 `403`；工作流显式等待 CI，
   不依赖 `gh pr merge --auto` 来等待检查。没有服务器端保护时，最终读取 `main` 与发送合并
   请求之间仍有并发窗口；自动发布期间应避免并行合并其他 PR，套餐升级后应补齐严格分支保护。
4. 仅启用 Squash merging，并将默认 Squash commit message 设为 **Pull request title and description**，
   确保标题和 `BREAKING CHANGE:` 正文进入 `main`。
5. 在 **Settings → Actions → General** 允许 Actions 创建 Pull Request。

首次运行使用 `release-please-config.json` 中的 `bootstrap-sha` 作为已有 `0.1.0` 基线；首次
自动版本生成后，Release Please 将使用已创建的版本标签继续计算，无需人工维护该 SHA。

GitHub Release 只表示源代码版本已经生成，不会自动部署。生产环境仍须先通过
[`docs/verification/release-gate.md`](../verification/release-gate.md)，部署时使用与 `VERSION`
一致的 `APP_IMAGE_TAG`。
