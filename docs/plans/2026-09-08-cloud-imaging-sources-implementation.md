# 云影像来源与受控访问实施计划

目标是完成[云影像来源设计](../specs/2026-09-08-cloud-imaging-sources.md)，并覆盖
[B3-02](../specs/2026-09-07-batches-one-five-requirements.md)及
[临床后续计划第 3 项](2026-09-08-clinical-followup-implementation.md)。本计划已获批准，
PR 1 已通过本地功能验证、独审和精确 head CI，并随 v1.14.0 发布；主线第二次 CI 已成功。
PR 2 已在 PR #68 合并并随 v1.15.0 发布；完整三 PR 仍在实施。设计与 PR 1 起点均基于实际 main
`5b668a1e9c900ee3abfb4e2be7cd65110cc9b4e4`。

## Global Constraints

- PR 1 的已合范围不含外链访问。三次功能 PR 均在前置实际合入后，
  从当时最新 `origin/main` 建独立 worktree，不从未合的功能或文档分支起步。
- 公共文档只写规范与匿名证据；私有临床资料、URL、二维码、日志和评测中间物不进入提交。
  腾讯云本地区域不在本任务范围内。
- 真实 actor 显式传入服务；来源、成员资格、账号与会话、输出对象的当前状态共同决定访问。
  源码接口以各 PR 开始时的实际 main 为准，合流后保留旧数组、迁移和全部文档登记。
- URL/QR 敏感内容默认不参与普通影像投影；这一边界属于 PR 1。PR 2 才开放受控打开，
  PR 3 才开放显式选择和有限分享，各 PR 的已开放能力独立闭合。
- READ 用户只查看核对结果，所有决定需要 WRITE。默认省略只改变输出投影，不改变
  原始 Fact/OCR、私有证据或已确认字段；省略上下文保留标记并清空 offset。
- 旧输出全部消费入口复用访问串规则；技术 URL 白名单只认服务端确认的固定规则，
  不接受用户文本或同名字段伪造例外。
- 不启动远程抓取、探测、登录或付费识别；浏览器外站行为用本地合成拦截，真实原件只做
  已授权本地识别。不可把医院网页是否能打开当成本地来源真实性判断。
- 每个有意义的行为先有失败回归；保留实际红/绿及受测身份。普通、PG、浏览器与真实来源
  质量分别记录，不能将跳过或无真实目标计为通过。
- 提交前检查暂存区，推送前检查所有新增提交父差异；PR 用中文 Conventional 标题/正文
  校验，必要检查与独审通过后由负责合并的代理按精确 head Squash。版本由 Release Please
  实际确定，再单独回填；生产门禁独立。

## PR 1：真实来源与核对

### Task 1: 建立来源、证据、扫描和不可变修订

**结果：** 可以保存 OCR / QR / 人工来源，证明它属于实际患者原页，并保留所有历史决定。

先读 `apps/facts/models.py`、`clinical_readmodels.py`、`apps/documents/models.py`、
`apps/patients/access.py`、`apps/operations/{audit,patient_audit}.py` 和账号删除路径。
新增 `apps/cloud_imaging/{apps,models,services,readmodels}.py` 及正式 migrations；在
`config/settings/base.py` 登记应用。模型与约束按规格第 3 节，不给 QR 伪造 Fact/OCR 来源。

先写合成模型/服务回归：跨患者/文档/页/报告引用拒绝；QR 有 OCR offset 拒绝；OCR 原词与
offset 不一致拒绝；人工证据伪造自动坐标拒绝；追加修订不可更新；同幂等决定唯一、旧预期
revision 冲突。加入既有多患者/分享历史迁移的新增 app 历史 `None` 边界，并保留原身份断言。

实跑迁移到最新及历史回退恢复；迁移不从既有未知原文批量制造已确认云来源。删除/恢复、
作者 SET_NULL 与文档/患者硬删除用服务回归证明，不跳过 FK 或迁移失败。

### Task 2: 本地明文与二维码扫描

**结果：** 受控资料可生成有原页身份的候选，失败与未判断保留，过期任务无法发布。

读 `apps/processing/{preparation,geometry,value_objects}.py`、
`apps/documents/{previews,storage,lifecycle}.py` 的真实接口。新增
`apps/cloud_imaging/{decoding,scan_services,tasks,url_policy}.py`；OCR 和 QR 使用独立证据分支。
不要改动当前 OCR 页策略来假定 PDF 文字层没有二维码；复用真实原页渲染。

用现有本机 OpenCV `QRCodeEncoder_create` 生成合成码；二维码载荷只用保留域名的合成
地址。回归覆盖图片/PDF、文字层 PDF、EXIF、旋转、缩放、多个码、重复结果、坏码、纯文本、
越界/不可逆几何、原件 I/O 失败。渲染图像与原页哈希、像素位置必须实际核对。

