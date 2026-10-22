# DocQA 智能文档分析与知识问答系统

面向小团队／企业内网的本地文档助手。管理员维护并授权知识库，客户依据获授权资料进行多轮问答、查看引用，也可上传临时资料；管理员工具提供解析、检索、摘要和信息提取。

**当前状态（2026-10-22）：已部署的本地团队试用版，尚未完成完整生产验收。** 本地8000已切换统一知识库对话和自动入库链路、数据库schema14，保留原历史、引用和索引；账号隔离、会话记忆、意图规则分流、链路追踪和真实分段流式已接通。服务驻留／异常退出、反馈工单、用户／全局配额、团队容量及完整运维出口仍待完成。数据库版本不是产品版本，历史测试成绩也不是当前服务在线的证明。

## 当前版本与入口

以下版本由 `scripts/check_docs.py` 对照源码检查；修改实现后应同步维护。

| 项目 | 版本 |
| --- | --- |
| 项目包 | `0.2.0` |
| 数据库 schema | `14` |
| 任务式问答 Prompt | `rag-task-v29` |
| 检索策略 | `hybrid-context-v5` |
| 事实引用协议 | `fact-context-parts-v7` |
| 意图路由 | `intent-rules-v2` |
| 流式协议 | `validated-fact-stream-v1` |
| 知识库任务协议 | `knowledge-task-v2` |

| 入口 | 用途／访问范围 |
| --- | --- |
| `/`、`/app` | 统一知识库对话：历史、库内文档筛选、自动入库、记忆、流式与个人头像 |
| `/app/knowledge`、`/single` | 旧链接跳转统一对话；旧记录与原引用继续读取 |
| `/setup`、`/login` | 本机首次初始化、团队登录 |
| `/admin` | 管理后台与知识库配置 |
| `/knowledge`、`/admin/chunks` | 上传、解析／分块、索引、发布授权；检查和调整分块边界 |
| `/admin/users`、`/admin/settings` | 账号管理；只读模型连接与运行配置，连接测试需主动操作 |
| `/admin/intents`、`/admin/traces` | 意图树／列表／测试台；实际任务链路和已记录节点时序 |
| `/admin/documents`、`/tools` | 文档工程工具、摘要／提取 |
| `/docs` | OpenAPI 交互文档，团队模式下限管理员 |

完整标准、实现证据与后续工作从[文档索引](docs/README.md)进入。继续开发先读[接力说明](docs/稳定试用版接力说明.md)和[下一阶段计划](docs/product/下一阶段优化计划书.md)。[原始需求](智能文档分析与知识问答系统.md)及旧任务书保留编写时的范围，不作为当前功能清单。

## 安装与启动

需要 Python 3.11+。PDF、DOCX、XLSX 的新解析需要独立 Docling + RapidOCR 服务；UTF-8 TXT、Markdown 在本地解析。读取已有历史结果不需要重新解析或调用模型。

```powershell
# 在项目根目录执行；首次安装
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.venv\Scripts\python.exe -m pip install -e ".[dev]"
# 仅首次且不存在 .env 时复制；已有配置须保留
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

# 只读预检，再启动 Web + parse-worker + analysis-worker
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_local.ps1 -DataDir data -Port 8000 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_local.ps1 -DataDir data -Port 8000
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/status_local.ps1 -DataDir data
# 只停止此数据目录下身份匹配的三个角色
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop_local.ps1 -DataDir data
```

填写本机 `.env` 中独立的生成／Embedding 密钥。配置优先级为进程环境变量 > `.env` > 代码默认值；相对数据目录以项目工作目录解析。管理后台不在线保存或回显密钥；修改服务器配置后须整体重启三个角色。生成使用 DeepSeek 适配器；Embedding 默认使用配置样例中的独立网关，不自动复用生成密钥。

正式本机实例使用 `D:\python\DocQA\data`、`http://127.0.0.1:8000`。脚本目录优先级为显式 `-DataDir` > 环境 `DOCQA_DATA_DIR` > `.env` > 项目 `data`。首次空目录自动建库；已有旧库默认拒绝启动，确认备份及升级范围后才加 `-AllowDatabaseUpgrade`。v13→14需增量迁移，先在一致性副本演练；v14库不再重复迁移。

