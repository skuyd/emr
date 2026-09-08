# 癌种候选与指标排序实施计划

目标：完成[规格](../specs/2026-09-08-cancer-candidates-and-ordering.md)及五批 B3-04。
计划为 `active / implementing`，下列新应用/测试文件均为拟建，不表示当前主线已有能力。

新功能已在获取最新 `origin/main` 后，从实际 `92825c287e35f72bc3df0420eee72bb1e0d2f76a`
创建独立 `feat/cancer-ordering` 工作区；后续只合入实际主线。现有应用检查基线来自该源码
发布的 CI，不把规划当作新功能测试通过。后续文档/功能提交的确切身份分别记录。

2026-09-08，独立审查提出的上游未收集输入遗漏已补齐并通过增量复核。契约在
`e2024cff5ceb47a8f6739428ea3bea81d241d05e` 冻结；开始 Task 1，尚无癌种功能测试或发布结论。

## Global Constraints

- 默认 `AUTO`，符合最初需求的上传即组织；不得改成必须先选癌种。自动模式仅用当前、明确、
  可靠、无冲突的报告字面候选重排，不写系统诊断、不自动确认 Fact。用户显式选择覆盖自动。
- 初始配置为 `GENERAL`、`LUNG`（CEA/CYFRA21-1/NSE/ProGRP）与 `PANCREAS`（CA19-9/CEA），
  使用规格列出的既有标准码。固定字面映射，不引入新的医学映射、阈值或单位规则。
- 未核对候选所用 OCR 片段置信度全部须至少 `0.95` 才可参与自动模式；测试 `0.9499`、
  缺失及跨块低置信度。确认仍不能抹去来源失效或原文断言。
- 候选原稿、原 Fact/OCR、修订事件不可覆盖。显示状态独立于通知偏好，完整保留已有迁移数据。
- AUTO 完整性和输出指纹包含全部当前上游输入及收集状态；未物化候选、零候选、失败或规则
  过期也有明确表示。发布生命周期与不可变收集输入分开，正常激活不使覆盖永久失效。
- 稳定重排发生在现有过滤/分组/计算及输出选择交集之后。完整多重集和显式联合趋势顺序不变。
- 沿用实际请求作者、事务中权限复查、患者优先锁与全部来源/生命周期指纹；不得逆序锁定。
- 未合病理接口不作为起点；同类 typed adapter 只能在其实际进入主线后接入并单独验证。
- 所有永久文档位于 `docs/` 并同步登记/入口。私有原件、凭据及执行证据不进入远程。
- 每个完整切片完成必要测试及独审后，按已有授权建立中文 Conventional PR，精确四项 CI
  全绿后 Squash；Release Please 自动发布。不得手改版本字段，生产门禁独立。

### Task 1: 固定字面候选和稳定排序纯契约

新增 `apps/cancer_ordering/profiles.py`、`schema.py`、`matching.py` 和
`tests/cancer_ordering/test_profiles.py`、`test_matching.py`。读取现有
`apps/labs/dictionaries/phase-two.json` 验证标准码，不修改字典。

实现规格固定的五个完整字面，并固定诊断标题/分句、断言/对象、来源区间、未知理由和
配置版本；肺叶例子仅“右肺上叶浸润性腺癌”，不组合生成新别名。反例覆盖转移部位、
他人病史、疑似/否定、混合诊断、较长未支持病名内的字面子串、
仅器官或标记物、分期数字、跨分句/跨页、NFKC 原索引和低置信度。先记录生产匹配接口的
真实失败，再实现。不能用候选标签相同替代原位置一一对应。

排序函数接受已有条目和配置，返回稳定排列；独立用已知不同码/同码异质组与未知项验证
每个 ID 出现次数、输入未被修改和 GENERAL 顺序，不只检查几个置顶名称。

### Task 2: 来源、候选和选择的不可变持久化

新增 `models.py`、`sources.py`、`services.py`、`readmodels.py`、迁移及对应测试；参考
`apps/facts/models.py`、`readmodels.py`、`clinical_readmodels.py` 和
`apps/patients/access.py`。绑定患者必须通过真实 Fact.document，不假设解析版本必填。

实现按原位置幂等收集、候选修订、选择/撤销及有效 AUTO 解析。父来源和候选独立核对；
来源更正后捕获新身份，旧确认不可复活。保持一位患者多来源冲突、多个家庭实际作者及
完整匿名历史。持久化收集范围与结果状态，以完整当前上游 Fact/解析/修订/作者身份验证
覆盖，包括成功零候选。未收集、失败、部分覆盖和规则过期时 AUTO 回 GENERAL。
全部状态在服务端重建，不能信任提交的 token/上下文/作者。

以真实 ORM/API 操作验证跨患者、viewer 写入、陈旧修订、同文不同位置、候选集合新增、
来源排除/恢复、实际作者注销和重复撤销。历史迁移须保留旧 Fact、观察项、患者通知偏好、
已存在分享和导出，不在迁移中自动重跑真实原件。

### Task 3: 接入实际抽取与可恢复候选收集

新增 `apps.py`、应用初始化与 `extraction.py`，接 `config/settings/base.py`、
`apps/processing/pipeline.py` 的已存在事实持久化完成点。先核对该路径事务与锁顺序，
避免文档锁内反向请求患者锁。失败记录匿名错误并保留原件归档及已有事实，不导致资料丢失。
在当前处理租约保护下记录不可变 Fact/OCR 输入的完整收集结果；读时单独检查发布/active/
生命周期。实际跑通 READY→PUBLISHED 后自动模式，不能用临时 active=False 使成功覆盖
永久失效。保留失败与重试记录；写请求重新收集只生成新的范围结果，不覆写旧尝试。

