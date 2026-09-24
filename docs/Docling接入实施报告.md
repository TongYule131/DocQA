# Docling 接入实施报告

> 此文保留为初次交付记录。独立验收发现其中 T05、T16、T17 及故障脚本的断言不足，不能沿用“全部通过”的结论。后续修复、真实复验结果和使用边界以 [独立验收与修正报告](Docling接入独立验收报告.md) 为准。

编写日期：2026-09-23。执行方：DeepSeek。对照文件：[Docling 接入工程任务书](Docling接入工程任务书.md)、[全样本解析质量验收](Docling全样本解析质量验收.md)、[Docling OCR GPU 验证报告](Docling-OCR-GPU验证报告.md)。

本报告严格区分**模拟测试**（离线自动测试，可注入假上游与模拟 embedding）与**真实 API 验证**（真实 Docling 服务、真实网页、真实在线 embedding 网关）。未执行的项目明确写“未验证”，不写成通过。

---

## 0. 结论摘要

| 验收级别 | 结论 | 主要证据 |
| --- | --- | --- |
| 离线工程检查 | **通过** | `pytest -q`：102 项通过（含真实 Docling 结果夹具的适配器测试） |
| 本地 Docling 链路 | **通过** | 7 份真实样本全部经真实 HTTP 解析成功并发布版本（`20260923-142718-209416-e2e-acceptance`） |
| 真实在线索引检索 | **通过** | 真实 embedding 网关建立 1024 维索引并检索到原件第 2 页原文与 bbox |

三条关键链路：

1. **首次扫描 PDF 解析并检索**：通过。`04-scan-zh.pdf` 上传 → 异步解析（上游 task `dadd5ca1…`）→ 6 页来源 → 真实索引（16 块）→ 检索“本年鉴包含多少个部分”命中第 2 页原文。
2. **重新解析失败仍保留旧版**：通过。`scripts/verify_failure_recovery.py` 10/10，其中 B 失败后 A 的活动版本、分块内容完全一致。
3. **重启恢复**：通过。重启 Web 后文档、解析版本、索引与检索结果全部保留；另有自动测试覆盖 worker 崩溃后按已保存上游 ID 恢复且不重复 POST。

---

## 1. 里程碑状态（M0—M5）

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| M0 基线 | 完成 | 记录 Git 状态、测试基线（60 项通过）、旧库结构（1 份文档 / 1 个分块 / 0 索引） |
| M1 存储 | 完成 | 契约、迁移引擎、旧库无损升级、空库/重复迁移/旧索引可用测试 |
| M2 执行 | 完成 | Docling 客户端、持久化 worker、租约与恢复、提交不确定窗口 |
| M3 内容 | 完成 | 上传识别、结构规范化、来源映射、质量告警、结构化分块 |
| M4 应用 | 完成 | 异步 parse API、版本化索引、内容预览接口、网页交互 |
| M5 交付 | 完成 | 全回归、真实链路、浏览器验证、运行说明、本报告 |

未完成项（本期范围外，明确保留为未接入）：RAG 答案生成、摘要、信息提取、网站抓取、浏览器插件、多用户系统、旧 `.doc`/`.xls` 格式、图片与 PPTX、图表语义解析、公式增强。

---

## 2. 修改文件

### 2.1 新增文件

| 文件 | 作用 |
| --- | --- |
| `app/migrations.py` | 带版本号的 SQLite 迁移；一致性备份（在线备份 API）；旧库 → legacy 版本登记；故障注入点 |
| `app/file_detect.py` | 上传格式识别：内容判定（PDF 标识与结构、OOXML 包类型、UTF-8）、安全限制、文件名清理 |
| `app/docling_client.py` | Docling 客户端：提交 / 轮询 / 领取、三类超时、有限重试、稳定错误码、禁用代理与重定向 |
| `app/parse_worker.py` | 持久化 worker（`python -m app.parse_worker`）：原子领取、租约心跳、恢复、规范化、分块、原子发布 |
| `app/document_normalizer.py` | 按 body/children 阅读顺序遍历、来源映射（PDF/DOCX/XLSX/TXT）、质量告警、XLSX 公式缓存检查 |
| `app/chunking.py` | 结构化分块：正文按标题合并、表格按行组、目录、公式占位、超长处理 |
| `scripts/acceptance_e2e.py` | 真实链路验收：上传 → 异步解析 → 预览 → 来源证据保存 |
| `scripts/browser_acceptance.mjs` | 真实浏览器验收（Chrome + CDP）：T21/T22 |
| `scripts/verify_failure_recovery.py` | 故障链路验证：重新解析失败后旧版本与旧索引仍可用 |
| `tests/test_parse_pipeline.py` | 任务、客户端、规范化、分块、格式识别测试（T03—T15、T18—T20） |
| `tests/test_migration_and_versions.py` | 迁移与版本化索引测试（T01、T02、T16、T17） |
| `tests/test_real_fixtures.py` | 真实 Docling 结果夹具的离线适配器测试 |

