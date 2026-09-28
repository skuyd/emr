# 检验报告整理与来源关联验证记录

状态：功能分支实施中；本页记录 2026-09-28 的本地合成数据验证。功能 PR、本地 `submit` 完整门禁、Squash 与发布结果待实际执行后补充。生产放行仍由[发布门禁](release-gate.md)决定。

## 验收追踪

| 验收项 | 已执行的验证 | 结果与边界 |
| --- | --- | --- |
| AC-01～02 | `test_document_with_two_report_regions_offers_two_specific_details`、桌面/手机资料详情选择场景 | 当前资料可逐单元选报告；报告详情有次要整理入口；同页两区域不混同。 |
| AC-03～05 | 既有自动关联/冲突测试、`test_organization_shows_all_current_sources_including_zero_results`、`test_different_decision_leaves_candidate_independent_and_resolved` | 既有归并规则继续适用；零结果续页可见；人工判定不同保留。 |
| AC-06～07 | `test_undo_preview_and_saved_page_show_remaining_relations`、`test_reassociate_keeps_originals_results_and_manual_revision` | 单关系撤销、重关联及剩余关系可见；原件、结果、修订和历史保留。 |
| AC-08～09 | `test_report_detail_tracks_grouped_originals_and_results_after_organize_and_undo`、`test_previous_whole_report_confirmation_does_not_cover_changed_source_scope`、`test_report_field_correction_can_precede_source_organization` | 当前报告详情随分组显示原图与指标；字段更正可先保存；旧整份确认在范围变化后不再证明新范围，原逐项修订保留。独立的逐份核对工作区仍按其[规格](../specs/2026-09-28-lab-report-review-workspace.md)实施。 |
| AC-10～11 | 权限/旧地址/伪造 ID/删除与旧版本/重复请求测试及真实 PostgreSQL 并发 2 项 | 旧写入地址关闭；失败不留下半完成关系；当前 URL 单元失效后保留输入并提示刷新。 |
| AC-12～13 | 整理页无内容编辑和确认控件的视图断言、桌面与手机浏览器 3 项 | 整理页只处理来源；原图、候选、保存、撤销及返回可操作。逐份核对页的统一职责和移除批量确认由独立规格交付。 |
| AC-14 | `test_organizing_changes_shared_report_projection_and_rejects_old_confirmation`、相关对比/趋势/输出回归 | 关系变化后共用最新读取分组；旧提交令牌过期；受限输出范围没有扩大。 |

## 本地执行

- 相关 Python 回归：`python -m pytest tests/labs/test_report_readmodels.py tests/labs/test_report_relations.py tests/labs/test_report_organization_views.py tests/labs/test_report_review_views.py tests/labs/test_report_revision_versions.py tests/labs/test_report_comparison.py tests/labs/test_report_trends.py tests/labs/test_trends.py tests/exports/test_lab_report_projection.py tests/security/test_csrf_and_idor.py -q`，**123 passed**。
- 整份确认回归：`python -m pytest tests/labs/test_batch_confirmation.py tests/labs/test_confirmation_quality.py tests/labs/test_confirmation_validation.py -q`，**53 passed**；旧测试的并发插入点随分组读取接口迁移更新，首次运行的 1 项接口引用失败已修正并重跑通过。
- 浏览器：`python -m pytest tests/browser/test_report_identity_review_browser.py -q`，**3 passed**，覆盖桌面与手机宽度。
- PostgreSQL：`python -m pytest --ds=config.settings.postgres_test tests/integration/test_lab_report_postgres.py -q -k concurrent_organization`，**2 passed**；用完已移除临时数据库容器。
- Django 系统检查与文档校验在提交前再次执行；完整业务门禁由本地 `submit` 执行，尚未以局部测试冒充完整通过。

以上使用合成原件和测试账号；未进行真实医疗资料验证或生产部署。
