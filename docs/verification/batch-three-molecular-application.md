# 第三批分子检测完整应用验证

分子应用已完成 M1–M6 的本地实现、实际癌症主线合流及受影响验证；精确 PR CI、合并和
本功能发布仍待完成，B3-03 与五批整体仍为 `implementing`。真实 M7 未启动，
不属于本轮继续执行项；生产放行和真实质量结论保持原有边界。
本记录不把已发布 A0 的 28 键纯值合同当作完整应用，也不把合成通过当作真实质量。

初始分支来自病理 PR 70 实际 Squash `4b73d2e9c925ef48c95c217c7c055c5b7157e13b`。
最终应用源码为 `32c6b2b620bf8225a208882c901234f8687adc68`（tree `d2eefd7fc3f616b59a28701d85e12884b026efa5`），
父提交为已独审的 `b60507a` 和实际癌症主线 `9cc0875052a25facd6e6b8ed0f04b66056d6b592`；
本文另保留 `8a54e18` 历史组合检查点，不把其执行身份改成最终头。
实现合同及步骤分别见[既有规格](../specs/2026-09-10-molecular-application.md)、
[实施计划](../plans/2026-09-10-molecular-application.md)，原范围仍见
[B3-03](../specs/2026-09-07-batches-one-five-requirements.md)和
[病理与分子原计划 Task 5](../plans/2026-09-08-pathology-molecular-evidence.md)。

## 已实现的应用边界

模式和适配器将 28 个 A0 字段实际接入报告路由、数据库、服务和 UI；三共享日期保留旧病理
值形状及原精度。额外检测范围陈述属于应用字段，不改 A0。变异/药物锚采用实际条件唯一约束；
同报告、标本、检测、变异的完整有序目标、来源角色、成员和作者修订参与当前资格。

实际合成 OCR provider 输入真实分段、worker 和 ORM，覆盖表格与明确叙述、拆列/续页、
多报告、多标本、CNV、融合、MSI/TMB 与报告药物依据；这不等于运行真实 OCR 模型质量评估。
字段原文依实际 OCR 半开区间或人工逐页首组片段核验，不能裁掉否定、借复制锚、补单位/日期，
或把未知识别状态变成报告“不确定/未提供”。未读分子页仍是未判断，不是阴性。

核对界面复用报告/字段路由，显示完整身份、原值和未知状态；原图高亮使用真实值片段，
无联合框不捏造框。确认、更正和整组替换保持旧原值；控件首尾空白、列表项内换行及仅 CRLF
传输差异不会造成假修改。最后来源复读之后仍检查权限和原件存活，包括无效 POST。

独立 `MOLECULAR_SEMANTIC_UNIT_V1` 让细选数值带齐该变异身份或必要报告药物依据，
不携带未选原件、整张药物表、private closure、manual_source 或完整私有原句。
公开别名只在本次选择内使用；原件、报告和字段范围各自授权。真实 PDF/JSON/CSV/ZIP、
分享交换和离线严格 reader 消费同一投影。报告药物依据不成为系统治疗建议。

## 最终实际主线整合与修复

实际主线 portable 1.7 的癌症、1.6 的病灶/云来源和分子内容共同使用 portable **1.8**。
严格 reader 保留真实旧 1.6 非空病灶/云数组及 1.7 非空癌症数组；1.0–1.7 声明携带
非空分子单元一律拒绝。未发布的分子草稿 1.6 不是旧版兼容义务。

导出和分享选择页在渲染前后比较完整来源状态；渲染外的短事务按患者锁串行化末次检查，
保留各入口原 EXPORT/MANAGE 权限。跨域后一次读取不能使先前已渲染的其他域值失效后仍返回。
实际 PostgreSQL 既验证先提交时拒绝旧正文，也验证持锁检查时另一连接等待后正常提交；
四域任一来源提交改变后，持续下载和分享正文/私有依赖绑定均失效清理。

