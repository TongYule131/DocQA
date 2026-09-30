# 智能文档分析与知识问答系统

依据《智能文档分析与知识问答系统.md》建立，当前已完成 Docling 解析接入阶段。需求分析与实施顺序见 [项目实施方案](docs/项目实施方案.md)，本阶段任务与验收见 [Docling 接入工程任务书](docs/Docling接入工程任务书.md)、[全样本解析质量验收](docs/Docling全样本解析质量验收.md) 与 [Docling 接入实施报告](docs/Docling接入实施报告.md)。

后续业务 Prompt 优化与验收参考 [Prompt 设计与验收参考](docs/Prompt设计与验收参考.md)。

单文档问答的实现边界、接口契约与验收标准见 [RAG 问答接入工程任务书](docs/RAG问答接入工程任务书.md)；当前修正与验证结果以 [RAG 问答接入独立验收报告](docs/RAG问答接入独立验收报告.md) 为准，初次交付记录保留在 [实施报告](docs/RAG问答接入实施报告.md)。

按用户决定暂缓 RAG 语义专项，推进摘要、信息提取和本地稳定试用版。实施依据见 [分阶段工程任务书](docs/稳定试用版分阶段工程任务书.md)、[配套执行 skill](skills/docqa-stable-release/SKILL.md) 与 [执行提示词](docs/DeepSeek稳定试用版执行提示词.md)。摘要与信息提取已实现，独立验收修复了引用映射、取消、预算、幂等与启停缺陷；最新结论以 [稳定试用版独立验收报告](docs/稳定试用版独立验收报告.md) 为准。历史 S2 存在新增计算，真实格式及长文档在线证据不足，**尚不能判定稳定试用版达标**。[实施报告](docs/稳定试用版实施报告.md) 原文保留，不作为独立通过证明。

本阶段的最新修正和验证范围以 [Docling 接入独立验收报告](docs/Docling接入独立验收报告.md) 为准；原实施报告保留为初次交付记录。

## 当前链路

```text
网页上传扫描 PDF / TXT / DOCX / XLSX
  → 创建持久化解析任务（SQLite）
  → 独立 worker 领取任务并调用 Docling（TXT 在本地解析）
  → 保存结构化结果、来源与质量告警（不可变解析版本）
  → 结构化分块（正文按标题、表格按行组，均带来源）
  → 网页查看带来源的预览与告警
  → 用户主动建立 embedding 索引（固定目标解析版本）
  → 检索并展示原文与来源
  → 基于当前文档提问：检索 → 固定证据版本 → DeepSeek 生成 → 后端校验引用
  → 网页展示答案、逐事实引用与可核对来源

基于**解析版本**（不需要向量索引）的两条新链路：
  → 信息提取：查看分析范围 → 创建任务 → 分析 worker 分批生成与校验 → 确定性合并
                → 持久化已校验结果 → 页面逐条来源 / 复制 / 导出 Markdown、JSON
  → 文档摘要：单批直接完成；长文档分批生成已校验中间结果 + 一次汇总
                → 最终引用回落原文连续子串 → 持久化与导出
```

三类状态彼此独立：**任务状态**（排队/执行/失败/完成）、**内容质量**（结构是否可用、有何告警）、**索引状态**（哪个解析版本的向量可用）。分析任务另有独立状态与**覆盖口径**：任务失败不会把已有解析内容或已有成功结果标记为失效。

## 启动（最多四个进程）

需要 Python 3.11 或更新版本，以及本机可用的 Docling 解析服务（见下一节）。

```powershell
# 1) 安装依赖（首次）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 2) 启动 Docling 解析服务（独立 Docker 服务；只有解析新文档时才必需）
$dc = @('compose', '--env-file', 'deploy/docling/lab.env', '-f', 'deploy/docling/compose.yaml', '-f', 'deploy/docling/compose.ocr-gpu.yaml')
$env:PARSER_DEVICE = 'cuda'
docker @dc up -d --pull never --wait --wait-timeout 240

# 3) 启动 Web（终端 A）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4) 启动解析 worker（终端 B；不启动则解析任务停在 queued）
.\.venv\Scripts\python.exe -m app.parse_worker

# 5) 启动分析 worker（终端 C；不启动则摘要／提取任务停在 queued）
.\.venv\Scripts\python.exe -m app.analysis_worker
```

