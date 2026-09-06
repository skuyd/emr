# 家庭健康资料系统 V1 部署与运行手册

本手册用于封闭试用和正式生产环境。代码包已具备生产配置，但在
`docs/verification/release-gate.md` 显示 `PASS` 之前不得接入真实用户。所有验证、
压测和恢复演练只使用系统生成的合成数据。

## 1. 上线前必须取得的外部条件

这些信息不需要产品用户决定，应由部署/运维服务方提供并保管：

- 已解析到服务器的正式域名和 ACME 联系邮箱；
- 支持 HTTPS、模板短信和请求签名的短信网关凭据；
- 私有、启用版本控制和服务端加密的正式 S3 桶及最小权限凭据；
- PostgreSQL、Redis、OCR Worker 和独立 control Worker 的生产容量；
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
  "migrate", "web", "worker", "control-worker", "beat", "tombstone-backup", "restore-verify"
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
由合并到 `main` 后的自动发布流程生成；具体规则见
[`docs/policies/versioning.md`](../policies/versioning.md)。
GitHub Release 不代表生产环境放行，本手册中的全部上线门禁仍须通过。

镜像安装包含传递依赖和 SHA-256 哈希的 `requirements-prod.lock`，并启用
`--require-hashes`；CI 安装同一生产锁及 `requirements-test.lock`。目标环境是
Python 3.11 / Linux x86_64。镜像使用非 root 用户、只读应用文件系统和预生成静态资源。

更新锁文件需要本机已安装 `uv`，执行 `python tools/update_dependency_locks.py`。
默认保留当前生产与测试版本并重算依赖闭包；主动升级时使用 `--upgrade`，审查差异后
重新运行 Linux 镜像构建、测试及离线模型验证。生成器从 `pyproject.toml` 读取生产、
OCR 和测试依赖，不会改写项目版本号。
应用启动检查会拒绝开发验证码、占位密钥、不安全 Cookie、SQLite、内存缓存、非 HTTPS
短信网关、公开/未版本化对象桶以及未指定的 OCR 模型目录。

## 4. 对象存储

### 正式 S3

由云平台预先创建私有桶，启用四项公共访问阻断、版本控制和 AES-256/KMS 加密。应用
凭据只授予当前桶和当前前缀所需权限，包括桶级 `s3:ListBucket`、
`s3:ListBucketVersions` 和对象级 `s3:GetObject`、`s3:PutObject`、`s3:DeleteObject`、
`s3:DeleteObjectVersion`，以及以下私有性检查需要的桶配置读取权限。版本列举应限定
本环境前缀；物理清理会删除精确目标键的全部版本和删除标记，再复查是否为空。
未授权、部分删除失败或残留均保留清理任务重试。验证：

```powershell
docker @compose run --rm --no-deps migrate `
  python manage.py check_private_storage --settings=config.settings.production
```

只有命令明确返回 `Private object storage checks passed.` 才能继续。该命令检查
私有性、版本控制和加密配置；还必须在环境独占前缀上传合成文件、生成多个版本、执行
应用删除，并列举确认全部版本与删除标记清空。真实写删和恢复证据继续登记在发布门禁中。

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

已有部署升级本批修复前，先停止新请求并保持旧 Worker 运行，等待在途任务完成和旧
`celery` 队列为空，再停止旧 Worker。如果有存量消息，使用旧镜像的专用 Worker 消费完
后再切换，禁止清空丢弃队列。迁移包含 `accounts.0008_smsdeliveryjob`；
`register_legacy_sessions` 会登记仍有效的历史会话，后续撤销按账号索引执行。
上线后同时确认 `ocr`、`control` 队列均有消费者且 Beat 运行。

使用正式 S3 时：

```powershell
docker @compose up -d
docker @compose ps
```

`migrate` 服务先执行生产配置检查、私有桶检查、数据库迁移和历史会话登记；Web、两类 Worker 和 Beat 只有
在它成功后才启动。Caddy 自动申请 HTTPS 证书，只向公网暴露 80/443。公网 `/admin`
、`/internal` 和 `/health/ready` 直接返回 404。
Caddy 使用独立内部 `proxy` 网络，默认固定地址 `172.30.50.2`；Web 仅信任该地址传入的
客户端转发头。若与宿主机网段冲突，同时调整 `PHR_PROXY_SUBNET` 和 `PHR_PROXY_ADDRESS`。
独立部署时显式设置 `TRUSTED_PROXY_NETWORKS` 为实际代理地址，默认空列表；不要信任整个
私网网段。生产和测试设置不自动读取根目录 `.env`，生产变量由部署环境显式注入。

```powershell
Invoke-RestMethod "https://$env:APP_DOMAIN/health/live/"
@'
import os
import urllib.request
request = urllib.request.Request("http://127.0.0.1:8000/health/ready/", headers={
    "Host": os.environ["APP_DOMAIN"],
    "X-Forwarded-Proto": "https",
    "Authorization": "Bearer " + os.environ["OPERATIONS_METRICS_TOKEN"],
})
with urllib.request.urlopen(request, timeout=15) as response:
    print(response.read().decode())
