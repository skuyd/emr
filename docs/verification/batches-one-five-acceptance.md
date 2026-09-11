# 五批原需求功能交付与证据验收

本记录按[原五批需求](../specs/2026-09-07-batches-one-five-requirements.md)逐项核对16项需求、
63条原文、7项全局契约及第8节；[机器验收](artifacts/batches-one-five-acceptance.json)保留原文、
每条实际来源与审查结论。功能交付及证据事实验收已完成，真实质量目标和生产门禁独立列示。

## 当前交付与验收

完整分子应用已由 [PR #88](https://github.com/skuyd/emr/pull/88) 按准确头 `625850ea9d54dd3309920346374b43d5f547f149`
完成独审及[精确CI](https://github.com/skuyd/emr/actions/runs/34544444229)，Squash为 `18181d9e68dd6adf0cf3438c63ac03fe9405b7f6`，随
[v1.19.0](../releases/v1.19.0.md)发布。63条及7项全局的每条功能结论和第8节均已由非作者据实际证据闭合，详见[最终验收](batches-one-five-acceptance.md)。
真实质量目标另列：病理1/10、癌种原评分、病灶retention=false和云未判定页保留；
治疗周期≥80%仍未建立，其规格/计划仍为implemented。真实M7及癌症第二次真实评估未启动，
生产仍BLOCKED。下文合同仍有效，带源码或时间的旧待交付措辞仅记录原检查点。


## 实际交付与执行

| 执行 | 准确头 / 运行 | 普通通过 / 跳过 / 未选择 | PG / 浏览器 / JS |
| --- | --- | --- | --- |
| 功能PR88 | `625850ea9d54dd3309920346374b43d5f547f149` / [34544444229](https://github.com/skuyd/emr/actions/runs/34544444229) | 4835 / 4 / 394 | 387 / 23 / 9 |
| 发布PR | `659c81a9912b38aa8a098d328a82968e7dd36d80` / [34548419312](https://github.com/skuyd/emr/actions/runs/34548419312) | 4835 / 4 / 394 | 387 / 23 / 9 |
| 发布主线 | `bfc0a010a2f0197667d95431ecb4cbfac6570231` / [34552496969](https://github.com/skuyd/emr/actions/runs/34552496969) | 4835 / 4 / 394 | 387 / 23 / 9 |


| Task10检查 | 实际HEAD | 结果与范围 | 证据SHA256 |
| --- | --- | --- | --- |
| documentation | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：125份登记文档通过；不是未来候选overlay文档检查。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| traceability | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：原PRDv1的62项，60verified/2external_pending；不替代五批63+7验收。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| release_gate_consistency | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：生产门禁一致性检查通过，实际仍BLOCKED，8passed/15pending。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| django_check | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：Django check零问题，config.settings.test。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| migration_drift | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：无模型迁移漂移；不替代已保留的真实PG旧行/长值反向迁移证据。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| release_automation | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：自动发布契约检查通过，源码当时版本1.18.0；不证明后续Release存在。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| version | 625850ea9d54dd3309920346374b43d5f547f149 | 源码CI：当时六处自动版本1.18.0一致；不把portable1.8或候选版本当已发布版本。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| ordinary | 625850ea9d54dd3309920346374b43d5f547f149 | 普通4835PASS/4skip/394deselected，2855.51s；Linux四个Windows专用跳过保留。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| browser | 625850ea9d54dd3309920346374b43d5f547f149 | 8文件必跑浏览器23PASS/0skip，119.41s；原本地TLS截图保留原阶段，不伪称CI截图。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| postgres | 625850ea9d54dd3309920346374b43d5f547f149 | 真实PostgreSQL387PASS/0skip/4869deselected，1381.00s；与普通和浏览器矩阵不相加。 | de48249c0847e196b0f0d1181d057b3677a959f9c5f9036bc0030dbb2c90153a |
| javascript | 625850ea9d54dd3309920346374b43d5f547f149 | JavaScript9PASS/0fail/0skip。 | eee1a881b70a53451c1eca8a74fb8dc776488897fba69a1da6cbdeebdfc5c515 |
| synthetic_parser | 625850ea9d54dd3309920346374b43d5f547f149 | PR88实际制品：固定observations554；whole556文件/564行含extra_context6文件/10行子集；target125与gene50独立。分组重叠不相加，metadata/persistence未assessed，真实准确率未评测。 | 73774e479c440e0c90ab6504147ae4d7ee2724c4a39c72a573f4a0f75126d089 |
| title | 625850ea9d54dd3309920346374b43d5f547f149 | PR88实际中文标题及正文检查通过：feat(molecular): 完成分子报告提取核对与选定输出分享；root独立标题/正文检查另附原件。 | 1f2630cef2e77f281f2339dc6f324ffb9bfe6922eb25d6ec69e5e1176af4ad7a |
| private_history | 625850ea9d54dd3309920346374b43d5f547f149 | 提交前22个新增提交逐父路径审查及root全文/PR内容复核通过；1545路径、1256非文档非自动版本字节保持。路径门禁不是万能秘密扫描。 | b5f6b0c08ae05747c654a2ff01726c2cbaee81b4d7aeb4d2e855d7d9238eb401 |
| windows_only_launcher | 625850ea9d54dd3309920346374b43d5f547f149 | 独立Windows启动器4PASS/0skip，3.53s；临时synthetic manage.py仅记参数与生命周期，无真实web/医疗处理。Linux原4skip不重标、不加到CI分母。 | f95c64f0420711898544c669a83e0aa5dda794d287c4d2903380e96b4448efa8 |

以上为实际源码CI或原精确本地执行；documentation行不是本次候选文档检查。候选校验另由输出文件哈希及源码基线绑定的render-receipt记录，不能改称原main HEAD自身执行。最终文档PR的独审、CI及合并由外部回执记录。


B3-03.2：双标本/检测、样本类型、panel及三日期的提取→ORM→核对→选定JSON专用证明已在b605及独审闭合，随后于ea843 source-tree的22项和d2e source-tree的45项再次执行。B3-03.4：真实HTTP DEFER/UNDO/CONFIRM、选定快照失效/恢复同样闭合；两个source-tree均由HEAD b605加MERGE_HEAD9cc的暂存源码起跑，32c6是之后提交，不改称从最终提交首跑。

Windows625启动器4PASS/0skip仅为合成参数和生命周期证据，与LinuxCI原4skip分列。最终CI浏览器核实真实执行和跳过；原本地原生TLS/手机桌面截图继承原阶段，不伪称CI截图。重叠矩阵不相加。

## 质量与证据限制


检验联合F1为41.81%→57.92%；事实正确60→97、漏提157→31，但精确率78.95%→48.02%、候选核对量76→202。原分母、分字段/类型得分和此前正确项保持证据均保留。实际本地OCR与固定OCR回放分别记录，不推断当前全部真实资料质量。

病理严格正确仍为1/10；病灶真实阳性关联为0，`retention_gate_passed=false` 保留。云二维码TP4/FP3/FN2、108页未判定；外部影像没有被自动取得为原件。癌种原评估TP1/FP0/FN10、完整表达0/11，后续类型化兼容通过不构成新的真实质量结果。

治疗周期无联合正例、12FP及19未判断，80%质量目标未建立，原复测未改善记录不变。个人30%/50%提示仅作描述。日常记录历史0.2005秒/0.2131秒是自动化表单到保存详情的计时，不是人类30秒可用性研究。

分子M7和癌症第二次真实评估均未启动；未标注、未读页面保持未知，不能当作没有事实。原合同不要求额外凑齐200份样本。合成通过不能代替真实质量，更新环境哈希也不能代替测试通过。

旧封存包、环境清单、独审和失败证据保留。环境重新绑定记录中的历史癌症审查未完成状态，应与后续[癌种交付](cancer-ordering.md)分开理解。[SQLite恢复](sqlite-observer-recovery.md)保留原42项失败及后续成功，不宣称42项全部同一根因。


## 最终范围

63条及7项全局的每条功能结论和第8节均已由非作者据实际证据闭合，原审计角色与历史失败保持。Task8规格/计划仍implemented，因为原80%质量目标未建立；这与五批功能交付和证据事实验收分列，不表示质量达标。

原[PRD v1.0矩阵](traceability.md)为62项、60verified/2external_pending，MUST-01是历史单患者阶段；不得替换为新63+7分母。生产门禁仍BLOCKED；本轮不包含真实医疗评估、B6/B7或新外部付费处理。
