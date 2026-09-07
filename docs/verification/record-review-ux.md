# 健康档案与指标核对交互优化验证

本次对应用户提出的第三阶段交互优化：档案列表进入详情、指标提示与操作收拢，以及核对操作的用途说明与保存反馈。
工作分支为 `feat/phase-three-records-ux`，从本次获取的 `origin/main`
（`85dfc5284eba0bd8d41c6420fb3fb3bb03f7f7fb`）创建独立工作树。
功能已在 [PR #31](https://github.com/skuyd/emr/pull/31) 中通过四项 CI 并 Squash 合入 `main`，
提交为 `bc7a8da39d49a2959736bed6aadd89320f11cce4`。Release Please 已通过 [发布 PR #32](https://github.com/skuyd/emr/pull/32)
发布 [v1.3.1](../releases/v1.3.1.md)。本次任务未执行生产部署。
提交前已同步至最新主分支 `a19c47becb5c3125affb5a6bad623fe3684130fa`，兼容主分支新增的隔离体验配置与本地上传修复。

## 交互变化

- 档案标题及卡片主体可打开资料详情；打开原件和重新整理仍为独立操作，搜索来源与结果位置保留。
- 指标按报告顺序紧凑排列，默认显示名称、结果、单位、参考范围、报告标记、核对状态与提示数量。
- 同类数据质量提示按受影响指标数汇总解释；展开指标时查看本项提示与来源，并进入核对与修订。
- 核对页同时展示持久化中文状态与本次保存反馈；刷新或返回资料详情后仍能看到当前状态。
- 核对、标记错误、暂缓、更正、撤销和版本冲突选择都有可见说明；已确认时禁用重复确认按钮。
- 原始识别值、人工修订历史、字段来源、质量检查和趋势准入规则保持原有语义。

## 提示与操作的含义

| 提示 | 含义 |
| --- | --- |
| 字段关联冲突 | 项目、结果、单位等字段的对应关系可能有误，需核对原件同一行。 |
| 识别不确定 | 部分文字或数字识别把握不足，需放大核对。 |
| 标本依据不足 | 尚无足够依据确定血液、尿液等标本类型，限制趋势比较。 |
| 单位不明 | 单位缺失或无法识别，暂不能可靠比较数值；原件有单位时可更正。 |

| 操作 | 实际作用 |
| --- | --- |
| 与原件一致 | 保存当前内容已核对的状态，数值保持不变；不会自动补全缺失依据或清除全部质量提示。 |
| 识别有误 | 标记识别问题，无需填写正确值；会限制比较，不会自动重新识别。 |
| 暂不处理 | 记录暂缓，保留当前内容与已有问题。 |
| 主动更正 | 保存用户填写的更正，保留原识别值和修订历史。 |
| 撤销上次操作 | 恢复上次操作前的内容与核对状态，保留历史。 |
| 授权内部复核 | 授权有复核权限的人员核对本项及所需来源，沿用原有七天有效期与撤回规则。 |

## 验证依据

新增服务端回归在修改前有 7 项预期失败；新增浏览器回归有 2 项预期失败，分别复现标题无链接及单项展开内容占用过高的问题。
修改后上述 9 项均通过。进一步补充了同一指标被不同规则重复提示、重新解析后的版本冲突，
以及手机上卡片重新整理按钮的回归。

最终相关回归 **637 项全部通过，无失败或跳过**，用时约 271 秒；重点复测 61 项通过。
Django 系统检查无问题，迁移检查无待生成内容。初始文档检查通过，登记 59 份 Markdown 文档。
同步主分支后，列表、详情、核对状态与浏览器交互共 83 项复测全部通过，无失败或跳过，用时约 39 秒。
命令、测试统计、代码文件与本地截图哈希见[机器验证记录](artifacts/record-review-ux.json)。

- [列表与搜索归因](../../tests/documents/test_records.py)
- [详情提示汇总](../../tests/documents/test_detail_viewer.py)
- [核对状态、撤销与失败反馈](../../tests/labs/test_record_review_ux.py)
- [桌面与手机浏览器交互](../../tests/browser/test_record_review_browser.py)

浏览器使用本机 Chromium，覆盖 1440 像素桌面和 390 像素手机视口；不代表真实移动设备或其他浏览器验证。
测试使用隔离数据库、合成报告和内存原件存储，不读取用户资料。截图可通过环境变量
`PHR_RECORD_REVIEW_ARTIFACT_DIR` 指定本地输出目录。

```powershell
$env:PYTHONUTF8 = '1'
python -m pytest tests/documents tests/labs tests/accessibility tests/ui tests/browser/test_record_review_browser.py tests/browser/test_viewer_layout_browser.py tests/browser/test_phase_three_browser.py -q
python -m pytest tests/documents/test_document_titles.py tests/documents/test_detail_viewer.py tests/documents/test_records.py tests/labs/test_record_review_ux.py tests/labs/test_phase_two_views.py tests/browser/test_record_review_browser.py -q
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python tools/verify_documentation.py
```

核对交互验证不等于自动提取准确率验证；第三阶段其余范围与既有证据见
[第三阶段验证记录](phase-three.md)。

## 源码交付证据

功能 PR 与发布 PR 的标题检查、完整测试、PostgreSQL 并发回归及容器构建均通过。
提交、CI、标签和 GitHub Release 链接见[源码交付记录](artifacts/record-review-delivery.json)。
[本地开发记录](artifacts/record-review-ux.json)保留初次验证时的快照，其中的合并和发布状态仅代表当时状态。
当前交付结论以本节与源码交付记录为准，生产放行仍以[发布门禁](release-gate.md)为准。
