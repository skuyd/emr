# 检验对比选择与报告批量确认验证

日期：2026-09-26。状态：验证进行中，当前状态以[登记表](../document-registry.json)为准。

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
- `npm run test:js`：9 通过；`python manage.py check --settings=config.settings.test`：无问题；`python manage.py makemigrations --check --dry-run --settings=config.settings.test`：无遗漏。
- 全部非浏览器、非 PostgreSQL、非 OCR 模型 pytest 回归正在运行，不能提前视为通过。

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

## 独立审查

审查范围包含相对 `cdecda3` 的所有新增和修改代码、模板、迁移与测试。审查者独立复现重复报告令牌计数、预览关联变化两个 P2；修复后原复现的三个定向测试通过，无其他重要发现。新增回执的作者删除 `SET_NULL` 和患者删除 `CASCADE` 也经独立合成验证通过。

保留的原始质量码注入另行核查：`report_identity_conflict` 和 `revision_conflict` 由当前校验器动态生成，没有找到将它们作为原始质量问题持久化的当前或历史正式生产路径。因此没有把普通字段关联疑问扩大为批量跳过条件；实际报告和修订冲突依据仍逐项检查。

## 图像与交付边界

浏览器图像位于 `artifacts/lab-selection-batch-browser/`，包含桌面/手机选择、来源和报告确认页面。所有截图仅含合成数据。

独立审查、功能及相关并发验收已通过，全量非浏览器回归尚未完成；规格和计划暂保持 `implementing`。本记录不宣称已合并、已发布或已部署。