访问工作台 http://127.0.0.1:8000 ，交互接口文档 http://127.0.0.1:8000/docs 。第一次启动会自动创建 `data/docqa.db`、`data/uploads/`，并在需要时执行数据库迁移（迁移前自动生成一致性备份到 `data/db-backups/`）。

**或用 Windows 脚本一次启动**（推荐，带端口检查与进程归属记录）：

```powershell
# 预检：只核对数据目录、端口与迁移状态，不启动进程
powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1 -DataDir data -Port 8000 -DryRun
# 启动 Web + 解析 worker + 分析 worker（隐藏窗口，PID 写入所选数据目录/.local-service/）
powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1 -DataDir data -Port 8000
powershell -ExecutionPolicy Bypass -File scripts/status_local.ps1 -DataDir data
powershell -ExecutionPolicy Bypass -File scripts/stop_local.ps1 -DataDir data
```

脚本行为边界：端口被占用时**只报告不杀进程**；停止时只停止“PID + 可执行路径 + 数据目录”
三者都匹配的进程；不启动、不拉取、不停止 Docling 容器；不会触发解析、索引或模型调用。

**启动顺序**：Docling → Web → worker。Web 与两个 worker 可以任意先后启动，但对应 worker 必须运行才能执行任务；三者使用同一个 `DOCQA_DATA_DIR`。

**停止**：在各自终端按 `Ctrl+C`，或使用 `scripts/stop_local.ps1`。worker 收到停止请求后会保留当前任务的可恢复状态（上游 task_id、租约、已校验检查点）后退出。

**端口冲突**：解析服务默认 `127.0.0.1:5001`，Web 默认 `8000`。解析服务端口由 `deploy/docling/lab.env` 与 `compose.yaml` 决定；如需更换，同时修改 `DOCQA_DOCLING_BASE_URL`。Web 端口冲突时用 `--port` 指定其他端口。

**任务恢复**：已保存上游 task_id 的解析任务在租约过期后恢复查询与领取，**不会重新上传**。提交阶段中断且没有上游 ID 时进入 `needs_attention`。分析任务：租约过期且**没有**残留调用意图时回到 `queued` 可继续；存在“已发出请求但未保存结果”的调用时转 `needs_attention`，**不会自动重发**（供应商是否已接收无法确定，重试可能重复计费）。

**升级现有项目**：先停止旧 Web 与 worker，再启动新版本。schema v2～v4 自动备份并保留现有版本、分块和向量；v4 增加分析请求键映射，复用已有任务时也保存新键。回退代码时须同时恢复匹配的迁移前备份，并先保留、移开现库的 WAL/SHM 文件。正式库升级需用户单独决定；本阶段只在独立目录演练。

## Docling 解析服务

沿用已验证的 GPU 派生镜像与模型缓存卷，启动必须同时包含 `compose.ocr-gpu.yaml`，跳过 CPU 基线，不重新下载镜像或清空缓存。详细说明见 [deploy/docling/README.md](deploy/docling/README.md)。

- 固定参数：`rapidocr`、`ocr_lang=ch`、`do_ocr=true`、`force_ocr=false`、`table_mode=accurate`、输出 JSON + Markdown、图片占位；不默认启用远程 VLM、图片描述或公式增强。
- 服务地址只来自后端配置，前端不能修改；客户端禁用环境代理与重定向，也不会把 DeepSeek / embedding 密钥发送给解析服务。
- `GET /api/parsing/status` 只探测 HTTP 可达性：**可达不代表模型已预热或解析内容质量合格**。

## 上传与格式识别

支持 `.pdf`、`.txt`、`.docx`、`.xlsx`；`.doc`、`.xls`、图片、PPTX 等明确拒绝。扩展名与客户端 MIME 只作提示，实际格式由内容决定：PDF 检查文件标识与结构（含加密与页数上限），Office 检查 ZIP 包结构与实际包类型，TXT 检查 UTF-8 与非空内容。扩展名与内容冲突、损坏或加密文件都返回可读错误。

原件以服务端随机 ID 保存（无后缀），提交给解析服务时使用清理后的原文件名与匹配 MIME。上传不会自动解析，也不会自动建立收费索引。

