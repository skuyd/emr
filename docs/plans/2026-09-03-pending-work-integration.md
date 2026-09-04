# Pending Work Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将旧工作区中仍有效的 Release Please、本地环境初始化和本地处理 Worker 改动拆成三个可独立验证的 PR，并按仓库规则 Squash merge 到 `main`。

**Architecture:** 三个分支都从最新 `origin/main` 建立，避免携带旧分支相对主分支的回退。每个行为变更先迁移测试并观察预期失败，再加入最小实现；三个 PR 独立审核、独立验证，全部合并后才清理原工作区。

**Tech Stack:** Git/GitHub Actions、Python 3.11、Django 5.2、pytest、Node.js 22 test runner

**Spec:** `docs/specs/2026-09-03-pending-work-integration-design.md`

## Global Constraints

- 面向 `main` 的 PR 必须使用 Squash merge，PR 标题成为主分支提交标题。
- 三个任务中列出的确切 PR 标题和正文必须分别通过 `tools/check_conventional_commit.py`。
- 不直接修改任何版本字段或 `CHANGELOG.md` 自动生成区域。
- Release Please action 必须固定到 40 位提交 SHA，不使用可变的 `v5` 引用。
- 本地 Worker 必须拒绝生产配置，并继续使用 `run_processing` 的租约和幂等保护。
- 在确认 `main` 含有对应结果前，不清理原始 `codex/phr-v1` 工作区。
- 不读取、输出、提交或覆盖 `.env` 中的任何秘密。

---

### Task 1: Upgrade Release Please to the Node.js 24 action runtime

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `tools/verify_release_automation.py`
- Test: `tests/tools/test_release_automation.py`
- Test: `tests/tools/test_release_version.py`

**Interfaces:**
- Consumes: `verify_release_automation.py` 对工作流 action 固定值的验证入口。
- Produces: `RELEASE_PLEASE_COMMIT = "45996ed1f6d02564a971a2fa1b5860e934307cf7"` 和使用同一 SHA 的工作流。

- [ ] **Step 1: 先更新测试中的 v5 固定值**

  将测试常量设置为：

  ```python
  RELEASE_PLEASE_COMMIT = "45996ed1f6d02564a971a2fa1b5860e934307cf7"
  RELEASE_PLEASE_TAG_OBJECT = "0dfd8538845b8e92600d271a895a5372865d4062"
  ```

  可变引用用例把工作流引用替换成 `googleapis/release-please-action@v5`，并断言验证器拒绝该引用。

- [ ] **Step 2: 运行测试并确认旧验证器拒绝 v5 SHA**

  Run: `python -m pytest tests/tools/test_release_automation.py::test_complete_automatic_release_configuration_is_accepted -q`

  Expected: FAIL，错误指出工作流必须固定到旧的 Release Please 提交。

- [ ] **Step 3: 更新验证器和工作流**

  在验证器中写入 v5.0.0 的提交 SHA，并把工作流步骤改为：

  ```yaml
  uses: googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7 # v5.0.0
  ```

- [ ] **Step 4: 验证发布自动化**

  Run:

  ```powershell
  python -m pytest tests/tools/test_release_automation.py -q
  python tools/verify_release_automation.py
  python tools/release_version.py check
  ```

  Expected: 10 个发布自动化测试通过，两个工具均以退出码 0 完成。

- [ ] **Step 5: 修复自动升版后的仓库级断言**

  在两个发布测试文件中，从根目录 `VERSION` 读取 `#` 注释前的 SemVer；只让
  `test_repository_release_automation_is_consistent` 和
  `test_repository_release_metadata_is_consistent` 使用该值。临时仓库夹具的
  `0.1.0` 断言保持不变。运行：

  ```powershell
  python -m pytest tests/tools/test_conventional_commit.py tests/tools/test_release_automation.py tests/tools/test_release_version.py -q
  ```

  Expected: 62 个发布工具测试通过。

- [ ] **Step 6: 验证标题并提交**

  Run:

  ```powershell
  python tools/check_conventional_commit.py "ci(actions): 升级 Release Please 至 Node.js 24" --body "将 release-please-action 升级至 v5.0.0，并保持不可变提交固定与自动化校验。"
  git add .github/workflows/release.yml tools/verify_release_automation.py tests/tools/test_release_automation.py tests/tools/test_release_version.py
  git commit -m "ci(actions): 升级 Release Please 至 Node.js 24"
  ```

### Task 2: Replace every supported local secret placeholder

**Files:**
- Modify: `deploy/bootstrap_dev_env.py`
- Test: `tests/deploy/test_bootstrap_dev_env.py`

**Interfaces:**
- Consumes: `bootstrap(example_path: Path, env_path: Path) -> bool`。
- Produces: `_generated_values(source: dict[str, str]) -> dict[str, str]` 为所有已识别占位值生成独立随机值。

