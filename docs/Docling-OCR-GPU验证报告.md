# Docling OCR GPU 后端验证报告（修订版）

日期：2026-09-22。范围：独立解析服务的 OCR GPU 后端、证据边界与验证工具；不涉及业务接口接入。

## 结论

本次通过真实 Docling Serve HTTP 请求，在服务进程内捕获检测、方向分类和文字识别三个 RapidOCR ONNX 会话的首次推理 profile，三者均存在 CUDA 节点执行。6 页扫描件转换成功，Markdown 与先前两轮结果的 SHA256 完全一致。

该结论适用于本次镜像、参数和样本，不代表所有节点都在 GPU、不代表所有文档质量合格，也不能反推历史首轮的实际设备。旧报告“首轮一定回退 CPU”“OCR 加速 6.6 倍”“已排除初始化影响”等结论已撤回或降级。

修订前完整报告保存在 Git 忽略目录 data/docling-validation/Docling-OCR-GPU验证报告-修订前备份.md；历史测试未被删除，也未伪装成本次复测。

## 一、本次服务内直接证据

### 配置与方法

- 镜像：docqa/docling-ocr-gpu:1.34.0-ort1.30.0。
- 本地镜像 ID：sha256:d3242e2f52ecc54f5b365b1b3f0b8726df0b5264b1410152f3610ebcfcf54135。
- docling-slim 2.128.0、RapidOCR 3.9.2、onnxruntime-gpu 1.30.0。
- 使用基础 Compose、OCR GPU 覆盖及临时探针覆盖；GPU 覆盖显式配置动态库路径。
- 普通客户端调用 /v1/convert/file/async，ocr_engine=rapidocr、ocr_lang=ch、force_ocr=false、table_mode=accurate。
- scripts/serve_with_ocr_probe.py 为服务创建的 RapidOCR 会话启用 profiling，不提前导入 torch、不改 providers、不改输入或模型。使用原 console entry point 启动服务。
- 探针仅记录每个会话的首次成功 run；不会据此声称覆盖所有页面的每次执行。
- profiling 会影响耗时，因此本轮仅用于执行设备验证，不作性能基准。
- 本轮没有进行 CPU 对照。

### 实际节点执行

| OCR 阶段 | 模型 | CUDA 节点执行事件 | CPU 节点执行事件 | 首次推理完成 |
| --- | --- | ---: | ---: | --- |
| 检测 | PP-OCRv6_det_small.onnx | 190 | 0 | 是 |
| 方向分类 | ch_ppocr_mobile_v2.0_cls_mobile.onnx | 179 | 0 | 是 |
| 文字识别 | PP-OCRv6_rec_small.onnx | 181 | 2 | 是 |

这些数值是 ORT profile 的 Node 事件计数，不是模型算子总数、GPU 利用率或吞吐率。识别模型两个 CPU 辅助节点不等于整模型回退。三个会话 providers 均为 CUDAExecutionProvider、CPUExecutionProvider；结论来自实际节点事件，而非仅来自此列表。

证据目录：[原始会话和 profile](../data/docling-validation/ocr-probe/)，其中 service.log 保留该轮服务日志。

| 阶段 | 本轮会话记录 |
| --- | --- |
| 检测 | f828eaaa273543769e37daf654d4b6b2.session.json |
| 方向分类 | 69c32659cb674ef180c25ec9ba15a4dd.session.json |
| 文字识别 | dd80e27cc50d441ba8a951908695d360.session.json |

会话文件同时保存完整模型 SHA256、运行进程 PID、依赖版本及原始 profile 文件位置。目录可能累积多轮记录，不能将后来生成的其他记录与本轮混用。

### HTTP 结果

结果目录：[本轮完整结果](../data/docling-validation/20260922-223611-627180-gpu-ocr-service-probe-fixed/)。

