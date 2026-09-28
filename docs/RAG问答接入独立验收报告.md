# RAG 问答接入独立验收报告

日期：2026-09-28。验收依据：`docs/RAG问答接入工程任务书.md`；Git 基线 `d0b4843`，检查对象为 DeepSeek 交付后的工作区及本轮修复。当前 Prompt 为 `rag-qa-v3`。

## 1. 结论与范围

**离线工程检查通过；真实在线语义验收暂未完全通过；网页端到端与已测故障场景通过。** 最新 v3 固定案例为 12/13，仍有一例补充说明错引，按任务书要求不能判定整体验收通过。真实文档问答和网页故障恢复已验证；连接失败、输出截断或不合规引述会受控报错，不自动重发，但引用语义错误仍可能通过机械校验并显示，不能宣称长期正确或“生产级全部完成”。

| 层级 | 结果 | 证据 |
| --- | --- | --- |
| 初次交付测试复核 | 256 passed；新增 11 个针对性反例最初全部失败 | `data/rag-review-20260928/initial-regressions.txt` |
| 修复后完整 Python 回归 | **274 passed，2 条依赖弃用警告** | `data/rag-review-20260928/pytest-final.txt` |
| 前端离线渲染安全 | **12/12**，实际执行渲染函数 | `node tests/web/render_safety.mjs` |
| 离线语义固件 | 12 个正确样例通过，1 个故意错引样例被判失败，符合负例设计 | `data/rag-review-20260928/offline/evidence/` |
| 原报告在线记录重判 | **12/13**；原报告的 13/13 不成立 | `data/rag-review-20260928/previous-online-rejudged.json` |
| Prompt v2 固定案例在线复测 | **13/13**；13 次 embedding + 13 次生成 | `semantic/evidence/rag-eval-online-20260928-070404.json` |
| Prompt v3 固定案例 | **12/13，S05 补充说明错引；严格验收不通过** | `semantic/evidence/rag-eval-online-20260928-114714.json` |
| 浏览器故障/竞态 | 生成失败恢复、禁重复提交、解析终态轮询不吞答案、手动刷新保留答案、HTML 作为文本显示均通过 | `ui-error-dom.txt`、`ui-poll-during-answer-dom.txt`、`ui-manual-refresh-dom.txt`、`ui-answer.png` |
| 真实扫描件网页问答 | v3 回答 21 个部分，引用第 2 页，与原件逐项核对相符 | `real/v3-scan-answer-dom.txt`、`real/scan-answer.png` |

表中省略前缀的本轮证据均位于 `data/rag-review-20260928/`（Git 忽略目录）。未修改正式业务库；测试复用已解析样本与索引的独立 SQLite 备份，未运行 GPU 解析、未下载模型、未重建收费索引。没有执行 Git commit/push。

## 2. 已修复的工程问题

| 问题及影响 | 修复 | 新增回归（`tests/test_rag_review_regressions.py`） |
| --- | --- | --- |
| 先截取 context_k，再做预算；前几块超长时漏掉后面的可用短块 | 遍历完整候选，按块预算装入，直到选满 | `test_budget_skips_large_first_hit_and_uses_later_candidate` |
| 仅按正文去重，丢失不同章节/来源的同文条款 | 排序后按正文与来源身份联合去重 | `test_identical_text_in_different_sections_keeps_both_sources` |
| 依据不足状态允许非空引述并静默丢弃 | 四个列表严格为空，否则 502 | `test_insufficient_must_reject_nonempty_evidence_quotes` |
| 引述 strip/换行归一化过宽，容许模型添加的空格被删后“匹配” | 仅 CRLF→LF，不删边界或内部空格，不将孤立 CR 改写 | `test_quote_must_not_strip_fabricated_boundary_spaces` |
| 块级告警把 block_id 与 chunk_id 比较，公式告警漏传 | 检索保留 block_id，按块、来源页、节点或工作表关联 | `test_block_warning_uses_block_id_not_chunk_id` |
| 规则、问题本身已超预算仍返回“依据不足” | 计入完整 JSON 包装，超限在 embedding 前返回 422 | `test_prompt_overhead_over_budget_is_422` |
| A 索引回答时使用 B 预览版质量告警 | 固定索引绑定解析版本并读取 A 告警 | `test_old_index_uses_old_version_quality_warnings` |
| A 索引版本 invalid、B 预览正常时可能放行 | 验证真正入选版本的有效性 | `test_invalid_indexed_version_rejected_when_preview_is_newer` |
| 文档范围校验依赖真实检索并不返回的可选 document_id | 服务层明确传入权威文档 ID，跨文档证据拒绝 | `test_scope_cannot_trust_optional_search_document_id` |
| RAG 最低分误改变原 `/search` 接口 | 阈值仅在 RAG 证据构建应用 | `test_rag_threshold_does_not_change_original_search_api` |
| 评估器发现事实在别的片段就容忍本句错引 | 结论和说明的错引均计失败，保留负例 | `test_semantic_judge_rejects_wrong_reference_even_if_fact_exists_elsewhere` |
| 解析完成轮询调用 select，使进行中的问题响应作废 | 同文档忙碌刷新不重置请求序号；普通列表刷新保留已有答案 | 真实浏览器延迟生成 + 解析任务终态注入 |
| 状态接口硬编码 v1，与实际 Prompt 不一致 | 统一读取 PROMPT_VERSION | `test_rag_status_reports_configuration_without_calling` |
| 模型事实中的换行拆成无引用独立段落 | 渲染时保持一条事实与其引用在同一条目 | `test_fact_newlines_cannot_detach_the_citation_when_rendered` |

