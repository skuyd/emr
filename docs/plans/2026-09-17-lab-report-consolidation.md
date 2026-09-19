# 检验报告归并实施计划

依据：[已确认规格](../specs/2026-09-17-lab-report-consolidation.md)。
交付状态以[登记表](../document-registry.json)为准；版本尚未确定。

## 工作区与前提

- 用户现已要求实现，覆盖规格中原先仅保存方案的指令。
- 工作分支：`feat/lab-report-consolidation`。
- 独立工作区：`D:/projects/EMR-System/lab-report-consolidation`。
- 已成功获取 `origin/main`，基线为 `bf67701ce1d50384d7340612dd5e37eafd0ccba0`。
- 主工作区未提交的规格、旧规格兼容说明、登记表及索引完整保留；在功能工作区复制这些需求改动。
- 不猜测时间、指标或医院；复用原件坐标、观察值修订与权限机制。历史日期不能升级为采样时分。
- 用户已确认混合页面保留承载有效报告的共享原图；无效单元不做正式提取、不进入结果，并提示。全部拒收时清理临时内容。
- 用户进一步确认同一 PDF 的不同页面混合有效和无效报告时，保留完整原 PDF，排除无效报告并逐项提示。

## 实施与验证

1. 时间证据和报告单元：从明确采样标签提取完整时间及精度、报告号、医院、患者信息和页间证据，支持多报告文件。
   用无效日期、日期无时分、报告时间、低置信度、时间冲突、多报告和续页测试验证 UP-02/04/05、MG-01/04。
2. 临时接收与批次协调：在正式解析前完成有效性识别；等待同批证据，分别接纳、待核对或拒收，清理拒收内容并重试失败清理。
   集成测试验证 AC-01/02/05/06/07/08/09/25/26，以及拒收内容不进入正式档案和输出。
3. 报告身份与人工决定：持久化报告单元、证据、可撤销关联和不可变修订；检查版本、幂等性、重新识别和删除恢复。
   集成测试验证 AC-03/04/14/18/19/21/22/25，检查原图与修订链完整。
4. 统一有效读视图：按日期医院合列、严格相等折叠、保留全部来源和参考差异；趋势使用同院同日最新可信采样点。
   单元与集成测试验证 AC-10 至 AC-20，尤其不换算折叠、不回退到较早结果、未知医院不合列、历史不完整时间排除。
5. 上传、报告详情、对比和核对界面：逐项状态、批次数量、全部报告入口、多来源展开及原件依据修订。
   浏览器验证桌面、手机、键盘和触屏操作，并检查原图数、报告数与展示结果数。
6. 导出与分享：权限及选择过滤先于分组，静态明细保留全部获准来源、时间、冲突和参考信息。
   集成测试验证 AC-23/24、部分来源授权与撤销访问不泄漏数量和最新时刻。
7. 完整验收：逐项核对全部 AC 和生命周期约束；执行相关 Python、JavaScript、浏览器回归、迁移检查及 `python tools/verify_documentation.py`。
   更新规格、索引、登记表和真实验证证据；不得用旧测试记录证明新增功能完成。

## 历史执行记录

下列记录保留各阶段的失败、修复和当时未完成事项；当前结论见文末验收与交付记录。

