# 病理与分子字段实施计划

本计划落实[病理与分子字段设计](../specs/2026-09-08-pathology-molecular-evidence.md)，
对应[五批计划](2026-09-07-batches-one-five-implementation.md) Task 7 中的 B3-03。
具体设计合同已经独立审查通过，登记为 `active / implementing`。病理/IHC 本地实现和第三次真实执行已有
[验证记录](../verification/batch-three-pathology-ihc.md)，独立旧任务完整捕获及保真已通过非作者验收，
病理交付状态见验证记录；分子完整应用已完成实际主线整合、合成验证与有界独审，PR CI
及交付仍待完成；真实M7未启动，不属于本轮执行项，见[分子应用验证](../verification/batch-three-molecular-application.md)。
目标是交付自动候选、原件核对及选定携带的完整能力，不把设计、合成通过或旧影像发布
记为本功能完成。B3-04 排序另行完成，仍保留在[后续总计划](2026-09-08-clinical-followup-implementation.md)。

## 基线与架构

已获取远端 main，并从 `d53657a5c78343cbd4b65a3b6b2f3579ad712ae0` 创建独立
`feat/batch-three-pathology-molecular` 分支。保留其他任务工作区，不复制未合并实现。
复用 ClinicalReport、Fact FIELD、SourceEvidence、原始片段和追加修订；扩展字段类别、
标本/检测/变异上下文及报告分段。旧 EXCERPT 与已发布字段模式不改写。

实施先合入实际 main `1f59f6217cb2e29f8b4d12f70d0f586d549e1d7a`，再合入
`830f06b2dd1d745fa79e0c92cefe9b13527e769b` 的已交付血糖功能，原设计、合同和各检查点制品保全。
公共实现分两次完整功能 PR：先病理/IHC，再分子检测与报告药物依据。后者须从前者已合入
后的最新 main 创建新分支。原件 gold/协议先于新解析实现冻结；真实运行须再批准完整执行身份。

## Global Constraints

- 原件、固定 OCR、已冻结覆盖和旧金标准只读。不得使用外部 OCR 或上传医疗正文。
- 64 文件/124 页的全集身份保留；已审 27 页不代表其他 97 页阴性。
- 独立原件标注、分母协议、评分器、生产映射和执行身份均在首次真实预测前冻结。
  本阶段可建立原件 gold 和纯合成回归，不能用模型输出反向决定字段真值。
- 逐字段保留 Unicode 原偏移和原件坐标，布局坐标仅作组织；无 OCR 不能补造字符证据。
- 实际 actor、Patient/批次/Document/领域锁序、来源失效和精细输出允许清单适用于所有新字段。
- 每个模式扩展保留旧字段及届时实际 main 的所有 portable 版本、表和选择契约；当前已合 1.4。
  共享 formats 从上述实际 main 集成，不复制未合兄弟；旧完成提取不在部署时重写。
- 新行为先有有意义的失败回归，修复后跑相关检查；有真实患者材料的评测只输出匿名统计和哈希。
- 普通分支不修改自动发布版本字段。独立审查、确切 PR CI、源码发布和生产门禁分别记录。

## 执行任务

### Task 1: 独审设计与冻结字段、实体合同

依赖：本次原件覆盖和设计评审。

读取已存在的 `apps/facts/{models,clinical_schema,clinical_segments,clinical_extraction}.py`、
`clinical_services.py`、`clinical_readmodels.py`、`clinical_forms.py`、`clinical_views.py`，
`apps/exports/clinical.py`、`apps/patients/sharing_content.py` 与本规格。

1. 确认两个功能交付的字段键、类别、实体键和相同报告内的上下文引用。
   明确 `entity_context` 的字段/令牌允许清单、失效及关联更正流程，不建立隐式跨报告合并。
2. 冻结陈述的否定/不确定、日期角色、数值量纲、MSI 分类、IHC 评分及药物证据模式。
   确认通用表单能表达原单位缺失，不把旧尺寸表单的默认单位带到新值。
3. 记录现有 ClinicalExtraction 每版本只执行一次的边界：新资料使用新版本，旧资料明确
   重新解析或人工补录；不要删除完成记录补跑。更新设计中尚待冻结的存储/portable 细节。

