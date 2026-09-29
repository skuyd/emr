# 检验报告逐份核对确认验证记录

日期：2026-09-28；2026-09-29 更新。状态：已验证并随 [v3.0.0](../releases/v3.0.0.md) 发布。本记录对应[规格](../specs/2026-09-28-lab-report-review-workspace.md)和[计划](../plans/2026-09-28-lab-report-review-workspace.md)。测试使用合成患者、原件和隔离数据库；不代表生产部署或真实医疗资料质量验收。

## 验收追踪

| 标准 | 可重复验证入口 |
| --- | --- |
| AC-01 | `test_report_workspace_routes.py` 的旧地址、历史指标定位和安全返回；`test_report_workspace_views.py` 的患者范围；`test_lab_selection_batch_browser.py` 的筛选后返回 |
| AC-02 | `test_report_workspace_routes.py::test_old_posts_do_not_modify_content`；`test_review_retirement.py`；页面模板无旧批量和单项写入表单 |
| AC-03 | `test_report_workspace.py::test_workspace_enumerates_zero_observation_report_and_joined_zero_observation_source`；既有 `test_report_readmodels.py` 和 `test_report_relations.py` |
| AC-04 | `test_report_workspace_submit.py` 的零指标补录；`test_report_workspace_views.py` 的处理中空态 |
| AC-05 | `test_report_workspace_views.py::test_workspace_get_shows_full_report_and_only_its_sources`；`test_lab_selection_batch_browser.py` |
| AC-06 | `test_report_workspace_submit.py` 的多字段、同次更正与排除、报告字段及时间清空；`test_lab_report_workspace_browser.py` |
| AC-07 | `test_report_workspace_submit.py::test_manual_observation_in_zero_row_report_preserves_source_and_retry` 及状态原文测试 |
| AC-08 | `test_report_workspace_submit.py` 的空可选字段和采样时间；`test_confirmation_display.py` 的计算限制 |
| AC-09 | `test_report_workspace_submit.py` 的排除/恢复及共享有效读取；对比、趋势、导出和分享回归 |
| AC-10 | `test_report_workspace_submit.py` 的保存、确认、下一份；`test_report_workspace_views.py`；桌面/手机浏览器主流程 |
| AC-11 | `test_report_workspace_submit.py` 的冲突阻止确认、原子回滚；`test_report_revision_versions.py` |
| AC-12 | `test_report_workspace.py` 的版本指纹、`test_report_workspace_submit.py` 的确认失效 |
| AC-13 | `test_report_workspace_manual_versions.py` 的漏识别、唯一匹配、歧义及同页处理 |
| AC-14 | `test_report_workspace_manual_versions.py` 与 `test_report_revision_versions.py` 的跨解析版本来源和历史 |
| AC-15 | `test_report_workspace_submit.py` 的重复请求和过期指纹；`test_lab_report_workspace_postgres.py` 的真实两窗口竞争 |
| AC-16 | `test_report_workspace_views.py` 和 `test_report_workspace_submit.py` 的 VIEWER、跨患者和删除；`test_review_retirement.py` |
| AC-17 | `test_lab_report_workspace_browser.py` 及报告修订、目录阶段浏览器回归，覆盖桌面 1280px 与手机 360px；桌面双栏几何断言另覆盖浏览器初始字号增大时的 1055px 视口 |
| AC-18 | `test_confirmation_quality.py`、`test_phase_two_comparison.py`、导出/分享回归与非检验测试 |
| AC-19 | `test_review_retirement.py` 的任务 GET/POST/source/image；`test_phase_two_dictionary_workflow.py` 的成员权限；PostgreSQL 候选来源撤权竞争 |
| AC-20 | `test_review_retirement.py::test_completed_historical_review_result_remains_visible_after_retirement` 及修订历史回归 |
| AC-21 | `test_report_workspace_submit.py` 的跨报告拒绝；旧批量路由测试；浏览器连续切换与未保存提示 |

## 已执行检查

