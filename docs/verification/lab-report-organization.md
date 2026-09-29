# 检验报告整理与来源关联验证记录

状态：`active`；来源整理已由 [PR #121](https://github.com/skuyd/emr/pull/121) 合并，随 [v2.3.0](../releases/v2.3.0.md) 发布。本页记录合成数据验证、首次失败及修复结果。生产放行仍由[发布门禁](release-gate.md)决定。

## v2.3.0 验收追踪

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
- 首轮本地完整 `submit`（候选 `587013b`）：Python **5718 passed、4 failed、5 skipped**；PostgreSQL **408 passed、1 failed**。其中本功能浏览器场景在返回详情后于原图加载完成前断言，现已改为等待图像加载，单项复测 **1 passed**；PostgreSQL 并发测试仍引用迁移前分组函数，已改用当前接口，真实 PostgreSQL 单项复测 **1 passed**。其余三项 Python 失败已由独立修复 [PR #119](https://github.com/skuyd/emr/pull/119) 完整验证并合入主线；本分支已变基至该主线。首轮失败不计为通过；浏览器独立组及后续组当时未运行。
- 变基后聚焦回归：`python -m pytest tests/labs/test_report_organization_views.py tests/labs/test_same_name_display.py tests/labs/test_catalog_projection.py tests/labs/test_batch_confirmation.py -q`，**111 passed**，覆盖本功能与基线修复的组合行为。
- 独立基线修复 [PR #119](https://github.com/skuyd/emr/pull/119) Squash 为 `c57046d679cc4c6b238502daa424e9d7467be9bb`，随 [v2.2.6](../releases/v2.2.6.md) 发布；其完整门禁 Python **5690 passed、5 skipped**，PostgreSQL **407 passed**，浏览器 **25 passed**，发布测试 **136 passed**。
- 功能候选 `bbc646b34b62624e1998a4cafef594b1049bf8f8` 的本地 `submit` 完整门禁通过：Python **5722 passed、5 skipped**，PostgreSQL **409 passed**，独立浏览器 **25 passed**；契约、Django、JavaScript、合成质量语料、生产镜像构建与 smoke 均通过。回执及逐组日志保存在本仓库本地 `.git/local-submit/runs/b050b777893e4e5b9a7d41c6f2323b48/`，不进入远程仓库。
- 功能 PR #121 Squash 为 `b62ef99339a920c9894c4d71e265869b28b096db`。发布候选 `f38972d58dfb3f5aa58900753fed9326012b60a7` 的发布门禁通过，**136 passed**；有效业务完整门禁凭据复用。发布 PR #122 合并为 `212d47d15bb1c758b716660f1bf3c912292aefc1`，已创建 `v2.3.0` 标签和 GitHub Release。
- Django 系统检查、迁移检查与 `python tools/verify_documentation.py` 均通过；初轮失败保留为历史记录，不计入最终通过集合。

以上使用合成原件和测试账号；未进行真实医疗资料验证或生产部署。

## 2026-09-29 导航方案 1 修正

在 v3.0.0 主线基础上，资料详情改为每份报告分别提供“整理报告”入口；旧报告详情 GET 导向同一份报告的统一核对页。原 v2.3.0 的验收表和门禁数字保留为当时的发布事实，不用于证明此后导航已通过完整门禁。

- 先修改旧地址跳转、安全返回位置与资料详情入口测试，三项均因旧行为失败；实现后这三项通过。联合运行 `tests/labs/test_report_workspace_routes.py`、`test_report_organization_views.py`、`test_report_review_views.py`，**35 通过**。
- 桌面和手机浏览器相关文件联合运行时 **6 通过、1 失败**；失败源于趋势页长文本误用精确匹配断言，改回包含匹配后该场景单独复测 **1 通过**。完整门禁以本修正的 `submit` 回执为准。
