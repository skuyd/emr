# 肿瘤个人病案与病情轨迹系统

面向肿瘤患者及家庭照护者的个人健康档案（PHR）系统。保存报告图片和 PDF 原件，提取可检索信息，按日期整理健康资料，并支持原件核对、检验对比、日常记录、家庭授权和选定内容导出。

当前源代码版本为 **v5.1.0**，见 [`VERSION`](VERSION) 和[版本清单](docs/releases/v5.1.0.md)。产品范围、功能边界和仍待验证的事项统一见[当前产品状态](docs/product/current-state.md)；其他文档从[文档中心](docs/README.md)进入。

本项目用于个人资料整理，不提供诊断、疗效判断、风险判断、用药或治疗建议。自动识别结果需要对照原件核实。生产放行门禁仍为 **BLOCKED**，源码发布不代表已允许接入真实用户或真实医疗资料。

## 主要功能

- 健康档案：图片/PDF 批量上传、后台识别、月历与列表浏览、搜索、原件查看和来源定位。
- 检验资料：报告归并与来源整理、逐份核对确认、指标分类筛选和结果对比表格。
- 临床资料：单份报告的影像、病理/IHC、分子信息核对，病灶关联、癌种候选和云影像来源管理。
- 日常记录：体重、体温、症状、ECOG 评分，支持日历/列表、连续添加、更正和确认删除。
- 家庭协作：多个患者档案、成员授权、患者切换、限定内容分享和撤销。
- 资料管理：就诊速查 PDF、原件及 CSV/JSON/ZIP 导出、30 天资料回收站和账号删除。

## 技术结构

服务端采用 Django 模块化单体，页面使用 Django Templates 与原生 JavaScript/CSS。OCR、导出、删除和通知由后台任务处理。

| 层次 | 实现 |
| --- | --- |
| Web | Python `>=3.11,<3.15`、Django 5.2 |
| 数据与队列 | PostgreSQL、Redis、Celery；开发和部分测试可使用 SQLite |
| 原件与文档处理 | 私有 S3 兼容存储、Pillow、pypdf、pypdfium2、可选 PaddleOCR |
| 导出与部署 | ReportLab、Gunicorn、WhiteNoise、Caddy |
| 验证 | pytest、Node Test Runner、Playwright、k6 |

业务模块位于 [`apps/`](apps/)，路由与配置位于 [`config/`](config/)，页面与静态资源位于 [`templates/`](templates/) 和 [`static/`](static/)。测试、部署脚本和开发工具分别位于 [`tests/`](tests/)、[`deploy/`](deploy/) 和 [`tools/`](tools/)。

## 本地开发

以下命令使用 Windows PowerShell，需要 Python、Docker 和 Docker Compose。前端测试另需 Node.js `>=22`。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
python deploy/bootstrap_dev_env.py
docker compose --env-file .env up -d
.\deploy\start-local.ps1 -PythonPath .\.venv\Scripts\python.exe
```

打开 <http://127.0.0.1:8000/>。初始化脚本生成被 Git 忽略的本地 `.env`，不会覆盖已有文件；启动器执行迁移、配置检查和开发账号初始化，并启动 Web 与本地处理 Worker。开发账号、登录方式、离线 OCR 配置和其他启动选项见[本地开发说明](docs/deployment/local-development.md)。

本地 Worker 只处理 OCR 和开发短信。完整的导出、回收站到期和物理删除还需要 Celery Worker 与 Beat；具体命令见同一开发说明。实际运行 PaddleOCR 时还需安装 `.[test,ocr]` 并准备离线模型。

## 验证与提交

按改动范围选择测试；常用检查如下：

```powershell
$env:PYTHONUTF8 = "1"
python manage.py check
python manage.py makemigrations --check --dry-run
python tools/verify_documentation.py
python tools/verify_traceability.py
python tools/release_version.py check
```

Python、JavaScript、PostgreSQL、浏览器和外部环境的执行要求见[验证入口](docs/verification/README.md)。跳过或未运行的检查不能记为通过；生产放行必须满足[发布门禁](docs/verification/release-gate.md)。

遵循 [`AGENTS.md`](AGENTS.md) 和[本地提交指南](docs/policies/local-submit.md)。由用户手动调用 `submit`，按改动影响验证并以 Squash 合并；版本和 [`CHANGELOG.md`](CHANGELOG.md) 由 Release Please 自动维护。日常 GitHub Actions 禁用，不调用 Jenkins，不自动部署。

## 部署与数据边界

部署、私有存储、短信、离线 OCR、备份恢复和运维操作统一见[生产运行手册](docs/deployment/production-runbook.md)。只在机器判定[发布门禁](docs/verification/release-gate.md)为 `PASS` 后接入真实用户和真实医疗资料。

仓库测试、演示和公开制品使用合成数据。医疗原件、账号凭据、私有评估资料和部署信息不得进入公开日志、截图或远程仓库；资料原件保存在私有对象存储中。授权的真实质量评估在私有位置执行，公开证据仅保留匿名计数和哈希。