- 首轮 `python -m pytest tests/labs -q --tb=no`：814 通过、8 失败，保留为修复前事实，不作为通过证据。第二轮 826 通过、3 失败，定位为历史候选趋势、筛选返回锚点和只读控件断言，修正后对应 4 项聚焦测试通过。第三轮 `python -m pytest tests/labs -q --tb=short`：829 通过；随后新增来源建立顺序回归，先复现失败，再与桌面/手机浏览器主流程合跑 5 通过。
- `python -m pytest tests/browser/test_lab_report_workspace_browser.py tests/browser/test_lab_catalog_browser.py tests/browser/test_lab_comparison_browser.py tests/browser/test_lab_report_consolidation_browser.py tests/browser/test_lab_selection_batch_browser.py tests/browser/test_record_review_browser.py tests/browser/test_report_identity_review_browser.py -q --tb=short`：20 通过。随后主流程补测未保存切换提示与保留输入，同上述 5 项合跑通过。
- 隔离 PostgreSQL 18.6：`python -m pytest --ds=config.settings.postgres_test tests/integration/test_phase_two_postgres_concurrency.py tests/integration/test_family_sharing_postgres.py -x -q --tb=short`，23 通过；另将候选权限测试与 `test_lab_report_workspace_postgres.py` 合跑，5 通过，覆盖真实行锁等待、重复请求和旧预览拒绝。
- 报告导出、分享投影和资料趋势索引：`python -m pytest tests/exports/test_lab_report_projection.py tests/patients/test_share_projection.py tests/documents/test_trend_index.py -q --tb=short`，31 通过。人工补录排除后重解析的状态继承，以及同次更正并解决人工来源冲突，均先由新增测试复现失败；修复后 `test_report_workspace_manual_versions.py` 9 通过，另覆盖仍漏识别、唯一识别及新版本恢复。
- 旧任务停用路由的 HTTP 方法与 IDOR 契约先由 `test_csrf_and_idor.py` 复现 2 项失败；声明允许方法、从现役患者资源矩阵移除已停用的写入入口，并增加真实任务 ID 与未知 ID 的相同停用响应测试后，`python -m pytest tests/security/test_csrf_and_idor.py tests/labs/test_review_retirement.py -q --tb=short`，13 通过。
- `python manage.py check`：无系统问题；`python manage.py makemigrations --check --dry-run`：无遗漏；`node --check static/js/lab-report-workspace.js`：通过；`npm run test:js`：9 通过。
- `python tools/verify_documentation.py`：147 份登记文档通过；`python tools/verify_traceability.py`、`python tools/verify_release_gate.py`、`python tools/release_version.py check` 均通过。发布门禁结论仍为 `BLOCKED`，不代表生产放行。
- 首次本地 `submit` 的已提交候选 `351b751`：契约与 Django 检查通过；PostgreSQL 完整组 385 通过；Python 完整组 5661 通过、5 跳过、2 失败，失败均为家庭成员测试仍要求旧单项 GET 直接返回 200。修正为跟随统一核对页跳转，并检查 VIEWER 只读、患者范围及旧授权 POST 410 后，两项聚焦测试通过。浏览器、JavaScript、质量评估及镜像组在首轮未运行，不能视为通过；修复候选须由 `submit` 继续验证。
- 原生表单缺少编辑载荷时曾错误地执行空编辑确认；新增测试先复现 200 响应，再要求明确拒绝，`test_report_workspace_views.py` 8 项通过。该修复已纳入下述完整 `submit`。

以上为开发阶段的检查与失败修复历史；最终门禁以本页下方的固定候选 `submit` 凭据为准。

## 2026-09-29 与报告整理功能的集成

- 从 `origin/main` 的 `272c612` 在独立工作区集成已完成的逐份核对实现；保留报告详情的只读信息与“整理报告”入口，将报告字段编辑和确认统一导向逐份核对页。旧批量服务、模板及其专用测试已移除；旧批量 POST 明确返回 410。
- 基线 `test_report_organization_views.py`、`test_report_relations.py`、`test_report_readmodels.py`、`test_batch_confirmation.py` 在集成前 86 通过。集成后联合运行报告整理、详情、工作区、路由等 46 项，首轮 45 通过、1 项旧错误文案断言失败；更新断言后该项及来源变动、零指标关联相关测试共 4 通过。
- `python manage.py check` 无问题；`python manage.py makemigrations --check --dry-run` 无遗漏；`node --check static/js/lab-report-workspace.js` 通过；`python tools/verify_documentation.py` 验证 151 份登记文档。
- 报告整理与工作区浏览器联合运行 4 项时 3 通过，1 项在另一轮模块测试并行期间发生 SQLite 共享内存数据库刷新错误；随后单独运行该场景 1 通过。完整浏览器门禁仍以最终 `submit` 的隔离运行结果为准。
- 集成候选 `9554286` 的首次完整本地 `submit`：契约及 Django 检查通过；PostgreSQL **387 通过**；Python **5692 通过、5 跳过、3 失败**，浏览器、JavaScript、语料、生产镜像与 smoke 均未运行。三个失败是报告详情提示的旧精确文案断言、原图异步加载前的即时断言，以及浏览器请求期间 SQLite 测试库刷新的错误。调整浏览器等待和断言后，`test_lab_report_consolidation_browser.py` 的冲突场景与 `test_report_identity_review_browser.py` 全文件联合运行 **4 通过**；完整门禁仍须对新提交重跑。

## 最终提交与发布门禁

