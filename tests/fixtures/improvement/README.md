# 提升工程合成资料与预标注

18份真实格式文件，六类各3份：双栏论文PDF、12页报告PDF、制度DOCX、复杂DOCX、复杂XLSX、扫描／混合PDF。每份6道问答、1摘要、1提取，共144场景；12份开发、6份holdout。全部内容为项目生成的虚构资料，采用CC0-1.0，不是公开研究或真实业务资料。

`manifest.json`记录每份文件的SHA-256、来源、许可、主题与模板族、事实原文及PDF页／DOCX段表／XLSX工作表范围，以及每场景必须覆盖的事实、联合证据组、禁止推断和预期状态。源稿先于模型响应编写。源稿作者标注尚未独立复核；扫描图像的文字目前只由作者源稿标注，未执行真实OCR验收。

`holdout`是工程分组，内容随源码可见，尚不属于由独立人员保管、未暴露的真实盲测。各主题和模板族不跨组，holdout使用不同叙述次序或资料组织；公用制版代码、相同合成写作风格和有限内容复杂度仍会限制泛化。真实资料与独立封存样本还需在后续联合验收中补齐，不能将本夹具成绩宣传为真实文档效果。

默认命令使用项目原有Python和只读库，复制规范二进制资产到隔离目录并核验；不读取`.env`、正式库、已有预算，不启动服务，不调用Docling／embedding／生成模型。运行`prepare_evaluation_corpus.py --output-dir <空目录>`；随后`evaluate_improvement.py --corpus-dir <目录> --split all --report <新报告路径>`。没有人工复核记录时报告明确为144项未执行，不能算144项通过。

复核记录为JSON列表，字段为`scenario_id`、`mode`（`engineering_fixture`／`saved_response_review`／`live_provider_review`）、`reviewer`、`output_sha256`、`supported_fact_ids`、`input_evidence_fact_ids`、`answer_status`、`fact_support_confirmed`及`violations`。它是对已有输出的人工作业记录，不是自动语义judge，也不会执行或验证上游调用。覆盖只对已复核场景计算，分别报告工程、保存响应与在线复核，不把缺测计为通过。`violations`非空、缺必须事实、错误拒答或未确认事实支持均失败。实际结果、原始输出、复核文件和调用凭证保存在独立忽略目录；不能伪造哈希或依靠一条人工标签宣称完成联合验收。

规范文件由`author_sources.py`和`build_workbooks.mjs`生成。显式重建需要bundled Python的reportlab、python-docx、Pillow，以及bundled Node的`@oai/artifact-tool`；Windows扫描字体为`C:/Windows/Fonts/msyh.ttc`。先将`author_sources.SOURCES`中三份工作簿的id／title／quotes导出为JSON，运行JS构建器，再运行Python源稿构建器。OOXML包时间固定、PDF使用invariant模式，默认复制命令跨平台字节一致；重新制版的字节可能随字体与库版本变化，应审阅哈希变化，不能覆盖旧评估输入。

XLSX中的公式无缓存值是刻意的缺值负例，构建器在渲染后移除公式缓存；业务代码不得重算或补零。目录不含供应商响应，样本全部可公开。视觉渲染与独立复核限制以工程交接记录为准，结构可读不等于版面已验收。


公开真实资料储备由 `public_sources.json` 和 `scripts/collect_public_corpus.py` 另行管理，不混入上述CC0合成文件。默认采集命令仅核验本地哈希／格式；只有显式 `--download` 访问6条固定官方原件URL。原采集记录为5份下载并冻结哈希、1份英国官方XLSX连接超时，详见隔离review证据；当前本机原件是否齐全须另做只读哈希核对，本次文档核对未新增下载。中国公开附件不推定开放再分发许可；原件不进Git。生态报告与NASA长报告超过默认容量边界，不能当成当前可执行正例；预标注及原页定位还需独立复核。工程holdout与真正封存盲测的区别仍适用。
