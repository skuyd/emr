# 后续第 1—5 批实施计划

目标是完整交付[第 1—5 批需求](../specs/2026-09-07-batches-one-five-requirements.md)，
依据[原始产品需求](../product/original-product-requirements.md)和用户本次 Goal 授权完成
实现、验证、文档、多个 PR、Squash 合并与 Release Please 源码发布。
当前功能状态以[登记表](../document-registry.json)及逐项真实证据为准。

初始证据基线为 `origin/main` 的 `b7d5f3485b1b73cacc08476863401c11ae927d46`。
总计划分支为 `docs/batches-one-five-plan`；每个新的功能分支都重新获取最新 `origin/main`，
从当时精确提交创建独立 worktree。依赖功能先合入 `main`，不以未合并功能分支为起点。

## Global Constraints

- 完整范围为规格 B1-01 至 B5-02，不因本轮上下文或测试难度缩减。第 6—7 批及生产
  放行不在本 Goal；保持原件、旧数据、隐私和来源契约。
- 原始文件不可变；几何变换必须回映到原件。自动、人工及有效版本分别保存；无可靠
  证据显示缺失或候选，不能伪造确定事实或用仅摘录代替结构化功能。
- 角色为创建者、管理员、协作者、只读；分享仅授予选定范围。邀请默认 7 天，分享默认
  24 小时且最长 7 天，默认需登录，原始令牌不得落库或进入日志。
- 所有旧入口、后台任务及派生下载同时遵守患者/操作者授权、到期、撤销和来源变化。
  PostgreSQL 并发验证不可用 SQLite 串行测试替代；后台测试不能只调用页面视图。
- 真实评测使用现有授权样本，不增加固定样本数量要求；真实与合成分别计数，保存失败
  和未判断范围。禁止把医疗内容、私有样本和云部署资料提交到 Git、PR 或外部服务。
- 腾讯云所有资料继续只保存在主工作区 `docs/deployment/local/tencent-cloud/`。新工作区
  不复制该目录；不改生产资源。普通开发记录按被忽略的本地执行记录管理。
- 新 Markdown 放在治理目录，同步登记、索引和引用；版本未确定时 `releases: []`，不得
  手工改版本字段和 Changelog 自动区域。发布后按真实版本建立关联。
- 使用 Windows PowerShell 和现有 Python 环境，设置 UTF-8；命令显式指定目标 worktree。
  保留原有 worktree 与未提交工作；提交、推送前分别检查暂存区及新增提交历史。
- 每个行为变更先建立有意义的回归失败，再实现和验证。每个重要 PR 独立审查，全部必要
  检查通过后按已授权范围 Squash 合并，不额外设置重复用户审批环节。

## 架构与依赖

现有应用为 Django Web，处理层位于 `apps/processing/`，检验在 `apps/labs/`，事实在
`apps/facts/`，导出在 `apps/exports/`。`apps/core/tenant.py` 和 `patient_required` 是
入口基础，但服务层还有所有者假设。新增医学结构化继续复用事实来源与修订，日常记录
使用独立记录模型，血糖保留分钟级时间。统一患者访问上下文必须先于 B3—B5 合并。

顺序为 Task 1 → Task 2/3 → Task 4/5 → Task 6/7 → Task 8 → Task 9 → Task 10。
互不修改共享文件的子任务可在独立分支先准备；集成前重新核对当前 `main` 和具体依赖。
Task 2、3 的图像与解析质量工作相互独立；Task 6、7 的业务可分别交付，但共享来源
模型应通过先行小 PR 固定。以下任务可继续拆为多个具有独立验收结果的 PR。

### Task 1: 固定范围、基线和评测环境

- 对应全规格。调查原始需求与代码、固定真实样本、当前测试工具及 CI；记录范围和
  未判断项，获得不含敏感正文的基线统计。