- 已完成工作区隔离和现状检查。现有采样字段只有日期；上传在有效性判断前创建文档，必须调整接收生命周期。
- 已实现时间证据判定、严格续页时间关联、报告单元/不可变修订/可撤销关联模型及迁移。服务测试覆盖重复决定、旧版本冲突、患者隔离、删除来源、参考差异和保留原件。
- 已实现精确值折叠与最新采样选择；折叠保留全部来源，不通过换算或四舍五入合并，不提高较弱来源的可比性。同医院/方法的最新争议不能回退到较早值。
- `effective_rows` 现已附加报告原件时间上下文并默认排除时间缺失的历史检验；`include_invalid=True` 用于保留核对入口。不重跑历史 OCR。
- 对比表已接入日期医院合列、来源展开、多报告入口和分别计数；主趋势已接入最新采样、医院分组及完整来源明细。原图下载权限沿用现有路由。
- 验证：本轮最终针对性复测 **148 passed**，覆盖新增时间/关联/折叠/历史读取/对比/趋势测试、既有对比优化/趋势回归及桌面键盘与手机触屏的新浏览器测试。输出位于 `docs/verification/artifacts/lab-report-consolidation-progress.xml`，仅代表该测试范围。浏览器测试先前意外同时收集了两个既有高级趋势测试，3 项均通过；随后改为导入别名避免重复收集。
- 迁移检查无遗漏，文档治理校验通过。尚未完成全部 AC，不标记规格为已实现或已验证。
- 已接入临时接收模型与 HTTP `202 / VALIDATING`、批次协调、接纳后入库及缓存 OCR 的正式解析。接纳前不创建文档或处理运行；报告身份在正式解析时持久化。标题缺失但有检验表格的截图也执行时间校验。
- 混合图片和跨页混合 PDF 均保留完整原件字节；永久 OCR、结果与下游提取仅见有效/待核对单元。拒收状态、逐单元原因与共享原件提示已进入上传页、任务卡、详情和查看器。
- 已接入 Celery 与本地 worker 的接收任务及过期租约恢复；临时内容清理失败时隔离重试。配额包含校验中的预留。患者删除会先清理暂存资料；进程在原件提升与数据库提交之间中断后可恢复，废弃的提升对象也纳入清理。
- 本轮接收相关测试 **221 passed**，证据 `docs/verification/artifacts/lab-report-intake-progress.xml`；另执行安全/隐私/上传无障碍回归 **10 passed**，JavaScript **9 passed**。范围包括混合图片/多页 PDF、拒收清理、顺序无关续页、重叠冲突、幂等重复任务、过期租约、进程中断、配额、删除和桌面/窄屏上传提示。此证据不覆盖下列未完成的全部功能。
- 随后用失败测试复现“清理未完成时重试/移除会丢失清理任务”边界，已禁止这两项操作在清理完成前改变上传项。受影响的接收与 HTTP 测试最终 **43 passed**，证据 `docs/verification/artifacts/lab-report-intake-cleanup.xml`。文档治理校验、迁移遗漏检查和差异空白检查通过。
- 已实现报告核对页面：身份依据与原件定位、无报告号时手动选择两份报告、同报告/不同报告/撤销、更正报告字段及不可变审计记录。服务和界面检查患者范围、只读权限、CSRF、旧版本冲突与重复请求。桌面键盘及窄屏触屏测试验证实际来源图片和更正流程。
- 已实现续页时间的当前证据投影：来源删除或取消关联后移出有效结果，来源更正后要求重新核对；重新确认关联后按当前原件依据解析时间。原接纳快照保持不变。导出的文档选择范围外不能提供续页时间。真实接收、正式解析及删除路径已覆盖，不仅测试人工构造的模型。
- 关联刷新由逐对反复读取观察值改为每个来源读取一次；10 张同报告图片的 45 对关系刷新要求最多 40 次查询。
- 用失败测试复现争议日被排除后折线跨日连通，已修复主趋势、表格缩略线与联合趋势断线；联合趋势的明细也按日期范围过滤。报告级时间更正已进入档案及详情日期，缺少完整时间的历史详情改为保留核对入口。对比页的修订关联检查包含无效历史来源，避免重复显示已继承修订。相关高级趋势/对比测试 **41 passed**，详情与身份读取等复测 **37 passed**；仍需下列扩大回归和完整验收。
- 已补上导出的当前报告号、医院、完整采样时间、精度和修订号（JSON/CSV）；只改时刻、日期不变也会使旧输出失效。按观察值选择及分享内容过滤后再次检查续页时间来源，不能保留未选择主报告提供的时刻。相关导出及分享复测 **33 passed**。
- 本轮最终针对性验证 **147 passed**，证据 `docs/verification/artifacts/lab-report-review-lifecycle.xml`，覆盖报告核对、关联查询预算、续页生命周期、接收、详情、高级趋势与对比、JSON/CSV、选择范围和实际浏览器操作。此记录替代同路径先前的 60 项阶段输出，不代表全量验收通过。迁移遗漏检查通过。
- 已实现导出与分享的日期医院分组及结果折叠，原始行和分组来源关联同时保留。选择范围过滤后重新计算报告数、原图数、结果数、最新采样时刻、争议与参考差异；默认分享明细包含所选资料的早期及不同值。JSON/CSV 提供分组与来源关联，PDF 和分享页面逐来源显示完整采样时间、报告号、原始与当前读数及参考信息。关联撤销会使旧快照失效，即使相同结果仍可折叠。
- 已验证实际生成 PDF 的文字与页面边界，以及分享令牌兑换后的页面内容。新增桌面键盘与窄屏触屏测试通过，检查日期医院分组、分别计数、全部采样来源及原图打开。续页现在读取主报告的时间置信度，接纳阈值与更高的趋势阈值仍分别执行。
- 扩大输出回归曾得到 **322 passed / 10 failed**，十项失败均在同一测试夹具读取日期证据时抛出多对象异常。夹具补充类型限定和每页完整采样时间、医院证据后，原有选择范围断言 **15 passed**；未减弱隐私边界断言。完整输出回归仍需以之后的终态结果为准。
- 新增失败测试复现分组输出未沿用既有指标排序，已按选择后的原始行顺序进行折叠；指标排序和输出投影复测 **22 passed**。
- 已补齐无重复标题的同页多报告分段：重复报告号或采样表头必须位于独立结果表格之间，同一表头内的相互矛盾字段不会拆成已接纳报告。含有效与无效单元的无标题混合图片仍完整保留原件，并从永久 OCR 和结果中排除拒收单元。时间、分段、历史读取、关联及接收集成验证 **81 passed**，证据 `docs/verification/artifacts/lab-report-unit-segmentation.xml`。
- 完整输出及相关分享、核对、指标排序回归 **396 passed**，证据 `docs/verification/artifacts/lab-report-output-regression.xml`。该终态记录替代同路径的 322 passed / 10 failed 输出；先前失败与处理原因保留在上面的执行记录中。
- 分组静态输出和分享明细已补回其他页面的字段来源说明。复测浏览器曾捕获 `OperationalError: no such savepoint`：内存 SQLite 的 LiveServer 共用连接被并发请求交错使用。新测试改用仓库已有的 `SQLiteSerializedStaticLiveServerTestCase`，仅内存 SQLite 串行，PostgreSQL 仍使用独立并发连接。实际图像请求检查 HTTP 200 后再验证图片加载。最终输出投影、PDF 及桌面/触屏浏览器 **15 passed**，证据 `docs/verification/artifacts/lab-report-output-focused.xml`。
- 报告级医院更正现在进入档案卡片及关键词搜索，原报告修订与原始识别字段保留。身份读取、档案和既有对比回归 **49 passed**，证据 `docs/verification/artifacts/lab-report-archive-projection.xml`。
- 已使用 EDB PostgreSQL 18.6 二进制包建立仅监听本机的合成测试实例。新增并发测试覆盖重复接收任务、主报告与续页同时识别、并发创建报告关联、竞争决定与幂等重试，首次必跑校验 **5 passed**、无跳过。随后连同既有删除恢复、重新解析、分享撤权与趋势访问扩大验证 **52 passed**、无跳过，证据 `docs/verification/artifacts/lab-report-postgres-concurrency.xml`。这证明所列并发场景，不代表全部 AC 和生命周期边界均已审计。
- 已用失败测试复现无时间续页重新识别后丢失结果，现按当前有效的既有时间关联重新检查同批主报告、报告号、医院、页间证据和重叠结果；不会根据同批关系重新猜测时间来源。新识别、来源校验与持久化在同一患者及处理租约事务保护下执行。主报告删除、撤销关联、报告号变化或结果冲突时，新的拒收单元不进入正式结果，旧解析证据和完整原件仍保留。
- 重新识别仅改变结果 ID、原件依据完全一致时，保留自动或人工关联结论，并写入来源刷新事件、递增核对版本；旧页面仍不能覆盖新版本。报告身份、原件文本、位置、字段依据或质量变化时继续进入待核对。已验证“同一报告／不同报告／已撤销”的人工决定和原审计事件均保留。
- 同一个 PDF 的主页面与续页重新识别时，续页使用本次主页面证据；主页面时间变化后要求重新核对。三页 PDF 的失败用例进一步复现“另一续页仍有效，导致已撤销页被重新接纳”，已逐单元检查原关联是否有效，完整 PDF 保留但被撤销页的结果不恢复。
- 本轮最终接收、报告关联、核对、历史读取、正式解析、质量重识别、处理运行器与输出投影回归 **102 passed**，证据 `docs/verification/artifacts/lab-report-reprocessing.xml`。解析与核对并发扩大验证 **34 passed**，证据 `docs/verification/artifacts/lab-report-reprocessing-postgres.xml`；最终多续页、人工决定和交错操作 PostgreSQL 验证 **12 passed**，证据 `docs/verification/artifacts/lab-report-reprocessing-races.xml`。其中删除主报告的交错测试通过实际数据库等待关系确认事务保护，删除提交后续页排除于有效结果。各集合有重叠，不相加为独立验收数。

