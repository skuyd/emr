# 第三阶段实施计划

目标：完成[已确认需求](../specs/2026-09-06-phase-three-requirements.md)中的 P3-01 至
P3-08，并逐项执行 P3-AC01 至 P3-AC16。开发分支 `feat/phase-three`，独立工作区
`.worktrees/phase-three`，已获取的最新 `origin/main` 基线为
`b9e86edfc30f79ea223e018b72b55a30e2365ac4`。当前状态见[登记表](../document-registry.json)。

## Global Constraints

- 一个账号对应一位患者；所有读取、修改、生成、下载都校验归属和有效状态。
- 回收站保留连续 30 × 24 小时，永久清理沿用 24 小时目标；导出从生成完成起最多保留
  24 小时。恢复不重启旧处理任务、不恢复复核授权、不释放或重复计算原件存储。
- 保留原始表达、否定、不确定性、日期精度、同日多结果和不同来源的冲突。
  事实必须对照原件核对；检验继续使用第二阶段有效读取及质量/可比性规则。
- PDF 正文 A4 一页，溢出必须由用户调整选择或选择附页；预览及导出共用冻结快照。
  版本或来源变化使旧生成结果失效。没有公开下载或分享链接。
- 不开发离线查看、治疗推断、家庭成员、在线分享及后续阶段功能。
- 不手改发布版本或 Changelog；遵守文档治理。真实示例只在已有授权的私有位置处理，
  仓库仅保留可公开的合成夹具、计数、哈希及范围说明。生产门禁保持实际结论。

## 模块与锁边界

文档继续以 `deleted_at` 隔离全部非正常资料，新增回收站时间与状态版本；旧删除记录
默认保持永久删除语义。资料生命周期服务遵循患者、批次、文档、任务的锁顺序，永久
删除任务只对已经不可恢复的状态执行对象清理。新增 `apps/facts/` 保存候选和追加式
核对历史；`apps/exports/` 组合有效内容、保存快照和生成私有文件。界面沿用 Django
服务端模板。检验不建立第二套判断规则。

## 实施任务

### Task 1: 资料生命周期与回收站

修改 `apps/documents/models.py`、`deletion.py`、`tasks.py`、`views/records.py`、
`urls.py`、`apps/accounts/deletion.py`、`apps/operations/tombstones.py`，新增生命周期
服务、迁移和回收站模板。保留原件与解析，撤销复核和工作租约；到期与提前删除进入
已有永久清理队列。原件、页面对象和数据库级联清理失败保持隔离并可重试。

验收：`tests/documents/test_recycle_bin.py`、原删除/账号/账本回归及 PostgreSQL 竞争
用例，覆盖 30 天边界、重复上传、配额、再次移入、恢复与清理竞态、旧记录迁移。
先验证缺少回收站时的新行为用例失败，再完成实现。对应 AC12–AC15。

### Task 2: 事实候选、核对与重解析

新增 `apps/facts/` 模型、抽取、有效读取、修订和界面，接入
`apps/processing/pipeline.py` 与文档详情。候选保留来源页、原文、解析版本和真实区域；
仅根据明确标题及原文摘录，不推断结论。支持失败状态、人工补录、确认/更正/暂缓/
排除/撤销。自动与人工分开，重解析按来源与内容对应核对有效性，不能覆盖修订。

验收：`tests/facts/` 覆盖所有事实类型、缺失及部分日期、否定和冲突、来源归属、
版本切换、确认失效、修订历史及恢复。对应 AC01–AC04。

### Task 3: 选择、速查内容与快照

新增 `apps/exports/` 内容组合与快照模型，复用 `apps/labs/readmodels.py`、`revisions.py`
和可比规则。提供六部分选择、资料/日期/全部筛选、未知日期独立选择、排除原因和
数量预览。快照包含文档、事实、检验版本及状态引用，校验失败要求重新确认。

验收：`tests/exports/` 覆盖六部分、空内容、筛选首尾边界及不完整日期、同日多结果、
受限值、撤销确认与资料版本变化。对应 AC05、AC06、AC10、AC11。