扫描 POST 先检查实际 WRITE 身份并冻结文档/页与规则；本地 I/O 和解码在锁外，保存前
复验请求者权限及原件/解析身份。复用现有队列/恢复模式，任务只携带内部 UUID，不把 URL
放入队列、日志或异常。重试不重复发布，失败不覆盖旧扫描或伪造“未发现”。

### Task 3: 原页核对、人工补录与默认投影保护

**结果：** 资料层和报告层可以核对来源；普通影像数据输出不带未选外部访问串。

新增 `apps/cloud_imaging/{forms,views,urls,projection}.py`、
`templates/cloud_imaging/` 和必要静态脚本；从 `config/urls.py` 接入路由。在实际现有
`templates/documents/detail.html`、`templates/facts/report.html` 加来源入口。原件 viewer 的
实现位于 `apps/documents/views/originals.py`，现只识别普通 `evidence`；新增明确的云证据
参数并校验同患者/文档/原页与当前令牌，用已有几何绘制高亮，不能把 QR ID 冒充 OCR ID。
该参数只改变授权患者的页级定位，不授予分享原件权限。给全部 GET 过滤及跨标签页导航
带显式患者范围，并维护 `apps/core/{tenant,route_security}.py` 的固定资源归属。

GET 只读取候选，不触发扫描或决定。所有读内容/表单选项保存所渲染依赖并在 render 后
复验；资料、报告或历史作者发生变化时丢弃旧响应。确认、更正、报告归属、排除、补录和
撤销在统一锁内验证实际 actor、来源令牌及预期修订。

默认输出保护必须覆盖 `apps/exports/{content,clinical,pdf,formats}.py` 和
`apps/patients/sharing_content.py` 的实际消费。新类型始终使用明确允许字段；既有摘录或
字段上下文含访问串时显式省略相关访问内容/上下文，保留省略提示，不能保留伪造的新原文
偏移。原件独立授权不变。普通转换规则的公开引用 URL 与原件访问串区分处理，避免破坏
已有单位换算依据。

永久反例至少包括：同文档两报告，一条明文访问 URL 和一个 QR；普通 imaging、整报告、
字段细选及旧摘录组合的实际 HTML/JSON/CSV/PDF/分享中均无未选载荷；已有来源上下文里的
同一访问串也无旁路。此 PR 不新增对外打开或显式分享控件来规避默认拒绝。

本 PR 验收加实际桌面/手机原页核对闭环及独立 PostgreSQL：扫描构建中撤权、原件删除或
parse 切换后禁止发布，两个提交只落一次决定，作者清理与来源读写不死锁。完成独审和
精确 head CI 后才合并；本 PR 交付不代表受控打开和显式输出已完成。

## PR 2：受控打开与访问审计

### Task 4: URL 校验与只接受内部来源的打开服务

**依赖：** PR 1 实际合入 main。**结果：** 已确认且当前的来源才能主动跳到外部站点。

扩展 `apps/cloud_imaging/{url_policy,services,views,urls}.py`，以内部来源 ID、当前令牌和
实际 actor 为接口，不接受客户端任意目标。合法 URL 不改变访问参数语义；校验失败返回
安全的稳定原因。补校验白名单与反例：空/相对/协议相对地址、脚本/file/data、嵌入凭据、
控制/换行/反斜杠/双向文字、非法主机/IP/端口、IDNA 展示、query 中看似新 URL、合法参数
与 fragment 不被重排。所有用例无网络访问。

新增 GET 说明页和 POST 打开；页面显示站点、访问性质和已确认状态。使用 CSRF、无缓存、
no-referrer、隔离 opener；不把完整地址或参数放进预加载 href、data-* 或客户端日志。
构建响应后最终来源/授权复验，才交付外部 Location。禁止把“打开已发起”记成医院响应成功。

浏览器使用同源 fetch POST 的 200 空正文/Location 导航响应，不跟随外部重定向；用户
主动预开的空窗先隔离 opener 和 Referer，校验成功才导航，拒绝或网络失败关闭。原生
直接 POST 仍返回 303。通过真实 TLS 验证正确 Origin 和 CSRF，无 JS 明确禁用打开并
保留返回原页能力，不用接纳空 Origin 解决无 Referer 原生表单的浏览器限制。

### Task 5: 审计、异常和浏览器闭环

**结果：** 成功/拒绝有真实可筛选审计，敏感访问内容不进入日志或错误报告。

接入 `apps/operations/{audit,patient_audit}.py` 的实际入口映射与其中
`PatientAuditMiddleware`。新增动作/资源归属映射、中文管理筛选标签与动态 auth route 矩阵。
扫描/决定/查看/打开各一次访问事件，原件/流式内部反复检查不重复记录。