- [ ] **Step 1: 添加两个行为测试**

  新增测试分别用 `NOTIFICATIONS_CRYPTO_SECRET=change-me-before-deployment` 和
  `OPERATIONS_METRICS_TOKEN=change-me-before-deployment-at-least-32-characters`
  生成临时 `.env`，断言输出不再等于占位值且长度至少为 32。

- [ ] **Step 2: 运行新测试并确认占位值仍会泄漏到输出**

  Run:

  ```powershell
  python -m pytest tests/deploy/test_bootstrap_dev_env.py::test_bootstrap_replaces_placeholder_for_derived_application_secret tests/deploy/test_bootstrap_dev_env.py::test_bootstrap_replaces_extended_operations_token_placeholder -q
  ```

  Expected: 两个测试均 FAIL，生成结果仍等于各自占位值。

- [ ] **Step 3: 实现通用占位值替换**

  把加长运维占位值加入 `_PLACEHOLDERS`，并在派生连接字符串之前替换 source 中仍为
  已识别占位值的字段：

  ```python
  for key, value in tuple(values.items()):
      if value.strip() in _PLACEHOLDERS:
          values[key] = _replacement()
  ```

- [ ] **Step 4: 运行 bootstrap 测试**

  Run: `python -m pytest tests/deploy/test_bootstrap_dev_env.py -q`

  Expected: 4 个测试通过，包括已有文件不被覆盖的竞争条件用例。

- [ ] **Step 5: 验证标题并提交**

  Run:

  ```powershell
  python tools/check_conventional_commit.py "fix(dev): 补全本地环境密钥初始化" --body "首次生成 .env 时替换全部已知秘密占位值，同时继续保护已有配置。"
  git add deploy/bootstrap_dev_env.py tests/deploy/test_bootstrap_dev_env.py
  git commit -m "fix(dev): 补全本地环境密钥初始化"
  ```

### Task 3: Add the local durable processing worker

**Files:**
- Create: `apps/processing/management/__init__.py`
- Create: `apps/processing/management/commands/__init__.py`
- Create: `apps/processing/management/commands/run_local_processing_worker.py`
- Create: `tests/processing/test_local_worker.py`
- Create: `README.md`
- Create: `docs/specs/2026-09-03-pending-work-integration-design.md`
- Create: `docs/plans/2026-09-03-pending-work-integration.md`

**Interfaces:**
- Consumes: `tasks.get_processing_pipeline()`、`run_processing(run_id, pipeline)`、`ProcessingRun.next_retry_at`。
- Produces: Django 命令 `python manage.py run_local_processing_worker [--once] [--poll-interval N] [--limit N]`。

- [ ] **Step 1: 先加入 Worker 行为测试**

  测试创建一个立即到期任务、一个已到期重试和一个未来重试。执行
  `call_command("run_local_processing_worker", once=True)` 后，断言前两个任务进入
  `NO_STRUCTURED_RESULT`，未来重试仍为 `QUEUED`，并断言同一批任务只创建一个
  pipeline。第二个测试在生产设置下断言命令抛出包含 `local development` 的
  `CommandError`。第三个测试把文档标记为待删除，断言 Worker 不初始化 pipeline、
  不执行其关联任务，避免持续重选无法取得租约的队列项。

- [ ] **Step 2: 运行测试并确认管理命令不存在**

  Run: `python -m pytest tests/processing/test_local_worker.py -q`

  Expected: FAIL，Django 报告 `Unknown command: 'run_local_processing_worker'`。

- [ ] **Step 3: 实现最小本地 Worker**

  `_due_run_ids(limit)` 查询 `stage=QUEUED`、文档未进入删除流程，且
  `next_retry_at` 为空或不晚于当前时间的任务，按 `created_at, pk` 排序并限制数量。
  命令验证本地设置和参数范围，批次存在时只初始化一次 pipeline，逐个调用
  `run_processing`，`--once` 完成后返回，空队列时通过
  `time.sleep(poll_interval)` 等待。

- [ ] **Step 4: 添加并核对项目入口文档**

  `README.md` 说明产品边界、目录结构、首次本地环境初始化、服务启动、数据库迁移、
  `runserver`、本地 Worker、Celery 替代方式、常用验证命令和生产 release gate。
  所有内部 Markdown 链接必须指向仓库内存在的文件。

- [ ] **Step 5: 验证 Worker 和项目配置**

  Run:

  ```powershell
  python -m pytest tests/processing/test_local_worker.py tests/processing/test_runner.py tests/processing/test_tasks.py -q
  python manage.py check --settings=config.settings.test
  python tools/verify_traceability.py
  ```

  Expected: 测试和两个检查均以退出码 0 完成。

- [ ] **Step 6: 验证标题并提交**

  Run:

  ```powershell
  python tools/check_conventional_commit.py "feat(processing): 新增本地处理工作进程" --body "增加仅限开发环境的持久化处理 Worker，并补充项目入口和本地运行说明。"
  git add README.md apps/processing/management tests/processing/test_local_worker.py docs/specs/2026-09-03-pending-work-integration-design.md docs/plans/2026-09-03-pending-work-integration.md
  git commit -m "feat(processing): 新增本地处理工作进程"
  ```

