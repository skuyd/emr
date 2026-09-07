# 隔离合成体验配置

`config.settings.synthetic_trial` 用于仅含虚构资料的体验环境。它要求显式设置
`SYNTHETIC_TRIAL_ACK=SYNTHETIC-DATA-ONLY`，保留 `DEBUG=False`、安全 Cookie 和 HTTPS，
并将 `PRODUCTION_DEPLOYMENT` 固定为 `False`。未选择该设置模块的环境不会启用体验行为。

## 登录与界面行为

体验账号使用密码和部署者单独提供的六位体验验证码登录；此模式不发送短信。首次注册
和找回密码页面返回 403，页面持续显示合成资料提示，并提供虚构报告下载入口。
账号、密码和验证码通过环境文件与本地运维流程配置，不写入应用源码或公开文档。

体验开关、非生产标志、关闭调试及体验 OTP 提供者必须同时满足，固定验证码才可用。
正式生产配置仍拒绝固定验证码，不应将体验配置直接切换为真实用户环境。

## 运行依赖与配置

- PostgreSQL 数据库、Redis 缓存及 Celery 队列必须单独配置。
- `APP_DOMAIN` 设置为明确的 IPv4 地址；入口须提供受信任的 HTTPS 证书。
- 应用和加密用途的密钥通过环境变量配置，不能沿用开发默认值。
- 原件使用隔离的本地存储卷，离线模型目录由部署环境提供。
- Web、OCR Worker、Control Worker 和单实例 Beat 按实际环境启动。

本地云部署资料的存放与忽略规则见 [AGENTS.md](../../AGENTS.md)。本文仅描述可复用的
应用行为；实际部署配置、地址、凭据和运行证据按该规则保存在项目的私有目录。

## 验证与正式开通

回归覆盖 [体验 OTP 边界](../../tests/accounts/test_synthetic_trial.py)、
[配置与路由隔离](../../tests/deploy/test_synthetic_trial.py) 和
[安全系统检查](../../tests/core/test_checks.py)。验证命令：

```bash
python -m pytest tests/accounts/test_synthetic_trial.py tests/deploy/test_synthetic_trial.py tests/core/test_checks.py -q
```

正式开通仍执行[生产运行手册](production-runbook.md)与[发布门禁](../verification/release-gate.md)。
发布部署流程使用 `python tools/verify_release_gate.py --require-pass`；门禁未通过时退出码
为 2，阻止生产放行。仅检查登记内容是否有效时沿用不带该参数的命令。