- 已有接口：`tools/phase_two_evaluation.py`、`tools/phase_three_evaluation.py`、
  `docs/verification/artifacts/labs-extraction-scope-evaluation.json`、
  `docs/verification/artifacts/phase-three-real-evaluation.json`。
- 编写本规格、计划并同步 `docs/document-registry.json`、`docs/README.md`。独立审查
  所有已讨论功能是否进入编号验收，执行 `python tools/verify_documentation.py` 后交付。
- 核查本地 Python/浏览器/PostgreSQL/OCR 环境，用现有脚本实际验证路径；不输出私有
  标注、患者身份和凭据。只有工具可运行后才据实际命令固化后续执行记录。

### Task 2: 可回溯的图像增强和非单据提示

- 对应 B1-01、B1-02。检查/修改 `apps/processing/images.py`、`preparation.py`、
  `pdf.py`、`ocr/paddle.py`、`pipeline.py`、`models.py`、`apps/processing/metadata.py`
  与 `apps/documents/views/records.py`、相关模板。新增图像变换模块及必要迁移。
- 先用已知纸张和文本位置、EXIF/透明/扫描页证明当前缺少增强及回映，再实现可靠边缘、
  纠偏、阴影归一化、可逆坐标和失败回退。非单据为可恢复分类状态，保留原件。
- 扩展 `tests/processing/test_image_preparation.py`、`test_paddle_adapter.py`、
  `test_pdf_preparation.py`、`tests/documents/test_detail_viewer.py` 及新增浏览器流程。
  实际 OCR 样本检查文字与坐标、原件哈希和处理耗时，质量结果不只凭图像视觉改善。
- 运行处理/查看器/上传回归及 JavaScript 检查；公开合成定位制品，私有样本只存统计。

### Task 3: 检验与事实的真实质量改进

- 对应 B1-03。检查 `apps/labs/extraction.py`、`layout.py`、`validation.py`、字典匹配，
  `apps/facts/extraction.py`、`layout.py`、`metadata.py` 及评测工具。
- 从冻结结果定位可复现的单位/名称/标本与医嘱表问题，建立去标识或合成回归，不修改
  原始金标准为迁就预测。每次修复复跑同基线，统计联合和分字段、分类型差异。
- 扩展 `tests/labs/test_extraction_scope.py`、`tests/facts/test_section_boundaries.py`、
  `tests/facts/test_record_metadata.py` 和 `tests/tools/test_phase_two_evaluation.py` /
  `test_phase_three_evaluation.py`。覆盖误抽、重复、跨页、否定、日期冲突及回退。
- 原件/OCR/标注/预测身份及评测版本进入私有执行记录；公共证据保存统计、哈希、命令
  与限制。真实质量未改善时继续调查，不能用功能测试代替这项结果。

### Task 4: 多患者和统一访问上下文

- 对应 B2-01 及 B2-02 基础。修改 `apps/patients/models.py`、`services.py`、
  `views.py`、`profile_urls.py`、`apps/core/tenant.py`、`decorators.py`，新增患者访问
  服务和成员/活动患者模型与迁移；迁移保留既有拥有关系。
- 全仓调查所有 `patient.account`、`request.user.patient`、`account_id` 过滤。改造
  `apps/accounts/deletion.py`、`apps/operations/services.py`、`apps/documents/quotas.py`、
  通知任务、导出、事实和检验服务，显式传入操作者与患者访问资格。
- 增加创建/切换/删除患者与成员页面，修订账号注销范围和逐患者清理。旧URL、窗口、
  表单和上传批次不能因切换误写其他患者。
- 开放共享患者访问前，旧导出任务、上传提交、事实/检验修订与通知均按真实成员授权。
  导出发起者绑定、旧任务迁移与成员通知隔离在本任务完成，不能延至 Task 5 才补齐。
- 扩展 `tests/patients/`、`tests/security/test_csrf_and_idor.py`、
  `tests/security/test_tenant_isolation.py`、`tests/accounts/test_account_deletion.py`、
  导出/复核/通知与实际迁移测试；完整角色矩阵含原件、缩略图和旧 labs/facts 路由。

