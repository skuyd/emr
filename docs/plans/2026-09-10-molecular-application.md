# 分子检测完整应用实施计划

## 当前交付与验收

完整分子应用已由 [PR #88](https://github.com/skuyd/emr/pull/88) 按准确头 `625850ea9d54dd3309920346374b43d5f547f149`
完成独审及[精确CI](https://github.com/skuyd/emr/actions/runs/34544444229)，Squash为 `18181d9e68dd6adf0cf3438c63ac03fe9405b7f6`，随
[v1.19.0](../releases/v1.19.0.md)发布。63条及7项全局的每条功能结论和第8节均已由非作者据实际证据闭合，详见[最终验收](../verification/batches-one-five-acceptance.md)。
真实质量目标另列：病理1/10、癌种原评分、病灶retention=false和云未判定页保留；
治疗周期≥80%仍未建立，其规格/计划仍为implemented。真实M7及癌症第二次真实评估未启动，
生产仍BLOCKED。下文合同仍有效，带源码或时间的旧待交付措辞仅记录原检查点。

执行[分子应用接入合同](../specs/2026-09-10-molecular-application.md)，完成原
[病理与分子计划 Task 5](2026-09-08-pathology-molecular-evidence.md)的应用范围。
起点为实际 main `4b73d2e9c925ef48c95c217c7c055c5b7157e13b`，分支
`feat/batch-three-molecular-application`，功能交付状态为 verified，真实质量与生产门禁另列。

## Global Constraints

- 先冻结 C1–C4 文档并独审，再完成 M1–M6；不能缩成字段注册或只写纯合同。
- 28 个 A0 键不变，三日期 raw→raw_value 无损适配，旧 IHC 策略及数据不重写。
- 新版本模式、范围陈述、多变异药物关联和公开最小语义须严格验证，未知不能猜测。
- 原源码制品、封存、gold、失败和独审记录不修改；真实 M7/评估保持 NOT_RUN。
- 有阶段提交但不推送、不建 PR，最终冻结源码与必要测试后交独审；不修改自动版本字段。
- 新源文件仅在本功能 worktree；临时执行信息用本地 runtime JSON，正式文档依登记规范。

### Task 1: 冻结增量合同并保存基线

结果：规格明确字段/日期适配、角色/锚/闭包、多目标关联与输出最小语义，不留依赖临时
提案才能解释的结构。登记本规格/计划，更新总索引及原 Task 5 引用，独审此文档提交。
记录实际基线、全部旧 verification artifacts 哈希和 A0 28 字段列表。
运行文档校验、Django check 和迁移干跑，基线测试仅选择相关已合合同与上下文。

### Task 2: M1 字段、路由和真实数据库约束

新建 `apps/facts/molecular_schema.py`、`molecular_adapters.py`，接入
`clinical_schema.py`、`models.py`、`clinical_services.py` 和实际迁移。
新 FieldSpec 按版本调用 A0；共享三日期及允许的 IHC/标本/检测键仍保持旧定义。
增加分子类别/报告路由、变异和药物锚的条件唯一约束，不运行数据重写。

先在 `tests/facts/test_molecular_schema.py` 证明未注册和错误范围失败，再覆盖全 28 键、
原十进制/比较符/范围/缺单位、三日期各精度与 raw 冲突，以及带检测类型的否定正反例。
对有限原词/代码做正负控制与实际补录、确认、更正反例；未知词不转成原报告不确定，
自身值窗口之外的标签/复制锚不能提供断言，限定范围不能由合法枚举扩大。
`test_molecular_migrations.py` 用 MigrationExecutor 建立旧 1.0/1.1、已确认病理日期/IHC、
完成提取后迁移，逐项证明原值/修订/资格未变，新锚实际数据库去重成立。

### Task 3: M2/M3 真实上下文与自动候选管线

新增 `apps/facts/molecular_context.py`、`molecular_extraction.py`，必要时拆分
`molecular_segmentation.py`、`molecular_source.py`。通过版本策略接入
`clinical_context.py`、`clinical_extraction.py`、`clinical_readmodels.py` 和实际 worker，
保留旧病理 resolver 的策略与令牌输入。

新上下文严格处理版本化多值 VARIANT 角色、实际锚 FK/实体、同报告/同解析版本、
原关联片段和 rank。低 rank 成员闭包不得反向依赖其使用者；原文角色参与可用性。
复用 FactSourceFragment 和已合 literal source 的位置覆盖规则，不用拼接文本偏移假装
原 OCR 索引。表格行和叙述的实际 source map 同时持久化；完整身份相同时才能并来源。
允许不完整/未关联候选可见，不能为达到完整而借用质控/文献或另一检测的锚。

`test_molecular_context.py` 覆盖跨患者/文档/报告/版本、错角色、重复/未知绑定、循环、
32/33 节点及 8/9 深度、同词错位、祖先/标签冒充值 proof、作者变更和新增成员。
`test_molecular_extraction.py` 经真实合成上传 worker 到 ORM 读回，覆盖表格/叙述、
同页多报告、配对标本、多 panel、表头缺失、跨页/拆列、截断、重复概览、完成幂等、
租约丢失和原子失败，区分失败、零候选、来源未判断。
`test_molecular_boundaries.py` 覆盖同基因异位点、各版本转录本、密码子、CNV/融合、
有序伙伴、MSI-L/MSS、TMB 数值/定性、TNB/ITH/CD274/阈值/对照和检测范围阴性。
药物单/多变异关联、同药异依据、组合/备选、获益/耐药列及文献角色须独立反例。

### Task 4: M4 核对界面、修订和搜索

修改 `clinical_forms.py`、`clinical_views.py`、`clinical_services.py`、
`clinical_readmodels.py`、`pathology_services.py` 的模式分派及实际 `templates/facts/`。
复用报告/字段路由和原件 viewer，分组显示标本、panel、变异、药物依据；未知/未印状态、
长尾表达、方向和等级体系完整可见。补录、确认、更正、暂缓、排除、撤销全部走真实服务。
上下文/身份更换原子复制完整依赖集合，旧项排除；组 UNDO 只恢复 PENDING，单项不能拆组。
报告路由的共同分类用于搜索、表单、速查与后续分享，不能只加一个类别枚举。

真实 HTTP 测试覆盖 viewer/editor/owner、旧表单、非法 source proof、同一值不同作者、
部分替换失败回滚、遗漏未选依赖和整组 UNDO。实际浏览器覆盖桌面和 360px，须等待
原图片加载/高亮和实际窗口事件，不能固定 sleep 或只检查元素存在。

### Task 5: M5 输出、分享和严格 reader

新增 `apps/exports/molecular.py`，接入 `clinical.py`、`content.py`、`formats.py`、
`pdf.py`、选择表单和 `apps/patients/{share_forms,sharing_content,sharing,share_views}.py`。
分子选择策略独立于旧 IHC，明示最小必要身份束；上下文闭包是私有验证材料。
变异数量、MSI/TMB、检测范围否定及药物等级不能导出为失去身份/作用范围的裸值。
公开形状移除整句 raw/私有目标/角色偏移，使用选择内别名，来源保留真实页/框。

`tests/exports/test_molecular_exports.py` 逐项读真实 PDF 文本、JSON、CSV 和 ZIP 原件字节，
验证单字段与混合领域、所有旧数组、HGVS 长表达/Unicode/公式文本/云 URL 默认省略。
删除模式/策略/必要身份、伪造伙伴顺序/作用范围/省略标记时 reader 拒绝，旧格式照常读取。
`tests/patients/test_molecular_sharing.py` 用实际分享交换和接收者请求，证明细选不包含
其他变异/药物表/报告正文/原件；原件和章节许可独立，角色/会话/到期持续核验。

### Task 6: M6 生命周期、真实并发、浏览器与完整回归

`tests/integration/test_molecular_postgres.py` 用独立合成 PG 库及正常 COMMIT 两连接，
覆盖预览/无效表单/渲染、构建、存储 IO、首字节/每块前后，以及部分作者 collector UPDATE。
检测原件/报告/字段/成员更正、重解析、删除恢复、注销作者、成员降权、分享撤销及期限。
证明锁序、旧快照失效/清理和失败重试；不能把统一回滚、吞异常或空文件当作通过。

`tests/browser/test_molecular_browser.py` 覆盖实际用户完整链路，纳入必跑机制。
新增 `tests/facts/test_molecular_privacy.py` 检查真实错误报告/日志的异常链、GET/POST/
渲染和输出路径：保留原因与请求身份，原访问串不外泄；原生 CSRF 同源行为保持。
最终按实际共享改动跑临床/影像/病理/导出/家庭/权限/迁移回归，再运行项目要求检查。
冻结执行前后源码、命令、退出码、非空/零跳过的 PG/TLS 和具体产物证据。

### Task 7: 文档、完整独审和交付准备

补本地合成验证的匿名公开制品和 verification 文档，登记实现、失败及尚未执行范围。
独审完整新增应用及最终精确源码，不能用 A0 145 项测试或早期局部通过代替。
主线变化只合流实际 origin/main，重核 portable 字段、全部数组和受影响测试后再独审。
PR 标题/正文检查、远端 CI 和 Squash 由主代理在授权范围内安排；本作者先不推送或建 PR。
版本以 Release Please 实际结果为准，生产放行和真实质量不由源代码发布建立。

## 实际执行与待交付边界

| 原任务 | 当前证据 |
| --- | --- |
| Task 1 / C1–C4 | `cc0647c` 文档先冻结并通过独审；A0 28 键及原制品基线保留 |
| Task 2 / M1 | 实际数据库锚约束；MigrationExecutor 旧 1.0/1.1、IHC/日期与确认逐行保真 |
| Task 3 / M2–M3 | 真实来源闭包、逐位置证明、合成上传 worker/ORM、表格/叙述、多报告/续页边界；保留429/8a54历史身份，最终45组已实际重跑retry并验证新提取身份 |
| Task 4 / M4 | 原件核对、分组替换/UNDO、完整搜索及控件原值；CODED、空白/项内换行、最后来源读取后权限 P2 均有新修订和非作者关闭证据 |
| Task 5 / M5 | 原 M5 `8bb4052` 经 root 非作者独审，组合 `8a54e18` 保留其核心；真实 PDF/JSON/CSV/ZIP、分享交换与严格 reader |
| Task 6 / M6 | 8a54 普通1459、PG72、SQLite TLS4、PG TLS4零跳过；四次1329原字节清单前后一致，有界独审34+5通过 |
| Task 7 | 32c6已合实际9cc、portable1.8与全部数组，b605专用证明闭合；分阶段有界独审和f851正式文档独审已完成；实际主线同步增量独审、精确PR CI和v1.19.0交付均已闭合 |
| 真实 M7 | 未启动；原真实分子未判定页和金标准边界不变，不用合成通过推导真实质量 |

真实 PG 永久入口为 `tests/integration/test_molecular_context_postgres.py` 与
`tests/integration/test_molecular_output_postgres.py`；原计划单文件名称是设计草案，实际按事实
与输出职责拆分。真实浏览器为 `tests/browser/test_molecular_browser.py` 和
`tests/browser/test_molecular_outputs_browser.py`；新增四域手机 `tests/browser/test_molecular_combined_browser.py`，三文件均纳入必跑名单且从普通 CI 排除。
worker 重试/租约入口为 `tests/processing/test_molecular_retry.py`；迁移入口为
`tests/facts/test_molecular_migrations.py`。逐条要求与真实断言见
[验证记录](../verification/batch-three-molecular-application.md)及其匿名制品。

冻结检查点已运行 Django check、迁移干跑、文档115、发布元数据1.17.0一致性和 diff 检查。
这些检查不替代上表实际应用执行；提交文档后仍须重新运行文档校验。最终主线变化仅重跑
受影响的集成边界，不重复健康8a54全套，不改旧日志/回执或原失败。PR由协调者按既有授权
安排，最终实际版本以 Release Please 为准。


## 实际主线联合完成检查点

应用源码 `32c6b2b620bf8225a208882c901234f8687adc68`（父b605+实际9cc）采用portable1.8，保留所有已发布数组和严格旧包边界。
新增跨域末次来源短锁与原EXPORT/MANAGE权限、完整提取身份精确兼容和0007内部字段扩展。
最终23PG/45普通均0跳过；ea843的22/256/8PG/5+5TLS按未变实现继承，源码差异逐路径披露。
新增PG迁移执行包含旧行/确认保持和长值反向收窄安全失败，不能以干跑或SQLite替代。
原32需求映射及b605两条专用元数据/DEFER证明保留，真实M7未启动。
本功能PR精确CI及v1.19.0发布已建立；实际主线身份和自动版本均据原件核实，未借未合分支。
