# 文档管理规范

本规范是本仓库文档存储、状态、版本关联和维护流程的权威规则。机器可读状态见
[`docs/document-registry.json`](../document-registry.json)，人类阅读入口见
[`docs/README.md`](../README.md)。

## 1. 基本原则

1. 正式文档集中存放在 `docs/`，不散落到根目录、源码、测试或部署脚本目录。
2. 根目录只保留平台和仓库级约定文件，不建立第二套文档目录。
3. 文档是否仍有效与它描述的功能是否已交付分开记录。
4. 计划复选框是执行日志，不是当前进度；当前状态以登记表和验证证据为准。
5. `CHANGELOG.md` 记录用户可感知变化，版本清单负责连接需求、设计、提交和证据。
6. GitHub Release 只代表源代码版本已发布，生产放行仍由验证门禁决定。
7. 新增、移动、取代或归档文档必须同步更新登记表、总索引和所有引用。

## 2. 存储目录

| 路径 | 内容 | 负责人 |
| --- | --- | --- |
| `docs/product/` | 产品方案、PRD、范围和验收目标 | Product |
| `docs/decisions/` | 产品、架构和评审决策 | Product / Engineering |
| `docs/specs/` | 经讨论形成的设计规格，说明做什么和为什么 | Engineering |
| `docs/plans/` | 可执行实施计划，说明如何完成 | Engineering |
| `docs/policies/` | 文档、版本、协作和质量规范 | Engineering |
| `docs/releases/` | 按源代码版本组织的关联清单 | Engineering / QA |
| `docs/verification/` | 需求追踪、测试结果和生产门禁证据 | QA |
| `docs/deployment/` | 本地环境、部署、运维、备份和恢复手册 | Operations |
| `docs/licenses/` | 第三方组件许可与发行声明 | Legal / Engineering |
| `docs/archive/` | 已归档且不再作为当前依据的资料 | 原文档负责人 |

根目录只允许以下 Markdown：

- `README.md`；
- `CHANGELOG.md`；
- `AGENTS.md`；
- `LICENSE*`、`NOTICE*`。

`.github/` 中的 Pull Request、Issue 等平台模板不属于内容文档，可保留在平台要求的
位置。`deploy/` 只保存可执行配置和脚本；其说明文档统一位于 `docs/deployment/`。

## 3. 命名规则

- 规格、计划和决策记录优先使用 `YYYY-MM-DD-kebab-case.md`；
- 版本清单固定使用 `vMAJOR.MINOR.PATCH.md`；
- 其他新文档使用小写英文 `kebab-case.md`；
- 目录入口可以使用 `README.md`；
- 已有中文产品文件名作为迁移兼容保留，新文件不继续扩大例外；
- 文件名前的日期只代表创建日期，不代表生效日期、当前状态或版本。

不要在文件名中使用 `final`、`最新版`、`新`、`最终版` 等相对描述。状态和取代关系由
登记表表达。

## 4. 文档类型

登记表的 `kind` 只能使用：

- `product`：产品方案与 PRD；
- `decision`：评审或关键决策；
- `spec`：设计规格；
- `plan`：实施计划；
- `policy`：管理规则；
- `guide`：开发或验证指南；
- `runbook`：部署与运维操作；
- `evidence`：验证和放行证据；
- `release`：版本关联清单或 Changelog；
- `license`：许可说明。

## 5. 两维状态

### 5.1 文档有效性 `lifecycle`

| 值 | 含义 |
| --- | --- |
| `draft` | 尚未批准，不能作为实施依据 |
| `active` | 当前有效，可作为工作依据或历史事实来源 |
| `superseded` | 已被明确文档取代，只保留历史价值 |
| `archived` | 不再参与当前工作，只供审计查询 |

### 5.2 功能交付状态 `delivery`

| 值 | 含义 |
| --- | --- |
| `not_applicable` | 文档不代表一项可交付功能 |
| `planned` | 已规划但尚未实施 |
| `implementing` | 正在开发或等待合并 |
| `implemented` | 代码或流程已实现，但证据尚未满足验证要求 |
| `verified` | 已有可重复执行的验证证据 |
| `blocked` | 存在明确门禁或外部环境阻塞 |

