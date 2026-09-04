# 待提交工作集成设计

## 背景

`codex/phr-v1` 工作区同时包含三类尚未交付的有效改动，以及两项已经进入
`main` 的 UI 改动。该分支早于当前 `main`，直接提交整个工作区会把新的认证、
上传和首页验收逻辑回退到旧版本。

本次交付的目标是保留所有仍有价值的本地改动，通过面向 `main` 的独立 Pull
Request 审核和 Squash merge，同时不重复提交已在主分支中的内容。

## 交付边界

### PR 1：Release Please Node.js 24

标题为 `ci(actions): 升级 Release Please 至 Node.js 24`。

该 PR 只更新：

- `.github/workflows/release.yml` 中 Release Please action 的不可变提交固定值；
- `tools/verify_release_automation.py` 中允许的提交固定值；
- `tests/tools/test_release_automation.py` 中对应的正向和可变引用验证；
- `tests/tools/test_release_version.py` 中跟随权威 `VERSION` 的仓库版本断言。

固定版本为 `googleapis/release-please-action` v5.0.0，对应提交
`45996ed1f6d02564a971a2fa1b5860e934307cf7`。工作流继续使用
`RELEASE_PLEASE_TOKEN`，不在代码、日志或文档中记录令牌值。

仓库级发布测试不得把当前版本写死为 `0.1.0`。它们读取 `VERSION` 中注释前的
SemVer，确保 Release Please 自动升级版本后测试仍验证实际仓库状态；临时仓库夹具
继续使用手工确定的 `0.1.0` 预期值。

### PR 2：本地环境密钥初始化

标题为 `fix(dev): 补全本地环境密钥初始化`。

该 PR 修改 `deploy/bootstrap_dev_env.py`，确保 `.env.example` 中使用以下占位值的
应用密钥在首次生成 `.env` 时全部替换为独立随机值：

- `change-me-before-deployment`
- `change-me-before-deployment-at-least-32-characters`
- `example-access-key`
- `example-secret-key`

已有 `.env` 仍不得覆盖；PostgreSQL、Redis 和 MinIO 的派生连接信息继续引用同一次
生成的对应凭据。`tests/deploy/test_bootstrap_dev_env.py` 覆盖普通应用密钥和加长运维
令牌占位值。

### PR 3：本地处理 Worker 与项目说明

标题为 `feat(processing): 新增本地处理工作进程`。

新增 Django 管理命令 `run_local_processing_worker`。它只允许在
`DEBUG=True` 且 `PRODUCTION_DEPLOYMENT=False` 时运行，按创建时间处理到期的持久化
`QUEUED` 任务，排除已经进入删除流程的文档，并把其余并发和租约判断交给现有
`run_processing`。Worker 在同一进程中复用处理流水线，空队列时按可配置间隔轮询；
`--once` 只处理启动时取得的一批到期任务后退出。

参数约束如下：

- `--poll-interval`：`0.1` 至 `60` 秒，默认 `1.0`；
- `--limit`：`1` 至 `1000`，默认 `100`；
- `--once`：处理一个快照后退出，供测试和一次性本地执行使用。

根目录 `README.md` 作为项目入口，说明系统边界、本地依赖、Worker 启动方式、测试
命令和生产放行门禁。它不得暗示 GitHub Release 等于生产放行。

## 明确不交付的差异

`static/css/viewer.css` 的 `grid-column: 2` 修复，以及
`tests/browser/test_ac02_upload_browser.py` 的原件预览尺寸校验，已经存在于当前
`main`。本次不复制旧工作区中的这两个文件，避免覆盖主分支后来加入的认证与 UI
验收。

不修改 `VERSION`、`.release-please-manifest.json`、`pyproject.toml`、
`package.json`、`package-lock.json` 的版本字段或 `CHANGELOG.md` 自动生成区域。

## 分支与合并策略

三个分支均从执行时最新的 `origin/main` 创建：

- `codex/release-please-node24`
- `codex/dev-env-secrets`
- `codex/local-processing-worker`

每个分支独立验证并创建面向 `main` 的 PR。PR 标题先通过
`tools/check_conventional_commit.py` 校验，然后使用 Squash merge，使 PR 标题成为
主分支提交标题。禁止直接推送提交到 `main`。

## 验证策略

在干净、没有项目根 `.env` 的隔离工作树中执行验证：

- Release Please PR：发布自动化单元测试、`verify_release_automation.py` 和
  `release_version.py check`；
- 本地环境 PR：bootstrap 单元测试，并验证不会覆盖已有文件；
- Worker PR：Worker 单元测试、processing 测试和 Django 系统检查；
- 每个候选 PR：完整 `python -m pytest -q`；
- 最终合并后的 `main`：完整 Python 测试和 `npm run test:js`。

跳过项必须保持显式跳过，不把缺少外部 PostgreSQL、PaddleOCR、Safari 或性能环境
误报为通过。

## 清理策略

在三个 PR 全部合并且逐文件确认 `main` 包含有效改动之前，原始
`codex/phr-v1` 工作区保持不变。确认后只清理本次已归档或已由 `main` 吸收的确切
路径；忽略的 `.env`、本地依赖、测试数据和其他私密配置不在清理范围内。
