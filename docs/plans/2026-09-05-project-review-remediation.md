# 项目审查修复计划

本计划落实用户批准的当前项目审查建议。保留 Django 模块化单体，修复数据质量、
删除、并发、认证、上传交互和验证可靠性问题。工作分支为
`fix/project-review-delivery`，基于已发布的 `v0.3.1`；
原 `fix/project-review-hardening` 工作区及其已有本地运行说明、验证材料保持独立。
本批修复经 [PR #8](https://github.com/skuyd/emr/pull/8) Squash 合并为
`908cf5430cb5356215d00a435e348250d33e870f`，Release Please 已发布
[v1.0.0](../releases/v1.0.0.md)。本地复验、PR CI 与发布提交 CI 均通过，登记表交付状态为
`verified`；生产部署仍以独立上线门禁为准。

## 实施约束

- 仅使用合成资料和隔离测试数据库；不连接真实短信或医疗资料存储。
- 先为已确认的行为缺陷补充失败回归，再实现修复并验证。
- 不修改自动版本字段或自动 Changelog；生产外部门禁保持真实状态。
- 文档变更同步登记表和总索引，完成时运行文档治理校验。

## 1. 认证与客户端身份

- [x] 使用受信任代理边界解析客户端 IP；认证与上传限流统一调用该入口。
- [x] 启用实际密码验证器，注册和重置拒绝弱密码。
- [x] 密码变更后使其他已经验证的重置授权失效。
- [x] 找回验证码的伪装失败路径执行等价哈希工作；请求流程不暴露同步短信时差。
- [x] 通过账号会话登记定向撤销会话，提供显式历史登记迁移工具。

主要范围：`apps/accounts/`、`apps/core/client_ip.py`、认证回归用例。
验收：不同代理客户端的限流隔离；旧重置授权失效；真实和伪装流程的工作量契约；
默认配置拒绝弱密码；历史会话迁移后可完整定向撤销。

## 2. 原件、删除与并发

- [x] 版本化 S3 删除精确目标键的全部版本及删除标记，并验证没有残留。
- [x] 保留删除任务直到对象删除确认；覆盖分页、部分失败与重试幂等。
- [x] 所有 Web PDFium 调用使用同一进程互斥边界。
- [x] 统一批次、文档、处理记录的加锁顺序，批次集合使用稳定排序。
- [x] 为处理完成与用户删除增加 PostgreSQL 并发回归。

主要范围：`apps/documents/storage.py`、`deletion.py`、`inspection.py`、`previews.py`、
`apps/processing/runner.py` 及存储与并发测试。
验收：版本对象清理完成才返回删除成功；两个并发 PDF 请求不同时进入 PDFium；
完成与删除互相交错时结果一致且不死锁。

## 3. 上传与原件交互

- [x] 修正共享组件的 `hidden` 语义，并为上传重试入口增加状态守卫。
- [x] 创建批次期间冻结列表变更，响应使用提交快照关联文件。
- [x] 修正文件输入的可见键盘焦点。
- [x] 原件下载使用 attachment 响应，查看入口继续提供原件预览。

主要范围：`static/js/upload.js`、上传 CSS/模板、原件响应与浏览器回归。
验收：开始上传前无可交互重试按钮；慢响应期间移除文件不能造成错配；键盘焦点可见；
下载能触发浏览器下载事件。

## 4. 解析、检索与任务资源

- [x] OCR 综合置信度低于 0.80 时仅保留 OCR；标准名和趋势准入使用明确阈值。
- [x] 低置信度历史结果不能继续进入趋势；日期选择考虑来源置信度。
- [x] 搜索使用独立 `Exists` 查询，避免 OCR 与指标明细笛卡尔膨胀。
- [x] 在 Worker 子进程内复用 OCR 引擎，配置变更不会沿用错误模型实例。
- [x] OCR 与删除、通知、恢复使用独立任务队列和执行能力。

主要范围：`apps/labs/`、`apps/processing/`、`apps/documents/archive.py`、Celery 配置。
验收：低置信度合成报告不生成稳定指标或趋势；搜索结果与既有契约一致且明细关联不
相乘；同进程重复任务复用引擎；控制任务不会排在 OCR 队列中。

## 5. 运维、依赖与验证

- [x] 深度 readiness 只供内部检查，探针设置短超时并短暂缓存。
- [x] 修复指标首次并发创建的事务恢复。
- [x] 仅开发配置加载根目录 `.env`，测试和生产配置隔离。
- [x] 生成包含传递依赖及哈希的生产依赖锁，CI 和镜像使用一致的依赖集合。
- [x] CI 添加 PostgreSQL 并发与真实浏览器合成回归；目标用例的意外跳过应失败。
- [x] 按已修复职责提取庞大的 views 模块，维持现有 URL 契约。

主要范围：`config/settings/`、`deploy/`、`.github/workflows/ci.yml`、
`requirements-prod.lock`、运维测试及运行说明。
验收：有开发 `.env` 时全量测试仍可重复执行；生产配置缺失时失败关闭；
依赖可按锁定结果安装；PostgreSQL 并发测试实际运行；公开 readiness 无依赖副作用。

## 6. 集成验证与交付记录

- [x] 执行 Python 全量回归、JavaScript 测试和相关浏览器合成流程。
- [x] 执行 Django 检查、迁移一致性、需求追踪、版本与发布自动化校验。
- [x] 执行 `python tools/verify_documentation.py`。
- [x] 对最终差异进行独立审查，修复实质问题后记录测试结果与外部门禁限制。

参考命令：

```powershell
$env:PYTHONUTF8 = "1"
python -m pytest -q
npm run test:js
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python tools/verify_release_automation.py
python tools/release_version.py check
python tools/verify_traceability.py
python tools/verify_release_gate.py
python tools/verify_documentation.py
```

本计划的复选框仅记录执行日志。当前交付状态以登记表为准；具体验证结果已在
`docs/verification/` 中记录，真实服务与生产恢复演练不能由合成回归替代。

本批验证见[项目审查修复验证记录](../verification/project-review-remediation.md)。
