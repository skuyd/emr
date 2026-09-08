# 治疗方案、周期与派生输出实施计划

目标是完整交付[治疗周期设计](../specs/2026-09-08-treatment-cycles-and-derived-exports.md)，
对应[五批计划](2026-09-07-batches-one-five-implementation.md) Task 8 剩余范围：B4-01/B4-02 和全部
派生数据的选定速查/导出，包含已发布 B4-03 的个人变化字段。

## Global Constraints

- 分支 `feat/batch-four-treatment-cycles`，开始前已重新 fetch，从最新 origin/main
  `56dec08c4c66988710fea867cc142f086ba1985f` 创建独立 worktree；未使用未合并功能作为基线。
- 新文件以下标为新增，现有接口已按当前 main 核查。B3 报告与 B5 自记录必须先合 main 才接入，
  保留 `build_snapshot/assert_snapshot_current` 和现有 clinical/self_records 数组。
- 开始实施时公共功能版本未知；源码现由 Release Please 发布为 [v1.12.0](../releases/v1.12.0.md)，
  未手工修改自动版本字段。资料、证据、审计、真实作者与撤权契约完整。
- 使用现有本地能力及既有授权样本；不访问私有云资料、不上传真实医疗内容。私有金标先独立审阅冻结，
  新周期预测后运行，全部输入/未判断计数保留；≥80% 按联合候选 precision，同表报告 recall 与整体错漏。
- 按有意义的红例、实现、绿例顺序推进。PG 竞争用独立库，浏览器必须实际执行；不得拿 SQLite 串行行为
  代替生产数据库竞争，不把质量目标与功能验收混同。

## 实现任务

### Task 8.1：冻结输入与原件周期标注

独立 worktree 从最新 main 创建；只读已授权的全量原件/OCR定位清单。先完成标注、独立审查和 SHA 冻结，写标注校验器
与缺失/重复来源反例。公开仅覆盖计数/hash。预测算法和真实评测在此之后开始；可并行设计合成边界。

### Task 8.2：事件提取与自动提议纯函数

实际新增 `apps/treatments/signals.py`、`proposals.py`、`input_material.py`，由 `derivations.py`
接只读提议和锁内保存；规则身份位于 `signals.py`，读取当前 Fact/元数据适配器。
先红例覆盖 C/D 同句关联、跨年/D8反推、无年/月精度、计划/否定、多个方案、医嘱开始/停止角色、住院
聚类、三锚点周期性、仅谷值无治疗线索。实现自动提议，保留无提议/冲突原因，禁止合成金标回读。

### Task 8.3：持久化、修订与真实 actor 服务

新增 `apps/treatments/models.py`、`migrations/0001_initial.py`、`services.py`、`readmodels.py`、`lifecycle.py`，
注册 app。覆盖真实页面当前404及重复决定/冲突/越权失败后实现方案事件周期状态、幂等提议保存、
全部决定和来源失效。MigrationExecutor 保留全部旧UUID/FactRevision/导出会话与审计，新域为空。

### Task 8.4：日历/周期组织与相对天叠图

新增 `views.py/urls.py/forms.py/timeline.py/overlays.py` 及 `templates/treatments/`；复用既有
`apps/documents/archive.py` 的实际搜索/分类/日期/分页，在档案与趋势模板提供治疗入口。
比较资格复用现有 comparable_cell；若需单点显示，不调用会删除单日系列的 `_series_for_code` 来误删数据。
先红例覆盖点/文件集合保全、闰年实际日差、负日、同日多份、末周期、换方案/暂停、未知日期、最低并列；
实际浏览器验证模式切换/纠正/合并拆分拒绝/来源/桌面与360宽移动/键盘/空结果。

### Task 8.5：全部派生数据的选定速查与导出

新增 `apps/exports/treatment.py`，扩展 `content.py/forms.py/formats.py/pdf.py` 与准备/预览模板。
添加 personal_changes/周期来源依赖与可逆schema，保留 B3 clinical 数组和旧CSV列。
验证缺所选基线不泄露、不改基线、各种null原因、A4正文/附页、真实PDF/JSON/CSV/ZIP值和来源一致、
CSV公式防御、无文档用户补记。同步受限分享projection/细选及失效；与其他已合并功能按main顺序合B5接口。

