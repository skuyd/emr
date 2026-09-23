# 固定检验指标目录与患者适用参考范围实施计划

**Goal:** 完整实现 [需求规格](../specs/2026-09-22-lab-indicator-catalog-and-reference-ranges.md) 的 IC-01 至 IC-15。

**Architecture:** 保留不可变的报告、识别结果与修订记录；增加独立的固定目录和按患者、采样日期、结果阶段计算的标准展示层。患者资料修改后在读取时重新计算历史适用范围，不批量覆盖原报告字段。目录匹配不参与报告合并或结果删除。

**Tech Stack:** Django、Python Decimal、现有 Django 模板、pytest、Playwright。

执行方式：在独立功能分支直接实施，使用 executing-plans 和 TDD；最后进行独立代码审查。计划、进度和证据均按仓库文档治理规则存放。

## 全局约束

- 来源工作表为“数据收集”，SHA-256 为 `f03e39f39af3774eb86460d443e292ef85072bbf391ba3625cde53a9744d49e7`；25 分组、208 原始条目。
- 用户明确规则优先于表格。其余单位、条件和范围不自行修正或扩大人群。
- 缺少适用范围时保留结果，隐藏范围及缺失提示；不沿用原报告高低标记。
- 保留患者权限、来源可靠性、结果核对限制、报告身份与修订链。
- 不改自动版本字段，不带入本地部署资料或患者样本。
- 基线为 `origin/main` 的 `327e9ac`；主工作区未提交需求文档保持原状。

## 审查重点

1. 同名不同标本、百分比与绝对值不能误合并。
2. 单位未知、OCR 纠正与真正换算的区别，原值不得被伪装成标准值。
3. 历史采样日、生日边界、资料更正与缺失条件，不采用当前年龄。
4. 阶段列表不能当成患者阶段；人工阶段修订与重解析保留来源。
5. 对比、详情、趋势和选定输出的语义一致，低可信来源不得绕过质量门禁。

## Task 1：患者完整资料

涉及 `apps/patients/{models,forms,services,profile,views,family_views,profile_urls}.py`、患者模板、迁移与 `tests/patients/test_demographics.py`。

接口：`Patient.sex`、`Patient.birth_date`；通过现有权限约束的资料编辑服务保存，原记录保持未知而不填默认性别或生日。

- [x] 先写日期无效、未来日期、非法性别、权限拒绝及正确日期保存测试。
- [x] 运行 `python -m pytest tests/patients/test_demographics.py -q`，确认新增行为缺失导致失败。
- [x] 添加字段、迁移、校验及新建/补填页面；首次建档和新增家人必填，旧患者可暂空。
- [x] 运行新测试和患者模块回归，验证旧患者不被填入猜测值。

```python
assert form.is_valid() is False  # birth_date="2026-02-30"
assert patient.birth_date.isoformat() == "2001-09-22"
```

## Task 2：固定目录、单位和参考规则

新增 `apps/labs/catalog.py`、`apps/labs/dictionaries/indicator-catalog.json`、目录测试及来源转换工具。

接口：目录精确匹配返回唯一指标或空；范围选择接收性别、完整出生日期、采样日期和单次检测阶段；标准化返回原值、标准值、标准单位、适用范围及可计算状态。

- [x] 先写来源全量覆盖、ALT 合并、颜色标本分离、定性范围及年龄边界测试。
- [x] 在实现前运行目录测试，记录失败。
- [x] 按来源哈希生成可审查数据，保留行号和原范围。按已确认规则保留重复项目的报告分组范围。
- [x] 使用 Decimal 实现明确的同维度单位换算、范围选择；无法证实的换算保持待核对。
- [x] 运行目录测试，逐项覆盖 IC-02 至 IC-10、IC-13、IC-15。

```python
assert age_on(date(2020, 9, 22), date(2026, 9, 21)) == 5
assert age_on(date(2020, 9, 22), date(2026, 9, 22)) == 6
```

## Task 3：新旧结果、阶段和页面

涉及 `apps/labs/{extraction,revisions,readmodels,comparison,comparison_policy,validation,views}.py`、处理持久化、检验模板、输出和相应测试。

接口：使用 Task 2 的固定目录解释有效结果，保留 raw 字段；阶段为每条结果的可追溯属性，支持明确提取和人工修订。

