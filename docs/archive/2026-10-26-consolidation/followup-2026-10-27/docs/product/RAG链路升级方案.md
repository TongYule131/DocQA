# RAG链路升级方案

> 定位：本文件保留原设计／分阶段方案，其旧“当前”、预算和完成状态属于原日期。实际交付与下一步统一见[当前任务书](../下一阶段任务书-S2-S5闭环补齐与试点验收.md)，发布／余额见[接力说明](../稳定试用版接力说明.md)。不再向本设计文件追加当前发布摘要。

2026-10-23。此次升级由用户要求参考 ragent 代码实施，重点是数字与上下文归属、证据处理和失败可诊断性。范围覆盖入库、检索、生成、校验、追踪和回归；保留既有模型、GPU解析部署、SQLite及账号权限，不重写为Java服务。

## 参考代码与取舍

已核对 [nageoffer/ragent](https://github.com/nageoffer/ragent/tree/ff93871ab88fff24a5dac7feee7ea342359a3439)，固定提交 `ff93871ab88fff24a5dac7feee7ea342359a3439`，Apache-2.0。以下仅借鉴架构思路，没有复制Java实现，也不将其视为PDF数字恢复或严格引用校验的现成方案。

| 参考实现 | 本项目采用方式 |
| --- | --- |
| [RetrievalBudget](https://github.com/nageoffer/ragent/blob/ff93871ab88fff24a5dac7feee7ea342359a3439/rag/src/main/java/com/nageoffer/ai/ragent/rag/core/retrieval/RetrievalBudget.java) | 分离召回数、候选数、最终条目和字符预算 |
| [MultiChannelRetrievalEngine](https://github.com/nageoffer/ragent/blob/ff93871ab88fff24a5dac7feee7ea342359a3439/rag/src/main/java/com/nageoffer/ai/ragent/rag/core/retrieval/MultiChannelRetrievalEngine.java) | 召回、融合、原文范围校验、压缩装入分别记时，保存实际输入清单 |
| [FusionPostProcessor](https://github.com/nageoffer/ragent/blob/ff93871ab88fff24a5dac7feee7ea342359a3439/rag/src/main/java/com/nageoffer/ai/ragent/rag/core/retrieval/postprocessor/FusionPostProcessor.java) | 保留本地BM25、向量和结构槽位融合，新增原文数值锚点；不把融合分数当语义置信度 |
| [EvidenceGatePostProcessor](https://github.com/nageoffer/ragent/blob/ff93871ab88fff24a5dac7feee7ea342359a3439/rag/src/main/java/com/nageoffer/ai/ragent/rag/core/retrieval/postprocessor/EvidenceGatePostProcessor.java) | 证据筛选与生成独立；没有远端精排分时不伪造阈值。本轮不引入付费语义重排器 |
| [CitationContextEnricher](https://github.com/nageoffer/ragent/blob/ff93871ab88fff24a5dac7feee7ea342359a3439/rag/src/main/java/com/nageoffer/ai/ragent/rag/core/source/CitationContextEnricher.java) | 引用由服务器关联真实身份；继续保留逐论断、连续原文、版本与坐标校验 |

## 数据与处理流程

上传 → 保留原件 → Docling/OCR → PDF内联碎片核验 → 标题恢复 → 分块 → 建立版本索引 → 发布知识库快照 → 提问及资料授权 → 查询向量 → 关键词/结构/数值锚点融合 → 原文范围校验 → 预算内压缩装入 → 流式生成 → 逐论断及整体校验 → 保存回答与引用。

1. **数字归位**：只有独立数字节点、同页同栏坐标、原PDF文字层三者一致，且正文对照只差这些数字时才补入。原始JSON及数字节点保留，新规范化版本注明重建；不能从用户问题推测数字，不用OCR错字互相补全。无法验证时保留原状并告警。
2. **标题与数据区分**：收稿日期、作者简介、带机构序号的作者行不作为正文小节。新版本的正文、原文来源及索引独立于旧版本；PDF物理页号与期刊印刷页号不混用。
3. **分阶段检索**：沿用关键词、向量、章节、定义及条件槽位；数值仅用于寻找原文，不成为答案依据。最终输入清单保存版本、块ID、原文字串哈希及实际可读范围；去重保留不同页的来源身份。
4. **数字校验**：先核对页/节/命题等地址，再核对字面数值和显式单位；同一数字不能从图号借给金额。单位别名仅限确定等价者，不做隐式换算或推断百分比分母。数值仍须选择自己引用内的真实context，不关闭原有负例校验。
5. **题干与原文边界**：允许本条引用支持的汉字排版空格、非数值连字符及同序显式概念枚举；不做任意语义近似匹配，不把题干缩写当原文逐字引述。结构归纳可用题干中的短分析名词组织讨论，但标为非逐字来源、保留待复核；原文直述和用户事实条件不能套用此规则。用户经历、数字、期限及未引用片段不能为事实补证。
6. **失败可复核**：流式校验失败停止展示新草稿，接收同一HTTP的结束状态与费用信息，不自动重发。保存该次失败正文、原因与论断位置，诊断不充当成功检查点。后台只展示阶段计数、错误码与位置，不因此取得客户正文、提示词或原始响应。
7. **旧资料修复**：`scripts/repair_cached_pdf_layout.py --data-dir <独立目录> --document-id <ID>` 默认只读规划。显式 `--apply` 仅用缓存与原PDF发布新解析版本，OCR/模型调用为零；建索引和知识库发布另行显式执行。新索引可复用同文档、同模型、其他成功版本中完全相同文本的向量，保留新的来源；显式重建绕过该缓存。

## 验证与交付

固定正反例覆盖同栏/跨栏/跨页、年份/百分比/金额、显式单位冲突、概念枚举与用户条件、单次流式失败的定位和计费、诊断权限隔离、旧版本来源及向量复用。使用现有全量回归入口；首次失败保留，不删除负例或更改用户原题凑通过。

真实验收优先复用最新失败问题和已保存查询；只在既有128次累计授权账本内执行，失败不自动重试。离线通过、真实题通过和正式发布分别记录在[阶段04](../阶段04-真实语义验收与提取改进.md)、[阶段05](../阶段05-代码审查与加固.md)及[接力说明](../稳定试用版接力说明.md)。实现或单题通过不代表任意论文、复杂公式和所有语义均已解决。