- 已用失败测试复现同一报告号的时间、患者信息或重叠结果冲突仍进入主趋势及结果折叠的问题。当前对比、档案详情、指标详情、报告核对、趋势和分组输出均传递冲突；来源明细保留。确认不同报告后重新计算；仅确认同一报告或撤销关联不能消除尚未解决的时间与结果矛盾。
- 历史报告在普通读取时即从已有 OCR 证据建立报告单元及关系，不必先进入核对页面，不重跑 OCR。输出按所选来源重新计算冲突与折叠，序列化相等键保留 `5.0` 与 `5.00` 的精确等价关系。全部结果争议时趋势页保留核对明细，允许记录零个趋势点的页面访问；不会返回 404 或任选一个值。
- 速查卡仅选无争议来源时重新检查报告关系；校验续页时间依赖先于按指标分组，因此已选主报告即使承载其他指标也可提供有依据的采样时间。主报告更正并重新确认关联后，续页的旧接纳时间快照不再被误当作当前冲突。
- 重复投影曾使人工日期与报告采样时间的冲突消失，已用失败测试复现并保留投影前的日期依据；再次读取和导出仍保持待核对。冲突、历史读取、趋势、对比、输出、事件及桌面/窄屏键盘触屏最终阶段回归 **134 passed**，证据 `docs/verification/artifacts/lab-report-source-conflicts.xml`；相关 PostgreSQL 并发、关系和读取检查 **44 passed**，证据 `docs/verification/artifacts/lab-report-source-conflicts-postgres.xml`。临时数据库已停止。扩大回归仍在执行，已出现失败项，尚不能据此认定全部 AC 通过。
- 直接缩小分享文档范围时，投影现先过滤文档、事实与检验来源，再计算分组和冲突；避免更宽快照中的未选来源影响当前结果。新增范围用例及既有检验输出、家庭分享、临床与分子分享回归 **69 passed**，证据 `docs/verification/artifacts/lab-report-conflict-share-scope.xml`。文档校验通过，迁移检查未发现遗漏。
- 首轮扩大回归在达到失败上限后停止：**1064 passed / 10 failed / 1 skipped**，并未执行完整集合，证据 `docs/verification/artifacts/lab-report-conflict-regression.xml`。失败包括已替换的历史元数据测试、日期与来源计数断言、成员上传接纳流程、复核日期显示，以及非检验资料的重试持久化；保留原失败证据，不改写为通过。
- 已将验证与持久化的外层事务限定于检验单元，保留非检验资料原有的未发布重试及审计保护。报告详情使用有效修订值，原自动结果不变；日期核对显示人工记录与报告采样日期的差异。历史元数据回读保留字段候选与原件置信度的较低值，待核对时间不再成为档案的确定日期。成员上传检查覆盖检验和非检验的实际接纳流程及操作人留痕。
- 修改后的接收与真实 PostgreSQL 并发检查 **62 passed**，证据 `docs/verification/artifacts/lab-report-regression-repairs-postgres.xml`；临时数据库已停止。此前跳过的公开照片已按既有验证文档的固定 OpenCV 4.12.0 来源下载到本地临时目录，两个 SHA-256 与测试常量一致；离线识别与恢复 **2 passed**，其余 OCR 模型与原图位置检查 **5 passed**，证据分别为 `docs/verification/artifacts/lab-report-regression-offline-ocr.xml` 和 `docs/verification/artifacts/lab-report-regression-ocr-models.xml`。照片未纳入仓库。处理与患者功能的扩大复测仍在进行，已有新的失败需要核对。

