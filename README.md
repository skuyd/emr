# 肿瘤个人病案与病情轨迹系统

面向肿瘤患者及家庭照护者的个人健康档案（PHR）系统。用户上传报告图片或 PDF 后，系统保存原件、异步提取可检索信息，并提供按日期浏览、全文搜索、原件回看和来源定位能力。

> 本项目不是医院电子病历系统（EMR），不提供诊断、疗效判断、风险判断、用药建议或治疗建议。自动识别结果仅用于资料整理和检索，原始报告始终是主要依据。

## 当前状态

V1 已实现主要产品流程和自动化验证，包括登录建档、批量上传、后台处理、病案检索、原件查看、实验趋势、删除、通知与隐私隔离。

当前上线门禁仍为 **BLOCKED**。在 [`docs/verification/release-gate.md`](docs/verification/release-gate.md) 由验证工具判定为 `PASS` 之前，不得接入真实用户或真实医疗资料。仍待完成的门禁包括真实短信、私有 S3、离线 PaddleOCR、多进程部署、浏览器兼容、无障碍、性能以及备份恢复演练。

## 核心能力

- 手机号验证码登录、必要同意和单账号单患者建档；
- 图片与 PDF 批量上传、逐文件状态、失败重试和原件私有保存；
- 完全相同文件去重及相似文件提示；
- OCR、文档类型、日期、机构候选识别；
- 125 项检验指标字典、宽覆盖抽取及解析版本管理；
- 按日期组织的病案列表，以及 OCR、指标和机构搜索；
- 文档详情、原件查看器和字段来源定位；
- 保守的实验性指标趋势和一键识别反馈；
- 站内任务提醒、可选浏览器通知和隐私安全埋点；
- 单份资料删除、账号全量删除、删除账本与恢复保护；
- 健康检查、Prometheus 指标、告警和最小权限运营能力。

完整范围、明确不做项及验收标准见
[`第一版产品需求文档-PRD-v1.0.md`](docs/product/第一版产品需求文档-PRD-v1.0.md)。

## 技术架构

项目采用服务端渲染的 Python 模块化单体，并将 OCR、删除和通知等工作放入独立后台任务。

| 层次 | 技术或职责 |
| --- | --- |
| Web | Python 3.11–3.14、Django 5.2、Django Templates、原生 JavaScript/CSS |
| 数据 | PostgreSQL；本地测试可使用 SQLite |
| 队列与缓存 | Celery、Redis |
| 原件存储 | 私有 S3 兼容对象存储；本地开发使用隔离 MinIO |
| 文档处理 | Pillow、pypdf、pypdfium2、Magika、可选 PaddleOCR |
| 边缘与静态资源 | Caddy、Gunicorn、WhiteNoise |
| 验证 | pytest、Node Test Runner、Playwright、k6 |

主要目录：

```text
apps/                 Django 业务模块
  accounts/           登录、验证码、会话和账号删除
  patients/           患者空间、同意和个人设置
  documents/          上传、存储、病案、原件和删除
  processing/         OCR、元数据、解析流水线和后台运行器
  labs/               指标字典、抽取和趋势
  notifications/      站内提醒与 Web Push
  analytics/          隐私约束下的产品事件
  operations/         健康检查、指标、告警和恢复控制
config/               Django 与 Celery 配置
templates/            服务端页面模板
static/               CSS、JavaScript 和静态资源
tests/                单元、集成、验收、浏览器及安全测试
docs/                  产品、规格、计划、部署手册和验证证据
deploy/                生产镜像、编排、备份和恢复脚本
tools/                 字典、测试数据及发布门禁工具
```

## 本地开发

### 环境要求

- Python `>=3.11,<3.15`；
- Docker 与 Docker Compose；
- Node.js `>=22`，仅运行 JavaScript 或浏览器测试时需要；
- PowerShell，以下命令按 Windows 环境编写。

### 1. 安装 Python 依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

需要在本机实际运行 PaddleOCR 时安装 OCR 可选依赖：

```powershell
python -m pip install -e ".[test,ocr]"
```

### 2. 初始化本地配置和依赖服务

```powershell
python deploy/bootstrap_dev_env.py
docker compose --env-file .env up -d
```

初始化脚本从 `.env.example` 生成带随机本地密钥的 `.env`，且不会覆盖已有文件。`.env` 已被 Git 忽略，不要提交或分享它。

