# 项目文档中心

这里是项目文档的唯一总入口。文档状态和版本关联以
[`document-registry.json`](document-registry.json) 为准；存储、命名和维护规则见
[《文档管理规范》](policies/document-governance.md)。

## 当前状态

| 项目 | 当前结论 | 权威来源 |
| --- | --- | --- |
| 源代码版本 | `1.7.0`，标签 `v1.7.0` | [v1.7.0 版本清单](releases/v1.7.0.md) |
| 后续五批开发 | 正在实施；第一批检验关联通过本地回归，真实联合 F1 57.92% | [检验关联验证](verification/batch-one-labs-quality.md) |
| 检验抽取范围修复 | 已随 `v1.1.1` 发布，额外误抽减少 95.74% | [修复验证记录](verification/labs-extraction-scope.md) |
| 第二阶段 | 九项功能与十一项验收完成本地验证，真实质量目标存在差距 | [第二阶段验证记录](verification/phase-two.md) |
| 第三阶段 | 八项功能与十六项验收完成本地验证；已随 v1.2.0 发布，真实自动提取仍需大量核对与补录 | [第三阶段验证记录](verification/phase-three.md) |
| 档案与指标核对交互优化 | 637 项本地回归、83 项同步复测及功能／发布 CI 通过，已随 v1.3.1 发布 | [交互优化验证记录](verification/record-review-ux.md) |
| 第 1 批图像增强与来源坐标 | 本地验证、独立审查与功能/发布 CI 通过，已随 v1.4.0 发布；五批整体仍在实施 | [图像增强验证记录](verification/batch-one-image-enhancement.md) |
| 第 1 批事实提取质量 | 正确 60→97、漏提 157→31；精确率 78.95%→48.02%，候选核对量 76→202；已随 v1.4.1 发布 | [事实质量验证记录](verification/batch-one-facts-quality.md) |
| 第 1 批检验关联 | 联合 F1 41.81%→57.92%，仍有原件核对限制；已随 v1.5.0 发布 | [检验关联验证](verification/batch-one-labs-quality.md) |
| 第 2 批多患者权限基础 | 本地验证、独立审查及 CI 通过，已随 v1.5.0 发布；邀请与分享继续实施 | [家庭访问权限验证](verification/batch-two-family-access.md) |
| 第 1 批非单据提示与资料恢复 | 本地验证、独立审查及功能/发布 CI 通过，已随 v1.6.0 发布 | [非单据与恢复验证](verification/batch-one-material-recovery.md) |
| 第 4 批多指标与个人变化 | 读视图验证、独审及 CI 通过，已随 v1.7.0 发布；周期、叠图及派生导出继续实施 | [高级趋势验证](verification/batch-four-personal-trends.md) |
| 第 2 批邀请、分享与访问审计 | 本地集成验证与独审通过，待 CI；五批整体仍在实施 | [家庭邀请与分享验证](verification/batch-two-family-sharing.md) |
| 第 3 批结构化证据基础 | 七类影像字段贯通核对、导出与精细分享；严格正确 24→37，仍有 50 错配/10 额外，等待交付 | [临床基础验证](verification/batch-three-clinical-foundation.md) |
| 产品需求追踪 | 60 项已验证，2 项待外部验证 | [需求追踪矩阵](verification/traceability.md) |
| 生产放行 | `BLOCKED`，8/23 通过 | [上线放行门禁](verification/release-gate.md) |
| Changelog | Release Please 自动维护 | [产品变更记录](../CHANGELOG.md) |

源代码版本已发布不等于生产环境已放行。在发布门禁变为 `PASS` 前，不得接入真实用户或
真实医疗资料。

## 推荐阅读顺序

1. [项目介绍、开发与部署入口](../README.md)
2. [产品方案 v2.0](product/产品方案-v2.0-评审完善稿.md)
3. [PRD v1.0](product/第一版产品需求文档-PRD-v1.0.md)
4. [V1 系统设计](specs/2026-08-30-phr-v1-system-design.md)
5. [需求追踪矩阵](verification/traceability.md)
6. [当前版本清单](releases/v1.7.0.md)
7. [生产部署与运行手册](deployment/production-runbook.md)

