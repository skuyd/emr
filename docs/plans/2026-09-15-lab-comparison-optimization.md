# 检验对比优化实施计划

依据：[检验对比优化规格](../specs/2026-09-14-lab-comparison-optimization.md)。
用户于 2026-09-14 要求完整实现；采用规格推荐默认值。当前状态见[登记表](../document-registry.json)。

## 基线与工作区

- 功能分支：`feat/lab-comparison-optimization`。
- 从已成功获取的 `origin/main`、`8cf6caca77ef7c0b02d8700b51a7b97b72bdaee5` 创建。
- 原主工作区无未提交改动；原版性能比较使用同基线的独立 detached worktree。
- 不更改自动版本字段，不部署或上传本地腾讯云资料。

## 实施与检查

1. 分离展示身份、历史绘点、可比连线及派生计算；保持同格多值和修订链。
2. 实现身份搜索、分组并集、日期校验、批量机构读取与共用表头。
3. 完成单位显示、数值核对入口、异常依据、趋势开关和阅读状态恢复。
4. 完成边界和权限回归、桌面／360 px／200% 等效视口及 100×50 合成浏览器验收。
5. 比较同环境原版性能，记录实际测量；更新文档登记及证据并执行文档校验。

初始四项回归已验证原版失败，分别覆盖拆行、别名历史丢失、异常标记缺失和日期倒置未报错。
五步均已完成本地实现与验证，结果见[验收记录](../verification/lab-comparison-optimization.md)。
其中 2 秒建议性能目标尚未达到；保留原版与本分支实测数据，不把建议目标视为通过。

## 验收命令

```powershell
python -m pytest tests/labs tests/cancer_ordering tests/analytics tests/documents tests/exports tests/treatments tests/patients tests/accessibility/test_detail_trend_markup.py -q --tb=short --junitxml=docs/verification/artifacts/lab-comparison-regression.xml
python tools/run_required_tests.py tests/browser/test_lab_comparison_browser.py tests/browser/test_lab_comparison_performance.py tests/browser/test_advanced_trends_browser.py -q
python tools/verify_documentation.py
```

方法规则采用 `method_comparability` 类型，由现有字典审核／发布流程处理。
必填指标、标本、单位、明确机构列表、等价方法列表、接受缺失的明确值和版本、审核人及依据。
没有预置真实指标放宽规则；合成样例只验证机制，不宣称临床可互换性。
