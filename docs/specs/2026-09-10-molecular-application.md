# 分子检测应用接入合同

本规格细化[病理与分子设计](2026-09-08-pathology-molecular-evidence.md)的分子切片和
[A0 值合同](2026-09-10-molecular-value-contracts.md)，不取代其来源与质量限制。
PR 70 已实际 Squash 为 `4b73d2e9c925ef48c95c217c7c055c5b7157e13b`；本功能独立分支
`feat/batch-three-molecular-application` 从重新 fetch 后该 `origin/main` 创建。
当前为 `active / implementing`，完整应用尚未交付，发布版本未知。

## Global Constraints

- 实现数据库、实际上传解析、来源核对与修订、搜索、选定输出和分享的完整链路。
- 保持 `MOLECULAR_VALUE_CONTRACT_V1` 的 28 个键及值形状；不把 A0 校验通过当成来源真实。
- 旧 `1.0/1.1` 字段、`PATHOLOGY_IHC_V1`、已确认原值及完成的提取记录不部署重写。
- 保留旧 IHC 上下文策略及其令牌输入；新字段不因注册而使旧确认失效。
- 仅用合成资料验证应用；原 gold、预测、环境封存、独审和失败证据保持原样。
  新真实资料评估继续为 NOT_RUN，必须另有完整身份和相应放行，不由本功能启动。
- 不推导诊断、阳性、药物建议或已采用的治疗事件，不连接外部药物服务。
- 新工作不消费未合云影像、病灶或其他功能分支。应用版本与 Changelog 由发布自动化管理。

## 1. 字段注册、日期和路由（C1）

新增应用字段模式 `MOLECULAR_REPORT_V1`、报告路由及类别 `MOLECULAR`。
新字段按应用模式先分派给 A0 `validate_value`，不能让同名 TEXT/CODED 类型误走旧校验。
25 个非日期 A0 键注册为新模式；三个日期复用既存 `assay.collection_date`、
`assay.received_date`、`assay.report_date`，不覆盖其定义、类别或 `PATHOLOGY_IHC_V1` 身份。

日期适配先校验 A0 `{value, precision, raw}`，持久化值仍为 `{value, precision}`，
`raw` 无损进入 FIELD 的 `raw_value`。通用 `date_raw` 是另一外层槽，不能用于替代该原文。
编辑器反向从有效值和有效 `raw_value` 重建 A0 输入；显式提供冲突原文时拒绝。
年月日及 UNKNOWN 各自保留，不补全、不互相填充三种日期角色。

分子报告仅允许以下既存共享键，不放宽全部病理字段的报告范围：
`specimen.identity/description/site/procedure`、`assay.identity/method/antibody`、上述三个日期，
以及 `ihc.marker/result/score`。它们保留原模式与类别；分子字段只能属于分子报告。
界面及选定章节使用共同的“报告路由＋允许字段”分类器，让共享日期出现在对应报告组。
原病理字段在病理报告中的展示与过滤规则不变。

分子报告中的 PD-L1 继续采用真实 IHC 标本、检测和 marker 链；不能把 NGS panel 当成
IHC 检测，也不能把 CD274 拷贝数映射为蛋白表达或评分。TPS、CPS、IC 的旧合同不变。
迁移只增加必要选择和新锚约束，不批量重写任何已有数据。

## 2. 完整身份与检测范围（C2）

28 个 A0 键全部接入；旧设计的 `variant.kind` 和 `variant.fusion` 分别由
`variant.identity.kind` 和 FUSION 身份的有序伙伴表达，不建立另一套重复字段。
重复转录本、密码子或位置可有独立原文组件字段；身份锚保留 A0 的有序完整组件列表。
合并概览与明细必须同时证明同报告、标本、检测、完整变异身份及真实来源关联。
同基因、相似文字或缺失组件都不足以去重；冲突及不完整候选并列保留。
COMPLETE 仅表示结构声明，不授予确认、可用或输出资格。

独立新增应用字段 `assay.negative_statement`，不加入 A0 28 键。严格值形状为：

```json
{
  "text": "原检测范围内的完整限定陈述",
  "assertion": "NOT_DETECTED",
  "scope": {
    "state": "EXPLICIT",
    "raw": "原检测种类及范围限定",
    "detection_kinds": [{"code": "COPY_NUMBER", "raw": "原文拷贝数检测名称"}],
    "targets": ["原文明确目标"],
    "limitations": []
  }
}
```

assertion 仅为 NEGATIVE、NOT_DETECTED、UNCERTAIN、NOT_TESTED、NOT_PROVIDED；
检测种类为 SMALL_VARIANT、COPY_NUMBER、FUSION、MSI、TMB、OTHER。
三个列表各最多 32 项，保留原顺序，不扩展基因或检测范围；重复种类不合并原文。
UNKNOWN 范围须 `raw=null` 且列表为空，不保存猜测目标。值原文非空，最多 30000 字符；
各范围原文最多 4096 字符。明确的否定/未检出只有范围与父关联有效才可作为当前结果。
一条原句对不同种类作不同断言时，分开保存相应原文窗口，不跨类型传播阴性。

