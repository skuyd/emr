# 浏览器与无障碍验证

## 当前结论

文档整理日期：2026-10-20。完整外部浏览器流程与人工无障碍验收尚未闭合，
[生产门禁](release-gate.md)仍为 `BLOCKED`。当前源码版本的专项验证范围见
[验证入口](README.md)；本次整理没有重新执行浏览器验证。

2026-09-05 的 Chrome 152.0.7977.75、Edge 152.0.4191.62 各 `6 passed` 仅为历史冒烟。
版本、命令及范围见[兼容性记录](external-compatibility.md)和
[历史预演制品](artifacts/local-gate-rehearsal-result.json)，不代替当前版本的完整验收。

## 当前自动化范围

已检入的 `tests/e2e/health-home-warm-ui.spec.ts` 与 `tests/e2e/phr-v1.spec.ts` 包含：

- P00 登录/隐私、P01 三项同意、P02 首页、P03 上传；
- P04 档案列表搜索、P05 详情、P06 原件首屏、P08 设置；
- 合成文档二次确认删除及删除后 404；
- 登录、重置、部分上传失败、政策不可用与一次性合成账号删除；
- 桌面、平板与手机视口下无横向溢出；
- 页面地标、单一 h1、持久标签、可见键盘焦点、44px 触控目标、强制配色、减弱动画和无外部字体请求；
- 仅在显式设置 `PHR_E2E_VISUAL_BASELINES=1` 时执行真实截图基线。

旧 P07 趋势已随[功能删减](../archive/verification/feature-pruning.md)撤销，不再列入当前流程。
以上是测试覆盖定义，不表示已在正式 HTTPS 验收环境通过。

## 支持矩阵

| 验收时的浏览器版本范围 | 所需证据 | 状态 |
| --- | --- | --- |
| Chrome 当前、前 1、前 2 主版本 | 每个版本独立完成当前完整流程 | 待验证 |
| Edge 当前、前 1、前 2 主版本 | 每个版本独立完成当前完整流程 | 待验证 |
| Safari 当前、前 1 主版本 | 真实 macOS/Safari 完整流程 | 待验证 |
| 上述全部受支持浏览器 | 键盘、读屏、缩放和高对比度人工记录 | 待验证 |

Playwright 的 `webkit-reference` 只能作为 WebKit 引擎回归参考，绝不等同于 Safari，也不能
解除 AC-22/SCN-26 的 Safari 阻断。历史记录中缺少设备或软件的原因只描述当次运行环境；
执行验收时应重新记录实际浏览器、操作系统及环境条件。

## 自动化命令

先按运行手册生成仓库外的合成浏览器状态和 fixture，然后设置：

```powershell
$env:PHR_E2E_BASE_URL="https://phr-staging.example.com"
$env:PHR_E2E_STORAGE_STATE="X:\phr-evidence\browser-state.json"
$env:PHR_E2E_ONBOARDING_STORAGE_STATE="X:\phr-evidence\onboarding-state.json"
$env:PHR_E2E_UPLOAD_FIXTURE="X:\phr-evidence\fixtures\synthetic-b01-f01.pdf"
$env:PHR_E2E_PARTIAL_UPLOAD_SAVED_FIXTURE="从受控 fixture 清单读取已保存样本"
$env:PHR_E2E_PARTIAL_UPLOAD_FAILED_FIXTURE="从受控 fixture 清单读取预期上传失败样本"
$env:PHR_E2E_QUERY="synthetic-token-001"
$env:PHR_E2E_EMPTY_QUERY="从 sessions.json 读取保证无结果的合成查询"
$env:PHR_E2E_FILTER_TYPE="从 sessions.json 读取分页档案类型"
$env:PHR_E2E_FILTER_STATUS="从 sessions.json 读取分页档案状态"
$env:PHR_E2E_FILTER_YEAR="从 sessions.json 读取分页档案年份"
$env:PHR_E2E_FILTER_MONTH="从 sessions.json 读取分页档案月份"
$env:PHR_E2E_DOCUMENT_ID="从 sessions.json 第 1 项读取 viewer_document_id"
$env:PHR_E2E_DELETE_DOCUMENT_ID="从 sessions.json 第 1 项读取 delete_document_id"
$env:PHR_E2E_POLICY_UNAVAILABLE_URL="从受控 release fixture 读取真实返回 503 的 URL"
$env:PHR_E2E_DELETE_ACCOUNT_STORAGE_STATE="X:\phr-evidence\disposable-delete-account-state.json"
$env:PHR_E2E_RELEASE="1"
$env:PHR_E2E_VISUAL_BASELINES="1" # 正式验收必须准备真实确定性样本及截图基线

npm ci --ignore-scripts
npm run test:e2e:chrome
npm run test:e2e:edge
npm run test:e2e:webkit-reference
```

`health-home-warm-ui.spec.ts` 在非发布模式缺少输入时记录 `skipped`，不计为通过；
`phr-v1.spec.ts` 缺少 `PHR_E2E_BASE_URL` 等必需输入会直接失败。正式验收必须设置
`PHR_E2E_RELEASE="1"`；此时两个套件都拒绝缺失输入，不能静默跳过。
`npx playwright test --list` 只证明用例可编译和被发现，不属于浏览器验收证据。设置
`PHR_E2E_VISUAL_BASELINES=1` 后，首次生成基线必须在真实 Chrome/Edge 上运行
`npx playwright test --project=chrome-current --update-snapshots` 与对应的 Edge 命令；不能
手写或复制截图二进制。执行前须准备 HTTPS 验收环境、隔离 storage state、合成上传样本及文档 ID。
账号删除状态必须是明确允许永久删除的一次性合成账号；政策 URL 必须真实返回通用 503。若这些受控输入
缺失，release 模式直接失败，绝不使用普通账号、假 200 页面或客户端路由模拟代替。
在 `PHR_E2E_RELEASE=1` 下未设置 `PHR_E2E_VISUAL_BASELINES=1` 也会使视觉检查 fail-fast；只有未设置
release 模式的本地契约发现允许将视觉用例标记 skipped。

Chrome/Edge 历史主版本必须在固定浏览器镜像或受管测试机分别执行相同套件。Safari 当前/
前一版本在真实 macOS 上手工走上述当前流程，同时保存版本、操作系统、视口、控制台、
失败响应、截图和带时区执行时间，并按[门禁要求](release-gate.md)记录制品 SHA-256。

## 无障碍人工补充项

每个受支持浏览器还要完成以下人工检查：

1. 只用 Tab、Shift+Tab、Enter、Space 和方向键完成登录、上传、搜索、查看及删除；
2. 焦点顺序符合视觉顺序，焦点环持续可见，不被固定区域遮挡；
3. 200% 浏览器缩放下不丢失控件或产生核心流程横向滚动；
4. Windows 高对比模式和 macOS 增强对比度下，状态仍有文字/图标，不只依赖颜色；
5. 浏览器/系统读屏验证表单标签、错误提示、状态 live region、对话标题和地标名称；
6. 原件查看器的翻页、缩放、旋转、缩略图和返回路径均可键盘操作。

任何跳过、崩溃、控制台错误、资源 4xx/5xx、遮挡或无法完成的键盘步骤都记为失败，不得
以“主流浏览器应该兼容”替代证据。
