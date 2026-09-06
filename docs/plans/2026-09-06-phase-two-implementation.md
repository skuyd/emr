# 第二阶段实现计划

> **For agentic workers:** 使用 `superpowers:subagent-driven-development` 按任务实现与评审；任务步骤用复选框记录执行日志，交付状态以登记表和验证证据为准。

**Goal:** 实现第二阶段 P2-01 至 P2-09，并逐项验证 P2-AC01 至 P2-AC11。

**Architecture:** 保留 Django 模块化单体和现有私有原件/OCR 流水线。字典与解析结果采用不可变版本；字段质量、人工修订、授权复核与字典候选作为独立记录。所有用户展示从同一个有效结果解析器读取，权限与并发检查在事务内完成。

**Tech Stack:** Python、Django 5.2、PostgreSQL、Django Templates、原生 CSS/JavaScript、pytest、Playwright；沿用现有依赖与 OCR 提供者。

**Spec:** [第二阶段需求范围](../specs/2026-09-06-phase-two-requirements.md)

## 全局约束

- 保留原件、OCR、原始字段、来源、解析与字典版本；不推算医学派生结果，不生成诊疗建议。
- 用户核对可跳过，确认不能绕过单位、日期、来源和可比性检查。
- 数值、比较符、定性、半定量、状态分别表示；不平均或丢弃同日记录。
- 字典候选必须审核并通过冻结回归后发布；新版本不能覆盖旧版本或人工修订。
- 复核只允许访问明确授权任务；撤权、删除、过期版本和并发写入必须失败。
- 真实样本与合成样本分开报告；失败、拒绝抽取、仅原件计入全量分母；无样本标为未评测。
- 现有示例不足不增加样本数量门槛，不虚构标注、版本或质量证据。
- 从已获取的最新 `origin/main`（`a8fe8bb`）创建 `feat/phase-two-trust`；工作区 `.worktrees/phase-two`。
- 文档仅进入治理允许目录；不修改自动软件版本字段或 Changelog 自动区域。

## Task 1: 检验指标清单与上下文字典（P2-01 / AC01、AC03）

**Files:** 修改 `apps/labs/dictionary.py`；新增 `apps/labs/dictionaries/phase-two.json`、`apps/labs/dictionaries/phase-two-coverage.json`、`tests/labs/test_phase_two_dictionary.py`。

**Interfaces:** `IndicatorDefinition` 增加有默认值的 `specimen`、`result_types`、`tier`；`IndicatorDictionary.match(raw_name, *, specimen="", panel="")` 保留旧调用形式，歧义无上下文返回空。已有标准编码保持稳定。发布逻辑继续使用 `load_dictionary(path)` 与 `current_dictionary()`。

- [x] 逐项转写原始需求 2.3 核心/扩展表，记录原始行、编码、标本、别名、类型、重复/拆分关系、缺口和样本类型；基因独立列出。
- [x] 先添加测试并运行，证明血/尿葡萄糖及两种 PCT 不会串配：

```python
assert dictionary.match("PCT") is None
assert dictionary.match("PCT", panel="CBC").code != dictionary.match("PCT", panel="INFLAMMATION").code
assert dictionary.match("葡萄糖", specimen="URINE").code != dictionary.match("葡萄糖", specimen="BLOOD").code
```

- [x] 补齐每一目标项目的正例与歧义负例；重复转铁蛋白保留两个原始条目的关系，钙与校正钙不得静默混同。
- [x] 实现严格校验及上下文消歧；旧字典仍可载入。对所有项目输出明确的合成覆盖与真实样本未验证标记。
- [x] 运行 `python -m pytest -q tests/labs/test_dictionary.py tests/labs/test_phase_two_dictionary.py`，审阅差异后纳入统一提交。

## Task 2: 版面关联与字段来源（P2-02 / AC02、AC03）

**Files:** 修改 `apps/labs/extraction.py`、`apps/labs/candidates.py`；新增 `apps/labs/layout.py`、`tests/labs/test_phase_two_layout.py`。

**Interfaces:** 保留 `extract_observations(pages, dictionary=None)`；`ExtractedObservation` 增加默认字段 `specimen`, `field_evidence`, `quality_issues`, `normalization_candidates`, `reference_range`。字段证据格式为 `{field: {page_number, polygon, precision}}`，precision 为 `region` 或 `page`。质量原因为带 `code`、`rule_version`、`fields`、`details` 的 JSON 条目。

- [x] 写失败用例：同行左右栏各一项、列顺序变化、多表/重复表头、跨页续表、缺失单元格、歧义关联、比较符/小数点混淆、上标单位和五种值类型。

```python
assert [(x.raw_name, x.raw_value) for x in extract_observations(two_column_pages)] == [("白细胞", "5.2"), ("血红蛋白", "130")]
assert result.field_evidence["raw_value"]["page_number"] == 2
```

