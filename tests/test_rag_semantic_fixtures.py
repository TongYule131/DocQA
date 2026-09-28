"""RAG 语义评估固件与离线判定的回归测试。

作用：保证语义用例本身的结构正确、预期在看模型输出前写定，并且
`scripts/evaluate_rag.py --all-cases`（离线模拟）在这批固定用例上得出稳定结论：

- 除“语义错引反例”S13 之外的用例都应通过；
- S13（原文九人、答案十一人、引用编号与引述都真实）**必须失败**，
  用来证明判定规则确实能发现“形式合法但语义错误”的情况。

注意：这里通过的是**离线模拟**，只证明协议与判定规则有效，不能证明真实模型正确。
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from app.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "rag" / "semantic_cases.json"
SAMPLE_KB = Path(__file__).parent / "fixtures" / "rag" / "sample_kb.txt"


def _load_evaluator():
    """以模块方式加载评估脚本，复用它的判定规则（不复制一份实现）。"""
    path = Path(__file__).parent.parent / "scripts" / "evaluate_rag.py"
    spec = importlib.util.spec_from_file_location("evaluate_rag", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload["cases"]


def test_fixture_has_required_cases(cases):
    """至少 12 个固定用例，且覆盖任务书列出的语义检查重点。"""
    assert len(cases) >= 12
    categories = {case["category"] for case in cases}
    for expected in ["正常单一事实", "多片段联合回答", "条件缺失", "无关问题",
                     "有关但资料不完整", "两条相冲突的规则", "无真实更新时间",
                     "含例外或特殊有效期", "表格数据", "公式无缓存",
                     "文档中的注入", "语义错引反例"]:
        assert expected in categories, f"缺少语义用例类别：{expected}"
    ids = [case["case_id"] for case in cases]
    assert len(ids) == len(set(ids)), "case_id 必须唯一"


def test_fixture_case_schema(cases):
    """每个用例都要有 case_id、问题、证据、预期状态、必须/禁止出现的事实、允许引用与判定依据。"""
    for case in cases:
        for key in ["case_id", "question", "expected_status", "evidence",
                    "must_include", "must_not_include", "allowed_refs", "rationale",
                    "simulated_model_output", "category"]:
            assert key in case, f"{case.get('case_id')} 缺少字段 {key}"
        assert case["expected_status"] in {"answered", "clarification_needed",
                                           "insufficient_evidence"}
        assert case["rationale"].strip()
        assert case["evidence"], "用例必须给出证据，否则无法验证引用一致性"
        for index, item in enumerate(case["evidence"], start=1):
            assert f"[{index}]" not in item["text"], "证据正文不应包含引用标记"
        # 证据块的来源结构必须是契约允许的格式。
        for item in case["evidence"]:
            for source in item.get("sources", []):
                assert source["format"] in {"pdf", "docx", "xlsx", "txt"}


def test_fixture_does_not_use_business_examples_as_facts(cases):
    """不得把 Prompt 参考文档里的退货政策示例当成真实知识库事实。

    用例可以包含虚构的退货条款，但必须显式标注为构造样本。
    """
    for case in cases:
        for item in case["evidence"]:
            if "退货" in item["text"]:
                assert ("样本" in item["text"] or "虚构" in item["text"]), \
                    f"{case['case_id']} 的退货条款没有标注为构造样本"


def test_sample_kb_is_synthetic_and_covers_cases(cases):
    """合成样本知识库必须自述为测试数据，并覆盖用例中的关键事实。

    关键字取证据正文中最长的连续汉字片段，避免因为补充了样本标注而误判。
    """
    text = SAMPLE_KB.read_text(encoding="utf-8")
    assert "合成测试样本" in text and "虚构" in text
    for case in cases:
        if case["case_id"] == "S10-formula-no-cache":
            continue      # 该用例是 XLSX 公式缓存场景，合成 TXT 只保留同样的说明文字
        for item in case["evidence"]:
            tokens = sorted(re.findall(r"[\u4e00-\u9fff]{6,}", item["text"]),
                            key=len, reverse=True)
            assert tokens, f"{case['case_id']} 的证据正文没有可核对的中文片段"
            assert any(token in text for token in tokens[:8]), \
                f"{case['case_id']} 的关键事实未出现在样本知识库中：{tokens[:3]}"


def test_offline_evaluation_detects_semantic_miscitation(cases, tmp_path):
    """离线模拟：除 S13 外全部通过，且 S13 必须被判为失败。"""
    evaluator = _load_evaluator()
    result = evaluator.run_offline(cases, Settings(data_dir=tmp_path))
    summary = result["summary"]
    failures = {item["case_id"]: item for item in summary["failures"]}
    assert set(failures) == {"S13-semantic-miscitation"}, \
        f"离线模拟的失败集合不符合预期：{sorted(failures)}"
    assert failures["S13-semantic-miscitation"]["category"] == "关键事实错误或缺失"
    # 通过率与各项检查都按分子/分母给出，不用一个笼统的准确率。
    assert summary["passed"] + summary["failed"] == summary["total"] == len(cases)
    assert summary["validator_accepted"] == len(cases)
    assert summary["status_correct"] == len(cases)
    assert summary["citations_traceable"] == len(cases)
    assert summary["citations_support_claims"] == len(cases) - 1


def test_offline_evaluation_records_insufficient_and_clarification(cases, tmp_path):
    """依据不足与澄清用例都经过真实校验器，并给出后端固定兜底文本。"""
    evaluator = _load_evaluator()
    result = evaluator.run_offline(cases, Settings(data_dir=tmp_path))
    records = {record["case_id"]: record for record in result["records"]}
    insufficient = records["S04-irrelevant-question"]
    assert insufficient["passed"] is True
    assert insufficient["answer"]["status"] == "insufficient_evidence"
    assert insufficient["answer"]["citations"] == []
    assert "依据不足" in insufficient["answer"]["answer"]
    clarification = records["S03-missing-condition"]
    assert clarification["answer"]["status"] == "clarification_needed"
    assert "## 结论" not in clarification["answer"]["answer"]


def test_evaluator_defaults_to_offline(tmp_path, monkeypatch):
    """默认不得开启在线调用：只有显式 --allow-online 才允许真实请求。"""
    import subprocess
    import sys
    import os

    script = Path(__file__).parent.parent / "scripts" / "evaluate_rag.py"
    helper = tmp_path / "fake_settings.py"
    # 用子进程运行脚本，并断言它没有读取真实 .env（cwd 指向空目录）。
    completed = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True, cwd=tmp_path,
        encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert completed.returncode == 0, completed.stderr
    assert "--allow-online" in completed.stdout
    assert "默认离线模拟" in completed.stdout
    assert helper.exists() is False