共享提取器记录实际临床、病理、分子三个组件的确切身份。癌症类型化输入继续接受已发布的
旧两组件身份，拒绝未知、缺项、额外后缀及 FAILED/PARTIAL 记录，不修改旧提取或确认。
完整新身份长 58 字符，`0007_expand_clinical_extractor_identity` 仅把
`ClinicalExtraction.extractor_version` 扩为 128；不改变另一张 FactExtraction 表的字段。
真实 PG 迁移验证旧短行 128→40→128 保真；已有长值时反向收窄须报 DataError，原值、
128 列及已应用迁移状态保持，不能承诺无条件回滚或静默截断。

| 源码阶段/执行 | 实际结果 | 范围与限制 |
| --- | --- | --- |
| `ea843bd0` 暂存树组合 | 22通过、0跳过 | 四域输出/reader、GET及无效POST、b605两项和实际迁移 |
| `ea843bd0` 既有受影响回归 | 256通过、0跳过 | 共享schema、原值控件、成功导出/分享POST、权限、旧域输出及worker |
| `ea843bd0` 新PG边界 | 8通过、0跳过 | 四域提交后持续失效、两路短锁的提交/等待次序 |
| `ea843bd0` 原生TLS | SQLite5、PG5均通过且0跳过 | M4/M5四场景及四域360px分享/来源失效；CI必跑与普通排除名单一致 |
| `ea843bd0` 既有PG | 15通过、8失败 | 15项分子/侧别输出通过；8项病理类型化候选因新提取身份不被接受而缺失，未进入权限断言 |
| `857352de` 身份修订 | SQLite14通过；PG8失败 | 实际提取/候选修复成立，但PG拒绝超过varchar(40)的新身份，不能用SQLite通过代替存储验证 |
| 最终 `32c6b2b` 同树PG | 23通过、0跳过，381.458秒 | 原8权限反例、新身份/未知控制、实际混合worker与列宽/无损迁移 |
| 最终 `32c6b2b` 同树普通 | 45通过、0跳过，615.661秒 | 类型化来源、分子pipeline/retry、b605读回、迁移和四域选定内容 |

所有命令实际从 HEAD b605 加 MERGE_HEAD 9cc 及各次暂存树启动，并非从稍后提交的 clean
32c6 首跑。最终两组1539路径NUL清单和原字节SHA前后一致且等于最终源码；ea843的1536
路径与857352的1537路径按各自树保留。其后只有六路径提取身份/存储增量，健康输出和TLS
按不变实现继承，不能改称全部在最终头重跑。各组覆盖重叠，不相加为不重复总数。

作者阶段回执SHA：`03382a347695e0ba4590bbc8179a6957f2087a765adeb59eac91c58e96d535c3`。
输出/短锁独审SHA：`ba88936934a0668af59d5c54504334021a0aa77e7fe5662e93172cfe66904e7a`，
独立8项既有输出、4项PG及另2项真实已提交权限对照通过；原2个探针事务设置失败保留。
六路径身份/存储独审SHA：`6b1793061d55722cb0d7858b058ad68dc103c59b27e6e17afa6c7848dbcd9d61`，
独立PG15通过、1539源码未变；其有界范围与最终提交对齐单独记录，不能自批原M5或代替PR CI。
最终32c6源码独审对齐SHA：`449d4e8a355916b1d83e6bf2bdd6034b59ebd35ca437926b5c760315d6dbbd93`，
本次范围无未关闭P1/P2。root的12阶段原件读回SHA：
`08f6b5d0da1a3af8d25ab25f7c75ef4b0814d27a4313c98a38f3c0025afbc478`；
独审闭合读回SHA：`d15fc548590d705a62a2b54ad94312597117534b7a0214a59456288cecca4791`。
这些闭合源码及其执行身份；正式文档f851已另经非作者审查通过，当前发布主线同步增量独审和PR精确CI仍是后续门禁。

## 历史8a54组合检查点的实际执行

