# 本地 submit 使用指南

在 Codex 中调用个人技能 `$submit`，完成本任务提交、本地验证、功能 PR Squash 合并、版本推断、
Changelog 和 GitHub Release。版本计算仍由固定版本 Release Please 执行，依据合并标题和正文，
不由模型猜版本。没有触发版本的变更会正常合并并报告“无需新版本”。

## 首次准备

- Windows 安装 Python 3.11、Node.js 22、Git，并准备 GitHub 凭据。
- WSL2 `Ubuntu-24.04` 中安装并启动 Docker Engine；默认 WSL 用户能执行 `docker info`。
- 在仓库运行 `npm ci` 安装锁定的 Release Please CLI。验证依赖由本地 Docker 构建缓存管理。
- 仓库 Actions 必须禁用；合并保护规则不能等待已停用的 Actions。详情见[版本规则](versioning.md)。

技能先读取当前仓库规则和 Git 状态，归纳当前任务的中文 Conventional Commit 标题。明确属于本任务
的未提交文件可以直接整理、提交；混入其他任务的变更或存在冲突时暂停相关动作并说明需要确认的范围。
不得使用 `git add -A` 把无法归属的文件一并提交。腾讯云本地资料始终排除。

## 执行与恢复

技能整理好提交后调用仓库工具，使用 UTF-8 正文文件避免 shell 转义改变内容：

```powershell
python tools/submit.py --title "feat(records): 新增报告导出功能" --body-file <正文文件>
```

默认处理当前功能分支；显式 `--branch` 用于恢复原任务。工具的 `--dry-run` 针对工作区干净的
已提交候选，只检查和展示计划，不推送、不合并、不创建版本。尚有未提交改动时，技能只读展示
拟提交内容，不为预览先提交。详细参数以 `python tools/submit.py --help` 为准。

工具导出已提交候选的 Git archive，验证时不包含未跟踪文件、凭据和本地部署资料。通过后创建或
更新功能 PR，以验证过的 head SHA 发起 Squash。发布候选由 Release Please 创建，本地通过后再
合并并创建标签及 Release。不调用 Jenkins、不部署、不强推、不重写标签、不删除功能分支。

状态、验证凭据和分组日志保存在 Git 公共目录的本地 submit 数据下，不提交到仓库。发生失败后
修复问题并以同一分支再次调用；工具核查远端事实再继续，不依靠上一次 HTTP 请求是否成功判断
是否已经合并。报告需要区分“功能已合并”“发布 PR 待验证”“Release 已创建”，不能统称成功。

## 验证范围和耗时

完整验证保留原 CI 的 Python 回归、八个必跑浏览器文件、独立 PostgreSQL 并发、JavaScript、
Django 系统与迁移检查、文档/版本/追踪/发布门禁、合成质量评估和生产 Docker 构建 smoke。
原 CI 排除的离线 OCR 模型及外部环境门禁仍不代表已通过。浏览器、PostgreSQL 等必跑分组
跳过或空集合即失败；通用 Python 中的平台限定用例按原规则记录跳过。

速度优化来自以下可核验的复用，首次下载及构建通常更慢：

- 源码放在 WSL Linux 文件系统，复用依赖及 Docker 层缓存。契约和 Django 检查后，普通
  Python 与 PostgreSQL 回归使用独立源码、容器和数据库并行运行，最多两个重任务；其余
  分组及发布候选验证保持顺序执行。两组的原测试命令和通过标准不变。
- 只有源码树、验证策略、命令和实际环境指纹匹配且证据完整，才能复用已通过的完整验证。
- 发布候选只改允许的版本字段并在原 Changelog 前增加发布记录时，可复用业务回归；任一
  依赖、脚本、业务源码或测试配置改变都会使复用失效，转为完整验证。实际检查失败则停止，
  不用另一轮重跑掩盖失败。版本、文档和最终镜像仍单独检查。
- 日志保存各组耗时及 pytest 慢用例；没有实测前不承诺固定提速比例。

Actions 禁用后不会新增日常 runner 用量，但历史 artifact/cache 的存储费用不会自动消失。
本流程不删除既有验证证据。GitHub Release 只代表源码发布，生产放行仍由
[发布门禁](../verification/release-gate.md)决定。

实施与实际验收状态见[实施计划](../plans/2026-09-26-local-submit.md)及文档登记表。
