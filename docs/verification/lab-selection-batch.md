# 检验对比选择与报告批量确认验证

日期：2026-09-26。状态：本次功能及直接关联回归本地验证通过，当前状态以[登记表](../document-registry.json)为准。

本记录对应[需求规格](../specs/2026-09-26-lab-comparison-selection-and-batch-confirmation.md)及[实施计划](../plans/2026-09-26-lab-selection-batch.md)。测试仅使用合成患者、报告和原图，不代表生产部署或真实医疗数据质量验收。功能分支为 `feat/lab-selection-batch`，基线为 `cdecda3`，本地实现提交为 `08e7095`；发布版本尚未确定。

## 验收覆盖

| 标准 | 实现及可重复验证入口 | 当前证据 |
| --- | --- | --- |
| AC-01 | `test_comparison_selection.py::test_page_uses_catalog_order_and_only_current_patient_history`；目录顺序、已有项目、其他末尾；个性化排序不再影响本页 | comparison-green XML |
| AC-02 | 同文件跨分类同义身份/同名不同检测对象测试；`TestLabSelectionBatchBrowser` 整类和部分选中、半选 | comparison-green、browser-feature-final XML |
| AC-03 | 新浏览器选择流程分别折叠树节点、表格分类并检查选择/结果 | browser-feature-final XML |
| AC-04 | 默认全选、显式空/非法键空态、重新选择、无空分类 | comparison-green、browser-feature-final XML |
| AC-05 | 日期/关键词相交；数值详情返回还原 GET 选择及筛选条件 | comparison-green、browser-feature-final XML |
| AC-06 | 未核对、已核对、错误、修订/报告冲突的单来源隐藏；数值与提示保留 | comparison-green、source-conflict-green XML |
| AC-07 | 既有 `TestLabReportConsolidationBrowser` 键盘/手机展开多个来源并检查采样时间、原结果、原图链接 | browser-final XML |
| AC-08 | `test_batch_confirmation.py` 完整报告重复来源、续页、单/多份选择及等值未选报告隔离 | backend-regression、browser-feature-final XML |
| AC-09 | 同文件混合状态、继承冲突、单份部分跳过、缺单位及低置信度、值/元数据不变 | backend-regression、browser-feature-final XML |
| AC-10 | 权限、CSRF、签名范围、重复提交、结果/报告/关系/重解析/删除变化、逐项审计；真实数据库并发测试 | review-final、postgres-final、postgres-preview-race-green XML |

测试路径：`tests/labs/test_comparison_selection.py`、`tests/labs/test_batch_confirmation.py`、`tests/browser/test_lab_selection_batch_browser.py`、`tests/integration/test_lab_batch_confirmation_postgres.py`。表中 XML 均位于本目录的 `artifacts/`，文件名前缀为 `lab-selection-batch-`。

## 已执行的验证