没有结果行不生成 NOT_PROVIDED 候选；该状态只来自明确原文或具有原页范围证明的人工
缺失声明。“未检测”不能从“未检出”推得。NOT_PRINTED 需要原件范围证明，OCR 没读到
只能保留 UNKNOWN。

新模式内容另存 `reported_assertion={code, raw, proof_fragment_ordinals}`，只表达报告
明示的 POSITIVE、DETECTED、NEGATIVE、NOT_DETECTED、UNCERTAIN、NOT_TESTED、NOT_PROVIDED。
未声明时 code 为 AS_REPORTED_NO_POSITIVITY_INFERRED，raw 为 null，片段列表为空。
明示时须非空原文和本字段实际片段证明。它不改变 A0 quantity.assertion，更不能由数值
自动设阳性。MSI-L/MSS、TMB 数值/定性、数量/阈值、单位缺失/未知保持独立。

## 3. 分子上下文和生命周期（C3）

新分子字段采用 `MOLECULAR_CONTEXT_V1`。旧 IHC_CONTEXT_V1 精确形状及成员集不改变。
新上下文固定含 `context_version/report_id/membership_policy/bindings/association`。
前三项版本、报告 UUID 与所属报告必须一致；每个 binding 仍只包含
`role/state/target_fact_id/target_entity_key/proof_fragment_ordinals/reason`。
BOUND 和 UNKNOWN 的原有形状与来源证明要求继续适用。

| 字段组 | rank | 必需角色 |
| --- | --- | --- |
| 既存标本锚／属性 | 10／11 | 原有规则 |
| 既存检测锚／元数据与三日期 | 20／21 | 原有 SPECIMEN、ASSAY 规则 |
| 分子检测名称、panel、方法、规模 | 21 | SPECIMEN、ASSAY |
| variant.identity | 30 | SPECIMEN、ASSAY |
| 变异各原文组件、tier | 31 | SPECIMEN、ASSAY、VARIANT |
| 变异丰度、拷贝数 | 40 | SPECIMEN、ASSAY、VARIANT |
| MSI、TMB、检测范围陈述 | 40 | SPECIMEN、ASSAY |
| drug_evidence.drugs | 50 | SPECIMEN、ASSAY、VARIANT |
| 药物 statement、direction、context | 51 | SPECIMEN、ASSAY、VARIANT、DRUG_EVIDENCE |
| 药物 level | 60 | SPECIMEN、ASSAY、VARIANT、DRUG_EVIDENCE |

VARIANT 指向同实体 `variant.identity`，DRUG_EVIDENCE 指向同实体 `drug_evidence.drugs`。
条件唯一锚增加这两种字段，排除仍占用原锚键。所有引用同患者、文档、报告、解析版本，
只向低 rank，父标本和检测必须一致。完整图仍最多 32 个依赖节点、深度 8；超限明确拒绝。

普通分子字段每个必需角色恰好一次，association 为 null。药物字段的 VARIANT 允许
1–8 个按原文次序排列的不同 BOUND 目标，或一个显式 UNKNOWN；不能混合或按基因合并。
其余角色恰好一次。药物 association 固定为 `{state, raw, proof_fragment_ordinals}`：
state 为 EXPLICIT 时原文非空、片段非空，并能证明整个目标集合；UNKNOWN 时 raw 为 null、
片段为空且 VARIANT 也是 UNKNOWN。单目标也要证明原关联。关联集合只表示原文引用，
不能推断 AND/OR 获益或拆成独立治疗结论。相互矛盾、跨检测或范围不完整时保持未关联。
resolver 使用版本化多值角色表示，不能用 `{role: target}` 覆盖多个 VARIANT。

新策略的标本/检测成员包括 description/site/procedure，以及 name/panel_name/panel_size/
molecular_method 和三日期。变异数值及药物闭包包括对应变异全部低 rank 组件/tier；
level 还包括本药物实体的 statement/direction/context。新增、冲突、排除、同值 UNDO、
实际作者和全部修订头均进入当前令牌。不能把这些成员加入旧 IHC 的全局 MEMBER_KEYS；
锚不反向依赖高 rank 子项，本字段自己的确认头单独进入输出指纹，避免自我失效。

自动 BOUND proof 覆盖目标自身完整值窗口，不能借相同词、标签或祖先片段；保留实际
OCR block、Unicode 半开区间、原 polygon。人工原页转录不能生成 OCR 坐标。
新来源角色区分 CURRENT_RESULT、PRIMARY_ASSAY_METADATA、REPORT_DRUG_EVIDENCE，
以及历史、送检诊断、文献解释、对照、QC、UNKNOWN。后几类可见但不是本次可用结果。
未关联可以核对本字段原文，仍为 usable=false，不能进入默认速查、导出或分享。

