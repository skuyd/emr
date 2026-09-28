# 检验报告逐份核对确认验证记录

日期：2026-09-28。状态：实施中；完整业务门禁、独立审查与提交发布尚未完成。本记录对应[规格](../specs/2026-09-28-lab-report-review-workspace.md)和[计划](../plans/2026-09-28-lab-report-review-workspace.md)。测试使用合成患者、原件和隔离数据库；不代表生产部署或真实医疗资料质量验收。

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
| AC-17 | `test_lab_report_workspace_browser.py` 及报告修订、目录阶段浏览器回归，覆盖桌面 1280px 与手机 360px |
| AC-18 | `test_confirmation_quality.py`、`test_phase_two_comparison.py`、导出/分享回归与非检验测试 |
| AC-19 | `test_review_retirement.py` 的任务 GET/POST/source/image；`test_phase_two_dictionary_workflow.py` 的成员权限；PostgreSQL 候选来源撤权竞争 |
| AC-20 | `test_review_retirement.py::test_completed_historical_review_result_remains_visible_after_retirement` 及修订历史回归 |
| AC-21 | `test_report_workspace_submit.py` 的跨报告拒绝；旧批量路由测试；浏览器连续切换与未保存提示 |

## 已执行检查

- 首轮 `python -m pytest tests/labs -q --tb=no`：814 通过、8 失败，保留为修复前事实，不作为通过证据。第二轮 826 通过、3 失败，定位为历史候选趋势、筛选返回锚点和只读控件断言，修正后对应 4 项聚焦测试通过。第三轮 `python -m pytest tests/labs -q --tb=short`：829 通过；随后新增来源建立顺序回归，先复现失败，再与桌面/手机浏览器主流程合跑 5 通过。
- `python -m pytest tests/browser/test_lab_report_workspace_browser.py tests/browser/test_lab_catalog_browser.py tests/browser/test_lab_comparison_browser.py tests/browser/test_lab_report_consolidation_browser.py tests/browser/test_lab_selection_batch_browser.py tests/browser/test_record_review_browser.py tests/browser/test_report_identity_review_browser.py -q --tb=short`：20 通过。随后主流程补测未保存切换提示与保留输入，同上述 5 项合跑通过。
- 隔离 PostgreSQL 18.6：`python -m pytest --ds=config.settings.postgres_test tests/integration/test_phase_two_postgres_concurrency.py tests/integration/test_family_sharing_postgres.py -x -q --tb=short`，23 通过；另将候选权限测试与 `test_lab_report_workspace_postgres.py` 合跑，5 通过，覆盖真实行锁等待、重复请求和旧预览拒绝。
- 报告导出、分享投影和资料趋势索引：`python -m pytest tests/exports/test_lab_report_projection.py tests/patients/test_share_projection.py tests/documents/test_trend_index.py -q --tb=short`，31 通过。人工补录排除后重解析的状态继承，以及同次更正并解决人工来源冲突，均先由新增测试复现失败；修复后 `test_report_workspace_manual_versions.py` 9 通过，另覆盖仍漏识别、唯一识别及新版本恢复。
- `python manage.py check`：无系统问题；`python manage.py makemigrations --check --dry-run`：无遗漏；`node --check static/js/lab-report-workspace.js`：通过；`npm run test:js`：9 通过。
- `python tools/verify_documentation.py`：147 份登记文档通过；`python tools/verify_traceability.py`、`python tools/verify_release_gate.py`、`python tools/release_version.py check` 均通过。发布门禁结论仍为 `BLOCKED`，不代表生产放行。

完整 Python、必跑浏览器、JavaScript、质量评估、Docker smoke、文档治理和发布门禁以最终本地 `submit` 的实际验证结果为准。未执行或未通过时，本记录及登记表保持实施中。