- 功能候选 `dd9dbae424fd286f6e89e90162c557666d71d97f` 的本地 `submit` **完整门禁通过**：Python **5700 通过、5 跳过**，PostgreSQL **387 通过**，独立浏览器 **25 通过**；契约、Django、JavaScript、合成质量语料、生产镜像构建和 smoke 均通过。凭据为本地 `.git/local-submit/runs/fec56a07ceb54dbb9ae13352b07a9067/receipt.json`，无组复用；本地路径仅用于复核，不进入仓库。
- 功能 [PR #125](https://github.com/skuyd/emr/pull/125) Squash 为 `65c820107ef935aced5ed523bebc19e4d2e3c6f3`。Release Please 的发布候选 `cf288c29a8dbce6fc5ddc2695889478c6fa224ba` 通过发布门禁，**136 项发布测试通过**；契约、合成质量语料、生产镜像构建及 smoke 均通过，业务完整门禁按有效逐组凭据复用。发布 [PR #126](https://github.com/skuyd/emr/pull/126) Squash 为 `9f2581efa99d68635c3afec37cd2d8756eff6e08`，已创建 [v3.0.0 标签与 GitHub Release](https://github.com/skuyd/emr/releases/tag/v3.0.0)。
- AC-01～AC-21 的可重复测试入口见上表；集成失败与修复复测见本页历史记录。`python tools/verify_documentation.py` 和发布门禁以本次文档回填提交时的复验结果为准。生产仍受[发布门禁](release-gate.md)约束，未进行生产部署。

## 2026-09-29 导航方案 1 修正

v3.0.0 发布后，按已确认的方案 1 将旧报告详情 GET 导向统一核对页；资料详情为选定报告提供独立“整理报告”入口。原发布门禁只证明 v3.0.0 候选，本修正需另行验证和发布。

- 旧地址跳转、安全返回位置及资料详情入口三项测试先在 v3.0.0 主线上复现失败，修改后均通过。关联后端回归 **35 通过**。
- 两个相关浏览器文件联合运行 **6 通过、1 失败**；唯一失败是测试在趋势页长文本上误用精确匹配，修正断言后该场景单独复测 **1 通过**。完整业务门禁以本修正的下述 `submit` 回执为准，不复用 v3.0.0 结论代替。
- 固定功能候选 `9a38c5fe4db1bee94fd4132756886a5dd0ed3051` 的本地 `submit` **完整业务门禁通过**：Python **5696 通过、5 跳过**，独立浏览器 **25 通过**，PostgreSQL **387 通过**；契约、Django、JavaScript、合成质量语料、生产镜像构建和 smoke 均通过。回执在本地 `.git/local-submit/runs/4721738bd1774e259d34c1b196652937/receipt.json`，无组复用。本地路径仅供复核，不进入仓库。
- 功能 [PR #128](https://github.com/skuyd/emr/pull/128) Squash 为 `2cc5f8c23cf341bbc40ac6aa1af530049eb44db7`。首次 `submit` 在功能合并后因本工作区缺少锁定的 Release Please 停止；按仓库指南运行 `npm ci` 后恢复同一流程，无需重跑仍有效的功能门禁。发布候选 `29096d72b9ad168d7578f9a5d81300d754865d7a` 的发布门禁通过，**136 项发布测试通过**；有效业务完整门禁凭据逐组复用。发布 [PR #129](https://github.com/skuyd/emr/pull/129) 合并为 `03867847ff715ee0c4c4ca92fd331836f2af8b0e`，已创建 [v3.0.1 标签与 GitHub Release](https://github.com/skuyd/emr/releases/tag/v3.0.1)。发布回执在本地 `.git/local-submit/runs/9196140a4ada4d4ca2c343d3db4698f7/receipt.json`。生产仍受[发布门禁](release-gate.md)约束，未进行生产部署。
- v3.0.1 文档回填在已发布源码基线运行 `python tools/verify_documentation.py`：**153 份登记文档通过**；`python tools/verify_traceability.py`：追踪校验通过（62 项需求中 60 verified、2 external_pending）；`python tools/verify_release_gate.py`：结论 **BLOCKED**；`python tools/release_version.py check`：版本元数据 **3.0.1 一致**。

## 2026-09-29 桌面双栏断点修正候选

- 最大化桌面浏览器现场的 `innerWidth` 为 1055px，`(max-width:58rem)` 匹配而 `(max-width:928px)` 不匹配；原图与编辑区因此进入单列。页面根元素计算字号为 16px，不能用它推断媒体查询中 `rem` 的初始字号。
- 新增 `test_desktop_layout_with_larger_browser_font_keeps_original_beside_editor`，在 1055px 视口及较大浏览器初始字号下检查原图位于编辑区左侧。旧断点下先复现失败：原图与编辑区左边缘同为 20px；只将核对页的 `58rem` 断点改为 `928px` 后，该测试 **1 通过**，整个工作区浏览器测试文件 **2 通过**。原有 1280px 桌面与 360px 手机主流程继续通过。
- 以上是修复候选的聚焦验证；完整本地 `submit` 门禁与发布尚未执行。生产放行结论仍为 `BLOCKED`。
