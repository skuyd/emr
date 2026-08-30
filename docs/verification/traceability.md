# PRD v1.0 需求追踪矩阵

源文件 SHA-256：`0e4a1a58cd1aa428bb53d0d9387a8fc6c6d9aea3d7169781ebfedeb7c11e699f`

共 62 项：自动验证 60 项，外部待验证 2 项。
`verified` 表示存在可执行自动化证据或经哈希证明的外部浏览器证据；`external_pending` 不计为发布通过。

## MUST 功能

| 编号 | PRD 章节 | 状态 | 要求 | 证据 |
| --- | --- | --- | --- | --- |
| MUST-01 | 3.1 | verified | 单账号、单患者建档 | `tests/acceptance/test_ac00_ac01.py::test_ac00_ac01_http_otp_onboarding_session_return_and_one_patient` |
| MUST-02 | 3.1 | verified | 手机号验证码登录 | `tests/accounts/test_otp.py::test_fifth_wrong_attempt_locks_challenge`<br>`tests/acceptance/test_ac00_ac01.py::test_ac00_ac01_http_otp_onboarding_session_return_and_one_patient` |
| MUST-03 | 3.1 | verified | 图片和 PDF 批量上传 | `tests/acceptance/test_ac02_ac07.py::test_ac02_accepts_exactly_twenty_files_and_sixty_pages`<br>`tests/documents/test_inspection.py::test_real_pdf_is_structurally_parsed_and_every_page_render_validated` |
| MUST-04 | 3.1 | verified | 原件保存、处理进度、失败重试 | `tests/acceptance/test_ac02_ac07.py::test_ac03_durable_save_survives_leaving_page_and_a_new_session_can_track_and_open_original`<br>`tests/documents/test_upload_views.py::test_failed_item_can_retry_and_reopens_completed_batch` |
| MUST-05 | 3.1 | verified | 文档类型、日期、机构候选识别 | `tests/processing/test_metadata_extraction.py::test_lab_summary_prefers_sampling_date_and_preserves_all_candidates_with_evidence`<br>`tests/processing/test_metadata_extraction.py::test_same_priority_conflicting_dates_remain_unknown_without_user_input` |
| MUST-06 | 3.1 | verified | 至少 125 项检验指标字典和宽覆盖抽取 | `tests/labs/test_dictionary.py::test_v1_dictionary_is_versioned_hashed_and_has_exactly_125_evidenced_indicators`<br>`tests/labs/test_extraction.py::test_unmapped_but_well_structured_result_gets_stable_candidate_code_and_search_only_capability` |
| MUST-07 | 3.1 | verified | OCR 全文搜索 | `tests/documents/test_records.py::test_search_covers_ocr_indicator_fields_and_institution_with_exact_source_snippet` |
| MUST-08 | 3.1 | verified | 按日期组织的文档级病案列表 | `tests/documents/test_records.py::test_records_sort_recognized_dates_descending_then_unknown_by_upload_time` |
| MUST-09 | 3.1 | verified | 文档详情、原件查看和字段证据定位 | `tests/documents/test_detail_viewer.py::test_detail_preserves_raw_results_orders_by_report_and_keeps_source_links`<br>`tests/documents/test_detail_viewer.py::test_viewer_uses_scoped_evidence_to_select_page_and_highlight` |
| MUST-10 | 3.1 | verified | 完全相同文件去重 | `tests/acceptance/test_ac02_ac07.py::test_ac06_ac07_exact_duplicate_opens_existing_document_with_date_unrecognized_placeholder` |
| MUST-11 | 3.1 | verified | 单份资料删除和账号全量删除 | `tests/documents/test_deletion.py::test_document_delete_requires_confirmation_then_immediately_hides_every_entrypoint`<br>`tests/accounts/test_account_deletion.py::test_account_delete_confirmation_immediately_disables_access_and_queues_every_document` |
| MUST-12 | 3.1 | verified | 账号数据隔离和必要同意 | `tests/security/test_csrf_and_idor.py::test_every_dynamic_patient_route_rejects_foreign_resources`<br>`tests/patients/test_onboarding_views.py::test_onboarding_requires_authentication_and_csrf` |
| MUST-13 | 3.1 | verified | 核心产品埋点和运行监控 | `tests/analytics/test_event_schema.py::test_every_prd_event_has_an_exact_valid_closed_schema`<br>`tests/operations/test_health.py::test_health_is_generic_and_metrics_require_bearer_token` |

## 验收标准

