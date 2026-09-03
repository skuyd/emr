# Changelog

本文件由 Release Please 根据合并到 `main` 的 Conventional Commits 自动维护，
版本号遵循三段式 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## Unreleased

<!-- Release Please 会把自动生成的版本记录插入到本标题下方，请勿手工填写。 -->

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