### Task 4: PDF、原件、CSV、JSON 与 ZIP

新增 PDF 排版、结构化序列化、打包及下载服务和模板。PDF 使用可验证的中文字体，
正文溢出可调整或转附页；保存同一快照。原件保持字节不变；CSV 拆分关联实体并防
公式，JSON 保留原文并带格式版本；ZIP 清单记录相对路径、原名、来源和 SHA-256。

验收：使用标准 PDF、CSV、JSON、ZIP 读取器验证固定合成产物；渲染 PDF 检查中文、
比较符、表格、来源索引及页数。原件单份/批量哈希一致。对应 AC07–AC09。

### Task 5: 导出任务生命周期及主流程界面

完善私有暂存、失败/取消/重试、24 小时失效清理和账号删除协作。生成及下载再次验证
会话、账号、患者与来源版本，部分失败不能成为成功。提供可达的核对、速查、导出及
回收站入口，明确下载副本与服务器期限。后台使用持久状态与可恢复清理任务。

验收：跨账号、过期会话、注销、生成中内容变更、存储失败、取消和期限边界的测试；
浏览器执行核对→预览→下载→移入→恢复→永久删除。对应 AC10–AC16。

### Task 6: 固定样本评估、完整回归与交付证据

依据已有授权示例原件建立事实标注，记录类型、报告组、可判断字段及排除理由。
失败/未抽取纳入总数，真实与合成分别报告正确/错配/漏抽/无法判断、精确率/召回率、
核对负担及未覆盖范围。真实缺少类型保持未验证，不假定总体医学准确率。

逐项建立 AC01–AC16 的代码、执行命令和结果对应证据，保存在
`docs/verification/phase-three.md` 及允许的机器制品目录，更新登记表与入口。
运行 `python tools/verify_documentation.py`、Django 检查与迁移检查、当前 CI 规定的
Python/浏览器/JS/PostgreSQL 回归，执行合成和可用真实样本评估。
完成代码核查与当前状态验证后提交功能分支；外部合并及生产放行不由本地测试替代。

## 执行记录

- 基线验证：删除、账号删除、账本重放、检验修订和处理 runner 共 61 项测试通过。
- 已接入普通删除转回收站、恢复及永久删除页面、30 天期限、原件与页面清理、
  账号注销和永久账本协作；已接入事实候选、原件对照、人工补录、追加式核对历史和
  相同来源重解析的修订继承。速查卡、导出快照、文件生成及其失效清理尚待实现。
- 2026-09-06：`python -m pytest -q tests/documents tests/processing
  tests/accounts/test_account_deletion.py tests/operations/test_restore_tombstones.py tests/facts
  tests/labs/test_phase_two_workflows.py -rs --tb=short` 最新执行 426 passed、1 skipped。
  跳过项为尚未启用真实 PaddleOCR 模型环境的 `test_paddle_adapter.py` 模型测试。
  事实表单、歧义来源、质量限制保留和真实迁移边界已纳入该回归；随后保留未知日期原文
  的修改完成 10 项事实测试复验。PostgreSQL 两项并发用例也已复验通过。
  完整三阶段矩阵尚未验收，不将这些局部结果解释为阶段完成。
- 独立 PostgreSQL 测试库位于本工作区 `.runtime/postgres/data`，仅监听
  `127.0.0.1:55433`，测试连接为 `postgresql://phr_test@127.0.0.1:55433/phr_phase_three_test`。
  `python tools/run_required_tests.py -q --ds=config.settings.postgres_test
  tests/integration/test_phase_three_postgres_concurrency.py` 执行 2 passed。
  该库为本任务新建的合成测试环境，与原有运行数据库无关。
- `python manage.py check`、`python manage.py makemigrations --check --dry-run`
  （测试 settings）和 `python tools/verify_documentation.py` 已通过。
- 为后续 PDF 接入 ReportLab 锁定依赖及附带授权的中文字体；字体探针可嵌入并从 PDF
  读回中文和比较符。这不是成品 PDF 或版面验收证据。
