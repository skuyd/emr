# 第三批云影像受控打开验证

云影像 PR 2 已在本地实现说明页、主动打开、完整来源与访问资格复查，以及打开审计。
当前应用提交为 `6d6315b4ccdbe21553c40188020c006d15fe1efa`；本地 HTTP、PostgreSQL 和
真实 TLS 浏览器验证通过，非作者审查与远端精确 head CI 尚待完成，尚未合并或发布。

本文 `verified` 只指下列可复核的本地执行事实。[三 PR 规格](../specs/2026-09-08-cloud-imaging-sources.md)
和[实施计划](../plans/2026-09-08-cloud-imaging-sources-implementation.md)仍为 `implementing`。
PR 3 的选定输出与有限分享尚未实施；[PR 1 真实识别的质量限制](batch-three-cloud-imaging-pr1.md)
与[生产门禁](release-gate.md)不由本次功能检查改变。

## 已实现的使用路径

| 入口 | 用户操作与返回约束 |
| --- | --- |
| 来源核对结果 | 当前已确认来源显示内部“查看外部访问说明”链接；普通 READ 成员可查看并主动打开，核对决定仍需 WRITE |
| GET / HEAD 说明页 | 只显示安全站点名称、确认修订及外部访问说明，不在预加载链接、页面数据属性中放入完整目标或访问参数 |
| 显式 POST 打开 | 提交内部来源 ID、患者范围、原来源令牌和修订，执行真实 CSRF 检查；目标只从当前已确认存储来源取得，不接受任意 url / next |
| 来源或资格变化 | 说明页渲染后与目标响应构建后重验；过期来源返回恢复提示，撤权或不可访问原件拒绝，旧目标不交付 |
| 审计管理 | 按“发起打开云影像外站”筛选；实际 actor、患者、来源、请求身份与结果沿既有审计规范保存，不记录访问串 |

完整依赖沿 PR 1 包括原件/页、解析、报告归属、来源修订、当前及历史作者。作者清理、
报告排除、资料回收或成员撤权不能由旧页面继续打开。无效 POST 的表单和最终令牌仍绑定
同一初始摘要，保留 PR 1 已修复的表单快照边界。真实决定没有事后回滚。

目标网址的登录、验证码和影像展示由外部站点负责；本地校验不查询 DNS、不向医院发起
HEAD/GET，也不验证域名归属或第三方可用性。已由用户主动交给外站的访问内容不能撤回。

## 浏览器与访问串保护

最初真实 TLS 浏览器测试暴露了原生表单与隐私策略的冲突：无 Referer 的跨窗口表单实际
产生 `Origin: null`，Django 拒绝请求。两个原失败保留；修复没有接受空 Origin 或放宽 CSRF。

现在脚本在真实用户点击时只预开空窗，立即隔离 opener 并设置 no-referrer。同源 fetch
携带浏览器生成的正确 Origin 和 CSRF，禁止跟随重定向；服务在最终复查后返回 200 空正文
及受保护的 Location，客户端才让空窗导航。直接 POST 客户端仍获得同样校验后的 303。
不依赖跨站 `redirect:manual` 读取不可见响应，也不由 fetch 在后台访问外站。

真实 Chromium 验证成功路径没有外站 Referer、`document.referrer` 或 opener；来源过期和
网络失败关闭预开空窗，当前页面显示恢复入口。无 JavaScript 时按钮禁用并明确说明，
用户仍可返回来源查看原页。桌面 1280px 与手机 360px 均完成打开、更正失效和重新打开。

站点显示采用与浏览器一致的 UTS 46 非过渡 IDNA，保留合法 path/query/fragment 的语义。
非法协议、账号嵌入、控制字符、内网/数字主机别名、无效端口和不完整转义拒绝。IDNA
依赖原已在锁中，本次声明为直接依赖；锁的版本和哈希未变。

说明与打开端点，包括早期 CSRF、权限、方法和异常响应，均设置 private/no-store、
no-referrer 和 opener 隔离策略。专用错误报告省略请求输入、目标及敏感局部变量，保留
稳定错误、栈与请求身份。实际 Django 日志处理器的捕获证明恶意 Origin 中的访问路径
不会写入日志。原始 Fact、OCR、私有来源和已确认字段没有因此被改写。