产物：经独审的规格增量、明确字段合同及旧值兼容样例；尚无真实预测。
公共文档进入正式实施依据后才把 `lifecycle` 改为 `active`，交付仍为 `implementing`。

### Task 2: 原件标注、评分与执行合同

依赖：Task 1 通过。只在忽略的私有材料区开展标注。

1. 从本次覆盖清单选择完整可判断报告/表/章节，逐页复核每个字段的值、角色、范围和来源。
   对完整主变异表覆盖全部行及等级；区分概览复述与新事件，不以基因名去重。
2. 对 IHC 报告缺页、无身份的解读、未审页和没有完整手术病理的切片明确标为未知或未评测。
   对说明、质控、对照、历史引用的排除按具体区域登记，不把整页标为没有事实。
3. 原件可读但 OCR 不支持的字段分别记原件真值与来源可证明性。核对每个引用的原块/偏移、
   字符原文、polygon、OCR/原件 SHA；粗略目视区域不能冒充准确 OCR 字段框。
4. 独立冻结 gold 和协议：每字段/报告范围分母、配对身份、归一化允许项、重复/额外、
   未核实来源、执行失败及未知页处理。独审通过后才开发纯合成评分器及生产输出映射。
5. 映射用实际持久化 Fact/SourceEvidence 和原 OCR，不接收 gold 答案定位；以 UUID 改变、
   真实来源改变、同页错行、同基因异位点、复制候选及未知页的合成反例独审。

病理/IHC 已新增 `tools/pathology_molecular_evaluation.py`、`tools/pathology_source_mapping.py`
及 `tools/pathology_pipeline_evaluation.py`，对应 `tests/tools/` 评分、来源映射和执行器用例。
每次准确 CLI 和真实执行清单仍须按上述合同单独批准，不能因入口存在而重复运行。
本计划不是执行真实预测的批准，不预填准确率或 gold 字段数量。

### Task 3: 病理/IHC 模式、来源分段与真实持久化

依赖：Task 1；真实评测另依赖 Task 2 的完整批准。

修改现有 `apps/facts/models.py`、`clinical_schema.py`、`clinical_segments.py`、
`clinical_extraction.py`、`clinical_services.py`；按实际需要新增迁移。
可新增 `apps/facts/pathology_extraction.py` 分离领域规则，不把大量规则继续塞进影像候选函数。
已新增 `tests/facts/test_pathology_schema.py`、`test_pathology_extraction.py`、
`test_pathology_tables.py`、`test_pathology_metadata.py`、`test_pathology_histology.py`、
`test_pathology_pipeline.py` 等定向入口，后续随真实边界补充回归。

1. 先以真实 ORM 写入和读取证明非影像字段被旧固定类别/实体校验拒绝；增加旧字段内容及
   已确认状态不变的兼容回归，再扩展模式/类别分发及人工报告类型。
2. 建立多标本、补充报告、同页独立面板、报告中历史引用、逐字拆块和跨块单位的失败用例。
   只有明确原件范围和标题能建立报告、实体或上下文，保留未归属文字和限制。
3. 实现取材、组织学、分化、尺寸对象、浸润、切缘、淋巴结及明示分期的表格和叙述路径。
   “倾向/不能除外/未见”、查体与病理节点、标本与肿瘤尺寸均有实际入库反例。
4. 实现 IHC 标记、结果和明示评分，区分 TPS/CPS/IC 的单位与检测条件；对照图、纯度、
   未选复选框及缺项不生成患者阳性/零值。每个候选核原字符范围和原件高亮。
5. 验证完成提取幂等、显式重新解析、自动失败与无候选状态的区别，保留旧记录和原文。
6. 按规格 4.3 实施可选不可变 `literal_source`，分别保存原值、完整值窗口与显式标签；
   用原 Unicode 位置验证自身覆盖及新绑定对目标值的完整覆盖，保留旧无声明候选资格。
   新 `pathology_metadata.py` 仅处理报告内明确且唯一的水平标签/值关联，不插入符号或
   猜下一行日期。`pathology_source.py` 在真实片段入库后验证角色与同字段归属。
   `tools/pathology_source_mapping.py` 只读实际角色，新声明错误与旧无标签状态分别保留。
   该补充保留两次已批准真实运行的全部输出；本阶段只做合成修复、正常 COMMIT PostgreSQL
   及必要界面/输出验证，独审和新执行身份批准前不得再次真实预测或重新评分。

