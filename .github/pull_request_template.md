## 变更说明

<!-- 简要说明用户可感知的结果及验证方式。 -->

## 关联文档

- 需求或决策：<!-- 填写 docs/ 路径；确实不适用时写 N/A 并说明原因。 -->
- 设计规格：<!-- 填写 docs/specs/ 路径，或 N/A。 -->
- 实施计划：<!-- 填写 docs/plans/ 路径，或 N/A。 -->
- 验证证据：<!-- 填写测试命令、测试文件或 docs/verification/ 路径。 -->
- Changelog 影响：<!-- feat/fix/perf 由 Release Please 生成；无用户可感知变化时说明。 -->

## 检查清单

- [ ] PR 标题符合 `<type>(<scope>)!: 中文描述`
- [ ] 已运行与改动最接近的自动化测试
- [ ] 新增、移动或修改文档时，已同步登记表、文档中心、版本清单和全部引用
- [ ] 已运行 `python tools/verify_documentation.py`
- [ ] 未手工修改自动版本字段或 Changelog 自动生成区域
- [ ] 如有不兼容变更，标题包含 `!`，正文包含迁移说明

<!--
版本影响：
- fix / perf -> PATCH
- feat -> MINOR
- ! / BREAKING CHANGE: -> MAJOR
- docs / test / chore / ci / refactor -> 不发布
-->