## 解析任务与版本

| 接口 | 用途 |
| --- | --- |
| `POST /api/documents` | 上传文档，返回 201、识别格式与判定依据 |
| `POST /api/documents/{id}/parse` | 提交解析任务；新建/复用活动任务返回 202 与 task_id；已有有效版本且未 `force` 返回 200 复用 |
| `GET /api/parse-tasks/{task_id}` | 任务阶段、时间、告警与错误 |
| `POST /api/parse-tasks/{task_id}/retry` | 用户明确重试（新建尝试，保留原失败记录） |
| `POST /api/parse-tasks/{task_id}/cancel` | 停止本次等待，保留上游任务编号 |
| `GET /api/documents/{id}` | 当前预览版本、可用索引版本、最近任务与质量状态 |
| `GET /api/documents/{id}/versions` | 解析版本列表（含旧库迁移的 legacy 版本） |
| `GET /api/documents/{id}/content` | 结构化预览（块、来源、告警），支持 `version_id` 与分页 |
| `GET /api/documents/{id}/chunks` | 分块，默认活动版本，可读取该文档的历史版本 |
| `GET /api/documents/{id}/original` | 按文档 ID 获取原件（PDF 内联，Office/TXT 下载） |

版本规则：解析结果写入**不可变解析版本**，分块与来源绑定该版本；重新解析成功后才切换预览版本。旧库（本阶段改造前的数据）迁移为 `legacy` 解析版本，保留原有文档 ID、分块 ID/内容/页码、向量、维度与提供方签名，不重新 OCR、不调用 embedding。

## Embedding 索引与检索

按用户提供的示例接入 `https://tokendance.space/gateway/v1` 网关，模型为 `qwen3.7-text-embedding`。这是用户指定的网关地址，不是阿里云官方直连地址；请使用此网关签发的密钥。DeepSeek 密钥不会被自动复用或发送到该网关。

```dotenv
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=https://tokendance.space/gateway/v1
EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_TIMEOUT_SECONDS=60
EMBEDDING_BATCH_SIZE=8
```

使用流程：**测试向量连接 → 上传并解析文档 → 建立向量索引 → 检索当前文档原文**。

| 接口 | 用途 |
| --- | --- |
| GET /api/embedding/status | 配置状态，不返回密钥，不验证账户连通性 |
| POST /api/embedding/test | 固定文本测试，返回实际维度与归一化向量的前五项 |
| GET /api/documents/{id}/index | 当前可用索引、绑定解析版本、是否对应当前预览版本 |
| GET /api/documents/{id}/index/attempts | 索引构建尝试（含失败与过期） |
| POST /api/documents/{id}/index | 建立索引，固定目标解析版本；同版本同模型已存在时复用 |
| POST /api/documents/{id}/index?rebuild=true | 主动重建，会再次调用网关 |
| POST /api/documents/{id}/search | 返回 index_id、parse_version_id、是否旧版本与每条来源 |

索引构建是**候选发布**：向量全部成功并核对目标解析版本后才原子切换；失败不改变已有成功索引的可用状态。构建期间活动解析版本发生变化时，本次构建标记为过期（superseded），不会成为“新版本的索引”。

`source_signature` 带算法版本：新算法（`sha256-version-chunks-v2`）绑定解析版本与块结构，旧算法（`sha256-chunk-dump-v1`）保留用于验证旧库索引，不会因为新增来源字段把旧索引误判为过期，也不会重算签名掩盖损坏。

检索会返回命中分块的来源（PDF 页码与 bbox、DOCX 章节与表格、XLSX 工作表与单元格范围、TXT 行范围）；预览版本与索引版本不同时，页面与响应都会明确提示“检索仍使用旧版本”。

## RAG 问答（单文档、单轮、非流式）

选中文档 → 输入问题 → 检索该文档的有效索引 → DeepSeek 基于片段生成回答 → 后端校验引用 → 页面展示答案与可核对来源。

