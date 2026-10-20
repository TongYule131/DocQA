"""阶段预算在实际HTTP边界计数；不访问网络或正式资料。"""
from concurrent.futures import ThreadPoolExecutor
import json

import httpx
import pytest

from app.evaluation_budget import BudgetTransport, EvaluationBudget, EvaluationStopped


def budget(tmp_path, limit=2):
    return EvaluationBudget(tmp_path / "ledger.db", limit=limit, fingerprint="frozen")


def test_limit_persists_and_failed_requests_are_not_refunded(tmp_path):
    ledger = budget(tmp_path)
    n = ledger.reserve(role="index", context="a", content=b"input")
    ledger.settle(n, status="failed", elapsed_ms=4, http_status=401)
    restored = budget(tmp_path)
    n = restored.reserve(role="query", context="b", content=b"question")
    restored.settle(n, status="succeeded", elapsed_ms=3)
    with pytest.raises(EvaluationStopped, match="上限"):
        restored.reserve(role="generation", context="b", content=b"prompt")
    assert restored.report()["used"] == 2


def test_inflight_and_uncertain_calls_block_automatic_continuation(tmp_path):
    ledger = budget(tmp_path)
    n = ledger.reserve(role="generation", context="a", content=b"input")
    with pytest.raises(EvaluationStopped): budget(tmp_path).reserve(role="query", context="b", content=b"")
    ledger.settle(n, status="uncertain", elapsed_ms=1)
    with pytest.raises(EvaluationStopped): budget(tmp_path).reserve(role="query", context="b", content=b"")


def test_frozen_limit_and_configuration_cannot_be_replaced(tmp_path):
    budget(tmp_path)
    for limit, fingerprint in ((3, "frozen"), (1, "frozen"), (2, "changed")):
        with pytest.raises(EvaluationStopped):
            EvaluationBudget(tmp_path / "ledger.db", limit=limit, fingerprint=fingerprint)


def test_concurrent_precharge_allows_only_one_inflight_call(tmp_path):
    ledger = budget(tmp_path, 40)
    def attempt(_):
        try: return ledger.reserve(role="query", context="parallel", content=b"input")
        except EvaluationStopped: return None
    with ThreadPoolExecutor(max_workers=8) as pool: values = list(pool.map(attempt, range(8)))
    assert values.count(1) == 1 and ledger.report()["used"] == 1


def test_physical_transport_counts_each_batch_and_sanitizes_metadata(tmp_path):
    ledger = budget(tmp_path)
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "safe-id", "usage": {"total_tokens": 7, "secret": "discard"},
            "choices": [{"finish_reason": "stop", "message": {"reasoning_content": "must not save"}}]})
    transport = BudgetTransport(ledger, role="index", context="doc-a", inner=httpx.MockTransport(response))
    with httpx.Client(transport=transport) as client:
        for _ in range(2): client.post("https://offline.invalid/embeddings", json={"input": ["synthetic"]})
        with pytest.raises(EvaluationStopped): client.post("https://offline.invalid/embeddings", json={"input": []})
    assert len(calls) == 2
    report = ledger.report()
    assert report["calls"][0]["metadata"]["usage"] == {"total_tokens": 7}
    assert "must not save" not in json.dumps(report) and "secret" not in json.dumps(report)


def test_network_failure_reserves_once_and_remains_uncertain(tmp_path):
    ledger = budget(tmp_path)
    def fail(request): raise httpx.ReadTimeout("not logged", request=request)
    with httpx.Client(transport=BudgetTransport(ledger, role="generation", context="q1", inner=httpx.MockTransport(fail))) as client:
        with pytest.raises(httpx.ReadTimeout): client.post("https://offline.invalid/test", content=b"input")
    assert ledger.report()["used"] == 1 and ledger.report()["calls"][0]["status"] == "uncertain"