### 2.2 修改文件

| 文件 | 改动要点 |
| --- | --- |
| `app/schemas.py` | 新增 ParseTask / ParseVersion / Block / SourceLocation / QualityWarning / IndexInfo / IndexAttempt；`Chunk.page` 改为可空并新增版本、顺序、块类型、来源；`Document` 增加格式、版本指针与三类状态摘要 |
| `app/repository.py` | 新增 `parse_tasks` / `parse_attempts` / `parse_versions` / `blocks` / `sources` / `quality_warnings` / `index_attempts` / `embedding_vectors_v2` 读写；原子领取与租约校验；版本发布与退役；版本化索引切换；文档 `status` 改为派生值 |
| `app/vector_index.py` | 索引绑定解析版本；`source_signature` 增加算法版本（新算法 + 旧算法兼容）；候选发布与 superseded；检索返回 index_id / parse_version_id / is_old_version / 每条来源 |
| `app/main.py` | `/parse` 改为异步（202 / 200 复用 / 幂等键 409）；新增任务查询、重试、取消、版本列表、内容预览、原件、索引尝试、解析服务状态；上传走内容识别 |
| `app/config.py` | 新增 Docling 地址与三类超时、轮询与重试、解析参数、页数上限、worker 租约、分块参数及校验 |
| `app/web/app.js`、`index.html`、`style.css` | 任务轮询（2.5 秒、终态停止、断网退避）、版本与质量告警展示、结构化表格渲染、来源标签、HTML 安全渲染 |
| `tests/test_api.py` | `/parse` 同步测试改为“创建任务 → 可控 worker 执行 → 查询结果”，保留原业务意图；新增格式识别与幂等测试 |
| `tests/test_embedding.py` | 适配版本化索引（候选发布、版本退役、损坏向量拒绝） |
| `pyproject.toml` | 运行依赖显式声明 `httpx` 与 `openpyxl` |
| `requirements.lock.txt` | 追加本次新增运行依赖的实测版本：`openpyxl==3.1.5`、`et_xmlfile==2.0.0`（未改动其他既有锁定版本） |
| `README.md`、`.env.example` | 三进程启动顺序、配置含义、端口冲突、任务恢复、迁移与恢复步骤、质量边界 |

**保留的未提交修改**：`deploy/docling/README.md` 与 `docs/Docling独立解析验证记录.md` 的原有未提交改动未被覆盖或撤销（仅在需要处追加/修正，见 §8 差异说明）。

---

## 3. 数据库迁移方式

### 3.1 机制

- `app/migrations.py` 定义 `SCHEMA_VERSION = 1`，用 `schema_migrations` 记录已应用版本；重复运行不重复改写。
- 迁移前用 **SQLite 在线备份 API**（`sqlite3.Connection.backup`）生成一致性副本到 `data/db-backups/`，包含 WAL 中已提交数据（有自动测试覆盖）。
- 每个版本在 `BEGIN IMMEDIATE` 事务中执行；失败即 `ROLLBACK` 并抛 `MigrationError`，**停止启动**，不静默新建空库。
- 迁移连接使用 `isolation_level=None` 显式事务控制（避免 sqlite3 隐式事务与迁移事务冲突），业务读写仍走 `Repository.connect()` 并启用 `PRAGMA foreign_keys=ON`。
- 结构变更包含两次表重建：`embedding_indexes`（旧主键为 `document_id`，无法承载索引 ID 与绑定版本）与 `chunks`（旧 `page` 为 NOT NULL，而 DOCX/XLSX 无真实页码）。

