# 分子检测完整应用实施计划

执行[分子应用接入合同](../specs/2026-09-10-molecular-application.md)，完成原
[病理与分子计划 Task 5](2026-09-08-pathology-molecular-evidence.md)的应用范围。
起点为实际 main `4b73d2e9c925ef48c95c217c7c055c5b7157e13b`，分支
`feat/batch-three-molecular-application`，完整交付状态仍为 implementing。

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

## 验证命令与状态

以下是待执行命令，不代表已经通过；PG/浏览器使用项目已有必跑工具和隔离环境。

```powershell
$env:PYTHONUTF8 = '1'
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python tools/verify_documentation.py
python tools/release_version.py check
python -m pytest -q tests/facts/test_molecular_schema.py tests/facts/test_molecular_context.py
python -m pytest -q tests/facts/test_molecular_extraction.py tests/facts/test_molecular_boundaries.py
python -m pytest -q tests/exports/test_molecular_exports.py tests/patients/test_molecular_sharing.py
git diff --check
```

任务执行日志不能覆盖登记表；初始文档冻结不标记功能 verified。真实 M7/新预测仍为 NOT_RUN。