完成标准：新 OCR 到持久化的真实合成路径通过，旧影像核心回归通过，人工不足来源可见。
真实手术病理无样本的字段质量仍未评测；该限制不能被合成通过替换。

### Task 4: 病理/IHC 核对、携带与第一个功能交付

依赖：Task 3；任何真实评测依赖 Task 2。

修改 `apps/facts/{clinical_forms,clinical_views,clinical_readmodels,readmodels,views}.py` 及
`templates/facts/{reports,report,field,index,detail}.html` 中实际受影响位置。
修改 `apps/exports/{clinical,content,formats}.py` 与
`apps/patients/{sharing_content,share_forms,share_views}.py` 的确切投影/选择入口。

1. 实现报告类型和实体选择、确认/更正/补录/排除/撤销及原页核对；日期和分数的有效展示
   从修订值生成。服务验证同报告实体引用，不信任提交的类别或其他患者 ID。
2. 将可用新字段接入搜索、速查和既有 CSV/JSON/ZIP，使用实际 HTTP 生成并打开产物检查。
   细选 IHC 评分显示必需的最小 marker/评分类型/原单位及不透明标本检测范围，不夹带独立
   未选字段、整报告、旧摘录或完整原件；未关联字段不得成为可携带裸值。
3. 增加 `tests/exports/test_pathology_exports.py`、`tests/exports/test_pathology_output_views.py`，
   并扩展原安全路由矩阵；以读后更改来源、字段/父报告排除、替换/撤销和旧分享验证失效。
4. 新增 `tests/integration/test_pathology_context_postgres.py`、`test_pathology_output_postgres.py`，
   并用 `test_pathology_literal_sources_postgres.py`、`test_pathology_metadata_sections_postgres.py`
   检查相应来源修复；在独立数据库验证实际 actor、撤权、文档重解析/删除恢复、作者注销
   与输出的正常 COMMIT 竞争，不复用其他任务的测试库。
5. 新增真实 `tests/browser/test_pathology_browser.py`：手机原件加载、表格/长文本、
   键盘核对、修订后速查和细选输出；记录截图及真实失败/修正，不使用假 API 替代链路。
6. 对获批的真实切片执行一次冻结评测并逐字段核分配，保留所有失败/未知；公共证据只用
   计数/方法/哈希。必要共享回归及 CI 通过、独审完成后交付病理/IHC PR。

### Task 5: 分子检测、变异与报告药物依据

依赖：第一个功能 PR 已合 main，从最新 main 建独立分子功能分支。
2026-09-10 已从 PR 70 实际 Squash `4b73d2e` 建立独立分支，具体 C1–C4 接入合同与
完整应用步骤见[分子应用实施计划](2026-09-10-molecular-application.md)。固定源码8a54已完成
本地合成应用验证；本节原真实验收范围保留，不能以合成通过将Task5整体标为完成。
补充 Task 1/2 的该切片合同和金标准独审后，才运行这部分真实预测。

拟新增 `apps/facts/molecular_extraction.py`，修改已合并的模式、分段、服务和展示分发。
新增 `tests/facts/test_molecular_schema.py`、`test_molecular_extraction.py`、
`test_molecular_boundaries.py`，复用已交付的临床事实与来源模型。

1. 先红后绿覆盖标本/配对样本/panel/日期继承、无标题列、跨页变异表和不同报告身份。
   panel 规模保留近似限定；收样/采样/报告时间不互补。
2. 保留完整小变异、CNV、融合及各自量纲；同基因异位点、原转录本版本、终止/延长/内含子
   表达和有序融合伙伴独立。匹配摘要与明细须完整身份，不把行数少当成“去重成功”。
