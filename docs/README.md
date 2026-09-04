# 项目文档中心

这里是项目文档的唯一总入口。文档状态和版本关联以
[`document-registry.json`](document-registry.json) 为准；存储、命名和维护规则见
[《文档管理规范》](policies/document-governance.md)。

## 当前状态

| 项目 | 当前结论 | 权威来源 |
| --- | --- | --- |
| 源代码版本 | `0.3.0`，标签 `v0.3.0` | [v0.3.0 版本清单](releases/v0.3.0.md) |
| 产品需求追踪 | 60 项已验证，2 项待外部验证 | [需求追踪矩阵](verification/traceability.md) |
| 生产放行 | `BLOCKED`，7/23 通过 | [上线放行门禁](verification/release-gate.md) |
| Changelog | Release Please 自动维护 | [产品变更记录](../CHANGELOG.md) |

源代码版本已发布不等于生产环境已放行。在发布门禁变为 `PASS` 前，不得接入真实用户或
真实医疗资料。

## 推荐阅读顺序

1. [项目介绍、开发与部署入口](../README.md)
2. [产品方案 v2.0](product/产品方案-v2.0-评审完善稿.md)
3. [PRD v1.0](product/第一版产品需求文档-PRD-v1.0.md)
4. [V1 系统设计](specs/2026-08-30-phr-v1-system-design.md)
5. [需求追踪矩阵](verification/traceability.md)
6. [当前版本清单](releases/v0.3.0.md)
7. [生产部署与运行手册](deployment/production-runbook.md)

## 如何判断文档和开发进度

登记表使用两个互不替代的状态：

- `lifecycle` 表示文档有效性：`draft`、`active`、`superseded`、`archived`；
- `delivery` 表示所述工作的交付状态：`not_applicable`、`planned`、`implementing`、
  `implemented`、`verified`、`blocked`。

历史计划中的复选框只记录当时的执行步骤。文件日期、目录位置和未勾选数量都不能用来
判断当前开发进度；应查看登记表中的 `delivery`、实现提交和验证证据。

## 产品与决策

| 文档 | 有效性 | 交付状态 | 关联版本 |
| --- | --- | --- | --- |
| [产品方案 v2.0](product/产品方案-v2.0-评审完善稿.md) | active | planned | 待确定 |
| [PRD v1.0](product/第一版产品需求文档-PRD-v1.0.md) | active | verified | 0.1.0 |
| [现有方案全面审查结论](decisions/方案审查结论.md) | active | not_applicable | — |

## 设计规格

| 文档 | 有效性 | 交付状态 | 关联版本 |
| --- | --- | --- | --- |
| [视觉风格画廊设计](specs/2026-08-29-phr-visual-style-gallery-design.md) | superseded | verified | 0.1.0 |
| [V1 系统设计](specs/2026-08-30-phr-v1-system-design.md) | active | verified | 0.1.0 |
| [暖笺 UI 与双重认证设计](specs/2026-08-31-health-home-warm-ui-auth-design.md) | active | verified | 0.1.0 |
| [健康趋势总览设计](specs/2026-09-02-health-trend-index-design.md) | active | verified | 0.1.0 |
| [待提交工作集成设计](specs/2026-09-03-pending-work-integration-design.md) | active | verified | 0.2.0–0.3.0 |
| [文档治理与版本关联设计](specs/2026-09-04-document-governance-design.md) | active | implementing | 待合并 |

## 实施计划

以下计划保留为实施方法和历史记录，实时状态以登记表和证据为准。

| 文档 | 有效性 | 交付状态 |
| --- | --- | --- |
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
| [文档治理实施计划](plans/2026-09-04-document-governance.md) | active | implementing |

## 管理规范

- [文档管理规范](policies/document-governance.md)
- [自动版本号与 Changelog 流程](policies/versioning.md)

## 版本清单

- [v0.1.0](releases/v0.1.0.md)：V1 历史基线；当前缺少对应 Git 标签。
- [v0.2.0](releases/v0.2.0.md)：自动版本与 Changelog。
- [v0.2.1](releases/v0.2.1.md)：本地环境密钥初始化修复。
- [v0.3.0](releases/v0.3.0.md)：本地处理工作进程，当前源代码版本。

## 验证证据

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

- [本地开发环境](deployment/local-development.md)
- [生产部署与运行手册](deployment/production-runbook.md)
- [PDF 组件许可说明](licenses/pdf-components.md)

## 维护检查

新增、移动、取代、归档或删除文档后运行：

```powershell
python tools/verify_documentation.py
```

提交 PR 前还应运行与改动最接近的测试。完整版本规则见
[《自动版本号与 Changelog 流程》](policies/versioning.md)。