回归实际失败请求、模板错误与服务异常，检查审计、捕获日志和错误报告构造结果无原始
URL/QR/Location/敏感局部变量，同时仍含稳定原因和请求身份。READ 与 WRITE 角色、患者
切换、账号注销、来源或报告变化须覆盖说明页和 POST 打开。

Playwright 使用合成外站请求拦截验证实际请求目标、没有预加载/Referer/opener，桌面和
360px 页面核对、返回、编辑和打不开后的修订路径可用。PG 两连接在最终响应前实际提交
撤权/修订，证明旧目标不会交付。原件回源继续是本系统权限路由，不能把医院页面当原件。

## PR 3：明确选定输出和有限分享

### Task 6: 来源投影、真 FK 和全部输出格式

**依赖：** PR 2 实际合入 main。**结果：** 独立或混合明确选中的来源可纳入输出，默认选择为空。

新增 `apps/cloud_imaging/{exporting,output}.py` 及输出 FK migration；接入实际主线
`apps/exports/{content,forms,pdf,formats,services,views}.py` 和 `templates/exports/`。
`build_snapshot` / `assert_snapshot_current` 签名保持；输入采用 `cloud_source_ids`，材料和
全部私有依赖在权限/锁内生成。空 documents 时可有显式独立来源，不能伪造 Document。

按规格建立 `cloud_imaging_sources`、`cloud_imaging_evidence` 数组及 CSV 关系。明确选定
来源才带当前访问 URL；普通影像投影的省略规则保持。只允许当前有效来源，不能把部分
失效选择悄悄丢弃。编辑者可导出，Viewer 不得通过新路径升级为 EXPORT。

逐一读取实际 PDF 文本、JSON/CSV 表和 ZIP 成员，验证选定来源与证据对应、URL 转义和
CSV 公式防御、无未选载荷/历史/上下文、没有隐含整份原件许可。混合包同时保留实际 main
全部旧数组及 ID/FK 关系；不靠生成文件存在就宣称内容正确。可移植 schema 取当时主线的
下一合法版本，兼容测试覆盖所有实际已发布旧版本，不在本计划猜测号值。

### Task 7: 精确分享与全部失效阶段

**结果：** 选定来源获得有限访问，任何来源/权限变化都不能从旧页面、任务或流继续输出。

接入 `apps/patients/{share_forms,share_views,sharing,sharing_content}.py` 与分享模板。
显式 `cloud_source_ids` 独立于普通临床章节；报告/字段细选冲突按合同拒绝。共享快照只含
安全摘要和私有依赖身份；原 URL 从真实绑定在受控 POST 打开时读取，不埋在分享 HTML。

源级分享 GET/POST 使用精确 share、actor、session、expiry 和 source 资格，不能调用
家庭 READ 来代替。默认 24 小时、最多 7 天、必须登录及成员修订绑定沿现有规则。
普通详情、原页、未选云来源及完整原件路由的真实 HTTP 反例全部拒绝；显式另选原件时
仅那份原件开放。选择来源不能把细选报告的其他字段带出。

用真实生命周期服务验证排队前、构建中、存储发布、HTML 渲染后、首字节和每块前后失效。
覆盖 URL 更正、报告归属/排除、资料回收和恢复、parse 切换、绑定丢失、账号/作者清理、
角色变化、会话撤销和期限边界。绑定与快照需清空，旧临时文件可清理；恢复/撤销不重开。
跨领域部分作者 UPDATE 尚未 COMMIT 的 PG 场景必须验证，保留主线的正确锁顺序。

## 验证、审查与交付

新增永久测试拟放在 `tests/cloud_imaging/`、`tests/integration/test_cloud_imaging_postgres.py`、
`tests/browser/test_cloud_imaging_browser.py` 与对应 `tests/exports/`；名字可依各 PR 测试边界
拆成有意义文件。先检查实际 `run_required_tests` / 浏览器选取工具，再把需要必跑的场景
纳入当前机制，不用被跳过的普通套件代替真实浏览器/PG。

通用检查命令按实际功能变化选择，下面命令从所用功能 worktree 运行：

```powershell
$env:PYTHONUTF8 = '1'
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python tools/verify_documentation.py
python tools/release_version.py check
```

有变更的 Django/URL/导出/权限/迁移测试先定向跑，修复新边界后再按项目 CI 选择全量。
PG 和浏览器记录精确环境、命令、退出码、无跳过与源码 Git/checkout 身份；Windows 换行
与 Git blobs 分开记录。真实本地识别若有明确目标，先冻结人工存在性/归属和未判断协议，
独立核对后再识别，保留失败与零候选；对外只发匿名总数和哈希，不发链接或二维码。

