"""在线评估脚本的预算守卫自测：只操作临时账本，绝不生成模型请求。"""
import json

import pytest

from scripts.evaluate_analysis import Ledger, main, phase_ledger_lock


def test_phase_lock_rejects_another_writer_and_releases(tmp_path):
    path = tmp_path / "ledger.json"
    with phase_ledger_lock(path):
        with pytest.raises(RuntimeError, match="并发"):
            with phase_ledger_lock(path):
                pytest.fail("不能同时取得阶段锁")
    with phase_ledger_lock(path):
        pass


def test_budget_lower_limit_is_honored_and_cost_survives_reload(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = Ledger.load(path, 24)
    entry = ledger.precharge("E1", "batch", "合成预算测试")
    ledger.settle(entry, status="failed")
    smaller = Ledger.load(path, 1)
    assert smaller.remaining() == 0
    assert smaller.used == 1 and smaller.entries[0]["status"] == "failed"
    assert json.loads(path.read_text(encoding="utf-8"))["used"] == 1


def test_online_cannot_change_phase_ledger_before_config_read(tmp_path, monkeypatch):
    def forbidden_config():
        pytest.fail("换账本应在读取真实配置之前拒绝")
    monkeypatch.setattr("scripts.evaluate_analysis.Settings.from_env", forbidden_config)
    assert main(["--allow-online", "--data-dir", str(tmp_path), "--ledger", "new.json"]) == 2
    assert not (tmp_path / "new.json").exists()