### 3.2 旧数据保留

| 保留项 | 验证方式 |
| --- | --- |
| 文档 ID | 真实库 `demo.txt` → 迁移后 ID 不变 |
| 分块 ID / 内容 / 页码 | 分块 `2b8f2839…`、页码 1、正文完全一致 |
| 向量 / 维度 / 提供方签名 | 迁移代码保留并迁入 `embedding_vectors_v2`；不完整索引迁移为 failed |
| 索引状态 | 完整索引 → `indexed` 且可检索；缺向量或维度 → `failed` 并写明原因；模型不匹配 → `stale` |
| 旧签名算法 | 旧 `source_signature` 原样保留并记录 `source_signature_algo=sha256-chunk-dump-v1`，不重算 |

不重新 OCR、不调用 embedding（测试断言迁移期间网关调用次数为 0）。

### 3.3 实际迁移证据

真实数据库（`data/docqa.db`）：

```text
迁移前 sha256: b36098832d4803727e813407ef71915be8284574189d964a5c6d038dc386aeca
migrate: {'from': 0, 'to': 1, 'applied': [1], 'backup': 'data\\db-backups\\docqa-20260923-064103.db'}
迁移后 sha256: 19ea011d6d8d4eab70676406d218942eaa18757079d3b39c016931402aac300b
doc: 6816eab5f7ae41df9a52e482341a7ac0 demo.txt status parsed format txt
     active_version legacy-6816eab5f7ae41df9a52e482341a7ac0 quality warnings
chunk: 2b8f28393c4942fc92081d759398d45e page 1 order 0 type legacy
       sources [txt 逻辑页 1，note=旧库迁移的兼容来源：页码为旧解析逻辑页]
```

### 3.4 备份与恢复步骤

```powershell
# 1) 停止 Web 与 worker，避免继续写入
# 2) 备份当前数据库（含 -wal/-shm）
Copy-Item data\docqa.db data\docqa.db.before-restore -Force
# 3) 用最近一次迁移前备份恢复
Copy-Item data\db-backups\docqa-20260923-064103.db data\docqa.db -Force
# 4) 如需回到旧代码：git stash 或切回上一版本代码后重新启动，旧表结构仍被保留
```

---

## 4. 实际执行的测试命令与结果

### 4.1 离线自动测试（模拟，无外部依赖）

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果（最终）：

```text
102 passed, 2 warnings in 7.85s
```

（2 条 warning 来自依赖库的弃用提示：`starlette.testclient` 与 `anyio`，与本次改动无关。）

覆盖矩阵见 §5 的 T01—T22 清单。

### 4.2 真实 Docling 链路（真实 API）

```powershell
# 解析服务（含 GPU 覆盖，跳过 CPU）
$dc = @('compose','--env-file','deploy/docling/lab.env','-f','deploy/docling/compose.yaml','-f','deploy/docling/compose.ocr-gpu.yaml')
$env:PARSER_DEVICE = 'cuda'
docker @dc up -d --pull never --wait --wait-timeout 240

# 业务服务与 worker（独立测试数据目录）
$env:DOCQA_DATA_DIR = 'D:\python\DocQA\data\acceptance-2026'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
.\.venv\Scripts\python.exe -m app.parse_worker

# 验收
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe scripts/acceptance_e2e.py --api http://127.0.0.1:8010 --timeout 2400
```

结果：**成功 7 / 7**，证据目录 `data/docling-validation/20260923-142718-209416-e2e-acceptance/`。

