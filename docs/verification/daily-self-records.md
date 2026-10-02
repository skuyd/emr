# 日常记录改造本地验证

验证日期：2026-10-01；2026-10-02 合入 v4.0.0 主线后复验。PR、合并和发布结果另行记录。本记录只描述本轮需求，旧版的[验证事实](batch-five-daily-records.md)不代替本轮检查。

依据：[需求规格](../specs/2026-10-01-daily-self-records.md)、[实施计划](../plans/2026-10-01-daily-self-records.md)。

## 2026-10-02 提交与发布

用户确认“合并提交”后，通过本地 `submit` 完成精确候选验证、PR Squash 合并和 Release Please 发布。
[修复 PR #145](https://github.com/skuyd/emr/pull/145) 合并为 `6c81231d87b08e15f2ff11889d36f0a471646632`；
[发布 PR #146](https://github.com/skuyd/emr/pull/146) 合并为 `5fe453d0fb9be84e9396b3268529a6c3982243bc`，
[v5.0.1](../releases/v5.0.1.md) 标签和 GitHub Release 已核实。

功能候选 `40dd01ee9c2e` 采用 `planned` 验证：相关回归 **135 通过**、浏览器 **7 通过**，
Django、迁移一致性、文档、版本、追踪和门禁记录校验通过。发布候选 `2e4a00bc0313` 的
发布专项 **136 通过**，契约、合成质量检查、生产镜像构建和 smoke 通过；发布复用已验证
专项基线，`validation_reused=true`、`business_reused=false`。未运行业务全量或 PostgreSQL 并发专项，
也未部署生产。原需求的 PostgreSQL 门禁缺口保持，规格和计划登记为 `implemented`。

[公开验证摘要](artifacts/release-v5-0-1.json)包含远端 PR、Squash、标签身份及本地收据和结果摘要。
本次修复分支及同名远端分支已清理，主工作区、本地证据和其他任务工作区保留。
以下章节保留开发阶段当时的执行事实；其中“未提交”仅描述当时状态。

## 2026-10-02 原型对齐修复

用户反馈正式界面未按已确认原型实现。检查时 `127.0.0.1:8000` 的日常记录 CSS、JavaScript 与当前仓库文件一致；不能将偏差归因于服务器仍使用旧静态文件。浏览器连接不可用，未直接操作用户已登录页面；交互复现与截图均使用隔离测试库的合成患者和记录。

确认并修复的偏差：

- 月历单元格、今天标记、记录摘要、类型筛选与视图切换布局未按原型呈现；记录行缺少类型图标和结果层级。
- 添加页类型按钮未采用等宽卡片；数值与单位上下分离；缺少底部返回入口；ECOG 分级和侧边操作样式与原型不同。按原型调整，窄屏仍将已有记录放在完整表单之后。
- 从列表进入更正时未传递 `view=list`，返回后错误切换日历；现保留浏览模式。
- 第一条保存成功后，第二条保存失败仍显示上一条成功提示；现发起提交时清除旧成功提示，失败保留输入及错误信息。
- 侧边记录补充今天标识，正在更正的条目禁用重复进入。
- 保存更正期间原本仍可点击取消，提前恢复输入后，保存响应会再次退出更正并清空该输入。现将取消按钮纳入保存忙碌状态；成功或失败后恢复可操作，避免打断输入恢复。

修改前真实浏览器复现：数量与单位纵向位置相差约 92px；失败后成功提示仍可见；列表更正返回后列表链接没有选中状态。首次返回测试被链接内箭头影响精确名称定位，修正测试定位后才确认模式丢失，不把定位超时算作业务缺陷。

收尾时另以延迟的真实更正请求复现等待期间取消按钮仍启用；修复后先检查等待期间禁用，再放行真实后端请求，确认原有 70 lb 输入恢复。未伪造成功响应代替实际保存。

修复后的必要检查：

| 命令或检查 | 实际结果 | 范围 |
| --- | --- | --- |
| `python -m pytest tests/self_records tests/browser/test_daily_records_browser.py tests/browser/test_self_records_browser.py -q --tb=short --maxfail=5` | 110 通过 | 布局、返回模式和成功提示修复后的日常记录输入、迁移、服务、输出、权限及真实浏览器交互 |
| 隔离测试库的原型对照截图检查 | 1 通过 | 1280px、360px、320px 的月历、添加页、ECOG；等宽类型按钮、侧边日期与今天标识、手机布局顺序、无横向溢出及无脚本异常 |
| `python -m pytest tests/browser/test_daily_records_browser.py tests/browser/test_self_records_browser.py -q --tb=short` | 7 通过 | 补充保存期间禁用取消后的最终浏览器复测，含底部返回、真实延迟响应及草稿恢复；与 110 项中的浏览器范围重叠，不累加 |
| `python tools/verify_documentation.py`、`git diff --check` | 通过 | 162 份文档登记及差异格式 |

已查看桌面月历/添加页、360px 月历及 320px 添加/ECOG 截图。截图与临时检查脚本仅留在本地 `.runtime/daily-records-alignment/`，不作为共享校验的依赖；永久行为回归保存在 `tests/browser/test_daily_records_browser.py`。独立只读审查及取消更正保护的补充复核未发现本次差异的 P1/P2 阻断项。

本轮不涉及数据库模型或服务层变更，未重跑业务全量或 PostgreSQL 并发专项；不能将上述前端修复结果当作原 PostgreSQL 门禁通过。当前修复尚未提交、合并或部署。

### 顶部导航与间距精简

按用户后续反馈，移除公共模板顶部“首页 · 日常记录”重复链接，将添加/更正页顶部“返回列表”移到标题同行，并收紧页面、标题、工具栏、表单字段和侧栏间距。保留返回、保存及更正操作，未修改数据或脚本行为。

执行 `python -m pytest tests/browser/test_daily_records_browser.py tests/browser/test_self_records_browser.py .runtime/daily-records-alignment/test_preview.py -q --tb=short`，结果 **8 通过**（7 项既有浏览器回归和 1 项本地截图检查）。已查看调整后的 1280px 添加页、320px 添加页及 360px 月历截图；重复链接已移除，标题行与返回入口正常，窄屏无横向溢出。此结果是上述浏览器范围的复测，不与旧通过数累加；本地截图脚本不作为共享校验的依赖。

用户进一步指出顶部空白仍多。完整视口测量确认 `.app-main` 和 `.self-record-page` 顶部内边距叠加：1280px 下任务状态栏至标题为 58.39px，360px/320px 下为 32px；基于测量的最大 16px 间距检查先失败。将日常记录页面外层顶部间距设为 12px、内层顶部间距设为 0，仅作用于包含日常记录内容的主区域。修复后上述尺寸的月历和添加页实测均为 12px；浏览器确认内容区域不存在“首页”及“日常记录”重复链接。完整视口截图检查了顶部导航、任务状态栏和正文之间的关系，未只截取内容卡片。

执行 `python -m pytest .runtime/daily-records-alignment/test_top_spacing.py tests/browser/test_daily_records_browser.py -q -k 'viewport_top_spacing or desktop_sidebar or phone_width' --tb=short`，结果 **3 通过、4 未选中**，覆盖顶部布局与桌面/手机添加、更正、删除交互；测量及截图使用隔离测试库。`python tools/verify_documentation.py` 和 `git diff --check` 通过；改动仍待提交，未部署。

## 首次提交前执行结果

| 命令或检查 | 结果 | 范围 |
| --- | --- | --- |
| `python -m pytest tests/self_records -q --tb=short --maxfail=8` | 103 通过 | 输入、迁移、服务、权限、浏览、输出与关联失效；使用真实迁移 |
| `python -m pytest tests/self_records/test_clinical_integration.py tests/exports/test_imaging_daily_records.py tests/exports/test_pathology_portable_compatibility.py tests/glucose/test_export_integration.py -q --no-migrations --tb=short --maxfail=8` | 37 通过 | 混合选定输出和分享边界 |
| `python -m pytest tests/browser/test_daily_records_browser.py tests/browser/test_self_records_browser.py -q --no-migrations --tb=short` | 5 通过 | 桌面、320px/360px、设备时区、键盘、重试、连续添加、更正、删除确认、ZIP 和分享 |
| `python -m pytest tests/browser/test_imaging_quantitative_browser.py -q --no-migrations --tb=short --maxfail=1` | 1 通过 | 影像与选定日常记录的真实浏览器混合输出 |
| `python -m pytest tests/security/test_csrf_and_idor.py -q --no-migrations --tb=short` | 4 通过 | 方法、患者范围、CSRF 与跨患者动态路由 |
| `python -m pytest tests/integration/test_self_records_postgres.py -q --no-migrations --tb=short` | 14 跳过 | 当前测试数据库为 SQLite，未获得 PostgreSQL 并发运行证据 |
| `python manage.py check` | 无问题 | Django 系统检查 |
| `python manage.py makemigrations --check --dry-run` | No changes detected | 模型与迁移一致 |
| `python tools/verify_documentation.py` | 160 份 Markdown 登记校验通过 | 文档、索引、状态与引用 |
| `git diff --check` | 通过 | 已跟踪文件的空白与补丁格式 |

浏览器整组最初发现直接更正页的修订号 0 被渲染为空值；修复后整组重跑为 5 通过。安全矩阵最初发现删除接口在患者范围检查前返回 400；调整检查顺序后整组重跑为 4 通过。上述通过数均为修复后的最终执行结果。

## v4.0.0 主线集成复验

主线已删除血糖与治疗功能。合并冲突保留日常记录的新输出语义，并移除已删除功能的导出和测试引用。

| 命令或检查 | 结果 | 范围 |
| --- | --- | --- |
| `python -m pytest tests/self_records tests/exports/test_pathology_portable_compatibility.py tests/exports/test_imaging_daily_records.py tests/security/test_csrf_and_idor.py -q --tb=short --maxfail=8` | 125 通过 | 日常记录、混合输出、分享、安全与真实迁移 |
| `python -m pytest tests/browser/test_daily_records_browser.py tests/browser/test_imaging_quantitative_browser.py -q --tb=short --maxfail=3` | 5 通过 | 日常记录交互与影像混合输出浏览器回归 |
| `python manage.py check` | 无问题 | Django 系统检查 |
| `python manage.py makemigrations --check --dry-run` | No changes detected | 模型与迁移一致 |
| `python tools/verify_documentation.py` | 162 份 Markdown 登记校验通过 | 合并后的文档索引与登记表 |
| `git diff --cached --check` | 通过 | 合并提交暂存差异 |

## DR-01～22 追踪

| 标准 | 本轮证据 |
| --- | --- |
| DR-01～03 | `test_daily_semantics.py`、`test_accessibility.py`、浏览器跨时区用例；输入与页面不要求时区或备注，夏令时重复分钟原样保存、重开和跨设备查看。 |
| DR-04～05 | `test_daily_browse.py`、浏览器设备日期/月份切换用例；日历、列表、筛选、跨年与同日多条。 |
| DR-06～08 | `test_payloads.py`、`test_daily_semantics.py`、`test_daily_entry_views.py`、浏览器类型切换及 ECOG 键盘用例；0/5、未选、缺失时间和无效数值。 |
| DR-09～11 | `test_daily_entry_views.py`、浏览器连续添加用例；保存留页、清空值、补记提示、设备时间重置及返回时保留模式。 |
| DR-12～14 | `test_daily_entry_views.py`、`test_daily_service.py`、浏览器侧边读取失败/重试用例；患者、日期、类型范围及重复请求。 |
| DR-15～16 | `test_daily_service.py`、`test_daily_entry_views.py`、浏览器侧边更正用例；同一行更新、草稿恢复、无新增业务修订或旧值副本。 |
| DR-17～19 | `test_daily_service.py`、`test_daily_entry_views.py`、浏览器删除用例；明确确认、取消、失效确认、编辑中删除、不可撤销及输出失效。 |
| DR-20 | `test_accessibility.py`、浏览器 320px/360px/桌面与键盘用例；布局、无横向溢出、焦点和确认操作。 |
| DR-21 | `test_daily_migration.py`、`test_migrations.py`、`test_views.py`、安全矩阵；旧当地时间回填、可用记录及患者权限。 |
| DR-22 | `test_daily_output.py`、`test_export_integration.py`、混合输出与浏览器 ZIP/分享用例；当前选定范围与 ECOG 日期精度、改删后的失效。服务层过期修订号测试通过；PostgreSQL 并发专项在本环境跳过。 |

## 未完成门禁

- PostgreSQL 并发专项需在隔离 PostgreSQL 测试库重跑；本地 SQLite 结果不能替代跨进程锁验证。
- 形成本记录时尚无提交、PR 或发布版本，登记表记录当时的 `implementing` 状态；后续交付状态以登记表及实际发布清单为准。