主线 EXCERPT 适配器必须实际产出候选记录；既有资料通过明确的 WRITE 请求收集当前
事实，不在 GET 上写库、不改历史抽取。后续 typed adapter 接当前有效的
`specimen.histology`，同时验证标本/报告上下文和来源，禁止 IHC/变异推癌种。
持久化集成测试须从合成 OCR/真实数据库入口走完整路径，区分原始候选、人工更正与显示偏好。
明确验证新诊断 Fact 已发布但收集未执行、失败或只覆盖部分输入时的 GENERAL 回退，
以及零候选完成、规则过期、来源更正、WRITE 恢复收集后重新解析完整集合。

### Task 4: 患者界面、核对与当前状态

新增 `forms.py`、`views.py`、`urls.py`、`templates/cancer_ordering/` 和需要的样式；
接 `config/urls.py`、现有 `apps/patients/profile.py` 与患者资料界面入口。
复用原件查看和来源接口，不建立绕过资料权限的新下载路由。

实现默认 AUTO 依据、通用回退理由、候选冲突列表、核对/更正/排除/暂缓/撤销、显式候选或
手动偏好选择及恢复自动。先做实际 HTTP 红例，覆盖渲染期间来源/成员/作者变化后拒绝旧
内容。360px 页面、标签和错误关联、键盘操作、空状态与长原文均可使用。

### Task 5: 将排序接入完整对比和趋势

修改 `apps/labs/comparison.py::comparison_view`、`trends.py::trend_summaries` 与
指标选择器调用点；检查 `apps/documents/views/records.py`、`apps/labs/views.py` 及其模板。
保持 `joint_trend_views` 的显式用户码顺序；不得改变 `apps/documents/archive.py` 搜索条件。

用跨医院、同码不同单位/标本/方法、非数值、同日多条、未知码和过滤为空的真实 readmodel
验证 AUTO、GENERAL 及用户覆盖切换。逐 ID/多重集核对数据无损，比较点、基线和变化值
完全一致。动态冲突或失效立即回退通用，不用缓存的旧有效候选维持排序。

### Task 6: 速查、导出和分享的选择与失效

新增 `exporting.py`/`output.py`；检查并接入 `apps/exports/content.py`、`services.py`、
`formats.py`、`forms.py`、`views.py` 和 `apps/patients/sharing.py`、`sharing_content.py`。
以当时实际已合主线格式为基线，保留全部既有数组/读取兼容，不复制未合分支实现。

先对已允许的指标应用顺序。候选/偏好正文需要独立明确选择；只选检验不携带诊断文字或
隐含源内容。私有依赖包含 AUTO 候选集合、冲突、当前来源和显示决定，不授予整文档权限。
AUTO 另外绑定全部当前相关上游 Fact/ParsingVersion、其修订/作者及收集状态，复用
Task 2 的完整性解析；未产生候选的新输入也必须使旧输出过期，不只指纹化显式输出文档。
为新可携带数据制定小型完整投影合同，检查 PDF 文字、JSON、CSV、ZIP 清单及 HTML 真正
生成的内容和拒绝路径，不仅比较内部字典。

在 `tests/integration/test_cancer_ordering_postgres.py` 用独立连接实际 COMMIT 检查
来源更正/新候选冲突/作者清理/撤权，以及新 Fact 发布但收集尚未发生或失败与预览、
worker 发布、分享及后续流块的竞争。实际打开并关闭生产
生成流，验证失效清理已经提交、旧快照拒绝且审计不重复。

### Task 7: 独立审查、真实范围及浏览器验收

新增 `tests/browser/test_cancer_ordering_browser.py`，沿用主线已修复的浏览器事务夹具
（参考 `test_glucose_browser.py`）。运行真实上传/收集、自动置顶、冲突通用回退、手动覆盖、
切换恢复、原件查看、移动/键盘、实际输出和分享撤销流程。

每个核心检查点保存实际红/绿测试、源码身份及限制，取得有界独审。真实原件执行前另行
冻结范围、独立标注、gold/协议/评分器、源码和唯一运行参数；不把未知页作为阴性，不给
同一开发集冠以留出集，不因变更抽取器而重写既有评分或原始结果。

### Task 8: 验证与交付闭环

PowerShell 使用实际工作区执行以下检查，并按变更范围补相邻测试：

```powershell
python tools/verify_documentation.py
python tools/verify_traceability.py
python tools/verify_release_gate.py
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python -m pytest -q tests/cancer_ordering
python -m pytest -q --ds=config.settings.postgres_test tests/integration/test_cancer_ordering_postgres.py
python tools/run_required_tests.py -q tests/browser/test_cancer_ordering_browser.py
```

PostgreSQL 使用已有专用测试配置和独立数据库，不把真实部署地址写入文档。必跑浏览器工具
的临时 XML 会自动清理；需要持久证据时额外保留真实结果或使用明确零跳过校验的 pytest XML，
不得声称不存在的制品。所有未执行、跳过及真实字段质量缺口单独记录。

在 `docs/verification/` 保存匿名功能/质量/交付证据，同步登记和入口。中文 PR 标题及实际
正文必须用 `tools/check_conventional_commit.py` 校验，检查暂存区和新提交全部父历史后
推送。确切 head 四项 CI 及独审通过后 Squash，核对 Release Please 实际标签/Release 后
补实际版本清单。五批整体验收仍由 Task 10 按全部 Bx-yy 完成。
