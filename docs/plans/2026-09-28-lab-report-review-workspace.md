# 检验报告逐份核对确认实施计划

> 实施依据：[逐份核对确认规格](../specs/2026-09-28-lab-report-review-workspace.md)。开发在从最新 `origin/main` 建立的独立 worktree 中进行；本计划的勾选仅记执行过程，交付状态以登记表及验证证据为准。

**目标：** 用户在一页逐份对照完整原件、修订或补录指标、排除误识别并确认整份报告；旧批量确认与授权复核停止提供功能。

**架构：** 以 `LabReportUnit` 和报告关联枚举当前完整来源，不从结果反推来源。`apps/labs` 的统一读取和原子提交服务使用既有患者与文档锁、不可变修订事件及共享有效结果投影；页面复用现有原图渲染与定位能力。历史批量、单项和复核数据保留只读审计价值。

**技术栈：** Django 5.2、PostgreSQL/SQLite 测试、服务端模板、现有原图查看器、pytest、Playwright。

## 全局约束

- 一次提交仅作用于一份范围明确的报告；确认包括本次编辑，普通保存不确认。
- 未确定归属或无可用解析版本时不创建报告单元、解析版本或整份确认；报告关联管理保持独立。
- 人工来源不得伪装 OCR；原件未提供的值不得由目录推写为原文；旧识别及修订历史不得覆盖。
- 当前报告版本、来源、关联和字段发生变化时拒绝旧提交；相同操作重试不重复写入。
- 取消授权复核的所有访问分支，包括字典候选来源；保留既有有效结果及历史。
- 普通对比、趋势、实时输出使用同一有效投影；静态分享/导出继续遵守既有选定范围与撤回规则。

## 计划与验证

### 1. 完整报告读取及来源指纹

**文件：** `apps/labs/report_reads.py`、`apps/labs/reports.py`、新建 `apps/labs/report_workspace.py`；`tests/labs/test_report_workspace.py`。

**接口：** `report_workspace(patient, *, selected_key=None)` 返回逐份导航、报告单元、来源页、全部指标、待处理问题和报告/来源/关联/结果指纹；`report_workspace_token(...)` 为提交生成签名令牌。

- [x] 先测零指标报告与续页、同文件多报告/同页区域边界、重复来源、待处理原图和无明确报告范围（AC-03～05）。
- [x] 验证新测试因现有读取不完整而失败，再从当前 `LabReportUnit` 与有效关联构建报告集合并复核来源页归属。
- [x] 测指纹在报告字段、指标、解析版本、关联或原件删除后改变，其他独立报告变更不影响当前报告（AC-12、15）。
- [x] 运行 `python -m pytest tests/labs/test_report_workspace.py -q`。

### 2. 人工指标身份、来源与重解析衔接

**文件：** `apps/labs/models.py`、`apps/labs/migrations/0009_*.py`、`apps/labs/readmodels.py`、`apps/labs/revisions.py`、`apps/labs/report_workspace.py`；`tests/labs/test_report_workspace_manual_versions.py`、`tests/labs/test_report_revision_versions.py`。

**接口：** `add_manual_observation(...)` 在明确的报告单元及原页创建 `LabObservation`/`SourceEvidence`，赋予跨版本稳定人工身份；`effective_rows(...)` 将人工来源与新识别唯一匹配或标为待处理。

- [x] 先测零指标单元补录、伪造患者/页/区域、无解析版本、相同操作重试、数字/比较符/定性/半定量/状态和目录外原文（AC-04、07～08、16）。
- [x] 测人工项目在重新识别仍漏项、唯一匹配、歧义匹配、区域变化及历史版本读取中的行为（AC-13～14）。
- [x] 最小迁移增加稳定身份与明确人工来源，约束同版本同身份唯一；人工证据保留空 OCR 块/置信度/精确框。
- [x] 实现重解析后的有效项目合并与同页待处理表示，禁止静默丢失或双计。
- [x] 运行目标测试和 `python manage.py makemigrations --check --dry-run`。

### 3. 原文更正、排除与共享有效投影

**文件：** `apps/labs/models.py`、`apps/labs/revisions.py`、`apps/labs/readmodels.py`、`apps/labs/validation.py`；`tests/labs/test_report_workspace_submit.py` 及对比/趋势/输出回归。

**接口：** 扩展 `append_revision(...)` 的可编辑原文字段及 `EXCLUDE`/`RESTORE` 动作；`effective_observation(...)` 暴露排除状态和有效来源，普通 `effective_rows(...)` 排除无效项。

- [x] 先测无效更正、目录外名称、空可选字段、原文类型重算、排除原因与恢复历史（AC-06～09）。
- [x] 测对比、趋势、实时导出/分享只读取保留项目，旧快照范围不扩大（AC-09、18）。
- [x] 实现修订事件与有效投影，重算类型、标准匹配、参考范围、质量状态，不改变自动识别原值。
- [x] 运行目标测试及相关读取回归。

