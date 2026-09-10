# 分子字段值合同验证

本记录覆盖[分子字段值合同 A0](../specs/2026-09-10-molecular-value-contracts.md)，
属于五批 B3-03 的共享模块检查点。应用入口尚未接入，完整分子功能仍在实施。
本地结果记录于[机器证据](artifacts/molecular-value-contracts.json)。

## 实际验证

2026-09-10 在主线基线 `9675f0e3f61f96eb4895c364229b9da9d8a27bdb` 的独立分支实施。
生产模块包含 28 个只读字段描述、原文组件校验、值校验和显示函数。
输入全部是代码中的合成值，没有加载患者资料、医疗原件或真实 OCR。

作者首先运行了 79 项失败测试，失败原因是待开发模块缺失；实现后 79 项通过。
补充密码子反例时实际出现 1 项失败，加入密码子身份组件后 94 项通过。
这段执行历史用于说明测试曾暴露实现缺口，不替代最终复测。

根代理对冻结代码运行：

```powershell
python -m pytest -q tests/facts/test_molecular_contracts.py tests/facts/test_clinical_foundation.py tests/facts/test_imaging_quantitative.py
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python tools/verify_documentation.py
python tools/verify_traceability.py
```

合成合同和既有临床/影像回归共 **145 项通过、0 失败、0 跳过**，耗时 27.34 秒。
Django 检查没有问题，迁移检查没有变化；文档和既有追踪矩阵检查通过。
追踪矩阵的 60 项已验证、2 项外部待验证是既有矩阵结果，不是本次新增分子应用验收数量。

## 独立审查与交付

独立审查覆盖本分支全部 9 个变更文件，结论为 A0 范围通过，P1/P2 均为 0。
审查者另行编写的 **454 个有界合成输入探针全部通过**，独立重跑的 **51 项既有临床/影像
回归全部通过、0 跳过**（26.78 秒）。探针数与 pytest 测试数分别记录，不合并成新的功能覆盖率。
审查前后源码与测试哈希一致；审查报告摘要和制品哈希见机器证据。
[PR #72](https://github.com/skuyd/emr/pull/72) 已提交，尚未合并，发布版本未确定。
首个提交 `b698109b8c27252a984061db526d2cc87b1e7a87` 的
[CI 运行 34462118014](https://github.com/skuyd/emr/actions/runs/34462118014) 四项均未启动：
GitHub 注释明确账户付款或支出限额问题，所有任务 `runner_id=0`、步骤列表为空。
这是该提交的外部执行阻塞证据；不能当作测试失败或通过，也不替代后续提交的 CI 检查。

## 证据能支持的范围

- 原组件的未知与未印状态、原字符、转录本版本、密码子、融合伙伴顺序保持可表达。
- panel 未解析数值不补零；比较符、范围、原单位和日期精度按声明保留。
- 错误 JSON 形状、布尔数值、NaN、无穷、负数、倒置范围和非法日期被拒绝。
- MSI 类别、原数量、TMB 原定性、药物原组合与等级体系独立保存，不执行医学推断。
- 新模块未注册进旧字段表，没有修改旧影像模式、数据库、IHC 或 PD-L1 评分。

校验器只验证结构，不证明原文与枚举/数值一致，也不证明标本、检测或来源身份。
缺失单组件的显示函数返回空文字，消费者必须同时显示状态。
变异 `COMPLETE` 仅表示声明的身份组件结构完整，不能作为确认、导出或分享门禁。

自动解析、来源范围、实际入库、修订、失效、权限、页面、搜索、导出/分享以及分子报告中
复用 PD-L1 的完整链路仍待后续开发和验收。本次没有执行真实资料评估，不能报告分子抽取
准确率或生产放行；既有环境封存包及独审证据均未改写。