```dotenv
# 问答预算：单位是字符，不是 token；真正的模型上下文限制仍需供应商实测。
DOCQA_RAG_RETRIEVAL_K=8            # 一次提问最多检索的候选块数（1～20）
DOCQA_RAG_CONTEXT_K=5              # 最多送入模型的证据块数（1～retrieval_k）
DOCQA_RAG_CONTEXT_MAX_CHARS=12000  # 参考资料序列化后的字符上限
DOCQA_RAG_INPUT_MAX_CHARS=20000    # system + user 消息字符上限
DOCQA_RAG_MIN_SCORE=               # 空值表示禁用分数阈值；配置时必须是 [-1,1] 的有限数
```

| 接口 | 用途 |
| --- | --- |
| `POST /api/documents/{id}/questions` | 提问，请求体只有 `{"question": "..."}`；返回答案、状态、逐事实引用与检索统计 |
| `GET /api/rag/status` | 问答配置、Prompt 版本与预算；不调用任何收费接口 |

**回答状态**：`answered`（至少一个带引用的事实）、`clarification_needed`（1～2 个澄清问题，原因同样带引用）、`insufficient_evidence`（后端固定兜底文本，引用与澄清为空，只说明本次检索片段依据不足，不断言全文没有答案）。

**引用规则**：引用编号由服务端对**本次最终入选证据**分配，模型只能引用确实进入 Prompt 的编号；文档 ID、chunk ID、页码、坐标与文件路径全部由后端从证据映射复制，模型看不到也无法伪造。每个引述必须是对应证据正文的连续子串（只允许统一 CRLF），被使用的每个编号恰好对应一条引述。Markdown 中的引用标记例如 `事实。[1][2]` 由后端按已校验编号生成，不集中堆在末尾。

当前 Prompt 为 `rag-qa-v5`：先选原文引述，按论断分别组织引用；补充说明避免重复结论，澄清问题直接询问缺失条件。答案不设最低字数，但必须保留条件与例外。引用的语义支持仍需独立评估，当前结果与历史失败见 [RAG 问答接入独立验收报告](docs/RAG问答接入独立验收报告.md)。

**上下文与预算**：采用确定性整块装入——块连同必要元数据序列化后计入预算，容纳不下就跳过并继续尝试后续候选；入选块不会被从尾部硬截断，标题、表头、条件与脚注不会为了凑字数被删掉。因字符预算舍弃整块时返回 `retrieval.truncated=true`（表示证据集合不完整，不表示引用文本被截断）。字符预算**不等于** token 预算。

**版本策略**：检索结果返回后，index_id、parse_version_id、chunk 文本与来源作为本次请求快照固定到结束。回答期间发布新解析版本或切换活动索引，都不会把 A 的正文与 B 的来源拼成一次回答：本次仍基于固定快照回答，并通过 `is_old_version` / `is_current_index` 与质量告警提示版本变化，**不会二次生成、不会二次计费**。下次提问才使用新版本（需先为新版本建立索引）。

**错误与降级**：问题空白/超长/含额外字段 → 422；文档不存在 → 404；未解析、无索引或索引与当前 embedding 配置不兼容 → 409（不自动建索引）；缺少模型配置 → 503（不调用任何在线接口）；模型输出结构或引用校验不通过 → 502 + `rag_output_invalid`（不伪装成“资料不足”，不返回未校验内容，不自动重试）；上游超时 → 504。错误响应只含安全提示与稳定错误码，不含上游正文、异常堆栈、密钥或完整 Prompt。

**费用与幂等（如实说明）**：点击“提问”会发生在线调用并可能产生费用——问题会发送给已配置的 Embedding 服务用于检索，入选片段与问题会发送给已配置的 DeepSeek 服务用于生成回答。本阶段**没有持久化幂等保证**：两个独立的合法 POST 会产生两次费用；刷新页面或中断浏览器也不代表上游停止计费；问答结果本身不持久化，刷新后答案区清空。系统不会自动重发生成请求，页面加载、列表刷新、解析轮询与能力探测都不会触发问答。问答为单轮：不保存会话历史，澄清后的下一轮需要提交补全条件的完整问题，上一轮答案不会被当作事实回传。当前仅限本地服务运行，不应扩展为无鉴权公网部署。