| 执行 | 实际结果 | 证明范围 |
| --- | --- | --- |
| ordinary | 1459通过、0跳过、2按标记未选择，1785.727秒 | facts、exports、patients、cloud_imaging真实组合回归 |
| PG边界 | 72通过、0跳过，497.378秒 | 分子事实/输出与既有病理/云输出的正常COMMIT、锁等待、collector和持续失效 |
| SQLite原生TLS | 4通过、0跳过，181.735秒 | 原图核对/高亮、手机及桌面操作、预览、真实接收者交换与来源失效 |
| PG原生TLS | 4通过、0跳过，267.312秒 | 同样真实浏览器链路且数据库用独立PG；原生secure cookie/同源CSRF |
| 组合增量非作者复核 | 34+5通过、0跳过 | 完整/截片否定、实际HTTP双域失效、混合输出、最后访问及旧reader；原M5另继承root独审 |

各次覆盖重叠，不能相加为不重复验收数。四组均保留实际 XML、日志、命令、退出码、截图及
1329条完整源码原字节清单；前后与最终8a54一致。ordinary/PG边界/SQLiteTLS的实际命令启动
HEAD是3bcc及两个未提交浏览器改动，随后无字节变化提交为8a54；PGTLS从clean8a54启动。
这证明执行字节等于最终头，不冒称四次命令都以clean8a54启动。

作者总回执SHA：`d6fe62190cd93bfc62e80ced61ca44cdda6dcd1e413121a6ce89015827249ea7`。
组合增量独审SHA：`4617dd9adcfc71afeb77e76a0ea80ea2aee2aee0881bffe1eba40006194013ac`。
root完整证据读回SHA：`86b2511e021eef4e91dace57fc91824ae025be91fa2723f33595d16b78885cbb`。
独审覆盖的准确头和继承关系见[匿名制品](artifacts/batch-three-molecular-application.json)。

迁移不是静态检查：MigrationExecutor建立旧1.0/1.1、已确认IHC/日期和完成提取，迁移前后
全表内容、确认资格和来源令牌保持相等。重试/租约5个案例继承429阶段的6项worker/迁移
运行；在429→8a54范围内，仅 `apps/processing/pipeline.py`、`tests/processing/test_molecular_retry.py` 及
`apps/facts/clinical_extraction.py` 入口的 Git 源码保持不变。传递依赖
`apps/facts/molecular_extraction.py` 已增加 CODED 原文校验，不能称整条 worker 依赖未变；
上述旧案例未称作8a54新执行。截图是合成原件界面辅助证据，实际断言同时
要求原图naturalWidth、高亮宽度、控件内容、真实状态/HTTP正文及来源失效。

## 原需求到实际断言

下表列出定位入口，匿名制品保留全部选定案例、行号及实际运行匹配；本地映射另保留原断言
全文和完整截图哈希。纯合同、实际服务、浏览器、PG与真实M7范围分别标注。

B3-03-02 的合同/输出证据分层记录：25 个非日期 A0 接入参数、12 个三日期适配参数，
PANEL_SIZE 三种单位状态的实际补录/核对/输出，以及报告日期输出精度均已有通过记录。
既有表格抽取 fixture 含 panel/日期，但断言主要核验变异、MSI/TMB 和原片段，不能据此
称样本类型、panel 原值和三日期角色已全部完成逐项抽取→ORM读回。对应抽取分派已实现；
这两项专用证明已在 b605 的两条实际用例及独立两条复核闭合，并在本次22与最终45组再次执行：
元数据从真实抽取到ORM、核对和选定JSON读回；MOL字段经真实HTTP暂缓、撤销、确认及
旧快照失效/恢复。初始参数反序导致的1个测试设置失败原样保留，不称应用缺陷。
作者两条通过回执SHA `9ed62c772e37aa9da73dc365fc0a30c8678fb1689799dd8ea34e8ed73e5dc823`；
独立两条通过回执SHA `e6c1019df45a2f60b12be14254badf7e9b4275c7f9684381dae8a3ca25d958c1`。