本地 Compose 仅把 PostgreSQL、Redis 和 MinIO 绑定到 `127.0.0.1`，并使用持久化命名卷。MinIO 仅用于隔离开发和封闭试用，不是正式生产存储建议。

### 3. 初始化数据库

```powershell
python manage.py migrate
python manage.py check
```

### 4. 启动应用

Windows 本机推荐使用一键启动脚本。它默认使用
`D:\EMR-Runtime\PaddleOCR\.venv\Scripts\python.exe`，执行迁移、配置检查和开发账号
初始化，后台启动本地处理 Worker，然后在前台运行 Web 服务：

```powershell
.\deploy\start-local.ps1
```

按 `Ctrl+C` 会同时停止 Web 与脚本启动的 Worker。可以使用 `-NoWorker`、`-NoSeed`、
`-Address localhost:8080` 或 `-PythonPath <python.exe>` 调整本地启动行为。Worker 日志保存在
`.runtime/logs/`。

也可以在两个终端中手工启动：

终端一：

```powershell
python manage.py runserver 127.0.0.1:8000
```

终端二可使用轻量本地处理 Worker，处理持久化 OCR 队列和开发短信 outbox：

```powershell
python manage.py run_local_processing_worker
```

若需要验证完整 Celery 任务和周期恢复机制，改为启动 Celery Worker 与 Beat：

```powershell
celery -A config worker --loglevel=INFO --pool=solo
celery -A config beat --loglevel=INFO
```

打开 <http://127.0.0.1:8000/>。当前开发配置在 `DEBUG=True`、
`OTP_PROVIDER=development` 时使用固定验证码 `230412`；生产配置明确禁止此行为。

停止本地依赖服务但保留数据卷：

```powershell
docker compose --env-file .env down
```

## 测试与质量检查

安装前端测试依赖：

```powershell
npm ci
npx playwright install
```

常用验证命令：

```powershell
python tools/verify_release_automation.py
python tools/release_version.py check
python tools/verify_documentation.py
python manage.py check
python manage.py makemigrations --check --dry-run
python tools/verify_traceability.py
python tools/verify_release_gate.py
$env:PYTHONUTF8 = "1"
python -m pytest -q
npm run test:js
```

CI 在独立 PostgreSQL 中必跑并发测试，并运行真实 Chromium 合成上传回归；这些必跑项
由 `tools/run_required_tests.py` 检查，跳过或空集合均失败。本地缺少对应环境时可以显式
跳过，但跳过不等于通过：

- PostgreSQL 并发用例需要 `PHR_POSTGRES_TEST_URL`；
- PaddleOCR 模型烟测需要已准备的离线模型目录；
- Safari、历史浏览器、性能与恢复测试需要对应外部环境。

正式 Playwright 流程还需要 `PHR_E2E_BASE_URL`、认证状态、合成上传文件和合成文档标识等输入。按 [`docs/verification/browser-accessibility.md`](docs/verification/browser-accessibility.md) 准备独立验收环境后再运行：

```powershell
npm run test:e2e:chrome
npm run test:e2e:edge
npm run test:e2e:webkit-reference
```

`webkit-reference` 只用于发现 WebKit 引擎回归，不能替代真实 Safari 放行证据。

当前配置会在基础设置加载根目录 `.env`。本地 `.env` 中的安全参数可能影响配置隔离测试；在该问题修复前，应在不加载开发 `.env` 的干净验证环境中生成正式回归证据。

验证证据及其判定规则见 [`docs/verification/README.md`](docs/verification/README.md)。

## 版本与 Changelog

全产品使用根目录 [`VERSION`](VERSION) 中的统一版本号。面向 `main` 的 PR 标题必须使用
`<type>(<scope>)!: 中文描述` 格式；合并后，Release Please 会根据提交类型自动计算版本号、
生成 [`CHANGELOG.md`](CHANGELOG.md)、同步 Python/npm 版本字段并创建 GitHub Release。

```powershell
python tools/release_version.py show
python tools/release_version.py check
python tools/check_conventional_commit.py "feat(records): 新增报告导出功能"
python tools/verify_release_automation.py
```

普通功能分支不要手工填写 Changelog 或修改版本字段。`fix`/`perf` 自动升级 PATCH，`feat`
升级 MINOR，带 `!` 或 `BREAKING CHANGE:` 的变更升级 MAJOR；其他允许类型不触发发布。
完整规则、GitHub 一次性设置和故障处理见
[`docs/policies/versioning.md`](docs/policies/versioning.md)。