**网页交互**：提问期间按钮与输入框禁用、文档切换被锁定（第一版沿用 busy 锁定），因此重复点击与 Enter 连按不会产生第二次请求；即使如此，成功与失败分支都会校验请求序号与当前选中文档，迟到响应不会覆盖新状态。答案、澄清问题、质量提示、适用范围与引用卡片全部用 `createElement`/`textContent` 渲染，只支持有限 Markdown（标题/列表/加粗），不解析任意 HTML、图片或外链；模型文本中的 `<script>`、`img onerror`、`javascript:` 只能作为文字出现。点击引用标记 `[n]` 会定位到对应引用卡片，卡片提供“查看引用版本”（版本级预览，不声称已精确高亮对应字符）与 PDF 原件页码链接；DOCX/XLSX/TXT 按真实来源展示，不伪造 PDF 页码。

## 文档摘要与信息提取（基于解析版本，不需要向量索引）

选中文档 → 点击“查看分析范围”确认解析版本、可分析单元数、输入批次与**请求数上界** →
明确发起“生成摘要”或“提取数据 / 结论 / 观点” → 分析 worker 分批生成并由服务端严格校验 →
结果持久化保存 → 页面逐条展示引用与来源，可复制或导出 Markdown／JSON。

```dotenv
# 分析预算：单位是字符，不是 token。每个任务的生成尝试次数是费用硬上限。
DOCQA_ANALYSIS_MAX_REQUESTS=8              # 单个分析任务的生成尝试上限（1～50）
DOCQA_ANALYSIS_BATCH_MAX_CHARS=12000       # 每批送入模型的输入单元字符上限
DOCQA_ANALYSIS_REDUCE_MAX_CHARS=16000      # 汇总阶段输入的字符上限
DOCQA_ANALYSIS_INPUT_MAX_CHARS=20000       # system + user 消息字符上限
DOCQA_ANALYSIS_MAX_DOCUMENT_CHARS=200000   # 单任务可遍历的解析版本文本上限
DOCQA_ANALYSIS_MAX_ITEMS_PER_BATCH=15      # 单批提取条目上限
DOCQA_ANALYSIS_MAX_ITEMS_TOTAL=60          # 单次任务提取条目总量上限
```

| 接口 | 用途 |
| --- | --- |
| `POST /api/documents/{id}/analysis-plan?kind=extraction\|summary` | **只做本地输入规划**：版本、范围、批次、消息长度、请求数上界与是否可执行；零外部调用 |
| `POST /api/documents/{id}/extract` | 创建／复用信息提取任务；新建或进行中 202，幂等已完成 200 |
| `POST /api/documents/{id}/summary` | 创建／复用文档摘要任务；状态同上 |
| `GET /api/analysis-jobs/{job_id}` | 状态、阶段、预算、覆盖口径与结果入口；不返回模型原始正文 |
| `GET /api/analysis-jobs/{job_id}/calls` | 调用账本：逐次请求的角色、状态与耗时 |
| `POST /api/analysis-jobs/{job_id}/retry` | 明确重试：预算不重置，已校验步骤复用 |
| `POST /api/analysis-jobs/{job_id}/cancel` | 取消后续处理，重复取消幂等 |
| `GET /api/documents/{id}/analysis-jobs` | 该文档的分析任务历史 |
| `GET /api/documents/{id}/analysis-results?kind=…` | 已校验结果历史（不含正文） |
| `GET /api/analysis-results/{result_id}` | 已校验结果（含逐条引用与来源） |
| `GET /api/analysis-results/{result_id}/export?format=markdown\|json` | 导出；文件名安全化、危险语法文本化 |
| `GET /api/analysis/status` | 分析配置与预算；不调用任何收费接口 |

**请求体只接受必要字段**（`parse_version_id`、`plan_fingerprint`、`idempotency_key`、`regenerate`）。
类型由 `summary`／`extract` 入口决定；拒绝未知字段，前端不能提交 system 提示、模型端点、证据正文或文件路径。
计划指纹不匹配（例如预览版本已切换）返回 409 并要求重新查看分析范围，避免静默扩大费用。

**输入规划**：遍历该解析版本的**完整合格正文与表格清单**（不是检索 top-k）。页眉、页脚、图片与空白块
被排除并单独计数；公式内容缺失时保留占位并标注“不能当作 0”。单个单元超过批次预算时本次规划
**不可执行**并说明原因——第一版不提供“只处理前几批却称为全文”的降级，也不自动拆成多个收费任务。