| 编号 | PRD 章节 | 状态 | 要求 | 证据 |
| --- | --- | --- | --- | --- |
| AC-00 | 12.1 | verified | Web 登录 | `tests/acceptance/test_ac00_ac01.py::test_ac00_ac01_http_otp_onboarding_session_return_and_one_patient`<br>`tests/browser/test_ac00_ac01_browser.py::TestAc00Ac01Browser::test_login_onboarding_and_safe_return_load_without_asset_or_console_errors` |
| AC-01 | 12.1 | verified | 首次建档 | `tests/patients/test_onboarding_views.py::test_onboarding_page_has_only_required_fields_and_reachable_policy_links` |
| AC-02 | 12.1 | verified | 批量上传 | `tests/acceptance/test_ac02_ac07.py::test_ac02_accepts_exactly_twenty_files_and_sixty_pages`<br>`tests/browser/test_ac02_upload_browser.py::TestAc02UploadBrowser::test_authenticated_user_uploads_a_real_image_without_console_asset_or_overflow_errors` |
| AC-03 | 12.1 | verified | 离开上传页 | `tests/acceptance/test_ac02_ac07.py::test_ac03_durable_save_survives_leaving_page_and_a_new_session_can_track_and_open_original` |
| AC-04 | 12.1 | verified | 上传失败 | `tests/acceptance/test_ac02_ac07.py::test_ac04_failed_upload_creates_no_document_and_same_item_can_retry` |
| AC-05 | 12.1 | verified | 解析失败 | `tests/acceptance/test_ac02_ac07.py::test_ac05_parsing_downgrade_keeps_original_readable` |
| AC-06 | 12.1 | verified | 完全重复 | `tests/acceptance/test_ac02_ac07.py::test_ac06_ac07_exact_duplicate_opens_existing_document_with_date_unrecognized_placeholder` |
| AC-07 | 12.1 | verified | 日期未识别 | `tests/acceptance/test_ac02_ac07.py::test_ac06_ac07_exact_duplicate_opens_existing_document_with_date_unrecognized_placeholder` |
| AC-08 | 12.1 | verified | OCR 搜索 | `tests/documents/test_records.py::test_search_covers_ocr_indicator_fields_and_institution_with_exact_source_snippet` |
| AC-09 | 12.1 | verified | 指标搜索 | `tests/documents/test_records.py::test_search_covers_ocr_indicator_fields_and_institution_with_exact_source_snippet` |
| AC-10 | 12.1 | verified | 字段回溯 | `tests/documents/test_detail_viewer.py::test_viewer_uses_scoped_evidence_to_select_page_and_highlight` |
| AC-11 | 12.1 | verified | 原始值 | `tests/documents/test_detail_viewer.py::test_detail_preserves_raw_results_orders_by_report_and_keeps_source_links`<br>`tests/labs/test_models.py::test_observation_preserves_raw_value_unit_status_and_dictionary_lineage` |
| AC-12 | 12.1 | verified | 结果状态 | `tests/labs/test_extraction.py::test_comparator_status_and_semi_quantitative_results_are_never_coerced_to_plain_numbers` |
| AC-13 | 12.1 | verified | 实验趋势 | `tests/labs/test_trends.py::test_eligible_trend_preserves_raw_values_and_each_point_links_to_evidence`<br>`tests/labs/test_trends.py::test_ineligible_combinations_have_no_entry_and_return_not_found` |
| AC-14 | 12.1 | verified | 一键反馈 | `tests/documents/test_detail_viewer.py::test_inaccuracy_feedback_is_one_click_idempotent_and_contains_no_medical_text` |
| AC-15 | 12.1 | verified | 单份删除 | `tests/documents/test_deletion.py::test_document_delete_requires_confirmation_then_immediately_hides_every_entrypoint`<br>`tests/documents/test_deletion.py::test_purge_removes_original_document_parse_feedback_and_empty_batch` |
| AC-16 | 12.1 | verified | 账号删除 | `tests/accounts/test_account_deletion.py::test_account_delete_confirmation_immediately_disables_access_and_queues_every_document`<br>`tests/accounts/test_account_deletion.py::test_account_purge_waits_for_originals_then_removes_credentials_consents_preferences_and_sessions` |
| AC-17 | 12.1 | verified | 任务提醒 | `tests/notifications/test_services.py::test_finished_batch_creates_one_exact_generic_notification`<br>`tests/notifications/test_webpush.py::test_push_delivery_contains_only_generic_allowlisted_payload` |
| AC-18 | 12.1 | verified | 数据隔离 | `tests/security/test_csrf_and_idor.py::test_every_dynamic_patient_route_rejects_foreign_resources`<br>`tests/security/test_upload_isolation.py::test_upload_status_summary_and_original_are_all_patient_scoped` |
| AC-19 | 12.1 | verified | 埋点最小化 | `tests/privacy/test_runtime_artifacts.py::test_synthetic_canaries_never_escape_to_runtime_artifacts`<br>`tests/privacy/test_event_payloads.py::test_sensitive_event_attributes_are_rejected_before_persistence` |
| AC-20 | 12.1 | verified | 未来解析 | `tests/processing/test_pipeline.py::test_reprocessing_keeps_old_version_traceable_and_atomically_switches_active_result`<br>`tests/documents/test_detail_viewer.py::test_failed_document_can_queue_exactly_one_patient_scoped_reprocessing_run` |
| AC-21 | 12.1 | verified | 桌面布局 | `tests/browser/test_ac02_upload_browser.py::TestAc02UploadBrowser::test_authenticated_user_uploads_a_real_image_without_console_asset_or_overflow_errors`<br>`tests/accessibility/test_shell_markup.py::test_app_shell_styles_keep_fixed_navigation_focus_and_responsive_overflow_contract` |
| AC-22 | 12.1 | external_pending | 浏览器兼容 | `tests/browser/test_ac00_ac01_browser.py::TestAc00Ac01Browser::test_login_onboarding_and_safe_return_load_without_asset_or_console_errors`<br>`tests/browser/test_ac02_upload_browser.py::TestAc02UploadBrowser::test_authenticated_user_uploads_a_real_image_without_console_asset_or_overflow_errors`<br>`docs/verification/external-compatibility.md` |

