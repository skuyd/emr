# 文档治理与版本关联设计

## 背景与已验证基线

本设计以 `main` 的 `478fc07`（`v0.3.0`）为基线。2026-09-04 在不加载项目根目录
`.env` 的隔离工作区中，Python 回归结果为 `935 passed, 6 skipped`；版本自动化、版本
一致性、Django 检查、迁移漂移检查、JavaScript 测试、需求追踪和发布门禁校验均可执行。
需求追踪为 62 项，其中 60 项 `verified`、2 项 `external_pending`；生产发布门禁为
`BLOCKED`，23 项必需门禁中 7 项通过、16 项待验证。

仓库当前有 3 份根目录产品文档、5 份设计规格、10 份实施计划、12 份验证制品、2 份
部署说明以及版本规则与许可证说明。10 份实施计划共有 319 个未勾选任务，0 个已勾选
任务，但相应功能大部分已进入代码和测试。因此，计划复选框只能作为当时的执行步骤，
不能继续承担当前进度状态的职责。

## 目标

建立一个可维护、可校验的文档治理体系，使团队可以从单一入口回答以下问题：

1. 每份文档的用途、有效性和负责人是什么；
2. 文档描述的功能处于计划、实施、实现、验证还是阻塞状态；
3. 某个源代码版本对应哪些需求、规格、计划、提交和验证证据；
4. `CHANGELOG.md`、Git 标签、GitHub Release 与生产放行之间分别代表什么；
5. 新增、移动、取代或归档文档时需要同步修改什么，以及 CI 如何发现遗漏。

## 非目标

- 本次不批量移动或重命名历史文档，避免破坏现有链接和 Git 历史；
- 不回填 319 个历史计划复选框，也不把它们解释为实时进度；
- 不改变 Release Please 的版本计算或 `CHANGELOG.md` 自动生成规则；
- 不把 GitHub Release 或源代码标签表述为生产已放行；
- 不修改产品功能、生产门禁结论或外部验证结果；
- 不在本次顺带修复根目录本地 `.env` 影响测试隔离的问题。

## 文档模型

### 权威入口

`docs/README.md` 是面向人的文档总入口，提供阅读顺序、角色说明、状态说明、当前版本
入口和按类别整理的文档索引。根目录 `README.md` 只保留项目介绍、开发与部署入口，
不再维护另一份完整文档清单。

`docs/document-registry.json` 是面向机器的权威登记表。索引中出现的状态和关联关系以
登记表为准；Markdown 计划中的复选框、文件日期和目录位置都不能覆盖登记表状态。

### 两套状态

文档有效性与功能交付进度是两个不同维度，必须分开记录：

- `lifecycle`：`draft`、`active`、`superseded`、`archived`；
- `delivery`：`not_applicable`、`planned`、`implementing`、`implemented`、`verified`、
  `blocked`。

`lifecycle` 回答“这份文档现在是否仍是有效参考”；`delivery` 回答“文档所描述的工作
交付到了哪里”。例如，历史实施计划可以是 `active + verified`，表示它仍是有效的实现
记录且对应工作已验证；旧视觉方案可以是 `superseded + verified`，表示曾实施并验证，
但已经被后续方案取代。

### 文档类别

登记表允许以下 `kind`：

- `product`：产品方向、方案和 PRD；
- `decision`：评审结论与关键决策记录；
- `spec`：描述做什么、为什么以及系统边界；
- `plan`：描述如何实施，不承担实时进度职责；
- `policy`：文档治理、版本和协作规则；
- `guide`：开发、验证或组件使用说明；
- `runbook`：生产部署和运维操作；
- `evidence`：需求追踪、测试记录与放行门禁；
- `release`：某个源代码版本的关联清单；
- `license`：第三方组件许可与发布声明。

### 登记字段

`docs/document-registry.json` 使用 `schema_version: 1`，每个 `documents` 条目包含：

- `id`：稳定、唯一的小写连字符标识；
- `title`：人类可读标题；
- `path`：相对仓库根目录、使用 `/` 的真实路径；
- `kind`、`lifecycle`、`delivery`；
- `owner`：`product`、`engineering`、`qa`、`operations` 或 `legal`；
- `releases`：已经确定关联的 SemVer 数组，未知版本不得预填；
- `supersedes`：本条目明确取代的文档 ID 数组；
- `implementation_refs`：`commit:<sha>` 或 `pr:#<number>` 数组；
- `evidence`：仓库内证据路径数组。

