import inspect
import app.moltbook_interaction as mi


def test_v39_budget_defaults():
    src = inspect.getsource(mi.discover_and_analyze)
    assert 'MOLTBOOK_AI_REQUEST_BUDGET", "50"' in src
    assert 'min(4, max(1, int(os.getenv("MOLTBOOK_AI_BATCH_SIZE", "4"))))' in src


def test_v39_ai_prompt_is_compact(monkeypatch):
    monkeypatch.setattr(mi, "AI_KEY", "")
    assert mi._ai_json_batch([]) == {}
    src = inspect.getsource(mi._ai_json_batch)
    assert '[:1800]' in src
    assert '"420"' in src
    assert "280" in src
    assert 'compact keys only' in src
