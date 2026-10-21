# 项目文档中心

本入口说明项目当前状态。文档有效性、交付状态和关联证据以
[文档登记表](document-registry.json)为准；已完成的实施过程和旧版记录进入[历史归档](archive/README.md)。

## 当前状态

当前源码版本为 [v5.1.1](releases/v5.1.1.md)。现有功能、操作流程及已移除范围统一见
[当前产品状态](product/current-state.md)，具体约束见下方有效规格。生产放行仍为 **BLOCKED**。

日常记录的 PostgreSQL 并发专项、真实提取质量与外部生产验收等缺口仍保留，详见
[验证与生产放行](verification/README.md)。历史版本的通过记录不能替代当前版本全量验证。

## 产品与使用

- [项目说明与快速开始](../README.md)
- [当前产品状态](product/current-state.md)
- [当前版本 v5.1.1](releases/v5.1.1.md)
- [Changelog](../CHANGELOG.md)

## 当前设计规格

- [报告癌种候选与指标显示顺序](specs/2026-09-08-cancer-candidates-and-ordering.md)
- [结构化临床证据基础与影像字段](specs/2026-09-08-clinical-evidence-foundation.md)
- [影像、病理、分子资料与排序规格](specs/2026-09-08-clinical-followup.md)
- [云影像来源、核对与显式访问设计](specs/2026-09-08-cloud-imaging-sources.md)
- [病理、IHC 与分子检测字段设计](specs/2026-09-08-pathology-molecular-evidence.md)
- [稳定病灶、范围侧别与选定输出](specs/2026-09-09-lesion-relations.md)
- [分子检测应用接入合同](specs/2026-09-10-molecular-application.md)
- [分子报告字段值合同](specs/2026-09-10-molecular-value-contracts.md)
- [检验对比需求规格](specs/2026-09-14-lab-comparison-optimization.md)
- [检验报告上传校验、报告归并与结果去重需求规格](specs/2026-09-17-lab-report-consolidation.md)
- [固定检验指标目录与患者适用参考范围需求规格](specs/2026-09-22-lab-indicator-catalog-and-reference-ranges.md)
- [检验报告整理与来源关联需求规格](specs/2026-09-28-lab-report-organization.md)
- [检验报告逐份核对确认需求规格](specs/2026-09-28-lab-report-review-workspace.md)
- [submit 按影响选择验证范围](specs/2026-09-30-submit-scoped-validation.md)
- [日常记录需求规格](specs/2026-10-01-daily-self-records.md)

## 验证与放行

- [验证与生产放行](verification/README.md)
- [备份、恢复与删除账本验证](verification/backup-restore.md)
- [浏览器与无障碍验证](verification/browser-accessibility.md)
- [外部浏览器兼容性验证](verification/external-compatibility.md)
- [固定负载性能验证](verification/performance.md)
- [V1 上线放行门禁](verification/release-gate.md)
- [PRD v1.0 需求追踪矩阵](verification/traceability.md)

## 开发与部署

- [Local development services](deployment/local-development.md)：Windows 启动器默认使用仓库 `.venv`。
- [家庭健康资料系统 V1 部署与运行手册](deployment/production-runbook.md)
- [隔离合成体验配置](deployment/synthetic-trial.md)

## 协作规范与许可

- [PDF components and release notices](licenses/pdf-components.md)
- [文档管理规范](policies/document-governance.md)
- [检验报告 OCR 异常处理规则](policies/lab-ocr-exception-rules.md)
- [本地 submit 使用指南](policies/local-submit.md)
- [自动版本号与 Changelog 流程](policies/versioning.md)

## 历史资料与维护

[历史归档](archive/README.md)收录旧产品方案、阶段性规格、实施计划、运行记录、整理前快照与历史版本清单。
这些资料只用于追溯，正文中的“当前”“下一步”和测试结果均属于其原检查点。

修改文档后运行：

```powershell
python tools/verify_documentation.py
python tools/verify_traceability.py
python tools/verify_release_gate.py
```

这些命令验证文档和证据关系，不代表重新执行业务验证。
