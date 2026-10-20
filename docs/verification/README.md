# 验证与生产放行

文档整理日期：2026-10-20。当前源码版本为 [v5.1.0](../releases/v5.1.0.md)；
生产放行结论仍为 **BLOCKED**。本页汇总已有证据及未满足的门禁，不产生新的测试结果。
文档与功能状态以[登记表](../document-registry.json)为准。

## 当前版本的验证范围

[v5.1.0 验证摘要](artifacts/release-v5-1-0.json)绑定了功能候选、发布候选和最终标签：
相关 Python 1522 项、浏览器 84 项、JavaScript 5 项、PostgreSQL 164 项及发布专项
136 项通过，生产镜像构建与 smoke 通过。各组按原执行范围分别记录，不合计为唯一用例数。

该版本未运行业务全量或外部 Chrome、Edge、Safari TypeScript E2E，未部署生产。
共享样式对额外页面的影响也未由本次选定范围完整覆盖。发布阶段复用了专项基线，
`business_reused=false`；早期版本的全量通过结果不能替代当前版本的全量验证。

| 当前范围 | 验证入口 | 证据边界 |
| --- | --- | --- |
| 健康档案月历、列表、日期归类、筛选与导航 | [健康档案日历](../archive/verification/records-calendar.md) | v5.1.0 专项、浏览器和相关 PostgreSQL 验证；不代表外部浏览器矩阵或业务全量通过 |
| 日常记录月历、ECOG、连续录入、更正、删除及选定输出 | [日常记录](../archive/verification/daily-self-records.md) | DR-01～22 及 v5.0.1 原型对齐验证；原需求的 PostgreSQL 并发专项缺口仍保留 |
| 检验报告逐份核对与来源整理 | [逐份核对](../archive/verification/lab-report-review-workspace.md)、[来源整理](../archive/verification/lab-report-organization.md) | 各自已发布候选的功能、并发及浏览器证据；后续报告核对与 OCR 修复见 [v3.1.1](../releases/v3.1.1.md) |
| 检验目录名称与分类匹配 | [检验目录验证](../archive/verification/lab-selection-batch.md) | 名称同步和匹配修复的必要范围验证；历史批量确认已由逐份核对替代 |
| 家庭协作、临床报告、导出与分享等既有能力 | [五批功能验收](../archive/verification/batches-one-five-acceptance.md) | 保留原 63 条需求及 7 项全局契约的交付事实；当前范围须排除后续撤销或改造的功能 |
| 功能删减及数据迁移 | [删减验证](../archive/verification/feature-pruning.md) | 治疗周期、血糖记录、健康趋势、多指标对照和独立就诊事实汇总已撤销；真实 PostgreSQL 历史库迁移未验证 |

已撤销功能的专用质量目标不再作为当前功能交付要求。共用的单份报告核对、治疗原文、
原始检验资料和检验报告中的血糖指标仍保留，不能连同旧工作流一起视为删除。

## 仍需保留的质量与验证缺口

下列真实资料结果来自各自冻结的历史评估集，仍是未闭合限制的证据，不是 v5.1.0
重新评测的分数，也不能外推为当前全部资料的准确率：