- [x] 先写新上传及历史记录的标准解释、不同阶段的逐格展示与原报告追溯测试。
- [x] 在实现前运行测试，确认缺失行为。
- [x] 接入读取与新提取；只明确标注的本次阶段可自动采用，列出多个阶段不选档。
- [x] 接入单次阶段编辑及现有不可变修订、权限和并发检查。
- [x] 对比表使用固定分类和同义历史行；每格显示自己的阶段、标准范围与换算值。
- [x] 详情保留原名称、原值、原单位、原范围和阶段证据；无范围不显示缺失占位。
- [x] 运行检验、处理、输出及权限回归，检查趋势计算仍受已有可靠性限制。

## Task 4：验收与交付审查

- [x] 建立 IC-01 至 IC-15 的测试追踪，补齐每个验收项实际证据。
- [x] 运行 CI 通用范围的 `python -m pytest`、受影响模块复测及 `npm run test:js`；环境限制、失败与后续复测分别记录，不宣称全部环境门禁通过。
- [x] 运行 Django 系统检查、迁移遗漏检查和浏览器验收；核对窄屏对比表、补填资料与结果阶段编辑。
- [x] 进行独立全分支审查，先以测试复现实际缺陷再修复。
- [x] 更新文档登记、索引与验证证据；版本未知保持空。
- [x] 运行 `python tools/verify_documentation.py`，全部验收证据完整后才报告功能完成。

## 执行记录

- 2026-09-22：已核对 Excel 哈希和全部原始行。发现额外同义范围冲突，已请求用户明确；在答复前不确定这些项目的标准范围。
- 2026-09-22：独立 worktree 已从最新远端 main 建立。患者与字典基线测试 190 项通过。
- 2026-09-22：已实现患者补填和校验、固定目录和适用范围、单位换算、逐结果阶段修订、对比详情及输出投影。新建患者必填策略，以及肌酐、胃蛋白酶原和 CRP 的重复定义仍等待用户确认，不据此选择标准。
- 2026-09-22：首次检验模块回归为 523 项通过、67 项失败，保留[原始结果](../verification/artifacts/lab-catalog-labs-progress.xml)。其中无患者对象的字典发布评估兼容修复后[56 项通过](../verification/artifacts/lab-catalog-dictionary-progress.xml)；不能据此宣称整个模块已通过。
- 2026-09-22：历史首次读取阶段及带界限值换算先复现 4 项失败，修复后[相关 61 项通过](../verification/artifacts/lab-catalog-historical-progress.xml)。目录外名称不再因旧编码混入目录内历史，先复现后修复，[展示投影 12 项通过](../verification/artifacts/lab-catalog-projection-progress.xml)。
- 2026-09-22：四种来源定性值（淡黄色、黄色、清晰、软便）的提取先复现失败，修复后目录及提取测试 39 项通过。全量回归、旧报告持久化单元的阶段补读、趋势入口一致性和浏览器验收仍需继续，交付状态保持 implementing。
- 2026-09-22：旧报告单元阶段补读使用原件区域内的 OCR 证据，未改写报告身份；相关[81 项通过](../verification/artifacts/lab-catalog-phase-read-progress.xml)。趋势历史按目录别名归组，保留原计算限制；标准范围单位单独进入输出，[趋势及输出 29 项通过](../verification/artifacts/lab-catalog-trend-output-progress.xml)。
- 2026-09-22：JavaScript 9 项通过；Django 系统检查通过、迁移检查无遗漏。全检验模块第二轮及患者/导出模块仍在执行；浏览器首轮未通过，原因是测试定位器未匹配带冒号的表单标签，正在复测，不作为验收通过证据。
- 2026-09-23：第二轮[全检验模块 606 项通过](../verification/artifacts/lab-catalog-labs-second-progress.xml)。随后补齐颜色的检验分类上下文、PCT 消歧和旧报告范围内的分类读取，[相关 117 项通过](../verification/artifacts/lab-catalog-context-progress.xml)；未把该后续改动计入前一轮全量证据。
- 2026-09-23：旧报告重试因新增阶段字段触发原始证据冲突，先复现后仅兼容旧快照缺失的新字段，原身份仍不改写，[48 项通过](../verification/artifacts/lab-catalog-retry-progress.xml)。处理流水线与迁移回归[10 项通过](../verification/artifacts/lab-catalog-processing-progress.xml)。
- 2026-09-23：浏览器新流程[1 项通过](../verification/artifacts/lab-catalog-browser-recheck.xml)，覆盖 1280/360 宽度的资料补填与阶段保存；既有对比和趋势浏览器[首轮 5 通过、2 失败](../verification/artifacts/lab-catalog-browser-regression.xml)，两项旧分类/范围断言调整后[2 项通过](../verification/artifacts/lab-catalog-browser-regression-recheck.xml)。
- 2026-09-23：资料详情摘要曾仍显示未换算值，先复现后接入同一标准展示层，展开区保留原报告，[50 项通过](../verification/artifacts/lab-catalog-document-detail-progress.xml)。导出参考信息专项[15 项通过](../verification/artifacts/lab-catalog-export-reference-progress.xml)。以上测试集合有重叠，不相加统计。
- 2026-09-23：只读成员的阶段编辑表单先复现可见问题，随后隐藏表单并验证 POST 拒绝及无修订写入，[阶段与权限 22 项通过](../verification/artifacts/lab-catalog-phase-access-progress.xml)。目录从原 Excel 再生成后与当前 JSON 的 SHA-256 同为 `a8a462ba82f585be56dc44cb144ba89379c7a9dae9a9ae0d98eb2fd7f8d95287`。
- 2026-09-23：[导出及患者大范围回归](../verification/artifacts/lab-catalog-output-patients-progress.xml)结束：514 通过、2 失败、39 未选入，耗时 1850.80 秒。两条失败为旧报告范围显示契约，已经调整并在上述导出参考信息 15 项专项回归中通过；39 条为 `-k 'not onboarding'` 排除的新建流程测试，其中包括待确认策略相关的新测试。未执行整个仓库的 `python -m pytest`。
- 2026-09-23：目录此前每次匹配均重复规范化全部别名，改为每个目录对象建立一次索引；同机冷启动后 1000 次 WBC 精确匹配从 4.552 秒降为 0.009 秒。该测量仅代表匹配函数，不作为整个回归耗时的原因或性能结论。
- 2026-09-23：索引调整后，[目录、展示投影与导出参考信息 71 项通过](../verification/artifacts/lab-catalog-final-independent-progress.xml)；Django 系统与迁移检查再次通过，当前没有仍在运行的测试进程。

