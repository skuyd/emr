# 自动版本号与 Changelog 流程

项目使用统一产品版本和三段式 SemVer：`MAJOR.MINOR.PATCH`。合并到 `main` 后，
[Release Please](https://github.com/googleapis/release-please) 会根据 Conventional Commits
由本地 `submit` 调用固定版本 CLI，自动计算下一版本、生成 Changelog、同步版本文件并创建
GitHub Release。日常提交不运行 GitHub Actions。操作入口见[本地提交指南](local-submit.md)。

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

1. Codex 的 `submit` 技能整理本任务变更，按实际差异选择本地验证，创建或更新功能 PR 并 Squash 合并。
   已识别的纯文档候选运行文档与一致性检查，其他候选保留完整验证，详见[验证范围](local-submit.md#验证范围和耗时)。
   本地工具复核候选 head、主线基线和验证源码，合并前若主线前进则停止，更新后重新验证。
2. Release Please 解析自上次版本以来的提交。只有 `feat`、`fix`、`perf` 或不兼容变更会触发新版本。
3. 机器人创建或更新发布 PR，并同步以下文件：
   - `VERSION`
   - `.release-please-manifest.json`
   - `pyproject.toml`
   - `package.json`
   - `package-lock.json`
   - `CHANGELOG.md`
4. 本地验证引擎检查发布 PR 的实际差异；只有允许字段内的版本变化、新增 Changelog 及已识别文档变化才能复用
   已通过的业务回归。发布契约、版本一致性、文档及最终镜像构建和 smoke 仍运行，然后 Squash 合并。
   先确定实际发布候选，再检查可复用凭据；不对同一次流程中的中间主线额外做一轮全量。
5. 本地 CLI 的 `github-release` 创建 `v<版本号>` 标签和 GitHub Release。

该内部发布 PR 不需要重复人工确认。检查失败、必跑任务缺失或跳过时均阻止合并。网络中断后
再次调用同一分支的 `submit`，工具读取本地状态并核查真实 PR、提交、标签和 Release，避免
重复合并或重复升级版本。更改标题、正文、候选或主线后必须重新核对，不能沿用旧结论。

`release-please-config.json` 根级启用 `always-update`，即使新提交只修改文档或 CI、发布
说明未变化，也刷新已有候选及工作流输出。门禁确认候选提交真正包含当前 `main` 基线，
避免使用基于旧主分支的验证结果；没有待发布功能时，该选项不会单独创建新版本。

GitHub API 认证使用本机已有凭据或明确设置的 `GH_TOKEN` / `GITHUB_TOKEN`；令牌不写入状态文件、
日志、命令行或验证容器。发布 PR 合并时携带已验证的 head SHA，避免检查期间新增提交被直接合并。

## 本地检查

普通开发不需要填写 Changelog，也不应手工修改任何版本字段。修改发布配置或提交 PR 前运行：

```powershell
python tools/verify_release_automation.py
python tools/release_version.py check
python -m pytest tests/tools/test_conventional_commit.py `
  tests/tools/test_release_automation.py `
  tests/tools/test_release_version.py -q
```

`verify_release_automation.py` 验证 Release Please 配置、manifest、本地 npm 固定依赖、手动诊断
入口和全部版本副本；`release_version.py check` 验证当前版本与 Changelog 条目一致。

## GitHub 仓库一次性设置

本地自动发布需要以下设置；`submit` 会拒绝在 Actions 仍启用时推送或合并：

1. 准备能访问仓库并具有 Contents、Issues、Pull requests 读写权限的本机凭据；日常还需
   Administration 读取权限，用于确认 Actions 禁用状态。一次性切换 Actions 设置需要
   Administration 写入权限；提交工作流变更还需相应 Workflows 写入权限。
2. 在 **Settings → Actions → General** 禁用 Actions；退役云端发布工作流，CI 只留 `workflow_dispatch`
   诊断配方。手动诊断需先明确接受计费并临时启用 Actions，运行后再次禁用。
3. 检查现有保护规则，不应继续要求已经停用的 Actions 检查。不要为了合并静默绕过人工审核或
   降低既有保护。**Require branches to be up to date before merging** 依赖服务器检查配置，
   无法单独替代本地候选验证。过期的发布 PR 必须刷新并重新判断能否复用回归。
   本地公共 Git 目录锁只能防止同一克隆内并行 submit；无服务器端串行保护时，最终读取 `main`
   与发送合并请求之间仍有并发窗口。执行期间应避免其他克隆或用户同时合并。
4. 仅启用 Squash merging，并将默认 Squash commit message 设为 **Pull request title and description**，
   确保标题和 `BREAKING CHANGE:` 正文进入 `main`。
5. 核查仓库 webhook 和外部自动化。本流程不调用 Jenkins，也不执行生产部署。

首次运行使用 `release-please-config.json` 中的 `bootstrap-sha` 作为已有 `0.1.0` 基线；首次
自动版本生成后，Release Please 将使用已创建的版本标签继续计算，无需人工维护该 SHA。

GitHub Release 只表示源代码版本已经生成，不会自动部署。生产环境仍须先通过
[`docs/verification/release-gate.md`](../verification/release-gate.md)，部署时使用与 `VERSION`
一致的 `APP_IMAGE_TAG`。

## v1.18.0 等待预算修订交付

PR #86 精确 `ddede19` 经72项作者回归、34项非作者有界验证及准确CI通过，已Squash为
`c9b2a67`，包含于[v1.18.0](../releases/v1.18.0.md)。原20分钟候选等待实际超时、HTTP504
与主线变更拒绝分别保留于[发布证据](../verification/artifacts/release-v1-18-0.json)。该修订只延长
有限等待预算，不放宽候选/必跑作业门禁，也没有实现HTTP504重试。