| 范围 | 已有证据与未完成项 |
| --- | --- |
| 检验与事实自动提取 | 检验联合 F1 的原结果为 57.92%；事实精确率为 48.02%，候选核对量为 202。自动化功能通过没有消除真实提取质量缺口。见[检验评估](../archive/verification/phase-two.md)、[事实评估](../archive/verification/batch-one-facts-quality.md) |
| 病理与病灶 | 病理严格正确 1/10；病灶真实阳性关联为 0，`retention_gate_passed=false`。见[病理验证](../archive/verification/batch-three-pathology-ihc.md)、[病灶验证](../archive/verification/batch-three-lesion-relations.md) |
| 云影像来源 | 二维码识别原评估 TP 4 / FP 3 / FN 2，108 页未判定；没有由此取得外部影像原件或建立完整质量结论。见[云影像验证](../archive/verification/batch-three-cloud-imaging-pr1.md) |
| 癌种与分子报告 | 癌种原评估 TP 1 / FP 0 / FN 10，完整表达 0/11；第二次真实评估及分子 M7 真实评估未启动。见[癌种验证](../archive/verification/cancer-ordering.md)、[分子验证](../archive/verification/batch-three-molecular-application.md) |
| 日常记录并发 | 本轮需求的隔离 PostgreSQL 并发验证尚未补齐；其他版本或模块的 PostgreSQL 通过数不证明该门禁完成。见[日常记录验证](../archive/verification/daily-self-records.md) |
| 删除功能的数据迁移 | v4.0.0 的正式提交验证中止，发布候选测试、业务全量与真实 PostgreSQL 历史库迁移未运行；开发阶段合成 SQLite 迁移通过不覆盖真实历史库。见[删减验证](../archive/verification/feature-pruning.md) |

## 生产门禁

[机器门禁](release-evidence.json)及其[生成报告](release-gate.md)记录
**23 项必需门禁，8 项 passed、15 项 pending，决定为 BLOCKED**。
该记录的生成时间为 `2026-09-05T00:04:06+08:00`；通过数、旧测试总数及“本机缺少”
某项服务的文字属于原证据检查点，不是当前开发机环境探测或当前版本全面通过声明。

后续源码版本已有 PostgreSQL 测试、镜像构建和 smoke 证据，但尚未据此完成生产门禁
要求的独立证明，不能自动把对应 `pending` 改为 `passed`。目前未关闭的门禁包括：

| 必需证明 | 待验证项数 | 执行说明 |
| --- | --- | --- |
| 受支持版本的 Chrome、Edge 与真实 Safari 完整流程 | 8 | [外部浏览器兼容性](external-compatibility.md) |
| 跨浏览器键盘、读屏、缩放和高对比度人工验收 | 1 | [浏览器与无障碍](browser-accessibility.md) |
| PostgreSQL 并发事务的生产门禁证明 | 1 | [门禁记录](release-gate.md)及 `tests/integration/test_processing_postgres_concurrency.py` |
| 正式短信网关、真实私有 S3、多进程 Web/Worker/Beat 完整编排 | 3 | [生产操作手册](../deployment/production-runbook.md) |
| 固定容量与负载下的性能 | 1 | [性能验收](performance.md) |
| 加密备份、恢复与删除账本演练 | 1 | [备份恢复验收](backup-restore.md) |

外部门禁需要带时区的执行时间、环境、命令、结果和制品 SHA-256。WebKit 参考运行
不能替代真实 Safari；跳过测试、合成契约、测试收集或文档校验均不能代替实际执行。
源代码发布与生产放行分别判断。

## 证据维护与复核

[原 PRD 追踪矩阵](traceability.md)保留 62 个历史编号：59 项 `verified`、
2 项 `external_pending`、1 项 `retired`。它覆盖 PRD v1.0 的原始范围，不是当前版本
的完整需求清单；单患者阶段、后续多患者能力和日常记录改造不能混用同一验收分母。
AC-13 已随功能删减撤销，AC-22 与 SCN-26 的外部浏览器证明仍未完成。

维护文档、矩阵与门禁材料时运行：

```powershell
python tools/verify_documentation.py
python tools/verify_traceability.py
python tools/verify_release_gate.py
```

这些命令检查登记、引用及证据契约的一致性，不重新执行业务测试；门禁材料校验成功
仍可得到 `BLOCKED`。生产放行检查使用 `python tools/verify_release_gate.py --require-pass`。
需要重跑功能验证时，按[本地验证与提交流程](../policies/local-submit.md)确认源码、
策略、命令及环境指纹，不直接复用旧通过数。

阶段性计划、旧版交付与历史运行记录由[文档中心](../README.md)的归档入口查阅。
机器制品保留原运行身份和哈希。仓库测试使用合成数据；授权真实资料评估的原件、
标注及带原文导出保存在私有位置，公开材料仅记录计数、哈希及方法。
