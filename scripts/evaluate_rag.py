"""RAG 语义评估脚本：区分“可追溯”与“回答正确”（任务书第 11 节）。

两种模式：

1. **离线模拟（默认）**：读取 `tests/fixtures/rag/semantic_cases.json` 中每个用例
   预先写好的模型输出，把它**真正送入生产校验器与生产渲染器**，再用独立的判定
   规则对最终业务响应打分。它验证的是“协议与引用校验是否按预期工作”，
   **不能**证明真实模型的回答正确。
2. **在线（必须显式 --allow-online）**：把用例的证据写入独立数据目录的样本知识库，
   建立索引，然后对每个问题调用真实的 `RagService`（真实 embedding + 真实
   DeepSeek）。脚本会统计并打印实际的 embedding / 生成请求数量，并受
   `--max-cases` 与 `--max-requests` 双重上限约束，失败不自动重试。

无论哪种模式，判定都包含：状态是否合适、关键事实是否正确、是否出现资料外事实、
引用是否真的支持论断（引用编号合法**不等于**语义正确）。原始结果写入 Git 忽略目录。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.rag import RagService
from app.rag_context import build_evidence
from app.rag_prompts import PROMPT_VERSION
from app.rag_validation import ModelOutputError, render_markdown, validate_model_output
from app.repository import Repository
from app.schemas import Document
from app.vector_index import DocumentIndex

FIXTURE = Path("tests/fixtures/rag/semantic_cases.json")
SAMPLE_KB = Path("tests/fixtures/rag/sample_kb.txt")

# 离线模拟中每份证据属于哪个样例文档；证据的 page 字段只用于展示与来源校验。
OFFLINE_VERSION = "v-semantic-offline"


class CountingEmbedding:
    """包装真实 Embedding，统计请求次数；不改变任何行为。"""

    def __init__(self, inner, budget=None):
        self.inner = inner
        self.settings = inner.settings
        self.calls = 0
        self.batches = 0
        self.budget = budget

    @property
    def signature(self) -> str:
        return self.inner.signature

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        size = self.settings.embedding_batch_size
        for start in range(0, len(texts), size):
            if self.budget: self.budget.take()
            self.calls += 1
            self.batches += 1
            vectors.extend(self.inner.embed(texts[start:start + size]))
        return vectors


class CountingModel:
    """包装真实模型，统计生成请求次数；不做任何自动重试。"""

    def __init__(self, inner, budget=None):
        self.inner = inner
        self.calls = 0
        self.budget = budget
        self.last_raw_output = None

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.last_raw_output = None
        if self.budget: self.budget.take()
        self.calls += 1
        self.last_raw_output = self.inner.generate(system_prompt, user_prompt)
        return self.last_raw_output


def load_cases(limit: int | None = None) -> list[dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert payload["prompt_version"] == PROMPT_VERSION, "用例的 Prompt 版本与代码不一致"
    return cases[:limit] if limit else cases


def build_pack(case: dict, settings: Settings):
    """用生产中相同的证据构建路径，把用例证据做成证据包。"""
    hits = []
    for order, item in enumerate(case["evidence"]):
        hits.append({
            "chunk_id": item["chunk_id"], "document_id": "doc-semantic",
            "page": item.get("page"), "text": item["text"], "score": 1.0 - order * 0.01,
            "parse_version_id": OFFLINE_VERSION, "chunk_type": "text",
            "heading_path": item.get("heading"), "order_index": order,
            "sources": item.get("sources", []),
        })
    result = {"index_id": "idx-semantic", "parse_version_id": OFFLINE_VERSION,
              "document_id": "doc-semantic", "is_old_version": False, "is_legacy": False,
              "chunk_count": len(hits), "results": hits}
    build = build_evidence(result, settings)
    if build.fatal_reason:
        raise RuntimeError(f"用例证据构建失败：{build.fatal_reason}")
    return build.pack


def judge(case: dict, answer: dict, *, retrieval: dict[int, str] | None = None,
          retrieved: list[str] | None = None, fallback_answer: str | None = None,
          check_allowed_refs: bool = False) -> dict:
    """对最终业务响应做法定判定；返回逐项检查与是否通过。

    判定只看**业务响应**（用户实际看到的内容）。引用一致性一律对照**本次实际
    证据正文**核对，而不是对照固件里预写的证据字节：检索可能返回不同片段，
    把固件文本当作唯一真相会误判。

    - `retrieval`：本次入选证据的 {引用编号: 正文}（在线时来自真实证据包，
      离线模拟时由固件证据按编号顺序构造）；
    - `retrieved`：本次送入模型的**全部**证据正文，用于判断期望资料是否被检索到；
    - `check_allowed_refs`：仅离线模拟启用。生产环境里引用编号由服务端按本次
      入选证据分配，固件中的“允许编号”只在编号可预测时才有意义，因此在线判定
      改用“被引用片段是否真的支持该论断”这一内容判定。
    """
    checks: list[dict] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    visible = answer["answer"] if answer["status"] != "insufficient_evidence" \
        else (fallback_answer if fallback_answer is not None else answer["answer"])
    facts = list(answer.get("conclusion", [])) + list(answer.get("explanation", []))
    used = {citation["reference_id"] for citation in answer["citations"]}

    # “资料里没有该信息”是可由原文支持的事实，与“检索不到相关资料”是两种合法回答。
    # 因此当固件给出“说明资料缺失”的原文短语时：
    #   - answered：只有在引用的片段确实包含该短语时才算合适；
    #   - clarification_needed / insufficient_evidence：属于对缺失条件的合理回应，同样合适。
    no_record = case.get("no_record_supported_answer")
    if no_record and answer["status"] == "answered":
        cited_refs = set(used)
        for fact in facts:
            cited_refs.update(fact["refs"])
        cited = "".join((retrieval or {}).get(ref, "") for ref in cited_refs)
        supported = any(marker in cited for marker in no_record)
        add("status", supported,
            "以“资料中无该记录”作答且被引用片段支持" if supported
            else "以“资料中无该记录”作答，但没有被引用片段支持该说法")
    elif no_record and answer["status"] in {"clarification_needed", "insufficient_evidence"}:
        add("status", True,
            "对缺少可判定信息的问题选择了澄清或依据不足，属于合理回应")
    else:
        # accepted_statuses 用于“澄清或依据不足都算合格”的用例（例如公式无缓存值）；
        # 默认仍只接受唯一期望状态，避免放宽判定标准。
        accepted = case.get("accepted_statuses") or [case["expected_status"]]
        add("status", answer["status"] in accepted,
            f"期望 {'/'.join(accepted)}，实际 {answer['status']}")

    missing = [text for text in case.get("must_include", []) if text not in visible]
    if missing and answer["status"] == "insufficient_evidence" \
            and case.get("accept_fallback_as_support"):
        # 该用例允许“依据不足 + 后端固定兜底文本”作为合格回答：此时没有结论，
        # 只要兜底文本明确说明缺少依据即可，不再要求出现某个具体措辞。
        missing = []
    add("must_include", not missing,
        "缺失：" + "、".join(missing) if missing else "全部关键事实出现")

    forbidden = [text for text in case.get("must_not_include", []) if text in visible]
    add("must_not_include", not forbidden,
        "出现禁止内容：" + "、".join(forbidden) if forbidden else "未出现资料外或矛盾事实")

    used = {citation["reference_id"] for citation in answer["citations"]}
    if check_allowed_refs:
        allowed = set(case.get("allowed_refs", []))
        extra = used - allowed
        add("references_within_allowed", not extra,
            f"超出允许范围的引用：{sorted(extra)}" if extra
            else f"引用 {sorted(used)} 均在允许范围内")

    pool = retrieval if retrieval is not None else _fixture_pool(case)
    all_evidence = retrieved if retrieved is not None else list(pool.values())

    if answer["status"] == "insufficient_evidence":
        if case["expected_status"] == "insufficient_evidence":
            # 预期就是依据不足：固定兜底文本与空引用即为正确表现。
            add("quotes_traceable_to_evidence", not answer["citations"],
                "依据不足时引用为空")
            add("conclusion_supported_by_cited_chunks", True,
                "本题预期即为依据不足，后端返回固定兜底文本，引用为空")
        else:
            add("quotes_traceable_to_evidence", not answer["citations"],
                "依据不足时引用为空")
            add("conclusion_supported_by_cited_chunks", False,
                f"本次检索片段依据不足，未给出期望的 {case['expected_status']} 结论")
        passed = all(item["passed"] for item in checks)
        return {"passed": passed, "checks": checks,
                "failure_category": None if passed else _category(checks)}

    # 引述必须是**本次证据正文**的连续子串。
    quote_problems = []
    for citation in answer["citations"]:
        text = pool.get(citation["reference_id"])
        if text is None:
            quote_problems.append(f"[{citation['reference_id']}] 不是本次入选证据")
        elif citation["quote"] not in text:
            quote_problems.append(f"[{citation['reference_id']}] 引述不在本次证据正文中")
    add("quotes_traceable_to_evidence", not quote_problems,
        "；".join(quote_problems) if quote_problems else "全部引述都是本次证据正文的连续子串")

    # 词面回归检查：能发现部分数字/单位错引，不能替代人工语义核对。
    # 逐片段核对（不能把多个片段拼成一个字符串再判断，否则跨片段的字面拼接会误判）。
    # 结论和补充说明一视同仁；事实在其他未引用片段中存在也仍是错引。
    unsupported = []
    misattributed = []
    evidence_text = "".join(all_evidence)
    def locate(fact: dict, token: str, *, note: str) -> None:
        variants = _token_variants(case, token)
        if any(variant in pool.get(ref, "") for variant in variants for ref in fact["refs"]):
            return
        if any(variant in evidence_text for variant in variants):
            misattributed.append(f"{token}{note}（引自 {fact['refs']}）")
        else:
            unsupported.append(f"{token}{note}（引自 {fact['refs']}）")

    for fact in answer.get("conclusion", []):
        for token in _fact_tokens(fact["text"]):
            locate(fact, token, note="")
    for fact in answer.get("explanation", []):
        for token in _fact_tokens(fact["text"]):
            locate(fact, token, note="（补充说明）")
    add("facts_supported_by_cited_chunks", not unsupported and not misattributed,
        "资料中也找不到：" + "、".join(unsupported[:5]) if unsupported
        else ("事实在资料中存在但引用归属错误" if misattributed else "关键数字与单位出现在本条事实的引用中"))
    if misattributed:
        checks.append({"check": "citation_attribution", "passed": False,
                       "detail": "以下关键事实在资料中存在，但未出现在其引用的片段里，"
                                 "需要复核引用归属：" + "、".join(misattributed[:5])})

    # 期望的关键事实是否真的出现在**被引用的**片段中（而不是某条没被引用的片段里）。
    expected_tokens = [text for text in case.get("must_include", [])
                       if re.search(r"[\u4e00-\u9fff]", text)]
    cited_text = "".join(pool.get(ref, "") for ref in used)
    absent_from_citations = [token for token in expected_tokens if token not in cited_text]
    add("conclusion_supported_by_cited_chunks", not absent_from_citations,
        "被引用片段中没有：" + "、".join(absent_from_citations) if absent_from_citations
        else "本题期望的关键事实能在被引用片段中找到")

    # 回收检查：期望证据（病例说明 + 证据正文）是否出现在本次送入模型的证据里。
    expectation = case["_expectation"] if "_expectation" in case else case["question"]
    all_text = "".join(all_evidence)
    absent = [token for token in expected_tokens if token not in all_text]
    add("expected_evidence_retrieved", not absent,
        "本次入选片段中缺少：" + "、".join(absent) if absent
        else f"期望资料已进入本次证据集合（对照说明：{expectation[:24]}…）")

    passed = all(item["passed"] for item in checks)
    return {"passed": passed, "checks": checks,
            "failure_category": None if passed else _category(checks, case, answer, missing)}


def _is_numeric_token(text: str) -> bool:
    """判断期望事实是否是数字/单位类短串：这类串在片段中可能出现多次，不用于召回判断。"""
    return bool(re.fullmatch(r"[0-9A-Za-z.\s%元万元人日天年个月，,、]+", text)) and bool(
        re.search(r"\d", text))


def _fixture_pool(case: dict) -> dict[int, str]:
    """离线模拟使用的证据映射：固件证据按引用编号顺序 + “无记录”说明片段。

    离线模拟里编号是可预测的（证据构建顺序），因此把表示“资料中无该记录”的
    原文也挂在编号之后，用于判定“以无记录作答时是否引用了支持该说法的原文”。
    """
    pool = {index: item["text"] for index, item in enumerate(case["evidence"], start=1)}
    for offset, marker in enumerate(case.get("no_record_marker", []), start=1):
        pool[len(pool) + offset] = marker
    return pool


def _token_variants(case: dict, token: str) -> list[str]:
    """返回该关键事实的等价写法（例如中文数字“七日”与“七天”）。

    等价关系必须由用例显式声明（`token_aliases`），不做隐式归一化，
    否则判定规则会变得不可复核。
    """
    variants = [token]
    for key, aliases in (case.get("token_aliases") or {}).items():
        if token == key:
            variants.extend(aliases)
        elif token in aliases:
            variants.append(key)
            variants.extend(alias for alias in aliases if alias != token)
    return variants


def _fact_tokens(text: str) -> list[str]:
    """提取事实中的数字与常见单位，作为“是否真有依据”的最小可判定依据。"""
    tokens = set()
    for pattern in (r"\d+(?:\.\d+)?", r"[一二三四五六七八九十百千万亿]+人",
                    r"[一二三四五六七八九十百千万]+(?:日|天|年|月|万元|元|个部分)"):
        tokens.update(re.findall(pattern, text))
    return sorted(tokens)


def _category(checks: list[dict], case: dict | None = None, answer: dict | None = None,
              missing: list[str] | None = None) -> str:
    """给出失败类别；对“状态不合适”进一步区分原因，避免笼统归类。"""
    names = {item["check"] for item in checks if not item["passed"]}
    if "status" in names and case is not None and answer is not None:
        expected = case["expected_status"]
        actual = answer["status"]
        if expected in {"clarification_needed", "insufficient_evidence"} and actual == "answered":
            if expected == "clarification_needed" and missing:
                return "缺少关键条件仍给出确定结论"
            return "资料依据不足仍给出确定结论"
        if expected == "answered" and actual in {"clarification_needed", "insufficient_evidence"}:
            return "资料已足够支持结论但未作答"
    for item in checks:
        if not item["passed"]:
            return {"must_include": "关键事实错误或缺失",
                    "must_not_include": "出现资料外或矛盾事实",
                    "status": "状态不合适",
                    "references_within_allowed": "引用超出允许范围",
                    "quotes_traceable_to_evidence": "引述与原文不一致",
                    "facts_supported_by_cited_chunks": "引用不支持论断（语义错引）",
                    "conclusion_supported_by_cited_chunks": "结论缺少被引用片段支持",
                    "expected_evidence_retrieved": "本次检索未召回期望证据"}.get(
                        item["check"], item["check"])
    return "未分类"


# ---------------------------------------------------------------------------
# 离线模拟
# ---------------------------------------------------------------------------
def run_offline(cases: list[dict], settings: Settings) -> dict:
    """把预写输出送入生产校验器/渲染器，再按同一套判定规则打分。"""
    records = []
    for case in cases:
        pack = build_pack(case, settings)
        raw = json.dumps(case["simulated_model_output"], ensure_ascii=False)
        record = {"case_id": case["case_id"], "category": case["category"],
                  "mode": "offline-simulated", "question": case["question"],
                  "expected_status": case["expected_status"], "raw_model_output": raw}
        try:
            validated = validate_model_output(raw, pack)
        except ModelOutputError as exc:
            # 预写输出被生产校验器拒绝：这本身就是重要结论，必须如实记录。
            record.update({"validated": False, "reason": exc.reason,
                           "answer": None, "passed": False,
                           "failure_category": f"校验器拒绝（{exc.reason}）"})
            records.append(record)
            continue
        if validated.status == "insufficient_evidence":
            from app.rag_prompts import INSUFFICIENT_EVIDENCE_TEXT
            answer_text = INSUFFICIENT_EVIDENCE_TEXT
            citations = []
        else:
            answer_text = render_markdown(validated)
            citations = [{
                "reference_id": ref, "chunk_id": pack.item_by_ref(ref).chunk_id,
                "page": pack.item_by_ref(ref).page, "quote": validated.quotes[ref],
                "parse_version_id": pack.parse_version_id,
                "document_id": pack.item_by_ref(ref).document_id,
                "sources": [source.model_dump() for source in pack.item_by_ref(ref).sources],
            } for ref in sorted(validated.quotes)]
        answer = {"status": validated.status, "answer": answer_text, "citations": citations,
                  "conclusion": [{"text": f.text, "refs": f.refs} for f in validated.conclusion],
                  "explanation": [{"text": f.text, "refs": f.refs} for f in validated.explanation]}
        verdict = judge(case, answer, retrieval={item.reference_id: item.text
                                                for item in pack.items},
                        check_allowed_refs=True)
        record.update({"validated": True, "answer": answer, **verdict})
        records.append(record)
    return {"mode": "offline-simulated", "records": records,
            "summary": summarize(records)}


# ---------------------------------------------------------------------------
# 在线评估
# ---------------------------------------------------------------------------
def _settings_with_dir(data_dir: Path) -> Settings:
    """从环境/.env 获取密钥等配置，但把数据目录换成独立测试目录。

    评估只使用独立数据目录，绝不触碰正式业务库。
    """
    env = Settings.from_env()
    values = {name: getattr(env, name) for name in env.__dataclass_fields__}
    values["data_dir"] = data_dir.resolve()
    return Settings(**values)


def ensure_sample_document(settings: Settings, *, allow_online: bool, cases: list[dict],
                           reuse: bool = True, embedding=None) -> tuple[str, bool]:
    """准备语义评估用的样本知识库与索引；返回 (document_id, 是否复用了已有索引)。

    索引版本由下述条目构建：**每个用例的每条证据自成一个检索块**，另加来自
    `sample_kb.txt` 的同主题干扰条款（不含任何关键事实）。这样做是因为
    逐条证据独立成块才能检验“本次引用了哪一块、该块是否真的支持该论断”，
    也才能让固定的期望证据与真实检索片段一一对应。

    说明：写入使用生产仓储入口 `Repository.finish_parse` + `DocumentIndex.build`，
    检索、生成、校验与渲染全部是生产路径；仅“分块粒度”为评测需要而固定。
    """
    repository = Repository(settings.data_dir / "docqa.db")
    repository.initialize()
    document_id = "doc-semantic-sample"
    chunks = build_sample_chunks(document_id, cases)
    existing = repository.get(document_id)
    index = DocumentIndex(repository, embedding or _real_embedding(settings))
    if existing is None:
        repository.create(Document(id=document_id, filename="样本知识库（合成测试样本）.txt",
                                   size=sum(len(chunk.text) for chunk in chunks),
                                   created_at=datetime.now(timezone.utc).isoformat(),
                                   status="uploaded", format="txt"))
    if existing is not None and reuse and index.index_info(document_id)["status"] == "indexed":
        current = repository.chunks(document_id)
        if [c.text for c in current] == [c.text for c in chunks]:
            return document_id, True
    repository.finish_parse(document_id, 1, chunks)
    if not allow_online:
        return document_id, False
    index.build(document_id, rebuild=True)
    return document_id, False


def build_sample_chunks(document_id: str, cases: list[dict]) -> list:
    """构造评估用检索块：逐条用例证据 + 同主题干扰条款。

    干扰条款用于检验检索是否会命中无关内容，以及模型是否会“有片段就强行回答”。
    """
    from app.schemas import Chunk, SourceLocation

    chunks: list[Chunk] = []
    for case in cases:
        for order, item in enumerate(case["evidence"]):
            chunks.append(Chunk(
                id=f"{case['case_id']}-c{order + 1}", document_id=document_id, page=1,
                text=item["text"],
                sources=[SourceLocation(**source) for source in item.get("sources", [])]
                or [SourceLocation(format="txt", page=1)],
                chunk_type="text"))
    for order, paragraph in enumerate(_distractor_paragraphs(cases), start=1):
        chunks.append(Chunk(
            id=f"distractor-{order:02d}", document_id=document_id, page=1, text=paragraph,
            sources=[SourceLocation(format="txt", page=1, note="合成干扰条款")],
            chunk_type="text"))
    return chunks


def _distractor_paragraphs(cases: list[dict] | None = None) -> list[str]:
    """从 sample_kb.txt 中取出真实的干扰条款。

    必须排除与用例证据重复或互相包含的条款：否则索引里会出现两条几乎相同的
    片段，既挤占送入模型的 5 个名额，也让“召回是否成功”的结论失真。
    """
    text = SAMPLE_KB.read_text(encoding="utf-8")
    paragraphs = [line.strip() for line in text.split("\n\n") if line.strip()]
    candidates = [line for paragraph in paragraphs for line in paragraph.split("\n")
                  if len(line) > 24 and not line.startswith(("说明：", "样本知识库", "它的唯一用途"))]
    evidence = [item["text"] for case in (cases or []) for item in case["evidence"]]
    unique: list[str] = []
    for line in candidates:
        if any(line in text_item or text_item in line for text_item in evidence):
            continue          # 与用例证据重复：不重复入库
        if line in unique:
            continue
        unique.append(line)
    return unique


def _real_embedding(settings: Settings):
    from app.embedding import APIEmbedding
    return APIEmbedding(settings)


def run_online(cases: list[dict], settings: Settings, *, doc_map: dict[str, str] | None,
               allow_online: bool, max_requests: int, embedding=None, model=None) -> dict:
    """真实调用模式：真实检索 + 真实生成 + 生产校验器。"""
    from app.deepseek import DeepSeekModel
    from scripts.rag_eval_safety import RequestBudget
    if not allow_online:
        raise ValueError('必须显式允许在线评估')
    budget = RequestBudget(max_requests)

    repository = Repository(settings.data_dir / "docqa.db")
    embedding = embedding or CountingEmbedding(_real_embedding(settings), budget)
    model = model or CountingModel(DeepSeekModel(settings), budget)
    index = DocumentIndex(repository, embedding)
    service = RagService(settings, repository, index, model)

    records = []
    for case in cases:
        document_id = (doc_map or {}).get(case["case_id"])
        if document_id is None:
            records.append({"case_id": case["case_id"], "category": case["category"],
                            "mode": "online", "passed": False,
                            "failure_category": "未提供文档映射",
                            "note": "该用例没有可用的真实文档，标为未验证"})
            continue
        if embedding.calls + model.calls >= max_requests:
            records.append({"case_id": case["case_id"], "category": case["category"],
                            "mode": "online", "passed": False,
                            "failure_category": "请求上限",
                            "note": "达到 --max-requests 上限，未执行"})
            continue
        before = (embedding.calls, model.calls)
        record = {"case_id": case["case_id"], "category": case["category"], "mode": "online",
                  "question": case["question"], "document_id": document_id,
                  "expected_status": case["expected_status"]}
        try:
            model.last_raw_output = None
            outcome = service.answer(document_id, case["question"])
        except Exception as exc:  # noqa: BLE001 - 记录真实失败，不掩盖
            record.update({"passed": False, "failure_category": "调用失败",
                           "error_code": getattr(exc, "code", type(exc).__name__),
                           "error": "本次调用失败，未通过验收",
                           "raw_model_output": getattr(model, "last_raw_output", None),
                           "requests": {"embedding": embedding.calls - before[0],
                                        "generation": model.calls - before[1]}})
            records.append(record)
            continue
        answer = outcome.answer.model_dump()
        record["answer"] = answer
        record["raw_model_output"] = getattr(model, "last_raw_output", None)
        record["requests"] = {"embedding": embedding.calls - before[0],
                              "generation": model.calls - before[1]}
        # 记录本次实际入选片段（编号 → 片段信息），便于复核引用与召回。
        record["selected_evidence"] = [
            {"reference_id": item["reference_id"], "chunk_id": item["chunk_id"],
             "page": item["page"], "text": item["text"], "text_head": item["text"][:200]}
            for item in outcome.diagnostics.get("selected", [])]
        # 用本次真实入选片段核对引述与论断支撑；判定规则与离线模式一致，
        # 但在线模式的引用编号由服务端按本次检索分配，因此不做固件编号范围检查。
        selected = outcome.diagnostics.get("selected", [])
        verdict = judge(case, answer,
                        retrieval={item["reference_id"]: item["text"] for item in selected},
                        retrieved=[item["text"] for item in selected])
        record.update(verdict)
        records.append(record)
    return {"mode": "online", "records": records, "summary": summarize(records),
            "requests": {"embedding_calls": embedding.calls, "embedding_batches": embedding.batches,
                         "generation_calls": model.calls}}


def summarize(records: list[dict]) -> dict:
    """给出分子/分母，而不是一个笼统的“准确率”。"""
    total = len(records)
    # 未执行、网络失败没有经过结构校验，不能计为校验器接受或拒绝。
    validated = [r for r in records if r.get("validated") is True
                 or (r.get("answer") and r.get("validated") is not False)]
    return {
        "total": total,
        "passed": sum(1 for r in records if r.get("passed")),
        "failed": sum(1 for r in records if not r.get("passed")),
        "status_correct": sum(1 for r in records if r.get("passed") or _check(r, "status")),
        "facts_correct": sum(1 for r in records if r.get("passed") or _check(r, "must_include")),
        "no_fabrication": sum(1 for r in records if r.get("passed") or _check(r, "must_not_include")),
        "citations_traceable": sum(1 for r in records
                                   if r.get("passed") or _check(r, "quotes_traceable_to_evidence")),
        "citations_support_claims": sum(1 for r in records
                                        if r.get("passed")
                                        or _check(r, "facts_supported_by_cited_chunks")),
        "conclusions_supported_by_citations": sum(
            1 for r in records if r.get("passed")
            or _check(r, "conclusion_supported_by_cited_chunks")),
        "expected_evidence_retrieved": sum(1 for r in records
                                           if r.get("passed")
                                           or _check(r, "expected_evidence_retrieved")),
        "validator_accepted": len(validated),
        "validator_rejected": sum(1 for r in records if r.get("validated") is False),
        "validator_not_evaluated": total - len(validated)
                                   - sum(1 for r in records if r.get("validated") is False),
        "failures": [{"case_id": r["case_id"], "category": r.get("failure_category"),
                      "expected_status": r.get("expected_status")} for r in records
                     if not r.get("passed")],
    }


def _check(record: dict, name: str) -> bool:
    for item in record.get("checks", []):
        if item["check"] == name:
            return item["passed"]
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 语义评估（默认离线模拟）")
    parser.add_argument("--allow-online", action="store_true",
                        help="显式允许真实在线调用（会产生费用）")
    parser.add_argument("--max-cases", type=int, default=6,
                        help="在线模式最多执行的用例数（默认 6）")
    parser.add_argument("--max-requests", type=int, default=20,
                        help="在线模式 embedding + 生成请求总数上限")
    parser.add_argument("--all-cases", action="store_true", help="离线模式运行全部固定用例")
    parser.add_argument("--data-dir", default="data/rag-eval", help="独立测试数据目录")
    parser.add_argument("--out", default=None, help="结果输出目录（默认写入 data/rag-eval）")
    parser.add_argument("--doc-map", default=None,
                        help="在线模式的 case_id → document_id 映射 JSON 文件")
    parser.add_argument("--reuse-index", action="store_true", help="复用已存在的样本索引")
    args = parser.parse_args()

    from scripts.rag_eval_safety import RequestBudget, validate_online_dir
    if args.max_requests < 1 or args.max_cases < 1:
        parser.error('请求数和用例数上限必须为正整数')
    data_dir = validate_online_dir(Path(args.data_dir)) if args.allow_online else Path(args.data_dir)
    # 离线模式不读取 .env，不因用户线上配置异常而无法运行。
    settings = _settings_with_dir(data_dir) if args.allow_online else Settings(data_dir=data_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out) if args.out else Path(args.data_dir) / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = load_cases(None if args.all_cases else (args.max_cases if args.allow_online else None))
    if args.allow_online:
        from app.deepseek import DeepSeekModel
        budget = RequestBudget(args.max_requests)
        embedding = CountingEmbedding(_real_embedding(settings), budget)
        model = CountingModel(DeepSeekModel(settings), budget)
        doc_map = json.loads(Path(args.doc_map).read_text(encoding="utf-8")) if args.doc_map else None
        if doc_map is None:
            document_id, reused = ensure_sample_document(settings, allow_online=True,
                                                         cases=cases, reuse=args.reuse_index, embedding=embedding)
            doc_map = {case["case_id"]: document_id for case in cases}
            print(f"样本文档：{document_id}（复用索引：{reused}）")
        result = run_online(cases, settings, doc_map=doc_map, allow_online=True,
                            max_requests=args.max_requests, embedding=embedding, model=model)
    else:
        result = run_offline(cases, settings)

    result["meta"] = {
        "prompt_version": PROMPT_VERSION,
        "model": settings.deepseek_model,
        "embedding_model": settings.embedding_model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "mode": result["mode"],
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"rag-eval-{result['mode']}-{stamp}.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = result["summary"]
    print(f"模式：{result['mode']}；用例 {summary['total']} 个；通过 {summary['passed']}，"
          f"失败 {summary['failed']}")
    print(f"状态正确 {summary['status_correct']}/{summary['total']}，"
          f"关键事实正确 {summary['facts_correct']}/{summary['total']}，"
          f"无资料外事实 {summary['no_fabrication']}/{summary['total']}，"
          f"引述可追溯 {summary['citations_traceable']}/{summary['total']}，"
          f"引用支持论断 {summary['citations_support_claims']}/{summary['total']}，"
          f"期望证据召回 {summary['expected_evidence_retrieved']}/{summary['total']}")
    if result.get("requests"):
        print(f"实际请求：embedding {result['requests']['embedding_calls']} 次"
              f"（{result['requests']['embedding_batches']} 批），"
              f"生成 {result['requests']['generation_calls']} 次")
    for record in result["records"]:
        if not record.get("passed"):
            print(f"  FAIL {record['case_id']}：{record.get('failure_category')}"
                  f"（期望 {record.get('expected_status')}）")
    print(f"结果文件：{target}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