## 如何判断文档和开发进度

登记表使用两个互不替代的状态：

- `lifecycle` 表示文档有效性：`draft`、`active`、`superseded`、`archived`；
- `delivery` 表示所述工作的交付状态：`not_applicable`、`planned`、`implementing`、
  `implemented`、`verified`、`blocked`。

历史计划中的复选框只记录当时的执行步骤。文件日期、目录位置和未勾选数量都不能用来
判断当前开发进度；应查看登记表中的 `delivery`、实现提交和验证证据。

## 产品与决策

2026-09-07，用户授权自主执行[后续第 1—5 批完整需求](specs/2026-09-07-batches-one-five-requirements.md)：
采集与解析质量、多患者家庭协作、影像/病理/基因结构化、治疗周期与高级趋势、日常记录
及日内血糖。按[实施计划](plans/2026-09-07-batches-one-five-implementation.md)拆分 PR，
经必要检查与审查后 Squash 合并，由 Release Please 发布源码。当前为 `active / implementing`，
图像增强已通过[验证与交付检查](verification/batch-one-image-enhancement.md)，随 v1.4.0 发布；
检验关联和多患者权限基础已随 [v1.5.0](releases/v1.5.0.md) 发布；非单据提示随
[v1.6.0](releases/v1.6.0.md)、多指标与个人变化读视图随 [v1.7.0](releases/v1.7.0.md) 发布。
邀请与分享、结构化报告、治疗周期、派生数据的选定速查/导出及自记录仍按各自实现与验证继续推进。
邀请、限时分享和访问审计已有[Task 5 实现及验证记录](verification/batch-two-family-sharing.md)，
本地集成检查与独立审查通过，尚未确定发布版本。

事实提取已有[固定全量质量与核对量证据](verification/batch-one-facts-quality.md)：
原有 60 条正确事实逐项保留，新增医嘱覆盖同时增加人工检查和纠错候选；独立复审及 CI 通过，
已随 [v1.4.1](releases/v1.4.1.md) 发布。五批总状态仍为 `implementing`，不由单项验证推定整体完成。

2026-09-06，用户确认[第三阶段需求规格](specs/2026-09-06-phase-three-requirements.md)：
事实候选经原件核对后纳入速查卡、A4 一页正文与可选附页、PDF/原件/CSV/JSON/ZIP
导出，以及保留 30 天的回收站与恢复。用户明确本期先不做离线查看，并在书面规格
审阅环节要求将规格合入 `main`。功能现已按[实施计划](plans/2026-09-06-phase-three-implementation.md)
完成本地验收，规格登记为 `active / verified`，见[三阶段证据](verification/phase-three.md)。
真实固定集保留 64 文件、60 报告组与未判断范围，自动提取精确率 60/76、召回率 60/232，
不代表总体医学准确率。功能 PR #23 的四项 CI 已通过并 Squash 合并，包含 Docker 构建；
Release Please 已发布 [v1.2.0](releases/v1.2.0.md)，见[交付证据](verification/artifacts/phase-three-delivery.json)。

2026-09-06，用户确认[第二阶段九项需求](specs/2026-09-06-phase-two-requirements.md)，
聚焦“准确率与可信”，以现有示例及合成边界用例验收，取消固定 200 份报告要求。
本地[示例清单](verification/artifacts/phase-two-sample-inventory.json)记录 64 个文件，
按内容已归为 60 个报告组，其中 40 个文件、42 个检验报告组完成 734 个源行表示的
双轮标注及分歧裁定，范围与本期运行证据见[第二阶段验证记录](verification/phase-two.md)。
第一阶段未完成的验证按用户安排先搁置，
待验证项继续保留在原证据记录中。
[原始产品需求](product/original-product-requirements.md)是后续功能的参考来源，具体实施
范围由各阶段确认后的规格决定。该参考文档保留用户提供的原始正文，并补充用途说明。

| 文档 | 有效性 | 交付状态 | 关联版本 |
| --- | --- | --- | --- |
| [原始产品需求参考](product/original-product-requirements.md) | active | not_applicable | — |
| [产品方案 v2.0](product/产品方案-v2.0-评审完善稿.md) | active | planned | 待确定 |
| [PRD v1.0](product/第一版产品需求文档-PRD-v1.0.md) | active | verified | 0.1.0 |
| [现有方案全面审查结论](decisions/方案审查结论.md) | active | not_applicable | — |