- [x] 根据列头/水平区域划分独立表格，按行关联字段；续表继承可靠列结构；不确定配对保留候选和原因，避免发布可信值。
- [x] 保留修复候选前后内容，不把推测当作原值；低置信度/OCR 失败不能从评测分母中消失。
- [x] 运行 `python -m pytest -q tests/labs/test_extraction.py tests/labs/test_phase_two_layout.py tests/processing`，审阅差异后纳入统一提交。

## Task 3: 质量、修订与授权复核（P2-03、P2-04、P2-05、P2-09 / AC04–AC06、AC10）

**Files:** 修改 `apps/labs/models.py`、`apps/processing/pipeline.py`、`apps/processing/models.py`、`apps/documents/deletion.py`；新增 `apps/labs/revisions.py`、`apps/labs/review.py`、`apps/labs/validation.py`、迁移和 `tests/labs/test_phase_two_workflows.py`。

**Interfaces:** `effective_observation(observation)` 返回保留原模型身份及来源的展示副本；`revise_observation(actor, observation_id, *, action, changes, expected_revision)` 追加事件并乐观检查；`create_review_task(owner, observation_id, *, reviewer=None)`、`transition_review_task(actor, task_id, *, action, expected_revision, changes=None)` 在资料聚合锁下检查授权、版本、删除和并发。`validate_observation(observation, *, previous=(), dictionary=None, rules=None)` 返回可解释质量条目。

- [x] 用真实 Django 模型测试可选核对、更正/撤销、原值不可变、确认仍保留单位/日期错误、不同用户隔离、撤权后读取/提交失败、版本切换和旧任务写入失败。

```python
revision = revise_observation(owner, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
row.refresh_from_db()
assert row.raw_value == "62"
assert effective_observation(row).raw_value == "6.2"
with pytest.raises(RevisionConflict):
    revise_observation(owner, row.pk, action="CONFIRM", changes={}, expected_revision=0)
```

- [x] 增加字段证据、质量原因、规则版本、并发计数；修订、复核任务/事件与 OCR 置信度分开存储。审核只解除具体已核实原因。
- [x] 校验类型、单位、关联、日期、同报告一致性及满足前置条件的历史差异；量级候选不推测疾病风险，不丢弃异常原值。
- [x] 重解析后按来源/值比较人工版本，显式返回冲突；切换旧版本或撤销立即使用统一有效结果。
- [x] 删除时撤销访问入口，级联清理任务与候选证据；统一锁顺序与现有删除流程相容。
- [x] 运行 `python -m pytest -q tests/labs/test_phase_two_workflows.py tests/documents tests/processing` 和迁移检查，审阅差异后纳入统一提交。

## Task 4: 字典候选审核与发布（P2-06 / AC07）

**Files:** 新增 `apps/labs/dictionary_workflow.py`、`tests/labs/test_phase_two_dictionary_workflow.py`；修改 `apps/labs/models.py`、`apps/labs/dictionary.py`、`apps/operations/models.py`、`apps/processing/pipeline.py` 和对应迁移。

**Interfaces:** `collect_dictionary_candidates(version)` 幂等收集未知项目/别名/单位；`review_candidate(actor, candidate_id, *, decision, definition, rationale, expected_revision, rules=None, totp_verified_at=None)`；`preview_dictionary(actor, *, candidate_ids, version)` 生成差异及稳定预览哈希；`publish_dictionary(actor, *, version, candidate_ids, expected_active_hash, expected_preview_hash, totp_verified_at=None)` 验证定义、规则和冻结回归后发布不可变快照；`rollback_dictionary(actor, release_id, *, expected_active_hash, totp_verified_at=None)` 重新执行评测并切换指针。每次发布或回退追加评测事件，原发布报告不可覆盖。

- [x] 先测试候选不影响映射、无授权不能读原件证据、重复去重、冲突拒绝、未经审核/回归失败不能发布、版本不可覆盖与回滚生效。

```python
assert current_dictionary().match("合成新指标") is None
with pytest.raises(DictionaryWorkflowError):
    publish_dictionary(operator, version="test-2", candidate_ids=[pending.pk], expected_active_hash=baseline.content_hash, expected_preview_hash="stale")
```

- [x] 分离候选证据访问与字典定义；换算/可比性/校验规则必须含项目、前置条件、审核人和依据。
- [x] 发布前展示结构差异并执行固定评测；记录候选和报告摘要，旧解析继续指向原版本。
- [x] 运行 `python -m pytest -q tests/labs/test_phase_two_dictionary_workflow.py tests/operations`，审阅差异后纳入统一提交。

## Task 5: 用户入口、纵向对比与一致展示（P2-04–P2-07、P2-09 / AC05–AC10）

**Files:** 新增 `apps/labs/comparison.py`、`apps/labs/views.py`、`apps/labs/urls.py`、`templates/labs/` 页面、`tests/labs/test_phase_two_views.py`、`tests/labs/test_phase_two_comparison.py`；修改 `config/urls.py`、`apps/documents/detail.py`、`apps/documents/archive.py`、`apps/labs/trends.py`、`templates/documents/detail.html`、导航及相应 CSS。

