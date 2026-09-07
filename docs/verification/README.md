# PRD v1.0 验证证据

本目录区分“真实运行过的证据”和“仍需外部环境执行的门禁”。仓库测试夹具和 CI
使用合成值；已有授权示例的质量评估在私有测试位置执行，原件、标注和带原文的导出
文件不进入仓库。公开文档和机器制品只记录计数、哈希及方法，不记录用户标识、患者
称呼或医疗原文。

当前权威入口：

- [第五批日常记录与修订验证](batch-five-daily-records.md)：体重、体温、症状及选定导出/分享通过本地验证与独审，最终 CI 及源码交付待完成；日内血糖继续独立实施；
- [第 3 批结构化证据基础与首批影像字段验证](batch-three-clinical-foundation.md)：本次基础及实际细选分享通过两轮独审和功能/发布 CI，随 [v1.9.0](../releases/v1.9.0.md) 发布；[交付制品](artifacts/batch-three-clinical-delivery.json)保留源码与评分身份，B3 整体继续实施；
- [第二批家庭邀请、限时分享与访问审计验证](batch-two-family-sharing.md)：B2-02 至 B2-04 的实现、独审和四项 CI 已通过，随 [v1.8.0](../releases/v1.8.0.md) 发布；[交付制品](artifacts/batch-two-family-sharing-delivery.json)记录确切身份；
- [第 1 批事实提取质量验证](batch-one-facts-quality.md)：B1-03 事实部分的固定全量结果、精确率下降与人工核对成本；复审通过，待 CI 及合并；
- [第 1 批图像增强与来源坐标验证](batch-one-image-enhancement.md)：B1-01 本地实现与实际 OCR 证据，独立审查及 CI 待完成；
- [第三阶段验证记录](phase-three.md)：事实核对、速查卡、导出与回收站的 AC01–AC16 本地证据及真实质量限制；
- [第二阶段验证记录](phase-two.md)：检验解析、修订及对比的验收与真实评测；
- [项目审查修复验证记录](project-review-remediation.md)：当前修复分支的验证与交付记录；
- `traceability.json`：机器可读的 62 项需求—证据映射；
- `traceability.md`：由上面的 JSON 自动生成的人类可读报告；
- `release-evidence.json` / `release-gate.md`：不可由说明文字绕过的上线门禁；
- `external-compatibility.md`：Chrome、Edge 与 Safari 的真实执行状态；
- `browser-accessibility.md`、`performance.md`、`backup-restore.md`：外部环境验收说明与证据位；
- `ac00-ac01.md`、`ac02-ac07.md`：早期阶段的历史执行记录。

验证命令：

```powershell
python tools/verify_traceability.py
python tools/verify_release_gate.py
python manage.py check
python manage.py makemigrations --check
python -m pytest -q
node --test tests/js/*.test.mjs
```

2026-09-05 本机历史预演结果：追踪矩阵 62/62 编号完整，其中 60 项有自动化验证，
AC-22 与 SCN-26 因缺少完整受支持浏览器证据保持 `external_pending`。2026-08-31 的
Python 全量回归基线为 `666 passed, 3 skipped`（0 failed，43.54s）；2026-09-04 已在
D 盘离线环境补跑 PaddleOCR 模型门禁，模型烟测 `1 passed`、适配器套件 `8 passed`，
并用合成图片完成禁网推理。当时的 JavaScript 契约为 `6 passed`；Chrome 152 与 Edge 152
当前版本冒烟各为 `6 passed`。上线门禁当前 `8 passed / 15 pending`，结论为 `BLOCKED`。

剩余 15 项已完成一次本机预演，结果见
`artifacts/local-gate-rehearsal-result.json`：当前 Chrome/Edge 冒烟各 `6 passed`，完整
Playwright 套件发现 57 项，自动无障碍 49 项、短信契约 20 项、S3 契约 54 项、部署与
恢复契约 32 项、性能数据契约 3 项均通过；2 个 PostgreSQL 并发用例按设计跳过。
这些结果不含正式 HTTPS、历史浏览器/Safari 真机、真实 PostgreSQL/短信/S3、Docker
多进程、k6 负载或加密恢复，因此全部 15 项仍为 `pending`，不得据此放行。

以上日期和计数保留为历史执行记录。后续交付验证见[项目审查修复验证记录](project-review-remediation.md)，
当前已发布源代码版本见 [v1.9.0 版本清单](../releases/v1.9.0.md)；第三阶段合并与 CI 结果见
[交付证据](artifacts/phase-three-delivery.json)。

剩余本地工作整理时重新执行了 Python 完整回归（排除独立 PostgreSQL 和模型环境）与
离线 PaddleOCR 套件，命令、结果及启动脚本校验值见
[本地工作提交验证记录](artifacts/pending-local-work-result.json)。
