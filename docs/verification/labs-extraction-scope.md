# 检验抽取范围修复验证

本次处理[第二阶段验证](phase-two.md)暴露的非检验内容误抽问题，依据
[第二阶段规格](../specs/2026-09-06-phase-two-requirements.md)中 P2-AC02、P2-AC03、
P2-AC04 和 P2-AC11 的范围与保真要求。分支 `fix/labs-extraction-scope` 从最新
`origin/main` 的 `d785fd201aca1f4d8aa8410df55bd7222041962f` 创建；补丁版本尚未确定。
实现提交为 `4bbdc163f6f156631d87d187b9b82bc0460de4be`；状态以[登记表](../document-registry.json)为准。

## 修复与适用范围

无表头回退曾把 PDF 文字层的单字、文献编号和基因变异说明交给检验行解析，其中
`K`、`P` 碎片会命中检验字典。已识别的表头也会延续到其后的非检验章节。

修复在[行关联层](../../apps/labs/layout.py)统一作用于检验观察和字典候选：

- 独立非检验标题或说明章节结束对应表格；换页重置，明确续表按原规则继承。
- 非检验范围只有出现项目、结果及单位或参考范围等表头证据才恢复提取。
- 双栏表分别保存列位置、结束状态和表头置信度；一栏重开或更新不会改变另一栏。
- 表格结束时移除其标本和面板上下文，避免后续项目借用旧依据。
- 对大量单字碎片组成的无表头行，要求完整可识别项目名或独立结果、单位结构；
  普通斜线字符串不作为已知单位证据。未知项目在有效检验表内仍保留为待核对候选。
- 行内备注可以位于仍在继续的表格中，因此仅独立备注标题结束范围。

提取过滤保留全部 OCR 原文和原件访问。新增[范围回归](../../tests/labs/test_extraction_scope.py)
与[持久化回归](../../tests/processing/test_phase_two_pipeline.py)覆盖混合页面、续表、
同页重开、双栏隔离、逐字项目名、未知候选、质量限制及原文保留。

## 冻结基线与评测边界

在修改解析器前，先用最新 `main` 重放 64 文件，结果与上一轮第二阶段完全一致：
指标摘要 SHA-256 为 `da71663eda627eaebfb5375d9f0bc87a577ba753f49141367eb3623553172538`。
[本轮基线清单](artifacts/labs-extraction-scope-baseline-manifest.json)固定每个文件的原始
SHA-256、OCR 缓存 SHA-256 和本轮基线预测 SHA-256，并记录真实基线提交、运行时间、
标注身份；[原历史基线](artifacts/phase-two-baseline-manifest.json)保持不变。

对照使用相同的 64 文件、60 个报告组、734 个裁定源行表示和同一评分器；不删除困难
文件，不修改 gold、报告分组、评测范围或分母。真实患者内容、文件路径、标注和预测
保留在本地忽略目录，公共制品只包含散列、计数和误差字段。

真实样本仍为开发回归集，没有外部留出集；本轮复用固定 OCR，不宣称新 OCR 准确率或
速度。先前未评测的报告内容仍独立统计，不能把过滤后的“只保留原文”当成处理失败。

## 验证结果

[真实评测](artifacts/labs-extraction-scope-evaluation.json)门禁通过：

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 文件总数 | 64 | 64 |
| 成功 / 只保留原文 / 失败 / 缺失 | 47 / 17 / 0 / 0 | 45 / 19 / 0 / 0 |
| 已标注源行表示 | 734 | 734 |
| 额外误抽（`unmatched_project`） | 1,316 | 56 |
| 全字段联合 TP / FP / FN | 268 / 1,639 / 367 | 268 / 379 / 367 |
| 全字段联合 F1 | 21.09% | 41.81% |
| 已知错误 / 被限制 / 未被限制 | 1,674 / 1,674 / 0 | 414 / 414 / 0 |
| 正确联合转录但仍被限制 | 262 / 268 | 262 / 268 |
| 可进入趋势的结果 / 总结果 | 3 / 2,025 | 3 / 752 |
| 已评测 / 未评测预测行 | 1,991 / 34 | 731 / 21 |

