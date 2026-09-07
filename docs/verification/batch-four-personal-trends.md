# 第四批多指标对照与个人变化验证

本记录仅覆盖[五批需求](../specs/2026-09-07-batches-one-five-requirements.md) B4-03，
详细行为见[多指标与个人变化设计](../specs/2026-09-08-personal-trend-comparison.md)。
源码 `3952edfe6e5b2cf1d39bb3b5d5bda1e58e47b282` 已同步主分支
`ddd5d285a20226fa60a3c9af4075c8cdf4894799`。本地验证、独立审查及功能/发布 PR 的四项 CI 已通过；
[PR #45](https://github.com/skuyd/emr/pull/45) 已 Squash 合并，随 [v1.7.0](../releases/v1.7.0.md) 发布。
本项读视图登记为 `verified`，Task 8 与五批总体仍为 `implementing`；最终 PR head、CI、合并及
发布身份见[交付制品](artifacts/batch-four-personal-trends-delivery.json)。

## 已实现及定向验证

多指标选择、日期筛选、独立数值轴、来源、比较表分组折叠/迷你图/内部横向浏览，
以及上次变化、实际每日变化、近三次均值与偏离均有实现。
同日多份、零/负基准、半定量、质量受限和不足样本按各自原因保留，不补造百分比。
原值、换算规则、基线来源与当前解析/人工修订关系可追溯。

| 实际检查 | 结果 | 范围 |
| --- | --- | --- |
| 算术、HTTP 和可访问语义 | 37 passed，7.14 秒 | 15 项算术、19 项高级趋势、3 项既有可访问性 |
| 最终真实 Chromium 必需用例 | 2 passed，无跳过，14.99 秒 | 桌面 1280 px 与手机 360 px，实际原件加载、筛选、独立轴、基线及键盘操作 |
| PostgreSQL 必需并发 | 4 passed，无跳过，10.97 秒 | 总览、单指标、多指标与比较表在构建中撤权后拒绝返回 |
| 冻结源码完整仓库回归 | 1711 passed、2 skipped、53 按标记排除，468.80 秒 | `13e0242`，在换算依据展示修正及非单据功能合流之前；两项跳过为本机无创建符号链接权限 |
| 换算依据修正后的算术/HTTP/可访问性 | 43 passed，9.78 秒 | 40 项公开用例及 3 项独立审查反例 |
| 与非单据功能合流后的近邻回归 | 428 passed，319.28 秒 | 检验、导出、恢复、家庭权限/迁移、安全矩阵及可访问性 |
| 合流后必需 Chromium | 5 passed，无跳过，31.48 秒 | 2 项高级趋势及 3 项非单据恢复流程 |

首次完整回归为 1710 passed、1 failed、2 skipped、53 按标记排除，382.84 秒。
唯一失败是旧可访问测试写死 `trend-points-1` 及 SVG 所在模板；共享图表已按指标生成唯一标识。
测试修正为检查实际响应中的图表与文字列表关联及 SVG 隐藏/链接语义，随后上述 37 项通过。
最终完整回归结果记录在[机器制品](artifacts/batch-four-personal-trends.json)。

更新旧 PRD 的阶段限制后，下一轮的两项需求追踪检查发现原始文档哈希未同步；已核对改动、
更新来源哈希并重新生成追踪矩阵，62 项既有需求及待外部验证范围保持。
同轮七项提交标题检查因本机 Python 子进程未继承 UTF-8 而失败；按 CI 配置设置
`PYTHONUTF8=1` 后，需求追踪与标题的 24 项定向复测通过，未修改或放宽标题校验。

真实浏览器使用合成患者和检验值；原件从实际对象存储接口加载并验证图片内容已完成解码。
手机实测表格内部可横向滚动、页面及主内容无横向溢出；图表在可见宽度内显示所有点。
早期截图暴露图形最小宽度裁掉后部日期和点标记拉伸，调整后重新执行并查看了手机图表。

## 失效与边界证据

单指标、总览、比较表最初在页面构建期间撤权后仍返回 200；新增回归先失败，再补返回前 READ
复查而得到 403。四个入口均以真实 PostgreSQL 独立请求/事务线程和事件握手重现，
等待撤权提交后才释放页面构建，不以睡眠模拟并发。

日期同组重复保留所有点并断线；近期同日另一条刚落在基线窗口外也不择一计算。
当前有效解析切换、更正、标为报告错误及原件删除均触发重新选择基线。
实际单位规则测试将 `2000 cells/uL`、`4`、`6`、`12000 cells/uL` 归为 `2、4、6、12 ×10^9/L`，
核对均值 4、偏离 +200%、上次变化 +6，同时保留原始值、规则与来源。
极大有限 Decimal 曾因模板转浮点将纵轴显示成无穷，先红回归后改用原 Decimal 文本，
保证仍可展示有限数值；没有靠剔除该观察使测试通过。

独立审查发现筛选掉早期记录后，个人基线没有完整显示该记录的换算规则。
三项永久 HTTP 回归先失败，随后在上次依据和每条基线依据中补全换算值、单位、规则编号与版本。
原始独立反例和永久用例共 6 项复审通过（13.28 秒），最终独立 Chromium 2 项通过（23.47 秒）。
独立审查还验证人工更正跨重新解析、保留核对、撤销及切回时，仍打开真实旧字段来源。
审查于 `634c9743f46ebcc58860c873b869bbc08b3ce950` 通过；其后仅合入自动版本文件，
18 份应用源码与复审快照逐项一致。PR CI 及发布结果仍需单独绑定最终 PR。

## 有界计算性能

同一机器对纯个人变化函数、单一可比组的合成日期序列分别运行三次，5000 条的中位耗时
由 6.1340 秒降至 0.1604 秒。实现改为日期排序一次并保留最近三条窗口，保留完整同日数量
用于歧义判断。前后源码哈希、三个规模及逐次测量在机器制品中保留。
这不包含数据库、模板或网络耗时，也不是端到端性能门禁；测量期间存在其他开发测试。

## 可重复执行

```powershell
$env:PYTHONUTF8 = '1'
python -X utf8 -m pytest -q tests/accessibility/test_detail_trend_markup.py tests/labs/test_personal_changes.py tests/labs/test_advanced_trends.py
python -X utf8 tools/run_required_tests.py -q tests/browser/test_advanced_trends_browser.py
# 为本测试单独配置本地 PostgreSQL 的 PHR_POSTGRES_TEST_URL。
python -X utf8 tools/run_required_tests.py -q --ds=config.settings.postgres_test tests/integration/test_trend_access_postgres.py
python -X utf8 -m pytest -q -m 'not postgres and not ocr_model' --ignore=tests/browser/test_ac02_upload_browser.py --ignore=tests/browser/test_upload_interactions_browser.py
python -X utf8 manage.py check --settings=config.settings.test
python -X utf8 manage.py makemigrations --check --dry-run --settings=config.settings.test
python -X utf8 tools/verify_documentation.py
python -X utf8 tools/verify_traceability.py
```

本项没有新增存储表和迁移；既有导出原值、来源、账号与患者生命周期由完整回归覆盖。
B4-01/02 治疗方案、自动周期提议、周期时间轴及叠图仍需独立完成。
Task 8 要求的新派生数据进入选定速查/导出也继续保留：本次完成个人变化读视图，
其派生字段的结构化导出与周期结果在后续 Task 8 集成中补齐，当前不标记整个任务已验证。
此记录不声称真实临床准确率、源码已经发布或[生产门禁](release-gate.md)已经放行。
