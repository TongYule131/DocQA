# 智能文档分析与知识问答系统

仅依据《智能文档分析与知识问答系统.md》建立的第一阶段基础框架。需求分析与后续实施顺序见 [项目实施方案](docs/项目实施方案.md)。未将目录内其他需求文件纳入本次设计。

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

## 当前可用

- 工作台：上传资料、文档列表、解析状态、查看按页分块、智能操作入口。
- 上传并持久化 PDF 和 UTF-8 TXT；校验空文件、扩展名及文件大小。
- PDF 文本层解析、TXT 解析、带页码与文档 ID 的重叠分块。
- 解析失败记录、重试、重复解析幂等、服务重启后保留资料。
- OCR、Embedding、向量存储和大模型协议接口，摘要、问答、提取响应结构。
- DeepSeek 模型适配器、本地配置加载、连接状态和手动连接测试。

## 尚未实现

OCR、Embedding、向量数据库、RAG 编排、摘要与信息提取均未接入。相关接口在文档未解析时返回 409，解析后返回 503，界面展示具体原因。当前分块不等于已建立向量索引。PDF 任一页没有文本层时暂停整份解析，包括空白页，避免扫描内容被静默遗漏；后续接入 OCR 时完善混合文档策略。

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

`requirements.lock.txt` 记录本次 Python 3.11 / Windows 环境验证通过的依赖版本。当前 29 项自动化测试通过，覆盖文档流程和模型模拟调用；依赖库的测试客户端存在两条弃用提示，不影响本次验证。