### Task 4: Review and verify every PR candidate

**Files:**
- Inspect: each branch diff from `origin/main`

**Interfaces:**
- Consumes: 三个已提交候选分支。
- Produces: 可创建 PR 的验证证据，不修改业务接口。

- [ ] **Step 1: 检查每个分支只包含计划内路径**

  Run: `git diff --name-status origin/main...HEAD`

  Expected: 每个分支只列出其任务的 Files 清单；Release、bootstrap 和 Worker 之间无交叉文件。

- [ ] **Step 2: 检查补丁格式和秘密泄漏风险**

  Run:

  ```powershell
  git diff --check origin/main...HEAD
  git grep -n "RELEASE_PLEASE_TOKEN=" HEAD
  ```

  Expected: `git diff --check` 退出码为 0；仓库不包含为该 secret 赋值的明文。

- [ ] **Step 3: 在每个候选分支运行完整 Python 测试**

  Run: `python -m pytest -q`

  Expected: 退出码为 0；仅保留明确依赖外部环境的跳过项。

- [ ] **Step 4: 自审行为、错误路径和回退风险**

  对照设计逐项检查：action SHA 一致、占位值不泄漏、Worker 生产限制有效、README
  不宣称已生产放行、UI 文件不在任何候选 diff 中。发现 Critical 或 Important 问题时
  返回对应任务，以测试先行修复并重新验证。

### Task 5: Create and squash-merge the pull requests

**Files:**
- Inspect: `.github/pull_request_template.md`
- Inspect: GitHub PR and check state

**Interfaces:**
- Consumes: 三个已验证且已推送的分支。
- Produces: `main` 上三个符合 Conventional Commits 的 Squash 提交。

- [ ] **Step 1: 推送三个候选分支**

  分别在对应工作树运行：

  ```powershell
  git push -u origin codex/release-please-node24
  git push -u origin codex/dev-env-secrets
  git push -u origin codex/local-processing-worker
  ```

  Expected: 每个远端分支指向本地候选提交，不使用 force push。

- [ ] **Step 2: 创建面向 main 的 PR**

  从 Git Credential Manager 读取 GitHub 凭据但不输出令牌，通过
  `POST https://api.github.com/repos/skuyd/emr/pulls` 分别提交任务规定的标题和正文，
  `base` 固定为 `main`，`head` 依次为三个确切的 `codex/*` 分支。记录 API 返回的
  PR 编号和 HTML URL，并确认仓库保护和必需检查已开始运行。

- [ ] **Step 3: 等待并核验必需检查**

  逐个读取 PR 检查结果；任何失败都回到对应工作树定位和修复。不得把 pending、
  skipped 或 cancelled 当作通过。

- [ ] **Step 4: 使用 Squash merge 合并**

  按 PR 1、PR 2、PR 3 顺序合并，合并标题保持对应 Conventional Commit 标题。
  每次合并后确认远端 `main` 前进到 GitHub 返回的 merge commit。

- [ ] **Step 5: 验证最终 main**

  在 `main` 工作树拉取最新提交后运行：

  ```powershell
  python -m pytest -q
  npm ci
  npm run test:js
  python tools/verify_release_automation.py
  python tools/release_version.py check
  ```

  Expected: 所有命令退出码为 0，并确认 Release Please 工作流使用 v5.0.0 固定 SHA。

### Task 6: Reconcile the original dirty workspace

**Files:**
- Reconcile: `.github/workflows/release.yml`
- Reconcile: `deploy/bootstrap_dev_env.py`
- Reconcile: `static/css/viewer.css`
- Reconcile: `tests/browser/test_ac02_upload_browser.py`
- Reconcile: `tests/deploy/test_bootstrap_dev_env.py`
- Reconcile: `tests/tools/test_release_automation.py`
- Reconcile: `tools/verify_release_automation.py`
- Reconcile: `README.md`
- Reconcile: `apps/processing/management/`
- Reconcile: `tests/processing/test_local_worker.py`

**Interfaces:**
- Consumes: 已验证的远端 `main`。
- Produces: 不再含重复源码改动的原始工作区，且保留所有忽略文件和私密本地状态。

- [ ] **Step 1: 逐文件证明有效内容已经存在于 main**

  对每个有效补丁逐个读取 `git show origin/main:文件路径`；UI 两个路径确认主分支
  版本包含原修复且保留后来增强。任何不一致都停止清理。

- [ ] **Step 2: 只清理已证明可恢复的确切路径**

  恢复七个 tracked 文件到当前旧分支的 HEAD，并删除五个已由 `main` 保存的 untracked
  路径。不得使用递归清理整个仓库，不得触碰 `.env`、`.venv`、`node_modules` 或未知
  untracked 文件。

- [ ] **Step 3: 验证最终工作区状态**

  Run: `git status --short`

  Expected: 本次列出的源码改动消失；若出现其他未知改动，保留并报告，不自动删除。