本轮补充验证（2026-09-17）：

- 处理、患者及相关核对扩大复测终态为 **396 passed / 1 failed / 7 skipped**，见 `docs/verification/artifacts/lab-report-regression-repairs.xml`。7 个跳过项均在前述独立 OCR 模型制品中实际通过；唯一失败为家庭迁移测试保留新报告迁移，同时回退其依赖，形成互相冲突的目标。测试历史基线补充检验迁移版本后，家庭、分享、资料分类及回收站迁移 **4 passed**，见 `docs/verification/artifacts/lab-report-migration-regression.xml`。仍未把扩大复测原失败制品改写为通过。
- 失败测试复现“主报告在接纳后、续页首次正式解析前被删除，或报告号/结果被人工更正，续页仍使用旧缓存提取”。首次解析现在在患者和租约事务内重新检查当前主报告；尚未解析的已接纳主报告使用其接纳证据，已有解析版本时不退回旧接纳缓存。主报告原件尚在入库时使用可重试状态，入库后恢复。当前依据失效时排除续页的正式 OCR、指标及其他提取，已保存原件仍保留。
- 每个解析版本仅保存报告有效性的页码、单元序号、状态和原因码；详情及原件页读取当前版本的提示，避免把重新识别后的拒收继续显示为最初已接纳，也不声称已保留的原件未入档。此记录不含拒收识别正文或医疗结果；首次解析仍保留初始混合文件已排除单元的提示。
- 索引阶段失败后的真实重试用例复现未发布报告单元阻止新识别重建。重试现仅清理无人工修订的未发布检验图谱；报告或观察值已有修订时明确终止，保留原件、已发布证据和审计记录。接收、解析、详情及既有分子/病理重试 **71 passed**，见 `docs/verification/artifacts/lab-report-initial-source-validation.xml`；实际详情响应及原因码检查 **5 passed**，见 `docs/verification/artifacts/lab-report-initial-source-display.xml`。
- 首次来源验证及既有 PostgreSQL 并发 **68 passed**，见 `docs/verification/artifacts/lab-report-initial-source-postgres.xml`；最终重试保护及首次解析重点 PostgreSQL **9 passed**，见 `docs/verification/artifacts/lab-report-retry-postgres.xml`。本机临时数据库已停止。上传交互、报告展开、冲突核对及桌面/触屏浏览器 **14 passed**，见 `docs/verification/artifacts/lab-report-lifecycle-browser.xml`；`npm run test:js` **9 passed**。迁移生成检查未发现遗漏。这些集合存在重叠，仍不等于全部验收完成。

