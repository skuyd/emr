# 健康之家暖笺 UI 验证记录

## 范围与证据边界

Task 9 新增 `tests/e2e/health-home-warm-ui.spec.ts`，与既有的
`tests/e2e/phr-v1.spec.ts` 一起覆盖匿名认证/政策页面、首次使用与引导、首页、上传、档案
搜索筛选、详情与原件查看、趋势、任务状态入口、通知、我的、隐私/敏感信息、文档删除
确认和账号删除确认。每个可用页面检查 1440×900、768×1024、390×844 视口，并在可用时
检查强制配色、减弱动画、焦点、标签、44px 控件和无外部字体请求。运行时观察器会记录
console/page error 以及 4xx/5xx 响应，失败即使测试失败。

测试只接受仓库外的合成 release 输入：

- `PHR_E2E_BASE_URL`
- `PHR_E2E_STORAGE_STATE`
- `PHR_E2E_ONBOARDING_STORAGE_STATE`
- `PHR_E2E_UPLOAD_FIXTURE`
- `PHR_E2E_PARTIAL_UPLOAD_SAVED_FIXTURE`
- `PHR_E2E_PARTIAL_UPLOAD_FAILED_FIXTURE`
- `PHR_E2E_QUERY`
- `PHR_E2E_EMPTY_QUERY`
- `PHR_E2E_FILTER_TYPE`
- `PHR_E2E_FILTER_STATUS`
- `PHR_E2E_FILTER_YEAR`
- `PHR_E2E_FILTER_MONTH`
- `PHR_E2E_DOCUMENT_ID`
- `PHR_E2E_DELETE_DOCUMENT_ID`
- `PHR_E2E_TREND_CODE`
- `PHR_E2E_POLICY_UNAVAILABLE_URL`
- `PHR_E2E_DELETE_ACCOUNT_STORAGE_STATE`

`PHR_E2E_QUERY` 及筛选值必须对应超过一页的合成档案，`PHR_E2E_EMPTY_QUERY` 必须确定无结果；两份
partial-upload fixture 必须分别产生一个已保存结果和一个 `UPLOAD_FAILED`。政策 URL 必须来自受控环境并
真实返回通用 503，删除账号 storage state 必须属于允许永久删除的一次性合成账号，不能复用人工账号或
普通回归账号。

Task 9 套件缺少输入时显式 skipped，不把 skip 当作通过；输入不得包含凭据、患者标识或医疗原文。
既有 `phr-v1.spec.ts` 保持 P00–P08 正式门禁，缺少基础地址或会话会直接 fail-fast；因此聚合脚本的
本地运行仍可能在 legacy 门禁处失败，这不是浏览器通过证据。
本机当前未提供正式 HTTPS 验收地址、隔离 storage state、合成上传样本或稳定文档/趋势
标识，故本次不声称外部 P00–P08 流程通过。

正式验收还必须设置 `$env:PHR_E2E_RELEASE="1"`。该模式对 Task 9 套件的任何缺失输入 fail-fast，确保
release job 不能以 skipped 伪装通过；legacy P00–P08 始终保持 fail-fast。未设置该变量的 Task 9 运行仅
属于本地契约发现。

## 本次执行

- `npx playwright test --list`：57 项在 Chrome、Edge、WebKit 三项目中被发现并成功编译。
- `npx playwright test tests/e2e/health-home-warm-ui.spec.ts --project=chrome-current`：10 skipped（缺少
  `PHR_E2E_BASE_URL`；0 failed），仅为本地契约发现。
- `npm run test:e2e:chrome`：legacy P00–P08 因缺少 `PHR_E2E_BASE_URL` fail-fast（9 failed）；Task 9
  用例显式 skipped（10 skipped），未计为浏览器通过。
- `npm run test:e2e:edge`：同一缺失输入条件下 legacy 门禁 fail-fast（9 failed）；Task 9 用例显式 skipped
  （10 skipped），未计为浏览器通过。
- `npm run test:e2e:webkit-reference`：本机缺少 Playwright WebKit executable，13 项（包括尝试启动浏览器的
  新套件匿名、引导、政策、账号删除页与既有 P00/P01）在浏览器启动或 legacy 门禁阶段 failed、其余 6 项 skipped；这不是 WebKit
  通过证据。WebKit 仅为兼容性参考，不能替代 Safari。
- `PHR_E2E_RELEASE=1 npm run test:e2e:chrome`：按设计在缺少 `PHR_E2E_BASE_URL` 时 fail-fast；该失败是
  release 输入门禁，不是浏览器通过证据。
- `PHR_E2E_VISUAL_BASELINES=1` 未设置；没有伪造或手写截图，也没有宣称视觉基线已批准。
  在 `PHR_E2E_RELEASE=1` 的正式模式下若未设置该变量，视觉用例会 fail-fast；仅本地契约模式允许跳过。

## 真实环境运行手册

在确定性样本和仓库外 storage state 准备完毕后，分别运行：

```powershell
npm ci --ignore-scripts
$env:PHR_E2E_RELEASE="1"
$env:PHR_E2E_VISUAL_BASELINES="1"
npm run test:e2e:chrome -- --update-snapshots
npm run test:e2e:edge -- --update-snapshots
npm run test:e2e:webkit-reference
```

截图将由真实 Playwright 浏览器写入
`tests/e2e/health-home-warm-ui.spec.ts-snapshots/`；Chrome 和 Edge 必须分别保留项目名、
版本、操作系统、视口、执行时间和制品 SHA-256。WebKit 结果只能记录为参考，不能声称
Safari 证据。发布前还需运行现有 Python 浏览器、安全、全量回归、PostgreSQL 并发、OCR
模型、真实短信/对象存储和人工读屏验收门禁。