- 任务：b7b82e33-dfd4-4358-99bb-d68f4d2d7763。
- 输入：04-scan-zh.pdf，SHA256 为 13fead8f3014dc5f74ed9b7dd6270dad3b6ac894db42d24df82ce8d58b2052a2。
- 任务状态与转换状态均为 success，errors=[]，6 页、4 个表格、19 个文本项。
- Markdown 9188 字符；服务报告处理时间 9.150 秒，客户端总等待 12.16 秒。
- Markdown SHA256：BB639C147685302EE6D025B6CECB3C1EDE12889B59F18C15D9AE91FD57D21D4E，与默认语言轮和显式 ch 轮一致。
- 字节一致只能证明这几轮输出一致，不能证明原文识别完全正确。

### 诊断过程中的失败记录

第一次探针运行（任务 fab0fe49-87f6-44d5-8dc2-babe55514edb）因探针查询不存在的 docling 分发元数据失败。镜像实际安装 docling-slim；探针已修正为同时查询并允许未安装包元数据为空。

该次失败保留在 gpu-ocr-service-probe 结果目录，不算模型失败或 GPU 验收通过。后续带 fixed 标签的轮次才是本报告采用的成功证据。客户端也已修正：失败任务不再盲目领取不存在的 result，以免 404 遮盖原始任务失败。

## 二、历史记录与证据等级

| 内容 | 本次核查结果 | 可得结论 |
| --- | --- | --- |
| 原始官方镜像 ORT 仅有 Azure/CPU providers | 前序容器检查和日志已记录 | 该配置当时不具备 ORT CUDA 后端 |
| GPU 包安装后的 7.233 秒首轮 | 本地 summary.json 存在，转换成功 | 首轮设备未直接记录，不能定性为 OCR CPU 回退 |
| 显式 ch 的 6.684 秒轮次 | 本地 summary.json 存在，转换成功 | 请求语言可追溯，设备不能只看标签 |
| 默认参数与显式 ch 输出哈希一致 | 已重新计算并核对 | 当前样本无输出差异 |
| 独立检测模型 2.2/109.6 次每秒及 25 秒 2776 次 | 旧报告有文字记录，当前未找到完整独立原始基准文件 | 标注历史报告记录，未独立复核，不替代服务内三模型验证 |
| 7.74 秒 GPU / 51.37 秒 CPU 对照、显存 5037/1513 MiB | 旧报告有片段，完整脚本和原始采样未找到 | 不能作可复核性能承诺，不能归因于 OCR 单独加速 |

51.37 / 7.74 约为 6.6，算术本身正确；DOCLING_DEVICE 同时影响多个环节，这最多是该次整条流水线的观察比例。总显存包含版面模型及其他程序，不能独立证明 OCR 上卡。本次不会为补旧数据而重新运行用户已要求跳过的 CPU 基线。

## 三、对旧报告的修正

### 1. 导入顺序与动态库

Docling 2.128.0 的 RapidOcrModel 在创建 RapidOCR 前调用 decide_device，而后者会 import torch。因此“Docling 实际流程不会先导入 torch”的说法错误，相应 YAML 注释已修正。

LD_LIBRARY_PATH 可为独立 ORT 进程显式提供 pip 安装的 CUDA/cuDNN 库搜索路径，减少对导入顺序的依赖；这是合理配置，但不能反向证明旧 HTTP 请求必然回退 CPU。独立脚本缺库和真实服务缺库必须分别取证。

### 2. 插件警告

ONNX Runtime 1.30.0 的 No registered plugin EP device found 警告来自插件设备发现分支，该分支未找到设备后仍有其他 CUDA provider 创建路径。不能仅凭此警告判定回退，也不要求此警告消失才判定成功。

本轮应以真实会话 profile 为准。get_available_providers 只表示可用能力；get_providers 表示会话注册后端；节点执行 profile 提供更直接的执行证据。设备利用率和吞吐只能作为辅助，且必须明确采样范围。

### 3. 模型缓存、加载与预热

