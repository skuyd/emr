# 第二阶段验证记录

本记录对应[第二阶段规格](../specs/2026-09-06-phase-two-requirements.md)和
[实施计划](../plans/2026-09-06-phase-two-implementation.md)。开发时分支为
`feat/phase-two-trust`，基线为 `a8fe8bb`，实现提交为 `4656d74f447b8891df4eb9cee64956af765fbd3e`；
代码已随 [v1.1.0](../releases/v1.1.0.md) 发布，功能 PR 为 [#16](https://github.com/skuyd/emr/pull/16)。
登记状态见[文档登记表](../document-registry.json)。

## 当前验证状态

P2-01 至 P2-09 已实现，P2-AC01 至 P2-AC11 已按规格第 5.1、5.2 节完成
功能验收和现有示例回归。真实质量改进目标尚未达到，具体差距及受限范围见下文。
以下记录开发阶段的验证结果；后续合并及自动发布见[交付证据](artifacts/phase-two-delivery-report.json)。最终检查见
[验证制品](artifacts/phase-two-verification-report.json)，各轮执行及修复历史见
[执行记录](artifacts/phase-two-worklog.json)。

## 需求与证据

| 验收 | 实现与验证范围 | 可重复执行的测试及证据 |
| --- | --- | --- |
| P2-AC01 | 原始清单逐项对应 125 个目标编码；转铁蛋白去重关系、钙与校正钙拆分关系明确，50 个基因不计入检验覆盖 | [清单](../../apps/labs/dictionaries/phase-two-coverage.json)、[字典测试](../../tests/labs/test_phase_two_dictionary.py)、[固定合成回归](artifacts/phase-two-synthetic-gate-report.json) |
| P2-AC02 | 同行双栏、重排表头、多表、续表与缺值分别处理；字段可定位，不唯一的关联保留原因并降级 | [版面测试](../../tests/labs/test_phase_two_layout.py)、[持久化测试](../../tests/processing/test_phase_two_pipeline.py) |
| P2-AC03 | 保留原值和来源；数值、比较符、定性、半定量、状态分别建模，推测修复只存为候选 | [提取测试](../../tests/labs/test_extraction.py)、[固定语料](../../apps/labs/dictionaries/phase-two-regression.json) |
| P2-AC04 | 类型、单位、来源、日期及显式审核的历史/内部关系规则；可靠正常控制与故障注入同时执行 | [校验测试](../../tests/labs/test_phase_two_validation.py)、[门禁测试](../../tests/labs/test_phase_two_regression.py) |
| P2-AC05 | 可选确认、反馈、暂缓、更正与撤销；确认不能跳过单位、日期或来源限制 | [工作流测试](../../tests/labs/test_phase_two_workflows.py)、[界面测试](../../tests/labs/test_phase_two_views.py) |
| P2-AC06 | 精确复核任务授权、撤销、过期、版本与并发约束；原件流式响应再次授权 | [PostgreSQL 测试](../../tests/integration/test_phase_two_postgres_concurrency.py)、[运行报告](artifacts/phase-two-postgres-report.json)、[界面复审](artifacts/phase-two-task-5-rereview.json) |
| P2-AC07 | 候选不直接改变映射；审核、差异预览、固定回归、版本快照与回退均有记录；发布前核对预览及当前版本 | [发布测试](../../tests/labs/test_phase_two_dictionary_workflow.py)、[运维测试](../../tests/operations/test_services.py)、[字典复审](artifacts/phase-two-task-4-context-rereview.json)、[发布评测复审](artifacts/phase-two-release-evaluation-review.json) |
| P2-AC08 | 指标为行、报告日期为列；同日多报告、重复结果及特殊值保留；满足项目/标本/方法/单位/来源依据时才比较或换算 | [对比测试](../../tests/labs/test_phase_two_comparison.py)、[趋势测试](../../tests/labs/test_trends.py) |
| P2-AC09 | 只解释明确的报告参考范围；缺失、冲突、数值不可计算或来源不可靠时显示无法对照 | [参考校验测试](../../tests/labs/test_phase_two_validation.py)、[上下文界面测试](../../tests/labs/test_phase_two_views.py) |
| P2-AC10 | 原始解析、用户修订、复核结论与字段来源分别保存；新解析与保留字段的差异显式核对，旧质量问题不丢失 | [版本与撤销测试](../../tests/labs/test_phase_two_workflows.py)、[有效字段校验](../../tests/labs/test_phase_two_validation.py)、[展示一致性测试](../../tests/labs/test_phase_two_comparison.py) |
| P2-AC11 | 冻结文件、OCR、独立标注、基线与解析依赖；失败与未评测范围明确，统计结果可复现 | [评估工具](../../tools/phase_two_evaluation.py)、[评估统计测试](../../tests/tools/test_phase_two_evaluation.py)、[统计复审](artifacts/phase-two-evaluation-rereview.json)、[真实评测](artifacts/phase-two-real-evaluation.json)、[重复执行比较](artifacts/phase-two-reproducibility-report.json) |

P2-01 至 P2-09 分别由上述清单、版面、校验、可选核对、授权复核、字典发布、
纵向对比、固定评测和修订连续性覆盖。源码仍沿用私有存储及原有删除契约。

## 固定示例及标注范围

- 全部 64 个文件固定纳入：20 个 PDF、34 个 JPG、10 个 PNG；PDF 共 80 页，加图片共 124 个渲染页。
- 按内容归为 60 个报告组，包含同报告多页、重拍和图片/PDF 表示。它们来自两个匿名患者组，五个已识别机构组及一个机构未识别的报告组。
- 40 个文件、42 个检验报告组具有标注，共 734 个源文件行表示。重复来源不充当独立报告；其余 23 个非目标文档和一个无目标字典行的文件保留分类与排除原因。
- 两轮直接查看原件的字段标注共发现 34 行分歧，已对照原件裁定；第三次来源核对覆盖 12 个名称分歧及 30 个字段框。见[裁定报告](artifacts/phase-two-annotation-adjudication-report.json)。
- 第二轮可见已有的分类及行数信息，未读取第一轮字段正文或解析预测。这是开发回归集，没有留出集，不代表独立泛化评估。
- 独立矩形只覆盖五行的 30 个字段，其他字段使用页面回退；页面回退不算精确定位成功。

原始文件、医疗字段标注及预测保存在被 Git 忽略的本地目录。公开制品仅含匿名计数、
版本、哈希和方法说明。[基线清单](artifacts/phase-two-baseline-manifest.json)冻结了修改前
解析器的 64 份预测及 OCR 输入；基线与新结果使用同一份裁定标注评分。

## 评测复现

发布、回退及解析器 CI 使用同一个公开评测入口。字典候选先合并已审核规则，再执行
完整快照评测；字典、规则、固定语料及代码指纹绑定到报告。每次发布或回退追加
`DictionaryEvaluationEvent`，不覆盖原发布记录；耗时保存为执行证据，不参与预览哈希。

固定合成回归无需真实资料：

```powershell
$env:PYTHONUTF8='1'
python tools/phase_two_evaluation.py --synthetic-only
```

可使用 `--dictionary`、`--rules`、`--baseline-report` 指定完整快照及冻结基线，
使用 `--report` 指定输出文件。没有覆盖到的规则标为 `not_evaluated`；实际适用或触发
只证明规则被执行，不证明阈值具有医学有效性。

公开历史基线由干净的 `a8fe8bb` 源码实际重放同一语料生成，冻结工具为
[freeze_phase_two_baseline.py](../../tools/freeze_phase_two_baseline.py)。基线不调用当前
质量或趋势代码，缺失字段保持缺失，下游能力保持未评测。两次执行的全部确定性字段
一致，基线指标哈希为 `0254b8c2cb07162aa76e9b7c5ddcaebefd95ecf1a994c15879807015f129cf51`。

精确的旧版字典仍可回退，仅接受冻结 v1 的 LF、CRLF 两个字节摘要，并同时校验
相同定义摘要；其他字节、改动的定义或伪装的版本均不能取得旧版豁免。新评测 JSON
通过 Git 属性固定为 LF，生成基线也明确写入 LF。旧版沿用原有
160 个解析用例的门禁，完整新语料的退化及未覆盖范围仍显示；不能据此声称旧版支持
第二阶段全部 125 个目标。任何新字典版本均须通过完整的新门禁及冻结基线召回检查。

真实评测需要本机授权范围内的私有源文件映射、OCR 缓存、裁定标注、分类及基线预测：

```powershell
python tools/phase_two_evaluation.py --source-map .runtime/phase-two-evaluation/source-map.json --ocr-cache-dir <固定OCR缓存目录> --annotations .runtime/phase-two-evaluation/source-annotations-adjudicated.json --classification .runtime/phase-two-evaluation/source-classification-frozen.json --baseline-predictions .runtime/phase-two-evaluation/baseline-predictions.json
```

工具校验源文件、OCR 与标注哈希，在独立内存数据库中执行实际持久化、有效结果解析、
质量校验和可比性计算。报告区分全量、成功子集、核心/扩展覆盖与未评测范围。
已知但未完整标注的混合报告范围不得被计为正确或错误；没有可信严重字段的条目不进入
严重错误分母。耗时属于固定 OCR 后的解析与读模型过程，不能解释为完整 OCR 耗时。

## 运行检查和质量限制

| 检查 | 实际结果 |
| --- | --- |
| 全量 Python | 1404 通过，38 跳过，0 失败；364.34 秒 |
| PostgreSQL 18.6 | 本期及处理并发 23 通过、原有兼容性 12 通过，均零跳过 |
| JavaScript | 6 通过，0 失败，0 跳过 |
| Django 配置与迁移 | `check` 无问题，`makemigrations --check --dry-run` 无变更 |
| 文档登记与链接 | 51 份文档校验通过 |
| 发布自动化与版本 | 开发阶段校验通过（当时版本 1.0.1）；已发布版本见 [v1.1.0 清单](../releases/v1.1.0.md) |
| 原需求与生产门禁记录 | 60 verified / 2 external_pending；生产门禁 BLOCKED，8 通过 / 15 待验证 |

全量 Python 的 38 个跳过项中，35 个数据库用例随后在真实 PostgreSQL 中独立通过，
见[本期并发报告](artifacts/phase-two-postgres-report.json)和
[兼容性报告](artifacts/phase-two-postgres-compatibility-report.json)。剩余一项为未启用的
真实 OCR 模型测试，两项为 Windows 无符号链接权限；这些不计为通过。43 条警告来自
本机 Requests 依赖组合及测试覆盖数据库配置，详情保留在验证制品中。私有 PostgreSQL
测试实例已停止，其他工作区未被修改。

真实示例中，64 个文件均完成处理，47 个产生结构化条目、17 个仅保留原件，没有执行
失败或遗漏的文件；全量分母始终保留这 64 个文件。具有独立标注的 40 个文件均产生了
条目，因此本轮成功子集与全量的已标注字段计数相同。这不表示每一行均被正确识别。

| 真实集联合转录匹配 | 正确 TP | 错配/额外 FP | 未匹配 FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 冻结历史基线 | 7 | 375 | 628 | 1.83% | 1.10% | 1.38% |
| 本期全量 | 268 | 1639 | 367 | 14.05% | 42.20% | 21.09% |
| 核心集合 | 243 | 442 | 294 | 35.47% | 45.25% | 39.77% |
| 扩展集合 | 19 | 24 | 23 | 44.19% | 45.24% | 44.71% |

联合匹配要求项目、类型、值和单位均正确，734 个源行表示中有 635 行具备完整的联合
评分依据；未知字段不补成正确答案。各字段分别使用自身可评测分母，八项关键字段的
召回均未低于同一裁定标注下的历史基线。FN 同时包含漏抽和已经抽取但未联合匹配的行，
不能全部解释为没有抽取。核心/扩展归属在评分前固定；1261 个预测行无法归入目标
层级，仍保留在全量统计中。125 个目标中有 95 个具备真实标注，其余 30 个保持真实
质量未评测。完整字段计数、成功子集和覆盖清单见[真实评测报告](artifacts/phase-two-real-evaluation.json)。
字典覆盖清单保留建表时的冻结状态；当前真实覆盖以评测报告的 `target_coverage` 为准。

核心和扩展 F1 均未达到规格中的 95% / 90% 优化目标。1991 个有可信评分依据的预测
中，保守计数的项目、类型、值或单位错误为 1674 个，全部进入限制路径，未有已知错误
作为可靠结果放行；另外 34 个预测缺少完整标注，不能算作正确。该错误率为
1674/1991（84.08%），并未达到严重转录错误率低于 0.5% 的优化目标。这里统计所有已
核实转录差异，包括已拦截条目，不等同于临床风险判定或可靠结果中的错误率。

正确转录的 268 行中仍有 262 行（97.76%）因日期、标本、方法或来源依据不足而受限，
所以“全部错误被拦截”不能单独证明规则体验良好。2025 个预测中仅 3 个（0.15%）
满足当前趋势条件。受限结果保留原件和解释，可通过可选更正或授权复核补齐依据；
不能仅通过点击确认跳过限制。

双栏报告的严格整组匹配为 0/14，未达到 95% 优化目标；该统计也将漏行和 OCR 值
差异计为整组失败，不能据此单独推断几何跨栏错误率。明确参考范围的完整匹配为
551/711（77.50%），未达到 95% 目标。精确来源位置为 20/30（66.67%），仅适用于
独立核对的五行字段框；其他页面回退不计精确成功。

公开合成评测共 556 个固定输入、564 个标注行：本次 556 个输入成功，联合转录匹配
564 正确、0 错配、0 漏抽；98 个可靠转录对照无误路由或缺失，9 个已知错误场景全部
进入限制路径。实际下游可进入趋势的记录为 63/564；其余包括特殊结果及依据不足的
记录。[公开评测报告](artifacts/phase-two-release-evaluation.json)两次运行除耗时外完全一致，
指标哈希为 `893f9bf8946c25f6f909d96a9f419805a32e6d2b3a3f01bf7f2eb542e86d41fb`。
这些结果只适用于公开合成语料；其日期和方法来自独立冻结的上下文，不测量原件元数据
识别、OCR 性能或数据库持久化。真实资料的实际流水线评测另行报告。

最终真实回归运行两次，全部确定性字段及预测摘要一致，门禁均通过；指标哈希为
`da71663eda627eaebfb5375d9f0bc87a577ba753f49141367eb3623553172538`。
固定 OCR 后的解析、持久化和对比总耗时分别为 170.40 秒、142.43 秒，包含并行验证时
的机器负载，不作为完整 OCR 或生产响应性能结论。三组重复执行比较及各自代码、规则
和数据身份见[复现报告](artifacts/phase-two-reproducibility-report.json)。

独立实际浏览器流程已经执行资料持有者和复核员两个会话，覆盖更正、撤销、授权、
开始复核、多字段更正、撤权后拒绝访问以及嵌入原件；桌面和 390 像素窄屏未发现页面
横向溢出。范围见[界面运行报告](artifacts/phase-two-task-5-report.json)。

对比与趋势通过统一有效结果读取并重新校验，尚未对大规模历史数据的查询量和响应
时间做性能基准；真实评测记录的是固定 OCR 后的解析、持久化及对比耗时。同一上传
文档内的不同已知日期可以分列；同日多个检查尚无额外报告身份时，重复值保留在格内。

本期只确认已覆盖的功能与开发回归结果。没有真实样本的指标仍为未评测，合成正反例
不替代真实准确率。第一阶段的外部浏览器、正式环境及其他放行验证继续独立记录在
[生产门禁](release-gate.md)，本期证据不改变生产放行状态。