- 2026-09-23 独立审查已完成，发现缺单位比例误换算、旧编码使表外项目混入趋势，以及五个来源缩写遗漏；已先复现再修复，[目录、投影、趋势和癌种排序 94 项通过](../verification/artifacts/lab-catalog-review-green.xml)。资料详情入口随后先复现链接缺少表外身份，再补充按原名称筛选。[相关页面及浏览器回归 120 项通过](../verification/artifacts/lab-catalog-review-browser-regression.xml)，涵盖桌面/手机癌种排序、对比和高级趋势；测试集合有重叠，不相加统计。
- 补跑此前被筛选排除的既有新建患者流程：[35 项通过](../verification/artifacts/lab-catalog-existing-onboarding.xml)。癌种排序首轮 [12 通过、4 失败](../verification/artifacts/lab-catalog-ordering-regression.xml)，其中旧类别和标准名称断言已按本规格调整，复测包含在上述 94 项及 120 项结果中。
- 发布自动化、版本一致性、历史需求追踪及发布门禁校验器通过；生产门禁仍为 BLOCKED（8 passed、15 pending），不能解释为生产可发布。来源缩写补齐后目录 JSON SHA-256 为 `e1a445a2847b959ef288c4e659f8e6310091e9acdce5710ce866187043bac48f`，原始 Excel 未修改。

- 2026-09-23：Django 系统检查通过，迁移检查无遗漏，差异空白检查通过。按 CI 通用回归筛选规则收集到 5,208 项测试，另有 402 项未选入；本次只执行了上述专项回归，收集成功不作为全仓库测试通过证据。

- 2026-09-23：启动与 CI 同范围的通用回归，并单独执行 CI 要求的 8 个浏览器测试文件；通用回归仅额外排除 `tests/patients/test_demographics.py::test_onboarding_rejects_invalid_demographics` 的 4 条待确认用例。通用回归尚未返回终态结果，不作为通过证据；单独的 [8 个浏览器文件共 25 项通过](../verification/artifacts/lab-catalog-required-browser.xml)，已检查 XML，无失败、错误或跳过。通用回归输出已超过初始 4%，只读调用栈确认正在执行既有癌种排序来源校验，不因输出间隔较长而重启。冻结公开合成语料的[发布评估](../verification/artifacts/lab-catalog-release-evaluation.json)已通过，125 项目标均被执行、554 条预期观察值全部保留；该评估限定既有提取契约，不评估真实报告准确率。当前命令环境未找到 Docker、Podman 或 psql，未执行容器构建或 PostgreSQL 门禁。

