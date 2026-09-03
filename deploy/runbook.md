# 家庭健康资料系统 V1 部署与运行手册

本手册用于封闭试用和正式生产环境。代码包已具备生产配置，但在
`docs/verification/release-gate.md` 显示 `PASS` 之前不得接入真实用户。所有验证、
压测和恢复演练只使用系统生成的合成数据。

## 1. 上线前必须取得的外部条件

这些信息不需要产品用户决定，应由部署/运维服务方提供并保管：

- 已解析到服务器的正式域名和 ACME 联系邮箱；
- 支持 HTTPS、模板短信和请求签名的短信网关凭据；
- 私有、启用版本控制和服务端加密的正式 S3 桶及最小权限凭据；
- PostgreSQL、Redis 和至少一个 Celery Worker 的生产容量；
- 已离线下载并校验的 PaddleOCR 检测与识别模型目录；
- `age` 备份公钥及存放在另一安全域的私钥；
- macOS/Safari、受支持 Chrome/Edge 版本、k6 和固定性能环境。

任何凭据都不得提交到 Git、工单正文、聊天记录或验证报告。

## 2. 首次生成生产配置

在仓库根目录执行：

```powershell
python deploy/bootstrap_production_env.py
```

命令只在文件不存在时创建 `.env.production`，并生成内部随机密钥；它永不覆盖已有
文件。随后由运维人员填写文件中仍以 `required-` 开头的域名、短信、模型和备份相关
值。正式 S3 环境还要用供应商提供的 S3 地址和凭据替换封闭试用 MinIO 值。

至少确认：

- `APP_DOMAIN`、`DJANGO_ALLOWED_HOSTS` 和 `CSRF_TRUSTED_ORIGINS` 指向同一正式域；
- `SMS_GATEWAY_ALLOWED_HOSTS` 是短信 URL 的精确主机名，不使用通配符；
- `DOCUMENT_S3_PREFIX` 是本环境独占前缀；
- 自定义 S3 地址的主机必须精确列入 `DOCUMENT_S3_ALLOWED_HOSTS`；正式 S3 使用 HTTPS；
- `ALLOW_PERFORMANCE_SEED=False`、`RESTORE_DRILL_MODE=False`；
- `.env.production` 权限仅允许部署账号读取。

## 3. 构建与静态检查

```powershell
$productVersion = (python tools/release_version.py show).Trim()
if ($LASTEXITCODE -ne 0) { throw "无法读取产品版本" }
$env:APP_IMAGE_TAG = $productVersion
$compose = @("compose", "--env-file", ".env.production", "--file", "deploy/compose.yaml")
python tools/release_version.py check
if ($LASTEXITCODE -ne 0) { throw "产品版本元数据不一致" }
$composeJson = docker @compose --profile "*" config --format json
if ($LASTEXITCODE -ne 0) { throw "Compose 配置无效" }
$resolvedCompose = $composeJson | ConvertFrom-Json
$expectedImage = "family-phr:$productVersion"
$invalidImages = foreach ($serviceName in @(
  "migrate", "web", "worker", "beat", "tombstone-backup", "restore-verify"
)) {
  if ($resolvedCompose.services.$serviceName.image -ne $expectedImage) { $serviceName }
}
if ($invalidImages) {
  throw "应用镜像标签与 VERSION 不一致：$($invalidImages -join ', ')"
}
docker @compose build --pull
docker @compose run --rm --no-deps migrate `
  python manage.py check --deploy --settings=config.settings.production
```

构建时使用的 `APP_IMAGE_TAG` 必须与根目录 `VERSION` 一致。版本号、Changelog 和 Git 标签
由合并到 `main` 后的自动发布流程生成；具体规则见 [`docs/versioning.md`](../docs/versioning.md)。
GitHub Release 不代表生产环境放行，本手册中的全部上线门禁仍须通过。

镜像使用固定版本的应用直接依赖、非 root 用户、只读应用文件系统和预生成的带摘要静态资源。
应用启动检查会拒绝开发验证码、占位密钥、不安全 Cookie、SQLite、内存缓存、非 HTTPS
短信网关、公开/未版本化对象桶以及未指定的 OCR 模型目录。

## 4. 对象存储

### 正式 S3

由云平台预先创建私有桶，启用四项公共访问阻断、版本控制和 AES-256/KMS 加密。应用
凭据只授予当前桶和当前前缀所需的读写删权限。验证：

```powershell
docker @compose run --rm --no-deps migrate `
  python manage.py check_private_storage --settings=config.settings.production
```

只有命令明确返回 `Private object storage checks passed.` 才能继续。

### 封闭试用 MinIO

MinIO 只作为隔离封闭试用依赖，正式 S3 部署不启动它：

```powershell
docker @compose --profile closed-trial up -d postgres redis minio
docker @compose --profile closed-trial run --rm minio-init
docker @compose --profile closed-trial up -d
```

`minio-init` 关闭匿名访问并启用版本控制。封闭试用配置允许内网 HTTP 和桶默认加密缺失；
正式 S3 必须把 `DOCUMENT_S3_ALLOW_INSECURE_INTERNAL` 设为 `False`、把
`DOCUMENT_S3_REQUIRE_ENCRYPTION` 设为 `True`，且只有真实私有桶探针证据通过后才能放行。

## 5. 启动、升级和健康检查

使用正式 S3 时：

```powershell
docker @compose up -d
docker @compose ps
```

`migrate` 服务先执行生产配置检查、私有桶检查和数据库迁移；Web、Worker 和 Beat 只有
在它成功后才启动。Caddy 自动申请 HTTPS 证书，只向公网暴露 80/443。公网 `/admin`
和 `/internal` 直接返回 404。

