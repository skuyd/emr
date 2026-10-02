# 日常记录改造实施计划

> 执行方式：在 `feat/daily-self-records` worktree 中逐项实施；每项先写能复现缺失行为的测试，再修改代码并复测。不得调用 `submit`、部署或手工改版本字段。

**目标：** 在正式应用中完成 [日常记录需求规格](../specs/2026-10-01-daily-self-records.md) 的 DR-01～DR-22，并保留既有有效记录及患者权限、选定输出边界。

**架构：** `DailyRecord` 增加用户填写的 `record_date` 和可空 `record_time`，迁移从旧 `current_data.local_time` 回填；旧 `measured_at` 可空且不再负责日常记录排序。`current_data` 保存当前有效内容，更正不追加 `DailyRecordRevision`；修订序号继续用于并发检查。浏览页面由服务端按月份、日期、类型查询渲染；添加页面用原生 JavaScript 保留当前页输入并调用患者范围内的读取和写入接口。

**技术栈：** Django 5.2、SQLite/PostgreSQL、Django 模板、原生 JavaScript、pytest、Playwright。

**规格：** `docs/specs/2026-10-01-daily-self-records.md`；交互参考 `prototype-gallery/daily-records-preview.html`。

执行结果见[本轮本地验证](../verification/daily-self-records.md)。检查项已执行；PostgreSQL 并发专项在当前 SQLite 环境跳过，尚无该环境的通过证据。

## 全局约束与验收

- 使用用户填写的当地日期和分钟，不要求时区，不用服务器或浏览器时区换算既有数据。ECOG 只有日期，等级 0～5，未选择不得提交。
- 只允许当前患者有写权限的成员新增、更正、删除；同次请求重试幂等，独立的第二次新增允许同日同类型同刻。
- 更正仅修改当前有效值，不追加业务修订事件或提供撤销入口；旧修订数据不做物理批量清理。删除需确认，失效确认和旧页面不能覆盖较新结果。
- 原有选定导出、速查和分享应使用当前有效值、精度与范围；更正、删除和权限变更应使衍生结果失效。
- 版本由 `submit` 后的发布流程决定；本分支不手填版本、Changelog 或发布清单。

## 实施顺序

### 1. 持久化与输入语义（DR-01～03、DR-07～08、DR-21）

**文件：** `apps/self_records/models.py`、`payloads.py`、`forms.py`、新迁移；`tests/self_records/test_payloads.py`、`test_migrations.py`。

- [x] 先写失败测试：时区空缺和夏令时歧义的合法当地分钟可保存；日期/时间缺失或无效被拒；ECOG 0、5 可保存而未选及越界不可保存；旧记录迁移后 `record_date`/`record_time` 等于原始 `local_time`。
- [x] 运行上述测试，确认失败源于旧时间规则和缺少 ECOG/日期字段。
- [x] 实现类型、输入规范化、表单与回填；迁移先加可空列、按旧 `local_time` 回填，再约束日期非空。旧 `measured_at` 只保留历史值，新记录和更正不制造 UTC 时刻。
- [x] 重跑专项测试并运行 `python manage.py makemigrations --check --dry-run`。

### 2. 写入、并发及选定输出（DR-13～19、DR-21～22）

**文件：** `apps/self_records/services.py`、`lifecycle.py`、`exporting.py`；`apps/exports/formats.py`、`pdf.py`；`apps/patients/sharing_content.py`；相关 `tests/self_records/`、`tests/exports/`、`tests/integration/`。

- [x] 先写失败测试：更正不产生新行、原始副本或新修订；删除只保留不可恢复的墓碑且旧修订号不能再写；重试只创建一次，真正第二次添加可创建；选定 ECOG 输出无虚构时刻，改删后预览和分享失效。
- [x] 运行失败测试，确认行为差异。
- [x] 改写更正和删除服务，保留记录锁及权限锁；移除可调用的撤销路由与服务动作；更新投影、速查、CSV、PDF、分享对日期精度和 ECOG 的处理。
- [x] 运行服务、迁移、导出、分享和 PostgreSQL 相关回归。

### 3. 浏览与添加页面（DR-04～06、DR-09～12、DR-14～18、DR-20）

**文件：** `apps/self_records/views.py`、`forms.py`、`urls.py`、`history.py`；`templates/self_records/`、`templates/exports/preview.html`、`templates/patients/shared_detail.html`；`static/js/self-records.js`、`static/css/self-records.css`；页面与浏览器测试。

