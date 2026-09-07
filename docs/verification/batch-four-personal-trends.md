# 第四批多指标对照与个人变化验证

本记录仅覆盖[五批需求](../specs/2026-09-07-batches-one-five-requirements.md) B4-03，
详细行为见[多指标与个人变化设计](../specs/2026-09-08-personal-trend-comparison.md)。
源码 `5734db71d98201aa8e0a17cc53f3b867793006e4` 已同步主分支
`6c00f4408252455bac07c657585e560bfe59b429`。本地验证进行中，独立审查和 PR CI 未完成；
本项及五批总体保持 `implementing`，发布版本未确定。

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
此记录不声称真实临床准确率、源码已经发布或[生产门禁](release-gate.md)已经放行。
