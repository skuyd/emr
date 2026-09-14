# 检验对比优化本地验收

依据：[规格](../specs/2026-09-14-lab-comparison-optimization.md)、
[实施计划](../plans/2026-09-15-lab-comparison-optimization.md)。
本记录汇总 2026-09-15 的本地实现与合成验收；状态以[登记表](../document-registry.json)为准。
实现提交为 `018cf6c020b4b63d9552d73f5b7fc6eb984effa1`，保留在本地功能分支 `feat/lab-comparison-optimization`。
测试数据全部合成，没有读取用户原始检验报告。没有为真实指标预置缺方法可比规则。

## 需求与验证对应

| 要求 | 实现及可重复证据 |
| --- | --- |
| LC-01 名称、代码及别名搜索、无结果和日期错误 | `tests/labs/test_comparison_optimization.py` 的身份历史、类别和日期用例；浏览器 Enter 提交与历史导航 |
| LC-02 同指标归并，方法／单位／质量不拆主行 | 同文件 `test_display_identity_is_independent_of_method_unit_and_value_quality` |
| LC-02 不混计数／百分比、血液／尿液／未知标本、未映射候选 | 同文件嗜碱性粒细胞及候选项目用例；保留独立身份和全部值 |
| LC-02 同报告多值、同日多份、更正及解析切换 | 新重复值用例及 `tests/labs/test_phase_two_comparison.py`、`test_phase_two_workflows.py`、`test_advanced_trends.py` |
| LC-03 紧凑结果、可靠单位集中、异常信息仍可达 | 单位范围变化用例；浏览器点击数值、现有依据页、返回焦点及患者范围 |
| LC-04 历史点、可比系列、计算资格分离 | 新历史点与方法规则用例；原有变化算法、同日断线和换算来源回归 |
| LC-04 方法规则审核发布链路 | `tests/labs/test_phase_two_dictionary_workflow.py::test_missing_method_rule_survives_review_publication_and_effective_reads`，涵盖不完整规则拒绝、发布后读取、机构和方法边界 |
| LC-05 日期、机构、重复报告编号及长名称 | 新机构证据与同文档多日期／多机构分列用例；报告计数不重复；浏览器超长名称展开；报告页显示完整关联机构 |
| LC-06 多选并集、日期／关键词交集、旧链接、清空和患者保持 | 新类别用例及浏览器多选、Enter、刷新和前进后退 |
| LC-07 共用表头、固定首列、分组折叠、一个主要纵向滚动面 | `tests/browser/test_lab_comparison_browser.py`、`test_lab_comparison_performance.py`；100 行、50 报告、5,000 个结果在顶部、中部及末尾核对列位置 |
| LC-08 箭头方向、边界、比较符、定性、标记／范围冲突 | 新参数化异常用例；方法缺失不单独阻断；一般异常标记不伪装为方向依据 |
| 通用 当前有效修订、只读、撤权、删除及无跨患者数据 | 检验既有工作流、来源授权、排序及高级趋势回归；每次读视图重建，不新增数据库表 |

## 浏览器和性能方法

- 本地 Windows，Intel Core i5-1135G7（4 核、8 逻辑处理器），约 16 GB 内存。
- Chromium 152.0.7977.83，无头模式；SQLite 隔离测试库及本地 Django 服务。
- 桌面 1280×800、200% 缩放等效的 640×400 CSS 像素／DPR 2、手机 360×800。
- 原版基线为 `8cf6caca77ef7c0b02d8700b51a7b97b72bdaee5`，在独立工作区运行相同合成夹具。
- 每个视口重复两次从带完整筛选参数的导航至加载并经过两次绘制帧的耗时；单独测量折叠响应。
- 不截断结果，不因性能隐藏报告。检查全部 5,000 个结果、100 行、50 报告列实际存在。
- 浏览器图像只含合成值，位于 `docs/verification/artifacts/lab-comparison/`。

性能制品：[原版](artifacts/lab-comparison-performance-baseline.json)、
[本分支](artifacts/lab-comparison-performance.json)。最终浏览器验收为 **6 passed（84.27 秒，无跳过）**，
覆盖三个新交互用例、一个大矩阵用例及两个原有高级趋势用例。