- 最新主线基线：`python -m pytest tests/labs/test_comparison_optimization.py tests/labs/test_report_comparison.py -q`，76 通过。
- R1/R2：`python -m pytest tests/labs/test_comparison_selection.py tests/labs/test_comparison_optimization.py tests/labs/test_report_comparison.py tests/labs/test_catalog_projection.py tests/labs/test_phase_two_comparison.py tests/cancer_ordering/test_lab_ordering.py -q`，相关回归 143 通过，见 [comparison-green](artifacts/lab-selection-batch-comparison-green.xml)。补充三个真实报告级冲突提示场景后，选择/报告修订/报告对比聚焦回归 30 通过，见 [source-conflict-green](artifacts/lab-selection-batch-source-conflict-green.xml)。
- R3：新增批量确认及既有报告/修订相关回归 114 通过，见 [backend-regression](artifacts/lab-selection-batch-backend-regression.xml)。
- 新增真实浏览器验收：`python -m pytest tests/browser/test_lab_selection_batch_browser.py -q`，桌面 1280px、手机 360px 共 4 通过，见 [browser-feature-final](artifacts/lab-selection-batch-browser-feature-final.xml)。最终连同既有对比、归并、癌种排序及指标目录浏览器测试共 18 通过，见 [browser-final](artifacts/lab-selection-batch-browser-final.xml)。
- PostgreSQL 18.6 隔离合成数据库首轮新增 10 项通过，见 [postgres](artifacts/lab-selection-batch-postgres.xml)；补充权限等待及既有报告/家庭权限/排序回归后 36 通过，见 [postgres-final](artifacts/lab-selection-batch-postgres-final.xml)。独立审查修复后 12 个批量确认、8 个报告、5 个家庭权限场景共 25 通过，见 [postgres-preview-race-green](artifacts/lab-selection-batch-postgres-preview-race-green.xml)。所有数据只在本机临时测试库中。
- 独立审查修复后的 `test_batch_confirmation.py`、`test_comparison_selection.py`、`test_report_relations.py`、`test_report_revision_versions.py` 联合回归 65 通过，见 [review-final](artifacts/lab-selection-batch-review-final.xml)。
- 报告分组变化的直接消费者补充回归：`python -m pytest tests/exports/test_lab_report_projection.py -q --tb=short --junitxml=docs/verification/artifacts/lab-selection-batch-export-regression.xml`，15 通过、无跳过，覆盖冻结快照失效、续页选择边界、等值折叠及导出/分享重投影，见 [export-regression](artifacts/lab-selection-batch-export-regression.xml)。
- [首轮远端 CI](https://github.com/skuyd/emr/actions/runs/36247512149)（提交 `9093c64`）：完整 PostgreSQL 回归 407 通过；Python 回归 5,354 通过、4 跳过、1 个历史分享迁移测试失败。该轮 CI 未通过，后续必跑浏览器和 JavaScript 步骤未执行。失败原因及修复见下一节；最终远端结论以 PR 检查为准。
- 修正历史迁移目标后：`python -m pytest tests/patients/test_sharing_migration.py tests/patients/test_family_migration.py -q --tb=short --junitxml=docs/verification/artifacts/lab-selection-batch-migration-ci-green.xml`，2 通过、无跳过，见 [migration-ci-green](artifacts/lab-selection-batch-migration-ci-green.xml)。
- `npm run test:js`：9 通过；`python manage.py check --settings=config.settings.test`：无问题；`python manage.py makemigrations --check --dry-run --settings=config.settings.test`：无遗漏。
- `python tools/verify_documentation.py`：137 份文档校验通过。
- 额外全仓回归命令为 `python -m pytest -q -m 'not postgres and not ocr_model' --ignore=tests/browser --tb=short --junitxml=docs/verification/artifacts/lab-selection-batch-regression.xml`，选中 5,275 项。执行到约 16% 后主动中止，以本次变更的直接影响范围完成验证；停止前未观察到失败，未生成完整 XML，不作为通过证据。没有修改测试配置或跳过本次功能验收。

上述测试集合存在重叠，不相加作为独立测试总数。

## 原始失败与复验

保留原始失败制品，不用通过结果覆盖：

- [comparison-red](artifacts/lab-selection-batch-comparison-red.xml)：新增选择/排序/单来源测试 11 失败，既有多来源能力 1 通过；实现后 [initial-green](artifacts/lab-selection-batch-comparison-initial-green.xml) 为 12 通过。
- [comparison-regression-initial](artifacts/lab-selection-batch-comparison-regression-initial.xml)：142 通过、1 个旧排序响应断言失败；仅对比页断言按本次规格调整，最终 143 通过。
- [backend-red](artifacts/lab-selection-batch-backend-red.xml)：初始 18 失败；实现后 [backend-green](artifacts/lab-selection-batch-backend-green.xml) 为 18 通过。
- [backend-expanded-red](artifacts/lab-selection-batch-backend-expanded-red.xml)：22 通过、等待锁期间账户停用场景 1 失败；新增最终权限复核后联合回归通过。
- [browser-red](artifacts/lab-selection-batch-browser-red.xml)：旧页面目录顺序失败；[confirmation-browser-red](artifacts/lab-selection-batch-confirmation-browser-red.xml) 与 [browser-diagnostic](artifacts/lab-selection-batch-browser-diagnostic.xml) 保留接入期间的失败。首轮批量测试入口标签与实现不一致，后统一按实际可访问名称操作。
- [browser-initial](artifacts/lab-selection-batch-browser-initial.xml)：12 个既有浏览器场景通过、4 个新增场景失败；[browser-feature](artifacts/lab-selection-batch-browser-feature.xml) 为 2 通过、2 失败。筛选夹具的表外项目误用了 `LAB_WBC`，关键词因此合法匹配两行；修正为独立表外代码后新增 4 项通过。
- 全 PostgreSQL 标记回归曾启动，出现失败后中止以定位；未完成，不能声称全 PostgreSQL 回归通过。已确认的首失败是对比页个性化排序旧断言，见 [postgres-ordering-red](artifacts/lab-selection-batch-postgres-ordering-red.xml)（8 通过、1 失败）；相关断言修正后 36 项关联回归通过。
- [source-conflict-red](artifacts/lab-selection-batch-source-conflict-red.xml)：3 个单来源报告级冲突提示遗漏失败；修复后聚焦 30 项通过。
- 独立审查发现重复报告预览凭证可重复计数。新增三种状态测试先 [3 失败](artifacts/lab-selection-batch-review-duplicate-red.xml)，按完整报告身份拒绝重复范围后 [26 通过](artifacts/lab-selection-batch-review-duplicate-green.xml)。
- 独立审查发现预览期间解除关联冲突可造成页面跳过、提交确认的不一致；[单元失败记录](artifacts/lab-selection-batch-preview-consistency-red.xml)和[真实数据库并发失败记录](artifacts/lab-selection-batch-postgres-preview-race-red.xml)均保存。指纹加入实际展示的项目状态与原因后，旧混合预览提交返回 409，不写入确认或回执；上述 65 项及 25 项回归均通过。
- 远端 CI 暴露历史分享迁移测试将患者回退至 `0004`，却保留依赖新版患者迁移的检验和事实记录最新节点，导致历史模型与实际表结构不一致；[本地复现](artifacts/lab-selection-batch-migration-ci-red.xml)同样因缺少 `birth_date` 列失败。试用迁移执行器返回状态仍有缓存字段残留，[该次复验](artifacts/lab-selection-batch-migration-state-red.xml)为 1 失败、1 通过，改动已撤回。最终只将测试中的 `labs` 固定为 `0004_alter_labobservation_raw_value_and_more`、`facts` 固定为 `0004_molecular_context_anchors`；独立依赖闭包审计确认目标一致，保留原数据、审计断言和最终恢复步骤，未修改应用迁移。两个患者迁移回归均通过。

## 独立审查

审查范围包含相对 `cdecda3` 的所有新增和修改代码、模板、迁移与测试。审查者独立复现重复报告令牌计数、预览关联变化两个 P2；修复后原复现的三个定向测试通过，无其他重要发现。新增回执的作者删除 `SET_NULL` 和患者删除 `CASCADE` 也经独立合成验证通过。

修复后的独立完成审计逐项核对 R1～R3 和 AC-01～AC-10，检查实际测试用例身份及 XML 结果，未发现需求实现或必要测试缺口。AC-01 依靠已导入目录的原表哈希/行号追踪、实际排序代码和代表性顺序断言，本轮未逐格重读 Excel。AC-06 的详情入口与原图可加载由不同浏览器场景共同覆盖，并未对每种核对状态重复加载原图。缺日期限制由既有 `test_confirmation_does_not_clear_unit_or_date_restrictions` 与批量入口复用相同的空更改确认调用证明；缺单位和低置信度另有批量入口直接测试。

保留的原始质量码注入另行核查：`report_identity_conflict` 和 `revision_conflict` 由当前校验器动态生成，没有找到将它们作为原始质量问题持久化的当前或历史正式生产路径。因此没有把普通字段关联疑问扩大为批量跳过条件；实际报告和修订冲突依据仍逐项检查。

## 图像与交付边界

浏览器图像位于 `artifacts/lab-selection-batch-browser/`，包含桌面/手机选择、来源和报告确认页面。所有截图仅含合成数据。

独立审查、AC-01～AC-10、功能及直接关联回归验收已通过，规格和计划标记为本地 `verified`。额外本地全仓回归未完成，不作为通过证据；合并及远端完整 CI 的实际状态见 [PR #101](https://github.com/skuyd/emr/pull/101)。源代码交付不代表生产部署放行。

## 2026-09-27 目录核对与血常规名称修正

用户要求“血常规”不带“急诊”，并反馈指标及分类未按原表。此次只读重读本地原表的
“数据收集”工作表，SHA-256 仍为 `f03e39f39af3774eb86460d443e292ef85072bbf391ba3625cde53a9744d49e7`。
25 个分类、208 个源条目的名称、分组、单位、参考范围及顺序与内置目录一致；逐条以源名称和分组匹配均成功。
因此没有发现源目录导入遗漏；没有具体页面例子时，不据此推断用户看到差异的原因。
既有“只列当前患者有记录的指标”规则继续适用。

“血常规（急诊）”来自原表 A4，原实现直接用于页面标签；另以“血常规”作为报告类别时，
跨分类 CRP 未能匹配。修正仅增加显示名称和类别名称兼容：选择器、普通及组合分类标题、
参考日期附加分类显示“血常规”，原目录身份、报告原文、顺序及参考范围保留。

- 改动前基线：目录与选择回归 70 passed。
- [失败复现](artifacts/lab-blood-count-label-red.xml)：新增页面标签与 CRP 类别匹配测试 2 failed。
- [相关回归](artifacts/lab-blood-count-label-green.xml)：目录、选择、标准投影及对比优化 182 passed；命令为
  `python -m pytest tests/labs/test_indicator_catalog.py tests/labs/test_comparison_selection.py tests/labs/test_catalog_projection.py tests/labs/test_comparison_optimization.py -q`。
- [浏览器验收](artifacts/lab-blood-count-label-browser.xml)：桌面 1280 px、手机 360 px 的分类选择、清空、筛选和详情返回共 2 passed、2 deselected（未选择同文件报告批量确认场景）。
- 独立代码审查未发现重要问题；补充跨分类参考日期名称断言后，[聚焦复验](artifacts/lab-blood-count-label-review.xml) 1 passed。
- `python tools/verify_documentation.py` 通过，139 份登记文档；`git diff --check` 通过。

本节记录针对当时原表的核对及名称修正验证。随后用户确认本地页面使用的原表有误并重整表格；新版目录更新见下节，原验证事实不覆盖新版验收。

## 2026-09-27 修订表格同步

用户已确认新版范围取代旧规格，B217/B218 是不同指标，B225/B226 的范围不调换。
当前已保存工作表 SHA-256 为 `fc5965e7812fed70409180a0a6b23a02f90953f9bebe5e3f453b7a29ed8ea202`，
包含 22 类、201 条指标。此前本地页面读取内置目录，修改本地 Excel 不会自动改变该目录。

导入工具改读 A–F，其中 D/E/F 为条件、下限、上限；不读取 G 列辅助公式或患者数据列。
111 条移动源行保留原编码，不按新行号重新生成身份。分类和顺序按新版表格，CRP、肌酐及
胃蛋白酶原Ⅰ/Ⅱ采用新版唯一分组；胃蛋白酶原Ⅰ/Ⅱ为 70–160 / 5–60 ng/mL。
旧报告类别、原值、原参考信息和来源保留；退出目录的比值及筛查评分仍在“其他”中显示。
泌乳素 50 岁、男性碱性磷酸酶 15 岁和磷 5 岁的既有确认边界继续适用。

- [来源核对](artifacts/lab-catalog-refresh-source-audit.json)：201 个指标均保留正确旧编码，536 组规范化的旧别名/编码/标本关系保留；生成器重建结果与内置 JSON 一致。
- 独立审查逐项核对源表 A–F、分类、单位和全部条件范围，与内置 JSON 一致；未发现错配或误用新行号的编码。
- [修改前核心复现](artifacts/lab-catalog-refresh-red.xml)：24 failed、47 passed；旧分类、旧分组范围和移动源行断言失败。
- [修改后核心回归](artifacts/lab-catalog-refresh-core-green.xml)：71 passed，包含固定编码、类别、范围、年龄边界及两个退出项目不误映射。
- [完整检验及关联回归](artifacts/lab-catalog-refresh-regression.xml)：`python -m pytest tests/labs tests/exports/test_lab_report_projection.py tests/cancer_ordering/test_lab_ordering.py -q --tb=short`，804 passed、无跳过，耗时 580.72 秒；与核心回归有重叠，不相加计数。
- [浏览器验收](artifacts/lab-catalog-refresh-browser.xml)：`python tools/run_required_tests.py tests/browser/test_lab_catalog_browser.py tests/browser/test_lab_comparison_browser.py tests/browser/test_lab_selection_batch_browser.py -q --tb=short`，12 passed、无跳过；覆盖桌面和手机。
- 已查看[桌面选择器](artifacts/lab-catalog-refresh-browser/selection-tree-1280.png)及[手机选择器](artifacts/lab-catalog-refresh-browser/selection-tree-360.png)，分类显示和筛选操作符合新版目录。
- 全分支独立代码审查未发现需修复的重要问题；额外比较 201 指标 × 104 年龄 × 3 性别 × 5 阶段的 313,560 个上下文，各指标与对应旧标准定义的参考范围行为没有意外差异，行号移动未误伤原有边界规则。
- `python tools/verify_documentation.py` 通过，139 份登记文档；`git diff --check` 通过；中文 Conventional Commit 标题检查通过（fix / PATCH）。

原表 G40 的 `#REF!`、C206/C207 空单位、A229 名为“7项”但实际 8 项仍保留并已向用户报告，
不自行猜补或修改原 Excel。本次实现提交为 `bb1fdf0`，规格及验收记录提交为 `7e72388`，本地验收状态为 `verified`。

已按本地流程调用 `python tools/submit.py --title "fix(labs): 按修订表同步检验指标目录与分类" --body-file <本地UTF-8正文文件>`。
工具退出码为 1，返回 `Submit stopped: another submit holds this repository's session lock`。
另一任务仍持有仓库提交会话锁，本任务未绕过锁、未进入 submit 完整环境验证、未创建 PR、未合并或发布；正在运行的本地服务及生产环境均未由本次任务更新。锁释放后应从原分支恢复 submit，不重新生成或猜测目录。