**Interfaces:** `comparison_view(patient, *, start=None, end=None, category="")` 返回报告独立列及指标分组行；每格保留有效/原始值、来源、质量原因、可比状态、参考对照及所用版本。只对可信精确数值和已审核项目规则生成趋势。

- [x] 先测试行列方向、同日双报告、项目/时间筛选、不同标本和方法隔离、规则换算留存原值、定性/比较符不连线、缺失参考不套默认值。

```python
view = comparison_view(patient)
assert len(view.columns) == 3  # 同日两份报告仍是两列
assert unknown_reference.reference_label == "无法对照"
assert special_result.trend_eligible is False
```

- [x] 详情提供“与原件一致 / 识别有误 / 暂不处理”、可选日期/项目/结果/单位更正与撤销、来源定位和修订历史。
- [x] 增加授权复核队列、任务对照页、授权/撤权、状态历史与冲突反馈；原件流式请求重复验证任务权限。
- [x] 增加字典候选、审核、差异、发布、回退页面；受权限保护，采用现有 CSRF/登录和私有响应模式。
- [x] 统一详情、列表搜索、对比及趋势有效结果；允许选择历史解析版本并提示人工冲突。
- [x] 运行 `python -m pytest -q tests/labs/test_phase_two_views.py tests/labs/test_phase_two_comparison.py tests/labs/test_trends.py tests/documents`、`npm run test:js` 及实际浏览器流程，审阅差异后纳入统一提交。

## Task 6: 固定评测与完整交付审计（P2-08 / AC01–AC11）

**Files:** 新增 `tools/phase_two_evaluation.py`、`tools/freeze_phase_two_baseline.py`、`apps/labs/release_evaluation.py`、`tests/tools/test_phase_two_evaluation.py`、`tests/labs/test_phase_two_release_evaluation.py`、`apps/labs/dictionaries/phase-two-regression.json` 与 `phase-two-release-evaluation.json` 固定合成数据、`phase-two-baseline.json` 历史基线、`docs/verification/phase-two.md` 及机器制品；更新登记表、文档入口及需求状态。

**Interfaces:** 评测器输入冻结的样本清单/分组、独立标注与预测，输出真实/合成、全量/成功子集的计数、字段 PRF、联合匹配、严重错误、拦截率、趋势比例、定位率、耗时、基线变化和未覆盖范围。`python tools/phase_two_evaluation.py` 提供可重复运行入口。

- [x] 先用手算夹具验证漏抽、错误、失败与未评测分母：

```python
assert metrics["correct"] == 1
assert metrics["wrong"] == 1
assert metrics["missed"] == 1
assert metrics["recall"] == 1 / 3
```

- [x] 固定全部 64 个示例文件哈希并逐份分类/按内容归组，记录是否可标注及排除理由；本地真实正文不进入仓库或公开日志。
- [x] 独立标注核对分歧并保留来源哈希；使用历史不明的示例一律归为开发回归；无可信标注不得伪造准确率。
- [x] 冻结基线并以同一标注比较新解析；每个原始检验项目使用固定合成正反例覆盖，真实质量单独报告。
- [x] 输出 AC01–AC11 到实际测试与制品的追踪矩阵，逐项审阅证据范围；记录性能与质量目标差距。
- [x] 运行 `python -m pytest -q`、`npm run test:js`、Django `check` / `makemigrations --check --dry-run`、必要 PostgreSQL 并发与浏览器检查、`python tools/verify_documentation.py`。CI 已接入公开评测及制品归档，同时执行发布自动化及版本一致性的强制校验。
- [x] 完整分支评审后修复缺口，保留第一阶段生产门禁现状；仅在本期规格逐项有证据时标记完成。

## 执行记录

- 基线：2026-09-06，`python -m pytest -q tests/labs tests/processing tests/documents`：397 passed、1 skipped（23.33 秒）；跳过项不计通过。
- 每个任务的实现记录、评审结论与测试命令追加至 `docs/verification/artifacts/phase-two-worklog.json`。
- 各任务先独立评审并修复确认的问题，完成集成验证后统一提交，避免提交尚未满足跨任务接口约束的中间状态。
- 最终验证：1404 项 Python 通过、38 跳过；其中 35 项数据库用例独立在 PostgreSQL 通过，剩余 1 项 OCR 模型和 2 项符号链接权限未运行。JavaScript 6 项通过。
- 64 文件真实回归、556 输入公开合成评测及历史基线均重复执行并比较确定性字段。功能与现有示例门禁通过，真实质量目标差距见[第二阶段验证记录](../verification/phase-two.md)。
- 集成评审与最终跨平台复审的问题均已修复，发布自动化、版本、迁移及文档校验通过；开发验收时版本待确定，后续随 [v1.1.0](../releases/v1.1.0.md) 发布。
