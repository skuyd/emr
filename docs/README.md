# 项目文档中心

这里是项目文档的唯一总入口。文档状态和版本关联以
[`document-registry.json`](document-registry.json) 为准；存储、命名和维护规则见
[《文档管理规范》](policies/document-governance.md)。

## 当前状态

分子字段值合同 A0 已通过 145 项合成合同及既有临床/影像回归，独立审查通过；
[PR #72](https://github.com/skuyd/emr/pull/72) 精确 CI 通过并合入主线 `6be1419`，源码包含于 v1.15.0；首次计费阻塞历史证据保留。
完整应用链路仍待完成，见[本地验证记录](verification/molecular-value-contracts.md)。

| 项目 | 当前结论 | 权威来源 |
| --- | --- | --- |
| 源代码版本 | `v1.16.1` 标签与 GitHub Release 已发布，功能／发布 PR、准确发布提交的主线 CI 及自动发布成功 | [v1.16.1 版本清单](releases/v1.16.1.md)、[v1.16.0](releases/v1.16.0.md) |
| 后续五批开发 | 正在实施；第一批检验关联通过本地回归，真实联合 F1 57.92% | [检验关联验证](verification/batch-one-labs-quality.md) |
| 检验抽取范围修复 | 已随 `v1.1.1` 发布，额外误抽减少 95.74% | [修复验证记录](verification/labs-extraction-scope.md) |
| 第二阶段 | 九项功能与十一项验收完成本地验证，真实质量目标存在差距 | [第二阶段验证记录](verification/phase-two.md) |
| 第三阶段 | 八项功能与十六项验收完成本地验证；已随 v1.2.0 发布，真实自动提取仍需大量核对与补录 | [第三阶段验证记录](verification/phase-three.md) |
| 档案与指标核对交互优化 | 637 项本地回归、83 项同步复测及功能／发布 CI 通过，已随 v1.3.1 发布 | [交互优化验证记录](verification/record-review-ux.md) |
| 第 1 批图像增强与来源坐标 | 本地验证、独立审查与功能/发布 CI 通过，已随 v1.4.0 发布；五批整体仍在实施 | [图像增强验证记录](verification/batch-one-image-enhancement.md) |
| 第 1 批事实提取质量 | 正确 60→97、漏提 157→31；精确率 78.95%→48.02%，候选核对量 76→202；已随 v1.4.1 发布 | [事实质量验证记录](verification/batch-one-facts-quality.md) |
| 第 1 批检验关联 | 联合 F1 41.81%→57.92%，仍有原件核对限制；已随 v1.5.0 发布 | [检验关联验证](verification/batch-one-labs-quality.md) |
| 第 2 批多患者权限基础 | 本地验证、独立审查及 CI 通过，已随 v1.5.0 发布；邀请与分享随后随 v1.8.0 发布 | [家庭访问权限验证](verification/batch-two-family-access.md) |
| 第 1 批非单据提示与资料恢复 | 本地验证、独立审查及功能/发布 CI 通过，已随 v1.6.0 发布 | [非单据与恢复验证](verification/batch-one-material-recovery.md) |
| 第 4 批多指标与个人变化 | 读视图已随 v1.7.0 发布；周期、叠图及个人变化选定输出已随 v1.12.0 发布，周期质量目标仍未建立 | [高级趋势验证](verification/batch-four-personal-trends.md) |
| 第 4 批治疗周期与派生输出 | 源码已随 v1.12.0 发布，独审及功能/发布 CI 通过；两次真实集均无联合周期正例、12 FP/19 未判断，80% 目标未建立且复测无改善 | [治疗周期验证](verification/batch-four-treatment-cycles.md) |
| 第 2 批邀请、分享与访问审计 | 本地集成验证、独审及功能/发布 CI 通过，已随 v1.8.0 发布；五批整体仍在实施 | [家庭邀请与分享验证](verification/batch-two-family-sharing.md) |
| 第 3 批结构化证据基础 | 七类影像字段核对、导出与分享经本地验证、两轮独审及功能/发布 CI 通过，随 v1.9.0 发布；严格正确 24→37，仍有 50 错配/10 额外，B3 整体仍在实施 | [临床基础验证](verification/batch-three-clinical-foundation.md) |
| 第 3 批 SUV 与对比原文 | 四类字段及主线 1.2 组合通过本地验证、独审和功能/发布 CI，已随 v1.11.0 发布；固定 54 目标，严格正确 27→33，仍有 10 错配/11 漏提/2 额外 | [影像量化验证](verification/batch-three-imaging-quantitative.md) |
| 第 3 批病理与 IHC | 独审与完整 CI 通过，PR #70 已合主线 `4b73d2e` 并随 v1.16.0 发布；第三次病理切片严格正确仍为 1/10，旧质量限制保留 | [病理验证](verification/batch-three-pathology-ihc.md) |
| 第 3 批云影像来源与核对 | PR 1 功能/发布 CI 通过且已随 v1.14.0 发布，主线第二次 CI 已成功；真实 QR 页面 TP4/FP3/FN2，108 页金标未判定；显式输出/分享待 PR 3 | [云影像 PR 1 验证](verification/batch-three-cloud-imaging-pr1.md) |
| 第 3 批云影像受控打开 | PR 2 本地验证、独审及精确 CI 通过，随 v1.15.0 发布；选定输出和有限分享仍属 PR 3 | [云影像 PR 2 验证](verification/batch-three-cloud-imaging-pr2.md) |
| 第 3 批云影像选定输出 | PR 3 本地验证与独审通过，`bf64c41` 的完整 CI 成功；合入发布文档后的最终提交仍待 CI/合并 | [云影像 PR 3 验证](verification/batch-three-cloud-imaging-pr3.md) |
| 第 5 批日常记录 | B5-01 的体重、体温、症状及修订、选定导出/分享通过本地验证、独审和功能/发布 CI，已随 v1.10.0 发布；日内血糖另见 B5-02 交付 | [日常记录验证](verification/batch-five-daily-records.md) |
| 第 5 批日内血糖 | 基础功能随 v1.13.0 发布，同排空腹申请拆块修复随 v1.15.0 发布；固定 7 个来源行恢复 3→4 的原评分不变，真实单位、时段与来源证明仍有缺口 | [日内血糖验证](verification/batch-five-glucose.md) |
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
6. [最新已发布版本清单](releases/v1.16.1.md)与[前一版本](releases/v1.16.0.md)
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
邀请、限时分享和访问审计已有[Task 5 实现及验证记录](verification/batch-two-family-sharing.md)，
本地集成检查、独立审查及功能/发布 CI 通过，已随 [v1.8.0](releases/v1.8.0.md) 发布。
首批影像字段与共享证据基础已随 [v1.9.0](releases/v1.9.0.md) 发布，实际字段细选分享和审计
已完成增量独审。体重、体温和自述症状的 B5-01 日常记录及其选定导出/分享已随
[v1.10.0](releases/v1.10.0.md) 发布，见[验证与交付记录](verification/batch-five-daily-records.md)。
SUV、明确最大限定、对比原文及引用日期已随 [v1.11.0](releases/v1.11.0.md) 发布，
见[影像量化交付记录](verification/batch-three-imaging-quantitative.md)。治疗方案、自动周期提议、
周期叠图及全部 Task 8 派生数据选定输出已随 [v1.12.0](releases/v1.12.0.md) 发布，
见[治疗周期交付记录](verification/batch-four-treatment-cycles.md)。Task 8 规格/计划为 `implemented`，
原 80% 周期目标尚未建立，两次真实集评分无改善。日内血糖、来源核对、图表及选定导出/分享
已随 [v1.13.0](releases/v1.13.0.md) 发布，见[血糖交付记录](verification/batch-five-glucose.md)；
真实单位、时段与来源证明仍有缺口。跨报告病灶关联、云影像、病理/基因字段及癌种排序继续
实施，新类型分享仍需随对应功能另行验证。

治疗方案的[原文分组修复](verification/treatment-regimen-identity.md)通过合成验证与独审，已提交
[PR #74](https://github.com/skuyd/emr/pull/74)，修复小数、范围、组合标点和缺损引号导致的身份丢失。
最终提交 `eff1fdd` 的完整 CI 与独审通过，已 Squash 合入 `cc68b47`，随 [v1.16.1](releases/v1.16.1.md) 发布。
该修复不计入 v1.16.0；原真实周期质量结果及账户计费阻塞证据保留。

病理/IHC 与分子检测的[细化设计](specs/2026-09-08-pathology-molecular-evidence.md)
及[实施计划](plans/2026-09-08-pathology-molecular-evidence.md)已通过具体合同独审：固定 124 页
中已目视核对 27 页、97 页未判断；另有首批 10 字段原件 gold/协议通过独审。病理/IHC 核心、
解析、核对页面与选定输出已有分段独审；三次获批真实评测及独立回读保留来源证明缺口，
第三次严格正确 1/10，质量尚未通过。分块元数据及不可变原值/值/标签窗口已补充，
章节归属修复已通过定向独审。主线云影像默认省略规则的集成、病理导出回读和分享分组兼容
已在 `a36cc22` 完成本地验证及独审；随后独立的 64 来源/124 页旧任务捕获及只读续比对完成，
五个历史检查点的旧正确项均保留，202 条旧摘录无增减；初始检查点仍有原值或来源差异，
详见[匿名兼容性制品](verification/artifacts/batch-three-pathology-old-task-compatibility.json)。
原执行 EXIT1 与各轮评测保持不变；[PR #70](https://github.com/skuyd/emr/pull/70) 已通过
独审与完整 CI，并合入主线 `4b73d2e`，已随 [v1.16.0](releases/v1.16.0.md) 发布。
完整分子应用仍在实施、尚未交付；旧结果和金标准保留，再次真实预测另需新执行身份批准。

云影像 PR 1 的本地来源扫描、原页核对和默认输出保护已有
[功能与首次真实验证记录](verification/batch-three-cloud-imaging-pr1.md)，独审与功能/发布 CI 通过且已合并；
[v1.14.0](releases/v1.14.0.md) 已由 Release Please 创建标签和 Release；主线第二次 CI 已成功，生产门禁保持。
64 文件/124 页均扫描，108 页存在性金标未知；文献明文清单没有已断定的云门户阳性，
不能将其 51 FN 当作云入口漏识别。PR 2 受控打开已有
[验证记录](verification/batch-three-cloud-imaging-pr2.md)，应用独审及精确 CI 通过，PR #68 已合并并随 v1.15.0 发布；
PR 3 明确选定输出/分享已完成[本地验证与独审](verification/batch-three-cloud-imaging-pr3.md)，`bf64c41` 完整 CI 成功；发布文档合流后的最终提交仍待 CI/合并，三 PR 整体仍在实施。

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
| [病理、IHC 与分子检测字段设计](specs/2026-09-08-pathology-molecular-evidence.md) | active | implementing | 待确定 |
| [分子报告字段值合同与实施检查点](specs/2026-09-10-molecular-value-contracts.md) | active | implementing（A0 值合同；应用链路仍待完成） | 1.15.0（仅包含合同源码） |
| [云影像来源、核对与显式访问设计](specs/2026-09-08-cloud-imaging-sources.md) | active | implementing（PR 1、PR 2 已发布，PR 3 未交付） | 1.14.0 / 1.15.0 |
| [第三批剩余影像、病理、分子与排序需求](specs/2026-09-08-clinical-followup.md) | active | implementing | 1.11.0 / 1.14.0 / 1.15.0（已交付部分） |
| [结构化临床证据基础与首批影像字段](specs/2026-09-08-clinical-evidence-foundation.md) | active | verified（本次基础范围） | 1.9.0 |
| [日内血糖记录与来源导入](specs/2026-09-08-intraday-glucose.md) | active | implemented（真实质量限制保留） | 1.13.0 / 1.15.0 |
| [后续第 1—5 批完整需求](specs/2026-09-07-batches-one-five-requirements.md) | active | implementing | 1.4.0 / 1.4.1 / 1.5.0 / 1.6.0 / 1.7.0 / 1.8.0 / 1.9.0 / 1.10.0 / 1.11.0 / 1.12.0 / 1.13.0 / 1.14.0 / 1.15.0（已交付部分） |
| [第三阶段需求范围](specs/2026-09-06-phase-three-requirements.md) | active | verified | 1.2.0 |
| [第二阶段需求范围](specs/2026-09-06-phase-two-requirements.md) | active | verified | 1.1.0 |
| [视觉风格画廊设计](specs/2026-08-29-phr-visual-style-gallery-design.md) | superseded | verified | 0.1.0 |
| [V1 系统设计](specs/2026-08-30-phr-v1-system-design.md) | active | verified | 0.1.0 |
| [暖笺 UI 与双重认证设计](specs/2026-08-31-health-home-warm-ui-auth-design.md) | active | verified | 0.1.0 |
| [健康趋势总览设计](specs/2026-09-02-health-trend-index-design.md) | active | verified | 0.1.0 |
| [多指标对照与个人变化设计](specs/2026-09-08-personal-trend-comparison.md) | active | verified（读视图及选定输出） | 1.7.0 / 1.12.0 |
| [治疗方案、周期与派生输出设计](specs/2026-09-08-treatment-cycles-and-derived-exports.md) | active | implemented（80% 质量目标未建立） | 1.12.0 |
| [日常自记录与修订设计](specs/2026-09-08-daily-self-records.md) | active | verified（B5-01） | 1.10.0 |
| [待提交工作集成设计](specs/2026-09-03-pending-work-integration-design.md) | active | verified | 0.2.0–0.3.0 |
| [文档治理与版本关联设计](specs/2026-09-04-document-governance-design.md) | active | verified | 0.3.1 |

## 实施计划

以下计划保留为实施方法和历史记录，实时状态以登记表和证据为准。

本轮审查修复已合并并发布为 `v1.0.0`，见[项目审查修复计划](plans/2026-09-05-project-review-remediation.md)
及[发布验证记录](verification/project-review-remediation.md)。

| 文档 | 有效性 | 交付状态 |
| --- | --- | --- |
| [病理与分子字段实施计划](plans/2026-09-08-pathology-molecular-evidence.md) | active | implementing |
| [云影像来源与受控访问实施计划](plans/2026-09-08-cloud-imaging-sources-implementation.md) | active | implementing（PR 1、PR 2 已发布，PR 3 待最终合流 CI/合并） |
| [第三批剩余临床结构化实施计划](plans/2026-09-08-clinical-followup-implementation.md) | active | implementing |
| [后续第 1—5 批实施计划](plans/2026-09-07-batches-one-five-implementation.md) | active | implementing |
| [日内血糖实施计划](plans/2026-09-08-intraday-glucose-implementation.md) | active | implemented |
| [治疗方案、周期与派生输出实施计划](plans/2026-09-08-treatment-cycles-and-derived-exports.md) | active | implemented（80% 质量目标未建立） |
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

病理/IHC 的[本地验证记录](verification/batch-three-pathology-ihc.md)和
[匿名制品](verification/artifacts/batch-three-pathology-ihc.json)保留三次原评分及来源限制。
病理质量切片仍仅一张局部 IHC 页；另有[独立旧任务兼容性制品](verification/artifacts/batch-three-pathology-old-task-compatibility.json)
记录完整捕获和旧正确项保留，初始原值/来源差异及原 EXIT1 继续保留。
完整分子应用仍在实施、尚未交付，B3 整体仍为 `implementing`。

## 管理规范

- [文档管理规范](policies/document-governance.md)
- [自动版本号与 Changelog 流程](policies/versioning.md)

自动发布工作流显式等待发布候选的四项 CI；历史私有仓库的分支保护套餐限制与并发发布
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
- [v1.7.0](releases/v1.7.0.md)：多指标对照与个人变化读视图。
- [v1.8.0](releases/v1.8.0.md)：家庭邀请、限时只读分享与患者访问审计。
- [v1.9.0](releases/v1.9.0.md)：结构化证据基础与首批影像字段核对、细选导出和分享。
- [v1.10.0](releases/v1.10.0.md)：体重、体温、症状记录及选定导出与分享。
- [v1.11.0](releases/v1.11.0.md)：影像 SUV、明确最大限定和对比原文字段的核对、选定导出与分享。
- [v1.12.0](releases/v1.12.0.md)：治疗方案、周期组织和选定派生输出；源码已发布，80% 周期质量目标未建立。
- [v1.13.0](releases/v1.13.0.md)：日内血糖、来源核对、图表及选定速查/导出/分享；源码已发布，真实质量限制保留。
- [v1.15.0](releases/v1.15.0.md)：云影像受控打开及同排空腹申请拆块修复已发布；包含 A0 合同源码，不代表完整分子应用；准确发布提交的主线 CI 已成功，生产门禁保持。
- [v1.16.0](releases/v1.16.0.md)：病理与 IHC 结构化核对和输出已发布，功能／发布及主线精确 CI 成功；严格正确 1/10 和完整分子待交付限制保留。
- [v1.16.1](releases/v1.16.1.md)：治疗方案原文分组及独立事件修复已发布，功能／发布 PR 精确 CI 成功，原周期质量与生产门禁保留。
- [v1.14.0](releases/v1.14.0.md)：云影像本地来源扫描、原页核对及默认输出保护；标签和 Release 已发布，主线第二次 CI 已成功，生产门禁保持。

## 验证证据

- [第三批云影像来源与核对验证](verification/batch-three-cloud-imaging-pr1.md)（PR 1 功能/发布 CI 各普通 2803 通过/4 跳过、PG 146 通过/零跳过、浏览器 10 通过/零跳过；1.14.0 已发布，主线第二次 CI 已成功，原真实质量限制保留）
- [第三批云影像受控打开验证](verification/batch-three-cloud-imaging-pr2.md)（应用独审通过：原源独立 18 PG/6 TLS，异常链修复后独立 22 项通过且无跳过；范围重叠不相加，PR #68 已合并，原本地验证记录保留）
- [第五批日内血糖与选定输出验证](verification/batch-five-glucose.md)（v1.13.0 功能/发布 CI 全绿，普通各 2684 通过/4 跳过、PG 各 132 通过；两次固定原件结果及质量限制保留）
- [第四批治疗周期与选定派生输出验证](verification/batch-four-treatment-cycles.md)（功能与评分事实 verified；v1.12.0 功能/发布 CI 全绿，普通各 2317 通过/4 跳过、PG 各 113 通过；真实复测无改善，80% 目标未建立）
- [第 3 批 SUV 与对比原文字段验证](verification/batch-three-imaging-quantitative.md)

- [第五批日常记录与修订验证](verification/batch-five-daily-records.md)

- [第 1 批非单据提示与资料恢复验证](verification/batch-one-material-recovery.md)
- [第二批多患者与家庭访问权限验证](verification/batch-two-family-access.md)
- [第 3 批结构化证据基础与首批影像字段验证](verification/batch-three-clinical-foundation.md)
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

- [第三批云影像选定输出与限时分享验证](verification/batch-three-cloud-imaging-pr3.md)（作者冻结普通 641／PG 34／TLS 2 项通过；旧失败和封存保留，待独审／CI，不建立真实质量或生产放行）
