# 第三阶段验证记录

本记录对应[第三阶段需求](../specs/2026-09-06-phase-three-requirements.md)和
[实施计划](../plans/2026-09-06-phase-three-implementation.md)。P3-01 至 P3-08 已实现，
P3-AC01 至 P3-AC16 已完成下述本地功能验收。真实自动候选提取仍有错配和大量漏提取，
需要对照原件核对及手工补录，不能据此声称总体医学事实准确率已经得到验证。

开发分支为 `feat/phase-three`，基线为 `b9e86edfc30f79ea223e018b72b55a30e2365ac4`。
实现提交为 `a887270`（事实与回收站）、`7f24789`（速查与导出）、`c6d651f`
（原文边界、流式导出及评估）、`3f5257b`（私有磁盘临时目录）。完整 SHA、执行记录和
制品哈希见[汇总报告](artifacts/phase-three-verification-report.json)。登记表的 `verified`
表示本地功能证据；分支尚未合并，发布版本未确定，本记录不构成生产放行。

## 功能与验收对应

以下各项的本地结果均为通过；对应测试包含正常路径和故障/拒绝路径，执行范围与
跳过项见后文。固定真实集的自动提取质量单列，不以合成测试替代。

| 验收 | 已验证行为 | 可重复执行的测试与证据 |
| --- | --- | --- |
| P3-AC01 | 五类候选保存文档、页码、原文和解析版本；只有真实单块区域才定位，缺失降级到页；失败仍可打开原件并补录 | [事实测试](../../tests/facts/test_facts.py)、[段落边界](../../tests/facts/test_section_boundaries.py) |
| P3-AC02 | 否定、不确定和患者提供资料的限定语保留；报告日期与事件日期分开，年/月/未知精度不补成具体日；多个治疗日期只表示原文提及，不推断周期 | [日期测试](../../tests/facts/test_record_metadata.py)、[固定合成集](../../tests/fixtures/facts/synthetic-corpus.json)、[卡片边界](../../tests/exports/test_boundaries.py) |
| P3-AC03 | 确认要求勾选对照原件；待核对、暂缓、排除及撤销均不能入卡；确认不能绕过来源和版本限制，检验不新增全部确认门槛 | [核对测试](../../tests/facts/test_facts.py)、[快照测试](../../tests/exports/test_snapshot.py) |
| P3-AC04 | 更正、撤销和撤销上次操作均追加历史；只在相邻解析版本中来源与内容唯一匹配时继承；重复、变化及手工摘录来源页变化须重新核对 | [修订与重解析](../../tests/facts/test_facts.py)、[快照失效](../../tests/exports/test_snapshot.py) |
| P3-AC05 | 六部分内容可选、可预览；空项不表示无病史；正文溢出要求调整或明确附页，不缩小到不可读或静默截断 | [格式测试](../../tests/exports/test_formats.py)、[主流程浏览器](../../tests/browser/test_phase_three_browser.py) |
| P3-AC06 | 复用第二阶段有效检验结果及历史质量上下文；同日多份、原值/有效值、比较符和特殊结果保留；不确定的新日期不覆盖可靠的最近结果，不作不可靠趋势 | [快照及可比性](../../tests/exports/test_snapshot.py)、[日期边界](../../tests/exports/test_boundaries.py)、[格式测试](../../tests/exports/test_formats.py) |
| P3-AC07 | PDF 与预览共用冻结内容；A4 一页正文、显式附页、中文/符号/来源完整；读取字符及坐标验证无越界，真实核对示例逐页视觉检查 | [PDF 测试](../../tests/exports/test_formats.py)、[原件核对到导出](artifacts/phase-three-real-export-roundtrip.json) |
| P3-AC08 | 单份与 ZIP 原件逐块核验 SHA-256，保持字节不变；同名文件用稳定文档路径区分，清单逐项对应；缺失或损坏则整体失败 | [原件/ZIP 测试](../../tests/exports/test_formats.py)、[流式 I/O](../../tests/exports/test_streaming.py)、[真实原件核验](artifacts/phase-three-real-export-roundtrip.json) |
| P3-AC09 | 标准 CSV/JSON 读取器校验类型、字符串/空值、结果状态、日期精度和来源关系；JSON 保留原文，CSV 文本防公式且能处理换行、引号和 Unicode 前导控制字符 | [结构化格式测试](../../tests/exports/test_formats.py) |
| P3-AC10 | 全部、指定资料及日期首尾范围与预览一致，未知/部分日期单独选择；支持超过 1000 份资料；空选择、部分失败不误报成功；取消、重试、完成后 24 小时到期及清理可恢复 | [选择边界](../../tests/exports/test_boundaries.py)、[任务生命周期](../../tests/exports/test_jobs.py)、[私有临时目录](../../tests/exports/test_temporary_storage.py) |
| P3-AC11 | 生成和下载复核账号、原会话和来源；退出、注销、撤销、重解析、删除后旧导出不可获取；失效预览持久清除敏感快照，S3 暂存响应丢失仍可清理 | [任务测试](../../tests/exports/test_jobs.py)、[跨账号/CSRF 路由矩阵](../../tests/security/test_csrf_and_idor.py)、[预览清除](../../tests/exports/test_boundaries.py) |
| P3-AC12 | 移入后立即从正常读取、搜索、趋势、事实和导出排除；原件、解析及修订保留，显示本次 30 天期限，仍计入配额 | [回收站测试](../../tests/documents/test_recycle_bin.py)、[快照测试](../../tests/exports/test_snapshot.py) |
| P3-AC13 | 到期前恢复保持版本、原件、修订和质量限制；不恢复旧复核授权或处理租约；活跃相同 SHA 副本阻止恢复；再次移入重新计时，配额不重复计算 | [恢复及重复副本](../../tests/documents/test_recycle_bin.py)、[事实版本测试](../../tests/facts/test_facts.py) |
| P3-AC14 | 30 天边界按连续时长执行，到期即不可恢复；提前彻底删除须二次确认；注销包含回收站和导出，物理清理失败保持隔离并重试 | [回收站](../../tests/documents/test_recycle_bin.py)、[账号删除](../../tests/accounts/test_account_deletion.py)、[导出清理](../../tests/exports/test_jobs.py) |
| P3-AC15 | PostgreSQL 实际竞争验证恢复/永久删除单一结果、生成中移入和取消/对象提交的清理；迁移不复活旧永久删除；删除账本重放继续隔离 | [并发测试](../../tests/integration/test_phase_three_postgres_concurrency.py)、[旧删除迁移](../../tests/documents/test_recycle_bin.py)、[账本测试](../../tests/operations/test_restore_tombstones.py) |
| P3-AC16 | 上传、原件查看、检索、检验修订、对比、趋势及新增页面通过回归；桌面与手机执行核对→预览→ZIP 下载→移入→恢复→永久删除 | [浏览器流程](../../tests/browser/test_phase_three_browser.py)、[完整执行汇总](artifacts/phase-three-verification-report.json) |