改身份组件、父链、药物组合或所指变异集合，创建新实体并原子排除完整依赖集合；
旧原值、片段和关系不重定向。整体 UNDO 核对所有新旧头后恢复旧项为 PENDING。
单字段 UNDO 不能拆开整体操作，普通更正不能造成组件与身份锚矛盾。
继续遵守 Patient → UUID 有序批次 → Document → 领域对象锁序和真实操作者权限。

## 4. 核对与选定语义（C4）

沿既有报告/字段路由完成自动和人工报告、原件核对、确认、更正、暂缓、排除、撤销及
整组替换；按标本、panel、变异和药物依据分组，长表达不裁掉尾部，未知状态不能只显示空白。
实际上传 worker 同时支持表格与叙述；多报告、多标本、跨页/拆列与缺表头必须有边界证明，
保留失败、零候选与来源未判断的区别，不根据阅读顺序猜测表头或患者结果。

选定输出新增 `MOLECULAR_SEMANTIC_UNIT_V1` 和独立的
`selection.molecular_semantic_unit_policy`，不覆盖旧 IHC 的选择策略。
表单在确认前说明最小必要语义；私有验证闭包不等于允许携带全部依赖字段。

| 选择项 | 必须保留的最小语义 |
| --- | --- |
| 检测元数据／日期 | 选定值、类型、原精度/单位状态、选择内标本和检测别名 |
| 变异身份或变异值 | 选定值及用于区分该变异的完整原身份组件、原变异类型、体细胞/胚系/未知范围；不带其他丰度、分级或药物条目 |
| MSI／TMB | 选定类别或数值、种类、原单位/比较符/约数、独立原断言；不推算另一项 |
| 检测范围陈述 | 原断言及完整检测种类/目标/限制和标本/检测别名 |
| 报告药物依据 | 报告记载标识、原药名/组合、原关联及变异身份、该项证据含义、方向/等级体系/报告日期状态；未知或未说明明确显示，不补值或跨体系排序 |
| PD-L1 | 既存 IHC 评分语义单元，不另造分子评分 |

变异和药物的必需身份束在选择控件和预览中明确列出。只有该声明范围可借用已核对身份；
未选标本名称、检测条件、其他字段统一标为 NOT_INCLUDED_NOT_COMPARABLE，不透露是否存在。
身份/检测关联未知则拒绝该结果单元；药物方向未说明、等级体系或报告日期未知按原状态
显示，不能为了让结果可用而猜测。药物只标注报告记载，不生成治疗建议。

细选采用每次选择内随机命名空间的标本、检测、变异和药物别名，不携带未选目标 Fact ID、
原 entity_key、私有上下文、依赖/作者头、原整句或角色偏移。保留真实来源页/框，不伪造裁框。
复合值 raw 可能包含邻列，公开形状移除该整句并保留必要组件；离线校验可用脱离数据的
占位原文校验严格结构，占位绝不能进入原值、返回文件或数据库。新 public 模式不得回退
为旧裸数值。文本仅带明确选中的有界原陈述，云访问串继续默认省略，不制造新 URL 例外。

细选报告标题和原件文件名中性化；报告全选、临床字段选择和完整原件字节各自授权。
PDF、JSON、CSV、ZIP 和分享都消费同一语义投影，保留 PDF/HTML 转义与 CSV 公式防御。
快照绑定实际字段、报告、来源、闭包、作者和修订，在预览/渲染、构建、存储 IO、首块/
每块读取及分享入口持续核验；更正、排除、重解析、删除恢复、撤权、会话/期限不能复活旧结果。

## 5. 格式兼容和验收

实际起点 `4b73d2e` 的 `apps/exports/content.py` portable 为 `1.4`，reader 接受 `1.0–1.4`；
`PATHOLOGY_IHC_V1` 是字段模式，应用 `1.15.0` 是发布版本，两者都不是 portable `1.5`。
本分支新增分子内容暂使用该基线下一 portable `1.5`，沿临床三数组扩展；交付前按届时
实际 main 重新合流和分配下一合法值，保留所有已发布数组和 reader，不导入未合分支。

验收必须覆盖真实 ORM/迁移、合成上传到持久化、全部核对行为、实际各格式内容读回、
分享越权反例、正常 COMMIT 的 PostgreSQL 竞争以及桌面/360px 浏览器。具体矩阵和
步骤见[实施计划](../plans/2026-09-10-molecular-application.md)。源码功能完成不建立真实
识别质量，金标准和真实评估另有门禁；不把历史证据或文件存在视为本次通过。