三个角色共用同一数据目录。就绪需同时满足 `running`、`identity_match`、`data_dir_match`、`ready`、`code_match`、`schema_match`；过期 `.ready` 文件不能证明进程存活。源码更新后旧进程拒绝继续处理，须整体重启。团队模式下匿名 `/api/health` 返回401；启动脚本结合 `/api/auth/me` 和本机角色身份核对就绪，不公开运行详情。

脚本不安装 Windows 服务、开机启动或进程守护。宿主关闭、系统重启或启动环境回收进程后，应重新检查状态并启动；无人值守部署及异常退出监控仍属运维待办。不要用历史验收日志代替实时检查。

也可在三个独立终端手动运行（均从项目根目录、使用相同配置）：

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
.venv\Scripts\python.exe -m app.parse_worker
.venv\Scripts\python.exe -m app.analysis_worker
```

Docling 为第四个独立服务，标准本地脚本不启动或停止它。沿用已有镜像和模型缓存，构建／GPU／健康检查命令见[解析服务部署](deploy/docling/README.md)。不要重建镜像或清除模型卷来解决 Web 登录／数据库问题。

## 首次设置与账号权限

首次在服务器本机打开 `http://127.0.0.1:8000/setup`，设置管理员账号和 **4～32字符**密码，没有默认密码。提交后团队登录配置随数据库持久化。CLI `python -m app.product_admin --data-dir <目录> --username <账号>` 仍可交互创建管理员，但须另行开启团队模式。

客户只能访问自己的会话和当前获授权的发布资料；管理员不因诊断权限默认获得客户私聊和临时原件。账号停用、撤权、附件到期影响后续任务、事件发送及历史读取。变更请求使用会话 Cookie、CSRF 和同源检查。

HTTPS／内网部署显式设置 `DOCQA_TEAM_MODE=1`、`DOCQA_SESSION_COOKIE_SECURE=1` 并重启，覆盖本机 HTTP 初始化保存的 Cookie 配置。当前没有公网 SaaS、多租户或企业单点登录的完整验收。

## 知识库完整使用流程

1. **建库**：管理员在 `/admin` 设置名称、Collection 和当前服务器使用的 Embedding 模型。Collection 为2～64位小写英文字母／数字且唯一，不能任意选择未配置模型。
2. **上传**：后台 `/knowledge` 或对话输入区“＋”选择目标库，上传 PDF、DOCX、XLSX、TXT 或 UTF-8 Markdown。团队库仅管理员可写；客户可建立私有会话临时库。对话支持一批多文件，逐文件反馈。旧二进制 `.doc`、`.xls` 应先转换；独立图片、PPTX 尚不支持。
3. **自动准备**：原件与上传任务同事务登记，worker衔接解析、分块和索引，关闭浏览器仍继续。Markdown 识别标题结构；表格保留表头、单位和注释。上传不自动生成回答；索引批次调用向量服务并保留预算和检查点。
4. **检查／修订**：从 `/admin/chunks` 检查真实原文，可拆分／合并相邻块并调整连续范围，必须保留全部原文和顺序。文字纠错通过上传新版原件完成。修订建立新草稿，不覆盖旧发布版。
5. **生效策略**：默认自动生效，每份资料成功后加入新的可用快照；也可在库内设为审核生效，逐份确认。失败、待处理或待审核文件不阻塞旧可用资料。手动分块修订与旧接口继续保留明确索引／发布操作。
6. **替换和授权**：上传时可明确替换已有文档，成功后切换新版，旧原件与快照保留引用；失败保留旧版。管理员另行授权客户，不因上传扩大成员权限。
7. **客户提问**：统一从 `/app` 进入，选择知识库，可筛选一份或多份文档。资料处理中也可保存一个等待问题，就绪后提交一次；指定文件失败会明确失败，不改用其他资料。每轮固定实际快照、文档及记忆版本。

当前存储为 **SQLite + 本地文件**：向量和版本记录保存在 SQLite，原件在数据目录 `uploads/`。Collection 是逻辑标识。没有接入 RustFS、Redis 或独立向量数据库，不会因建库自动创建这些外部服务的桶／集合。

## 会话、意图与流式回答

只有一个客户对话入口和历史列表。单篇文档问答是知识库内的文档筛选；切换知识库开始新会话。旧文档会话、独立问答和库级会话通过适配层合并展示，保留原ID、结果和引用，不重新生成；原会话中的问答不再作为独立记录重复列出。继续旧会话时，新轮次转入所属知识库，未归库资料先提示管理员处理。

