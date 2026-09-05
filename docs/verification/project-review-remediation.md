# 项目审查修复验证记录

本记录对应[项目审查修复计划](../plans/2026-09-05-project-review-remediation.md)，
最初覆盖 `fix/project-review-hardening` 工作区；交付分支为
`fix/project-review-delivery`，基于已发布的 `v0.3.1`。登记表的计划状态保持
`implementing`，表示等待合并；本批修复的新发布版本尚未确定。

## 修复范围

| 领域 | 已实现的行为与主要回归 |
| --- | --- |
| 数据质量 | 指标 ≥0.80、标准名 ≥0.90，趋势要求指标及选中日期来源 ≥0.95；旧解析质量标记缺失时不进入趋势。见 `tests/labs/test_quality_gate.py`、`tests/documents/test_quality_review.py`。 |
| 历史资料 | 档案统计、结构字段检索与详情准入一致；历史资料可重新整理，新质量版本发布后撤下重复操作入口。 |
| 原件删除 | 版本化 S3 精确键的所有版本与删除标记均清除并复查；失败保留任务重试，补偿仍只删除自身版本。见 `tests/documents/test_storage.py`。 |
| 并发一致性 | 删除、处理、版本激活、通知和账号清理统一锁序；PDFium 从构造至资源关闭均使用同一进程锁。见 `tests/integration/`、`tests/operations/test_services_postgres.py`、`tests/documents/test_pdfium_concurrency.py`。 |
| 身份认证 | 可信代理客户端隔离限流、默认密码策略、旧重置票据失效、定向会话撤销；找回请求通过加密 outbox 异步发送并统一限流。见 `tests/accounts/`。 |
| 上传交互 | 正确处理 hidden、慢批次响应、重试条件和键盘焦点；原件响应触发下载。见 `tests/browser/test_upload_interactions_browser.py`、`tests/browser/test_ac02_upload_browser.py`。 |
| 查询与资源 | 搜索使用独立 Exists，30×30 合成明细由 900 条关联行降为 1 条；进程内复用 OCR 模型，OCR 与控制任务使用独立消费者。 |
| 运维与工程 | 内部鉴权 readiness、短缓存及独立短超时连接、指标首次插入冲突恢复；隔离开发环境配置，分拆文档 views，完整依赖哈希锁与 CI 必跑检查。 |

## 初始工作区验证

机器可读结果见[集成结果记录](artifacts/project-review-remediation-result.json)。
2026-09-05 在 Windows / Python 3.11 和隔离 PostgreSQL 18.6 上完成：

| 检查 | 结果 |
| --- | --- |
| Python 全量回归 | 1107 passed，18 skipped，0 failed；99.08 秒。 |
| PostgreSQL 必跑集合 | 15 passed，0 skipped；通过 `tools/run_required_tests.py` 执行。 |
| 本地 Chromium 合成流程 | 12 passed，已包含在全量回归中，覆盖认证、上传和下载。 |
| PaddleOCR 适配器与真实模型 | 8 passed；使用 D 盘既有环境与模型缓存，禁用 Python socket 连接。 |
| JavaScript | 6 passed。 |
| Django 状态 | 系统检查及迁移一致性通过。 |
| 仓库契约 | 文档治理、需求追踪、版本一致性、发布自动化和发布门禁校验通过。 |
| 依赖锁 | Linux / Python 3.11 解析 112 个依赖；普通再生成保留所有版本及 SHA-256 集合。 |

全量回归跳过的 18 项中，15 项 PostgreSQL 与 1 项真实模型已在对应环境单独执行通过；
另外 2 项文档符号链接安全用例受本机 Windows 权限限制，保留跳过，Linux CI 会执行。
测试环境仍有 Django 覆盖数据库设置、已安装 requests 依赖范围及本地 Paddle 编译缓存警告，
它们未导致本轮测试失败。

在临时 PostgreSQL 中用合成数据复现了旧版文档删除、通知创建、推送和订阅撤销的真实
死锁，再验证修复结果。搜索回归也先观察到 900 条关联记录，修改后为 1 条。上述结果
属于工作区验证，未声称远端 CI 已运行或生产性能门禁已通过。

## 交付分支复验

实现提交：`14161d1fd8db5e66d0d157961eea1f64baf35d49`。
机器结果见[交付复验记录](artifacts/project-review-delivery-result.json)。

交付工作区仅整理本轮修复；原工作区已有的本地启动脚本与外部验收材料未纳入。
因此完整测试比初始工作区少 3 项启动脚本用例：`1104 passed, 18 skipped`，
0 failed（110.57 秒）；PostgreSQL 必跑集合 `15 passed, 0 skipped`（23.65 秒），
JavaScript 6 项通过，Django、迁移一致性、文档与发布契约均通过。

本分支沿用主分支的生产门禁记录：`BLOCKED`，7 项通过、16 项待验证。原工作区中的
8/23 结论含尚未提交的独立模型验收材料，两套记录的范围不同，不能混用。
CI 新增生产 Docker 镜像构建和非 root 运行检查；结果以本 PR 的实际 CI 运行记录为准。

## 升级与外部边界

升级操作见[生产运行手册](../deployment/production-runbook.md)。需要执行短信 outbox
迁移及历史会话登记，配置版本列举/删除权限，启动两类队列消费者和 Beat。历史趋势点
可能减少，用户可从资料详情重新整理，达到新质量门槛后恢复资格。

本机未执行 Docker 镜像构建或真实多容器部署。Linux 锁解析不等于镜像已构建。
真实短信网关的投递 ID 去重、正式 S3 写删、外部浏览器版本、负载与备份恢复继续按
[上线门禁](release-gate.md) 验收；本批合成验证不将它们改为通过。