| 条目 | 原要求/实现边界 | 实际断言入口 |
| --- | --- | --- |
| B3-03-01 | 既有病理/IHC 与 A0 不是本次完整分子应用 | `tests/facts/test_molecular_schema.py::test_application_registers_every_non_date_value_without_changing_original`、`tests/facts/test_molecular_schema.py::test_a0_date_adapter_roundtrips_raw_without_changing_published_date_shape` |
| B3-03-02 | 标本、样本类型、检测、panel 名称/规模与三日期 | `tests/facts/test_molecular_delivery_boundaries.py::test_extracted_metadata_keeps_own_specimen_assay_dates_and_selected_values`；旧日期值合同另见 `tests/facts/test_molecular_schema.py::test_a0_date_adapter_roundtrips_raw_without_changing_published_date_shape` |
| B3-03-03 | 完整变异、同基因异位点、版本化转录本、密码子/位置 | `tests/facts/test_molecular_boundaries.py::test_same_gene_different_transcript_site_never_collapses_identity`、`tests/facts/test_molecular_context.py::test_two_printed_transcript_components_of_same_identity_are_not_a_conflict` |
| B3-03-04 | CNV、融合及有序伙伴组件 | `tests/facts/test_molecular_boundaries.py::test_cnv_and_fusion_keep_own_identity_and_as_printed_partner_order`、`tests/facts/test_molecular_boundaries.py::test_unprinted_fusion_direction_is_not_inferred_from_separator` |
| B3-03-05 | 数量、比较符、范围、约数与未知单位 | `tests/facts/test_molecular_boundaries.py::test_quantities_preserve_long_decimal_qualifiers_without_positive_inference`、`tests/exports/test_molecular_exports.py::test_numeric_units_ranges_approximation_and_measurement_kind_are_independent` |
| B3-03-06 | MSI-H/MSI-L/MSS、TMB 数值/定性与有限原词 | `tests/facts/test_molecular_coded_sources.py::test_typed_categories_must_match_their_complete_original_window`、`tests/facts/test_molecular_coded_sources.py::test_unclassified_category_preserves_complete_original_without_claiming_report_uncertainty` |
| B3-03-07 | 分子报告内 PD-L1 复用旧 IHC 评分 | `tests/facts/test_molecular_boundaries.py::test_actual_pdl1_ihc_assay_stays_separate_from_cd274_copy_number`、`tests/facts/test_molecular_pipeline.py::test_mixed_pdl1_shared_ihc_context_is_current_only_under_its_actual_assay` |
| B3-03-08 | 限定阴性、未检出、未检测与未判断 | `tests/facts/test_molecular_boundaries.py::test_scoped_negative_retains_detection_kinds_and_no_missing_variant_rows_are_synthesized`、`tests/facts/test_molecular_context.py::test_negative_scope_cannot_expand_beyond_own_original_text` |
| B3-03-09 | 药名组合、完整变异集合、方向/等级体系及报告日期 | `tests/facts/test_molecular_boundaries.py::test_drug_association_is_ordered_entire_set_and_direction_is_reported_only`、`tests/facts/test_molecular_context.py::test_drug_relation_keeps_both_actual_variant_heads_and_exclusion_of_either_invalidates` |
| B3-03-10 | 药物关联不完整、跨 panel 或未知方向 | `tests/facts/test_molecular_boundaries.py::test_partial_reference_to_one_known_and_one_unknown_variant_is_not_reduced_to_known_subset`、`tests/facts/test_molecular_boundaries.py::test_drug_reference_to_a_previous_panel_is_preserved_unlinked` |
| B3-03-11 | 明确表格/叙述、实际 OCR 与 worker 持久化 | `tests/facts/test_molecular_extraction.py::test_actual_persistence_from_ocr_and_completed_idempotence`、`tests/facts/test_molecular_extraction.py::test_actual_upload_worker_keeps_unpublished_candidates_unusable_then_publishes` |
| B3-03-12 | 多报告/并列列、缺表头、续页与完整身份去重 | `tests/facts/test_molecular_extraction.py::test_two_reports_in_one_block_never_borrow_panel_or_variant_identity`、`tests/facts/test_molecular_boundaries.py::test_missing_table_header_records_unresolved_scope_without_guessing_columns` |
| B3-03-13 | 来源角色、对照/QC、TNB/ITH 与未读页面 | `tests/facts/test_molecular_boundaries.py::test_control_metadata_and_result_sections_do_not_become_current_assay`、`tests/facts/test_molecular_context.py::test_non_current_source_never_usable` |
| C3-01 | 真实锚唯一性、同报告闭包、UNKNOWN 与完整成员 | `tests/facts/test_molecular_context.py::test_context_rejects_false_actual_associations`、`tests/facts/test_molecular_context.py::test_duplicate_variant_anchor_rejected_and_identity_correction_requires_replacement` |
| C3-02 | 原片段完整窗口、手工分片和复制锚界限 | `tests/facts/test_molecular_assertion_windows.py::test_actual_manual_assertion_uses_complete_source_window`、`tests/facts/test_molecular_manual_sources.py::test_real_anchor_proof_cannot_hide_same_statement_modifier_past_own_count` |
| C3-03 | CODED 更正与旧错误修订持续失效 | `tests/facts/test_molecular_coded_sources.py::test_correction_cannot_invert_a_typed_category_against_the_original`、`tests/facts/test_molecular_coded_sources.py::test_existing_mismatched_typed_revision_is_never_a_current_usable_result` |
| M4-01 | 原件核对、补录、确认及未知不可用 | `tests/facts/test_molecular_views.py::test_real_report_and_field_routes_use_molecular_controls_and_preserve_source`、`tests/facts/test_molecular_views.py::test_manual_unlinked_molecular_quantity_remains_pending_then_confirmed_but_unusable` |
| M4-02 | 整组替换/回滚、完整药物用户与 UNDO | `tests/facts/test_molecular_replacement.py::test_replace_variant_includes_every_drug_user_and_preserves_second_variant`、`tests/facts/test_molecular_replacement.py::test_partial_replacement_rolls_back_every_new_candidate` |
| M4-03 | 长组件搜索和原始控件无损往返 | `tests/facts/test_molecular_search.py::test_actual_records_search_finds_full_identity_components_without_reviving_excluded_fields`、`tests/facts/test_molecular_form_transport.py::test_unchanged_original_whitespace_and_list_structure_survive_http_confirmation` |
| M4-04 | 手机/桌面原图加载、高亮与整组操作 | `tests/browser/test_molecular_browser.py::TestMolecularBrowser::test_phone_actual_automatic_source_images_and_highlights_preserve_components`、`tests/browser/test_molecular_browser.py::TestMolecularBrowser::test_desktop_and_phone_actual_original_review_and_group_undo` |
| M5-01 | 最小身份束与全格式实际内容 | `tests/exports/test_molecular_exports.py::test_selected_variant_quantity_carries_complete_identity_but_no_private_anchors`、`tests/exports/test_molecular_exports.py::test_actual_pdf_json_csv_zip_contents_and_explicit_original_bytes` |
| M5-02 | 严格 reader、全部已合旧域和有限来源 | `tests/exports/test_molecular_exports.py::test_reader_rejects_new_molecular_data_with_lost_identity_or_policy`、`tests/exports/test_molecular_exports.py::test_strict_reader_cannot_restore_private_data_or_remove_required_meaning` |
| M5-03 | 真实分享交换、角色/会话及选定范围 | `tests/patients/test_molecular_sharing.py::test_actual_http_share_exchange_preserves_only_selected_molecular_value`、`tests/patients/test_molecular_sharing.py::test_non_managing_family_member_cannot_share_selected_molecular_unit` |
| M5-04 | 手机/桌面选择→预览→分享→失效 | `tests/browser/test_molecular_outputs_browser.py::TestMolecularOutputsBrowser::test_desktop_selected_output_and_fine_share`、`tests/browser/test_molecular_outputs_browser.py::TestMolecularOutputsBrowser::test_phone_selected_output_and_fine_share` |
| M6-01 | 真实迁移保留旧 1.0/1.1、IHC/日期与确认 | `tests/facts/test_molecular_migrations.py::test_real_0003_to_0004_preserves_legacy_rows_and_confirmations` |
| M6-02 | 预览/构建/存储/首块每块与正常提交失效 | `tests/integration/test_molecular_output_postgres.py::test_waiting_output_rejects_real_committed_dependency_change`、`tests/integration/test_molecular_output_postgres.py::test_real_author_collector_commit_after_render_discards_old_body` |
| M6-03 | 字段/报告最后来源读取后撤权或删除 | `tests/facts/test_molecular_final_access.py::test_final_source_reread_does_not_leave_a_private_response_after_access_changes`、`tests/integration/test_molecular_context_postgres.py::test_final_source_read_discards_body_after_another_connection_commits` |
| M6-04 | 上下文更正/排除、替换等待与分享持续清理 | `tests/integration/test_molecular_context_postgres.py::test_waiting_whole_molecular_replacement_rejects_committed_changes`、`tests/integration/test_molecular_context_postgres.py::test_waiting_molecular_confirmation_cannot_escape_committed_report_exclusion` |
| M6-05 | 早期事实异常报告与原因链保护 | `tests/facts/test_molecular_privacy.py::test_actual_early_fact_failure_keeps_originals_out_of_error_chain_and_logs` |
| M6-06 | worker 重试、租约丢失、已发布图和原件保真 | `tests/processing/test_molecular_retry.py::test_actual_retry_replaces_unpublished_graph_but_preserves_published_history_and_original`、`tests/processing/test_molecular_retry.py::test_retry_refuses_to_destroy_audited_or_manual_unpublished_material` |
| M7 | 真实分子金标准完整分母、逐页标注/独审及获批评测 | NOT_RUN |
| M4-ACTION-COVERAGE-LIMIT | 原专用证据限制已由b605与最终组合闭合 | `tests/facts/test_molecular_delivery_boundaries.py::test_actual_molecular_defer_undo_confirm_controls_selected_output` |