## 设计规格

| 文档 | 有效性 | 交付状态 | 关联版本 |
| --- | --- | --- | --- |
| [后续第 1—5 批完整需求](specs/2026-09-07-batches-one-five-requirements.md) | active | implementing | 1.4.0 / 1.4.1 / 1.5.0 / 1.6.0 / 1.7.0（已交付部分） |
| [结构化临床证据基础与首批影像字段](specs/2026-09-08-clinical-evidence-foundation.md) | active | implementing | 待确定 |
| [第三阶段需求范围](specs/2026-09-06-phase-three-requirements.md) | active | verified | 1.2.0 |
| [第二阶段需求范围](specs/2026-09-06-phase-two-requirements.md) | active | verified | 1.1.0 |
| [视觉风格画廊设计](specs/2026-08-29-phr-visual-style-gallery-design.md) | superseded | verified | 0.1.0 |
| [V1 系统设计](specs/2026-08-30-phr-v1-system-design.md) | active | verified | 0.1.0 |
| [暖笺 UI 与双重认证设计](specs/2026-08-31-health-home-warm-ui-auth-design.md) | active | verified | 0.1.0 |
| [健康趋势总览设计](specs/2026-09-02-health-trend-index-design.md) | active | verified | 0.1.0 |
| [多指标对照与个人变化设计](specs/2026-09-08-personal-trend-comparison.md) | active | verified（读视图） | 1.7.0 |
| [待提交工作集成设计](specs/2026-09-03-pending-work-integration-design.md) | active | verified | 0.2.0–0.3.0 |
| [文档治理与版本关联设计](specs/2026-09-04-document-governance-design.md) | active | verified | 0.3.1 |

## 实施计划

以下计划保留为实施方法和历史记录，实时状态以登记表和证据为准。

本轮审查修复已合并并发布为 `v1.0.0`，见[项目审查修复计划](plans/2026-09-05-project-review-remediation.md)
及[发布验证记录](verification/project-review-remediation.md)。

| 文档 | 有效性 | 交付状态 |
| --- | --- | --- |
| [后续第 1—5 批实施计划](plans/2026-09-07-batches-one-five-implementation.md) | active | implementing |
| [第三阶段实施计划](plans/2026-09-06-phase-three-implementation.md) | active | verified |
| [第二阶段实现计划](plans/2026-09-06-phase-two-implementation.md) | active | verified |
| [视觉风格画廊计划](plans/2026-08-29-phr-visual-style-gallery.md) | superseded | verified |
| [V1 病案、检索、查看器与趋势计划](plans/2026-08-30-phr-v1-archive-search-viewer-trends.md) | active | verified |
| [V1 基础与建档计划](plans/2026-08-30-phr-v1-foundation-onboarding.md) | active | verified |
| [V1 隐私、通知、运维与发布计划](plans/2026-08-30-phr-v1-privacy-notifications-operations-release.md) | active | verified |
| [V1 处理与字典计划](plans/2026-08-30-phr-v1-processing-dictionary.md) | active | verified |
| [V1 上传与存储计划](plans/2026-08-30-phr-v1-upload-storage.md) | active | verified |
| [双重认证计划](plans/2026-08-31-health-home-authentication.md) | active | verified |
| [暖笺 UI 计划](plans/2026-08-31-health-home-warm-ui.md) | active | verified |
| [健康趋势总览计划](plans/2026-09-02-health-trend-index.md) | active | verified |
| [待提交工作集成计划](plans/2026-09-03-pending-work-integration.md) | active | verified |
| [文档治理实施计划](plans/2026-09-04-document-governance.md) | active | verified |
| [项目审查修复计划](plans/2026-09-05-project-review-remediation.md) | active | verified |

## 管理规范

- [文档管理规范](policies/document-governance.md)
- [自动版本号与 Changelog 流程](policies/versioning.md)

自动发布工作流显式等待发布候选的四项 CI；当前私有仓库的分支保护套餐限制与并发发布
边界见该流程说明及[项目审查验证记录](verification/project-review-remediation.md)。