- 2026-09-23：通用回归仍在执行，约 15% 时出现趋势首页用例失败；已[单独复现](../verification/artifacts/lab-catalog-trend-index-red.xml)。该用例给 `LAB_SINGLE` 设置了“单次指标”的标准名称，却保留测试工厂默认原名“白细胞”，与本规格按原始别名归组的行为冲突；随后仅修正该已执行用例的合成原始名称，应用代码保持不变，[趋势首页整个文件 9 项通过](../verification/artifacts/lab-catalog-trend-index-green.xml)。仍在执行的完整回归保留原始失败记录，不因专项通过而覆盖或重写本轮全量结果。额外核对采样时间按原报告本地时间存入 JSON、读取时直接恢复，不经过数据库 UTC 日期转换；WSL Ubuntu 中也未找到 Docker 或 PostgreSQL 工具。

- 2026-09-23：补充患者性别和完整生日更正后的导出依赖验证，[2 项通过](../verification/artifacts/lab-catalog-demographic-snapshot.xml)。旧标准范围快照被 `SnapshotChanged` 拒绝，新快照采用更正后的适用范围，原报告数值、单位、范围及旧快照本身均保持原样；未修改应用代码。这 2 条新测试不包含在此前已收集、仍在执行的通用回归中。

- 2026-09-23：[通用回归最终结果](../verification/artifacts/lab-catalog-ci-regression.xml)为 5,200 通过、2 失败、2 跳过、406 未选入，耗时 7,464.37 秒。两条失败分别为趋势首页的合成名称不一致和治疗旧迁移测试模型状态与数据库不一致；前者专项 9 项通过，后者先[单独复现](../verification/artifacts/lab-catalog-treatment-migration-red.xml)，修正测试状态后[迁移及患者资料专项 8 项通过、4 条新建用例未选入](../verification/artifacts/lab-catalog-migration-green.xml)。应用代码在本轮通用回归启动后未改变；不将原始失败记录改写为全绿结果。
- 2026-09-23：两项跳过均为文档治理工具的符号链接越界测试，原因是当前 Windows 账号缺少创建符号链接权限。PostgreSQL、真实 OCR 模型及生产容器未执行。本轮没有仍在运行的验证进程。
- 2026-09-23：[完成状态核对](../verification/artifacts/lab-catalog-completion-audit.json)确认新建表单仍没有性别和出生日期字段，四组重复指标仍保留未定的多套候选定义。两项业务问题已跨多轮请求确认，尚无答复；其余已确认范围的独立实现、审查修复和当前环境可执行验证已完成，实现尚未提交，登记表保持 implementing；任务因等待上述业务确认而受阻。文档规范要求 blocked 关联真实提交或 PR，此处不以基线提交代替尚未提交的实现。

- 2026-09-23：用户确认新建患者性别与完整生日必填、旧患者可暂空；肌酐、胃蛋白酶原Ⅰ/Ⅱ和 CRP 保留分组差异并按报告类别选择范围。已补新建两个入口、目录变体和报告上下文测试，先复现缺失行为后实现；不再存在上述业务决策阻塞。

- 2026-09-23：补充规则的[患者及相关流程 233 项通过](../verification/artifacts/lab-catalog-patient-decisions.xml)。分组范围与类别提取先[复现 13 项失败](../verification/artifacts/lab-catalog-group-decisions-red.xml)，同日相同数值的跨分组归并另[复现 1 项失败](../verification/artifacts/lab-catalog-group-fold-red.xml)；修复后[目录、投影、归并与输出 136 项通过](../verification/artifacts/lab-catalog-decisions-focused.xml)。两套分组不再互相覆盖参考范围；同义名称仍保留一行历史。
- 2026-09-23：受影响浏览器[首轮 13 通过、1 失败](../verification/artifacts/lab-catalog-decisions-browser-initial.xml)，新增用例遗漏标签冒号；修正后[1 通过、1 失败](../verification/artifacts/lab-catalog-decisions-browser-recheck.xml)，原因是测试在 Playwright 上下文内同步查询 Django。仅调整测试定位器及查询位置，随后[目录浏览器 2 项通过](../verification/artifacts/lab-catalog-decisions-browser-final.xml)，涵盖桌面和手机新建、旧患者补填及逐次阶段编辑。
- 2026-09-23：检验及参考输出[首轮 661 通过、1 失败](../verification/artifacts/lab-catalog-decisions-labs-initial.xml)，失败为字典发布预览返回 400；随后[该文件 20 项复测通过](../verification/artifacts/lab-catalog-decisions-dictionary-recheck.xml)，未确定首轮原因，不据此声称已修复产品缺陷。代码固定后重跑同范围检验回归，终态结果另行记录。[冻结公开合成语料评估再次通过](../verification/artifacts/lab-catalog-decisions-evaluation.json)，不代表真实 OCR 准确率。目录从未改动的原表再次生成，SHA-256 与实现同为 `19c3d45d1c7d686fcf5252a9fd9bd40b81acee4dcd62149650e88ba28a1dc8bb`。