### Task 5: 家庭邀请、分享撤销和访问审计

- 对应 B2-02 至 B2-04，依赖 Task 4 已合并。新增邀请/分享服务、路由、模型与页面，
  复用 `apps/accounts/` 登录与 CSRF，明确只读/读写/管理动作矩阵。
- 改造 `apps/exports/sessions.py`、`services.py`、`tasks.py`、`files.py`，使快照绑定
  发起者和访问范围，并在生成/提交对象/下载检查实际资格；普通成员角色不继承内部复核。
- 扩展 `apps/operations/audit.py` 和审计模型以提供按患者、资源类型的查询与允许动作。
  记录读取/下载和拒绝访问，排除医疗正文与令牌；授权管理者可在界面查看。
- 测试邀请重放、令牌摘要、角色升级、过期、撤销、猜测ID、分享越界、跨患者、下载中
  撤权及渲染后资格变化；在 `tests/integration/` 加真实 PostgreSQL 竞争测试。
- 桌面/手机浏览器实走创建患者→邀请接受→只读拒绝写→分享→撤销→失效流程。

### Task 6: 类型化临床字段、影像和病灶

- 对应 B3-01、B3-02。基于 `apps/facts/models.py`、`revisions.py`、`readmodels.py`、
  `extraction.py`、`forms.py`、`views.py` 和 `templates/facts/` 新增结构化模式验证、
  来源与字段修订、影像实体和病灶关联，不复制只有文档所有者可写的旧假设。
- 先交付可自动提取/核对/修订的影像字段和跨报告关联候选，再实现尺寸趋势与冲突纠正。
  保存三维及原单位，病灶匹配依据可见；云影像链接只作来源访问，不后台抓取。
- 先建立同一文件内多报告/检查的独立分段、日期和来源身份；自动提取读取 OCR 报告段，
  覆盖旧事实摘录忽略的检查所见、标本和检测方法等内容。
- 更新 `apps/exports/content.py`、`formats.py`、`pdf.py`、搜索和详情，保证类型化
  字段贯穿速查、CSV/JSON 和 ZIP。扩展 `tests/facts/`、`tests/exports/` 及浏览器测试。
- 验收三维尺寸、左右/不同器官、同报告多灶、多个相似灶、重解析、删除和共享权限。
  真实样本与合成覆盖逐字段记录，预测字段必须实际进入数据库及产品界面。

### Task 7: 病理、基因与癌种排序

- 对应 B3-03、B3-04，复用 Task 6 已合并的结构化来源接口。扩展 `apps/facts/`
  的病理/分子提取器和字段模式，关联报告标本、panel、日期、位点和来源。
- 基因变异保留蛋白/编码及完整原文，不把同基因不同变异合并；IHC、MSI、TMB、PD-L1
  和报告给出的药物证据保留自身语义。癌种来自报告候选，用户选择只影响排序。
- 更改 `apps/labs/trends.py`、`comparison.py` 与显示选择，全部项目仍可访问；同步
  事实核对、速查和结构化导出。专门测试用户更正癌种后的稳定身份与零数据损失。
- 新增基因位点差异、标本差异、阴性/未检出、否定/不确定及来源冲突回归。真实报告未
  标注页继续显示范围限制，不拿字典条目数量当作结构化能力。

### Task 8: 治疗周期、双轴与高级趋势

- 对应 B4-01 至 B4-03。新增治疗方案/事件/周期候选、关联和修订服务，基于 `apps/facts/`
  的治疗来源；扩展 `apps/documents/views/records.py`、`templates/documents/records.html`、
  `trend.html`、`trends.html`、`apps/labs/comparison.py`、`trends.py` 和对比模板。