状态变化必须基于事实：提交只能证明 `implemented`，只有测试、追踪矩阵或运行制品才能
证明 `verified`；跳过测试、预计结果和说明文字不能作为通过证据。

## 6. 登记表

[`docs/document-registry.json`](../document-registry.json) 是状态和关系的唯一机器权威来源。
每份登记文档必须包含：

- 稳定唯一的 `id`；
- `title` 和仓库相对 `path`；
- `kind`、`lifecycle`、`delivery`、`owner`；
- 已确定的 `releases`；
- `supersedes` 文档 ID；
- `implementation_refs`，格式为 `commit:<7-40 位 SHA>` 或 `pr:#<编号>`；
- `evidence` 仓库相对路径。

未知值使用空数组，禁止猜测版本、提交或证据。`verified` 或 `blocked` 必须有证据；已
实现、已验证或阻塞的产品、规格和计划必须有实现引用。被取代文档必须能反向找到明确
取代者。

以下内容不作为独立人类文档登记：

- `AGENTS.md` 和 `.github/` 平台模板；
- `docs/verification/*.json` 等由验证工具维护的机器制品；
- 源码注释、测试夹具和配置文件。

## 7. 文档与版本记录

版本链路为：

```text
产品/PRD → 规格 → 计划 → PR/Squash Commit
         → CHANGELOG + VERSION + Git 标签/GitHub Release
         → docs/releases/v<版本>.md
         → 验证证据 + 生产发布门禁
```

- `VERSION`、标签和 GitHub Release：标识源代码版本；
- 根目录 `CHANGELOG.md`：由 Release Please 自动维护用户可感知变化；
- `docs/releases/v<版本>.md`：关联文档、实现提交、验证证据和部署结论；
- `docs/verification/traceability.md`：证明需求覆盖；
- `docs/verification/release-gate.md`：决定能否接入真实用户和真实医疗资料。

功能开发期间若版本尚未确定，登记表保持 `releases: []`。Release Please 确定版本后，
通过 `docs` 类型 PR 建立或更新版本清单并回填关联；不得提前猜测版本。版本清单不复制
Changelog 全文，只链接相应记录并补充文档与证据关系。

## 8. 新建和修改流程

每次文档变更按以下顺序执行：

1. 选择标准目录和合规文件名；
2. 编写或修改有证据支持的内容；
3. 更新 `docs/document-registry.json`；
4. 更新 `docs/README.md` 中的入口；
5. 若实际版本已确定，更新 `docs/releases/v<版本>.md`；
6. 修复全部相对链接、代码路径和测试断言；
7. 在 PR 中填写关联需求、决策、规格、计划、证据和 Changelog 影响；
8. 运行 `python tools/verify_documentation.py` 和与变更最接近的测试。

只修改错别字或表达且不改变含义时，仍需运行文档校验，但不需要伪造新的版本、提交或
证据关联。

## 9. 取代、归档和删除

### 取代

新文档在 `supersedes` 中引用旧文档 ID，旧文档的 `lifecycle` 改为 `superseded`。旧文档
保留原路径，除非同时进行受控目录迁移。

### 归档

只有不再参与当前决策且已明确保留原因的文档才能进入 `docs/archive/`。归档前更新登记
表、索引和引用；归档不等于删除。

### 删除

仅当内容重复、无审计价值且所有引用已移除时才能删除。删除文档必须同时删除登记项，
并通过文档校验确认没有失效链接。

## 10. 自动校验与 AI 约束

标准校验命令：

```powershell
python tools/verify_documentation.py
```

CI 对每个 PR 和 `main` 推送执行该命令。校验范围包括目录边界、登记模式、状态约束、
文件与证据路径、版本清单、总索引、根 README、`AGENTS.md` 和 CI 接入。

所有 AI 或自动化代理开始工作时必须先读取根目录 `AGENTS.md`，并遵守本规范。AI 不得：

- 在标准目录外创建临时需求、设计、计划、报告或交接 Markdown；
- 用历史计划的复选框推断当前开发进度；
- 在没有验证证据时把状态标记为 `verified`；
- 手工修改 Release Please 管理的版本字段或 Changelog 自动区域；
- 只移动文件而不更新登记表、总索引、版本清单和引用。

若用户明确要求的路径与本规范冲突，AI 应先说明冲突和影响，再取得明确确认；不得静默
绕过规范。