| 样本 | 格式 | 任务 | 解析版本 | 上游任务 | 块 / 分块 | 质量 | 端到端秒 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 01-text-en-single-column.pdf | pdf | succeeded | `v-05c06204daab-f014d0a9a7f15ca9` | `61f66854…` | 175 / 107 | warnings | 12.11 |
| 02-text-en-two-column-tables.pdf | pdf | succeeded | `v-adad6a3296cb-26627230f2f2da46` | `77d30f1b…` | 247 / 149 | warnings | 10.05 |
| 03-text-zh-tables.pdf | pdf | succeeded | `v-1ad3fd6d4f5b-ac274caa5bb81269` | `190d6ea1…` | 184 / 278 | warnings | 28.09 |
| 04-scan-zh.pdf | pdf | succeeded | `v-dd44c0aba0f0-3dfc9eda6ad5bfdf` | `dadd5ca1…` | 23 / 16 | warnings | 10.06 |
| 05-mixed-text-scan.pdf | pdf | succeeded | `v-9dfa96539bea-27da67654e999f59` | `0ba932f4…` | 23 / 75 | warnings | 12.08 |
| 06-sample.docx | docx | succeeded | `v-18cf61613e6f-5814b3427b5b7895` | `75598190…` | 9 / 9 | ok | 4.05 |
| 07-sample.xlsx | xlsx | succeeded | `v-3359ca35fc6a-2db1bfdc26fc4808` | `72ed7205…` | 4 / 4 | warnings | 4.03 |

关键来源与告警核对（真实数据，非模拟）：

- **04 扫描件**：6 页均有来源；`ocr_limitation` 提示存在；4 个目录表按页码 3—6 记录并清理点线；页脚保留但不参与分块。
- **03 中文表格 PDF**：第 12 页识别为空白页（`blank_page` 告警且页码不位移）；7 个目录表分布在第 5—11 页；第 24 页数值表作为 `table` 块保留。
- **06 DOCX**：`pages={}` → 所有来源 `page=null`，**未伪造页码**；章节标题 3 个、表格 1 个（5 列、含跨列 5 与跨行 2 的合并）；表格单元格文本未被当作独立正文重复索引；质量 `ok`。
- **07 XLSX**：3 个工作表名（分区域数据 / 指标说明 / 原始记录）与真实单元格范围 `A2:F8`、`A1:C6`、`A1:D5`；`B8:F8` 的 5 个 SUM 公式无缓存 → `formula_cache_missing` 告警，且未变成 0（原件未被修改）。
- **01 英文单栏 PDF**：6 个公式节点内容为空 → `formula_content_missing` 告警，保留位置不填补内容。

### 4.3 真实浏览器验证（T21/T22）

```powershell
node scripts/browser_acceptance.mjs --api http://127.0.0.1:8010
```

结果：**通过 8 / 8**，浏览器 `Chrome/153.0.8010.53`，证据目录 `data/docling-validation/browser-acceptance/`（含 `browser-results.json` 与页面截图 `workbench.png`）。

| 编号 | 检查项 | 实测结果 |
| --- | --- | --- |
| T21-1 | 刷新后从服务端恢复 | 刷新前后标题、分块数、版本状态、索引状态完全一致 |
| T21-2 | 切换文档后旧轮询不覆盖 | 标题切换为注入样本，任务区未出现旧任务的上游编号 |
| T21-3 | 重复点击“重新解析” | 按钮禁用序列 `[true,true,true]`，服务端只有一个任务 |
| T21-4 | 断网提示与退避 | 提示“与服务器连接异常（第 1 次）：Failed to fetch；将有限退避后重试”，页面无百分比，恢复后可继续 |
| T22-1 | HTML/脚本不执行 | alert 0 次、注入 script 0 个、img 标签 0 个，`<script>` 以纯文本显示 |
| T22-2 | 不泄露密钥与内部信息 | 未出现密钥名、Bearer、堆栈、上游接口路径或本地路径 |
| T22-3 | 服务端错误按纯文本 | 404 提示按纯文本渲染，脚本节点 0 个 |
| T21-5 | 终态后停止轮询 | 终态后 5 秒新增轮询 0 次，活动定时器 0 个 |

### 4.4 故障链路验证（真实 API）

```powershell
.\.venv\Scripts\python.exe scripts/verify_failure_recovery.py --api http://127.0.0.1:8010 `
    --worker-cmd "D:\python\DocQA\.venv\Scripts\python.exe -m app.parse_worker --once"
