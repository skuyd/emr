# 第 1 批非单据提示与资料恢复验证

本记录对应[后续五批规格](../specs/2026-09-07-batches-one-five-requirements.md) B1-02、
[实施计划](../plans/2026-09-07-batches-one-five-implementation.md) Task 2 的资料判断部分。
实现源码为 `61a8ee88a5b31f48afe22c99816b8f53299e2ab8`，功能分支
`feat/batch-one-material` 已同步主分支 `6c00f4408252455bac07c657585e560bfe59b429`。
本地验证及独立审查已通过，PR CI 与合并尚未完成；本项及五批总体保持 `implementing`，
本项发布版本未确定。[机器制品](artifacts/batch-one-material-recovery.json)绑定源码、模型与评测身份。

## 已实现行为

本地判断生成“存在资料内容”“可能不是单据”或“暂无法判断是否为单据”的逐页依据。
文本层、可读报告文字和可靠纸张边缘优先支持资料判断；空白、弱文字、灰度照片等缺少充分依据时保持不确定。
分类独立于 OCR 失败与未知文档类型，所有已接受文件继续识别、保留全部页面和既有整理结果。
混合 PDF 展示各页提示，包含资料页时不会把整个文件归为非单据。

上传结果、任务、列表和详情展示相同提示。详情可“按资料保留并重新整理”，也可恢复自动判断；
使用原文档和原始对象重新整理，不产生第二次上传或重复计入配额。原件 SHA-256、对象键、字节数、页数和页面记录保持不变。
人工保留方式与自动分类分开记录，历史保留判断版本、原件哈希、实际作者、序号、自动依据快照及关联任务。
重复请求幂等；过期的版本或序号返回冲突。切回没有分类依据的旧解析版本后，仍可撤销人工保留方式。

分类来源是整页链接，不伪造局部高亮。既有 OCR、检验和事实的来源坐标保持原契约，
几何增强证据见[图像增强验证](batch-one-image-enhancement.md)。本功能没有新增医疗诊断或用途判断模型。

资料恢复复用实时家庭写权限，记录实际成员，而非一律记录患者所有者。只读成员可查看提示和原件，
没有保留/恢复按钮，也不能提交写操作。多患者写入需要明确患者范围；错误范围返回 404，缺失范围返回冲突。
成员撤销、降权、账号注销和患者删除继续阻断后续写入；用户发起的重新整理绑定成员 revision，并在撤权后停止发布。

## 已执行回归

| 检查 | 结果 | 主要覆盖 |
| --- | --- | --- |
| 处理、文档、患者、安全、账号删除和操作审计 | 546 passed，7 项按标记排除；63.27 秒 | 最终集成源码，独立模型与 PostgreSQL 用例分别执行 |
| 分类与实际本地 OCR 必需用例 | 13 passed；26.18 秒 | 五张合成图、两页混合 PDF、两张公开照片及水果照片实际重新整理 |
| 真实 Chromium 与上传必需回归 | 12 passed；51.51 秒 | 原生原件图片加载、保留/重整/重载/恢复、先于文档创建打开任务页、原有上传、家庭旧标签页；含 360 px |
| PostgreSQL 必需并发与实际迁移 | 5 passed；19.04 秒 | 重复请求真实阻塞、撤权及账号注销的作者外键竞争、资料及家庭迁移 |
| JavaScript | 6 passed | 原有通知与任务状态契约 |
| Django 系统与迁移生成检查 | 通过，无待生成迁移 | 测试设置 |

家庭兼容回归先确认协作者被旧的所有者守卫拒绝、只读成员错误显示写按钮，修正后通过。
新增恢复路由先触发安全矩阵缺项，加入后完整跨患者检查通过。集成广回归还发现旧家庭迁移测试
保留了新增处理迁移的叶节点，导致回退计划混合前进与后退；补齐旧处理版本边界后，SQLite、
PostgreSQL 及完整近邻回归均通过。这是迁移测试的历史状态边界修正，没有重写已合并迁移。

独立审查还复现了一个 P2：在文档 ID 尚未生成时打开任务页，后续轮询虽展示非单据提示，
却缺少恢复入口。永久 Chromium 用例先出现“找不到链接”的失败，再由轮询动态创建或更新文档恢复链接。
已存在的入口保持单一链接；新用例还验证当前会话切到患者 B 后，患者 A 的任务仍携带 A 的范围轮询，
点击返回的资源链接后由服务器重新解析归属和权限，最终进入 A 的资料。修复仅改变任务脚本和浏览器测试，
后端、OCR 与迁移源码保持此前验证的哈希；12 项浏览器与 6 项 JavaScript 回归重新通过。

2026-09-08 独立审查基于 `dc626f41c460bdbca35a3e1493fd87ff05c2f24d` 通过，并独立关闭上述 P2。
审查者重新执行实际离线 OCR 与分类 13 项（43.11 秒）、原始反例及资料浏览器/恢复/家庭/安全
29 项（41.52 秒），以及独立 PostgreSQL 测试数据库中的并发和实际迁移 5 项（35.73 秒），均通过且未跳过。
25 个应用源码哈希与 Git、冻结快照逐项一致；初始 10 份源码、当前 9 份准备/OCR 源码、30 份模型文件、
6 份冻结制品及 64 文件/124 页分母均经独立核对。原有 72 项文档登记的发布、实现和证据关联完整保留，
73 份文档校验通过。该结论限于 B1-02，PR CI、合并、源码发布及生产门禁仍需各自满足。

