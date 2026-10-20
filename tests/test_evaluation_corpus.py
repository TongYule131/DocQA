"""提升评估底座测试只证明资料工程与记账规则，不证明模型语义效果。"""
from copy import deepcopy
import json
import socket
import zipfile

import pytest

from app.evaluation_corpus import (
    CATEGORIES, CorpusError, FIXTURE_ROOT, contained_path, evaluate_saved_reviews,
    load_manifest, prepare_corpus, sha256, validate_manifest, verify_document,
)
from scripts.evaluate_improvement import main as evaluate_main
from scripts.prepare_evaluation_corpus import main as prepare_main


def engineering_review(scenario, **changes):
    review = {"scenario_id": scenario["id"], "mode": "engineering_fixture", "reviewer": "pytest fixture",
        "output_sha256": "0" * 64, "supported_fact_ids": scenario["required_fact_ids"],
        "input_evidence_fact_ids": scenario["required_fact_ids"], "answer_status": scenario["expected_status"],
        "fact_support_confirmed": True, "violations": []}
    review.update(changes)
    return review


def test_complete_manifest_source_license_and_split():
    manifest = load_manifest()
    assert len(manifest["documents"]) == 18
    assert sum(len(d["scenarios"]) for d in manifest["documents"]) == 144
    assert sum(d["split"] == "holdout" for d in manifest["documents"]) == 6
    assert {d["category"] for d in manifest["documents"]} == set(CATEGORIES)
    assert all(d["provenance"]["kind"] == "synthetic" for d in manifest["documents"])
    assert "not_independently_reviewed" in manifest["annotation_status"]
    assert {s["extraction_mode"] for d in manifest["documents"] for s in d["scenarios"] if s["task"] == "extraction"} == {"auto", "hybrid", "only"}


@pytest.mark.parametrize("document", load_manifest()["documents"], ids=lambda d: d["id"])
def test_real_format_hash_and_exact_source_locations(document):
    report = verify_document(FIXTURE_ROOT, document)
    assert report["source_facts"] == 10
    path = FIXTURE_ROOT / document["asset"]
    if document["format"] == "pdf":
        assert path.read_bytes().startswith(b"%PDF-")
    else:
        assert zipfile.is_zipfile(path)
    if document["category"] == "scan_mixed_pdf":
        assert report["raster_source_facts"] > 0
        assert report["raster_review"] == "requires_independent_ocr_or_visual_review"


@pytest.mark.parametrize("field", ["source_family", "template_family"])
def test_same_source_and_template_cannot_cross_split(field):
    manifest = deepcopy(load_manifest())
    manifest["documents"][2][field] = manifest["documents"][0][field]
    with pytest.raises(CorpusError, match="跨"):
        validate_manifest(manifest)


def test_path_traversal_and_wrong_hash_fail(tmp_path):
    with pytest.raises(CorpusError, match="越过"):
        contained_path(tmp_path, "../outside")
    doc = deepcopy(load_manifest()["documents"][0])
    doc["sha256"] = "0" * 64
    with pytest.raises(CorpusError, match="哈希"):
        verify_document(FIXTURE_ROOT, doc)


def test_wrong_locator_and_quote_fail():
    doc = deepcopy(load_manifest()["documents"][0])
    doc["facts"][0]["locator"]["page"] = 500
    with pytest.raises(CorpusError, match="页码"):
        verify_document(FIXTURE_ROOT, doc)
    doc = deepcopy(load_manifest()["documents"][0])
    doc["facts"][0]["quote"] = "这个句子从未存在"
    with pytest.raises(CorpusError, match="原文"):
        verify_document(FIXTURE_ROOT, doc)