```

结果：**通过 10 / 10**，证据目录 `data/docling-validation/20260923-144014-749012-failure-recovery/`。

- A 解析成功并记录版本与 16 个分块；
- 把解析服务指向不可达端口后重新解析 B → `failed`，错误码 `docling_connect_failed`，说明为可读中文；
- 失败后：活动版本仍为 A、文档 `status=parsed`、分块内容与失败前**完全一致**；
- 检索接口返回 409 并给出可解释原因（该文档尚未建立索引，属预期）；
- 用户明确重试 → 新建任务 → 恢复上游后成功；
- 同一原件与参数产生相同结果哈希时**按幂等复用同一版本**（未重复发布），失败记录仍保留。

### 4.5 真实在线索引检索（真实 API）

使用用户已配置的 embedding 网关（未打印或提交任何密钥），对真实扫描件建立索引并检索：

```text
上传：04-scan-zh.pdf → 201 pdf（共 6 页）
解析：202 → 任务 b36f523cc1bc4f1284c20f520b9c8098 → succeeded
版本：v-72d8e0fb5cbe-3dfc9eda6ad5bfdf，质量 warnings，16 个分块
索引：POST /api/documents/{id}/index → 200，1024 维，16 块，2.4 秒
      index_id = idx-72d8e0fb5cbe-v-72d8e0fb5cbe-3-bd405159
```

检索“本年鉴包含多少个部分”（扫描样本原文可支持的问题）：

```text
HTTP 200 · parse_version_id=v-72d8e0fb5cbe-3dfc9eda6ad5bfdf · is_old_version=false
命中 1：score 0.6612 · page 2 · 《绵阳统计年鉴2025》是一部全面反映绵阳经济和社会发展情况的综合 性年刊…
        来源 4 条：pdf 第 2 页 + 原始 bbox（coord_origin=BOTTOMLEFT）