**引用规则**：服务端把单元编号**按批独立分配**，模型只能引用本批实际收到的编号；块 ID、页码、章节、
工作表与单元格范围全部由后端从本次输入映射复制。每条提取项必须有非空 `refs`，每个被使用的编号恰好
对应一条**原文连续子串**引述（仅允许 CRLF 归一化）。非空的事实字段（对象／数值／单位／时间／主体／口径）
必须能在**任一**被引用单元的原文中找到，否则判失败——不允许主句有引用、数值或时间字段另行编造。

**摘要的中间结果不是原文**：长文档先逐批生成**已校验**中间摘要，再用一次汇总调用合并；汇总阶段
只接收已校验条目与其引述，最终引用由服务端沿链**回落到原文单元编号**。任一必需批次未完成、
校验失败或预算耗尽时，本次不发布“完整全文摘要”，已校验检查点保留供明确重试，旧成功结果继续可读。

**覆盖口径**（与任务状态分开）：`total_units`、`processed_units`、`unresolved_units` 及原因、
`excluded_units` 及原因、完成批次数、是否执行汇总、`complete`，以及最终引用是否全部回落到原文。
`complete=true` 表示“该解析版本的可分析内容已全部处理”，**不是** OCR 正确率或任意全文理解率。

**错误与费用**：不存在 404；非法参数 422；解析版本不可用、计划指纹或幂等键冲突 409；缺生成模型配置 503；
上游错误只返回稳定安全提示与错误码，不含密钥、路径或上游原始响应。**查询状态、查看历史、复制与导出
都不会触发生成**；重试不会重置预算，页面明确提示重试可能重复计费。

**语义边界（必读）**：后端校验只证明结构合法、编号范围正确、引述可追溯到原文，
**不能证明每个结论在语义上必然正确**。真实语义由 `scripts/evaluate_analysis.py` 在明确预算内评估，
逐题结果与失败记录见 [稳定试用版实施报告](docs/稳定试用版实施报告.md)。

## 质量边界（必须阅读）

接口 `success` 不等于识别无误。本阶段明确记录并展示：

- PDF `formula` 节点内容为空 → `formula_content_missing` 告警，保留公式位置，不填补内容；
- XLSX 公式无缓存值 → `formula_cache_missing` 告警，**不得当作 0 或空白**，也不交由模型猜结果；有缓存时也只声明“文件保存的缓存”，不宣称已重新计算；
- 目录被识别成表格或长点线 → `toc_dot_leaders` 提示，只在目录上下文保守清理点线，不全局删除点号/连字符/百分号；
- 空白页 → `blank_page` 提示，页码保持原编号不变；
- 扫描 / 混合 PDF → `ocr_limitation` 提示，可能存在错字、目录页码错位与阅读顺序问题；系统**不声称能自动发现全部 OCR 错误或阅读顺序错误**；
- 图片 / 统计图 → 未做语义解析，图内文字与数据系列不作为可靠事实；
- 页眉页脚 → 保留在预览中，但不参与检索分块；
- 整份文档没有可索引内容 → 明确失败，不创建空的“成功索引”。
- 表格标题、表头、注释本身超过分块容量 → 报 `chunking_invalid`，保留旧版；可在模型输入限制内调大 `DOCQA_CHUNK_MAX_CHARS` 后重试，不截断关键条件。

已知上游缺陷（见全样本验收报告）不会被自动修复：中文条款错序、扫描目录页码错误、科学计数法被展平、公式内容缺失。页面始终保留原件入口用于人工核对。

## 尚未实现

网站抓取、浏览器插件、多用户系统、多文档知识库、跨文档问答、多轮会话记忆、查询改写、
重排服务、联网搜索、图表语义识别、公式重算均未接入；`/api/capabilities` 中对应项为 `false`。
旧 `.doc`/`.xls`、图片、PPTX 等格式未验收，不能因 Docling 支持某格式便直接对外承诺。
当前为本地单机、单用户部署，未引入 Redis/Celery 或专用向量数据库。

已实现（`/api/capabilities` 为 `true`）：上传与格式识别、Docling 解析（含扫描件 OCR）、
版本化解析与来源、质量告警、embedding 索引与检索、单文档单轮问答、**文档摘要**、
**数据／结论／观点提取**、分析任务持久化与结果导出。

