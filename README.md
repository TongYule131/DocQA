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

可选环境变量：`DOCQA_DATA_DIR`（默认当前目录下 `data`），`DOCQA_MAX_UPLOAD_MB`（默认 20）。`.env.example` 仅用于说明，不会自动读取；PowerShell 中使用 `$env:DOCQA_MAX_UPLOAD_MB="10"` 设置。

## 当前可用

- 工作台：上传资料、文档列表、解析状态、查看按页分块、智能操作入口。
- 上传并持久化 PDF 和 UTF-8 TXT；校验空文件、扩展名及文件大小。
- PDF 文本层解析、TXT 解析、带页码与文档 ID 的重叠分块。
- 解析失败记录、重试、重复解析幂等、服务重启后保留资料。
- OCR、Embedding、向量存储和大模型协议接口，摘要、问答、提取响应结构。

## 尚未实现

OCR、Embedding、向量数据库、RAG 编排、模型适配器、摘要与信息提取均未接入。相关接口在文档未解析时返回 409，解析后返回 503，界面展示具体原因。当前分块不等于已建立向量索引。PDF 任一页没有文本层时暂停整份解析，包括空白页，避免扫描内容被静默遗漏；后续接入 OCR 时完善混合文档策略。

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

`requirements.lock.txt` 记录本次 Python 3.11 / Windows 环境验证通过的依赖版本。当前 7 项自动化测试通过；依赖库的测试客户端存在两条弃用提示，不影响本次验证。
