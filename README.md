# 智能文档分析与知识问答系统

依据《智能文档分析与知识问答系统.md》建立，当前已完成 Docling 解析接入阶段。需求分析与实施顺序见 [项目实施方案](docs/项目实施方案.md)，本阶段任务与验收见 [Docling 接入工程任务书](docs/Docling接入工程任务书.md)、[全样本解析质量验收](docs/Docling全样本解析质量验收.md) 与 [Docling 接入实施报告](docs/Docling接入实施报告.md)。

后续业务 Prompt 优化与验收参考 [Prompt 设计与验收参考](docs/Prompt设计与验收参考.md)。

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
```

三个状态彼此独立：**任务状态**（本次解析是否排队/执行/失败/完成）、**内容质量**（结构是否可用、有何告警）、**索引状态**（哪个解析版本的向量可用）。新任务失败不会把已有内容标记为失效。

## 启动（三个进程）

需要 Python 3.11 或更新版本，以及本机可用的 Docling 解析服务（见下一节）。

```powershell
# 1) 安装依赖（首次）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 2) 启动 Docling 解析服务（独立 Docker 服务，必须先启动）
$dc = @('compose', '--env-file', 'deploy/docling/lab.env', '-f', 'deploy/docling/compose.yaml', '-f', 'deploy/docling/compose.ocr-gpu.yaml')
$env:PARSER_DEVICE = 'cuda'
docker @dc up -d --pull never --wait --wait-timeout 240

# 3) 启动 Web（终端 A）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4) 启动解析 worker（终端 B，必须单独运行，否则任务只会停在 queued）
.\.venv\Scripts\python.exe -m app.parse_worker
```

访问工作台 http://127.0.0.1:8000 ，交互接口文档 http://127.0.0.1:8000/docs 。第一次启动会自动创建 `data/docqa.db`、`data/uploads/`，并在需要时执行数据库迁移（迁移前自动生成一致性备份到 `data/db-backups/`）。

**启动顺序**：Docling → Web → worker。Web 与 worker 可以任意先后启动，但 worker 必须运行才能执行解析任务；两者使用同一个 `DOCQA_DATA_DIR`。

**停止**：在各自终端按 `Ctrl+C`。worker 收到 SIGINT 后会保留当前任务的恢复信息（上游 task_id 与租约）后退出，不会把正在执行的任务标记为失败。

**端口冲突**：解析服务默认 `127.0.0.1:5001`，Web 默认 `8000`。解析服务端口由 `deploy/docling/lab.env` 与 `compose.yaml` 决定；如需更换，同时修改 `DOCQA_DOCLING_BASE_URL`。Web 端口冲突时用 `--port` 指定其他端口。

**任务恢复**：已保存上游 task_id 的任务在租约过期后恢复查询与领取，**不会重新上传**。提交阶段中断且没有上游 ID 时进入 `needs_attention`，明确重试可能产生重复转换。点击“停止等待”会暂停任务；恢复已保存 ID 的暂停任务时继续等待原任务。索引候选构建中断后，最长等待 300 秒租约过期即可重建，原可用索引不受影响。

**升级现有项目**：先停止旧 Web 与 worker，再启动新版本。schema v2 自动备份并保留现有版本、分块和向量；新解析版本与候选索引独立保存。B 解析成功但尚未建索引时，预览 B，检索继续使用 A，页面会提示。不要让旧后端进程与新结构同时写同一数据库。回退代码时须同时恢复匹配的迁移前备份，并先保留、移开现库的 WAL/SHM 文件。

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

RAG 答案生成、摘要、信息提取、网站抓取、浏览器插件、多用户系统均未接入，相关接口明确返回 503，`/api/capabilities` 中对应项为 `false`。旧 `.doc`/`.xls`、图片、PPTX 等格式未验收，不能因 Docling 支持某格式便直接对外承诺。当前为本地单机部署，未引入 Redis/Celery 或专用向量数据库。

## 目录

```text
app/
  main.py                 应用工厂、API、页面入口
  config.py               环境配置与校验
  schemas.py              数据契约（任务/版本/块/来源/告警/索引）
  migrations.py           带版本号的数据库迁移与一致性备份
  repository.py           SQLite 持久化（任务领取、版本发布、索引切换）
  file_detect.py          上传格式识别（内容判定与安全限制）
  docling_client.py       Docling 客户端（提交/查询/领取、错误分类、期限）
  parse_worker.py         持久化解析 worker（python -m app.parse_worker）
  document_normalizer.py  Docling 结果规范化、来源映射与质量告警
  chunking.py             结构化分块（正文/表格/目录/公式占位）
  parsing.py              本地 TXT 解析与旧分块兼容
  vector_index.py         版本化向量索引与检索
  embedding.py            Embedding 网关调用、分批与向量校验
  deepseek.py             DeepSeek API 调用与安全错误转换
  providers.py            OCR / Embedding / 向量库 / 大模型协议
  web/                    工作台页面
scripts/
  acceptance_e2e.py       真实链路验收（上传→异步解析→预览→来源）
  browser_acceptance.mjs  真实浏览器验收（T21/T22，Chrome + CDP）
  validate_docling.py     独立解析服务验证（仅本机）
deploy/docling/           解析服务 GPU 部署与验证
docs/                     需求、验收与实施报告
tests/                    离线自动测试（不依赖 Docker、密钥或现有数据）
```

## 验证

```powershell
# 全部离线自动测试（临时目录、临时 SQLite、可注入客户端与模拟 embedding）
.\.venv\Scripts\python.exe -m pytest -q

# 真实链路验收（需要已启动 Docling、Web 与 worker）
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe scripts/acceptance_e2e.py --api http://127.0.0.1:8010 --timeout 2400

# 真实浏览器验收 T21/T22（需要 Chrome 与正在运行的 Web）
node scripts/browser_acceptance.mjs --api http://127.0.0.1:8010
```

自动测试默认离线，覆盖旧库迁移、任务幂等与租约、上游错误分类、结果幂等发布、空白页、表格去重、多来源、XLSX 坐标与公式缓存、长表分块、索引候选发布与失败保留、格式伪装与越权访问等场景。`tests/test_real_fixtures.py` 使用真实 Docling 结果夹具（位于被 Git 忽略的 `data/` 下），夹具缺失时会跳过并提示，不会把跳过当成通过。

## 数据库迁移与恢复

启动时按版本号执行迁移（当前 `SCHEMA_VERSION=1`），迁移前用 SQLite 在线备份 API 生成一致性副本到 `data/db-backups/`（包含 WAL 中已提交数据）。迁移失败会**停止启动**并保留原库与备份，不会静默新建空数据库。

恢复步骤：

1. 停止 Web 与 worker，避免继续写入；
2. 备份当前 `data/docqa.db`（连同 `-wal`、`-shm`）；
3. 把 `data/db-backups/` 中最近的备份复制回 `data/docqa.db`；
4. 如需回到旧代码：`git stash` 或切回上一版本代码后重新启动，旧表结构仍被保留。