页面入口为资料详情中的事实核对、`/facts/documents/<id>/`、`/visit/` 和
`/recycle-bin/`。原件对照不伪造高精度区域；跨页摘录和日期冲突有可见限制提示。
服务端撤销只能阻止后续访问，已下载到用户设备的静态副本无法收回。离线查看明确不在
本期范围内。

## 原件标注和评估范围

[公开样本范围](artifacts/phase-three-sample-scope.json)固定纳入已有授权的 64 个文件、
60 个报告组和 124 页。逐份保存匿名编号、文件/OCR 哈希、实际类型、报告组、标注页
与未判断页以及字段数。相同报告的多页、重拍或不同文件表示仍留在同一报告组，不作为
独立报告数量。第二阶段的检验标注未直接作为本期事实标注使用。

编码代理直接查看原件，必要时在视觉核对之后用 PDF 文本层或原始 OCR 辅助转录，
再冻结标注；没有独立临床专家复核。标注 SHA-256 为
`c94a74c1e83fc92263ecf5b2e9ff8af5bebdf7bf1d311b7f2d9cbfa941307e19`。
评测执行前后均核对原件、OCR、标注和解析代码的身份，元数据由实际流水线预测，
不将标注日期或类别注入应用预测。

可判断字段共 232 个：105 个有独立核对意义的标题叙述/明确字段，以及 127 个完整
药品医嘱行。每个连续叙述或完整医嘱行为一个单位，长度不同；这些计数不是医学实体数。
另有 2 个被截图边界裁断的医嘱行无法判断。一个 40 页分子报告只对 6 页完成事实标注，
其余 34 页只作目录/标题范围检查，明确列为未判断，不能声称已完整标注或没有事实。
这 34 页和 2 个截断字段不混入可判断字段召回率分母，文件和页仍保留在全量范围中。

实际类型包含入院、出院、病程、CT/MRI/PET-CT/超声、检验、药品医嘱、病理、免疫组化、
分子检测及混合资料包。一个既有清单误列为检验的文件已按原件改为药品医嘱；原报告组
不变。冻结标注中关于它“原报告组”的历史说明有一处笔误，公开范围制品附有勘误，
未修改冻结字段、实际分组或评分结果。

使用原件和冻结 OCR 重放实际处理及数据库持久化流程，不重新计量 OCR 模型性能。
评分先作逐文件一对一匹配，仅归一化空白/NFKC、允许省略原有标题；否则原文、页码、
类别及来源关系须一致。相似文本仍按错配记录，重复候选不能重复占用同一标注。
无法抽取或没有候选的文件保持在统计中。报告日期另评分，不与正文匹配混为同一指标。
统计规则及失败分母由[评估工具测试](../../tests/tools/test_phase_three_evaluation.py)覆盖。

