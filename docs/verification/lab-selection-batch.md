# 检验对比选择与报告批量确认验证

日期：2026-09-26；后续变更记录至 2026-09-28。状态：当前目录的 27 项名称同步已完成必要范围验证、独审及 `focused` / `release-focused` 发布流程，已随 [v2.2.3](../releases/v2.2.3.md) 发布；本地运行服务已更新，未部署生产。2026-09-26 原验收及 2026-09-27 确认语义变更的完整 submit、[v2.2.2](../releases/v2.2.2.md) 发布事实分别保留，当前状态以[登记表](../document-registry.json)为准。

本记录对应[需求规格](../specs/2026-09-26-lab-comparison-selection-and-batch-confirmation.md)及[实施计划](../plans/2026-09-26-lab-selection-batch.md)。测试仅使用合成患者、报告和原图，不代表生产部署或真实医疗数据质量验收。2026-09-26 原验收时的功能分支为 `feat/lab-selection-batch`，基线为 `cdecda3`，本地实现提交为 `08e7095`；当时发布版本尚未确定。本次确认语义后续变更的验证和发布另见下文 2026-09-27 记录。

## 2026-09-26 原验收覆盖

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
当时已保存工作表 SHA-256 为 `fc5965e7812fed70409180a0a6b23a02f90953f9bebe5e3f453b7a29ed8ea202`，
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
当时另一任务持有仓库提交会话锁，本任务未绕过锁，该次调用未进入 submit 完整环境验证、未创建 PR、未合并或发布；该次调用也未更新本地服务或生产环境。这是原始锁阻塞事实，后续恢复、主动中止全量及名称同步另见下文，不继续将旧锁记录视为当前状态。

## 2026-09-27 确认语义后续变更（本地聚焦验收通过）

用户明确要求单项和批量“与原件一致”均表示标本、指标和结果已人工核实，历史已有的当前有效 `CONFIRM` 直接生效，无需迁移。相应待核对提示与人工质量核对限制应解除；计算必需信息仍实际缺失时明确说明缺项，不补造字段或任意放开计算前提。撤销确认恢复原核对状态；更正及重解析存在内容冲突时不误用旧确认，批量继续跳过识别有误、修订冲突和报告归属冲突。

本节对应修订后的 AC-09 及新增 AC-11～AC-14，功能分支为 `fix/lab-confirmation-quality`，实现提交为 `1ae02d101cd80285e5f1f3a0557ed9f422fa1d0d`。上文各项通过结论、实现提交和 XML 均为原验收事实；特别是原“确认不解除质量限制”的断言不能直接作为本次验收依据。

### 覆盖与最终结果

固定源码后的[最终聚焦回归](artifacts/lab-confirmation-quality.xml)为 146 项通过、0 失败、0 跳过，耗时 180.65 秒。执行命令：

```powershell
python -m pytest tests/labs/test_confirmation_quality.py tests/labs/test_confirmation_validation.py tests/labs/test_confirmation_display.py tests/labs/test_batch_confirmation.py tests/labs/test_record_review_ux.py tests/labs/test_report_readmodels.py tests/labs/test_report_relations.py tests/labs/test_report_source_conflicts.py tests/labs/test_report_indicator_identity.py tests/browser/test_lab_comparison_browser.py tests/browser/test_lab_selection_batch_browser.py tests/browser/test_record_review_browser.py tests/labs/test_phase_two_dictionary_workflow.py::test_dictionary_mutations_recheck_authority_after_lock tests/labs/test_phase_two_release_evaluation.py::test_extra_context_extraction_failure_blocks_phase_two_release -q --tb=short --junitxml=docs/verification/artifacts/lab-confirmation-quality.xml
```

| 标准 | 本次覆盖 |
| --- | --- |
| AC-09、AC-11 | 单项、批量及历史有效确认采用同一质量语义；仍跳过识别有误、报告归属冲突和修订冲突；已确认的人工核对提示清除，未确认提示保留 |
| AC-12 | 标本、单位、日期、方法及标准指标等实际缺项说明；可计算时通过真实两日期趋势端点验证，比较符或非数值结果及不可用原参考范围仍解释具体限制 |
| AC-13 | 撤销确认恢复原质量限制，已获得的趋势资格同步撤销 |
| AC-14 | 更正须重新核实完整结果；内容变化的重解析不能继承旧确认放开计算；后续报告冲突继续可见 |