## 生产部署

生产部署面向封闭试用或受控正式环境。完整操作必须遵循
[`docs/deployment/production-runbook.md`](docs/deployment/production-runbook.md)，以下内容仅作为入口摘要。

### 外部前置条件

部署前需准备：

- 正式域名、DNS 和 ACME 联系邮箱；
- HTTPS 短信网关、API 凭据、签名密钥和短信模板；
- 私有、启用版本控制和服务端加密的 S3 桶；
- PostgreSQL、Redis 以及 Web、Celery Worker、Celery Beat 的运行容量；
- 已离线下载并校验的 PaddleOCR 检测与识别模型；
- `age` 备份公钥和存放于另一安全域的私钥；
- Chrome、Edge、macOS/Safari、k6 和固定验收环境。

任何生产凭据都不得提交到 Git、聊天记录、工单正文或验证报告。

### 1. 生成生产配置

```powershell
python deploy/bootstrap_production_env.py
```

该命令仅在文件不存在时创建 `.env.production`，不会覆盖已有配置。运维人员随后必须替换所有 `required-` 值，并复核正式域名、短信主机白名单、S3 主机白名单、对象前缀、离线模型目录和备份密钥。

### 2. 构建与生产检查

```powershell
$compose = @("compose", "--env-file", ".env.production", "--file", "deploy/compose.yaml")
docker @compose config --quiet
docker @compose build --pull
docker @compose run --rm --no-deps migrate `
  python manage.py check --deploy --settings=config.settings.production
docker @compose run --rm --no-deps migrate `
  python manage.py check_private_storage --settings=config.settings.production
```

生产检查会拒绝占位密钥、不安全 Cookie、SQLite、内存缓存、非 HTTPS 短信网关、公开或未版本化对象桶，以及未指定的离线 OCR 模型目录。

### 3. 启动与健康检查

```powershell
docker @compose up -d
docker @compose ps

$appDomain = Read-Host "Production application domain"
Invoke-RestMethod "https://$appDomain/health/live/"
# 深度 readiness 需从内部携带令牌调用，命令见生产运行手册。
```

`live` 只代表 Web 进程存活；`ready` 由内部鉴权检查数据库、缓存和桶连接，写删权限需单独验收。公网只通过 Caddy 暴露 80/443，内部运维接口不得直接开放。

### 4. 上线门禁

```powershell
python tools/verify_traceability.py
python tools/verify_release_gate.py
```

只有机器生成的上线门禁结论为 `PASS` 才能接入真实用户。预计结果、跳过测试、WebKit 对 Safari 的替代运行，以及只有说明文字而无执行制品的记录，均不算通过。

生产环境还必须完成：

- 真实短信、私有 S3 和离线 OCR 全流程；
- Chrome、Edge、Safari 支持版本的 P00–P08 流程；
- 键盘、读屏、200% 缩放和高对比度验收；
- PostgreSQL 并发事务与固定负载性能测试；
- 加密备份、删除账本回放和隔离恢复演练。

### 5. 备份、恢复与停机

备份和恢复只能针对明确的专用目录、隔离数据库及隔离对象前缀执行。不要使用破坏性数据库回退，也不要在未回放最新删除账本前开放恢复环境。

具体命令、安全检查、RPO/RTO 目标和事件响应流程见
[`docs/deployment/production-runbook.md`](docs/deployment/production-runbook.md)。正常停机使用：

```powershell
$compose = @("compose", "--env-file", ".env.production", "--file", "deploy/compose.yaml")
docker @compose down
```

不要默认附加 `--volumes`；数据卷销毁必须单独取得授权。

## 项目文档

正式文档统一存放在 `docs/`，状态、版本关联和阅读顺序从
[项目文档中心](docs/README.md)进入。根目录不维护另一份完整文档清单。

- [项目文档中心](docs/README.md)
- [产品变更记录](CHANGELOG.md)
- [当前版本清单](docs/releases/v1.1.0.md)
- [上线放行门禁](docs/verification/release-gate.md)

## 安全与数据使用

- 开发、自动化测试、性能测试和演示只使用合成数据；
- 不在日志、埋点、通知、截图或测试制品中记录手机号、患者称呼、文件名、OCR 原文或医疗内容；
- 原件必须保存在私有对象存储中，不通过公开媒体目录提供；
- 发现跨账号访问、公开桶、密钥泄漏、删除数据复现或日志含医疗内容时，应立即停止入口流量并按运行手册处置。
