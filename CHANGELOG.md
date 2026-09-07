# Changelog

本文件由 Release Please 根据合并到 `main` 的 Conventional Commits 自动维护，
版本号遵循三段式 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## Unreleased

<!-- Release Please 会把自动生成的版本记录插入到本标题下方，请勿手工填写。 -->

## [1.5.0](https://github.com/skuyd/emr/compare/v1.4.1...v1.5.0) (2026-09-07)


### 新增

* **patients:** 支持多患者与家庭访问权限 ([022ef4a](https://github.com/skuyd/emr/commit/022ef4a218f0aba9799de309ffdc1da0a4d060f2))


### 修复

* **labs:** 修正双栏表头与项目代码关联 ([be5124e](https://github.com/skuyd/emr/commit/be5124e2542cfb0fc7f97a542af11988fa63e69d))

## [1.4.1](https://github.com/skuyd/emr/compare/v1.4.0...v1.4.1) (2026-09-07)


### 修复

* **facts:** 修复医嘱表提取与事实边界 ([86d7fb3](https://github.com/skuyd/emr/commit/86d7fb3eabf79cd912611a0e1be263e319e235b1))

## [1.4.0](https://github.com/skuyd/emr/compare/v1.3.1...v1.4.0) (2026-09-07)


### 新增

* **processing:** 新增可回溯的单据图像增强 ([70e3ed1](https://github.com/skuyd/emr/commit/70e3ed1ab04fb06c024e5b3668a7178c88e98709))

## [1.3.1](https://github.com/skuyd/emr/compare/v1.3.0...v1.3.1) (2026-09-07)


### 修复

* **records:** 优化档案详情与指标核对交互 ([bc7a8da](https://github.com/skuyd/emr/commit/bc7a8da39d49a2959736bed6aadd89320f11cce4))

## [1.3.0](https://github.com/skuyd/emr/compare/v1.2.1...v1.3.0) (2026-09-07)


### 新增

* **trial:** 新增隔离体验模式并修复本地上传稳定性 ([2c3972d](https://github.com/skuyd/emr/commit/2c3972d97f28ba82500b5f5d6539281407787ac9))

## [1.2.1](https://github.com/skuyd/emr/compare/v1.2.0...v1.2.1) (2026-09-06)


### 修复

* **ui:** 修复顶部导航菜单换行 ([753973b](https://github.com/skuyd/emr/commit/753973baa40fdff18ce5291bd6b8828a1eddb235))

## [1.2.0](https://github.com/skuyd/emr/compare/v1.1.1...v1.2.0) (2026-09-06)


### 新增

* **records:** 新增事实核对、就诊速查、资料导出与回收站 ([0bdcd8f](https://github.com/skuyd/emr/commit/0bdcd8f6b58b65e4f41dd247b86b259f30bc15ce))

## [1.1.1](https://github.com/skuyd/emr/compare/v1.1.0...v1.1.1) (2026-09-06)


### 修复

* **labs:** 限制非检验内容进入检验结果 ([5620c54](https://github.com/skuyd/emr/commit/5620c54d21b8f26a59755c7aee6209330a18431e))

## [1.1.0](https://github.com/skuyd/emr/compare/v1.0.1...v1.1.0) (2026-09-06)


### 新增

* **labs:** 实现第二阶段检验解析与可信复核 ([ae97779](https://github.com/skuyd/emr/commit/ae9777957def1707df9d2951777224a1adde4f3d))

## [1.0.1](https://github.com/skuyd/emr/compare/v1.0.0...v1.0.1) (2026-09-05)


### 修复

* **documents:** 优化原件整页预览与检查名称展示 ([82e5904](https://github.com/skuyd/emr/commit/82e5904f4b0cf427102d2862a6ee54493f283bf3))

## [1.0.0](https://github.com/skuyd/emr/compare/v0.3.1...v1.0.0) (2026-09-05)


### ⚠ BREAKING CHANGES

* **core:** 升级须先停止新请求、排空旧 celery 队列，再启动 ocr/control 两类消费者与 Beat；执行 accounts.0008_smsdeliveryjob 迁移和历史会话登记。短信网关须支持签名正文 delivery_id 及 Idempotency-Key 去重，S3 凭据须允许版本列举和版本删除。历史未标记质量版本的报告需在详情重新整理后才能恢复趋势资格。生产部署继续受 release-gate.md 门禁约束，本 PR 不自动放行生产。

### 修复

* **core:** 修复资料处理与认证链路的安全和并发缺陷 ([908cf54](https://github.com/skuyd/emr/commit/908cf5430cb5356215d00a435e348250d33e870f))

## [0.3.1](https://github.com/skuyd/emr/compare/v0.3.0...v0.3.1) (2026-09-04)


### 修复

* **governance:** 忽略仓库排除的生成文档 ([5e8a8d6](https://github.com/skuyd/emr/commit/5e8a8d64ca9088849c3340dec85a5fa363c9dc47))
* **governance:** 收紧文档校验边界 ([d483628](https://github.com/skuyd/emr/commit/d4836287953a165a33cfca6e43c0ad49aacc3e1e))
* **governance:** 隔离本地运行时制品 ([8de41f8](https://github.com/skuyd/emr/commit/8de41f811fbbda1e111998b4f28dcfa21b7ecc79))

## [0.3.0](https://github.com/skuyd/emr/compare/v0.2.1...v0.3.0) (2026-09-03)


### 新增

* **processing:** 新增本地处理工作进程 ([aa39f3d](https://github.com/skuyd/emr/commit/aa39f3df049d5b4989d847035874f8caf5906651))

## [0.2.1](https://github.com/skuyd/emr/compare/v0.2.0...v0.2.1) (2026-09-03)


### 修复

* **dev:** 补全本地环境密钥初始化 ([d01d69b](https://github.com/skuyd/emr/commit/d01d69beb62e509006efe3d43ec752fb2888481a))

## [0.2.0](https://github.com/skuyd/emr/compare/v0.1.0...v0.2.0) (2026-09-03)


### 新增

* **release:** 增加自动版本与更新日志 ([f820645](https://github.com/skuyd/emr/commit/f820645cc5f3772a67358d0e8f6c47b4a0407e89))

## [0.1.0] - 2026-09-03

### Added

- 建立患者登录、建档、隐私同意和个人设置流程。
- 支持图片与 PDF 批量上传、后台 OCR 处理、病案检索、原件查看和实验室指标趋势。
- 增加通知、资料删除、账号删除、审计、健康检查和运行指标能力。
- 增加本地开发环境、生产镜像、部署运行手册及机器可验证的上线门禁。
- 建立 Python、JavaScript、浏览器、可访问性、性能和备份恢复验证入口。
- 统一产品版本号，并增加自动生成 Changelog、计算版本和发布 GitHub Release 的主分支流程。

### Security

- 增加租户隔离、私有对象存储、安全响应头、敏感信息日志约束和删除账本保护。

> `0.1.0` 是首个统一版本号的开发基线；生产使用仍须通过
> `docs/verification/release-gate.md` 定义的全部上线门禁。