- 2026-09-23：最终固定代码的[检验模块及参考输出 663 项全部通过](../verification/artifacts/lab-catalog-decisions-labs-final.xml)，包含此前失败的字典发布预览及新增同日跨分组归并测试；未再现首轮预览失败。结合患者及相关流程 233 项通过、专项 136 项通过及受影响浏览器 14 个不同用例最终通过，完成本地实现与验收。各集合有重叠，不相加统计；本次未再次运行整个仓库通用回归。完成状态与完整环境限制见[核对记录](../verification/artifacts/lab-catalog-completion-audit.json)。

## 当前交付状态

- 两项业务确认及其实现已完成：新建必填、旧患者可暂空；重复指标按报告类别保留分组范围。
- 本地验收结束，没有待回答的业务问题或仍在运行的验证任务。原始失败及后续通过证据分别保留，不改写历史结果。
- 实现位于 `feat/lab-indicator-catalog`，尚未提交、推送、创建 PR 或合并。无实现提交引用，因此登记表保持 implementing，不虚构 verified 或发布版本。
- Windows 符号链接权限、PostgreSQL、容器和真实 OCR 的环境验证边界仍按此前记录；本地验收不代表生产门禁已放行。

## 验收项与现有局部证据

下表只记录已执行的检查与剩余边界，不将功能交付状态提升为 verified。

| 验收项 | 已执行检查 | 剩余边界 |
| --- | --- | --- |
| IC-01 | 旧患者未知值、补填/更正、非法输入、只读拒绝、审计；浏览器补填流程 | 患者回归 233 项及新建浏览器通过；新建两个入口均必填 |
| IC-02～04 | 采样日完整周岁、生日边界、泌乳素 50/51 岁、磷 5/6 岁、男性 ALP 15/16 岁及来源年龄上限 | 无新增边界待确认 |
| IC-05 | ALT 三别名一行、常规性别范围、历史来源保留、趋势历史归组 | 分组变体、同日归并、历史、导出及最终检验回归通过 |
| IC-06 | 标本消歧、新提取面板保留、历史本报告区域的颜色分类；区域外或低可信不借用 | 通用回归及修正复测见本轮记录；补充规则的最终检验回归已通过 |
| IC-07 | 表外保留及 OTHER 分类；旧编码不导致表外名称混入标准行；异常结果保留 | 通用回归及修正复测见本轮记录；补充规则的最终检验回归已通过 |
| IC-08～10 | 原值/单位/范围保留、同量纲换算、带界限值换算、未知单位禁判、标准范围单独单位、来源质量门禁 | 通用回归及修正复测见本轮记录；补充规则的最终检验回归已通过 |
| IC-11～12 | 阶段明确声明提取、范围列表不选档、不同报告不共享、人工补充/清空/撤销、旧报告首次读取和已有单元补读、桌面/手机逐格展示 | 通用回归及修正复测见本轮记录；补充规则的最终检验回归已通过 |
| IC-13 | 缺少性别或适用范围时隐藏范围及主表缺失提示，保留结果 | 通用回归及修正复测见本轮记录；补充规则的最终检验回归已通过 |
| IC-14 | 补填/更正生日重新选择历史范围、旧记录不回写标准值、旧报告重试保持原身份、资料详情/趋势/导出读取一致；更正性别/生日使旧导出快照失效，新快照范围更新且原报告不变 | 通用回归及修正复测见本轮记录；补充规则的最终检验回归已通过 |
| IC-15 | 阴性等定性比较、颜色/透明度/性状文字提取，不转换为数值 | 通用回归及修正复测见本轮记录；补充规则的最终检验回归已通过 |