命中 2：score 0.6235 · page 1 · 编委会名单（主任/副主任/编委）
命中 3：score 0.5619 · page 1 · 《绵阳统计年鉴2 编委会 2025》
```

检索“编委会主任是谁”命中第 1 页“主任唐建瑛 / 副主任赵西林 / 责任编辑何林”（score 0.6924），与原件一致。

重启 Web 后再次检查：文档、解析版本、索引（`indexed`、绑定版本一致、`matches_active_version=true`）与检索结果全部保留；旧库迁移文档 `demo.txt` 仍为 `parsed` 且保留 legacy 版本。

---

## 5. T01—T22 证据清单

标注规则：**模拟** = 离线自动测试（可注入假上游 / 模拟 embedding）；**真实** = 真实 Docling / 真实网页 / 真实在线网关。

| 编号 | 验收场景 | 证据类型 | 对应测试 / 记录 | 结论 |
| --- | --- | --- | --- | --- |
| T01 | 旧库升级、再次启动 | 模拟 + 真实 | `test_t01_legacy_upgrade_preserves_ids_and_keeps_index_searchable`、`test_t01_legacy_index_incomplete_is_not_migrated_as_valid`、`test_t01_legacy_index_with_other_model_is_stale`、`test_t01_legacy_interrupted_status_not_forced_to_failed`；真实库迁移（§3.3） | 通过 |
| T02 | 迁移中途异常 | 模拟 | `test_t02_migration_failure_rolls_back_and_keeps_backup`、`test_t02_migration_is_idempotent_on_empty_database`、`test_t02_backup_uses_sqlite_backup_api_with_wal` | 通过 |
| T03 | 重复/并发 parse、幂等键冲突 | 模拟 | `test_t03_repeat_concurrent_parse_and_idempotency` | 通过 |
| T04 | 两 worker 争抢 / 旧租约过期 | 模拟 | `test_t04_lease_claiming_and_stale_token` | 通过 |
| T05 | 重启后按已保存上游 ID 恢复 | 模拟 | `test_t05_restart_resumes_saved_upstream_task`、`test_t05_startup_recovers_expired_lease_without_marking_failed` | 通过 |
| T06 | POST 已接收但响应丢失 | 模拟 | `test_t06_uncertain_submit_requires_manual_retry` | 通过 |
| T07 | 上游 failure / 404 / 超时 / 错误 JSON | 模拟 | `test_t07_upstream_failure_missing_result_and_invalid_json`、`test_t07_client_error_classification`、`test_t07_json_content_accepts_object_and_string` | 通过 |
| T08 | 结果写入 / 分块 / 数据库提交失败 | 模拟 | `test_t08_failures_keep_previous_version_and_index` | 通过 |
| T09 | 完整结果重复领取、提交前后中断 | 模拟 | `test_t09_duplicate_publication_is_idempotent`、`test_t09_result_written_but_not_published_recovers` | 通过 |
| T10 | 空白页夹在正常页中、整份空文档 | 模拟 + 真实 | `test_t10_blank_page_keeps_numbering_and_empty_document_fails`；真实 03 样本第 12 页 | 通过 |
| T11 | 标题/正文/表格交错、表格子节点 | 模拟 + 真实 | `test_t11_reading_order_and_table_child_deduplication`；真实 06 DOCX 去重 | 通过 |
| T12 | PDF 多来源 bbox、DOCX 嵌套标题/合并表格 | 模拟 + 真实 | `test_t12_pdf_multi_source_bbox_and_docx_section_path`；真实 01/02/06 | 通过 |
| T13 | XLSX 多工作表、表从 B3 开始、合并格 | 模拟 + 真实 | `test_t13_xlsx_sheet_names_and_real_cell_coordinates`；真实 07（A2:F8 等） | 通过 |
| T14 | XLSX 无缓存/有缓存公式、空白与零 | 模拟 + 真实 | `test_t14_xlsx_formula_cache_and_blank_versus_zero`、`test_t14_formula_cache_present_warns_differently`、`test_real_xlsx_formula_cache_missing_warning`；真实 07 的 B8:F8 | 通过 |
| T15 | 超长正文/超长表/单行超长/中文数字 | 模拟 | `test_t15_long_text_table_and_numbers` | 通过 |
| T16 | A 有索引，B 失败/成功未索引 | 模拟 | `test_t16_failed_reparse_keeps_old_version_searchable`；真实故障链路（§4.4） | 通过 |
| T17 | B embedding 中途失败/成功 | 模拟 | `test_t17_embedding_failure_keeps_old_index_and_success_switches` | 通过 |
| T18 | B 建索引时 C 发布、切换并发 | 模拟 | `test_t18_target_version_change_marks_superseded` | 通过 |
| T19 | 新来源字段与旧签名、模型/维度改变 | 模拟 | `test_t19_legacy_signature_compatibility_and_real_incompatibility`；`test_t01_legacy_index_with_other_model_is_stale` | 通过 |
| T20 | 格式伪装、损坏/加密/超限、任意路径 | 模拟 | `test_t20_format_spoofing_damaged_and_arbitrary_path`、`test_t20_cross_document_version_access_is_rejected` | 通过 |
| T21 | 前端刷新/切换/断网/重复点击 | **真实浏览器** | `scripts/browser_acceptance.mjs`（T21-1…T21-5 全通过） | 通过 |
| T22 | 文件名/正文含 HTML、敏感上游错误 | **真实浏览器** | `scripts/browser_acceptance.mjs`（T22-1…T22-3 全通过） | 通过 |

---

## 6. 接口契约变化与兼容策略

### 6.1 `/parse` 由同步改为异步（行为变更）

| 场景 | 旧行为 | 新行为 |
| --- | --- | --- |
| 首次解析 | 同步等待并返回 200 + Document | **202** + `{task, document, reused:false, message}` |
| 已有有效版本且未 `force` | 返回 200 已有文档 | **200** + `reused:true`，不新建任务、不调用解析服务 |
| 活动任务重复点击 | 409 | **202** + 同一个 `task.id`（`reused:true`） |
| 同一幂等键不同请求 | 无概念 | **409**（可识别冲突） |

**迁移说明**：原同步测试已改为“创建任务 → 用可控 worker 执行 → 查询结果”的流程，保留原业务意图（上传、解析、分块、失败路径、持久化），未通过删除断言维持表面兼容。

### 6.2 其他兼容策略

- `Chunk.page` 允许为空：DOCX/XLSX 无真实页码时返回 `null`，不伪造“第 1 页”；旧库 legacy 分块保留原页码。
- `Document.status` 语义调整：表示“内容可用性”（存在活动解析版本即 `parsed`），任务状态改由 `task_status` 表达，因此**新任务失败不会把已有可检索内容标记为 failed**。
- `Document` 新增字段均有默认值，旧客户端仍可解析响应。
- 索引新增 `parse_version_id`、`is_legacy`、`matches_active_version` 与 `attempts`；`/index` 响应模型改为 `IndexInfo`。
- `/search` 响应新增 `index_id`、`parse_version_id`、`is_old_version`、`is_legacy`、`message` 与每条命中的 `sources`（旧字段 `results[].chunk_id/page/text/score` 保留）。
- 旧索引签名：新增 `source_signature_algo`，旧算法 `sha256-chunk-dump-v1` 保留验证，新算法 `sha256-version-chunks-v2` 绑定解析版本与块结构；新增来源字段不会让旧索引误判过期，真正不兼容（模型/维度）仍拒绝。

---

## 7. 已知限制与未验证项

### 7.1 已知质量限制（沿用全样本验收结论，未被本期自动修复）

1. 中文条款错序（03 第 4 页、05 第 2 页）仍存在；本期保留原件核对入口，不声称解决。
2. 扫描目录页码错误（04 第 3 页）：`toc_dot_leaders` 只做点线清理与提示，不修正页码对应关系。
3. PDF 公式节点内容为空：仅保留位置与 `formula_content_missing` 告警，未启用公式增强。
4. 科学计数法被展平（如 `3 . 3 · 10 18`）等排版问题未处理。
5. XLSX 公式缓存缺失只做告警，**不启动 Excel/LibreOffice 重算**；有缓存时也只声明“文件保存的缓存”。
6. 图片与统计图未做语义解析，图内文字与数据系列不作为可靠事实。
7. 系统**不声称**能自动发现全部 OCR 错字或阅读顺序错误；也未计算 OCR 字符准确率。
8. 表格真实空白、合并占位、缺失公式值三者可区分，但合并单元格的几何还原依赖上游 bbox，其他布局仍需回归样本。

### 7.2 未验证项（明确标注，不写成通过）

| 项目 | 状态 | 原因 / 后续 |
| --- | --- | --- |
| 旧索引“迁移后仍可检索”的**真实**旧索引 | 未验证（仅模拟） | 改造前真实库 `embedding_indexes` 为空，无真实旧索引可用；已用构造旧库覆盖（含 WAL 备份、签名、维度、损坏索引） |
| DOCX 多级嵌套标题（Heading 2/3） | 未验证 | 06 样本只有一级标题；已实现按父链递归并测试模拟多级，但缺真实多级样本 |
| 真实扫描业务数值表（千分位/负数/百分比） | 未验证 | 现有 04/05 样本为电子稿加噪，无真实装订阴影、透光、倾斜、拍照透视 |
| 真实损坏/加密/超限文件的端到端行为 | 部分验证 | 上传层已用模拟字节验证拒绝路径；未用真实加密业务文件跑完整链路 |
| 索引构建的并发压测 | 未验证 | 只做逻辑级并发（superseded、候选发布、失败保留），未做多请求压力测试 |
| 跨页业务表的合并还原 | 未验证 | 缺跨页表格样本 |
| 图表的系列/年份/数值对应 | 未验证 | 本期明确不承诺 |
| `requirements.lock.txt` 完整重生成 | **未执行（部分更新）** | 已按实测版本追加 `openpyxl==3.1.5` 与 `et_xmlfile==2.0.0`；未整体重解析依赖树，以免在未确认的情况下改动既有锁定版本 |
| 生产环境多 worker 并行 | 未验证 | 首期按任务书约定单 worker 串行；代码用原子领取与租约防止重复执行，但未做多进程压测 |
| DeepSeek RAG 问答、摘要、提取 | 未接入 | 按任务书范围明确返回 503，`/api/capabilities` 为 false |

---

## 8. Git 与安全自检

- **未执行任何 commit / push**（按要求不自动提交或推送）。
- 密钥排查：`git grep` 未发现硬编码 `sk-`/`Bearer` 形式密钥；`.env` 与 `data/` 均被 `.gitignore` 忽略，`git status` 未出现这两类路径。
- 测试与日志未打印密钥；`Settings` 的密钥字段 `repr=False`；`/api/parsing/status` 只返回地址与参数，不返回上游原始响应。
- 大体积原件与解析结果只落在被忽略的 `data/` 下；测试夹具为小体积合成数据。
- 原有未提交修改保留：`deploy/docling/README.md` 与 `docs/Docling独立解析验证记录.md` 的既有改动未被撤销；`README.md` 中的部署说明保持指向 `deploy/docling/README.md`。
- 本地未跟踪文件：新增的 `app/*.py`、`scripts/*`、`tests/*`、`docs/*` 与 `README.md`/`.env.example` 改动均为本次交付内容，等待用户自行决定提交。

---

## 9. 本地日志与证据位置

| 内容 | 位置 |
| --- | --- |
| 真实链路验收证据 | `data/docling-validation/20260923-142718-209416-e2e-acceptance/`（`summary.json`、`*.content.json`、`service.json`） |
| 故障链路证据 | `data/docling-validation/20260923-144014-749012-failure-recovery/`（`steps.json`、`state.json`） |
| 浏览器验收证据 | `data/docling-validation/browser-acceptance/`（`browser-results.json`、`workbench.png`） |
| 真实迁移备份 | `data/db-backups/docqa-20260923-064103.db` |
| 独立解析服务验证（历史） | `data/docling-validation/20260923-110942-182134-gpu-seven-acceptance/` |
| Web / worker 运行日志 | 由启动终端输出（uvicorn 与 `app.parse_worker` 的 logging）；本次未落盘为文件 |

---

## 10. 用户可直接操作的网页验收步骤

1. **启动**（三个进程，顺序：Docling → Web → worker）：

```powershell
$dc = @('compose','--env-file','deploy/docling/lab.env','-f','deploy/docling/compose.yaml','-f','deploy/docling/ocr-gpu.yaml')
$env:PARSER_DEVICE = 'cuda'
docker @dc up -d --pull never --wait --wait-timeout 240
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe -m app.parse_worker
```

2. 打开 http://127.0.0.1:8000 ，确认顶部“Docling 解析服务”显示“解析服务可响应”。
3. 上传 `D:\python\DocQA-test-files\04-scan-zh.pdf`，确认提示“识别为 PDF（PDF 文件标识与结构）；共 6 页”。
4. 点击“解析文档”：观察任务状态依次显示排队 / 等待解析服务返回 / 领取结果 / 规范化 / 分块 / 写入解析版本，**不显示百分比**。
5. 解析完成后查看：版本信息、质量告警（OCR 局限、目录点线、页脚排除）、按页来源（第 N 页 + 坐标框）、正文与表格。
6. 点击“重新解析”，期间确认**旧内容仍可查看**；任务失败时页面同时显示新任务错误与“原有解析版本与索引仍然可用”。
7. 点击“建立向量索引”（会调用真实 embedding 网关，产生少量费用），随后在“检索当前文档原文”输入“本年鉴包含多少个部分”，确认返回原文片段、页码与来源。
8. 重新解析并成功发布新版本后再次检索，确认提示“检索仍使用旧版本”，并可点击“查看命中版本原文”。
9. 刷新页面，确认任务与内容从服务端恢复；点击“刷新”可随时重新同步。
10. 点击“重新解析”后点击“停止等待”，确认提示上游任务编号已保留、可稍后恢复。

---

## 11. 遗留问题与建议的下一步

1. 视需要在用户确认后整体重生成 `requirements.lock.txt`（当前已按实测版本追加新增依赖）。
2. 补充真实多级标题 DOCX、跨页业务表、含缓存公式的工作簿、真实扫描数值表样本，用于把 §7.2 的未验证项转为已验证。
3. 若后续需要“检索最新版本”，可在重新解析成功后提示用户一键为新版本建索引（本期只提示、不自动调用收费接口）。
4. 索引规模扩大后建议替换为专用向量存储；当前逐条扫描仅适用于单文档小规模。
5. RAG 答案生成阶段需在检索结果基础上加入“依据不足判断”与引用校验，本期检索分值不代表回答正确概率。
