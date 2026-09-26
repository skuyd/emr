# 本地 submit 与验证复用实施计划

**目标：** 在 Codex 中一次调用完成本地验证、PR Squash 合并和 Release Please 发布，日常不运行 Actions；保持测试覆盖并消除重复执行。

**依据：** 用户已批准本会话完整方案并要求实施。使用个人 submit 技能、仓库 Python 工具、WSL2 Ubuntu 24.04、Docker 和固定版本 Release Please。保留现有版本规则、分支和 worktree；不部署、不调用 Jenkins，不改其他任务。

## 实施与接口

1. 验证引擎：`tools/submit_validation.py` 导出精确 Git 候选，按源码、命令、依赖和环境指纹复用通过结果；发布差异须逐字段校验。接口 `validate_revision(repo, revision, state_dir, *, mode="full", baseline_receipt=None)` 返回 JSON 可序列化结果，含 `status`、`revision`、`tree`、`receipt_path`。失败抛出异常。
2. 本地环境：`tools/local_validation.py` 在 WSL 中接收 `--archive`、`--output`、`--mode full|release`，把源码解包至 Linux 文件系统，用隔离 Docker 环境执行当前必跑检查，输出 `result.json`；最多两个重任务。`--fingerprint` 返回环境指纹 JSON。不接受或输出 GitHub 凭据。
3. 提交编排：`tools/submit.py` 使用明确的分支、中文标题及正文文件执行；技能负责选择并提交本任务文件。编排器锁定 Git 公共目录，验证功能/发布候选、复核远端、合并、生成标签/Release并保存可恢复状态。调用上述验证接口，纯版本候选可复用完整验证凭据。
4. 迁移与入口：更新 Actions 为手动诊断、退役云端发布入口，同步发布校验与文档规范；创建个人技能。远端切换在本地验证通过后进行，并核查已有工作流、分支规则及 webhook。

## 验收

- 测试先行，覆盖字段级差异、缓存失效、失败/跳过不能复用、合并冲突、主线前进、网络超时后的远端事实核验及恢复。
- 使用临时 Git 仓库和模拟 GitHub API，不为测试制造正式版本。
- WSL 实际执行完整验证；记录分组耗时及 pytest 慢用例，复用结果时不重新运行重测试。
- 运行文档、发布自动化和版本一致性校验；独立审查后再执行远端迁移。

## 执行记录

- 2026-09-26：从最新 origin/main `cdecda3` 创建 `feat/local-submit` 独立工作区；已批准方案不重复要求审批。
- 分工：验证引擎、本地环境、提交编排分别限定文件；主代理负责集成、仓库契约与文档。使用本计划记录进度，遵守 Markdown 必须位于 docs 的仓库规则。
- 验证引擎最终 33 项临时 Git 用例通过，含精确快照、缓存失效、凭据隔离、证据摘要与版本字段级复用。
- 编排 28 项回归及最后的分页 API 2 项专项通过；主线前进恢复、标题/源码版本绑定、版本递增和 Release Please 遗留标签均经测试及独立审查修复。真实 SDK 只读预检与 34 条历史发布 PR 查询通过。
- 本地 runner 与必跑报告持久化用例 28 项通过；发布契约 15 项通过；Windows 启动脚本和旧发布门禁回归 28 项通过。不同阶段集合有重叠，不合并计数。
- WSL Docker Engine 已安装，依赖镜像通过实际 Chromium/git/Python/Node/Release Please smoke。个人技能结构校验及预览/恢复场景验收通过；文档登记 137 项、发布配置和版本一致性校验通过。
- 当前交付：implementing。完整 Linux 集成回归及远端 Actions 切换尚未执行；实际通过证据以后续精确候选验证凭据为准。
