# 项目审查修复验证记录

本记录对应[项目审查修复计划](../plans/2026-09-05-project-review-remediation.md)，
最初覆盖 `fix/project-review-hardening` 工作区；交付分支为
`fix/project-review-delivery`，基于已发布的 `v0.3.1`。修复已通过 PR #8 合并到 `main`，
Release Please 已发布 [v1.0.0](../releases/v1.0.0.md)；登记表计划状态为 `verified`。

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
CI 新增生产 Docker 镜像构建和非 root 运行检查；实际远端结果见下文。

## 合并与源代码发布复核

机器结果见[发布复核记录](artifacts/project-review-release-result.json)。

- [PR #8](https://github.com/skuyd/emr/pull/8) 于 `2026-09-05T15:41:44Z` Squash 合并，
  提交 `908cf5430cb5356215d00a435e348250d33e870f`，提交标题与 PR 标题一致。
- [修复 PR CI](https://github.com/skuyd/emr/actions/runs/33973956300) 四项通过，
  包括标题校验、完整回归、PostgreSQL 并发和 Docker 镜像构建。
- Release Please 经 [PR #9](https://github.com/skuyd/emr/pull/9) 自动确定 `1.0.0`，
  发布提交为 `0c52ad309d649003aae8ae3289eeed77dabe07e3`。
  [v1.0.0 GitHub Release](https://github.com/skuyd/emr/releases/tag/v1.0.0)
  于 `2026-09-05T15:42:15Z` 发布。
- [发布 PR CI](https://github.com/skuyd/emr/actions/runs/33975599612) 四项通过；
  [发布提交 CI](https://github.com/skuyd/emr/actions/runs/33975601143) 的测试、并发和镜像构建
  均通过，标题校验按配置仅在 PR 执行，因此在主分支推送中跳过。

本次复核发现发布 PR 在 CI 完成前已经自动合并：主分支 `protected=false`，分支保护与
rulesets API 均返回 `403`，提示当前私有仓库套餐不支持该功能。
`gh pr merge --auto` 依赖仓库必需检查配置，不能独立保证等待 CI。本次发布提交事后验证
通过；后续自动发布需要在工作流中显式检查 CI 并阻止失败或过期候选版本合并。
该问题属于发布流程限制，与生产门禁 `BLOCKED` 分开记录。

## 升级与外部边界

升级操作见[生产运行手册](../deployment/production-runbook.md)。需要执行短信 outbox
迁移及历史会话登记，配置版本列举/删除权限，启动两类队列消费者和 Beat。历史趋势点
可能减少，用户可从资料详情重新整理，达到新质量门槛后恢复资格。

本机未执行 Docker 镜像构建或真实多容器部署。Linux 锁解析不等于镜像已构建。
真实短信网关的投递 ID 去重、正式 S3 写删、外部浏览器版本、负载与备份恢复继续按
[上线门禁](release-gate.md) 验收；本批合成验证不将它们改为通过。
