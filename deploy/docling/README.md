# Docling + RapidOCR GPU 验证环境

这里维护独立的 GPU 解析服务与验证脚本；业务 app/ 已通过 HTTP 客户端接入该服务。独立验证脚本不加载业务 .env、不调用在线模型，输出位于 Git 忽略目录 data/docling-validation/。按用户要求跳过 CPU 基线。业务 Web 与 worker 的启动方式见项目根目录 README。

## 构建和启动

在项目根目录 PowerShell 中执行：

```powershell
# 已有派生镜像时无需重复构建。
docker build --pull=false -f deploy/docling/Dockerfile.ocr-gpu -t docqa/docling-ocr-gpu:1.34.0-ort1.30.0 deploy/docling
$dc = @('compose', '--env-file', 'deploy/docling/lab.env', '-f', 'deploy/docling/compose.yaml', '-f', 'deploy/docling/compose.ocr-gpu.yaml')
$env:PARSER_DEVICE = 'cuda'
docker @dc up -d --pull never --wait --wait-timeout 180
docker @dc ps
docker @dc logs --tail 60 parser
```

两份配置必须同时使用，否则会回到官方原始镜像。API 文档：http://127.0.0.1:5001/docs 。healthy 只表示 HTTP 可用；本配置关闭启动时模型预加载，权重缓存不等于模型加载或预热。

models 命名卷首次创建时复制镜像缓存，重建时复用。不要用空宿主机目录覆盖缓存，也不要执行 down -v 删除模型卷。日常停止使用 `docker @dc stop`。

服务采用 `restart: unless-stopped`：Docker 引擎重新可用后恢复服务，显式手动停止仍保持停止。已有容器可执行 `docker update --restart unless-stopped docqa-parser-lab-parser-1` 同步该策略，无需重建镜像或模型卷。该策略不能替代 Docker 引擎本身的启动，也不能保证解析任务在容器重启后仍保留；提交结果不明时继续按已保存任务编号核对，不盲目重投。

## 样本与自动验证

```powershell
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe scripts/validate_docling.py D:\python\DocQA-test-files --pattern '04-*.pdf' --label gpu-scan --ocr-lang ch

pwsh -NoProfile -File deploy/docling/validate.ps1 -Samples D:\python\DocQA-test-files
Get-Content data/docling-validation/stage1-status.json -Encoding UTF8
Get-Content data/docling-validation/stage1.log -Tail 30 -Encoding UTF8
```

自动脚本检查本地派生镜像，用两份 Compose 文件启动 GPU 服务；执行全部现有样本，再对扫描件预热一次、重复三次。不会拉取不存在的公共派生镜像，也不会运行 CPU 基线。

每轮保存版本、OpenAPI、请求语言、文件哈希、任务、JSON、Markdown 与耗时。部分成功、失败或客户端错误会停止后续投递。失败任务可能没有 result 接口，应查看 task.json 和服务日志；提交超时不能盲目重试。

当前 Docling 默认语言也是 ch。显式 --ocr-lang 固定请求条件，并非修复已证实的语言错误；语言值不能唯一标识模型，还需模型哈希及版本。上游区分原生语言代码和 iso: 前缀标签。

--label 仅为实验标签，不能证明实际设备或缓存状态。比较重复耗时前需确认同一进程、相同参数且 converter 未被淘汰。服务处理时间与客户端等待时间分开记录，不能直接认为处理时间完全排除了初始化。completed / awaiting_review 也不代表质量验收通过。

## 服务内 OCR 探针（仅诊断）

探针为真实 HTTP 解析中的 RapidOCR ONNX 会话开启 profiling；不提前导入 torch、不改变模型或 providers。记录模型哈希、会话后端和首次推理的节点后端。profiling 会影响耗时，本轮不能作为性能基准。

```powershell
New-Item -ItemType Directory -Force data/docling-validation/ocr-probe | Out-Null
$probe = $dc + @('-f', 'deploy/docling/compose.ocr-probe.yaml')
# 重建前确保无未完成任务；local 引擎的任务不保证跨重建保留。
docker @probe up -d --force-recreate --pull never --wait --wait-timeout 180
.\.venv\Scripts\python.exe scripts/validate_docling.py D:\python\DocQA-test-files --pattern '04-*.pdf' --label gpu-ocr-service-probe
Get-ChildItem data/docling-validation/ocr-probe -Filter '*.session.json' | ForEach-Object { Get-Content $_.FullName }

# 结束后移除探针覆盖，恢复普通服务。
docker @dc up -d --force-recreate --pull never --wait --wait-timeout 180
```

检查本轮检测、方向分类、识别模型的独立记录：

- first_run_completed=true 才表示实际执行过输入。
- session_providers 是会话声明，不能单独证明节点在哪里执行。
- node_provider_counts.CUDAExecutionProvider > 0 表示 profile 捕获到实际 CUDA 节点。
- CPU 辅助节点不等于整模型回退；未执行的可选模型应标记未验证。
- 输出累积保存，需结合本轮文件修改时间、PID、模型哈希和任务记录，避免混用旧证据。

No registered plugin EP device 警告不能独立证明 CPU 回退。总显存包含其他模型与程序；整条流水线设备切换的耗时比不能归因于 OCR 单独加速。

## 验收边界