额外误抽减少 1,260 条（95.74%）。最大单个专科文件的额外误抽从 1,267 降至 22；
全部文件均保留在分母。八个字段的 TP、FN 与召回均保持原值：

| 字段 | TP | FN | 修复前后召回 |
| --- | --- | --- | --- |
| 项目编码 | 524 | 148 | 77.98% |
| 原始项目名 | 404 | 328 | 55.19% |
| 标本 | 340 | 394 | 46.32% |
| 结果值 | 641 | 93 | 87.33% |
| 结果类型 | 621 | 113 | 84.60% |
| 单位 | 378 | 303 | 55.51% |
| 参考范围 | 551 | 160 | 77.50% |
| 报告日期 | 610 | 112 | 84.49% |

[固定合成评测](artifacts/labs-extraction-scope-synthetic-evaluation.json)使用原有
556 个输入、564 个 gold 行，联合 TP 564、FP 0、FN 0；9 个故障注入全部拦截，
98 个正常控制全部保留，未改变原语料或发布门槛。真实集与合成集分别统计。

最终[自动化检查记录](artifacts/labs-extraction-scope-verification.json)：完整 Python 套件
1,433 通过、38 跳过、0 失败；其中 35 项 PostgreSQL 测试交由独立 CI 环境执行，
另外跳过 1 项未启用的真实 OCR 模型检查和 2 项 Windows 符号链接用例。
JavaScript 6 项通过；Django 检查、迁移漂移检查及文档校验（53 份）通过。
独立审查复现并验证 27 个范围用例，未发现剩余重要缺陷。

检查覆盖修复后的最终代码。普通完整检验行不构建碎片识别索引，避免引入额外解析开销。
本地测试有 43 条环境或既有设置警告，具体检查与制品身份见自动化记录。

## 复现

准备上一轮保留的私有输入目录：`source-map.json`、
`source-annotations-adjudicated.json`、`source-classification-frozen.json`；另保留固定
OCR 缓存和本轮修改前生成的 `baseline/current-predictions.json`。从修复工作区执行：

```powershell
python -m pytest tests/labs/test_extraction_scope.py tests/labs/test_phase_two_layout.py tests/processing/test_phase_two_pipeline.py -q
python tools/phase_two_evaluation.py --synthetic-only --report .runtime/labs-extraction-scope/synthetic-replay.json
python tools/phase_two_evaluation.py --source-map "$env:PHR_EVALUATION_INPUTS/source-map.json" --ocr-cache-dir "$env:PHR_EVALUATION_OCR" --annotations "$env:PHR_EVALUATION_INPUTS/source-annotations-adjudicated.json" --classification "$env:PHR_EVALUATION_INPUTS/source-classification-frozen.json" --baseline-predictions .runtime/labs-extraction-scope/baseline/current-predictions.json --baseline-manifest docs/verification/artifacts/labs-extraction-scope-baseline-manifest.json --private-output .runtime/labs-extraction-scope/replay --report .runtime/labs-extraction-scope/real-replay.json
python tools/verify_documentation.py
```

两项环境变量分别指向私有输入和 OCR 缓存目录；工具会验证原件、缓存、裁定标注及
基线预测的身份。运行期间不得修改解析依赖。

## 剩余限制与后续工作

这次修复针对提取范围，不声称消除全部误抽或解决单位识别、误映射和过度限制问题。
通用行内备注、缺少章节证据的图表或专科内容仍可能产生候选；保留的已知错误仍由质量
限制阻止可信使用。单位准确性和正确结果的过度限制是后续独立工作。

[生产放行门禁](release-gate.md)仍为 `BLOCKED`，8/23 通过；本记录不改变生产放行状态。