已知限制（详见 [稳定试用版实施报告](docs/稳定试用版实施报告.md) 第 10 节）：

- RAG 语义专项按用户决定延期（D-RAG-01）：历史固定集 12/13，S03 的用户条件复述未通过既定来源检查；
- 网页行为的**真实浏览器**验收尚未执行（仅有离线渲染回归与代码级竞态校验）；
- 摘要汇总为单层（批次数 + 1 次调用），未实现多层汇总树；
- 新功能在线语义评估只覆盖 8 个合成场景，不代表长期正确率。

## 目录

```text
app/
  main.py                 应用工厂、API、页面入口
  config.py               环境配置与校验（含分析预算）
  schemas.py              数据契约（任务/版本/块/来源/告警/索引/问答/分析）
  migrations.py           带版本号的数据库迁移与一致性备份（当前 SCHEMA_VERSION=3）
  repository.py           SQLite 持久化（任务领取、版本发布、索引切换、分析任务与账本）
  file_detect.py          上传格式识别（内容判定与安全限制）
  docling_client.py       Docling 客户端（提交/查询/领取、错误分类、期限）
  parse_worker.py         持久化解析 worker（python -m app.parse_worker）
  analysis_worker.py      持久化分析 worker（python -m app.analysis_worker）
  document_normalizer.py  Docling 结果规范化、来源映射与质量告警
  chunking.py             结构化分块（正文/表格/目录/公式占位）
  parsing.py              本地 TXT 解析与旧分块兼容
  vector_index.py         版本化向量索引与检索
  embedding.py            Embedding 网关调用、分批与向量校验
  deepseek.py             DeepSeek API 调用与安全错误转换
  rag.py                  RAG 问答编排（前置检查、快照、生成、降级）
  rag_context.py          证据构建、字符预算与引用编号分配
  rag_prompts.py          固定系统规则与内部 JSON 协议（Prompt 版本 rag-qa-v5）
  rag_validation.py       严格解析、结构与引用校验、Markdown 渲染
  analysis_sources.py     分析输入规划（单元、批次、上界、覆盖口径、计划指纹）
  analysis_prompts.py     提取／摘要／汇总提示词与独立版本常量
  analysis_validation.py  严格解析、字段与引用校验、确定性合并
  extraction.py           提取分批、校验入口与三类状态汇总
  summarization.py        摘要分批与汇总、引用链回落
  analysis_jobs.py        分析任务生命周期服务（规划/创建/重试/取消/结果/导出）
  providers.py            OCR / Embedding / 向量库 / 大模型协议
  web/                    工作台页面
scripts/
  start_local.ps1         Windows 启动入口（端口检查、角色记录、隐藏窗口）
  status_local.ps1        服务状态与进程归属核对（只读）
  stop_local.ps1          只停止自身服务的停止入口
  seed_recovery_sample.py M5 恢复演练（注入合成解析版本，零真实调用）
  evaluate_analysis.py    摘要／提取语义评估（默认离线；在线需显式授权与累计预算）
  review_analysis_evidence.py 对已保存的原始模型正文做逐条复核（零在线调用）
  acceptance_e2e.py       真实解析链路验收（上传→异步解析→预览→来源）
  browser_acceptance.mjs  真实浏览器验收（T21/T22，Chrome + CDP）
  evaluate_rag.py         RAG 语义评估（默认离线模拟；--allow-online 才真实调用）
  evaluate_rag_live.py    真实在线最小批次评估（6 类样本，独立数据目录）
  rag_browser_acceptance.mjs  RAG 网页端到端验收（真实 Chrome + CDP）
  inspect_scan_page.py    扫描 PDF 页面结构核对（确认无文本层、来源为 OCR）
  container_ocr_page.py   容器内独立重新 OCR 指定页（核对引文是否来自原件）
  check_secrets.py        密钥自检（比对本地密钥是否出现在 Git 可见文件，不输出密钥值）
  validate_docling.py     独立解析服务验证（仅本机）
deploy/docling/           解析服务 GPU 部署与验证
docs/                     需求、验收与实施报告
tests/                    离线自动测试（不依赖 Docker、密钥或现有数据）
  fixtures/rag/           RAG 语义评估用例、合成样本知识库
  fixtures/analysis/      分析任务的固定合成样例清单（8 场景 + 4 负例）
  web/render_safety.mjs   网页渲染安全回归（加载真实 app.js 的 DOM 桩测试，17 项）
```