## 旧失败、清单边界与尚未完成范围

基础独审发现的重复组件误判、阴性范围扩张、有限代码反转、裁掉否定/人工分片边界均保留
原红例及各次作者修订；2e72人工来源增量已由非作者24项关闭。CODED87bfe再经独立42项，
M4控件原值和最后来源访问两项P2由44a8修订关闭。早期通过不重标为后续缺陷已关闭。
M5原8bb由root独立88+3项审查；组合审查者曾是M5作者，因此只独立批准其他作者的合流增量，
不自审其原M5实现。

87bfe原清单漏掉3个中文历史Markdown，1284不代表完整1287；原回执不改，另有勘误。
不同checkout的68项CRLF布局差异不能直接当源码内容变化；独审保留原执行1329字节SHA，
规范换行后的内容与准确Git头一致。原135个verification JSON Git blobs保持；最初作者132
制品计数是不同范围，不混为相同分母。

首次M5组合88通过/3失败包含“报告耐药”有限前缀回归和两处新测试错误地要求携带private raw。
短暂改成“原报告耐药”的91通过不是最终证明；原合法fixture恢复，完整原词及全/裁片否定
反控随后40通过，最终1459包含原M5用例。原失败、临时尝试及fixture设置错误单独保留。

v3草稿中的DEFER与元数据专用证据待补状态作为历史原件保留；当前已按上述b605及最终运行
闭合。32条需求映射仍区分纯合同、旧检查点和新增实际执行，不把单一夹具存在视为整行验收。