修改包含中文说明；保留原上传、解析、版本、索引和检索功能，摘要及信息提取继续保持未接入。

## 3. 验收工具修正

1. 在线脚本必须显式 `--allow-online`，所有 embedding 批次、建索引调用与生成调用共享请求预算；在每次外部请求前扣额度，失败也计数。
2. 在线目录限制为项目 data 下的独立子目录；禁止覆盖正式数据目录。`--refresh` 不再递归删除已有目录，需要重新开始时使用新目录。
3. SQLite 使用 backup 复制一致快照，不直接复制活跃数据库及 WAL 文件。
4. 六类真实样本中的“条件澄清”改用确实缺条件的问题，并校验状态；失败不能仍以退出码 0 宣称通过。
5. 未调用/网络失败不计为“校验器接受”。记录安全错误码，不输出上游异常全文。在线评估保存完整入选正文与原始模型正文，避免只保留前 200 字而无法复核；不保存 reasoning_content、密钥或请求头。
6. 密钥检查使用 `git ls-files -z`，修复中文文件名被 Git 转义后漏扫；只报告文件与键名，绝不输出真实密钥值。

以上保护有离线回归；评估原文与模型输出仍只允许存本地忽略目录。

## 4. Prompt 修正、失败与真实调用

### v1 → v2：逐事实错引

原报告最后保存的 13 例中，S03 澄清说明提到七日期限，但该句只引用“从签收日起算”片段，没有引用规定七日的条款。原评估器将其当作提示，仍计通过。重判后为 12/13，未删除原记录。

v2 增加结论、解释及澄清原因逐条引用要求，明确不能因为事实在其他片段出现就认为本句引用正确。完整固定案例复测 13/13。词面规则仍只是回归工具，不能完整证明所有非数字论断的语义支持；本轮另行阅读实际答案、来源及原件。

### v2 → v3：扫描 OCR 引述

真实六类测试第一批：DOCX 澄清、无依据、用户注入 3 类成功；扫描 PDF 因模型去掉“名录 库”等原文空格触发 quote_not_found；XLSX 一次输出未完整返回；例外问题一次连接失败。因此第一批是 **3/6**，不能将安全失败算成功。

v3 明确逐字保留 OCR 空格、换行、标点和错字，选择足够支撑结论的短连续原文；简单数量问题不为凑字数枚举全部项目。保留严格子串校验，未用清洗模型引述的方式放宽验收。

定向复测每题一次，共 3 次 embedding + 3 次生成：

| 类别 | 实际结果 | 核对 |
| --- | --- | --- |
| 扫描 PDF | answered，21 个部分；约 4.1 秒 | 引述“全书内容分为21个部分”；实际 PDF 第 2 页“编者说明”确有该句 |
| XLSX | answered，说明毛利公式未保存计算结果，不能当作 0 | 保留 `formula_cache_missing` 告警，未输出猜测的毛利数值 |
| DOCX 例外 | answered，定制品、生鲜、已拆封软件；另列第三章适用范围 | 引用实际条款，不将所有商品说成均可退 |

本轮真实调用均使用现有配置的 `deepseek-flash`、`qwen3.7-text-embedding`，未改变用户模型、密钥、thinking 或费用设置。生成/embedding 失败不自动重试；上述复测是修正后的显式验收批次。不同版本的通过记录不合并宣称一次 6/6。

### v3 完整回归：保留未通过项

S05 问采购审批流程与金额门槛，结论正确引用 [1] 的“超过十万元”；补充说明再次说“金额门槛为十万元”，却只引用 [2] 的流程说明。完整入选证据证明 [2] 不含金额。该输出通过 JSON/引述校验，但语义评估明确失败，不是误报。

最新完整一轮：状态 13/13、固件关键事实 13/13、引述可追溯 13/13、期望证据召回 13/13、逐事实引用支持 **12/13**。保留原始生成正文、完整入选块、最终响应和判定原因。新增 `test_repeated_amount_in_explanation_still_needs_its_own_correct_reference`，防止评估器再次把这类问题放行。

