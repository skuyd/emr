# V1 上线放行门禁

**结论：BLOCKED**

生成时间：`2026-09-05T00:04:06+08:00`。必需门禁 23 项，通过 8 项，待验证 15 项。

本报告由 `tools/verify_release_gate.py --write` 从 `release-evidence.json` 生成。
只要一个必需项仍为 `pending`，结论就只能是 `BLOCKED`；手工修改本页不能放行。

| 门禁 | 状态 | 结果或阻断原因 | 证据 |
| --- | --- | --- | --- |
| PRD 需求追踪 | passed | 62 项编号完整：60 verified，2 external_pending | `docs/verification/traceability.json`<br>`tools/verify_traceability.py` |
| 125 项指标字典 | passed | 示例提取字典与版本/哈希契约自动验证通过 | `apps/labs/dictionaries/v1.0.0.json`<br>`tests/labs/test_dictionary.py` |
| Python 全量回归 | passed | 666 passed，3 个外部环境用例按门禁保留 skipped，0 failed（43.54s） | `tests/test_project_configuration.py` |
| Service Worker JavaScript 回归 | passed | 3 passed，0 failed，0 skipped | `tests/js/service-worker.test.mjs` |
| 生产配置检查 | passed | 安全完整配置可通过 check --deploy，缺失配置会失败关闭 | `tests/deploy/test_production_stack.py`<br>`config/settings/production.py` |
| 隐私与安全回归 | passed | 隐私 canary、日志/埋点/通知扫描、CSRF、IDOR、租户隔离和安全头全部通过 | `tests/privacy/test_runtime_artifacts.py`<br>`tests/security/test_headers.py` |
| 部署静态契约 | passed | Compose YAML、PowerShell 语法、生产检查和隔离恢复契约通过 | `deploy/compose.yaml`<br>`deploy/backup.ps1`<br>`deploy/restore.ps1`<br>`tests/deploy/test_production_stack.py` |
| Chrome 当前主版本完整流程 | pending | 当前版本冒烟已通过，但正式 HTTPS 环境 P00–P08 完整 Playwright 套件未运行 | `docs/verification/browser-accessibility.md`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| Chrome 前一主版本完整流程 | pending | 缺少该历史主版本测试环境 | `docs/verification/artifacts/local-gate-rehearsal-result.json` |
| Chrome 前二主版本完整流程 | pending | 缺少该历史主版本测试环境 | `docs/verification/artifacts/local-gate-rehearsal-result.json` |
| Edge 当前主版本完整流程 | pending | 当前版本冒烟已通过，但正式 HTTPS 环境 P00–P08 完整 Playwright 套件未运行 | `docs/verification/browser-accessibility.md`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| Edge 前一主版本完整流程 | pending | 缺少该历史主版本测试环境 | `docs/verification/artifacts/local-gate-rehearsal-result.json` |
| Edge 前二主版本完整流程 | pending | 缺少该历史主版本测试环境 | `docs/verification/artifacts/local-gate-rehearsal-result.json` |
| Safari 当前主版本完整流程 | pending | 本机没有 macOS/Safari 真机；WebKit 参考不能替代 | `docs/verification/external-compatibility.md`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| Safari 前一主版本完整流程 | pending | 缺少对应的受安全支持 macOS/Safari 真机 | `docs/verification/external-compatibility.md`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| 跨浏览器无障碍人工验收 | pending | 需在全部受支持真实浏览器完成键盘、读屏、200% 缩放和高对比度检查 | `docs/verification/browser-accessibility.md`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| PostgreSQL 并发事务验证 | pending | 本机没有可用 PostgreSQL 服务，2 个真实并发用例仍跳过 | `tests/integration/test_processing_postgres_concurrency.py`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| 离线 PaddleOCR 模型实跑 | passed | 仓库模型烟测 1/1、Paddle 适配器套件 8/8 通过；禁用网络连接后，显式 D 盘模型目录对合成图片识别出 2 个文本区域 | `tests/processing/test_paddle_adapter.py`<br>`docs/verification/artifacts/offline-ocr-model-result.json` |
| 真实生产短信网关 | pending | 尚未提供正式短信网关域名、API 凭据和模板 | `tests/accounts/test_sms_gateway.py`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| 真实私有 S3 | pending | 尚未连接真实版本化/加密/公共访问阻断 S3 桶执行启动探针 | `tests/deploy/test_private_storage_check.py`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| 多进程 Web/Worker/Beat 部署 | pending | 本机未安装 Docker，尚未构建并运行完整生产编排 | `deploy/compose.yaml`<br>`deploy/Dockerfile`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| 固定负载性能 | pending | 本机无 k6 和固定容量环境，P50/P95/P99 及 5 分钟结果尚无真实数据 | `tests/performance/k6-upload-search.js`<br>`docs/verification/performance.md`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |
| 加密备份恢复与删除账本演练 | pending | 本机无 Docker/age/真实 S3，RPO≤15 分钟和 RTO≤4 小时尚未实测 | `deploy/backup.ps1`<br>`deploy/restore.ps1`<br>`docs/verification/backup-restore.md`<br>`docs/verification/artifacts/local-gate-rehearsal-result.json` |

## 放行前待办

- Chrome 当前主版本完整流程：当前版本冒烟已通过，但正式 HTTPS 环境 P00–P08 完整 Playwright 套件未运行
- Chrome 前一主版本完整流程：缺少该历史主版本测试环境
- Chrome 前二主版本完整流程：缺少该历史主版本测试环境
- Edge 当前主版本完整流程：当前版本冒烟已通过，但正式 HTTPS 环境 P00–P08 完整 Playwright 套件未运行
- Edge 前一主版本完整流程：缺少该历史主版本测试环境
- Edge 前二主版本完整流程：缺少该历史主版本测试环境
- Safari 当前主版本完整流程：本机没有 macOS/Safari 真机；WebKit 参考不能替代
- Safari 前一主版本完整流程：缺少对应的受安全支持 macOS/Safari 真机
- 跨浏览器无障碍人工验收：需在全部受支持真实浏览器完成键盘、读屏、200% 缩放和高对比度检查
- PostgreSQL 并发事务验证：本机没有可用 PostgreSQL 服务，2 个真实并发用例仍跳过
- 真实生产短信网关：尚未提供正式短信网关域名、API 凭据和模板
- 真实私有 S3：尚未连接真实版本化/加密/公共访问阻断 S3 桶执行启动探针
- 多进程 Web/Worker/Beat 部署：本机未安装 Docker，尚未构建并运行完整生产编排
- 固定负载性能：本机无 k6 和固定容量环境，P50/P95/P99 及 5 分钟结果尚无真实数据
- 加密备份恢复与删除账本演练：本机无 Docker/age/真实 S3，RPO≤15 分钟和 RTO≤4 小时尚未实测

## 判定规则

外部环境门禁只有在提交带时区执行时间、环境、命令、结果和制品 SHA-256 的独立证明后才能改为 `passed`。
WebKit 参考运行不能代替真实 Safari；跳过测试、预计结果、空白性能数字和仅有说明文字都不算通过。
