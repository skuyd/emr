# PRD v1.0 验证证据

本目录区分“真实运行过的证据”和“仍需外部环境执行的门禁”。所有自动化
测试只使用合成值；文档不得记录用户标识、患者称呼或医疗原文。

当前权威入口：

- `traceability.json`：机器可读的 62 项需求—证据映射；
- `traceability.md`：由上面的 JSON 自动生成的人类可读报告；
- `external-compatibility.md`：Chrome、Edge 与 Safari 的真实执行状态；
- `ac00-ac01.md`、`ac02-ac07.md`：早期阶段的历史执行记录。

验证命令：

```powershell
python tools/verify_traceability.py
python manage.py check
python manage.py makemigrations --check
python -m pytest -q
```

2026-08-31 当前结果：追踪矩阵 62/62 编号完整，其中 60 项有自动化验证，
AC-22 与 SCN-26 因缺少真实 Safari 环境保持 `external_pending`。Python
全量回归为 `551 passed, 3 skipped`；Chrome 和 Edge 浏览器套件分别为
`2 passed`。跳过项不计为通过，最终发布状态以发布门禁文档为准。
