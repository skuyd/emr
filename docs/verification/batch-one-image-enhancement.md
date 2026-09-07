# 第 1 批图像增强与来源坐标验证

本记录对应[后续五批规格](../specs/2026-09-07-batches-one-five-requirements.md) B1-01，
执行范围来自[实施计划](../plans/2026-09-07-batches-one-five-implementation.md) Task 2 的图像部分。
实现提交为 `9e725b1b9188ba6a51cac8da164854cfd05fa91a`，功能分支为 `feat/batch-one-intake`。
创建时基线为 `b7d5f3485b1b73cacc08476863401c11ae927d46`，提交后同步已合并主分支
`e790876`。当前代码已有本地验证，正在独立审查及准备 PR；尚未完成 CI、合并或源码发布。
本项及五批规格、计划的交付状态保持 `implementing`，发布版本保持未确定。

手机照片的 OCR 派生图支持保守裁边、透视校正、文本行共识纠偏及光照归一。原始文件字节
保持不变；边缘依据不足时保留整页，不执行裁切。小于 1° 的估计不触发重采样，大块深色
界面且无可靠纸边时不按纸张阴影处理。异常返回可用的原始派生栅格，诊断只记录稳定原因。
文本型 PDF 继续优先使用文本层，扫描页采用相同的几何及光照处理。

预处理版本为 `document-image-1`。OCR 布局多边形用于行列与段落关联；持久化的检验、日期、
事实来源多边形映射到现有查看器使用的方向校正后原件坐标。`OcrBlock.layout_polygon`
保存布局，`polygon` 保存来源，旧记录保留原值并兼容读取。无法反映射时使用页级来源。
迁移只增加可空布局字段并允许来源区域为空，不重写原件或历史识别值。

## 本地回归

图像裁边、阴影、来源传递与持久化的行为测试先复现原代码失败，再验证实现。
补充回归还先复现了深色界面误归一、亚角度文字重采样、晚期失败残留几何诊断，
以及事实侧栏日期因混用原件坐标而截断正文的问题。

| 检查 | 已执行结果 | 覆盖与限制 |
| --- | --- | --- |
| 处理、事实、详情查看及上传回归 | 201 passed | 包含真实持久化、字段来源、事实 lane 与查看器高亮；排除独立模型测试 |
| 上传浏览器必需回归 | 7 passed | 原有上传入口与任务状态，未跳过 |
| 四场景实际本地 OCR | 4 passed | 平拍、斜拍、阴影及组合场景；显式本地模型并拦截网络连接 |
| JavaScript | 6 passed | 既有任务轮询与界面契约 |
| Django 系统与迁移检查 | 无问题、无待生成迁移 | 使用测试设置 |
| Linux 原生库导入及裁边 | 已通过 | WSL Ubuntu 24.04 / Python 3.12.3，使用锁定版本的 Linux wheels |
| 生产容器构建及独立审查 | 待完成 | 本机无 Docker CLI；Dockerfile 已增加 OpenCV 导入检查，仍须 CI 实际构建 |

同步主分支后重新执行处理、事实与文档集成回归，201 项通过，5 个独立模型用例按命令
筛选排除；文档校验通过，登记 66 份 Markdown。模型测试使用 Windows
Python 3.11.9、PaddleOCR 3.7.0、Paddle 3.3.1 和现成 PP-OCRv5 mobile det/rec，
具体模型文件与实现源码哈希见[机器制品](artifacts/batch-one-image-enhancement.json)。

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings.test'
python -X utf8 -m pytest -q tests/processing tests/facts tests/documents/test_detail_viewer.py tests/documents/test_upload_views.py -m 'not ocr_model'
python -X utf8 tools/run_required_tests.py -q tests/browser/test_ac02_upload_browser.py tests/browser/test_upload_interactions_browser.py
$env:PHR_RUN_OCR_MODEL = '1'
python -X utf8 -m pytest -q tests/processing/test_image_enhancement_ocr.py
npm run test:js
python -X utf8 manage.py check
python -X utf8 manage.py makemigrations --check --dry-run
python -X utf8 tools/verify_documentation.py
```

关键行为测试为[图像增强](../../tests/processing/test_image_enhancement.py)、
[实际 OCR 与原件定位](../../tests/processing/test_image_enhancement_ocr.py)、
[持久化及来源查看](../../tests/processing/test_source_geometry.py)和
[OCR 适配契约](../../tests/processing/test_ocr_contract.py)。

## 实际 OCR 与来源证据

四组合成图共 40 行目标文字，增强前后均识别 40 行；独立已知字形中心全部落在映射后的
原件区域内。与紧字形边界的 IoU 有 39/40 达到 0.5，阴影场景一行是 0.480568；
OCR 框边缘范围仍有差异，不把中心定位通过描述为所有紧边界均完全重合。

现有授权开发样本先选取 4 份，再按图像准备结果补充 2 份几何样例，最终保留 6 份。
增强前后均实际调用同一本地 OCR，原件 SHA-256 全部不变，受监测的网络连接尝试为 0。
真实原图、OCR 正文、原始文件名、私有路径及标注未进入本记录或 Git。

| 已有审核范围 | 增强前 | 最终增强后 |
| --- | --- | --- |
| 已标注检验行 | 34 | 34 |
| 联合 TP / FP / FN | 13 / 17 / 17 | 13 / 17 / 17 |
| 联合 F1 | 43.33% | 43.33% |
| 已评测分字段计数 | 基线 | 全部保持 |
| 已标注字段来源定位 | 4/6 | 4/6 |

初轮深色界面误当阴影后增加一条误提，单位字段也下降；保守条件修复后对同一真实样本
重新运行，OCR 文本及区域恢复到基线。另一份约 0.49° 的微纠偏使字符数从 752 降至 705；
最低角度收紧后重新运行，文本及全部区域与基线一致。失败轮次保留在本地执行记录，
机器制品明确记录了原因与处理方式。

实际裁边照片的 OCR 字符数从 147 增至 164，增强后 25 个区域均能反映射；这份样本没有
审核检验行，仅有一条至少四字符的独有相同文本可作区域对比，不能据字符增加推断准确率。
全部真实样本的独有相同 OCR 文本共 396 对，395 对原件区域 IoU≥0.5；至少四字符的
311 对全部通过。未通过的一对是一个字符在不同原件位置的识别对应。这个比较参考基线
OCR 区域，不代替人工来源标注。

这些是开发样例，不是独立保留集或医学准确率估计。检验统计由实际 OCR 后直接提取与
页面元数据计算，尚不涵盖数据库有效结果或人工确认流程。机器制品中的无检验行状态
属于该评测约定，不代表应用的处理任务状态。样本计时含首次模型加载和并行 CPU 影响，
不作性能提升声明。

本次验证仅支撑 B1-01 的实现和本地回归。B1-02 非单据提示及 B1-03 固定全量提取改进
分别交付；既有质量差距与[生产放行门禁](release-gate.md)保留各自状态。独立审查、PR CI、
合并与 Release Please 发布的后续结果须按实际证据补充，不能由本地测试推定通过。