def test_prepare_idempotent_no_calls_and_refuse_overwrite(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("离线准备不得发出网络或模型请求")
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setenv("DOCQA_DATA_DIR", "must-not-be-used-formal-directory")
    target = tmp_path / "isolated-corpus"
    first = prepare_corpus(target)
    second = prepare_corpus(target)
    assert first == second and first["provider_calls"] == 0
    assert first["semantic_acceptance"] == "not_executed"
    for doc in load_manifest()["documents"]:
        assert sha256(target / doc["asset"]) == doc["sha256"]
    tampered = target / load_manifest()["documents"][0]["asset"]
    tampered.write_bytes(b"preserve this changed document")
    with pytest.raises(CorpusError, match="拒绝覆盖"):
        prepare_corpus(target)
    assert tampered.read_bytes() == b"preserve this changed document"


def test_corpus_rejects_database_directory(tmp_path):
    (tmp_path / "existing.db").write_bytes(b"existing database, do not access")
    with pytest.raises(CorpusError, match="数据库目录"):
        prepare_corpus(tmp_path)
    assert (tmp_path / "existing.db").read_bytes() == b"existing database, do not access"


def test_zero_reviews_does_not_claim_144_passes():
    report = evaluate_saved_reviews(load_manifest(), [], split="all")
    assert report["overall"]["planned"] == 144
    assert report["overall"]["not_executed"] == 144
    assert report["overall"]["passed"] == report["overall"]["executed"] == 0
    assert report["overall"]["supported_fact_coverage"] is None
    assert report["provider_calls_this_run"] == 0


@pytest.mark.parametrize("violation", ["wrong_numeric_scope", "process_parameter_as_outcome", "exception_omitted", "citation_unsupported"])
def test_known_semantic_negative_must_fail_despite_full_fact_coverage(violation):
    manifest = load_manifest()
    scenario = manifest["documents"][0]["scenarios"][3]
    report = evaluate_saved_reviews(manifest, [engineering_review(scenario, violations=[violation])])
    assert report["overall"]["failed"] == 1
    assert report["overall"]["supported_fact_coverage"] == 1
    assert report["overall"]["passed"] == 0
    assert report["by_evidence_mode"]["live_provider_review"]["executed"] == 0


def test_coverage_uses_executed_denominator_and_missing_fact_fails():
    manifest = load_manifest()
    scenario = manifest["documents"][0]["scenarios"][2]
    review = engineering_review(scenario, supported_fact_ids=scenario["required_fact_ids"][:1])
    report = evaluate_saved_reviews(manifest, [review])
    assert report["overall"]["planned"] == 96
    assert report["overall"]["not_executed"] == 95
    assert report["overall"]["supported_fact_numerator"] == 1
    assert report["overall"]["required_fact_denominator_executed_only"] == 2
    assert report["overall"]["supported_fact_coverage"] == .5
    assert report["overall"]["failed"] == 1


def test_wrong_refusal_status_fails():
    manifest = load_manifest()
    scenario = manifest["documents"][0]["scenarios"][5]
    report = evaluate_saved_reviews(manifest, [engineering_review(scenario, answer_status="answered")])
    assert report["overall"]["failed"] == 1


def test_no_cross_split_cross_document_or_unseen_evidence():
    manifest = load_manifest()
    dev = manifest["documents"][0]["scenarios"][0]
    holdout = manifest["documents"][2]["scenarios"][0]
    with pytest.raises(CorpusError, match="越组"):
        evaluate_saved_reviews(manifest, [engineering_review(holdout)], split="dev")
    with pytest.raises(CorpusError, match="跨文档"):
        evaluate_saved_reviews(manifest, [engineering_review(dev, supported_fact_ids=holdout["required_fact_ids"])])
    with pytest.raises(CorpusError, match="实际输入"):
        evaluate_saved_reviews(manifest, [engineering_review(dev, input_evidence_fact_ids=[])])


def test_cli_offline_zero_calls_and_preserves_first_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: pytest.fail("CLI不得访问网络"))
    target = tmp_path / "corpus"
    assert prepare_main(["--output-dir", str(target)]) == 0
    assert evaluate_main(["--corpus-dir", str(target), "--split", "all"]) == 0
    report = json.loads((target / "evaluation-all.json").read_text(encoding="utf-8"))
    assert report["overall"]["not_executed"] == 144
    assert evaluate_main(["--corpus-dir", str(target), "--split", "all"]) == 2
    assert "报告已存在" in capsys.readouterr().out
