# 治疗方案、周期与派生输出实施计划

目标是完整交付[治疗周期设计](../specs/2026-09-08-treatment-cycles-and-derived-exports.md)，
对应[五批计划](2026-09-07-batches-one-five-implementation.md) Task 8 剩余范围：B4-01/B4-02 和全部
派生数据的选定速查/导出，包含已发布 B4-03 的个人变化字段。

## Global Constraints

- 分支 `feat/batch-four-treatment-cycles`，开始前已重新 fetch，从最新 origin/main
  `56dec08c4c66988710fea867cc142f086ba1985f` 创建独立 worktree；未使用未合并功能作为基线。
- 新文件以下标为新增，现有接口已按当前 main 核查。B3 报告与 B5 自记录必须先合 main 才接入，
  保留 `build_snapshot/assert_snapshot_current` 和现有 clinical/self_records 数组。
- 公共功能版本未知，releases=[]；不手工修改自动版本字段。资料、证据、审计、真实作者与撤权契约完整。
- 使用现有本地能力及既有授权样本；不访问私有云资料、不上传真实医疗内容。私有金标先独立审阅冻结，
  新周期预测后运行，全部输入/未判断计数保留；≥80% 按联合候选 precision，同表报告 recall 与整体错漏。
- 按有意义的红例、实现、绿例顺序推进。PG 竞争用独立库，浏览器必须实际执行；不得拿 SQLite 串行行为
  代替生产数据库竞争，不把质量目标与功能验收混同。

## 实现任务

### Task 8.1：冻结输入与原件周期标注

独立 worktree 从最新 main 创建；只读已授权的全量原件/OCR定位清单。先完成标注、独立审查和 SHA 冻结，写标注校验器
与缺失/重复来源反例。公开仅覆盖计数/hash。预测算法和真实评测在此之后开始；可并行设计合成边界。

### Task 8.2：事件提取与自动提议纯函数

新增 `apps/treatments/extraction.py`、`proposals.py`、`types.py`、`rules.py`；读取 Fact/元数据适配器。
先红例覆盖 C/D 同句关联、跨年/D8反推、无年/月精度、计划/否定、多个方案、医嘱开始/停止角色、住院
聚类、三锚点周期性、仅谷值无治疗线索。实现自动提议，保留无提议/冲突原因，禁止合成金标回读。

### Task 8.3：持久化、修订与真实 actor 服务

新增 `apps/treatments/models.py`、`migrations/0001_initial.py`、`services.py`、`readmodels.py`、`lifecycle.py`，
注册 app。覆盖真实页面当前404及重复决定/冲突/越权失败后实现方案事件周期状态、幂等提议保存、
全部决定和来源失效。MigrationExecutor 保留全部旧UUID/FactRevision/导出会话与审计，新域为空。

### Task 8.4：日历/周期组织与相对天叠图

新增 `views.py/urls.py/forms.py/timeline.py/overlays.py` 及 `templates/treatments/`；扩展已确认存在的
`apps/documents/views/records.py`、`archive.py`、`templates/documents/records.html`、趋势入口与对比模板。
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
Release Please 确定真实版本后另行回填关联，当前整批状态继续 implementing。