新增确认质量、校验和显示测试分别为 13、13、21 项，均包含于上述 146 项，不重复相加；相关既有报告读模型、归属、来源冲突、指标身份及三个浏览器文件一并通过。所有测试使用合成数据，不建立真实医疗数据质量或生产放行结论。

### 首轮失败、修复与独立审查

[首轮完整检验测试及三个浏览器文件](artifacts/lab-confirmation-quality-initial.xml)共 799 项：796 通过、3 失败、无跳过，耗时 512.89 秒，原始失败制品保留。其中一个浏览器旧断言要求确认后仍显示“项提示”，与本次确认语义冲突，现已改为确认后无提示、未确认仍有提示。另两个解析发布测试报告 `Parser dependencies changed during release evaluation`；首轮运行期间仍有并行源码修改，固定源码后在最终聚焦命令中复测通过。最终 146 项覆盖这三个首轮失败场景，未将首轮 799 项声明为完整通过。

独立审查发现三项问题：已关联续页时间未在确认后的读视图正确复用、比较符结果缺少剩余计算限制说明、非法原参考范围缺少不可计算说明。三项均先用失败测试复现再修复，最终聚焦回归覆盖；独立复审定向 22 项通过，给出可继续交付结论。确认仅复用已有合法时间关联，不改变报告关系，也不将比较符结果或非法参考范围变成可用计算依据。

`python manage.py check --settings=config.settings.test` 无问题；`python manage.py makemigrations --check --dry-run --settings=config.settings.test` 无变更。文档回填后执行 `python tools/verify_documentation.py`：139 份登记文档校验通过；该校验仅证明文档结构与登记合规。

上述 146 项是本次合并前的聚焦验收，完整 `submit` 及发布结果单独记录如下；不以新一轮通过覆盖首轮失败或扩大原聚焦验收范围。

### 完整 submit 与 v2.2.2 发布

以下事实仅依据[匿名发布摘要](artifacts/release-v2-2-2.json)，远端发布状态回读时间为 `2026-09-27T14:18:05.163806+00:00`。