报告级修订继承补充验证（2026-09-17）：

- 重新识别在同一原件页面和唯一相同报告区域上继承人工更正字段，保留本次未更正的自动字段。原始识别快照和旧修订记录不改写；区域移动、重复或分段不明确时不按单元序号强行套用旧修订。
- 新自动依据与既有人工修订不一致时标为报告身份待核对，排除自动归并、折叠和主趋势。核对页可对照原件选择沿用人工修订或采用本次识别，操作保存不可变事件及前序修订引用；版本切换后回到同一页面仍检查当前来源令牌，防止旧页面覆盖新决定。
- 当前报告页和资料详情提供已发布的报告修订历史，即使新解析的单元分段发生变化也保留历史核对依据。修复第二个原件来源选择控件没有更新定位框的问题，桌面键盘和窄屏触屏均实际验证。
- 接收、版本继承、关系、读取与输出重点回归 **131 passed**，见 `docs/verification/artifacts/lab-report-revision-lineage.xml`；浏览器 **6 passed**，见 `docs/verification/artifacts/lab-report-revision-browser.xml`；PostgreSQL 含竞争核对与幂等重试 **69 passed**，见 `docs/verification/artifacts/lab-report-revision-postgres.xml`；旧修订数据迁移、唯一区域与既有家庭/分享迁移 **12 passed**，见 `docs/verification/artifacts/lab-report-revision-migration.xml`。这些集合有重叠，不相加计数。迁移生成检查及 JavaScript 9 项通过，临时 PostgreSQL 已停止。
- 扩大回归已启用实际 OCR 模型和固定公开照片，仍在执行且已有失败，尚无终态结论。另用失败测试复现已知原名称和标准指标不一致仍自动归并，正在补齐指标身份核对；不能据此声称已定位真实图片的误识别根因。

## 收尾验证记录

本轮指标身份和当前状态修复：

- 已用失败测试复现原名称为“前白蛋白”、标准指标为“白蛋白”仍自动归并的问题。现按记录所用字典的唯一名称匹配提示“指标身份待核对”，不按数值推断改名；阻止归并和结果折叠。原件核对后的指标选择或名称更正会重新计算，其他字段的修订不能替代指标确认。原自动名称、值及修订链保留。
- 资料详情原先只显示解析时的接纳统计，人工修订继承出现冲突后仍显示已接纳。现按当前报告身份、关联及时间来源投影提示，同时保留不可变的原接纳记录；拒收单元不会被额外的关联冲突覆盖为待核对。当前状态、实际接收解析、指标身份、修订继承与迁移 **74 passed**，见 `docs/verification/artifacts/lab-report-current-review.xml`。
- 首次检验模块扩大验证 **337 passed / 12 failed**，在失败上限处停止，见 `docs/verification/artifacts/lab-report-indicator-regression.xml`。其中 8 项个人变化测试的轻量对象没有新的医院分组上下文；1 项嗜碱性粒细胞测试误用了默认白细胞原名称；2 项旧断言要求争议趋势返回不存在，现按新规格保留无主线的核对页。修正这些测试后相关集合 **106 passed**。另 1 项发布评估明确检测到运行期间应用源码改变，保留此失败，固定源码后的复跑仍在进行。