### 4. 单份原子提交与整份确认

**文件：** `apps/labs/report_workspace.py`、`apps/labs/models.py`、迁移；`tests/labs/test_report_workspace_submit.py`、PostgreSQL 并发测试。

**接口：** `submit_report_workspace(patient, actor, report_key, token, operation_id, edits, *, confirm=False)` 在锁内复核当前单份来源及版本，原子追加指标/报告修订和单份确认事件；重复提交只返回原结果。

- [x] 先测跨报告 ID、多报告请求、越权、过期指纹、同 ID 不同请求和任一字段无效时零写入（AC-15～16、21）。
- [x] 测保存与确认分离、确认带编辑、确认后改动/来源变动失效、待处理问题阻止确认（AC-10～12）。
- [x] 新确认事件记录完整来源、有效指标、排除、报告信息、作者及最终指纹；旧批量事件只作历史。
- [x] 使用现有患者→上传批次→文档→结果锁顺序实现整体事务，真实 PostgreSQL 测两窗口和解析激活竞争。

### 5. 单页交互与导航

**文件：** `apps/labs/report_views.py`、`apps/labs/urls.py`、新建 `templates/labs/report_workspace.html`、必要的静态脚本及样式；`tests/labs/test_report_workspace_views.py`、`tests/browser/test_lab_report_workspace_browser.py`。

- [x] 先测患者/VIEWER 权限、空态、处理中、零指标、上一份/下一份、合法返回位置和提交错误保留输入（AC-01、04、10～11、16）。
- [x] 桌面左原图右全部指标；手机上下布局；来源页切换、可靠坐标高亮、无坐标页定位、编辑焦点及未保存离开提示（AC-05、17）。
- [x] 同页提供报告信息、多字段更正、补录、排除/恢复、保存/确认/确认并下一份；提交仅引用当前报告（AC-06、10、21）。
- [x] 运行视图、JavaScript 与真实桌面/手机浏览器主流程。

### 6. 旧内容确认入口收口

**文件：** `apps/labs/views.py`、`apps/labs/report_views.py`、`apps/labs/urls.py`、`templates/labs/{base,comparison,observation,report_detail}.html`、资料详情模板及患者 URL 范围映射；`tests/labs/test_report_workspace_routes.py`。

- [x] 先测旧批量、单项和报告信息 POST 均不写入，GET 重验权限后定位统一页面；报告关联独立写入仍可用（AC-01～02）。
- [x] 把患者侧结果链接导向所属报告位置，移除平行确认表单及批量多选；普通原图只读路由保留。
- [x] 测旧链接 `return_to` 只接受站内安全目标，历史解析内容不直接落到当前确认态（AC-01、14）。

### 7. 授权复核功能下线

**文件：** `apps/labs/views.py`、`apps/labs/review.py`、`apps/labs/dictionary_workflow.py`、相关导航模板与审计路由；`tests/labs/test_review_retirement.py`。

- [x] 先测旧任务 GET/POST/source/image、未过期任务、旧权限、通知入口和字典候选间接读取均不能访问任务内容或写入（AC-19）。
- [x] 关闭创建/分配/执行与全部任务原件授权，移除页面入口；任务表和既有结果/修订历史保留（AC-20）。
- [x] 回归普通患者/字典管理自身合法权限，以及非检验核对（AC-16、18）。

### 8. 跨流程验收、文档与交付

**文件：** 相关回归测试、`docs/verification/` 验证记录、`docs/README.md`、`docs/document-registry.json`、需求追踪。

- [ ] 对 AC-01～AC-21 逐条记录可重复证据；覆盖 SQLite 服务/视图、PostgreSQL 事务、桌面/手机浏览器、对比/趋势/导出/分享与授权下线。
- [ ] 运行 `python tools/verify_documentation.py`、迁移与系统检查、适用的回归门禁；未运行或失败项如实留在证据中。
- [ ] 独立审查全部差异并修复发现的问题；检查暂存区与新增提交不含腾讯云本地资料。
- [ ] 按 `docs/policies/local-submit.md` 使用本地 submit 完成 PR Squash 和 Release Please 发布；版本确定后再更新对应发布清单。

## 验收映射

| 验收场景 | 计划步骤 |
| --- | --- |
| AC-01～02 | 5、6 |
| AC-03～05 | 1、2、5 |
| AC-06～09 | 2、3、4、5 |
| AC-10～12 | 1、4、5 |
| AC-13～15 | 1、2、4、6 |
| AC-16～18 | 3、5、6、7、8 |
| AC-19～20 | 7 |
| AC-21 | 4、5、6 |
