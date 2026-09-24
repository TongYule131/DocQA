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

2026-09-23 已完成 7 份样本转换和首轮内容对照，详见 [全样本解析质量验收](../../docs/Docling全样本解析质量验收.md)。全部接口成功，但公式缺失、中文条款错序、扫描目录页码错误及 Excel 无缓存公式静默为空仍需处理。该报告同时给出格式支持边界与下一阶段接入顺序。

当前实际样本 7 份。合成扫描件不能证明真实扫描件准确率。还需核对姓名、数字、表头、阅读顺序与来源位置，以及重连和重启恢复。Excel 缺公式缓存要与解析漏字区分；PDF 空白页不应拒绝整份文档。

历史数字、本次证据及待验证事项见 docs/Docling-OCR-GPU验证报告.md。
