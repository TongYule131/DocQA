# DocQA 智能文档分析与知识问答系统

面向本地和团队试用的文档工作台：上传资料、版本化解析、知识库问答、摘要与信息提取，保存任务、结果及原文引用。回答与解析均有已知质量边界。

当前发布、CI、调用预算和未完成验收只在[接力说明](docs/稳定试用版接力说明.md)维护；[文档索引](docs/README.md)提供使用、开发和历史入口。不要将旧任务书的目标或某次ready快照当成当前验收结论。

## 主要能力

- 支持PDF、DOCX、XLSX、UTF-8 TXT和Markdown。原件、解析版本、来源与质量告警保留。
- 管理员准备知识库资料、解析／分块、索引及生效范围；客户在获授权资料中提问，可筛选单份文档。
- 问答支持真实流式、逐事实引用、任务历史及明确取消／重试。局部草稿通过不代表整篇回答成功。
- 意图决定回应、授权检索、工具或澄清；概览、比较、因果等任务策略影响证据选择。可选语义候选默认关闭，测试分数不是正确概率。
- 摘要与信息提取基于固定解析版本，不依赖向量索引；长文按计划处理并核对覆盖，支持Markdown／JSON导出。
- 提供账号与库权限、临时资料、反馈审核、配额、用量和运行诊断，以及Windows登录用户驻留、备份和隔离恢复。

## 安装与启动

需要Python3.11+。复杂格式的新解析依赖已有Docling／RapidOCR服务；TXT和Markdown在本地解析。查看旧结果不重新调用模型。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.venv\Scripts\python.exe -m pip install --no-deps -e .
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

.venv\Scripts\python.exe -X utf8 -m app.runtime --data-dir D:\python\DocQA\data
powershell -NoProfile -File scripts\start_local.ps1 -DataDir D:\python\DocQA\data -Port 8000
powershell -NoProfile -File scripts\status_local.ps1 -DataDir D:\python\DocQA\data -AsJson
powershell -NoProfile -File scripts\stop_local.ps1 -DataDir D:\python\DocQA\data
```

以上数据目录是本项目部署示例，其他机器应明确替换。保留已有`.env`；供应商密钥只在服务器配置。已有旧库升级须先备份、演练，再显式使用`-AllowDatabaseUpgrade`；不能跳过结构检查。登录驻留和完整备份命令见[工程与恢复](docs/阶段03-稳定版工程与恢复.md)。

## 使用入口

| 入口 | 用途 |
| --- | --- |
| `/app` | 客户会话、资料选择、引用、历史、临时上传与反馈 |
| `/admin`、`/knowledge` | 管理、资料准备、解析／索引、生效与授权 |
| `/admin/intents`、`/admin/traces` | 意图配置／测试台与实际执行链路 |
| `/tools`、`/admin/documents` | 文档工程、摘要和信息提取 |
| `/docs` | 实际请求类型和OpenAPI，团队模式下限管理员 |

普通工程上传只保存原件；统一知识库入库流程可自动解析和索引，索引可能产生向量请求。手工审查模式须明确生效，不能把上传当成已发布。新分块只作用于新解析版本，旧索引／引用不自动改写。

## 实现版本

此表由文档检查器与源码字面量核对；发布提交和验收成绩见接力说明。

| 项目 | 版本 |
| --- | --- |
| 项目包 | `0.2.0` |
| 数据库 schema | `16` |
| 任务式问答 Prompt | `rag-task-v62` |
| 检索策略 | `hybrid-context-v12` |
| 事实引用协议 | `fact-context-parts-v8` |
| 意图路由 | `intent-execution-v4` |
| 流式协议 | `validated-fact-stream-v2` |
| 知识库任务协议 | `knowledge-task-v2` |

## 配置与费用

所有参数及默认值见[.env.example](.env.example)，由`app/config.py`读取；前端不能覆盖模型、网关或thinking设置。默认分块800字符、重叠120；任务问答最多64候选／32装入，资料24000／总输入32000字符。字符预算不是token窗口或覆盖保证。

每次供应商请求先记账；失败和不确定调用不自动重发。任务的请求上限与工程验收总账、用户／全局日配额是不同口径。纯交互、查看、刷新、本地分析预算预估、事件重放和导出不调用模型；主动连接测试、索引、业务问答及分析可能计费。复杂问答的选材规划与正文写作分别计请求；可选查询理解、精排、问题摘录有独立开关，当前状态见接力说明。

## 验证与质量边界

遵守[回归检查策略](docs/回归检查策略.md)：本地可直接做关联定向检查，每次本地全量须另获用户明确同意。不能通过拆批跑遍全部测试、环境变量或包装脚本绕过。GitHub CI按风险分级，阶段完整候选可手动选full，普通CI不注入模型密钥。

```powershell
.venv\Scripts\python.exe scripts/check_docs.py
.venv\Scripts\python.exe scripts/run_regression.py --profile targeted --changed-file app/web/knowledge-admin.js --output-dir data/checks-page-new
```

报告目录须是新的`data/`子目录。工程通过和精确引用不能代替语义正确；OCR、数学表示、缺公式缓存、关系推断及新资料泛化仍有边界。状态、限制和真实语义证据分别见[接力说明](docs/稳定试用版接力说明.md)、[语义验收](docs/阶段04-真实语义验收与提取改进.md)和[任务书](docs/下一阶段任务书-S2-S5闭环补齐与试点验收.md)。

## 开发与维护

核心链路为`main.py`装配，`product_access.py`控制范围，知识库／文档worker执行持久任务，`rag_*`处理证据与答案，`migrations.py`管理结构。业务数据在显式数据目录内，测试和演练必须隔离；密钥、原件、数据库、模型原始响应及截图不提交Git。

开始续作阅读[续作指引](docs/新对话续作指引.md)；领域实现见[文档索引](docs/README.md)。原始需求、设计方案及历史成绩保留为历史标准，不混入当前功能列表。整合前README见[逐字快照](docs/archive/2026-10-26-consolidation/README.md)。