'@ | docker @compose exec -T web python -
```

`live` 只说明进程存活；`ready` 需要内部 Bearer 令牌，检查数据库、缓存、桶的连接。
S3 使用只读 `head_bucket`，不替代写删验收。结果默认按进程缓存 2 秒，单次依赖操作超时
默认 1 秒（PostgreSQL 建连最小 2 秒）；这些限制不等于整个 HTTP 请求的总截止时间。
并发刷新时返回 503，探针不复用应用的长超时连接池。
升级时先生成加密备份，再构建新镜像并执行 `docker @compose up -d`。数据库迁移只能向前
执行；若应用回滚，保留已迁移数据库并回滚镜像，禁止使用破坏性数据库回退命令。

## 6. 短信、OCR 和后台任务验收

在受控合成手机号上完成一次验证码申请和一次错误码验证。确认短信供应商只收到手机号、
模板 ID、一次性验证码、投递 ID 和防重放签名，不收到患者称呼、文件名或医疗内容。
找回密码请求仅写入加密短信 outbox；Beat 每 5 秒恢复待发任务，由 control Worker 投递。
伪装请求执行等量哈希并走相同排队入口，消费者销毁其密文而不调用供应商。
网关必须按稳定 `Idempotency-Key`（与签名正文 `delivery_id` 对应）去重；验收应覆盖
网关已接受后连接断开或 Worker 死亡、恢复后携带相同验证码和投递 ID 重试。
应用端不能单独保证运营商恰好投递一次，供应商幂等合同未经实测不得作为已通过证据。
应用日志不得记录手机号、验证码、供应商响应正文或 URL 查询参数。

使用离线模型分别处理合成图片、带文本层 PDF 和无文本层 PDF；确认 Worker 日志无模型
联网下载，处理结果可在 Web 进程中检索。至少启动两个 Worker 进程执行重复消息、失联
恢复和幂等验证。OCR 队列由 `worker --queues=ocr` 消费，模型在子进程内按配置复用；
短信、导出、删除、通知与恢复走 `control-worker --queues=control`，两者并发量分别配置。
验收时用慢 OCR 任务占满 OCR 执行能力，确认 control 任务仍可完成。

### 三阶段导出与回收站

`exports.generate_file` 在 control 队列生成文件。Beat 每 60 秒调度
`exports.recover_jobs`、文档删除恢复和账号删除恢复，处理丢失的工作租约、待清理对象
及回收站到期。资料保留连续 30 × 24 小时，到期即不可恢复；永久删除立即隔离，正常
物理清理目标为 24 小时，失败持续重试。导出文件从生成完成起最多保留 24 小时，下载
时重新校验会话与来源；失效后立即不可获取。已下载的静态副本无法由系统收回。

生产镜像通过 `EXPORT_TEMP_DIRECTORY=/var/lib/phr/export-tmp` 使用私有磁盘卷
`export_tmp`。镜像预建目录，属主/属组为 `10001:10001`，权限 `0700`；Web 下载与
control Worker 生成均挂载该卷。它存放流式打包和下载校验的临时文件，不属于原件库，
不需要备份，也不得通过 Web 或共享目录开放。自定义挂载必须保留应用用户写入权限。
临时文件独占创建，关闭即清理；Linux 匿名文件在进程退出后由内核回收。

不要将大包导出改回容器的 1 GB `/tmp` 内存盘。单患者原件配额默认为 2 GB，磁盘容量
还需按同时生成、打包、下载的数量预留；256 MiB 本地合成探针不代表生产并发容量。
上线验收应覆盖非 root 写入、容量不足、工作进程中断和重试清理，并监控磁盘余量及
control 队列积压。目录不可写或空间耗尽会使本次生成/下载失败，界面不能显示完整成功。
本机没有 Docker，镜像构建及非 root 写入烟测仍需由 CI 或部署环境执行，见
[第三阶段验证记录](../verification/phase-three.md)。

新解析结果：指标来源置信度至少 0.80 才进入结构结果，标准名展示至少 0.90；趋势要求
指标和选中日期的来源均至少 0.95，且具有当前质量策略标记。历史未标记版本仍可查看
原件和 OCR，但不进入趋势；旧趋势点可能减少，用户可在资料详情点击“重新整理”恢复
评估资格。找回入口对所有手机号统一按可信 IP 每小时最多 30 次，超限仍显示中性提示
但不再创建短信任务。注册和重置密码至少 12 位，并拒绝常见密码和纯数字密码。

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