| 视口 | 原版两次加载（秒） | 本分支两次加载（秒） | 本分支折叠（毫秒） |
| --- | --- | --- | --- |
| 桌面 1280×800 | 30.314 / 31.061 | 6.337 / 5.459 | 83.1 |
| 200% 等效 640×400，DPR 2 | 33.109 / 37.334 | 5.787 / 5.433 | 175.6 |
| 手机 360×800 | 30.902 / 30.828 | 5.432 / 5.358 | 80.9 |

三个视口分别按两次平均耗时比较，降低约 81%、84%、83%。这是同机合成数据的少量测量，
不是生产百分位延迟。规格建议的 **2 秒目标尚未达到**；本次不设置新门槛或宣称该目标已通过。
批量读取报告、日期及机构，复用单次请求内的字典和已校验结果，降低了重复查询和对象构造；
现有个性化排序依赖的实时有效性核查仍保留。

截图已核对：[桌面矩阵](artifacts/lab-comparison/comparison-matrix-1280.png)、
[200% 等效矩阵](artifacts/lab-comparison/comparison-matrix-640.png)、
[手机矩阵](artifacts/lab-comparison/comparison-matrix-360.png)。

可重复浏览器命令（`run_required_tests.py` 会把跳过作为失败）：

```powershell
$env:PHR_TREND_BROWSER_ARTIFACT_DIR='docs/verification/artifacts/lab-comparison'
$env:PHR_COMPARISON_PERFORMANCE_OUTPUT='docs/verification/artifacts/lab-comparison-performance.json'
python tools/run_required_tests.py tests/browser/test_lab_comparison_browser.py tests/browser/test_lab_comparison_performance.py tests/browser/test_advanced_trends_browser.py -q --tb=short
```

在基线工作区复制同一个性能测试文件，设置 `PHR_COMPARISON_BASELINE=1`，输出到独立基线
JSON，再单独执行该测试，得到原版测量。新旧测量均未与其他回归测试并行运行。

## 回归与静态检查

完整相关回归 **1,918 passed（1,708.89 秒，无跳过）**，
[JUnit 制品](artifacts/lab-comparison-regression.xml)记录每项结果。
此后仅补齐返回地址容错、旧修订与趋势来源的患者参数，并增加同文档分列断言；
这些末轮改动由针对性回归 **107 passed（43.96 秒，无跳过）** 覆盖，
[末轮 JUnit 制品](artifacts/lab-comparison-final-regression.xml)记录每项结果。
最终浏览器复验 **5 passed（48.63 秒，无跳过）**；矩阵数据读取、布局及性能路径未改动，
沿用上述 6 项浏览器验收中的大矩阵与性能证据。

```powershell
python -m pytest tests/labs tests/cancer_ordering tests/analytics tests/documents tests/exports tests/treatments tests/patients tests/accessibility/test_detail_trend_markup.py -q --tb=short --junitxml=docs/verification/artifacts/lab-comparison-regression.xml
python -m pytest tests/labs/test_comparison_optimization.py tests/labs/test_phase_two_comparison.py tests/labs/test_advanced_trends.py tests/labs/test_trends.py tests/labs/test_phase_two_views.py tests/accessibility/test_detail_trend_markup.py -q --tb=short --junitxml=docs/verification/artifacts/lab-comparison-final-regression.xml
python tools/run_required_tests.py tests/browser/test_lab_comparison_browser.py tests/browser/test_advanced_trends_browser.py -q --tb=short
```

`npm run test:js`：9 passed；`node --check static/js/lab-comparison.js`：通过。
Django 系统检查无问题，迁移检查无变更，`git diff --check` 通过。
`python tools/verify_documentation.py` 通过：130 份已登记文档。
本轮数据库回归使用 SQLite；未运行 PostgreSQL 实例或生产环境验证。

## 边界

本验收不说明真实报告提取准确率、临床可互换性或生产放行。用户举例报告的实际拆行原因
未经原始报告复核，不能用合成样例推断。未发布版本，不修改 Release Please 管理的版本字段。