8a54的候选portable1.6建立在当时main1.5上，仅代表该历史检查点；最终32c6已正常合入
实际癌症main9cc并使用portable1.8。随后已从实际main bb896b9正常同步39dd的自动版本更新及PR87发布文档，本功能应用发布号仍为空；未从未合文档/功能分支取内容。
新PR精确CI、Squash与本功能发布证据尚未建立。
另已核实已发布主线 `39dd1aef73170482085301ac231ebf73549562ba` 的 CI `34537823185`
成功：普通4404通过、4跳过、353未选择；PG346、必跑浏览器18、JavaScript9通过，容器构建通过。
该发布主线仅较9cc增加六个自动版本文件，现随实际main bb896b9同步；它的通过不替代分子PR CI。
原始读回回执SHA：`3ae776ed7e745a1aa05bbbe21a4b5c218e58a6411a8221f5173718a7efe561bf`。
真实M7、真实分子准确率/完整报告质量及生产放行均未建立；原金标准、未判断页与封存不改。


## 新增实际断言定位

- 元数据读回：`tests/facts/test_molecular_delivery_boundaries.py::test_extracted_metadata_keeps_own_specimen_assay_dates_and_selected_values`。
- 真1.6/1.7旧包与低版分子拒绝：`tests/exports/test_molecular_combined_domains.py::test_actual_16_17_compatibility_is_not_unpublished_molecular_16`。
- 跨域末次读取：`tests/exports/test_molecular_combined_guards.py::test_last_domain_read_cannot_return_other_domain_stale_controls`；PG次序见 `tests/integration/test_molecular_combined_postgres.py::test_final_choice_guard_serializes_actual_molecular_commit`。
- 原生TLS四域手机：`tests/browser/test_molecular_combined_browser.py::TestMolecularCombinedBrowser::test_phone_four_domain_share_clears_after_molecular_change`。
- 实际混合worker：`tests/cancer_ordering/test_molecular_extraction_inventory.py::test_actual_worker_combines_pathology_and_molecular_without_losing_typed_candidates`；旧确认保真、未知身份拒绝见同文件。
- 真实PG列宽与拒绝有损收窄：`tests/integration/test_molecular_inventory_migration_postgres.py::test_actual_legacy_identity_column_upgrade_preserves_records_and_allows_full_pipeline`。

