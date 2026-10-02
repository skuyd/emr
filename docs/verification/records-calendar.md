# 健康档案日历验证

## 范围与状态

2026-10-02，用户确认在健康档案中增加默认月历，并保留列表切换。
本轮在独立功能分支 `feat/records-calendar` 开发，基线为获取远端后的
`origin/main`（`d6db18f`）。功能已通过 [PR #148](https://github.com/skuyd/emr/pull/148)
Squash 合并为 `7fc8cdcc4f3c73f766b918a92d4d93f1a489a0ce`，并随
[v5.1.0](../releases/v5.1.0.md) 发布。证据登记为 `active / verified`，未部署。

## 2026-10-03 提交与发布

最终功能候选 `d3bf11176f8f` 的相关 Python 1522 项、浏览器 84 项、JavaScript 5 项、
PostgreSQL 164 项通过；契约、Django 和迁移一致性检查通过。此前的全局搜索兼容问题
及旧列表浏览器操作流程均已修复并通过正式候选验证。

[发布 PR #149](https://github.com/skuyd/emr/pull/149) 的候选 `7faeaebaec37` 通过发布专项
136 项、合成评估、生产镜像构建和 smoke；发布 Squash 为
`088b358524952b4acb5490d17eceebcaa5a9be5b`，标签与 GitHub Release 已核对。
发布阶段复用专项验证基线，`validation_reused=true`、`business_reused=false`，
不代表业务全量通过。用户选择相关测试并接受共享样式及外部浏览器未覆盖风险；
外部 Chrome/Edge/Safari TypeScript E2E 未运行，生产部署仍受发布门禁约束。

[发布验证摘要](artifacts/release-v5-1-0.json)记录实际身份、分组结果和收据摘要。
本次功能分支、本地及远端开发分支和开发 worktree 已清理；本地预览资料与验证日志已保存。

## 验收标准

- 默认显示当前月份与当天；支持上个月、下个月、今天和年月输入，边界为 1900—2100 年。
- 月历按周一到周日显示资料数；选择日期后桌面右侧、手机下方显示当天资料。
- 一个上传文件可以出现在多个可靠报告日期；同日只计一次，本月总数按文件去重。
- 只有年、月或未知日期的资料另列。年/月精度按对应年月显示，完全未知日期持续可见。
- 临床报告已更正、已排除或存在冲突的日期不能由旧摘要日期重新进入日历。
- 搜索、类型、整理状态、患者和选中日期在筛选、日历/列表切换中保留。
- 日历视图中的搜索保留已选月份和日期；列表视图支持跨月搜索，切换视图时保留已有条件。
- 当天与未定日期资料分别每页 20 份，翻页保留各自页码，日历数量不受分页影响。
- 资料详情、原件和原有列表行为继续可用；无权访问及删除的资料不进入统计或卡片。

## 开发阶段验证记录

- 改动前：档案与标记测试 24 项通过。
- 首轮新月历测试在缺少月历上下文处失败，实施后 9 项通过。
- 列表往返丢失日期、两组分页相互重置均先复现失败；修复后月历上下文 10 项通过。
- 首轮档案相关回归为 384 通过、5 失败；失败均为历史列表测试仍访问默认入口。
  默认入口改为月历后，列表测试已改为显式 `view=list`；随后相关回归 470 项通过。
- 现有资料卡片与整理浏览器回归 6 项通过，见
  [现有浏览器结果](artifacts/records-calendar-existing-browser.xml)。
- 独立审查发现筛选表单丢失日期及临床日期被旧摘要覆盖的问题，已增加对应回归。
  DAY 摘要的冲突、更正、排除 3 项先失败，修复后通过；合法 YEAR/MONTH/UNKNOWN
  的 3 项随后复现转换异常，已按实际精度修复。限定复审通过。
- 最终[日期与月历专项](artifacts/records-calendar-dates.xml) 18 项通过。
- 最终[新日历浏览器](artifacts/records-calendar-browser.xml) 2 项通过，覆盖选日、键盘、
  非今天日期筛选、视图切换、上下月、日期边界、详情与原件导航；已检查桌面 1440px、
  手机 390px/320px 截图，未发现横向溢出。原件内容加载另由既有浏览器回归验证。
- 最终[相关综合回归](artifacts/records-calendar-regression.xml) 475 项通过。
- 较早候选 `39dc7ba` 的 Python 回归为 1519 项通过、2 项失败、4 项 deselected。已将原列表日期筛选测试明确为 `view=list`，并修复未指定视图和日期的全局搜索入口被当前月份限制的问题。
- 正式候选 `2e58007`：Python 1522 项通过、4 项 deselected（1342.79 秒）；PostgreSQL 164 项通过、4 项 deselected（519.77 秒）；浏览器 83 项通过、1 项失败（852.12 秒）。失败用例从默认月历搜索并期待多个历史月份的卡片；与已确认的“日历搜索保留所选月份”不符。适配后通过真实 UI 切换列表、清除条件再搜索，生产代码保持不变。[修复后的浏览器聚焦复验](artifacts/records-calendar-lesion-browser.xml) 3 项通过。此证据覆盖适配用例及日历浏览器；当时完整提交验证尚未结束，最终结果见上文。
- 搜索词非空且没有 `view`、`year`、`month`、`date` 参数时使用列表结果；日历页面内搜索继续保留当前模式和日期。搜索兼容聚焦回归[14 项结果](artifacts/records-calendar-global-search-pytest.xml)覆盖日历参数、列表历史月份搜索、检验日期归档和病灶来源搜索。
- Django 系统检查无问题；文档治理校验通过（164 份登记文档）。
- Playwright 外部环境 E2E 仅完成 `test --list` 收集（54 项），未连接外部环境运行；
  本地真实浏览器验证使用上述 pytest 浏览器用例。

复现命令：

```powershell
python -m pytest tests/documents tests/labs/test_report_readmodels.py tests/facts/test_clinical_views.py tests/facts/test_molecular_search.py tests/accessibility tests/patients/test_family_access.py tests/patients/test_family_resource_navigation.py -q --junitxml=docs/verification/artifacts/records-calendar-regression.xml
python -m pytest tests/documents/test_records_calendar.py tests/documents/test_records_calendar_reports.py -q --junitxml=docs/verification/artifacts/records-calendar-dates.xml
python -m pytest tests/browser/test_records_calendar_browser.py -q --junitxml=docs/verification/artifacts/records-calendar-browser.xml
python -m pytest tests/browser/test_record_review_browser.py tests/browser/test_material_browser.py -q --junitxml=docs/verification/artifacts/records-calendar-existing-browser.xml
python manage.py check --settings=config.settings.test
python tools/verify_documentation.py
```

上述开发阶段测试集合有重叠，不累计为不重复用例总数。该阶段未运行完整业务、PostgreSQL
专项、外部环境 E2E 及生产门禁；正式提交阶段已补齐本次相关 PostgreSQL 专项，结果见上文。
本轮没有数据库结构变更。

测试实现见 [月历行为](../../tests/documents/test_records_calendar.py)、
[报告日期](../../tests/documents/test_records_calendar_reports.py) 和
[浏览器交互](../../tests/browser/test_records_calendar_browser.py)。

本记录不代表业务全量、外部环境门禁或生产部署通过。

## 月份按钮样式对齐

2026-10-02，根据用户预览反馈，将健康档案的上个月、下个月文字按钮改为与日常记录
一致的圆角描边 `‹` / `›` 箭头按钮，保留原有无障碍名称、月份链接和边界处理。
粗指针设备保留 44px 触控尺寸。本次仅调整模板与按钮样式。

`python -m pytest tests/browser/test_records_calendar_browser.py tests/accessibility/test_records_markup.py -q --junitxml=docs/verification/artifacts/records-calendar-navigation-style.xml`
实际结果为 4 项通过，见[样式复测结果](artifacts/records-calendar-navigation-style.xml)；
桌面与手机截图已检查，独立样式复核通过。475 项功能回归记录对应此前功能实现，
本次样式变更后没有重复运行该整批测试。分支本地预览服务已重启，当时改动尚未提交，最终发布结果见上文。

## 截图工具栏样式

2026-10-02，按用户提供的截图，将月份导航与视图切换移到日历卡片上方：左侧依次为
上个月箭头、无空格的粗体年月、下个月箭头、今天；右侧为独立圆角的日历/列表按钮，
当前视图使用深绿色实心样式。下一行显示“本月 N 条记录”，计数仍按本月文件去重。
箭头尺寸为 44×50px；窄屏允许视图切换换行，保留原有筛选、日期边界和无障碍名称。
新增主题颜色变量承载截图中的深绿色，仅用于当前视图按钮。

`python -m pytest tests/browser/test_records_calendar_browser.py tests/accessibility/test_records_markup.py -q --junitxml=docs/verification/artifacts/records-calendar-toolbar-style.xml`
最终结果为 4 项通过，见[工具栏复测结果](artifacts/records-calendar-toolbar-style.xml)。
已检查桌面 1440px 和手机 320px 截图；浏览器测试同时覆盖 390px 与 320px 无横向溢出。
独立布局复核通过。本次仅调整模板、样式及相应月份标题断言，没有重跑此前 475 项功能回归。
本地分支预览已重启，当时代码尚未提交，最终发布结果见上文。

用户随后反馈工具栏字号与翻页按钮偏大。对照主工作区正在运行的日常记录样式，
将工具栏按钮改为 14px / 400 字重、42px 高、10px 圆角，翻页按钮改为 36×42px、
24px Arial 箭头；触屏仍保留 44×44px。月份字号维持 16px，字重从 750 降至 500，
本月计数调整为日常记录使用的 12px。日历/列表保持独立按钮并靠右，窄屏换行后也靠右。
本次只修改档案 CSS，未修改主工作区日常记录代码。

`python -m pytest tests/browser/test_records_calendar_browser.py tests/accessibility/test_records_markup.py -q --junitxml=docs/verification/artifacts/records-calendar-compact-toolbar.xml`
实际为 4 项通过，见[紧凑工具栏复测](artifacts/records-calendar-compact-toolbar.xml)。
桌面和 320px 截图已检查，按钮尺寸缩小、视图切换靠右；390px 与 320px 的无横向溢出
检查通过。预览服务已返回最新 CSS，当时改动尚未提交，最终发布结果见上文。

## 页面入口精简

2026-10-02，按用户反馈删除“按报告日期整理”、可见标题“收好的健康资料”及其说明，
浏览器标题简化为“健康档案｜健康之家”，页面区域保留无障碍名称“健康档案”。
“就诊准备与导出”和“回收站与恢复”分别改为“导出”“回收站”，与“上传资料”合并为
顶部靠右的一排紧凑按钮；手机端取消上传按钮单独占满一行。三个入口仍使用原患者作用域链接。

```powershell
python -m pytest tests/browser/test_records_calendar_browser.py tests/accessibility/test_records_markup.py tests/accessibility/test_shell_markup.py::test_authenticated_page_titles_use_current_health_home_brand tests/documents/test_records.py::test_archive_card_exposes_required_metadata_and_original_action -q --junitxml=docs/verification/artifacts/records-calendar-compact-actions.xml
python -m pytest tests/exports/test_views.py::test_preview_generate_download_and_cancel_page_flow -q --junitxml=docs/verification/artifacts/records-calendar-export-entry.xml
```

[入口与页面复测](artifacts/records-calendar-compact-actions.xml) 11 项通过，
[导出流程复测](artifacts/records-calendar-export-entry.xml) 1 项通过。
桌面及 320px 截图已检查，三个操作按钮保持同排，无可见大标题或说明；日历/列表保持靠右。
浏览器用例的 390px 与 320px 无横向溢出检查通过。分支预览服务已重启，当时代码尚未提交，最终发布结果见上文。