近期最多3条已成功的用户问题和主动保存的800字符条件参与检索理解，旧回答不是新证据。记忆可开关、清除，支持名称、归档和恢复；变化会取消等待中的旧记忆请求。结构化对象指代与比较规划的真实语义验收尚未完成。

意图为受控规则引擎：完整示例、限定纯交互、当前库内业务关键词。纯问候／反馈可持久化短回复，0供应商请求；混合业务和歧义回到知识检索。管理员测试台最多100条问题，展示候选、回退原因和配置版本，不调用模型／工具，不要求发布知识库。规则用例通过不等于通用语义理解准确率。MCP 只读适配器有离线协议验证，尚无已验收的真实销售、工单或天气接口。

新文档／知识库问答任务采用真实上游流式：

```text
提交幂等任务 → analysis-worker 领取并预扣调用
  → 意图分流；知识问答检索并固定证据
  → 一次上游生成请求，接收增量 JSON
  → 完整论断到达 → 局部引用／限定校验 → 持久化事件 → SSE 展示“生成中”
  → 全部协议、覆盖和引用复验 → 保存正式结果 → 完成／开放导出
```

不显示思考、半截 JSON 或未经局部校验的文字。局部事件不是完整成功答案；失败、截断、取消和 worker 中断保留未完成状态。刷新／SSE 重连按游标读取原任务，不另发生成；每条事件重验登录和权限。原同步 `/questions`、无流式快照的旧任务继续兼容，不在读取旧结果时补造新认证。

当前 thinking 参数不变，首块仍需等待模型正文；本轮单题实测首块约41.95秒、上游结束约43.33秒，6个有效块在上游结束前落盘。这证明流式接通，不代表普遍性能或P95成绩。

## 摘要、信息提取与临时资料

摘要／提取基于固定解析版本，**不需要向量索引**。先查看分析范围、批次和请求上界，再明确提交。长文档逐批校验并汇总，最终引用回落原文；必要批次未完成时不发布“全文完成”。支持历史、来源、复制和 Markdown／JSON 导出。详细协议及语义边界见[阶段03](docs/阶段03-稳定版工程与恢复.md)和[阶段04](docs/阶段04-真实语义验收与提取改进.md)。

聊天上传也进入知识库链路；可向当前可写库追加多份文档，或创建本人专属临时库。临时库默认24小时、每人同时5个，其他用户和管理员不能读取其原件及问答。到期／移除后停止读取、事件发送和后续调用。已发出的上游请求可能继续计费。新旧临时资料物理清理共用停机命令：

```powershell
.venv\Scripts\python.exe -m app.attachment_maintenance --data-dir D:\python\DocQA\data
```

清理保留调用账本和请求键，不清历史备份或 Docling 缓存，不承诺安全擦除。自动定时清理和备份保留政策待完成。

## 配置、容量与调用预算

完整参数见 [.env.example](.env.example)。以下为当前默认值，字符容量不等于模型 token 窗口。

| 范围 | 参数与默认值 |
| --- | --- |
| 上传 | `DOCQA_MAX_UPLOAD_MB=20`、`DOCQA_MAX_PDF_PAGES=100` |
| 临时资料 | `DOCQA_ATTACHMENT_TTL_HOURS=24`、`DOCQA_ATTACHMENT_MAX_ACTIVE=5` |
| 分块 | `DOCQA_CHUNK_MAX_CHARS=800`、`DOCQA_CHUNK_OVERLAP_CHARS=120`、`DOCQA_TABLE_ROWS_PER_CHUNK=20` |
| 任务式问答 | `DOCQA_QUESTION_CANDIDATE_K=64`、`DOCQA_QUESTION_CONTEXT_K=32`、资料24000／总输入32000字符；`DOCQA_QUESTION_MAX_REQUESTS=4` |
| 同步兼容问答 | `DOCQA_RAG_*` 独立控制：候选8／装入5、资料12000／总输入20000字符 |
| 摘要／提取 | `DOCQA_ANALYSIS_*`：最多8请求／任务，批次12000／汇总16000／总输入20000字符，文档200000字符 |

任务问答的装入数≤候选数≤100、资料字符上限<总输入上限；配置变更会使旧排队快照受控失败，新任务使用新配置。必要证据槽位优先装入，正文按问题抽取连续原句，表格保留表头／单位／空白和0；未装入内容不能引用。更大容量须另验供应商窗口、延迟和费用，不靠扩大字符数宣称覆盖必然充分。