## 自动提取结果与限制

当前抽取器为 `literal-sections-3`。实际结果见
[真实评估](artifacts/phase-three-real-evaluation.json)及
[合成评估](artifacts/phase-three-synthetic-evaluation.json)。

| 口径 | 可判断字段 | 正确 | 错配 | 漏提取 | 额外候选 | 精确率 | 召回率 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 真实全量可判断字段 | 232 | 60 | 15 | 157 | 1 | 60/76 = 78.95% | 60/232 = 25.86% |
| 其中叙述/明确字段 | 105 | 60 | 15 | 30 | 1 | 60/76 = 78.95% | 60/105 = 57.14% |
| 其中完整药品医嘱行 | 127 | 0 | 0 | 127 | 0 | 无候选，不适用 | 0/127 = 0% |
| 独立合成契约集 | 12 | 12 | 0 | 0 | 0 | 12/12 = 100% | 12/12 = 100% |

真实集 52 个文件产生 76 个候选，12 个文件没有候选，抽取器异常 0 个；无候选包含
低质量 OCR 和当前抽取规则未覆盖的情况，异常为零不能解释为全部成功识别。
未判断页上的候选为 0，无法判断的源字段为 2，未完整标注页为 34。报告日期单独计数
为 62 正确、13 错配、157 因无候选缺失；事件日期与事件关联、机构正确性未作全面评分。

按事实类别的固定集结果也有明显限制：两个分期和两个病理摘录均漏提取；影像类别
8 个候选均未满足严格原文匹配，另漏提取 6 个字段。真实低质量病理 OCR、叙述排版及
药品表格仍是手工核对/补录的主要来源。这里的错配包含 OCR 原文差异，不是临床错误率，
也不能只看大量检验单上的短诊断字段而推断复杂影像/治疗摘录质量。

核对负担为 76 个候选全部需要对照原件，其中 16 个需要更正或移除，157 个已标注字段
需要手工补录；还有未判断范围。这只是操作单位计数，没有进行计时用户研究。
合成集覆盖真实资料缺少的否定、日期精度、重复及空结果边界；其他功能测试补齐权限、
分页、字符转义、删除和并发，不能把合成 100% 解释为真实质量。

本集同时用于开发和回归，没有留出集；不构成独立泛化评估。按规格 5.2，本期不设
额外样本数量或未经验证的准确率门槛。功能验收验证的是核对准入、来源追溯和已核对
内容的正确传递；上述自动提取缺口仍明确保留，未被“功能通过”消除。

## 卡片、原件及大包实际制品

在独立内存数据库、测试账号与私有位置，选择 3 份原件和 6 条原件标注事实，通过人工
补录/更正服务模拟原件核对，再生成 PDF、JSON、CSV 和含原件的 ZIP：

- 6 条正文及文档/页码/类型在 JSON 中精确对应，CSV 行数一致；
- PDF 共 2 页（一页正文加明确附页），逐页查看中文和布局，并用解析器检查正文和
  字符边界；不同来源的分期表达同时保留；
- 3 份同名原件打包后路径互不覆盖，每份 SHA-256 与原件一致；
- 已核对内容在这组固定文件中没有发现静默错引或损失；结果不代表自动 OCR 准确率。

见[公开往返验证结果](artifacts/phase-three-real-export-roundtrip.json)。原件、标注、预测、
真实 PDF/ZIP 和截图只保存在被 Git 忽略的私有目录；该验证没有独立临床专家参与。
仓库合成 PDF 测试另覆盖比较符、百分数、零值、特殊检验值、长文附页及来源索引。
字体来源、授权及确定性生成方式见[PDF 组件许可](../licenses/pdf-components.md)。

另实际生成并下载 16 × 16 MiB 合成原件的 ZIP，随后取消及清理：原件总计 268,435,456
字节，ZIP 268,522,592 字节，哈希匹配，耗时 12.201 秒；预热后的 Windows 进程峰值
工作集从 112,861,184 增至 113,020,928 字节。见[大文件探针](artifacts/phase-three-large-export.json)。
合成对象只用于 I/O，不是有效医疗报告；该数值不代表上传/OCR 性能或生产容量。
分块读取、失败清理和文件关闭由正式测试另行验证。

## 回归命令和实际结果

在独立功能工作区执行，Windows 设置 `$env:PYTHONUTF8='1'`；Django 检查使用
`DJANGO_SETTINGS_MODULE=config.settings.test`。命令及结果明细在汇总报告中。