所有字段都显式存在；没有值时使用空数组，避免“字段缺失”和“尚未建立关联”混淆。
`implemented`、`verified` 或 `blocked` 的规格与计划必须包含实现引用；`verified` 或
`blocked` 必须包含证据。`superseded` 文档必须能从其他条目的 `supersedes` 找到取代者，
或者在登记表中保留明确的历史例外说明。

## 版本与 Changelog 的职责边界

版本记录链路如下：

```text
产品/PRD → 规格 → 计划 → PR/Squash Commit
         → CHANGELOG + VERSION + Git 标签/GitHub Release
         → docs/releases/v<版本>.md
         → 验证证据 + 生产发布门禁
```

- `VERSION`、Git 标签和 GitHub Release 标识源代码版本；
- `CHANGELOG.md` 由 Release Please 维护，只记录用户可感知的 `feat`、`fix`、`perf`；
- `docs/releases/v<版本>.md` 解释该版本与相关文档、实现提交、验证证据和部署结论的
  关系，不复制完整 Changelog；
- `docs/verification/traceability.md` 说明需求是否有证据；
- `docs/verification/release-gate.md` 决定能否接入真实用户和真实医疗资料。

首次迁移为 `0.1.0`、`0.2.0`、`0.2.1` 和 `0.3.0` 建立版本清单。`0.1.0` 没有现存
Git 标签，清单必须如实记录为历史基线缺口，不补造标签。未来只有在版本号已经由
Release Please 确定后，才能把文档关联到该版本；功能开发阶段保持 `releases: []`。
版本清单更新使用 `docs` 类型 PR，因此不会再次触发产品版本。

## 管理范围与迁移策略

本次登记以下 Markdown 范围：

- 根目录 `README.md`、`CHANGELOG.md` 和 3 份产品/评审文档；
- `docs/**/*.md`；
- `deploy/*.md`。

`AGENTS.md`、Pull Request 模板、代码内注释和自动化工具不作为内容文档登记，但它们
必须指向本规范。JSON 验证制品继续由现有验证工具管理，由对应的 Markdown 权威入口
引用；不把机器生成 JSON 当作独立阅读文档重复登记。

迁移时保留现有目录结构。只在有明确取代关系时标记 `superseded`；不确定的历史资料
保留为 `active` 并通过交付状态说明现状。后续需要归档时，先更新登记表和所有引用，
再移动到 `docs/archive/`，不得直接删除以“整理目录”。

## 自动校验

新增 `tools/verify_documentation.py`，只使用 Python 标准库，并提供 `--root` 测试入口。
校验至少覆盖：

1. 登记表是合法 JSON，模式版本、枚举、必填字段和字段类型正确；
2. ID 和路径唯一，路径规范化、位于仓库内且文件真实存在；
3. 管理范围内的所有 Markdown 都已登记，登记表中没有失效路径；
4. `supersedes` 引用、证据路径、实现引用和状态约束有效；
5. `releases` 使用 SemVer，并存在对应 `docs/releases/v<版本>.md`；
6. 每份版本清单链接 `CHANGELOG.md`、相关文档和发布门禁，且不得宣称被阻塞的生产
   发布已经放行；
7. `docs/README.md` 链接治理规范、登记表、版本规则、当前版本清单和所有登记文档；
8. CI 和根目录 README 中包含文档校验命令。

校验工具接入 `.github/workflows/ci.yml`，并由独立单元测试覆盖成功路径、漏登记、失效
引用、非法状态、缺少证据和版本清单缺失等失败路径。

## 协作流程

新文档或文档变更遵循以下顺序：

1. 创建或修改文档；
2. 同步更新 `docs/document-registry.json`；
3. 更新 `docs/README.md` 的阅读入口；
4. 若版本已确定，更新对应版本清单；未确定时不要猜版本；
5. 在 PR 模板中填写需求、规格、计划、验证证据和 Changelog 影响；
6. 运行 `python tools/verify_documentation.py` 以及与变更最接近的测试；
7. 合并后以登记表和验证证据更新交付状态，不回填历史计划复选框冒充进度。

## 验收标准

- 团队从根目录 README 能在一次跳转内到达 `docs/README.md`；
- 所有管理范围内 Markdown 文档都有唯一登记项并可从总入口找到；
- 文档有效性和交付进度使用独立字段，历史计划复选框不再被解释为当前状态；
- `0.1.0` 至 `0.3.0` 都有版本清单，且 Changelog、提交、证据与生产门禁职责清晰；
- PR 模板和 `AGENTS.md` 明确要求同步文档关系；
- CI 会运行文档校验；
- 文档校验单元测试、完整 Python 回归、JavaScript 测试和现有发布校验均通过；
- 最终说明保持生产门禁 `BLOCKED`，不因建立文档规范而改变放行结论。