当前 DOCLING_SERVE_LOAD_MODELS_AT_BOOT=false。artifacts_path 有效、No model weights will be downloaded at runtime 只说明权重已缓存，不代表启动时已创建模型会话。healthy 同样不是模型预热完成证明。

旧报告“初始化都发生在启动阶段，所以 6.6 倍完全排除了初始化影响”不成立。性能测试应单独记录首次请求；相同进程、文件、参数预热后多次重复，并确认 converter 未被淘汰。不擅自给服务 processing_seconds 添加已排除全部初始化成本的含义。

### 4. OCR 语言

Docling 2.128.0 的 RapidOCR 默认语言明确为 ch，并非偶然选中中文。显式 ch 对固定中文样本条件有益，但不是本版本处理中文的唯一合法方式或必须修复项。上游支持原生代码和 iso: 前缀语言标签；裸 zh、zh-Hans 的失败不能解释为映射表不支持中文。

验证脚本新增 --ocr-lang，默认 ch，并记录实际请求值。该值不能唯一确定模型；本次补充模型文件哈希与版本。省略参数不等于必然成功，更不等于当前已发生静默识别错误。

### 5. 环境与样本

旧报告关于 cudnn64_8.dll / cudnn64_9.dll 的 Windows 历史讨论不能解释当前 Linux 容器，已移除。容器内依赖应按 .so、实际包版本和执行结果判断。

当前实际样本为 7 份，第 8 份清单所列文件不存在。合成扫描件的通顺文本仅是定性观察，不能替代原件校对或量化识别率。

## 四、工具和启动修正

- validate.ps1 同时使用 compose.yaml 与 compose.ocr-gpu.yaml，检查本地派生镜像，不再从公共仓库拉取派生镜像或切回基础镜像。
- 继续仅运行 GPU。全样本后先独立预热扫描请求，再重复三次；不把 converter 可能已被淘汰后的首请求称作热运行。
- 验证客户端记录可配置语言；部分成功、失败和客户端错误均停止后续批量投递。
- 新增可选 compose.ocr-probe.yaml 和服务内探针；正常启动不包含 profiling。
- 部署 README 已统一为 GPU 派生镜像的命令，删除旧 CPU 标签和过时流程。
- 本次原始证据保留在 data/ 下，业务密钥、样本和解析全文不纳入 Git。
- 相关自动测试 8 项通过，覆盖语言传参、部分成功停止、提交超时不重复投递、失败任务不领取结果、节点事件统计和精简发行包元数据；PowerShell 语法及 Compose 合并配置检查通过。
- 验证后已移除临时探针，确认容器命令恢复为 docling-serve run、只保留模型卷，并恢复检查前的停止状态。再次启动请使用部署 README 中包含 GPU 覆盖文件的命令。

## 五、仍待验证

1. 其余文档格式、复杂表格、真实扫描件的内容质量与引用定位。
2. 干净的热运行性能记录、统计汇总和资源采样；本次 profile 轮不提供加速倍率。
3. 客户端重连、重复领取、任务过期、容器重启恢复和离线模型缓存行为。
4. 不同语言、模型版本、批量参数下的行为。
5. 本轮只验证各 OCR 会话首次推理，不能承诺全部节点和所有后续请求均只使用 GPU。

## 六、依据

- [Docling 2.128.0 RapidOCR 实现](https://github.com/docling-project/docling/blob/v2.128.0/docling/models/stages/ocr/rapid_ocr_model.py)
- [Docling 2.128.0 设备选择](https://github.com/docling-project/docling/blob/v2.128.0/docling/utils/accelerator_utils.py)
- [ONNX Runtime 1.30.0 provider 创建代码](https://github.com/microsoft/onnxruntime/blob/v1.30.0/onnxruntime/python/onnxruntime_pybind_state.cc)
- 本项目版本、任务、summary.json、Markdown 哈希及本轮服务内 profile；原始文件路径见上文。
