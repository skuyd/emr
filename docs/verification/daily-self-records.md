# 日常记录改造本地验证

验证日期：2026-10-01；2026-10-02 合入 v4.0.0 主线后复验。PR、合并和发布结果另行记录。本记录只描述本轮需求，旧版的[验证事实](batch-five-daily-records.md)不代替本轮检查。

依据：[需求规格](../specs/2026-10-01-daily-self-records.md)、[实施计划](../plans/2026-10-01-daily-self-records.md)。

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