3. 将 MSI 分类与原值、TMB 数值/单位/定性、同一 IHC 的 PD-L1 评分接入；否定仅作用于
   明示检测种类。CD274 拷贝数、TNB、ITH 和参考阈值不能借标记词进入错误字段。
4. 分列提取报告药物组合、方向、等级/体系和明确关联，不从知识背景生成患者变异或治疗。
   同药不同依据、潜在耐药列、跨癌种说明及截断条目均用原始来源反例验证。
5. 按 Task 4 同样完成实际核对、搜索/速查、精细导出/分享和失效；新增
   `tests/exports/test_molecular_exports.py`、`tests/patients/test_molecular_sharing.py`、
   `tests/integration/test_molecular_postgres.py`、`tests/browser/test_molecular_browser.py`。
   仅选一条变异/一个评分不能携带其他变异、整张药物表、报告来源全文或未选原件。

完成标准：表格与叙述均有自动持久化的合成证据、完整用户链路及获批真实切片评分；
不同变异类别与范围保留，不用总字段正确数替代报告准确率。

### Task 6: 保真、独审及交付记录

依赖：对应功能的源码及评测身份已冻结。每个功能 PR 各执行一次适当范围。

1. 跑领域回归及受影响的现有临床、导出、家庭、路由安全与迁移用例。保留旧七类和四类
   影像字段合同；共享解析修改时按批准入口重放旧切片及 64 文件 EXCERPT 并做逐条保真。
   若只比较未变源码，明确不是新执行；历史 gold、评分和所有旧预测保持原字节。
2. 运行必要真实 PostgreSQL 与浏览器，再跑仓库必需选择范围。已经通过的全量不因没有
   新风险而反复重跑；发生合流或修复时分别记录新源码身份和定向验证，不混计不同运行。
3. 冻结 review package、实际 Git/磁盘身份、候选数/分母/未知、全部报告及原始失败证据，
   独立审查通过后补去标识 verification 文档、JSON、registry 与索引。
4. 校验确切中文 Conventional PR 标题/正文、暂存区和新增历史的隐私；PR 由协调者处理
   Squash 与实际发布。只有真实版本确定后回填版本清单，不能提前把 B3 整体改为 verified。

## 运行方式与本阶段检查

命令在对应独立功能工作区运行；PowerShell 设置 `PYTHONUTF8=1` 以保证子进程编码。
当前已实施字段核心、自动解析及核对页面。`83753a6` 核心修复通过独审 63 项；
`e7c524a` 解析检查点通过 73 项合成/持久化与旧影像近邻；合入血糖 main 的 `431919d`
另有 76 项近邻通过。这些运行分开记账，均不代表真实病理提取质量或完整功能交付。
以下为可运行入口，具体执行身份和结果须保存在对应验证制品中。
来源窗口与分块元数据补充使用 `tests/facts/test_pathology_literal_source.py`、
`test_pathology_split_metadata.py`、`tests/tools/test_pathology_literal_mapping.py`，
实际提交边界使用 `tests/integration/test_pathology_literal_sources_postgres.py`。
元数据章节归属使用 `tests/facts/test_pathology_metadata_section_scope.py`，
并通过 `tests/integration/test_pathology_metadata_sections_postgres.py` 检查真实上传后
另一数据库连接可见的绑定；保留正常送检、标题前元数据和结果章节重开控制。
核对页面沿用 `tests/browser/test_pathology_browser.py` 的自动分块材料手机路径。
章节归属修复在 `58f5a75` 已通过独立定向验证：45 项普通、5 项 PostgreSQL 全部通过，
均无跳过；这不代表两次真实提取结果已经改善。