- 先提取报告明确 C/D 和治疗时间，再实现依据治疗/住院记录的自动周期提议及周期性辅助
  依据。加入确认/纠正/拒绝，显示方案→周期→检查，不为完成测试只提供手工周期输入。
- 增加周期相对天 ANC/PLT/HGB 叠图、关键节点模式、多指标联合图、迷你趋势、个人基线
  与按实际间隔计算的变化；统一复用可比性规则，不另造宽松的趋势入口。
- 在新周期/趋势测试中覆盖缺日、同日多份、暂停/换方案、三次基线不含当前点、零/负值、
  单位方法差异、特殊结果及撤销。浏览器实测切换、筛选、来源、移动/键盘和空结果。
- 用已知时间和数值的独立计算核对图表，真实周期标注与预测分开评测。所有新数据纳入
  选定速查/导出、删除与家庭权限。

### Task 9: 日常记录与日内血糖

- 对应 B5-01、B5-02，依赖 Task 4/5 访问接口。新增 `apps/self_records/` 应用（拟建），
  覆盖模型、迁移、服务、表单、路由、模板、审计和历史；在 `config/settings/base.py`、
  `config/urls.py` 接入，复用当前导航组件。
- 体重/体温/症状提供快速输入、更正和来源列表。血糖独立 measured_at 与 time_slot，
  时区明确，保存原单位和标准化值，同分钟多来源可展开；导入护理/血液报告来源。
- 建立日内曲线与多天时段热力表，不平均替代重复值；尿糖和 HbA1c 不进入血糖视图。
  更新 `apps/exports/` 和患者删除/账号注销服务，覆盖创建者及协作者的实际作者。
- 新增 `tests/self_records/`、集成和浏览器测试。验证角色、时间/时区边界、单位换算、
  重复、格式错误、来源删除、修订历史、导出和原件跳转，并记录快速路径实际操作时长。

### Task 10: 全范围审查、发布与文档闭环

- 每个前序任务在实现后完成针对性测试与独立审查，检查真实 diff 和未跟踪文件；本任务
  对最终状态按规格所有 Bx-yy 重新审计，逐项列完成证据、局限及仍缺工作。
- 运行 `python tools/verify_documentation.py`、`python tools/verify_traceability.py`、
  `python tools/verify_release_gate.py`、`python manage.py check`、
  `python manage.py makemigrations --check --dry-run`，必要全量 Python、JavaScript、
  必需浏览器、PostgreSQL 并发和固定解析评测。未执行或跳过不能记为通过。
- PR 标题及正文用 `python tools/check_conventional_commit.py` 校验。PR 正文从文件
  读取，确认无私有内容；检查暂存区和本次新增提交历史后推送。精确 PR head 的四项 CI
  通过且重要审查发现已解决后 Squash 合并；随后检查 Release Please 的发布 PR、标签
  和 Release 实际状态。新功能/文档分支基于下一次最新 `origin/main`。
- 在 `docs/verification/` 保存按批次的人类验证记录和去标识机器制品；登记状态以真实
  证据更新。待 Release Please 确定版本后关联 `docs/releases/v<版本>.md` 并更新索引。
- 不仅凭 PR 合并或全量测试绿灯声明 Goal 完成；全部五批需求、实际交付与最终证据链
  完整才结束。仍有工作时保留 Goal 和执行记录，继续下一任务。

## Task10实际闭环记录

## 当前交付与验收