初始主线组合14项中的2个应用红例和1个共享云证据期望错误、后续8项提取身份失败与8项PG
列宽失败均保留原XML/日志/源树。独立因果实验只修改隔离测试库中的一个身份值证明原因，
不属于生产修复；并行write-tree的index.lock启动失败发生在pytest之前，单列为运行器设置问题。
更新哈希仅绑定这些真实执行，不把失败改标为通过，不启动任何真实资料评估。

## 已审文档后的实际发布主线同步

正式文档 `f8519a734688b8bb6b99819533c40c8bf3d1208f` 经独立审查，无未关闭P1/P2；
root闭合SHA `7288f3cf86b4db86d2b57ff319fe43f4c687162bd20629d995fa2382004e6b8b`。
PR87准确CI34540046604成功（普通4404通过/4跳过/353未选择、PG346、浏览器18、JavaScript9），
随后已实际Squash为main `bb896b9a0084b5a2c7ccdedf12feaa8d6a5fc23b`。
本分支只正常合入这一实际主线；两入口及登记表保留双方完整记录，六个自动版本文件原样继承。
应用和测试仍为已独审32c6的Git字节，portable仍1.8；本次文档/版本同步不重跑健康应用矩阵，
旧阶段、失败及真实评估边界保持原样。PR87原始终态回执SHA：
`224275b582f73f21ba102785dbc22863dc42678b32940a4be91d6ea760f167fd`。
该同步增量待独审；分子自身精确PR CI、Squash及发布尚未建立，真实M7未启动。
