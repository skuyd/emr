# 本地 submit 与验证复用实施计划

**目标：** 在 Codex 中一次调用完成本地验证、PR Squash 合并和 Release Please 发布，日常不运行 Actions；保持测试覆盖并消除重复执行。

**依据：** 用户已批准本会话完整方案并要求实施。使用个人 submit 技能、仓库 Python 工具、WSL2 Ubuntu 24.04、Docker 和固定版本 Release Please。保留现有版本规则、分支和 worktree；不部署、不调用 Jenkins，不改其他任务。

## 实施与接口

1. 验证引擎：`tools/submit_validation.py` 导出精确 Git 候选的源码归档及仅含当前提交、树和文件对象的 Git pack，按源码、命令、依赖和环境指纹复用通过结果；发布差异须逐字段校验。接口 `validate_revision(repo, revision, state_dir, *, mode="full", baseline_receipt=None, base_revision=None)` 返回 JSON 可序列化结果，含 `status`、`revision`、`tree`、`receipt_path`。`docs` 模式必须给出主线基线并证明只含允许的文档差异；文档凭据不能冒充完整回归凭据。失败抛出异常。
2. 本地环境：`tools/local_validation.py` 在 WSL 中接收 `--archive`、`--git-pack`、`--revision`、`--output`、`--mode full|docs|release`，把源码解包至 Linux 文件系统，并建立不含宿主配置或父提交历史的隔离 Git 快照，输出 `result.json`。文档模式直接用 Python 执行文档、追踪、门禁记录和版本检查，不准备或调用 Docker。其他模式用 Docker 执行当前必跑检查；完整模式仅普通 Python 与 PostgreSQL 两组隔离并行，其他步骤和发布模式保持顺序，最多两个重任务。`--fingerprint` 返回对应模式的环境指纹 JSON，无需初始化 Git 快照。不接受或输出 GitHub 凭据。
3. 提交编排：`tools/submit.py` 使用明确的分支、中文标题及正文文件执行；技能负责选择并提交本任务文件。编排器锁定 Git 公共目录，验证功能/发布候选、复核远端、合并、生成标签/Release并保存可恢复状态。调用上述验证接口，纯版本候选可复用完整验证凭据。
4. 迁移与入口：更新 Actions 为手动诊断、退役云端发布入口，同步发布校验与文档规范；创建个人技能。远端切换在本地验证通过后进行，并核查已有工作流、分支规则及 webhook。

2026-09-28 用户确认本次优化只覆盖纯文档和重复全量，不扩大为按业务模块选择测试。功能候选
根据实际差异选择文档或完整模式；主线前进时先确定发布候选，避免预先验证中间主线。发布复用
允许完整基线后的已识别文档差异，并核验已有全量凭据；业务、依赖和测试配置变化仍回退完整验证。
同一业务分支在完整验证后仅追加文档时，也先核验原业务凭据及环境，再只执行文档检查；
新凭据保留业务基线来源，不标记为重新执行了全量。
失败检查不自动重复执行。回归用例位于 `tests/tools/test_submit.py`、
`tests/tools/test_submit_validation.py` 和 `tests/tools/test_local_validation.py`；实际执行结果仍以
下述精确候选凭据为准。

## 验收

- 测试先行，覆盖字段级差异、缓存失效、失败/跳过不能复用、合并冲突、主线前进、网络超时后的远端事实核验及恢复。
- 使用临时 Git 仓库和模拟 GitHub API，不为测试制造正式版本。
- WSL 实际执行完整验证；记录分组耗时及 pytest 慢用例，复用结果时不重新运行重测试。
- 运行文档、发布自动化和版本一致性校验；独立审查后再执行远端迁移。

## 执行记录

- 2026-09-28：在最新主线 `eeb093d` 的独立 `fix/submit-validation-scope` 分支修复文档误触发与重复全量。
  新增用例先复现失败再修复；最终验证引擎 93 项、执行器 51 项、提交流程及相关契约 134 项通过。
  Windows 上两项文档符号链接环境用例跳过，不记为通过。真实 WSL 隔离文档候选四项检查通过，
  首次约 35 秒，再次调用复用结果约 15 秒，未准备 Docker 或执行业务回归；该耗时仅代表本次样例。
  独立审查及追加文档复用分支复审均未发现阻断缺陷。以上不代替最终提交的完整门禁及远端合并证据。
- 2026-09-26：从最新 origin/main `cdecda3` 创建 `feat/local-submit` 独立工作区；已批准方案不重复要求审批。
- 分工：验证引擎、本地环境、提交编排分别限定文件；主代理负责集成、仓库契约与文档。使用本计划记录进度，遵守 Markdown 必须位于 docs 的仓库规则。
- 验证引擎最终 33 项临时 Git 用例通过，含精确快照、缓存失效、凭据隔离、证据摘要与版本字段级复用。
- 编排 28 项回归及最后的分页 API 2 项专项通过；主线前进恢复、标题/源码版本绑定、版本递增和 Release Please 遗留标签均经测试及独立审查修复。真实 SDK 只读预检与 34 条历史发布 PR 查询通过。
- 本地 runner 与必跑报告持久化用例 28 项通过；发布契约 15 项通过；Windows 启动脚本和旧发布门禁回归 28 项通过。不同阶段集合有重叠，不合并计数。
- WSL Docker Engine 已安装，依赖镜像通过实际 Chromium/git/Python/Node/Release Please smoke。个人技能结构校验及预览/恢复场景验收通过；文档登记 137 项、发布配置和版本一致性校验通过。
- Windows 实际集成补充修正：导出归档时禁用换行转换，避免源码字节偏离 Git；WSL 使用 `--exec` 保留 Windows 路径参数。含空格路径的真实环境指纹查询已通过，失败诊断保留证据目录。

## 交付与证据边界

原实现提交为 `3530d9315c60b331c3f522d0e12fd3ebfaf22f7a`，Windows 集成修正提交为
`12b64f4371fa2115ee02979699517ef6e72c0fc7`。登记状态保守保持 `implemented`，
不把开发阶段单元测试和环境准备视为完整验收。仓库内的
[环境证据](../verification/artifacts/local-submit-environment.json) 只记录依赖指纹与实际工具 smoke，
不证明业务回归、生产镜像或远端切换已经通过。

2026-09-28 文档验证与复用优化实现提交为 `23f8a1bf209167bf95bdd0a171791cc468a38b8d`；
本地专项验证及独立审查已完成，最终候选的完整门禁、合并和版本发布仍以实际 submit 结果为准。

每次完整验收对应的源码 revision、tree、环境指纹、分组结果及日志摘要保存在 Git 公共目录的
`local-submit/runs/<运行 ID>/receipt.json` 和 `output/result.json`。必须以该次精确候选的通过
凭据为准；中断、失败和修改源码后的旧结果均不能代替通过凭据。功能是否已合并、Release 是否
已发布，以该目录内的分支状态文件及实际 GitHub PR、标签和 Release 核验。执行结果可在 PR
和交付报告中引用，不为追写 `verified` 状态而反复改变已验证的源码树。