- 固定应用源码后，检验模块完整复跑 **536 passed**、无跳过，见 `docs/verification/artifacts/lab-report-indicator-regression-recheck.xml`。同一报告的部分重叠来源补充验证独有指标、折叠后的来源总数和静态输出；报告核对、来源展开及桌面/触屏浏览器 **12 passed**，见 `docs/verification/artifacts/lab-report-current-browser.xml`。
- 首次尝试 PostgreSQL 复测时未显式选择专用 Django 配置，实际得到 SQLite **70 passed / 8 skipped**，不能作为并发证据，保留 `docs/verification/artifacts/lab-report-current-review-postgres.xml`。明确使用 `--ds=config.settings.postgres_test` 后相同集合 **78 passed**、无跳过，见 `docs/verification/artifacts/lab-report-current-review-postgres-recheck.xml`；数据库停机日志已确认。
- 新失败用例复现报告身份冲突时导出分组仍显示“范围内”，与对比表“无法对照”不一致。分组参考提示现按所选来源中的当前冲突计算；缩小分享范围、排除冲突来源后恢复逐来源参考判断。输出、分享及真实浏览器 **48 passed**，见 `docs/verification/artifacts/lab-report-reference-projection.xml`。
- 已补齐新报告详情和关联路由的 GET/POST 越权矩阵；并用失败测试复现“缺少完整时间且另有身份冲突”被错误标为待核对，报告列表/详情现保留“不满足接纳条件”，同时单列身份冲突。核对、对比、路由安全及输出 **34 passed**，见 `docs/verification/artifacts/lab-report-review-scope.xml`。
- 跨模块扩大回归终态为 **1245 passed / 10 failed**，达到失败上限而停止，见 `docs/verification/artifacts/lab-report-comprehensive-regression.xml`。本轮实际 OCR 用例没有跳过。10 项失败分别为 2 项旧趋势断言、7 项个人变化测试上下文及 1 项新增路由未登记；修正后的相关集合已通过。当前完整收集 1713 项，保留这次 1245 个通过项，补跑失败、新增及尚未执行的 468 项；补跑尚未结束，不能宣称完整集合通过。

- 补跑终态为 **468 passed / 1245 deselected**、无失败或跳过，见 `docs/verification/artifacts/lab-report-comprehensive-remaining.xml`。将两份 JUnit 的通过用例与本次收集清单逐项匹配，**1713 项均有通过记录，遗漏为 0**；原先 10 项失败记录继续保留。清单、匹配口径及两份 XML 的 SHA-256 见 `docs/verification/artifacts/lab-report-regression-coverage.json`。这是分批回归的合并证据，不是声称原始失败运行全部通过。
- 指标身份校验新增后，又对引用该校验的既有治疗导出、临床导出、快照、资料标题、档案及趋势入口进行当前源码复测，**100 passed**，见 `docs/verification/artifacts/lab-report-indicator-consumers.xml`。JavaScript **9 passed**；迁移生成检查无遗漏，文档治理与差异空白检查通过。
- 用户已确认：同一原件已有人工核对的完整采样时间，但后续重新识别缺少时分时，按本次识别排除有效结果，保留原件和历史核对入口，不沿用旧人工时间恢复本次结果。规格 4.2 已同步。现有运行代码符合该规则，无需修改；新增日期无时分、完全未识别时间两种实际重新解析测试，验证当前 OCR/结果排除、有效视图与趋势为空、导出分享不含结果、原件字节及历史修订不变，且详情保留原件核对入口。连同完整时间仍可继承人工修订的正向场景，**4 passed**，见 `docs/verification/artifacts/lab-report-reparse-missing-time.xml`。其中 2 项为新增用例，先前 1713 项覆盖记录保持原统计口径。

## 验收追踪

以下矩阵记录 AC-01 至 AC-26 的实现与验证位置。逐项复核覆盖接收、来源保留、关系生命周期、展示与输出；执行结果见收尾验证和交付记录。历史失败制品继续保留，不改写为成功。

