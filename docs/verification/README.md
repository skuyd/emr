# PRD v1.0 验证证据

本目录区分“真实运行过的证据”和“仍需外部环境执行的门禁”。所有自动化
测试只使用合成值；文档不得记录用户标识、患者称呼或医疗原文。

当前权威入口：

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

2026-08-31 当前结果：追踪矩阵 62/62 编号完整，其中 60 项有自动化验证，
AC-22 与 SCN-26 因缺少完整受支持浏览器证据保持 `external_pending`。最终 Python
全量回归为 `666 passed, 3 skipped`（0 failed，43.54s）；3 个跳过分别为 2 个真实
PostgreSQL 并发用例和 1 个离线 Paddle 模型实跑。JavaScript 为 `3 passed`；Chrome
151 与 Edge 152 当前版本冒烟各为 `2 passed`。上线门禁当前 `7 passed / 16 pending`，
结论为 `BLOCKED`。跳过项、未执行的性能/恢复和仅有模拟供应商结果均不计为通过。
