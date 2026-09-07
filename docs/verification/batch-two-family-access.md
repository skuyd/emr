# 第二批多患者与家庭访问权限验证

本记录对应[第 1—5 批规格](../specs/2026-09-07-batches-one-five-requirements.md)的 B2-01、
B2-02 基础，以及[实施计划](../plans/2026-09-07-batches-one-five-implementation.md)的 Task 4。
多患者、角色授权及既有入口改造已实现并通过本地验证和独立审查，已提交
[PR #39](https://github.com/skuyd/emr/pull/39)，等待 CI 与合并；总规格、计划和本记录
保持 `active / implementing`，版本尚未确定，`releases: []`。
邀请、限时选定范围分享和完整访问审计界面属于后续 Task 5，本记录不将整个第二批标记完成。

功能分支 `feat/batch-two-family` 从最新主分支
`b7d5f3485b1b73cacc08476863401c11ae927d46` 创建；实现提交为
`4e3deb9ac7ebf8c9430fc09a4dd120ae7317d72f`。随后同步已合并总规格的主分支
`e7908768f68998f5d33eca7820f14c406ef0fb71`，合并提交
`adeff68dd19dda06b4e14d6e2d8e554d9c41eb09` 只带入四份文档，无源码变化。
独立审查后的资源导航修正提交为 `1c57c469ee6b040aecad9c9af69a611ea1932907`。
提交 PR 前又同步主分支 `9ef8cdcb736df8d37c0dbb6afc726017cee9358d` 的图像增强与自动发布，
合并提交为 `2f6f74d7d2b51e3f18732e896693edbef04a1eba`；只处理了登记表和索引的内容合并，
无源码冲突，未手工修改版本字段。该主分支的 1.4.0 不包含家庭功能，家庭版本继续待确定。
之后同步事实提取主分支 `86d7fb3eabf79cd912611a0e1be263e319e235b1`，合并提交
`51ade0888a50d6b2a33f81be2c1be707147a20dc`。原 101 个已审应用文件中，只有事实详情模板增加
主分支的转录审计 include，其余 100 个 Git blob 不变；权限字段与表单保留，独立集成差异审查通过。
具体命令、计数、路由清单和文件摘要见[去标识验证记录](artifacts/batch-two-family-access.json)。

## 功能与访问规则

账号可以创建、切换和管理多个患者。创建者归属由 `Patient.account` 保留，成员角色独立保存。
默认只有一个可访问患者时自动选择；多个患者未选择时进入列表；删除最后一个自有患者后仍可创建新患者。
患者名称及切换入口在桌面与移动页面持续可见。

| 角色 | 读取 | 修改资料 | 导出 | 管理成员 | 删除患者/所有权 |
| --- | --- | --- | --- | --- | --- |
| 创建者 | 是 | 是 | 是 | 是，含管理员 | 是 |
| 管理员 | 是 | 是 | 是 | 仅协作者和只读成员 | 否 |
| 协作者 | 是 | 是 | 是 | 否 | 否 |
| 只读成员 | 是 | 否 | 否 | 否 | 否 |
| 非成员、撤销成员 | 否 | 否 | 否 | 否 | 否 |

只读成员可以维护自己的通知偏好和已读状态，这些动作不授予病历写权限。
管理员不能升降其他管理员或修改创建者；创建者只能通过专有患者删除流程删除患者。

旧表单和 JS 请求携带显式患者标识；多患者写入缺少显式标识时返回 409。
两个标签页切换患者后，旧标签页提交仍写入原患者，并将该患者带到保存确认页。
缓存的 `request.patient` 或成员对象不能替代实时授权。

独立审查复现了切换会话后旧原件图片、通知及 GET 筛选失去页面患者的问题，随后补充回归并修正：
仅 GET/HEAD 且没有显式患者时，固定白名单路由从不可变资源 ID 查所属患者，再执行同一实时授权。
显式不匹配患者仍返回 404，写入不采用隐式推断；导出保留原有实际发起者与会话校验。
无默认患者的新会话打开通知也在重定向保留通知所属患者。主导航、正文列表入口和趋势入口显式携带患者，
GET 搜索/筛选表单和分页同样保留患者。无解析版本的人工事实使用其文档归属。
回归见[旧页面与资源导航](../../tests/patients/test_family_resource_navigation.py)。

文档、上传批次、处理任务、人工事实/检验修订、反馈和导出保存实际操作者。
导出同时绑定发起账号、发起会话、成员权限修订号和来源状态；同一家庭其他成员不能读取另一成员的
预览、编辑、下载或取消任务。撤权后快照与选项被清空，正在构建的导出不能发布对象。

撤权和角色变更还会失效该成员的推送订阅、用户手动重处理及其授予的专业复核访问。
专业复核继续要求显式有效任务、内部角色与权限，不能获得完整患者上下文。
已持久接受的初次上传属于患者的整理任务，移除上传者后可以继续；手动重处理持续要求发起者具有
WRITE 资格及相同成员 revision。患者删除后两种任务均不能发布当前结果。

患者删除会持久记录逐患者清理任务，覆盖活动、回收站及已请求永久删除的文档，等待文档与导出清理后
再删除患者行。账号注销处理全部自有患者，退出参加的其他家庭并保留其资料和已经接受的贡献。
注销中的旧建档/新增患者请求在账号锁后重验有效状态，不能重新创建资料。

## 旧入口与服务授权覆盖

实际路由清点为 44 个 `patient_scoped` 入口，其中 27 个带动态资源标识。
[CSRF 与 IDOR 测试](../../tests/security/test_csrf_and_idor.py)将全部动态患者路由集合与
测试矩阵作集合相等检查，逐个确认外部患者资源返回 404，并检查全站写入路由的 CSRF 和允许方法声明。

| 范围 | 路由数 | 能力及附加边界 |
| --- | --- | --- |
| 导出准备、预览、PDF、下载、取消 | 5 | EXPORT；实际发起者、会话、revision、来源；下载逐块重验 |
| facts 列表、文档、事实详情 | 3 | GET READ / POST WRITE；实际修订作者 |
| labs 比较、结果、复核授权、来源、来源图片、激活版本 | 6 | 读取 READ、修订/激活 WRITE、授予复核 MANAGE；图片 IO 后重验 |
| 通知列表、已读、打开、推送订阅、撤销订阅 | 5 | READ；回执、偏好与订阅只作用于本人 |
| 回收站、上传、趋势、档案、原件、缩略图、上传批次 API | 19 | GET READ / POST WRITE；最终上传提交、删除及重处理服务再次授权 |
| 首页、个人页、称呼、产品反馈、通知偏好、任务 | 6 | READ；修改患者称呼 WRITE；反馈记录本人，偏好不修改其他成员 |

新增患者列表/创建/切换/成员/删除入口分别使用可访问患者集合、活跃账号及当前同意、READ、MANAGE、OWNER。
账号注销为独立账号范围动作，不依赖当前患者选择。内部复核与词典管理继续沿用任务、内部角色及二次认证边界。

[角色与家庭访问回归](../../tests/patients/test_family_access.py)覆盖五种身份与五种能力，
[租户隔离回归](../../tests/security/test_tenant_isolation.py)覆盖 header、form、query、session
四种患者选择渠道以及缓存成员撤销。旧 labs/facts、原件、导出、通知和后台服务均纳入测试。

## 实际迁移验证

[迁移回归](../../tests/patients/test_family_migration.py)使用 MigrationExecutor 真正回退到旧
OneToOne schema，以历史模型写入合成数据，再执行当前全部迁移；SQLite 与 PostgreSQL 均执行通过。

| 迁移对象 | 迁移后断言 |
| --- | --- |
| Patient | 原主键和账号归属不变，新增创建者 ADMIN 成员 |
| Document / UploadBatch / ProcessingRun | 原主键不变，created_by / requested_by 回填原账号 |
| 已永久删除的文档及 DocumentDeletionJob | 原删除任务主键及关联不变，不重新进入回收站 |
| ExportJob | 原主键、snapshot、session_digest 不变，requested_by 回填原账号 |
| PatientPreference / PushSubscription | 原偏好保留，原订阅主键保留并绑定原账号 |
| TaskNotification | 原 read_at 保留到原账号 NotificationReceipt |
| 明确存在 AccountDeletionJob 的患者 | 补逐患者删除任务、deleted_at 和已撤销创建者成员 |
| 仅账号 is_active=False 的患者 | 保留患者与成员，不将暂时停用推断为永久删除 |

迁移不重写原件或生成新的病例主键。已经产生多患者数据后，旧 OneToOne schema 无法表达这些关系；
发布和恢复应使用前向迁移策略，不能假定可以无损直接回滚旧 schema。

## PostgreSQL 竞争与下载性能

新增[真实 PostgreSQL 并发回归](../../tests/integration/test_family_postgres_concurrency.py)
使用独立合成数据库，验证以下实际竞争：

- 撤销成员与实际作者外键写入串行化，已持有患者守卫的写入先完成，之后拒绝新写入。
- 账号注销与相同外键写入不发生死锁。
- 导出构建暂停时成员被降为只读，恢复后快照清空且没有发布存储对象。
- 处理任务运行时删除患者，恢复后失去 lease，不能成为当前结果。
- 建档请求在注销事务上真实等待，注销完成后拒绝新增患者。

前两项及建档竞争通过 `pg_stat_activity` / `pg_blocking_pids` 验证真实阻塞关系。
主要锁序是 Patient → 有序 UploadBatch → Document → ProcessingRun/ExportJob 等子行。
账号注销使用 `FOR NO KEY UPDATE` 再按 UUID 锁患者，使已取得患者守卫的实际作者外键写入可取得
KEY SHARE 并完成，避免 Account → Patient → Account 死锁。

原件、导出及 inline PDF 使用 256 KiB 块，每个非空块在 IO 前后重验。
EOF 不再执行无意义的后置重验；禁用会绕过读取守卫的 WSGI sendfile。
[流式回归](../../tests/patients/test_family_access.py)实际迭代 1 MiB 合成原件，得到 **4 块、36 次 SQL**，
计数仅包含 `streaming_content` 迭代。旧 4 KiB 默认块的同类实测约 2056 SQL/MiB，查询减少约 98%。
回归保留完整字节相等、块数与查询数上限以及首块之后撤权即停止传输的行为断言。

36 次 SQL 仅适用于上述原件测量。独立审查以相同 1 MiB、4 块测量导出下载：1 个来源文档为
243 次 SQL、约 0.209 秒；25 个来源文档为 459 次 SQL、约 0.449 秒，均为 SQLite 本地
`streaming_content` 迭代范围。导出每块还校验发起会话、来源与生命周期，查询量随来源数量增加；
本任务保留完整撤权重验，第二批完整验收需再用实际导出大小评估，不将原件指标外推为导出指标。

## 执行结果与复现

| 验证 | 实际结果 |
| --- | --- |
| 审查修正后的完整常规 Python 套件 | 1609 passed、2 skipped、45 deselected，364.04 秒 |
| 完整 PostgreSQL 必需套件 | 43 passed、1598 deselected，106.55 秒 |
| 最终新增 PostgreSQL 并发及实际迁移 | 6 passed，31.38 秒 |
| 最终患者、账号注销、family 浏览器专项 | 59 passed，18.32 秒 |
| labs 工作流、家庭权限、迁移、family 浏览器专项 | 70 passed，40.69 秒 |
| 审查修正后的原上传与交互浏览器套件 | 7 passed，28.93 秒 |
| 最终资源导航与导出页面专项 | 14 passed，9.40 秒 |
| 独立复审：安全矩阵、全部反例与真实浏览器 | 56 passed，15.04 秒；导出调整链接追加 14 passed，7.40 秒 |
| 同步图像增强主分支后的患者、处理、导出、通知、安全、删除与真实浏览器 | 315 passed、5 deselected，50.22 秒 |
| 同步事实主分支后的患者、事实、处理、导出、安全与真实浏览器 | 329 passed、5 deselected，54.28 秒 |
| 最终租户选择渠道 | 10 passed，6.61 秒 |
| 最终角色矩阵及流式性能 | 6 passed，5.17 秒 |
| JS 回归 | 6 passed |
| Django check / 迁移漂移检查 | 无问题 / 无待生成迁移 |

初次完整常规套件为 1588 项通过；完成建档活跃账号、角色矩阵、选择渠道及独立审查修正后，重新运行完整套件得到
上述 1609 项通过。最后两处导出调整链接的 query 拼接修正另有 14 项专项及独立重复执行验证。
完整 PostgreSQL 43 项之后新增了一项建档竞争，所以准确记录为“全套 43 项 + 最终专项 6 项”。
两项跳过是 Windows 无符号链接创建权限时的文档/证据 symlink 逃逸测试，已单独核对 WinError 1314；
PostgreSQL 和浏览器专项无跳过。既有 requests 依赖及 Django override DATABASES 警告未在本任务升级依赖处理。

[真实浏览器回归](../../tests/browser/test_family_browser.py)使用本地 Chromium 与 StaticLiveServer：
1280×800 创建第二患者，两个真实标签页验证旧患者表单与保存确认目标，360×780 点击切换并检查当前患者文字及
切换控件处于视口内，没有新页 pageerror。审查修正后还在当前会话选中第二患者时，实际打开第一患者的嵌入原件，
检查浏览器原生图片取得非零 naturalWidth。所有请求限于本地服务器，使用合成账号；不代表真实手机或其他浏览器认证。

```powershell
python -m pytest -q -m 'not postgres and not ocr_model' --ignore=tests/browser/test_ac02_upload_browser.py --ignore=tests/browser/test_upload_interactions_browser.py
python tools/run_required_tests.py -q --ds=config.settings.postgres_test -m postgres tests
python -m pytest -q --ds=config.settings.postgres_test tests/integration/test_family_postgres_concurrency.py tests/patients/test_family_migration.py
python -m pytest -q tests/patients tests/accounts/test_account_deletion.py tests/browser/test_family_browser.py
python tools/run_required_tests.py -q tests/browser/test_ac02_upload_browser.py tests/browser/test_upload_interactions_browser.py
npm run test:js
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python tools/verify_documentation.py
```

PostgreSQL 命令要求预先为 `PHR_POSTGRES_TEST_URL` 配置独立测试库，禁止指向业务数据。
本任务未使用真实医疗样本、向真实用户发送通知或执行生产部署；既有[生产门禁](release-gate.md)
仍是 BLOCKED，8 项通过、15 项待验证，功能测试不替代生产放行。