实际主线 `9675f0e3f61f96eb4895c364229b9da9d8a27bdb` 已通过本地合并提交 `3f17952`
集成；文档登记保留双方条目，自动版本文件全部来自该主线。原 69 项输出与页面近邻通过，
新增地址省略边界先得到 12 失败/8 通过，另复现分享中两个标本范围错误合并。
本次仅在病理输出层兼容省略后的严格回读、先值后限定展示及真实锚分组；新增入口为
`tests/exports/test_pathology_cloud_projection.py`。修复后 100 项普通、23 项 PostgreSQL、
2 项实际手机 Chromium 输出/分享用例通过，均无跳过；各轮用例重叠，不累加计数。
上述集成在 `a36cc22` 经独立 76 项普通、4 项 PostgreSQL 及另列 1 项原合组反例审查通过。
随后第三次精确身份经批准实际执行一次并完成独立只读回读；原失败、旧源码及前两次评分不变。
真实结果为 1 严格正确/6 错配/3 缺失，原 15 个正确分项保留，新增 9 个正确分项；
全部分母、来源变化和未知范围见[证据](../verification/batch-three-pathology-ihc.md)。
三次 native 都只保存其他 345 个 Fact 的数量，原三轮本身仍不能证明 Task 6 的完整值/来源保真。
之后已获独立批准执行一次 64 来源/124 页旧任务重放，在同一数据库捕获完整 FIELD、EXCERPT
和只读备份；这是病理分支第 4 次应用重放、旧任务第 1 次，不产生新的病理 10 目标评分。
原过程在量化历史索引解释处 EXIT1，所有原始输出与评分已保存；另一次经独审的只读续比对
修正索引坐标的解释并退出 0，没有新解析、重新分配或重新评分。五个历史检查点的旧严格/值
正确项全部保留，已发布七字段 73/73 和量化 35/35 的完整字段相同，202 条旧摘录完整保留。
初始七字段和量化的原值及来源仅 44/46、27/35 精确相同，其完整字段仅 33/46、27/35 相同，
差异、原 EXIT1 和三轮原评测均保留。非作者验收及所有范围见
[匿名兼容性制品](../verification/artifacts/batch-three-pathology-old-task-compatibility.json)。

```powershell
$env:PYTHONUTF8='1'
python tools/verify_documentation.py
python -m pytest -q tests/facts/test_clinical_foundation.py tests/facts/test_clinical_segments.py tests/facts/test_clinical_report_boundaries.py tests/facts/test_clinical_views.py tests/exports/test_clinical_exports.py tests/patients/test_clinical_sharing_and_audit.py
python tools/run_required_tests.py -q --ds=config.settings.postgres_test -m postgres tests
python tools/run_required_tests.py -q tests/browser/test_pathology_browser.py
```

PostgreSQL 命令需预先指定自己的合成测试数据库，真实评测需另获冻结执行入口批准。
新文件在各任务创建后再加入定向命令；准确标题、正文和实际执行输出保留于对应交付证据。

## 当前执行日志

- [x] 从最新已合并 main 创建独立分支和工作区，保留其他任务。
- [x] 核对主线类型化接口及固定 128 个原件/OCR 身份。
- [x] 目视核对 27 页、冻结 97 页未判断及原页制品身份。
- [x] 整理本设计和计划，进入独立评审。
- [x] Task 1 的字段/上下文合同与详细设计通过独立评审（合同检查，不是应用验证）。
- [x] 首批原件 gold/协议经独立原页核验冻结：10 字段，2 评分和 8 上下文；完整报告 0。
- [x] 核心来源、不可变关联与整组替换/恢复两项独审缺陷闭环；自动解析另留独立检查点。
- [x] 病理/IHC 切片 Task 2 金标准、评分/映射及三次各自执行身份冻结，原结果和失败均保留。
- [ ] Task 2 分子切片继续原件标注、独立审查和完整分母冻结。
- [x] Task 6 独立旧任务捕获及只读续比对通过非作者验收；旧正确项保留，初始原值/来源差异与原 EXIT1 均另列。
- [x] 病理核对 UI、精细输出及来源生命周期完成已述分阶段独审，最终交付仍需更新文档独审及确切 PR 头 CI。
- [ ] 两个完整功能交付、各自独审与发布关联。

复选框只记本次执行过程，不能覆盖文档登记表或制造未执行的测试结果。

分子应用最新源码 `32c6b2b620bf8225a208882c901234f8687adc68` 已合实际癌症main9cc；portable1.8、最终23PG/45普通及分阶段独审见上述分子验证记录。原8a54、b605和真实质量证据不改，真实M7未启动。