### Task 8.6：失效、审计与 PG 竞争

扩展 `apps/operations/audit.py/patient_audit.py`、`apps/core/tenant.py`、文档/患者删除钩子。
真实PG独立库 emr_treatment_cycles_test：确认 vs 确认、纠正 vs 解析切换、合并/拆分重放、来源删除恢复、
构建中撤权、成员降级/账号注销等待及作者FK、队列导出 vs 修订、流中周期/源变更；用实际阻塞/独立提交
证明锁语义。无患者/源授权的后台调用拒绝，不能借已确认周期或分享grant通行。

### Task 8.7：固定真实质量、完整回归及公开交付

在已冻结标注上运行真实持久化评测并保留所有64/124范围；功能、质量和未判断各报。
运行相关 Django/迁移/源生命周期/事实/检验/导出/家庭回归、PG全集、必要浏览器、JS、文档/追踪/版本检查，
合适时按同CI完整验证。独立审查后只提交去标识证据；中文 Conventional PR标题/正文校验，暂存及新增历史
隐私检查。一个完整 Task8功能PR为默认，若拆PR必须各自权限/导出/删除闭环，后续从最新main重新起步。
CI与Squash按已授权流程核对，Release Please实际版本后再回填；不提前标五批整体verified。

## 验证与交付

Windows PowerShell 设置 `$env:PYTHONUTF8='1'`，在本 worktree 执行相关 Python/JS、迁移和真实浏览器测试。
PostgreSQL 使用独立 `emr_treatment_cycles_test`，只通过测试设置连接；不得操作其他任务或业务库。
新领域测试拟放在 `tests/treatments/`，并扩展 `tests/exports/`、`tests/patients/` 与必要生命周期回归。
独立原件标注审查、功能源码审查、最终精确源码/测试身份和新增提交历史检查分别保存证据。

完成涉及文档/验证的步骤运行 `python tools/verify_documentation.py`；交付时同时运行
`python tools/verify_traceability.py`、`python tools/release_version.py check` 与相关仓库门禁。
实际 PR 标题/正文先通过 Conventional Commits 校验，再等待该精确 head 的必要 CI，按 Squash merge 合入。
Release Please 已确定 v1.12.0 并回填关联；本计划为 implemented，原 80% 质量目标未建立，五批整体继续 implementing。

## 当前执行证据

实际实现及全量、PG、浏览器与首次固定源结果见
[治疗周期验证](../verification/batch-four-treatment-cycles.md)。首次真实执行冻结为 6b5fd23，
其后 0caf1b0 只补页面返回前来源/历史身份复验，该边界通过独立 PG 复核。最新
91008cc 根据合成反例修复计划/取消作用范围，并保留明确执行断言的否定、拟议和未知限定，
规则升为 2。c31b346 的最终普通全量 2315 通过/2 个 Windows 符号链接权限跳过，
PostgreSQL 全集 113 通过/无跳过，必跑上传浏览器 8 通过；应用文件保持已审 91008cc。
之后首轮 CI 发现共享 SQLite 浏览器清库与下载收尾竞争，129f003 只修复测试夹具并经独立复验。
最终 4ecc79e 的功能 CI 和 08182e8 的发布 CI 各四项通过，普通各 2317 通过/4 个 Windows
启动器用例跳过、PostgreSQL 各 113 通过/无跳过。PR #60 已 Squash 合并为 d08e57b，
随 v1.12.0 发布；精确身份见[交付制品](../verification/artifacts/batch-four-treatment-cycles-delivery.json)。
完整真实输入仍为 64 文件/124 页，两次报告分别保留原字节。
核准后规则 2 只执行一次新的真实重放，六分量评分与首跑相同，没有质量提升；逐项正确
日期 10、字面日期 11、方案 1、原词序号 1 全部同源保留，丢失/新增均为 0。

联合周期没有独立可判断正例；两次原严格评分均为 12 FP、19 未判断，80% 目标未建立。
严格字段展开/边界和重复来源不匹配与临床含义分开解释，不修改 gold 或协议提高分数。
本计划登记为 `implemented`，表示源码已交付而原质量目标仍待证明；五批整体继续
`implementing`。功能/CI 和评分事实的验证不替代质量目标，不用计划执行日志判定整体完成。