一次普通知识问答通常为1次查询向量＋1次生成；无合格证据可跳过生成。每次外调先预扣，失败／不确定也计次，明确重试不重置；不确定在途请求进入 `needs_attention`，不自动重发。刷新、历史、配置、意图测试台、事件重放、导出不新增模型请求。主动连接测试、建索引、业务提问、摘要和提取可能计费。

工程验收另用同一累计账本，当前102/128，余26；包含失败和不确定调用。本轮新增3次用于新TXT自动入库及知识库流式回答。该账本不是全部客户的日用量／货币限额，用户及全局配额仍待实现。

## API 与实现导航

请求体、字段及鉴权以管理员登录后的 `/docs` 为准。不同链路使用各自请求键，不应将文档会话的 `idempotency_key` 与库级的 `request_key` 混用。

| 接口族 | 关键入口 |
| --- | --- |
| 账号／知识库 | `/api/auth/*`、`/api/admin/users`、`/api/knowledge-bases`（发布／成员授权） |
| 库内上传／索引／分块 | `/api/admin/knowledge/bases/{base_id}/documents`、`/api/admin/knowledge/documents/{id}/index-tasks`、`chunks`／`chunk-revisions` |
| 文档解析／版本／检索 | `/api/documents/{id}/parse`、`/api/parse-tasks/{id}`、`content`／`versions`／`chunks`／`original`、`index`／`search` |
| 文档会话 | `/api/conversations`、`/{id}/turns`、`PATCH /{id}`（记忆／归档／名称） |
| 库级会话 | `/api/kb-conversations`、`/{id}/turns`、`/{id}/memory`、`/api/kb-tasks/{id}` |
| 问答任务／结果 | `/api/documents/{id}/question-jobs`、`/api/question-jobs/{id}`、`/api/question-results/{id}`，含明确重试／取消及结果导出 |
| 流式重放 | `GET /api/question-jobs/{id}/events`、`GET /api/kb-tasks/{id}/events`，支持 `after`、`Last-Event-ID` |
| 摘要／提取 | `/api/documents/{id}/analysis-plan`、`summary`／`extract`、`/api/analysis-jobs/{id}`、`/api/analysis-results/{id}` |
| 意图／链路 | `/api/admin/intents`、`POST /api/admin/intents/test`、`/api/admin/traces` |
| 运行诊断 | `/api/health`、`/api/runtime/status`、`/api/admin/settings`、`/api/parsing/status`；仅解析状态会探测本地解析服务 |

核心实现：`app/main.py` 装配和权限入口；`product_access.py`／`product_routes.py` 管理账号与发布；`knowledge_pipeline.py`／`knowledge_questions.py` 管理库流程；`conversations.py` 管理文档会话；`intent_routing.py` 执行规则；`question_worker.py`／`knowledge_worker.py` 执行任务；`rag.py`／`rag_answer_contract.py` 处理证据及事实协议；`answer_stream*.py` 与 `web/answer-stream.js` 实现流式持久化和重放；`migrations.py` 是数据库结构来源。

## 数据库升级与恢复

当前代码要求 schema14，迁移为1～14连续登记。v6账号及发布、v7会话、v8临时资料、v9安装设置、v10库级任务／意图／链路、v11头像、v12意图策略、v13流式事件、v14统一知识库链路。v14新增6张表及库／会话属性，旧未归库文档只增加管理员私有范围投影，已有有效索引复用；旧结果、解析及向量不重写或重建。正式升级保全检查以发布核验为准。

升级前停止三个角色，检查活动任务，使用 SQLite backup API 做一致性副本（包含 WAL 已提交数据），在副本演练并验证旧记录／来源后再正式升级。标准入口对获准旧库自动备份至 `db-backups/`；结构不兼容、断裂迁移或未来版本拒绝启动，`-SkipMigrationCheck` 不能绕过。

回退须先保存升级后新增会话、原件、头像、账本及当前库／WAL／SHM，再恢复**匹配版本的源码和一致性备份**，不能让旧 WAL 覆盖恢复库。数据库备份不包含 `uploads/` 原件、服务器 `.env` 或 Docling 模型缓存，完整灾备需分别保存和演练。历次切换证据见[阶段03](docs/阶段03-稳定版工程与恢复.md)；实时状态另查本机进程。