| 验收项 | 主要验证位置与断言 |
| --- | --- |
| AC-01 | `test_lab_intake.py::test_exact_file_dedup_is_patient_scoped_and_skips_recognition`；重复文件不识别、不增加原件 |
| AC-02 | 同一测试检查患者范围隔离；`test_report_relations.py::test_patient_and_deleted_source_boundaries` 检查跨患者关系 |
| AC-03 | `test_report_comparison.py::test_same_report_rephotograph_counts_one_report_two_originals`；报告和原图分别计数 |
| AC-04 | `test_report_comparison.py::test_overlapping_report_sources_keep_each_unique_indicator_and_every_original`；重叠项折叠、独有指标与原件全部保留 |
| AC-05 | `test_lab_intake.py::test_batch_continuation_waits_for_main_report_regardless_of_arrival`；实际接纳和正式解析 |
| AC-06 | `test_lab_intake.py` 无法关联续页、清理失败隔离及重试；临时内容不入正式档案 |
| AC-07 | `test_report_identity.py` 缺少时分拒收；`test_report_readmodels.py::test_historical_date_only_is_retained_but_ineligible` |
| AC-08 | `test_report_identity.py::test_report_time_on_same_line_cannot_supply_missing_sampling_clock` 及历史日期证据测试 |
| AC-09 | `test_lab_intake.py` 混合图片、无标题混合图片及混合 PDF 测试；完整原件字节不变，拒收区域不提取 |
| AC-10 | `test_report_comparison.py::test_same_day_hospital_column_contains_all_reports_and_folded_sources` |
| AC-11 | `test_result_consolidation.py::test_equal_numeric_formats_fold_but_keep_every_real_sampling_source`；桌面/触屏展开 |
| AC-12 | `test_report_trends.py::test_trend_and_change_summary_use_latest_daily_sampling`；变化摘要与主图使用同一组点 |
| AC-13 | `test_report_trends.py::test_latest_conflict_retains_details_without_a_main_point`；不回退早期值 |
| AC-14 | `test_report_identity.py` 完整时间冲突；`test_report_source_conflicts.py` 跨来源冲突及无主线核对页 |
| AC-15 | `test_result_consolidation.py::test_distinct_or_unreliable_results_are_never_folded`；单位、比较符和标本边界 |
| AC-16 | `test_result_consolidation.py` 数值格式相等与极小差异；输出选择后仍保持精确相等 |
| AC-17 | `test_result_consolidation.py::test_reference_differences_fold_without_a_unified_abnormal_conclusion` 及输出参考差异 |
| AC-18 | `test_report_indicator_identity.py` 已知名称冲突、禁止归并折叠、原图核对入口及人工更正后重算 |
| AC-19 | `test_report_relations.py` 缺少报告号不自动关联；`test_report_comparison.py` 跨报告展示折叠 |
| AC-20 | `test_report_comparison.py::test_unknown_institutions_do_not_share_a_column`；其他页面医院不借用 |
| AC-21 | `test_report_revision_versions.py`、`test_report_readmodels.py` 和真实重新解析接收测试；版本、字段来源及修订保留；`test_reparse_missing_clock_excludes_results_despite_prior_manual_time` 验证新识别缺时分时不沿用旧人工时间恢复结果 |
| AC-22 | `test_report_readmodels.py::test_continuation_loses_deleted_time_source_and_rechecks_on_restore`、关系删除及输出范围测试 |
| AC-23 | `test_lab_report_projection.py` JSON/CSV/PDF 与分享完整来源；实际 PDF 文字边界及浏览器查看 |
| AC-24 | `test_lab_report_projection.py` 选择后重算；`test_report_source_conflicts.py` 收窄文档与观察值范围，不带出未授权时间及数量 |
| AC-25 | `test_lab_report_postgres.py` 独立连接竞争、幂等重试、删除交错；核对页来源令牌和旧版本检查 |
| AC-26 | `test_lab_intake.py::test_non_laboratory_document_does_not_need_sampling_time` 及既有分子/病理流水线重试回归 |

## 整体审查结论

- 接收调用链已检查：公开上传入口统一调用 `register_intake`；`finalize_upload` 的正常新文件调用发生在接纳后，完全相同文件复用原有去重。接纳事务、提升对象补偿、过期租约、清理失败、患者删除及成员权限版本均有对应保护与回归。混合原件保留完整字节，拒收正文在永久 OCR 和各类提取前排除。
- 生命周期已检查：重新识别从原件取得新证据，初次解析才读取接纳缓存，并再次检查续页来源。报告关联依据变化转为待核对；人工修订仅沿唯一相同原件区域继承，分段变化不按序号套用。重新识别完全拒收时仍可在资料详情查看旧修订和原件，本次结果不使用旧人工时间恢复。
- 状态与界面已检查：详情和原件页显示当前有效性；上传批次记录初次接纳结果。拒收状态不会被关系冲突改成待核对。报告字段、关联操作和原件入口保留患者权限、CSRF、来源版本检查；桌面键盘和窄屏触屏覆盖两侧原件定位与来源展开。
- 展示与输出已检查：严格相等折叠保留原始值、全部采样时刻与参考差异；不同值和独有指标不丢失。最新争议不回退早期值，趋势在争议日断线。JSON、CSV、PDF 和分享先过滤获准来源，再计算关系、数量、最新时间及冲突；原件说明不会授予原件访问权限。既有分享格式的兼容入口仍保留。
- 迁移与并发已检查：新增字段保留旧修订默认含义，历史资料按既有证据投影，不批量重跑 OCR。患者事务锁和操作幂等约束经过真实 PostgreSQL 竞争验证。关系计算按报告号候选及既有关系进行，十张同报告图片的 45 对关系刷新有最多 40 次查询的回归约束。

