# 外部浏览器兼容性验证

本文件只记录真实执行结果，不以代码审查代替浏览器证据。

| 浏览器 | 登录/建档 | 上传/返回 | 1280×720 | 1440×900 | 状态 |
| --- | --- | --- | --- | --- | --- |
| Chrome 151.0.7922.109（Windows） | 已自动验证 | 已自动验证 | 已验证 | 已验证 | 本机完成 |
| Edge 152.0.4191.53（Windows） | 已自动验证 | 已自动验证 | 已验证 | 已验证 | 本机完成 |
| Safari（macOS） | 待外部设备验证 | 待外部设备验证 | 待验证 | 待验证 | 外部待验证 |

本机没有 macOS/Safari，故 AC-22 与 SCN-26 在追踪矩阵中保持
`external_pending`。发布门禁不得把它们改为 `verified`，除非附上真实
Safari 执行记录、版本、视口和失败响应/控制台检查结果。

2026-08-31 最终代码本机执行结果：Chrome `2 passed in 12.02s`，Edge
`2 passed in 12.83s`；两次运行均覆盖登录、建档、上传、返回首页、
失败响应、控制台错误以及 1280×720 和 1440×900 横向溢出检查。

上述结果是当前浏览器冒烟证据。PRD 还要求 Chrome/Edge 当前及前两个主版本、Safari
当前及前一个主版本完成 P00–P08（含搜索、原件查看和删除）。完整 Playwright 套件已实现，
但尚未在正式 HTTPS 验收环境运行；因此上线门禁中的所有完整浏览器项仍为 `pending`。

自动化入口：

```powershell
$env:PHR_BROWSER_EXECUTABLE='C:\Program Files\Google\Chrome\Application\chrome.exe'
python -m pytest tests/browser -q

$env:PHR_BROWSER_EXECUTABLE='C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
python -m pytest tests/browser -q
```