- [x] 先写失败测试：日历默认今天、月份跨年与可见箭头、视图和类型筛选一致、多条同日记录、ECOG 仅日期、读取失败单独呈现；添加页左右布局及窄屏顺序；类型切换保留输入、侧边异步刷新、连续添加、更正恢复草稿、删除确认和失效确认。
- [x] 运行页面/浏览器测试，确认旧页面不满足验收。
- [x] 服务端渲染月历和列表，按 `record_date`/`record_time` 排序；添加页在当前页内维护每种类型草稿，异步读取侧边记录、保存与更正，删除仅在明确确认后提交。读取和写入失败保留表单并显示错误。
- [x] 运行 Django 页面测试和 320px、360px、桌面真实浏览器流程，并检查键盘操作、无横向溢出及无失效内容。

### 4. 回归与文档收尾（DR-01～22）

**文件：** 旧行为测试、`docs/verification/` 验收记录及制品、`docs/document-registry.json`、`docs/README.md`、本计划和规格的交付状态描述。

- [x] 将旧时区、图表、修订及撤销测试改为当前需求的真实断言，保留既有数据兼容、权限、并发与输出边界的覆盖。
- [x] 运行 `tests/self_records`、相关 `tests/exports`、`tests/integration/test_self_records_postgres.py`、真实浏览器测试、文档校验；按实际环境和结果记录执行范围、通过/失败/跳过。
- [x] 逐项核对 DR-01～22；只有实际证据足够时更新 `delivery`，登记验证文档和引用。运行 `python tools/verify_documentation.py`，检查 `git diff --check` 和暂存区敏感内容。

## 特别复核

1. 旧 `current_data.local_time` 含 `T` 分钟，新 ECOG 只有日期；迁移和输出都不能把 ECOG 变成午夜。
2. 保存后日期重置为设备今天，补记成功提示仍指向原日期；“返回列表”定位已保存月份且保留视图模式。
3. 侧边更正或删除时，未提交的其他类型输入、日期和单位在当前页面内保持。
4. 异步读取失败不显示空态；旧请求晚于新筛选返回时不能污染侧边结果。
5. 旧页面提交的修订号、已删除记录和失效患者权限均不能复活或覆盖内容。

## 2026-10-02 原型对齐修复

用户反馈正式页面没有按已确认原型呈现。修复在最新 `origin/main`（`d6db18f`）建立的 `fix/daily-records-prototype-alignment` 分支进行，不重新定义需求。

1. 对照原型核对月历、记录行、等宽类型卡片、数值与单位同行、ECOG 选项、侧边已有记录及底部返回/保存布局；以 1280px、360px、320px 的实际浏览器截图检查。
2. 先用真实浏览器复现数值与单位错行、下一条保存失败残留成功提示，以及从列表更正后返回错误视图，再修复页面和脚本。
3. 执行日常记录及浏览器回归，核对无横向溢出、手机已有记录位于完整表单之后、旧内容不误显示为新保存结果；结果见[本轮验证记录](../verification/daily-self-records.md#2026-10-02-原型对齐修复)。

本轮仅修改日常记录模板、样式及相关页面状态，不调整数据模型、业务修改历史、权限或数据库内容。未执行提交、合并或部署。

随后按用户“去掉上面的多余链接，尽量紧凑”的反馈，移除日常记录公共模板中的“首页 · 日常记录”重复导航，将添加/更正页顶部返回入口并入标题行，缩小页面、标题、工具栏、表单和侧栏间距。验收继续使用既有浏览器流程及桌面/手机截图，确认返回操作有效、控件可读、无横向溢出。

## 2026-10-02 提交与发布

用户确认合并提交后，修复经 [PR #145](https://github.com/skuyd/emr/pull/145) Squash 合入主线，
随 [v5.0.1](../releases/v5.0.1.md) 发布。正式功能候选的相关回归 135 项和浏览器 7 项通过；
发布候选的 136 项专项、镜像构建及 smoke 通过。结果与范围见[验证记录](../verification/daily-self-records.md#2026-10-02-提交与发布)。
未运行业务全量和 PostgreSQL 并发专项，未部署生产；登记为 `implemented`，不将发布完成等同于 PostgreSQL 门禁通过。