上述审查未发现尚未修复的规格内缺陷。性能证据限于合成查询预算与并发场景，未进行生产容量压测；OCR 验证使用合成资料和固定公开照片，不构成对未提供医疗原图的识别准确率承诺。它们不替代生产发布门禁。

## 交付记录

开发与本地验收范围为本规格的 AC-01 至 AC-26；发布版本由 Release Please 在合并后确定。功能分支保留完整实现、测试与历史失败证据；生产部署另按发布门禁执行。

- 实现提交：`386bb814b53e32b20c682743cacbad023796a8a0`，分支 `feat/lab-report-consolidation`；交付关联 [PR #95](https://github.com/skuyd/emr/pull/95)，实际合并状态及 CI 结果见 PR。生产部署不属于本次合并。
- 固定源码最终验收 **207 passed**、无失败或跳过，耗时 95.44 秒，证据 `docs/verification/artifacts/lab-report-acceptance.xml`。覆盖矩阵对应的时间与身份、接收、关系、比较、趋势、结果折叠、读视图、修订、跨来源冲突、核对界面、输出、安全及三个报告浏览器测试文件。
- 跨模块 1713 项分批通过清单与当前源码 536 项检验模块、100 项下游入口、78 项真实 PostgreSQL 复测共同构成回归证据；最终 207 项包含最新补充的两个缺时分重新识别用例。集合有重叠，不相加计数，不声称原失败运行曾完整通过。
- 最终执行 `npm run test:js`：9 项通过；`python manage.py makemigrations --check --dry-run --settings=config.settings.test`：无遗漏迁移；`python tools/verify_documentation.py`：通过；源码及文档的差异空白检查通过，保留失败 JUnit 原文中的行尾空白。暂存路径检查未包含本地部署文件或凭据签名。
- 覆盖清单中的 `sha256` 对应仓库采用 LF 换行的 XML，`captured_sha256` 保留 Windows 原始采集哈希；仅规范换行，测试结果与失败正文不变。已核对暂存 Git 制品与清单哈希一致。
- 上传接口兼容变化：调用方应处理 `202 / VALIDATING` 并轮询批次状态；采样时间不完整的新检验报告拒收，历史报告保留原件但排除有效结果。内置上传界面和两种 worker 已同步，非检验资料继续正常处理。
- 2026-09-19 合并前复跑相同验收集合：**207 passed**、无失败或跳过，耗时 99.67 秒，见 `docs/verification/artifacts/lab-report-premerge-acceptance.xml`。JavaScript 9 项、迁移生成和文档治理检查通过；待推送提交历史检查未含本地部署资料或凭据签名。仓库完整 Python、必跑浏览器、PostgreSQL 并发与容器构建由 PR CI 执行，未完成的 CI 不提前记为通过。
- 首轮 [PR CI](https://github.com/skuyd/emr/actions/runs/35443861566) 的 Python 集合为 **5072 passed / 35 failed / 4 skipped**；PostgreSQL **395 passed**，标题检查及容器构建通过。Python 步骤失败后，必跑浏览器及 JavaScript 步骤未执行；机器记录见 `docs/verification/artifacts/lab-report-first-pr-ci.json`，未将此运行记为成功。
- CI 失败涉及旧上传夹具仍期待同步入档、PDF 测试使用未锁定的 PyMuPDF，以及有效结果筛选和逐页医院证据变化后的旧测试前提。上传夹具现实际执行接纳后再检查解析、原件及生命周期；PDF 夹具改用已锁定的 pypdf；日期精度测试继续验证历史记录不被改写，并验证不完整时间排除有效结果。相关九个测试文件本地复跑 **129 passed**，见 `docs/verification/artifacts/lab-report-ci-repairs.xml`；本轮未修改应用行为。
- 将首轮 CI 的 35 个失败用例与上述 129 项逐项匹配，均已有通过记录，无遗漏。另补跑首轮未执行的八个必跑浏览器文件：上传场景实际完成接纳，再模拟后续处理失败；模拟请求去除条件缓存头，避免把 `304` 空响应解析为 JSON。最终 **25 passed**、无失败或跳过，见 `docs/verification/artifacts/lab-report-ci-required-browser.xml`。这些本地结果不替代新提交的完整 CI。