```powershell
Invoke-RestMethod "https://$env:APP_DOMAIN/health/live/"
Invoke-RestMethod "https://$env:APP_DOMAIN/health/ready/"
```

`live` 只说明进程存活；`ready` 必须同时显示 database、cache、object_storage 为 `up`。
升级时先生成加密备份，再构建新镜像并执行 `docker @compose up -d`。数据库迁移只能向前
执行；若应用回滚，保留已迁移数据库并回滚镜像，禁止使用破坏性数据库回退命令。

## 6. 短信、OCR 和后台任务验收

在受控合成手机号上完成一次验证码申请和一次错误码验证。确认短信供应商只收到手机号、
模板 ID、一次性验证码和防重放签名，不收到患者称呼、文件名或医疗内容。应用日志不得
记录手机号、验证码、供应商响应正文或 URL 查询参数。

使用离线模型分别处理合成图片、带文本层 PDF 和无文本层 PDF；确认 Worker 日志无模型
联网下载，处理结果可在 Web 进程中检索。至少启动两个 Worker 进程执行重复消息、失联
恢复和幂等验证。

## 7. 运维与监控

Prometheus 指标和告警接口需要 Bearer 令牌，且公网代理不转发这些路径。采集器应位于
私有网络；紧急检查可在 Web 容器内执行。指标标签只允许固定枚举，不得包含账号、文档、
文件名、手机号、查询词或 OCR 文本。

重点告警：队列积压、处理阶段耗时、重试率、原件打开失败、索引差异、删除积压、短信/
Push 供应商错误以及 readiness 降级。

## 8. 加密备份

宿主机需安装 Docker、`age` 和 `tar`。备份目录必须是专用的非根目录，私钥不能放在
同一服务器：

```powershell
.\deploy\backup.ps1 `
  -BackupRoot "E:\phr-encrypted-backups" `
  -AgeRecipient "age1..." `
  -RetentionDays 30
```

脚本备份 PostgreSQL、自有对象前缀和签名删除账本，生成 SHA-256 清单，再用 `age` 加密；
明文快照和压缩包无论成功或失败都会清理。保留期被限制为 1–30 天。生产数据库还必须
启用供应商 PITR/WAL，使恢复点间隔不超过 15 分钟；每天核验最新备份可解密且清单完整。

## 9. 隔离恢复与删除账本演练

每月至少演练一次。必须准备两个加密备份：A 是要恢复的数据快照；B 是不早于 A 的最新
备份，仅用于取得最新删除账本。验证“备份 A 后删除”的场景时，B 必须在删除完成后创建。

```powershell
.\deploy\restore.ps1 `
  -EncryptedBackup "E:\phr-encrypted-backups\phr-backup-A.tar.gz.age" `
  -LatestTombstoneBackup "E:\phr-encrypted-backups\phr-backup-B.tar.gz.age" `
  -AgeIdentityFile "X:\offline-key\identity.txt" `
  -WorkRoot "E:\phr-restore-work" `
  -RestoreDatabase "phr_202608_restore_drill" `
  -RestorePrefix "restore-drill/202608" `
  -Confirmation "RESTORE-DRILL"
```

脚本拒绝正式数据库名和正式对象前缀；它校验两个备份的 SHA-256 清单，把 A 恢复到隔离
目标，使用 B 的更新删除账本，然后在隔离进程内同步删除数据库、搜索来源和对象。它不向
正式 Celery 队列发送删除任务，也不切换正式流量。演练后人工确认：

- 被删账号无法登录，被删文档在首页、病案、搜索、详情和原件入口均不存在；
- 隔离 S3 前缀不存在被删对象；
- 非删除样本仍可打开；
- 最新可恢复点距故障假设不超过 15 分钟，总恢复时间不超过 4 小时。

隔离数据库和 `restore-drill/*` 对象由运维人员审核证据后单独删除；脚本不会自动销毁它们。

## 10. 浏览器与性能验收数据

只在专用验收环境临时设置 `ALLOW_PERFORMANCE_SEED=True`。会话文件必须写到仓库之外，
使用后立即删除。默认命令生成 100 个账号、每账号 300 份文档、每文档 3 页、每页 2,000
个纯合成字符以及浏览器状态文件：

```powershell
python manage.py seed_performance_environment `
  --settings=config.settings.production `
  --confirm SYNTHETIC-PERFORMANCE-DATA `
  --namespace release-202608 `
  --base-url https://phr-staging.example.com `
  --session-output X:\phr-evidence\sessions.json `
  --browser-state-output X:\phr-evidence\browser-state.json `
  --onboarding-state-output X:\phr-evidence\onboarding-state.json

python tools/generate_performance_fixtures.py `
  --output-dir X:\phr-evidence\fixtures `
  --confirm SYNTHETIC-PERFORMANCE-FIXTURES
```

浏览器和 k6 的执行命令见 `docs/verification/browser-accessibility.md` 与
`docs/verification/performance.md`。验收结束后恢复 `ALLOW_PERFORMANCE_SEED=False`、重启服务，
删除合成环境及全部会话/fixture 文件。

## 11. 事件响应与停机

发现跨账号访问、公开桶、密钥泄漏、删除数据复现或日志含医疗内容时，立即停止入口流量，
保留审计与加密备份，轮换受影响密钥，并按数据安全事件流程处理。不要删除日志来“修复”
泄漏证据，也不要在未回放最新删除账本前开放恢复环境。

正常停机使用 `docker @compose down`，不加 `--volumes`。只有在确认备份、恢复证据和数据
销毁授权后，才能由运维人员对明确的环境执行卷或隔离目标删除。