每个 PR 形成独立证据和源码审查，不把上一 PR 或某个增量复审当作整个 B3-02 已完成。
源代码发布后以实际 tag、Release、功能/发布 CI 回填版本清单；原五批状态与生产门禁不被
单项通过自动改写。

## PR 1 当前执行证据

Task 1—3 已在 `cfcd4e4ecd77af62c768a00a9d5f4fd283c4e7e8` 实现。实际永久入口为
`tests/cloud_imaging/`、`tests/integration/test_cloud_sources_postgres.py` 和
`tests/browser/test_cloud_sources_browser.py`，云来源浏览器已经加入必跑 CI 选择。
当前定向普通 141、真实 PG 14、桌面/手机浏览器 2 均通过且无跳过；此前 `2d39d18`
全量及原失败证据保留，不能称为当前提交重新执行的全量。无效 POST 表单与渲染材料
不一致的独审问题已用原 HTTP/正常 COMMIT PG 反例关闭。

唯一批准的首次真实本地扫描已完成 64 文件/124 页，完整预测和匿名原评分保留；
108 页存在性金标未知，检测、定位和载荷仍有质量缺口。文献字面清单不等同云影像门户
金标，源文件之间保持独立患者容器，未访问真实外链。详见
[PR 1 验证记录](../verification/batch-three-cloud-imaging-pr1.md)。不更改金标或评分协议，
本次交付整理不再次运行原件扫描。

PR #66 和 Release Please PR #67 已通过各自精确 CI 并合并，版本确定为 `1.14.0`。
后续 PR 2 按已经合入的最新 `origin/main` 建立独立工作区；后续任务、完整真实质量和
生产门禁不由本次局部验证代替。原账户阻塞证据保留；自动发布第二次执行已成功创建
标签和 Release，主线第二次 CI 随后成功，见 [v1.14.0 清单](../releases/v1.14.0.md)。

以上为 PR 1 的本地证据范围。PR 1 之后已经实际合入 main，PR 2 新工作区以再次 fetch 的
`9675f0e3f61f96eb4895c364229b9da9d8a27bdb` 为基线，未从旧功能或未合文档分支起步。

## PR 2 本地执行证据与合并

Task 4 及完整打开路径必需的 Task 5 已在 `6d6315b4ccdbe21553c40188020c006d15fe1efa`
实现。该初始应用 75 项定向、最终 22 项 HTTP、18 项 PG 和 6 项实际 TLS/PG Chromium 通过，
均无跳过，原失败全部保留；范围重叠不加总。受控打开浏览器已加入 CI 必跑选择，当前
该阶段仅有本地执行结果。完整合同、准确身份与限制见[PR 2 验证](../verification/batch-three-cloud-imaging-pr2.md)。

完整非作者审查另跑 18 PG/6 TLS 通过，并发现异常链 P2；`bcf8027` 完成有界修复，
22 项独立定向验证关闭该问题。原源码身份、全部失败及旧公共制品保持，修复制品另行追加。

[PR #68](https://github.com/skuyd/emr/pull/68) 随后已合并为
`61dbbc8835702bd998272a036152ea2ba466cede`；原本地验证及账号阻塞记录保持原字节。
Task 6—7、完整真实质量和生产门禁保持待办，
不能由受控打开局部通过代替。PR #68 精确 CI 后续已通过，Release Please 已发布 v1.15.0，
见[版本清单](../releases/v1.15.0.md)及[追加发布制品](../verification/artifacts/release-v1-15-0.json)。
没有重复真实原件扫描或手工修改自动版本字段。

## PR 3 当前执行证据

PR 2 已随 `61dbbc8` 实际合入。Task 6/7 作者实现为 `ecb0932`，
实际 TLS 原生表单修正为 `084e29a`，第一阶段独审三项 P2 的作者修订为 `82ad10e`。
已从原独立功能分支整合实际病理主线 `4b73d2e`，冻结头 `89909da` 的相关普通 641 项、
PG 34 项和真实 TLS 2 项均通过、零跳过；五个 PG 标记项目从普通配置排除。
来源独选、混合文件、真绑定、精确分享与全阶段检查已完成非作者复审，
见[PR 3 验证记录](../verification/batch-three-cloud-imaging-pr3.md)。
原失败、主动中止的非通过记录和全部旧封存／独审证据保留。
格式号按交付前实际 main 再核对，不预定应用版本；三 PR 整体保持 `implementing`，
没有运行新的真实资料评估，不改变 PR 1 已记录的质量限制或生产门禁。

后续 `bf64c41` 修正六个测试文件的 actor 或 writer 版本预期，应用授权逻辑不变；
完整 CI 成功：普通 3544 通过/4 跳过、PG 215、必跑浏览器 18、JS 9 通过。
原失败及定向修复复审保留；合入已发布文档后仍需核对最终提交的 CI，不能把旧通过重标。