| 范围 | 实际结果 | 适用范围 |
| --- | --- | --- |
| Python 完整回归，排除独立 PostgreSQL/模型及两个单独必跑浏览器文件 | 1526 passed、2 skipped、40 deselected，292.11 秒 | `c6d651f` 的代码；两个跳过均为 Windows 缺少符号链接权限，不是功能用例通过 |
| 随后的导出临时目录变更：exports、deploy、core checks | 168 passed、0 skipped，19.84 秒 | `3f5257b`，包含两个新目录测试；与完整回归范围重叠，不相加 |
| 必跑上传、交互及三阶段浏览器 | 8 passed、0 skipped，31.44 秒 | Python Playwright 驱动本机 Edge；桌面及 390 像素手机，未发现页面脚本错误、静态加载失败或横向溢出 |
| PostgreSQL 全套并发 | 39 passed、0 skipped，98.51 秒 | 本任务较早执行；最后的事实文字边界调整之后又复验下列本期 4 项 |
| PostgreSQL 三阶段竞争 | 4 passed、0 skipped，11.08 秒 | 真实隔离 PostgreSQL 事务，恢复/删除、生成中移入、取消/对象提交 |
| JavaScript | 6 passed、0 skipped | 原有交互契约 |
| 第二阶段固定合成评估 | passed | 现有检验字典/解析契约；不等于本期真实事实质量 |
| Django 系统检查、迁移检查 | passed，无待生成迁移 | 测试设置 |
| 文档、版本及发布自动化检查 | passed | 版本保持 `1.1.1`，本期发布版本待定 |
| 原 PRD 追踪矩阵、上线门禁 | 60 verified / 2 external_pending；BLOCKED 8/23 | 外部条件没有被本期本地结果替代 |

核心复现命令：

```powershell
$env:PYTHONUTF8='1'
$env:DJANGO_SETTINGS_MODULE='config.settings.test'
python -m pytest -q -m 'not postgres and not ocr_model' --ignore=tests/browser/test_ac02_upload_browser.py --ignore=tests/browser/test_upload_interactions_browser.py
python tools/run_required_tests.py -q tests/browser/test_ac02_upload_browser.py tests/browser/test_upload_interactions_browser.py tests/browser/test_phase_three_browser.py
python -m pytest -q tests/exports tests/deploy tests/core/test_checks.py
# PHR_POSTGRES_TEST_URL 应指向隔离合成测试库。
python tools/run_required_tests.py -q -m postgres --ds=config.settings.postgres_test
npm run test:js
python tools/phase_two_evaluation.py --synthetic-only --report .runtime/phase-three-verification/phase-two-release-evaluation.json
python manage.py check
python manage.py makemigrations --check --dry-run
python tools/verify_documentation.py
python tools/verify_release_automation.py
python tools/release_version.py check
python tools/verify_traceability.py
python tools/verify_release_gate.py
```

完整回归现在会包含随后添加的两个目录用例；上表保留已经执行的分阶段结果，不预测
未来运行计数。浏览器和 PostgreSQL 必跑入口拒绝跳过或空集合；模型烟测及原阶段外部
浏览器/生产环境要求仍按原有证据管理。

合成事实评估不需要真实资料：

```powershell
$env:PHR_FACT_SYNTHETIC_REPORT='.runtime/phase-three-evaluation/synthetic-evaluation.json'
python -m pytest -q tests/facts/test_synthetic_evaluation.py
```

真实评估需要已授权且已冻结的私有原件/OCR 清单和原件标注，公开仓库不提供这些内容。
具备同一私有输入时运行：

```powershell
$factAnnotationDigest=Get-Content -Raw .runtime/phase-three-evaluation/fact-annotations-sha256.txt
python tools/phase_three_evaluation.py --inventory .runtime/phase-three-evaluation/source-inventory.json --annotations .runtime/phase-three-evaluation/fact-annotations-frozen.json --annotation-sha256 $factAnnotationDigest --report .runtime/phase-three-evaluation/real-evaluation.json --private-output .runtime/phase-three-evaluation
```

## 运行依赖和交付边界

完整本地功能须启动 Celery Worker 与 Beat；一键启动脚本的轻量 Worker 只处理 OCR
和开发短信。导出、到期和清理任务通过 control 队列执行，详见
[本地开发说明](../deployment/local-development.md)及
[生产运行手册](../deployment/production-runbook.md)。生产导出使用私有磁盘临时卷，
避免大包占满容器的 1 GB 内存临时目录；容量按实际并发单独验收。

本机没有 Docker 可执行程序，因此没有运行本期 Linux 镜像构建。CI 已配置锁定依赖
构建及非 root 用户导入、私有导出临时文件写入烟测，结果待 CI 或部署环境执行。
真实短信/S3、离线模型、多进程容量、支持浏览器、无障碍及备份恢复等生产证据仍以
[上线门禁](release-gate.md)为准，当前为 `BLOCKED`（8/23）。功能分支没有合并、发布
或部署；版本由后续 Release Please 确定。