完整分子应用已由 [PR #88](https://github.com/skuyd/emr/pull/88) 按准确头 `625850ea9d54dd3309920346374b43d5f547f149`
完成独审及[精确CI](https://github.com/skuyd/emr/actions/runs/34544444229)，Squash为 `18181d9e68dd6adf0cf3438c63ac03fe9405b7f6`，随
[v1.19.0](../releases/v1.19.0.md)发布。63条及7项全局的每条功能结论和第8节均已由非作者据实际证据闭合，详见[最终验收](../verification/batches-one-five-acceptance.md)。
真实质量目标另列：病理1/10、癌种原评分、病灶retention=false和云未判定页保留；
治疗周期≥80%仍未建立，其规格/计划仍为implemented。真实M7及癌症第二次真实评估未启动，
生产仍BLOCKED。下文合同仍有效，带源码或时间的旧待交付措辞仅记录原检查点。


| Task10检查 | 实际HEAD | 结果与范围 | 证据SHA256 |
| --- | --- | --- | --- |
| documentation | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：125份登记文档通过；不是未来候选overlay文档检查。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| traceability | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：原PRDv1的62项，60verified/2external_pending；不替代五批63+7验收。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| release_gate_consistency | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：生产门禁一致性检查通过，实际仍BLOCKED，8passed/15pending。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| django_check | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：Django check零问题，config.settings.test。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| migration_drift | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：无模型迁移漂移；不替代已保留的真实PG旧行/长值反向迁移证据。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| release_automation | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：自动发布契约检查通过，源码当时版本1.18.0；不证明后续Release存在。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| version | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：当时六处自动版本1.18.0一致；不把portable1.8或候选版本当已发布版本。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| ordinary | 625850ea9d54dd3309920346374b43d5f547f149 | 普通4835PASS/4skip/394deselected，2855.51s；Linux四个Windows专用跳过保留。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| browser | 625850ea9d54dd3309920346374b43d5f547f149 | 8文件必跑浏览器23PASS/0skip，119.41s；原本地TLS截图保留原阶段，不伪称CI截图。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| postgres | 625850ea9d54dd3309920346374b43d5f547f149 | 真实PostgreSQL387PASS/0skip/4869deselected，1381.00s；与普通和浏览器矩阵不相加。 | de48249c0847e196b0f0d1181d057b3677a959f9c5f9036bc0030dbb2c90153a |
| javascript | 625850ea9d54dd3309920346374b43d5f547f149 | JavaScript9PASS/0fail/0skip。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| synthetic_parser | 625850ea9d54dd3309920346374b43d5f547f149 | PR88实际制品：固定observations554；whole556文件/564行含extra_context6文件/10行子集；target125与gene50独立。分组重叠不相加，metadata/persistence未assessed，真实准确率未评测。 | 73774e479c440e0c90ab6504147ae4d7ee2724c4a39c72a573f4a0f75126d089 |
| title | 625850ea9d54dd3309920346374b43d5f547f149 | PR88实际中文标题及正文检查通过：feat(molecular): 完成分子报告提取核对与选定输出分享；root独立标题/正文检查另附原件。 | 1f2630cef2e77f281f2339dc6f324ffb9bfe6922eb25d6ec69e5e1176af4ad7a |
| private_history | 625850ea9d54dd3309920346374b43d5f547f149 | 提交前22个新增提交逐父路径审查及root全文/PR内容复核通过；1545路径、1256非文档非自动版本字节保持。路径门禁不是万能秘密扫描。 | b5f6b0c08ae05747c654a2ff01726c2cbaee81b4d7aeb4d2e855d7d9238eb401 |
| windows_only_launcher | 625850ea9d54dd3309920346374b43d5f547f149 | 独立Windows启动器4PASS/0skip，3.53s；临时synthetic manage.py仅记参数与生命周期，无真实web/医疗处理。Linux原4skip不重标、不加到CI分母。 | f95c64f0420711898544c669a83e0aa5dda794d287c4d2903380e96b4448efa8 |

以上为实际源码CI或原精确本地执行；documentation行不是本次候选文档检查。候选校验另由输出文件哈希及源码基线绑定的render-receipt记录，不能改称原main HEAD自身执行。最终文档PR的独审、CI及合并由外部回执记录。


原62项traceability与新63+7验收分开；verify_release_gate检查一致性通过不等于生产放行。固定解析评测仅为CI --synthetic-only制品，未启动真实M7；日志、跳过与分母均按实际执行保留。