## 验证与质量边界

GitHub Actions 的 `Full regression` 在每次推送、PR更新及手动运行时执行全量离线回归，配置位于[现有工作流](.github/workflows/rag-quality.yml)。使用Windows、Python3.11、Node24和锁定依赖，在项目`.venv`运行，覆盖Windows PowerShell5.1与PowerShell7的实际启停测试；不使用模型密钥，不启动Docker或下载模型。

本地可运行与CI相同的入口，报告目录必须是`data/`内的新目录，已有结果不会覆盖：

```powershell
.venv\Scripts\python.exe scripts/run_regression.py --output-dir ("data/ci-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
```

入口执行依赖一致性、全量pytest、自动发现的全部`tests/web/*.mjs`、文档一致性、既有RAG检索基线与防退步关卡、事实引用和范围回放，以及`git diff --check`。独立检查失败后继续收集其他结果，最终返回非零；不自动重试。GitHub运行摘要及保留14天的报告包含JUnit、逐检查日志和JSON，上传范围不含运行数据库、原件或`.env`。跳过项明确保留，不代表在线语义或真实浏览器验收通过。分项排查仍可使用：

```powershell
.venv\Scripts\python.exe scripts/check_docs.py
.venv\Scripts\python.exe -m app.runtime --data-dir data
.venv\Scripts\python.exe -m pytest -q
Get-ChildItem tests/web -Filter *.mjs | ForEach-Object { node $_.FullName; if ($LASTEXITCODE -ne 0) { throw '网页检查失败' } }
```

测试默认使用临时目录／模拟传输，不依赖真实密钥；仓库必备格式夹具缺失会失败，需要外部条件的跳过项会明确列出，不能算通过。`check_docs.py` 检查文档链接、冻结归档哈希、README版本及配置样例与源码的一致性，不证明每句业务描述或模型语义正确。在线评估、真实解析和故障演练按各自冻结范围在独立目录执行，不拿正式默认库做实验。

2026-10-22使用上述CI共用入口的本地单次验证：1425项pytest通过（486.49秒、0跳过、2条既有依赖警告），9组网页及其余离线关卡通过，共17项检查通过。工作流actionlint校验通过。GitHub托管状态以[对应提交的实际运行](https://github.com/TongYule131/DocQA/actions/workflows/rag-quality.yml)为准，不能把本地结果当成远端运行结果。

RAG 历史三题同一关键引文实际输入覆盖8/17→17/17，核心内容和 DOCX／XLSX 回归经过开发复核，仍有性质声明待复核；这是规定资料的历史开发成绩。本轮统一链路单次全量1417通过（428.43秒、2条既有依赖警告）；随后完善并发别名、临时资料清理和文件列表，追加29项定向及8组网页检查通过，pip依赖检查通过，不累加成一次全量。真实隔离TXT链路新增3次HTTP，索引／查询／生成各一次，流式和最终引用保存成功。不是全部资料或多轮语义重新验收。首次失败、修复和正式切换见[阶段05](docs/阶段05-代码审查与加固.md)及接力说明。

原文精确匹配证明可追溯性，不能自动证明语义支持。OCR错字、阅读顺序、扫描表格、缺失公式／XLSX公式缓存仍需原件核对；空白不等于0，不重算公式。引用坐标为块区域，不承诺原始 PDF 逐字符高亮；结构推断、未装入范围及语义待复核状态保留在详情与导出。OCR实验尚未替换正式解析默认。独立新资料盲测、真实多轮收益、完整无缓存性能和容量验收继续按[提升路线](docs/整体能力提升方案.md)推进。

## 隔离界面预览

```powershell
$env:DOCQA_DATA_DIR = Join-Path (Get-Location) "data/preview-local"
.venv\Scripts\python.exe scripts/product_preview.py --data-dir $env:DOCQA_DATA_DIR --port 8997 --streaming-preview
```

仅允许本工作树下独立目录及回环地址。脚本运行实际解析worker（复杂格式仍需 Docling）和模拟索引／生成，页面有明确标识；`--streaming-preview` 演示分段输出，不证明真实模型质量。使用 `http://localhost:8997`；预览 Cookie 名为 `docqa_preview_8997`，正式实例保留 `docqa_session`，仅换端口不能隔离同主机Cookie。普通启动使用实际供应商配置。