## 版本清单

- [v0.1.0](releases/v0.1.0.md)：V1 历史基线；当前缺少对应 Git 标签。
- [v0.2.0](releases/v0.2.0.md)：自动版本与 Changelog。
- [v0.2.1](releases/v0.2.1.md)：本地环境密钥初始化修复。
- [v0.3.0](releases/v0.3.0.md)：本地处理工作进程。
- [v0.3.1](releases/v0.3.1.md)：文档治理校验修复。
- [v1.0.0](releases/v1.0.0.md)：资料处理、认证、并发与工程验证修复。
- [v1.0.1](releases/v1.0.1.md)：原件整页预览与检查名称展示。
- [v1.1.0](releases/v1.1.0.md)：第二阶段检验解析、修订复核与可信对比。
- [v1.1.1](releases/v1.1.1.md)：检验抽取范围修复。
- [v1.2.0](releases/v1.2.0.md)：第三阶段事实核对、就诊速查、资料导出与回收站。
- [v1.2.1](releases/v1.2.1.md)：顶部导航菜单换行修复。
- [v1.3.0](releases/v1.3.0.md)：隔离体验模式与本地上传稳定性，已发布。
- [v1.3.1](releases/v1.3.1.md)：档案详情与指标核对交互优化。
- [v1.4.0](releases/v1.4.0.md)：可回溯的单据图像增强。
- [v1.4.1](releases/v1.4.1.md)：医嘱表与事实边界修复，保留核对成本及真实质量限制。
- [v1.5.0](releases/v1.5.0.md)：检验关联修复、多患者与家庭权限基础。
- [v1.6.0](releases/v1.6.0.md)：可恢复的非单据提示，全部原件及页面保留。
- [v1.7.0](releases/v1.7.0.md)：多指标对照与个人变化读视图，当前源代码版本。

## 验证证据

- [第 3 批结构化证据基础与首批影像字段验证](verification/batch-three-clinical-foundation.md)

- [第 1 批非单据提示与资料恢复验证](verification/batch-one-material-recovery.md)
- [第二批多患者与家庭访问权限验证](verification/batch-two-family-access.md)
- [第二批邀请、分享与访问审计验证](verification/batch-two-family-sharing.md)
- [第四批多指标对照与个人变化验证](verification/batch-four-personal-trends.md)
- [第 1 批事实提取质量验证](verification/batch-one-facts-quality.md)
- [第 1 批图像增强与来源坐标验证](verification/batch-one-image-enhancement.md)
- [健康档案与指标核对交互优化验证](verification/record-review-ux.md)
- [第三阶段验证记录](verification/phase-three.md)
- [第一批检验关联质量验证](verification/batch-one-labs-quality.md)
- [检验抽取范围修复验证](verification/labs-extraction-scope.md)
- [第二阶段验证记录](verification/phase-two.md)
- [项目审查修复验证记录](verification/project-review-remediation.md)
- [验证证据说明](verification/README.md)
- [需求追踪矩阵](verification/traceability.md)
- [上线放行门禁](verification/release-gate.md)
- [AC-00 / AC-01 验证记录](verification/ac00-ac01.md)
- [AC-02 至 AC-07 验证记录](verification/ac02-ac07.md)
- [健康之家暖笺 UI 验证](verification/health-home-warm-ui.md)
- [浏览器与无障碍验证](verification/browser-accessibility.md)
- [外部浏览器兼容性验证](verification/external-compatibility.md)
- [固定负载性能验证](verification/performance.md)
- [备份、恢复与删除账本验证](verification/backup-restore.md)

## 开发与部署

- [本地开发环境](deployment/local-development.md)：启动服务、SQLite 并发写入与中断任务恢复。
- [生产部署与运行手册](deployment/production-runbook.md)
- [隔离合成体验配置](deployment/synthetic-trial.md)
- [PDF 组件许可说明](licenses/pdf-components.md)

## 维护检查

新增、移动、取代、归档或删除文档后运行：

```powershell
python tools/verify_documentation.py
```

提交 PR 前还应运行与改动最接近的测试。完整版本规则见
[《自动版本号与 Changelog 流程》](policies/versioning.md)。
