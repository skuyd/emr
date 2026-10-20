# 外部浏览器兼容性验证

文档整理日期：2026-10-20。完整支持矩阵的外部验收仍未完成，原 PRD 的 AC-22 与
SCN-26 保持 `external_pending`，生产门禁中的 8 项完整浏览器流程仍为 `pending`。
当前版本的功能专项见[验证入口](README.md)，完整流程和支持版本要求见
[浏览器与无障碍验证](browser-accessibility.md)。

## 已有历史冒烟

以下仅记录 2026-09-05 的真实执行，不以旧浏览器版本冒烟代替当前版本验收：

| 浏览器 | 登录/建档 | 上传/返回 | 1280×720 | 1440×900 | 状态 |
| --- | --- | --- | --- | --- | --- |
| Chrome 152.0.7977.75（Windows） | 已自动验证 | 已自动验证 | 已验证 | 已验证 | 历史冒烟通过 |
| Edge 152.0.4191.62（Windows） | 已自动验证 | 已自动验证 | 已验证 | 已验证 | 历史冒烟通过 |
| Safari（macOS） | 无完整外部证据 | 无完整外部证据 | 待验证 | 待验证 | 外部待验证 |

该次预演结果：Chrome `6 passed in 43.28s`，Edge
`6 passed in 37.41s`；两次运行均覆盖登录、建档、上传、返回首页、密码重置、旧账号升级、
通知与响应式外壳，并检查失败响应、控制台错误和横向溢出。完整命令与版本见
[历史门禁预演制品](artifacts/local-gate-rehearsal-result.json)。当次未具备 macOS/Safari
真机，该原因不描述当前开发机状态。

## 当前验收要求

Chrome/Edge 当前及前两个主版本、Safari 当前及前一个主版本均须完成当前完整流程，
包括搜索、原件查看和删除；已撤销的趋势页面不再属于验收范围。正式 HTTPS 环境、
真实 Safari、完整流程及人工无障碍证明未闭合，不能由代码审查、测试收集或本地专项代替。

本地真实浏览器测试入口如下；此命令与外部完整矩阵分别记录，不自动更新放行结论：

```powershell
$env:PHR_BROWSER_EXECUTABLE='C:\Program Files\Google\Chrome\Application\chrome.exe'
python -m pytest tests/browser -q

$env:PHR_BROWSER_EXECUTABLE='C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
python -m pytest tests/browser -q
```

外部套件命令和受控合成输入见[操作说明](browser-accessibility.md#自动化命令)。
每份验收记录须绑定源码身份、实际浏览器与操作系统版本、视口、执行时间、命令、
控制台及失败响应检查、结果和制品 SHA-256。真实 Safari 证明必须来自 macOS/Safari；
Playwright `webkit-reference` 不能替代。缺失证明的项目继续保持待验证。