| 阶段 | 精确验证 head / 实际 Squash | 结果 |
| --- | --- | --- |
| [功能 PR #106](https://github.com/skuyd/emr/pull/106) | `230ec4e1082200ac7b211389c9c1b09661a9d6de` / `de0a3d5bd275968933ce02042ac322d9eb259da3` | 完整验证通过，`reused=false`；Python 5522 通过、5 跳过，PostgreSQL 407、浏览器 25、JavaScript 9 通过；契约、Django、合成语料、生产镜像构建及 smoke 均通过 |
| [发布 PR #107](https://github.com/skuyd/emr/pull/107) | `a132d51ec24c5f7c879497dd43f78034b0622b68` / `99e07c0629bd9249f44a302a2d307ce386df5c49` | 136 项发布专项通过，`business_reused=true`；业务回归复用功能候选结果；契约、合成语料、生产镜像构建及 smoke 均通过 |

功能候选完整验证对应主线基线 `2295fb36303b6c784b7b564fc3ae9c7455db354d`，发布候选对应功能 Squash `de0a3d5bd275968933ce02042ac322d9eb259da3`。两阶段的验证回执、结果及制品 SHA-256 均见匿名摘要，完整本地 submit 私有状态和日志未上传。

Python 的 5 个跳过均为 Windows 专用用例：4 个 PowerShell 本地启动器用例和 1 个 Windows WSL 参数边界用例；跳过不计通过。发布阶段复用了业务回归，不声称重跑 Python、PostgreSQL、浏览器和 JavaScript；阶段集合与原 146 项聚焦测试均不能相加。

功能 PR 于 `2026-09-27T14:13:34Z` 合并，发布 PR 于 `2026-09-27T14:16:04Z` 合并。[GitHub Release v2.2.2](https://github.com/skuyd/emr/releases/tag/v2.2.2) 于 `2026-09-27T14:16:30Z` 发布，非草稿、非预发布。没有执行生产部署，生产门禁仍为 `BLOCKED`；详见[版本清单](../releases/v2.2.2.md)。


## 2026-09-27 同步主线后的目录回归

为本地预览保留已发布的确认规则功能，目录修复分支同步了当时最新 `origin/main`（`99e07c0`），不重写原分支提交。比较模块的确认语义自动合并，三处文档冲突保留双方需求、实现和历史证据；内置目录与已验收修订版完全一致，仍为 22 类、201 项及用户确认的新版范围。

[合并后核心回归](artifacts/lab-catalog-refresh-main-merge.xml)：目录、目录投影、分类选择、比较优化、确认质量、确认校验及确认展示共 249 passed、无跳过，耗时 82.57 秒。执行命令：

```powershell
python -m pytest tests/labs/test_indicator_catalog.py tests/labs/test_catalog_projection.py tests/labs/test_comparison_selection.py tests/labs/test_comparison_optimization.py tests/labs/test_confirmation_quality.py tests/labs/test_confirmation_validation.py tests/labs/test_confirmation_display.py -q --tb=short --junitxml=docs/verification/artifacts/lab-catalog-refresh-main-merge.xml
```

该回归针对合并后的交互范围，不重复计入此前 804 项回归，也不替代本地 submit 完整环境验证。主线同步与测试本身不更新运行服务、不改动业务数据库，也不代表目录修复已合并到远端或发布。

## 2026-09-27 本地开发服务预览

用户继续反馈本地页面未变化。核对 8000 端口进程的工作目录与代码后，确认其仍运行主工作区 `2295fb3` 的旧目录：25 分类，来源哈希为原版 `f03e39f…`。修复位于独立 worktree，刷新网页和保存 Excel 均不会切换运行代码；本地 submit 也不负责更新该开发服务。

将干净主工作区以 detached HEAD 加载已验收的 `12d9769` 快照，保留 `main` 分支指针与功能分支，重启本地 Web 及处理 Worker。沿用原运行环境和数据目录，`.env` 哈希前后一致，未执行迁移或初始化账号。服务 `/health/live/` 返回 200，Web 与 Worker 进程均存活；读取运行目录的内置 JSON 为 22 分类、201 指标，胃蛋白酶原Ⅰ/Ⅱ范围为 70–160 / 5–60。

对用户指定页面对应的读取模型进行更新前后核对，现显示“血常规”和合并后的“肝功”，CRP 归入“炎症三项”，指标行数保留。核对在最终回滚的事务内执行，未保存派生写入；患者标识、检测值和页面内容未写入公开证据。当前没有可连接的浏览器，以上是服务及读取模型验证，不宣称用户浏览器会话的截图验收。

当时本地更新已生效，用户刷新原页面即可加载；当时正式目录修复的 submit 锁仍由另一任务持有，远端合并与发布待恢复。此处保留预览时点事实，本地预览不是生产部署，也不证明 2026-09-28 新名称已在运行服务生效。

## 2026-09-28 名称同步与必要范围验证

再次核对本地原表时发现来源哈希发生变化，先向用户说明差异并暂停依赖该版本的发布记录；用户随后明确确认本次同步当前 Excel 的 27 项名称。当前确认的 SHA-256 为 `699f659730683792de0f98dc27a05c76fe75980f3b76adf0024497812154b006`。

当前工作表仍为 22 类、201 源条目。与前一来源 `fc5965e7812fed70409180a0a6b23a02f90953f9bebe5e3f453b7a29ed8ea202` 的 A–F 对比，分类、顺序、单位、条件和范围完全相同，只有 27 项名称变化：B4“白细胞”、B172“载脂蛋白A”、B174“载脂蛋白A/B”及其余名称去空格。历史“白细胞计数”、AI、AI/B 和带空格别名继续识别为原指标；本轮不修改原始 Excel，不读取 G 列或患者数据列。

### 验证范围与原结果边界

- [本轮来源审计](artifacts/lab-catalog-names-source-audit.json)通过：当前哈希的 A–F 生成结果与内置目录完全一致，22 类、201 项中恰有 27 项名称变化；201 项的编码、分类、样本、单位、来源行及全部条件范围与 `d245df2` 均一致。539 组旧名称/别名在原样本和分类上下文中仍匹配原编码。此前 536 组旧来源审计保持原制品，不覆盖或累加。
- `bb1fdf0` 对应上文 804 项检验及关联回归、12 项桌面/手机浏览器和独立审查；`12d9769` 对应同步主线后的 249 项核心回归。各轮结果保持原快照归属，集合有重叠，不相加。
- `12d9769` 至 `d245df2` 仅改变 10 个文档文件，业务源码相同，可保留适用的已有回归作为相关基线。随后新增的 27 项名称变化由下述本轮必要专项验证，不直接套用旧结果称为新名称已通过。
- 本次曾对 `d245df2` 启动唯一一轮 full 验证；用户明确要求“必须测试，不要全量”后已主动停止。停止前 Python 日志有 3084 个通过标记，未见 F/E；没有完整终态结果，不能记为 Python 全量通过。该轮 PostgreSQL 407 项已通过，但整轮 full 未完成、不是通过，也不将已通过组改写为失败。
- 后续只执行本次名称、旧别名、目录及相关页面的必要范围，不重新启动全量，不修改仓库通用验证策略。本次 submit 已使用 `focused` / `release-focused` 验证，公开证据按实际模式与范围记录，不表述为“完整 submit 通过”。既有 `v2.2.2` 的 full 结果属于另一次已完成任务，不受本次主动中止影响。

### 新名称专项结果

- [修改前复现](artifacts/lab-catalog-names-red.xml)：4 项失败，覆盖当前来源哈希及白细胞、载脂蛋白 A、载脂蛋白 A/B 的新旧名称和稳定编码。
- [首轮必要专项](artifacts/lab-catalog-names-initial.xml)：83 项中 82 通过、1 失败，无错误和跳过；包括 74 个目录用例、3 个新旧名历史用例、2 个选择用例、3 个趋势精确用例及 1 个手机选择浏览器用例。
- 唯一失败是测试误要求未变更的 B189“免疫球蛋白 IgG”也去空格。当前 Excel 仍保留该名称；应用代码及 JSON 原名保持不变，仅修正该测试期待。
- [五参数组复测](artifacts/lab-catalog-names-green.xml)：5 项通过，无失败、错误和跳过，覆盖原唯一失败。新增 expected 参数使 XML 节点名增加后缀，但仍对应原 name/abbreviation 的五个场景。其余 82 项的首轮通过证据保留，83 个目标用例均有通过证据；5 项复测与首轮有重叠，不相加，不表述为一次 83 项全绿。
- 独立审查确认业务数据与别名隔离无阻断；发现 `tests/documents/test_trend_index.py` 的两处旧展示名称断言后，仅更新相应期待并复测其 4 个用例。[页面名称复测](artifacts/lab-catalog-names-page-labels.xml)全部通过、无跳过，覆盖 `test_trend_index_lists_only_eligible_current_patient_summaries` 及 `test_ordered_pages_open_with_adjacent_ocr_section_boxes` 的三个页面参数。该 4 项与前述 83 个目标用例不同，本轮共 87 个目标用例分别有通过证据。

实际命令如下；首轮最初输出为 `lab-catalog-names-green.xml`，结束后改名为 `lab-catalog-names-initial.xml` 保留原结果，再由复测写入 green 文件。各轮使用本地 Python 3.11，不执行全仓回归。

```powershell
python -m pytest tests/labs/test_indicator_catalog.py tests/labs/test_catalog_projection.py::test_renamed_catalog_items_keep_old_and_new_report_names_in_one_history tests/labs/test_comparison_selection.py::test_page_uses_catalog_order_and_only_current_patient_history tests/labs/test_comparison_selection.py::test_selection_filters_intersect_dates_and_alias_search_without_losing_options tests/labs/test_trends.py::test_trend_summaries_include_only_current_patients_eligible_codes tests/labs/test_trends.py::test_trend_summaries_order_codes_by_newest_observation_then_name tests/labs/test_trends.py::test_eligible_trend_preserves_raw_values_and_each_point_links_to_evidence tests/browser/test_lab_selection_batch_browser.py::TestLabSelectionBatchBrowser::test_phone_selection_filter_empty_and_return -q --tb=short --junitxml=docs/verification/artifacts/lab-catalog-names-green.xml
python -m pytest tests/labs/test_indicator_catalog.py::test_source_printed_immunology_abbreviations_match_same_indicator -q --tb=short --junitxml=docs/verification/artifacts/lab-catalog-names-green.xml
python -m pytest tests/documents/test_trend_index.py::test_trend_index_lists_only_eligible_current_patient_summaries tests/documents/test_trend_index.py::test_ordered_pages_open_with_adjacent_ocr_section_boxes -q --tb=short --junitxml=docs/verification/artifacts/lab-catalog-names-page-labels.xml
```

### 本轮交付状态

本轮使用 `fix/lab-catalog-alignment` 原分支，27 项名称同步已完成必要范围的本地验证，状态为 `verified`，依据为上述 87 个目标用例分别通过的证据和 539 组旧别名来源审计。独立审查结论及页面断言修正结果已分别记录；实现 `e83478b` 已经 PR #109 合并并随 v2.2.3 发布，未部署生产环境。

### focused submit 与 v2.2.3 发布

以下发布事实依据[匿名发布摘要](artifacts/release-v2-2-3.json)，远端回读核验时间为 `2026-09-28T04:21:53.166183+00:00`。

| 阶段 | 精确验证 head / 实际 Squash | 结果 |
| --- | --- | --- |
| [功能 PR #109](https://github.com/skuyd/emr/pull/109) | `e83478b88f70222589a8954771b56b9e8ec43bf2` / `6de82a2ae045f1fe901dd42eae5d9b4d2758a448` | `focused` 通过；校验现有必要范围证据、文档和版本，未重跑 pytest |
| [发布 PR #110](https://github.com/skuyd/emr/pull/110) | `cbf871b90af6a4bc03ec034da72f4291b71eb62c` / `fa91bdb5e0a541c9aa6973ca08d8690971c740aa` | `release-focused` 通过，`business_reused=true`；非版本业务源码与功能候选一致，未重跑 pytest |

功能 PR 于 `2026-09-28T04:19:30Z` 合并，发布 PR 于 `2026-09-28T04:20:55Z` 合并。[GitHub Release v2.2.3](https://github.com/skuyd/emr/releases/tag/v2.2.3) 于 `2026-09-28T04:21:16Z` 发布，非草稿、非预发布；标签指向发布 Squash `fa91bdb5e0a541c9aa6973ca08d8690971c740aa`。

本次遵照用户“必须测试，不要跑全量测试”的明确要求。当前业务源码证据为上述 87 个目标场景的组合验证，804/12/249 仅是较早快照的历史补充；原环境指纹未记录，不声称与本次环境等价。初轮 83 项并非使用最终测试文件完整重跑，应用代码在三轮间保持不变，仅修正 IgG 和页面名称测试期待后复测对应场景。已提交首轮 XML 只去除行尾空白，原件在本地保留，节点与结果已核对未变。

两阶段回执、结果及制品哈希见匿名摘要，`full_gate_passed=false`。先前 full 的 Python 因主动停止以 143 退出；PostgreSQL 407 项通过，浏览器、JavaScript、合成语料和生产镜像构建及 smoke 未执行，整轮 full 未通过。focused 发布不覆盖该原始结论，也不代表生产放行；生产门禁仍为 `BLOCKED`，详见[版本清单](../releases/v2.2.3.md)。

## 2026-09-28 本地运行更新

本地运行工作区同步了 `e83478b88f70222589a8954771b56b9e8ec43bf2` 的 9 个本次非文档文件；原有其他任务的 7 个文档文件字节哈希全部保留，未切换分支，未修改 `.env`，未执行迁移或持久化业务数据变更。处理队列空闲后，以隐藏后台方式重启 Web 和 Worker；健康检查返回 200，运行工作区非文档源码与该提交一致。

在最终回滚的事务内核对指定患者的读取模型，确认“白细胞”新名称、“血常规”及“肝功”分类和旧 AI 别名映射正确，历史结果行数保留。本段仅记录本地服务与读取模型验证，不包含患者标识、检测内容或具体行数；没有真实浏览器截图，不据此声称浏览器会话验收。新名称已在本地运行服务生效，本次未部署生产环境。

## 2026-09-28 印刷名称与分类匹配修复

用户反馈 Excel 中没有的“血常规/其他”分类仍出现在检验对比页。投影匹配先前只用报告原名称直接查目录；带序号、星号及连写英文缩写的已知名称未命中后进入 `OTHER`，与正常名称的历史展示合并时形成混合分类，部分指标则被拆成两行。

本次仅修复目录投影的名称匹配：先保留原文精确匹配；未命中时采用既有名称规范化规则清理印刷标记，保留名称中的结果文本（`strip_result=False`）；若名称包含缩写和中文名称，只有全部片段各自匹配并指向同一标准编码时才采用该指标。全过程仍传入原标本和报告分类，不用部分已知片段覆盖未知名称或冲突身份，既有人工标准编码覆盖仍优先。原始名称、结果、单位、标本与来源不改写，确认状态及计算前提继续检查。

- [修改前复现](artifacts/lab-catalog-printed-names-red.xml)：7 项中 3 失败、4 通过，无错误和跳过。失败分别复现单核细胞百分比的“血常规 / OTHER”、ALT 的“肝功 / OTHER”，以及钠的两行历史；4 个拒绝不确定匹配的场景当时已经通过。
- [修复后必要验证](artifacts/lab-catalog-printed-names-green.xml)：19 项全部通过，无失败、错误和跳过；XML 耗时 49.760 秒。覆盖三项印刷名称的单行历史、表格及选择器分类、原始字段保留；未知片段、冲突缩写、无归属“颜色”、冲突标本及残留结果文本不强制匹配；带印刷标记的历史“颜色”仍依赖有效报告分类；既有别名、旧编码、可靠性与缺单位计算限制、被移除指标的“其他”展示保持不变。

实际命令如下，使用本地 Python 3.11.9 和测试内存 SQLite。首轮拒绝匹配测试当时有 4 个参数；修复后增加残留结果文本与带标记“颜色”的覆盖，后续共 19 项。

```powershell
python -m pytest tests/labs/test_catalog_projection.py::test_printed_alias_history_uses_only_its_catalog_category tests/labs/test_catalog_projection.py::test_printed_names_do_not_map_unknown_conflicting_or_ambiguous_results -q --tb=short --junitxml=docs/verification/artifacts/lab-catalog-printed-names-red.xml
python -m pytest tests/labs/test_catalog_projection.py::test_printed_alias_history_uses_only_its_catalog_category tests/labs/test_catalog_projection.py::test_printed_names_do_not_map_unknown_conflicting_or_ambiguous_results tests/labs/test_catalog_projection.py::test_alias_history_is_one_row_and_uncataloged_items_are_other tests/labs/test_catalog_projection.py::test_unknown_name_with_legacy_code_does_not_join_catalog_history tests/labs/test_catalog_projection.py::test_historical_color_uses_only_its_report_panel tests/labs/test_catalog_projection.py::test_standardization_keeps_result_reliability_gate tests/labs/test_catalog_projection.py::test_missing_percentage_unit_preserves_review_and_calculation_gates tests/labs/test_catalog_projection.py::test_removed_catalog_results_remain_visible_as_other -q --tb=short --junitxml=docs/verification/artifacts/lab-catalog-printed-names-green.xml
```

独立代码审查通过。本轮只执行上述必要测试，未运行全量；首轮 7 项与后续 19 项有重叠，不相加。测试使用合成数据，不包含真实患者内容。首轮 XML 仅去除行尾空白，原件在本地保留，节点与结果经核对一致。该修复的提交与发布尚未在本节核验，不以此前 v2.2.3 发布代替本轮交付证据。

本地运行仅同步 `apps/labs/catalog_projection.py`，在处理队列空闲后重启原 Web 与 Worker；健康检查返回 200。对指定患者的对比读取模型核对，混合分类已消失；更新前后全部原始检验记录、展示来源集合及 `.env` 的哈希分别一致，数据库检查在最终回滚的事务内执行。上述为服务和读取模型验证，没有浏览器截图，不声称实际浏览器会话验收；公开记录不包含患者标识、结果值或私有运行证据。
