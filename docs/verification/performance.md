# 固定负载性能验证

## 当前结论

文档整理日期：2026-10-20。性能工具和合成数据生成器已有静态/单元验证，
但尚无固定容量环境的真实负载验收证据。当前没有 P50/P95/P99 数字，
[发布门禁](release-gate.md)保持 `BLOCKED`；空白结果不得填成 0 或“预计通过”。
本次整理未执行负载测试。旧预演中缺少 k6、Docker 或外部服务的描述仅适用于当时环境，
不能用于判断当前机器条件；执行前须重新确认下列固定环境与服务依赖。

## 固定环境与数据

- Web：4 vCPU/8 GiB；CPU Worker：4 vCPU/16 GiB；PostgreSQL：4 vCPU/8 GiB；
- 对象存储同地域；下行 50 Mbps、上行 20 Mbps、RTT ≤50 ms；
- 100 个独立在线账号；每账号 300 份文档；每文档平均 3 页；每页 2,000 OCR 字符；
- 10 个并发上传批次，每批 20 个不同的 3 页合成 PDF，共 60 页；
- 至少一个 60 页批次在全部原件上传后 5 分钟内出现首个整理结果。

`seed_performance_environment` 会生成数据库基准和 100 个独立会话；
`generate_performance_fixtures.py` 会生成 200 个内容唯一、无医疗信息的三页 PDF。k6 在
`setup()` 中强制校验以上规模，不满足即终止。

## 阈值

| 指标 | PRD 阈值 | P50 | P95 | P99 | 失败率 | 状态 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 首页 HTTP 响应 | P95 <3,000 ms | 待运行 | 待运行 | 待运行 | 待运行 | BLOCKED |
| 病案首屏 HTTP 响应 | P95 <3,000 ms | 待运行 | 待运行 | 待运行 | 待运行 | BLOCKED |
| 300 文档搜索 | P95 <2,000 ms | 待运行 | 待运行 | 待运行 | 待运行 | BLOCKED |
| 原件首张/首页 | P95 <3,000 ms | 待运行 | 待运行 | 待运行 | 待运行 | BLOCKED |
| 单文件原件持久化 | 记录分位数 | 待运行 | 待运行 | 待运行 | 待运行 | BLOCKED |
| 首个整理结果 | max ≤300,000 ms | 待运行 | 待运行 | 待运行 | 待运行 | BLOCKED |

k6 对所有请求先记录耗时再判断成功，`request_failures` 和 `first_result_failures` 均要求
`rate==0`，所以失败样本不会从百分位数中消失。输出强制包含中位数（P50）、P95、P99。
浏览器的 `domInteractive` 由 Playwright 单独采集并要求首页/病案 ≤3 秒；k6 HTTP 耗时不
冒充浏览器可交互时间。

## 执行

在固定环境中先按[生产运行手册](../deployment/production-runbook.md#10-浏览器与性能验收数据)
生成合成数据和 fixture，然后：

```powershell
$env:PHR_K6_SESSION_FILE="X:\phr-evidence\sessions.json"
$env:PHR_K6_FIXTURE_MANIFEST="X:\phr-evidence\fixtures\fixture-manifest.json"
$env:PHR_K6_BROWSE_DURATION="3m"
k6 run --summary-export "X:\phr-evidence\k6-summary.json" `
  tests/performance/k6-upload-search.js
```

在 100 VU 浏览阶段并行运行 Playwright Chrome 的交互时间用例，受控输入及启动命令见
[浏览器操作说明](browser-accessibility.md#自动化命令)。保存：源码身份、执行时间、资源规格、网络整形
配置、镜像摘要、数据库/Redis/S3 版本、Worker 数量、k6 原始输出、summary JSON、
Playwright JSON、服务端监控截图及制品 SHA-256。报告必须列出冷/热缓存策略和所有失败请求。

只有所有阈值通过、错误率为 0、队列持续有明确状态且首个结果不超过 5 分钟，性能门禁才
能改为 `passed`。