```powershell
python -X utf8 -m pytest -q tests/processing tests/documents tests/patients tests/security tests/accounts/test_account_deletion.py tests/operations/test_audit.py -m 'not ocr_model and not postgres'
python -X utf8 tools/run_required_tests.py -q tests/browser/test_material_browser.py tests/browser/test_ac02_upload_browser.py tests/browser/test_upload_interactions_browser.py tests/browser/test_family_browser.py
# 先配置独立测试数据库的 PHR_POSTGRES_TEST_URL。
python -X utf8 tools/run_required_tests.py -q --ds=config.settings.postgres_test tests/integration/test_material_postgres_concurrency.py tests/processing/test_material_migration.py tests/patients/test_family_migration.py
npm run test:js
python -X utf8 manage.py check --settings=config.settings.test
python -X utf8 manage.py makemigrations --check --dry-run --settings=config.settings.test
python -X utf8 tools/verify_documentation.py
```

关键行为用例见[分类](../../tests/processing/test_material_classification.py)、
[恢复与原件](../../tests/processing/test_material_recovery.py)、
[家庭权限](../../tests/documents/test_material_family.py)、
[真实并发](../../tests/integration/test_material_postgres_concurrency.py)和
[实际 OCR](../../tests/processing/test_material_local_ocr.py)。

## 新鲜本地 OCR 与分类复放

最初候选 `material-1` 在完整授权开发集实际运行本地 PP-OCRv5 mobile det/rec；文本型 PDF 使用文本层。
输入共 64 个文件、124 页，得到 63 个 `DOCUMENT`、1 个 `UNCERTAIN`，逐页为 123 / 1，
没有 `NON_DOCUMENT`。原件哈希全部保持，拦截记录中的网络连接尝试为 0，耗时 3329.884 秒。
54,756 个 OCR 区域均可进行来源回映；这个计数仅说明映射存在，不替代人工坐标金标准。
源码、30 个本地模型文件、输入清单和完整结果在修改分类器前冻结。

最终规则版本为 `material-2`。它对冻结运行的已有测量进行**分类复放，没有重新运行全量 OCR**：
123 页原有文字/纸张依据路径不变，仅 1 页进入视觉判断复放，该页继续保持不确定。
64 个文件及 124 页的分类结果均无变化。准备与 OCR 的九个源码文件逐项保持同一哈希；
分类器变更及两个版本的哈希在机器制品中分别记录。

这是医疗开发样本的误提示守卫，不提供生活照片召回率、独立保留集准确率或医学准确率。
没有删去困难样本，也没有用减少识别或丢弃页面换取结果。

## 真实照片与恢复证据

公开开发照片取自固定 OpenCV 4.12.0：
[fruits.jpg](https://github.com/opencv/opencv/blob/4.12.0/samples/data/fruits.jpg) 与
[basketball1.png](https://github.com/opencv/opencv/blob/4.12.0/samples/data/basketball1.png)。
照片未纳入 Git；原图哈希与分类信号保存在机器制品，测试会核对哈希。

| 原件 | 最初 `material-1` 实际 OCR | 最终 `material-2` 实际 OCR | 恢复检查 |
| --- | --- | --- | --- |
| 彩色水果照片 | `UNCERTAIN`，0 文字区域 | `NON_DOCUMENT`，0 文字区域 | 完整 pipeline 后保留、再次实际 OCR、恢复自动判断；原件/配额/历史保持 |
| 灰度篮球照片 | `UNCERTAIN`，0 文字区域 | `UNCERTAIN`，0 文字区域 | 原件和完整页面保留 |

水果照片暴露了最初密集纹理条件对较平滑真实照片的漏判，完整 pipeline 回归先失败后修正。
新增条件要求更高的色彩多样性与灰度变化，同时保留稀疏文字、彩色面积和背景约束。
没有整体降低原来的阈值；灰度照片、空白、笔画和低纹理混合 PDF 页继续保持不确定。
这两张照片用于开发检查，其中水果参与了规则调整，不把它们描述为独立测试集。

模型测试需要预备本地模型，并把已下载的这两张公开照片目录设置为
`PHR_MATERIAL_PUBLIC_PHOTOS_DIR`。实际执行时禁止网络连接；公开测试不依赖私有医疗样本存在。

```powershell
$env:PHR_RUN_OCR_MODEL = '1'
$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = 'True'
$env:OMP_NUM_THREADS = '2'
# PHR_MATERIAL_PUBLIC_PHOTOS_DIR 指向两张已下载且哈希匹配的公开照片目录。
python -X utf8 tools/run_required_tests.py -q tests/processing/test_material_local_ocr.py tests/processing/test_material_classification.py
```

实际环境为 Windows / Python 3.11.9、PaddleOCR 3.7.0、Paddle 3.3.1。
本机模型测试有 Requests 依赖提示及缺少 ccache 的警告，未发生跳过或网络请求。
本次证据不替代 PR CI、Release Please 源码发布或[生产放行门禁](release-gate.md)。
