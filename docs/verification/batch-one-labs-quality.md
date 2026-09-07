# 第一批检验关联质量验证

本记录对应[五批需求](../specs/2026-09-07-batches-one-five-requirements.md)的 B1-03
检验部分和[实施计划](../plans/2026-09-07-batches-one-five-implementation.md) Task 3。
双栏表头及项目代码关联已完成本地验证；事实提取、图像增强及其他批次另行交付。
当前交付状态以[登记表](../document-registry.json)为准。

## 修复内容

双栏报告中的“序”“代号”“序代号”此前没有完整参与表头识别，右栏序号和代号会
进入左栏单位。新增简写及“标志单位”表头识别，并根据后续完整表头划分表格；单表
中的代号列即使排在结果列后，也不会被误当作新表格。

OCR 合并了序号、代号和中文项目名时，保留整个来源区域及关联不确定性，避免漏掉
这一行。独立代号与项目名称仅通过字典中已审核的组合别名消歧；冲突代号进入待核对
候选，不推断标本。原始项目名、原文、来源坐标及单位字符保持可追溯。

实现位于[布局关联](../../apps/labs/layout.py)和[检验提取](../../apps/labs/extraction.py)，
布局规则版本为 `lab-layout-v3`。[回归用例](../../tests/labs/test_phase_two_layout.py)使用
合成数据，覆盖非对称表头、列顺序、合并名称、冲突代号和来源保留。

## 固定基线与真实结果

[本轮基线](artifacts/batch-one-labs-baseline-manifest.json)固定主分支
`e7908768f68998f5d33eca7820f14c406ef0fb71`。它与本轮起始提交 `b7d5f34` 的差异仅为
规格和计划文档。基线预测与[既有验证](artifacts/labs-extraction-scope-evaluation.json)
身份一致，并独立复现了 268 / 379 / 367 的联合 TP / FP / FN。

[真实评测](artifacts/batch-one-labs-evaluation.json)保留全部 64 文件、60 报告组、734 个
裁定源行表示及未判断范围。全部原件、OCR 缓存、标注和基线预测均校验身份；使用同一
评分器，不修改金标准或减少分母。运行经过实际持久化、有效结果验证和趋势可用性计算。

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 成功 / 仅原件 / 失败 / 缺失文件 | 45 / 19 / 0 / 0 | 45 / 19 / 0 / 0 |
| 联合 TP / FP / FN | 268 / 379 / 367 | 373 / 280 / 262 |
| 联合精确率 | 41.42% | 57.12% |
| 联合召回率 | 42.20% | 58.74% |
| 联合 F1 | 41.81% | 57.92% |
| 额外项目误抽 | 56 | 48 |
| 已知错误 / 受限制 / 未受限制 | 414 / 414 / 0 | 313 / 313 / 0 |
| 正确联合转录但仍受限制 | 262 / 268 | 368 / 373 |
| 可进入趋势 / 总结果 | 3 / 752 | 3 / 758 |
| 已评测 / 未评测预测行 | 731 / 21 | 737 / 21 |

联合评分仅纳入联合字段均有裁定值的 635 行；734 个源行仍全部参与各自有标注的字段
评分。完整匹配增加 105 条，各字段召回率均未下降。另按固定的一对一来源配对逐条
比较，原先正确的八类字段没有变成错误或缺失。

| 字段 | 修复前 TP / FN | 修复后 TP / FN |
| --- | --- | --- |
| 项目编码 | 524 / 148 | 546 / 126 |
| 原始项目名 | 404 / 328 | 577 / 155 |
| 标本 | 340 / 394 | 340 / 394 |
| 结果值 | 641 / 93 | 654 / 80 |
| 结果类型 | 621 / 113 | 634 / 100 |
| 单位 | 378 / 303 | 489 / 192 |
| 参考范围 | 551 / 160 | 560 / 151 |
| 报告日期 | 610 / 112 | 624 / 98 |

## 自动化验证与复现

具体检查和制品身份见[自动化验证记录](artifacts/batch-one-labs-verification.json)。

- 检验与真实评测工具回归：321 项通过。
- 布局、提取范围及处理持久化回归：78 项通过；与上述套件存在重叠，不相加计数。
- [固定合成评测](artifacts/batch-one-labs-synthetic-evaluation.json)门禁通过。
- 真实评测门禁通过，全部已知严重字段错误仍受限制。

公开制品仅包含散列、计数、误差字段及代码身份；真实原件、路径、预测和标注保存在
本地忽略目录。准备相同私有输入后执行：

```powershell
python -m pytest tests/labs tests/tools/test_phase_two_evaluation.py -q
python -m pytest tests/labs/test_phase_two_layout.py tests/labs/test_extraction_scope.py tests/processing/test_phase_two_pipeline.py -q
python tools/phase_two_evaluation.py --synthetic-only --report .runtime/batch-one-labs/synthetic-replay.json
python tools/phase_two_evaluation.py --source-map "$env:PHR_EVALUATION_INPUTS/source-map.json" --ocr-cache-dir "$env:PHR_EVALUATION_OCR" --annotations "$env:PHR_EVALUATION_INPUTS/source-annotations-adjudicated.json" --classification "$env:PHR_EVALUATION_INPUTS/source-classification-frozen.json" --baseline-predictions "$env:PHR_EVALUATION_BASELINE/current-predictions.json" --baseline-manifest docs/verification/artifacts/batch-one-labs-baseline-manifest.json --private-output .runtime/batch-one-labs/replay --report .runtime/batch-one-labs/real-replay.json
python tools/verify_documentation.py
```

运行期间保持解析依赖不变；工具会拒绝中途发生代码变动的评测。`PHR_EVALUATION_BASELINE`
必须指向本记录固定的主分支预测，不能使用更早版本或本轮修复后的预测。

## 适用限制

这是固定 OCR 后解析的开发回归，不能证明新 OCR 或图像增强效果，也没有外部留出集。
单位 OCR 字符混淆、单位与参考范围合并、标本缺失等问题仍存在；正确转录结果仍有大量
待核对，趋势可用量尚未增加。本次没有放宽质量限制，也未把不确定候选改成已核对事实。

源码交付仍需独立审查和 PR 检查；版本确定后补充实际发布关联。
[生产放行门禁](release-gate.md)沿用原有结论。