## 验证

```powershell
# 全部离线自动测试（临时目录、临时 SQLite、可注入客户端与模拟 embedding）
.\.venv\Scripts\python.exe -m pytest -q

# 网页渲染安全回归（离线，无需浏览器）
node tests/web/render_safety.mjs

# 分析任务离线语义固件（零在线调用）：8 正例通过、4 负例按设计失败
.\.venv\Scripts\python.exe scripts/evaluate_analysis.py --offline `
  --data-dir data/stable-release-dev/analysis-eval

# 分析任务在线语义评估（必须显式授权；受跨目录累计预算约束，不会重置）
.\.venv\Scripts\python.exe scripts/evaluate_analysis.py --allow-online `
  --cases E1,E2,E3,E4,S1,S2,S3,S4 --max-requests 24 `
  --data-dir data/stable-release-dev/analysis-eval

# 逐条复核已保存的原始模型正文（零在线调用）
.\.venv\Scripts\python.exe scripts/review_analysis_evidence.py

# RAG 语义评估：默认离线模拟（不产生任何在线请求）
.\.venv\Scripts\python.exe scripts/evaluate_rag.py --all-cases --data-dir data/rag-eval

# RAG 语义评估：真实在线（必须显式允许，会产生费用；有病例与请求上限）
.\.venv\Scripts\python.exe scripts/evaluate_rag.py --allow-online --all-cases --max-requests 30 --data-dir data/rag-eval

# 真实在线最小批次（6 类样本，复用扫描 PDF 的解析版本与索引）
.\.venv\Scripts\python.exe scripts/evaluate_rag_live.py --allow-online --max-requests 20 --data-dir data/rag-live

# 真实解析链路验收（需要已启动 Docling、Web 与 worker）
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe scripts/acceptance_e2e.py --api http://127.0.0.1:8010 --timeout 2400

# 真实浏览器验收 T21/T22（需要 Chrome 与正在运行的 Web）
node scripts/browser_acceptance.mjs --api http://127.0.0.1:8010
```

自动测试默认离线，覆盖旧库迁移、任务幂等与租约、上游错误分类、结果幂等发布、空白页、表格去重、多来源、XLSX 坐标与公式缓存、长表分块、索引候选发布与失败保留、格式伪装与越权访问、RAG 证据范围与字符预算、输出协议与引用校验、HTTP 状态与调用次数、版本竞态与上游错误脱敏，以及**分析任务的迁移 v3、输入规划与覆盖口径、幂等与原子领取、租约与过期令牌、逐请求预算预扣、不确定调用不自动重发、检查点复用与重试、版本竞争、跨批编号拒绝、字段依据校验、多批摘要引用链回落、导出与转义、真实 SDK + MockTransport 的 HTTP 契约**等场景。`tests/test_real_fixtures.py` 使用真实 Docling 结果夹具，夹具缺失时会跳过并提示，不会把跳过当成通过。

## 数据库迁移与恢复

启动时按版本号执行迁移（当前 `SCHEMA_VERSION=4`），迁移前用 SQLite 在线备份 API 生成一致性副本到所选数据目录的 `db-backups/`（包含 WAL 中已提交数据）。迁移失败会停止启动并保留原库与备份。v3 新增分析表；v4 新增 `analysis_request_keys` 并结转旧请求键，不改写旧任务或结果。

恢复步骤：

1. 停止 Web 与 worker，避免继续写入；
2. 完整保留现库及其 `-wal`、`-shm`；活跃库另取副本时使用 SQLite backup API；
3. 在服务全部停止后，把现库及对应 WAL/SHM 移到保留目录，再将选定的迁移前一致备份恢复到目标库路径；不能残留旧 WAL；
4. 使用与该备份版本匹配的代码启动。先核对工作区未提交修改，不能为了回滚覆盖尚未保存的工作；正式恢复也需用户决定。

**本阶段未对正式库执行迁移**，也没有执行 Git 提交或推送；升级正式库需用户单独决定。
