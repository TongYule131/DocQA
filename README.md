# DocQA 智能文档分析与知识问答系统

> 续作完善候选（2026-10-25，未部署）：浏览器／服务端重复限定判断统一、流式引用收敛为可点击编号、复数指代补齐、校准错维负例拒绝、校准／验收分离冻结CLI及驻留跨登录证据已实现并定向验证，待一次整包GitHub full。正式仍9114554／schema16。新增60题合成业务标签和7份回答待用户分别审核，未封存或进行新验收评分；原60题与旧成绩不改。账本318/328、余10，本轮HTTP0，C02不重发，本地全量未执行。细节与剩余出口见[任务书](docs/下一阶段任务书-S2-S5闭环补齐与试点验收.md)。

> 最新受控工程试用发布（2026-10-25 19:13，Asia/Shanghai）：源码9114554的[GitHub full CI](https://github.com/TongYule131/DocQA/actions/runs/40175984783)通过（1923项Python、13项网页、共21项检查）；先保全源码／配置和完整业务备份、验证恢复及副本升级，再部署schema16。正式8000三角色同版、心跳与监管器ready；已注册当前用户Interactive／Limited登录任务。旧业务字段、59个资产、10个活动索引、头像、4份单文档／12份知识库结果保留。新增语义意图／模型规划／精排／问题摘录保持关闭。60题标注已确认封存，但检索完整可见22/24、业务合成未见意图22/24未达95%；C02不重发、旧语言gate和跨登录／人工试点待验保留。唯一账本318/328、余10，发布HTTP0，本地全量未执行。全部38项状态与限制见[任务书](docs/下一阶段任务书-S2-S5闭环补齐与试点验收.md)；不能概括为S2～S5全部语义出口通过。以下旧状态为对应日期历史。


> 2026-10-25完整性复核：6d7dd06的GitHub full CI通过记录保留；S2～S5均有未闭环内容，本次隔离复现9项缺陷，列出38项未完成工作及6项保留待办。当前候选不宜直接部署，先修计费／配额、熔断、备份恢复和隐私生命周期问题，再按S2→S3→S4→S5补齐。具体任务、证据和出口见[下一阶段任务书](docs/下一阶段任务书-S2-S5闭环补齐与试点验收.md)。正式v60／schema14未改，285/288、余3；本轮真实调用0、本地全量未执行。


> 运行核对（2026-10-24 22:16，Asia/Shanghai）：Web、parse-worker、analysis-worker均已退出，当前8000服务未就绪；代码和schema仍匹配。19:55三个角色就绪是发布时快照，持续驻留故障未诊断。本次只读核对，没有重启服务。

> 当前RAG功能主线恢复ragent方案S2剩余项：统一通道候选契约与融合归因 → 冻结候选离线对照 → 按收益决定是否启用可选精排；随后才是S3问题理解/实体记忆、S4反馈、S5配额/容量。服务驻留是可独立处理的运维阻断，不据此重排已采纳的RAG依赖。

> 最新发布（2026-10-24 19:55）：本地8000已更新rag-task-v60／evidence-composition-v24／事实v8／流式v2，Web、解析与分析三个角色同版就绪，schema14无需迁移。用户最新要求“不一定要全部有提升,但语言逻辑要流畅”，本次按该口径完成受控试用发布；原12题6新版/5平局/1旧版、四维均分4.67/4.83/4.83/4.50，原9/12出口仍未通过，未改题或评分。开发复核及辅助审阅未发现关键事实/条件/覆盖错误，组织和连贯单题最低均4。6d9bbd5对应[GitHub全量CI](https://github.com/TongYule131/DocQA/actions/runs/40090840957)1838项及全部离线关卡通过；本地全量未执行。累计285/288、余3，无发布新增模型调用。 恢复以[续作指引](docs/新对话续作指引.md)为准。

面向小团队／企业内网的本地文档助手。管理员维护并授权知识库，客户依据获授权资料进行多轮问答、查看引用，也可上传临时资料；管理员工具提供解析、检索、摘要和信息提取。

**当前状态（2026-10-23）：已部署的本地团队试用版，尚未完成完整生产验收。** 本地8000已切换统一知识库对话和自动入库链路、数据库schema14，保留原历史、引用和索引；账号隔离、会话记忆、意图规则分流、链路追踪和真实分段流式已接通。服务驻留／异常退出、反馈工单、用户／全局配额、团队容量及完整运维出口仍待完成。数据库版本不是产品版本，历史测试成绩也不是当前服务在线的证明。

2026-10-23本次正式实例更新已完成：ragent方案前三批的查询计划／预算、全局证据槽位、数学证据组、页地址映射和对应界面诊断已在8000生效。三个角色同版且就绪；数据库与代码均要求schema14，初始化无需结构迁移，56张表／11,084条记录、9个活动索引及旧引用保留。新解析格式v9用于后续解析，不会将旧解析版本改写或自动重建索引。发布证据位于本地 `data/ragent-release-20261023-1659/verification-final.json`，详见[接力说明](docs/稳定试用版接力说明.md)。本次只引用既有有效定向结果并补发布兼容检查，本地全量未执行，新增模型HTTP0；不将部署完成说成S0～S5或真实模型语义验收全部完成。

## 当前版本与入口

下表是根工作区正式源码版本，由 `scripts/check_docs.py` 核对；新链路schema15候选独立保存在codex/rag-s2-s5-ci临时分支，本轮未覆盖正式app。

| 项目 | 版本 |
| --- | --- |
| 项目包 | `0.2.0` |
| 数据库 schema | `16` |
| 新解析规范化结构 | `docqa-normalized-9`（旧版本继续可读） |
| 任务式问答 Prompt | `rag-task-v60` |
| 检索策略 | `hybrid-context-v12` |
| 事实引用协议 | `fact-context-parts-v8` |
| 意图路由 | `intent-context-semantic-v3` |
| 流式协议 | `validated-fact-stream-v2` |
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

当前已在rag-task-v60生成协议上发布schema16工程候选，补齐S2实际通道与冻结指标、S3有界理解与持久澄清、S4反馈回归证据及S5配额／维护／驻留。语义意图95%、独立人工回答认证、跨登录和完整人工试点仍开放，可选收费功能保持关闭。账号／改密、真实MCP和新解析档位仍按任务书保留。详见[38项任务最新矩阵](docs/下一阶段任务书-S2-S5闭环补齐与试点验收.md)。

## 安装与启动

需要 Python 3.11+。PDF、DOCX、XLSX 的新解析需要独立 Docling + RapidOCR 服务；UTF-8 TXT、Markdown 在本地解析。读取已有历史结果不需要重新解析或调用模型。

PDF首次解析会检测明显的文本映射异常。命中大量控制字符、未映射字形或装饰符号时，保留原件，使用同页同尺寸的图像副本交给现有OCR；正文或表格仍有明显乱码则拒绝发布新解析，不覆盖旧版本。渲染有页数、单页像素和提交大小限制；公式、上下标及数学推导仍须核对原件，正文恢复不代表建模能力完整通过。

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

工作区已实现 ragent 对比方案首阶段：`query-plan-v1` 将业务路由与自动任务策略分开。事实、概述、多跳、对比、边界、概念和公式核对采用不同证据需求与最终块数上限；节点 TopK 只能收紧上限，不能扩大资料选择或授权。查询计划与预算随新任务冻结，候选上限使用实际配置（默认64），输入预算按系统规则、问题、记忆和资料的真实序列化长度计算。管理员测试台可预览任务策略、证据需求、预算及未就绪资源，不把规则匹配分值显示成语义概率。

`grounded-retrieval-pipeline-v4` 在所有资料候选核对后统一保全证据槽位，区分没有候选、未装入和锚点不在实际可引用范围。定义、案例、数值等锚点保留完整原句及邻近限定。`math-evidence-groups-v1` 对同一命题／推论下有明确关联的公式、解释及条件实行整组装入；候选或预算只够半组时不默默使用半组，数学关联不合并不同原文的quote。不可读数学表示不用于该轮公式核对，普通正文问题不会因无关公式限制而全部拒绝。

`grounded-number-roles-v4` 区分物理页、印刷页、PDF标签等定位、显式文献序号、变量数字上下标与真实数值；上下标还核对变量身份，不能为金额或人数背书。`pdf-page-labels-v1` 为新PDF解析建立地址映射：目录标签不冒充印刷页码，页脚需真实节点和底部坐标，无目录时还需邻页独立观察到连续标签；冲突仅显示物理页，不推算缺失页。旧来源的序列化、索引签名和引用身份保持；不会为旧资料自动重解析。`source-diagnostics-v3` 只读记录异常字符、定位缺失、数字角色、页标签状态及未索引公式区域，不宣称OCR或公式语义已验证。只有全部索引原文实际装入才记录完整索引阅读；其余为入选证据范围。工作区状态与定向证据见[接力说明](docs/稳定试用版接力说明.md)，不等于整个提升方案或真实模型语义验收已完成。

新文档／知识库问答任务采用真实上游流式：

```text
提交幂等任务 → analysis-worker 领取并预扣调用
  → 意图分流；知识问答检索并固定证据
  → 比较／多跳／概念题先做有预算的选材与段落规划，保存有效检查点
  → 一次正文生成请求，接收增量 JSON
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

事实、边界及公式等单次写作路径通常为1次查询向量＋1次生成；比较、多跳、概念题的选材／段落规划与正文写作各占1次生成，通常共3次供应商HTTP。它与S3已实现的可选问题理解规划职责不同；后者默认关闭、另计请求上限。无合格证据可跳过生成。每次外调先预扣，失败／不确定也计次，明确重试不重置；不确定在途请求进入 `needs_attention`，不自动重发。刷新、历史、配置、意图测试台、事件重放、导出不新增模型请求。主动连接测试、建索引、业务提问、摘要和提取可能计费；测试台主动按上限准备语义示例也会产生向量请求，查看／分流／导入已审校准本身不调用模型。

工程验收唯一累计账本318/328、余10；最新追加40使上限288→328，原历史不重置。169／182／184及确认保留；290为引用守卫中止且用户接受风险、禁止重发C02；292原uncertain行保留并附SDK完成及usage证据。剩10次不自动使用。原12题9/12 gate仍失败，不因工程发布改分。客户日用量、用户／全局队列和存储配额已实现，与工程总账分开；未知费用不估算为零。

历史快照（2026-10-23，不能作为当前余额）：工程验收沿用同一累计账本，用户再次追加40次后上限248；当前196/248，余52，原184条记录逐条保持。第二轮累计28次计账尝试，已知用量364,353 token；169、182、184三条不确定记录均已单独确认风险，用量仍未知，未改写为免费或成功。校准未整体通过，未跑12题整批验收。实施及边界见[语言专项第12节](docs/product/RAG语言组织与逻辑增强方案.md#12-续作校准与再次追加额度)。验收账本不是客户日用量／货币限额，用户及全局配额仍待实现。

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

GitHub Actions 的 `Regression checks` 按[回归检查策略](docs/回归检查策略.md)分级：开发分支push定向、已有PR时交由PR检查；PR／main根据实际改动选择定向或全量，手动运行可选范围。文档和普通页面不会默认触发全部Python测试；共享核心、迁移、权限、计费恢复及多模块集成才升级。使用Windows、Python3.11、Node24和锁定依赖，不使用模型密钥，不启动Docker或下载模型。

**本地每次全量必须先取得用户明确同意，定向检查可直接执行。** 开发约束见[AGENTS.md](AGENTS.md)，不可用裸pytest或拆分运行全部测试绕过。默认入口为定向，可明确指定本次改动；报告目录必须是`data/`内的新目录：

```powershell
.venv\Scripts\python.exe scripts/run_regression.py --profile targeted --changed-file app/web/knowledge-admin.js --output-dir ("data/checks-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
```

定向入口只运行计划列出的模块／网页检查及文档、diff，不执行全量质量关卡。全量入口保留依赖一致性、全部pytest、全部网页、RAG离线基线、事实引用和范围回放；本地须经同意后加`--profile full --local-full-approved`。两类报告明确区分，失败保留、不自动重试；GitHub报告保留14天，不上传运行库、原件或`.env`。分项排查可使用：

```powershell
.venv\Scripts\python.exe scripts/check_docs.py
.venv\Scripts\python.exe -m app.runtime --data-dir data
.venv\Scripts\python.exe -m pytest tests/test_regression_policy.py tests/test_regression_runner.py -q
node tests/web/trace_status.mjs
```

测试默认使用临时目录／模拟传输，不依赖真实密钥；仓库必备格式夹具缺失会失败，需要外部条件的跳过项会明确列出，不能算通过。`check_docs.py` 检查文档链接、冻结归档哈希、README版本及配置样例与源码的一致性，不证明每句业务描述或模型语义正确。在线评估、真实解析和故障演练按各自冻结范围在独立目录执行，不拿正式默认库做实验。

2026-10-23历史工程记录：单次1619项pytest通过（451.84秒、0失败／0跳过、2条既有依赖警告），10组网页及18项检查通过。RAG采用分阶段检索、PDF内联数字归位、显式单位核验和失败论断定位；结构归纳的分析术语与用户事实条件分别处理。原真实问题的17条论断、13条引用已零新增HTTP重验通过，语义与发布边界见[升级方案](docs/product/RAG链路升级方案.md)及接力说明；链路仍仅给状态标签着色。

2026-10-22首次CI实现的本地单次验证：1425项pytest通过（486.49秒、0跳过、2条既有依赖警告），9组网页及其余离线关卡通过，共17项检查通过。工作流actionlint校验通过。GitHub托管状态以[对应提交的实际运行](https://github.com/TongYule131/DocQA/actions/workflows/rag-quality.yml)为准，不能把本地结果当成远端运行结果。

RAG 历史三题同一关键引文实际输入覆盖8/17→17/17，核心内容和 DOCX／XLSX 回归经过开发复核，仍有性质声明待复核；这是规定资料的历史开发成绩。本轮统一链路单次全量1417通过（428.43秒、2条既有依赖警告）；随后完善并发别名、临时资料清理和文件列表，追加29项定向及8组网页检查通过，pip依赖检查通过，不累加成一次全量。真实隔离TXT链路新增3次HTTP，索引／查询／生成各一次，流式和最终引用保存成功。不是全部资料或多轮语义重新验收。首次失败、修复和正式切换见[阶段05](docs/阶段05-代码审查与加固.md)及接力说明。

原文精确匹配证明可追溯性，不能自动证明语义支持。OCR错字、阅读顺序、扫描表格、缺失公式／XLSX公式缓存仍需原件核对；空白不等于0，不重算公式。引用坐标为块区域，不承诺原始 PDF 逐字符高亮；结构推断、未装入范围及语义待复核状态保留在详情与导出。OCR实验尚未替换正式解析默认。独立新资料盲测、真实多轮收益、完整无缓存性能和容量验收继续按[提升路线](docs/整体能力提升方案.md)推进。

## 隔离界面预览

```powershell
$env:DOCQA_DATA_DIR = Join-Path (Get-Location) "data/preview-local"
.venv\Scripts\python.exe scripts/product_preview.py --data-dir $env:DOCQA_DATA_DIR --port 8997 --streaming-preview
```

仅允许本工作树下独立目录及回环地址。脚本运行实际解析worker（复杂格式仍需 Docling）和模拟索引／生成，页面有明确标识；`--streaming-preview` 演示分段输出，不证明真实模型质量。使用 `http://localhost:8997`；预览 Cookie 名为 `docqa_preview_8997`，正式实例保留 `docqa_session`，仅换端口不能隔离同主机Cookie。普通启动使用实际供应商配置。
