import inspect
import app.moltbook_interaction as mi


def test_v42_4_budget_defaults_and_hard_cap():
    src = inspect.getsource(mi.discover_and_analyze)
    assert 'MOLTBOOK_AI_REQUEST_BUDGET' in inspect.getsource(mi._configured_ai_request_budget)
    assert mi.AI_REQUEST_BUDGET_MAX == 50
    assert mi.AI_BATCH_SIZE_MAX == 2


def test_v42_4_budget_env_cannot_exceed_50(monkeypatch):
    monkeypatch.setenv("MOLTBOOK_AI_REQUEST_BUDGET", "999")
    assert mi._configured_ai_request_budget() == 50


def test_v42_4_batch_size_env_cannot_exceed_2(monkeypatch):
    monkeypatch.setenv("MOLTBOOK_AI_BATCH_SIZE", "999")
    assert mi._configured_batch_size() == 2


def test_v42_3_ai_output_caps_are_compact():
    src = inspect.getsource(mi._ai_json_batch)
    assert '"360"' in src
    assert "220" in src