## 实际验证与身份

功能分支从先 fetch 的实际 `origin/main` `9675f0e3f61f96eb4895c364229b9da9d8a27bdb`
建立；没有从未合并功能或文档分支起步。537 个应用文件与全部 1178 个受测 Git 文件的
Git/checkout 身份分别封存。版本字段保持主线状态，可移植数据仍为 1.4，本 PR 未新增数组。

| 验证范围 | 结果 | 精确范围限制 |
| --- | --- | --- |
| 受控打开、URL、日志、依赖与动态授权矩阵 | 75 passed / 0 skipped | 当前应用；随后只加强四个错误测试对真实日志处理器的捕获 |
| 最终受控打开 HTTP 文件 | 22 passed / 0 skipped | 包含加强后的四项实际错误日志断言 |
| 独立 PostgreSQL 竞争 | 18 passed / 0 skipped，4 deselected | 16 项新打开边界与 2 项 PR 1 无效表单回归；4 项未选中的是普通实例 |
| PostgreSQL + TLS Chromium | 6 passed / 0 skipped | 桌面、手机、网络失败、无 JS、Unicode 参数和 IDNA 实际目标 |
| 保留的较早领域回归 | 172 passed，2 deselected | 早于最终 IDNA 修正，不冒称最终提交重新执行了全套 |

各次范围重叠，不能相加为独立用例总数。PG 使用真实两连接和正常 COMMIT：在说明页、
303 响应及 200 导航响应构建后提交修订、撤权、作者清理或回收；另有实际未提交事务的
锁等待，以及报告排除发生在无效 POST 校验期间。最终返回不包含旧 Location 或旧表单。
SQLite 浏览器共享内存夹具的串行调度没有作为 PG 并发证据。

浏览器使用真实本地 TLS socket 和临时自签名证书，正确的 Origin/Referer 来自浏览器，
没有使用测试请求头代替。全部外站请求都由保留域合成响应拦截，未访问任何真实医院。
CI 配置已将这六项浏览器测试纳入必跑选择；未以被跳过的浏览器测试宣称通过。

本机 Python 3.11.9、Django 5.2.17、pytest 8.4.2、PostgreSQL 18.6、Playwright 1.62.0；
本机 idna 3.11 与锁中的 3.19 分开记录。Django check、迁移检查、JavaScript 语法、版本
一致性与差异空白检查均 exit 0；没有新增迁移，也没有手改自动版本。

文档登记校验为 101 份通过，文档/标题/版本/依赖工具回归为 94 passed、2 skipped。
两个跳过都是 Windows 未获创建符号链接权限（WinError 1314）的文档边界测试，不是业务
通过；本次 HTTP、PG 和浏览器没有跳过。

原失败、范围、XML SHA 和检查结果见[测试制品](artifacts/batch-three-cloud-imaging-pr2-tests.json)，
源码和匿名身份见[身份制品](artifacts/batch-three-cloud-imaging-pr2-identity.json)。永久入口为
[HTTP 回归](../../tests/cloud_imaging/test_controlled_open.py)、
[地址规则](../../tests/cloud_imaging/test_url_policy.py)、
[PG 竞争](../../tests/integration/test_cloud_open_postgres.py)和
[TLS 浏览器](../../tests/browser/test_cloud_open_browser.py)。

## 仍待完成

本次没有重复真实扫描、解码或评分，没有修改原金标、协议和首次结果。PR 1 的页面
TP4/FP3/FN2、定位 TP2/FP5/FN3、载荷 TP2/FN2，以及 108 页未判定继续保留；文献清单的
51 FN 不等于云门户漏识别，当前没有独立断定的明文云门户阳性。

PR 2 仍需独立审查、最终文档交付核验和远端精确提交 CI；发布版本只由实际自动流程确定。
PR 3 仍需明确选择、全部输出格式、有限分享及其独立权限和全阶段失效验证。五批总状态、
真实识别质量和生产放行保持各自原有边界。
