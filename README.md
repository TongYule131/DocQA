# 智能文档分析与知识问答系统

仅依据《智能文档分析与知识问答系统.md》建立的第一阶段基础框架。需求分析与后续实施顺序见 [项目实施方案](docs/项目实施方案.md)。未将目录内其他需求文件纳入本次设计。

后续业务 Prompt 优化与验收参考用户提供的 [Prompt 设计与验收参考](docs/Prompt设计与验收参考.md)，包含原始示例、体检清单及 DocQA 适配原则。

## 启动

需要 Python 3.11 或更新版本。在项目根目录执行（PowerShell）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

访问工作台 http://127.0.0.1:8000 和交互接口文档 http://127.0.0.1:8000/docs 。第一次启动自动创建 `data/docqa.db` 和 `data/uploads/`。

可选环境变量：`DOCQA_DATA_DIR`（默认当前目录下 `data`），`DOCQA_MAX_UPLOAD_MB`（默认 20）。应用启动时读取当前目录的 `.env`，进程环境变量优先于文件配置。修改后需要重启服务。

## DeepSeek API 配置

已使用 OpenAI Python SDK 接入 DeepSeek 兼容接口。默认 `deepseek-flash`、`reasoning_effort=high`、`thinking.type=enabled`，依据 [DeepSeek 官方文档](https://api-docs.deepseek.com/guides/thinking_mode/)。

1. 若本地没有 `.env`，复制 `.env.example` 为 `.env`；已有文件请直接编辑，不要覆盖密钥。
2. 填写 `DEEPSEEK_API_KEY=你的真实密钥`。基础地址为纯文本 `https://api.deepseek.com`，不要粘贴 Markdown 链接或在下划线前添加反斜杠。
3. 停止旧服务，按上方命令重新启动；刷新工作台，点击“测试连接”。

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_THINKING=enabled
DEEPSEEK_REASONING_EFFORT=high
DEEPSEEK_TIMEOUT_SECONDS=120
DEEPSEEK_MAX_TOKENS=8192
```

思考模式支持 `enabled` / `disabled`，思考强度支持 `low` / `high` / `max`。超时单位为秒，输出预算按 token 计；若答案被截断，接口会报错，不将其当作完整结果。

- `GET /api/model/status`：读取配置状态，不调用模型，不返回密钥。“已配置”不代表密钥已验证。
- `POST /api/model/test`：发送固定短消息，返回最终正文，不发送上传文档，不返回思考内容；此操作调用真实 API，可能产生少量费用。
- 未配置密钥或上游限流返回 503，超时返回 504，其他上游调用失败返回安全的 502 提示；不自动重试。

`.env` 已被 Git 忽略。自动化测试使用模拟响应，不证明真实账户权限、余额或网络可用。模型连接成功后，文档问答仍需接入 Embedding 和 RAG。

## Embedding 向量化与检索

按用户提供的示例接入 `https://tokendance.space/gateway/v1` 网关，模型为 `qwen3.7-text-embedding`。这是用户指定的网关地址，不是阿里云官方直连地址；请使用此网关签发的密钥。现有 DeepSeek 密钥不会被自动复用或发送到该网关。

在本地 `.env` 中填写以下配置后重启服务：

```dotenv
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=https://tokendance.space/gateway/v1
EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_TIMEOUT_SECONDS=60
EMBEDDING_BATCH_SIZE=8
```

使用流程：**测试向量连接 → 上传并解析文档 → 建立向量索引 → 检索当前文档原文**。已有解析文档可直接建索引。连接测试只发送固定短文本；建立索引会将所选文档分块发送到网关；检索会发送查询文本。这些 API 操作可能计费，页面加载不会自动调用。

| 接口 | 用途 |
| --- | --- |
| GET /api/embedding/status | 配置状态，不返回密钥，不验证账户连通性 |
| POST /api/embedding/test | 固定文本测试，返回实际维度与归一化向量的前五项 |
| GET /api/documents/{id}/index | 独立的索引状态 |
| POST /api/documents/{id}/index | 建立索引；同模型同原文已完成时直接复用 |
| POST /api/documents/{id}/index?rebuild=true | 主动重建，会再次调用网关 |
| POST /api/documents/{id}/search | 接收 `query` 和 `top_k`，返回相似度排序后的原文与页码 |

向量保存在 SQLite 的 `embedding_vectors` 表中，索引元数据保存在 `embedding_indexes` 表中，首次启动自动新增两张表，保留原有文档数据。没有强制指定向量维度，以网关实际输出为准并校验一致性。当前采用单位向量点积计算余弦相似度，逐条扫描当前文档，适合单机小规模使用，尚未接入专用向量数据库或近似最近邻索引。

索引状态包括未创建、处理中、已完成、失败和配置/内容过期；更换模型或网关后需重建。所有批次成功后才整体写入索引，失败不暴露半成品；失败后可重试。沿用单进程同步任务模式，重启会将处理中任务标记为失败。向量接口不自动重试；批次大小可根据网关限制调小。

检索结果是候选原文，分值不是可信度或回答正确概率；当前没有相关性阈值，即使问题与文档无关，也可能返回相对最近的片段。下一步 RAG 应加入依据不足判断与引用校验，再调用 DeepSeek 生成答案。

## 当前可用

- 工作台：上传资料、文档列表、解析状态、查看按页分块、智能操作入口。
- 上传并持久化 PDF 和 UTF-8 TXT；校验空文件、扩展名及文件大小。
- PDF 文本层解析、TXT 解析、带页码与文档 ID 的重叠分块。
- 解析失败记录、重试、重复解析幂等、服务重启后保留资料。
- OCR、Embedding、向量存储和大模型协议接口，摘要、问答、提取响应结构。
- DeepSeek 模型适配器、本地配置加载、连接状态和手动连接测试。
- Embedding 网关适配器、向量持久化、索引状态、原文相似度检索。

## 尚未实现

OCR、专用向量数据库、RAG 答案生成、摘要与信息提取尚未接入。摘要、提取和问答接口在文档未解析时返回 409，解析后返回 503。解析完成不等于已建立向量索引，需要主动建索引。PDF 任一页没有文本层时暂停整份解析，包括空白页，避免扫描内容被静默遗漏；后续接入 OCR 时完善混合文档策略。

这是本地单进程开发框架。解析同步执行，暂未加入任务队列；不要用多 worker 启动，后续后台任务阶段需替换启动时的中断任务恢复机制。账号、权限、多租户和部署运维未在原文指定，本次未实现。

## 目录

```text
app/
  main.py          应用工厂、API、页面入口
  config.py        环境配置
  schemas.py       文档、分块、回答及引用数据契约
  repository.py    SQLite 持久化与解析状态
  parsing.py       文档解析与分块
  providers.py     OCR / Embedding / 向量库 / 大模型协议
  deepseek.py      DeepSeek API 调用与安全错误转换
  embedding.py     Embedding 网关调用、分批与向量校验
  vector_index.py  SQLite 向量索引状态与单文档检索
  web/             HTML、CSS、JavaScript 工作台
docs/
  项目实施方案.md   需求映射、架构、阶段及验收要求
tests/
  test_api.py      API 流程、数据持久化及失败路径测试
```

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

手动操作：上传 `examples/demo.txt` → 解析 → 查看分块；点击摘要或提问应收到“尚未接入”的明确提示。上传扫描 PDF 应显示需要 OCR。测试不调用外部模型或服务。

`requirements.lock.txt` 记录本次 Python 3.11 / Windows 环境验证通过的依赖版本。自动化测试覆盖文档流程、模型与 Embedding 模拟调用、索引持久化和检索隔离；不会发送真实文档到外网。依赖库的测试客户端存在两条弃用提示，不影响本次验证。