当前 Prompt 已要求逐条核对，包括补充说明，但模型仍有随机性。本轮没有自动给错误句补引用、放宽标准、删题或反复调用直到成功。后续应围绕重复事实与复合句做专门 Prompt/答案结构实验，保留负例，再完整重跑固定集后申请通过。

独立验收的真实调用总计 **35 次 embedding + 35 次生成尝试，共 70 次**：v2 固定集 26、真实六类首批 12、v3 定向三题 6、v3 固定集 26。包含连接失败与截断尝试；不含此前 DeepSeek 的历史调用，未产生索引构建请求。模拟浏览器请求不计入该真实调用数。

## 5. 网页与原件证据

隔离模拟服务监听 8014，真实服务监听 8015。外部 SDK HTTP 边界模拟用于浏览器故障检查，真实服务用于扫描件端到端问答；两类证据明确区分。

- 模拟上游 401 含测试假密钥/路径：页面仅显示安全错误，控件恢复，日志记录仅一次生成请求，没有自动重发。
- 模拟延迟回答期间，重新解析任务结束并触发轮询刷新：最终答案保留。手动刷新文档列表后答案也保留；整页重载清空答案仍是单轮产品约定。
- 恶意文件名和模型文本 `<img ...>` 显示为普通文字；DOM 没有新增 img/script，标题未改变。
- 点击逐条 `[1]` 可定位引用卡；查看引用版本后答案保留。扫描件来源版本 `v-2eb3b5eca6ba-bce03a647b82b6bc`，索引 `idx-21f94e3d006b4e41941b90e4b017aae3`，chunk `eb1bff9d45e8464fa3732f1600a5204e`。
- 原件第 2 页经 pdftoppm 渲染并目视核对，数字 21 与页码正确；本次未声称逐字校对整份扫描件。解析服务当前不可达，但已有持久化解析与索引可正常问答，符合独立服务边界。

## 6. 与 R01～R26 的关联

| 条目 | 独立验证落点 |
| --- | --- |
| R01～R03 | 全量 pytest；test_rag_api 的真实 SDK + MockTransport 路由集成；真实网页扫描件 |
| R04～R06 | test_rag_context 范围/阈值/预算；新增预算、异源同文、权威文档范围反例 |
| R07～R12 | test_rag_validation 严格协议/引述；API 的 200 三状态与 502；新增空格和依据不足引述反例 |
| R13～R14 | 固定语义 S06/S07/S11/S12 及真实注入请求；不声称完美防注入 |
| R15～R16 | Office/PDF 来源测试；新块级告警、旧版本告警与 invalid 索引反例；真实 XLSX 缺缓存 |
| R17～R20 | B 解析/索引失败、B 发布、生成期间切换的真实 repository 测试；A 证据保持不变 |
| R21～R22 | 上游截断/超时/凭证路径脱敏用例；真实上游失败页面；密钥扫描 |
| R23～R24 | 离线 12 项渲染检查 + 真实浏览器故障恢复、轮询竞态、引用入口、HTML 文本化证据 |
| R25～R26 | 启动/状态/刷新无收费调用测试；原检索兼容性与配置非法值测试；在线请求计数 |

## 7. 已知限制

- 正式后端校验只证明结构合法、编号范围正确、引述可追溯；无法证明模型每个结论必然正确。13 个合成案例不能代表复杂真实业务分布。
- 最新 S05 存在真实语义错引，属于任务书核心案例失败，是本轮整体验收未通过的直接原因；不是仅供参考的优化建议。
- 注入样本较小；部分合成资料本身包含“无相关记录”等答案线索，难度低于开放业务库。下一阶段应补独立人工标注难例，而不是扩大“准确率”宣传。
- 真实服务出现过连接失败与输出不完整；本轮保留失败，不添加隐式重试或自动额外计费。
- R23 已验证最关键的解析终态与问答并发，未穷举所有浏览器网络乱序及多窗口竞态。单文档单轮范围外的多轮记忆、跨文档、流式和公网部署不在本次验收。
- 两条 pytest 警告来自 Starlette/httpx 与 anyio 的弃用提示，本次不进行无关依赖升级。

## 8. 本地复现

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node tests/web/render_safety.mjs
.\.venv\Scripts\python.exe scripts/evaluate_rag.py --all-cases --data-dir data/rag-eval-offline
.\.venv\Scripts\python.exe scripts/check_secrets.py
git diff --check
```

在线评估须显式允许并设置请求上限；首次建立样本索引也计入上限。先核实独立数据目录，不能以正式库运行。

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_rag.py --allow-online --all-cases --max-requests 40 --data-dir data/rag-eval-new
```

有既存样本索引时可使用 `--reuse-index`；已有真实 DOCX/XLSX/扫描样本的六类脚本使用 `--allow-online --skip-parse --max-requests 12`，如果需要建索引则要额外预留明确额度。原始答案、日志、截图只存 data 下，不加入 Git。