2026-09-23 已完成7份样本转换和首轮内容对照，结论合并在[阶段01：文档解析与OCR](../../docs/阶段01-文档解析与OCR.md)。全部接口成功不代表内容质量合格；公式缺失、中文条款错序和扫描目录页码错误仍需原件核对，后续接入已增加公式缓存缺失告警。

当前实际样本 7 份。合成扫描件不能证明真实扫描件准确率。还需核对姓名、数字、表头、阅读顺序与来源位置，以及重连和重启恢复。Excel 缺公式缓存要与解析漏字区分；PDF 空白页不应拒绝整份文档。

历史数字、GPU执行证据及质量边界统一见[阶段01](../../docs/阶段01-文档解析与OCR.md)，旧原报告完整保留在文档整理备份中。

## small/medium隔离试验（2026-10-19）

当前量化标准与结果见[阶段01](../../docs/阶段01-文档解析与OCR.md)，当时冻结原文见[OCR标准归档](../../docs/archive/2026-10-20/OCR识别量化标准与提升计划.md)。该轮medium通过Docling集成和真实CUDA节点验证，但保留集关键术语出现新增错误，未通过默认切换门槛，正式5001仍用small。大分辨率方案也未通过；不要仅因模型更大而全局替换缓存。

新增工具不读取`.env`或访问生成/embedding供应商：

- `scripts/download_ocr_medium.py --output <隔离模型目录>` 默认只展示计划，显式`--download`才下载固定URL，校验两个ONNX文件的官方SHA256；已有失败文件不覆盖。
- `scripts/evaluate_ocr_quality.py --corpus <已冻结目录> --predictions <保存结果> --output <新报告>` 只做离线评分。追加`--baseline <同保留集small结果>`会运行默认切换关卡，不合格返回1，不能把正常生成评分报告当作质量通过。
- `scripts/run_ocr_profile.py`在镜像内执行区域对照；原始图/模型哈希、框、文字、置信分、初始化/预热/区域耗时和NVML设备显存均留档。不同profile用独立容器，实验时避免其他GPU任务。高细节配置本轮淘汰，不建议在8GB设备继续运行。
- `scripts/convert_with_ocr_profile.py`在镜像内提供small/medium的1至4页隔离转换，保留Docling JSON坐标和Markdown；不是正式业务API的模型切换入口。

复现一页集成的示例使用本轮已经复制的本地资料、只读缓存及既有镜像；输出目录必须是新的。`--probe`只用于执行证据，影响耗时：

```powershell
$ocrRoot = (Resolve-Path 'data/ocr-quality-20261019').Path
$ocrScripts = (Resolve-Path 'scripts').Path
docker run --rm --pull never --gpus all --network none `
  --mount "type=bind,source=$ocrRoot,target=/work" `
  --mount "type=bind,source=$ocrScripts,target=/project-scripts,readonly" `
  --mount 'type=volume,source=docqa-parser-lab_models,target=/cache,readonly' `
  --env 'LD_LIBRARY_PATH=/opt/app-root/lib/python3.12/site-packages/nvidia/cu13/lib:/opt/app-root/lib/python3.12/site-packages/nvidia/cudnn/lib' `
  docqa/docling-ocr-gpu:1.34.0-ort1.30.0 python /project-scripts/convert_with_ocr_profile.py `
  --input /work/sources/curriculum.pdf --models /work/models `
  --output /work/integration-medium-new --profile medium --start-page 8 --end-page 8 --probe
```

这会真实运行本地OCR，不消耗供应商请求额度。默认保留可用PDF文字层；`--force-ocr`只适用于已确认要重新识别的选定页，不能对所有文件一律开启。该入口不自动上传、建索引或发布解析版本。

## 整页、局部与分流验证工具

10月19日整页批的冻结原文见[整页计划](../../docs/archive/2026-10-20/OCR整页与分流验证计划.md)和[候选保护v2](../../docs/archive/2026-10-20/OCR整页分流候选保护-v2.md)。该批完成8页、100次局部识别，不应在原输出目录重复推理。10月20日新增用户PDF分流及后续代码修复统一见阶段01/05；两批均未通过默认升级，这些实验脚本未接入正式问答页面。

```powershell
# 默认只显示计划，不加载模型或创建输出。
.\.venv\Scripts\python.exe scripts/run_ocr_layout.py `
  --inputs data/ocr-layout-20261019/inputs --profile routed --split holdout

# 只读已有结果重评分；使用新的报告路径，无OCR和供应商调用。
.\.venv\Scripts\python.exe scripts/evaluate_ocr_layout_grid.py `
  --root data/ocr-layout-20261019 --profile routed-guarded --split holdout `
  --output data/ocr-layout-20261019/replay-new.json
```

真实运行需要在隔离Docker中显式`--execute`；只挂载inputs图像/无答案计划、只读模型缓存和隔离输出，禁网，不挂gold或业务库。运行时选框只看实际Docling框和置信分，中文/数字按冻结规则走medium，拉丁路径复用small。`guard_ocr_layout.py`只从已有候选离线派生保护结果，数字/限制词/表格文字分歧保留原值并待复核。

最终评分用`evaluate_ocr_layout_grid.py`：按官方`TableData.grid`还原稀疏空白，不补非空内容、不修复行列跨度。早期`score-*`未还原空格，原样保留为计分缺陷证据；`score-grid-*`经18个真实表与安装版本官方实现对照。不要将计分修正当作模型质量提升。
