# 浏览器与无障碍验证

## 当前结论

本机已真实运行 Chrome 151.0.7922.109 和 Edge 152.0.4191.53 的登录、建档、上传、返回
首页及 1280×720/1440×900 溢出检查，二者均为 `2 passed`。这属于当前版本冒烟证据，
不等于 PRD 要求的全部支持版本和 P00–P08 正式环境证据。

新增的 `tests/e2e/health-home-warm-ui.spec.ts` 与既有 `tests/e2e/phr-v1.spec.ts` 已由 Playwright
成功发现并编译，共 45 个项目化用例，覆盖：

- P00 登录/隐私、P01 三项同意、P02 首页、P03 上传；
- P04 搜索、P05 详情、P06 原件首屏、P07 趋势、P08 设置；
- 合成文档二次确认删除及删除后 404；
- 1440×900、768×1024 与 390×844 无横向溢出；
- 页面地标、单一 h1、持久标签、可见键盘焦点、44px 触控目标、强制配色、减弱动画和无外部字体请求；
- 仅在显式设置 `PHR_E2E_VISUAL_BASELINES=1` 时执行真实截图基线。

该完整套件尚未连接正式 HTTPS 验收环境运行，因此当前发布状态仍为 `BLOCKED`。

## 支持矩阵

| 环境 | 当前证据 | 发布状态 |
| --- | --- | --- |
| Chrome 当前主版本 | Windows 本机冒烟通过；完整 P00–P08 待运行 | 待验证 |
| Chrome 前 1、前 2 主版本 | 尚无对应二进制/环境 | 待验证 |
| Edge 当前主版本 | Windows 本机冒烟通过；完整 P00–P08 待运行 | 待验证 |
| Edge 前 1、前 2 主版本 | 尚无对应二进制/环境 | 待验证 |
| Safari 当前主版本 | 缺少 macOS/Safari 真机 | 待验证 |
| Safari 前 1 主版本 | 缺少对应 macOS/Safari 真机 | 待验证 |

Playwright 的 `webkit-reference` 只能作为 WebKit 引擎回归参考，绝不等同于 Safari，也不能
解除 AC-22/SCN-26 的 Safari 阻断。

## 自动化命令

先按运行手册生成仓库外的合成浏览器状态和 fixture，然后设置：

```powershell
$env:PHR_E2E_BASE_URL="https://phr-staging.example.com"
$env:PHR_E2E_STORAGE_STATE="X:\phr-evidence\browser-state.json"
$env:PHR_E2E_ONBOARDING_STORAGE_STATE="X:\phr-evidence\onboarding-state.json"
$env:PHR_E2E_UPLOAD_FIXTURE="X:\phr-evidence\fixtures\synthetic-b01-f01.pdf"
$env:PHR_E2E_QUERY="synthetic-token-001"
$env:PHR_E2E_DOCUMENT_ID="从 sessions.json 第 1 项读取 viewer_document_id"
$env:PHR_E2E_DELETE_DOCUMENT_ID="从 sessions.json 第 1 项读取 delete_document_id"
$env:PHR_E2E_TREND_CODE="SYNTHETIC_METRIC"
$env:PHR_E2E_VISUAL_BASELINES="1" # 只有已准备真实确定性样本并准备更新基线时才设置

npm ci --ignore-scripts
npm run test:e2e:chrome
npm run test:e2e:edge
npm run test:e2e:webkit-reference
```

新 Task 9 契约套件在本地缺少输入时会安全地以 skipped 状态结束，绝不将缺少环境计为通过；既有
`phr-v1.spec.ts` 保持正式 P00–P08 门禁，缺少 `PHR_E2E_BASE_URL` 等输入会直接 fail-fast。正式验收必须
设置 `$env:PHR_E2E_RELEASE="1"`；Task 9 套件在此模式下同样对任何缺失输入 fail-fast，不能静默跳过。
`npx playwright test --list` 只证明用例可编译和被发现，不属于浏览器验收证据。设置
`PHR_E2E_VISUAL_BASELINES=1` 后，首次生成基线必须在真实 Chrome/Edge 上运行
`npx playwright test --project=chrome-current --update-snapshots` 与对应的 Edge 命令；不能
手写或复制截图二进制。当前工作区没有正式 HTTPS、隔离 storage state、合成上传样本和文档
/趋势 ID，因此没有截图基线或 P00–P08 外部流程被声称通过。

Chrome/Edge 历史主版本必须在固定浏览器镜像或受管测试机分别执行相同套件。Safari 当前/
前一版本在真实 macOS 上手工走同一 P00–P08 清单，同时保存版本、操作系统、视口、控制台、
失败响应、截图和执行时间。

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