## 测试场景

| 编号 | PRD 章节 | 状态 | 要求 | 证据 |
| --- | --- | --- | --- | --- |
| SCN-01 | 13 | verified | 新用户正常建档和首次上传 | `tests/browser/test_ac00_ac01_browser.py::TestAc00Ac01Browser::test_login_onboarding_and_safe_return_load_without_asset_or_console_errors`<br>`tests/browser/test_ac02_upload_browser.py::TestAc02UploadBrowser::test_authenticated_user_uploads_a_real_image_without_console_asset_or_overflow_errors` |
| SCN-02 | 13 | verified | 一批 20 张清晰检验报告图片 | `tests/acceptance/test_ac02_ac07.py::test_ac02_accepts_exactly_twenty_files_and_sixty_pages` |
| SCN-03 | 13 | verified | 包含多页检验报告的 PDF | `tests/processing/test_pdf_preparation.py::test_mixed_multi_page_pdf_uses_text_per_page_and_preserves_page_numbers`<br>`tests/documents/test_inspection.py::test_real_pdf_is_structurally_parsed_and_every_page_render_validated` |
| SCN-04 | 13 | verified | 低清、旋转、截图和裁切不全的图片 | `tests/processing/test_image_preparation.py::test_exif_rotation_is_applied_only_to_derived_page_and_recorded`<br>`tests/processing/test_image_preparation.py::test_large_image_is_downscaled_for_ocr_but_retains_oriented_source_dimensions` |
| SCN-05 | 13 | verified | 双栏、多表格和跨页报告 | `tests/processing/test_value_objects.py::test_page_orders_regions_deterministically_and_builds_full_text`<br>`tests/tools/test_candidate_normalization.py::test_table_row_heuristic_requires_name_and_result_and_keeps_only_hashed_context`<br>`tests/processing/test_pdf_preparation.py::test_mixed_multi_page_pdf_uses_text_per_page_and_preserves_page_numbers` |
| SCN-06 | 13 | verified | 影像、病理、出院小结等仅 OCR 文档 | `tests/processing/test_metadata_extraction.py::test_nonempty_unclassified_ocr_is_other_while_empty_ocr_is_unknown`<br>`tests/documents/test_records.py::test_search_covers_ocr_indicator_fields_and_institution_with_exact_source_snippet` |
| SCN-07 | 13 | verified | 完全相同文件重复上传 | `tests/acceptance/test_ac02_ac07.py::test_ac06_ac07_exact_duplicate_opens_existing_document_with_date_unrecognized_placeholder` |
| SCN-08 | 13 | verified | 同一报告的截图和 PDF 只提示可能重复 | `tests/documents/test_similarity.py::test_possible_duplicate_distance_eight_hints_nine_does_not_and_query_never_mutates` |
| SCN-09 | 13 | verified | 日期未识别或多个日期冲突 | `tests/processing/test_metadata_extraction.py::test_same_priority_conflicting_dates_remain_unknown_without_user_input`<br>`tests/acceptance/test_ac02_ac07.py::test_ac06_ac07_exact_duplicate_opens_existing_document_with_date_unrecognized_placeholder` |
| SCN-10 | 13 | verified | 指标名称识别但值与单位关联不明确 | `tests/labs/test_extraction.py::test_numeric_row_without_an_explicit_unit_is_rejected_unless_dictionary_defines_unitless_result` |
| SCN-11 | 13 | verified | 未检出、阴性、阳性和半定量结果 | `tests/labs/test_extraction.py::test_comparator_status_and_semi_quantitative_results_are_never_coerced_to_plain_numbers` |
| SCN-12 | 13 | verified | OCR 服务失败 | `tests/processing/test_runner.py::test_retryable_failures_use_exact_delays_then_terminalize`<br>`tests/processing/test_tasks.py::test_process_document_leaves_queued_work_untouched_when_pipeline_is_not_configured` |
| SCN-13 | 13 | verified | 结构化解析服务超时 | `tests/processing/test_runner.py::test_retryable_failures_use_exact_delays_then_terminalize`<br>`tests/processing/test_runner.py::test_due_retry_is_recovered_without_waiting_for_stale_cutoff` |
| SCN-14 | 13 | verified | 上传过程中网络中断 | `tests/documents/test_upload_views.py::test_storage_failure_is_503_safe_and_retryable_without_document`<br>`tests/documents/test_upload_service.py::test_retryable_failed_item_can_succeed_and_clears_safe_error_code` |
| SCN-15 | 13 | verified | 处理过程中用户退出并再次进入 | `tests/acceptance/test_ac02_ac07.py::test_ac03_durable_save_survives_leaving_page_and_a_new_session_can_track_and_open_original` |
| SCN-16 | 13 | verified | 搜索 OCR 关键词、指标名和值 | `tests/documents/test_records.py::test_search_covers_ocr_indicator_fields_and_institution_with_exact_source_snippet` |
| SCN-17 | 13 | verified | 点击字段定位原件 | `tests/documents/test_detail_viewer.py::test_viewer_uses_scoped_evidence_to_select_page_and_highlight` |
| SCN-18 | 13 | verified | 删除单份资料 | `tests/documents/test_deletion.py::test_document_delete_requires_confirmation_then_immediately_hides_every_entrypoint` |
| SCN-19 | 13 | verified | 删除账号 | `tests/accounts/test_account_deletion.py::test_account_delete_confirmation_immediately_disables_access_and_queues_every_document` |
| SCN-20 | 13 | verified | 两个测试账号互相尝试访问资料地址 | `tests/security/test_csrf_and_idor.py::test_every_dynamic_patient_route_rejects_foreign_resources`<br>`tests/security/test_upload_isolation.py::test_upload_status_summary_and_original_are_all_patient_scoped` |
| SCN-21 | 13 | verified | 通知内容和埋点内容敏感信息检查 | `tests/privacy/test_runtime_artifacts.py::test_synthetic_canaries_never_escape_to_runtime_artifacts`<br>`tests/notifications/test_webpush.py::test_push_delivery_contains_only_generic_allowlisted_payload` |
| SCN-22 | 13 | verified | 新解析版本重跑同一原件 | `tests/processing/test_pipeline.py::test_reprocessing_keeps_old_version_traceable_and_atomically_switches_active_result` |
| SCN-23 | 13 | verified | 手机号验证码登录、错误次数限制和会话过期 | `tests/accounts/test_otp.py::test_fifth_wrong_attempt_locks_challenge`<br>`tests/accounts/test_session.py::test_idle_session_expires_inclusively_and_encodes_return_path` |
| SCN-24 | 13 | verified | 拖拽上传、文件选择上传及混合格式批次 | `tests/browser/test_ac02_upload_browser.py::TestAc02UploadBrowser::test_authenticated_user_uploads_a_real_image_without_console_asset_or_overflow_errors`<br>`tests/documents/test_upload_views.py::test_mixed_metadata_failures_are_per_item_and_response_never_echoes_names` |
| SCN-25 | 13 | verified | 关闭标签页后重新登录并验证后台任务状态连续 | `tests/acceptance/test_ac02_ac07.py::test_ac03_durable_save_survives_leaving_page_and_a_new_session_can_track_and_open_original` |
| SCN-26 | 13 | external_pending | Chrome、Edge、Safari 及两种桌面视口验证 | `tests/browser/test_ac00_ac01_browser.py::TestAc00Ac01Browser::test_login_onboarding_and_safe_return_load_without_asset_or_console_errors`<br>`tests/browser/test_ac02_upload_browser.py::TestAc02UploadBrowser::test_authenticated_user_uploads_a_real_image_without_console_asset_or_overflow_errors`<br>`docs/verification/external-compatibility.md` |
