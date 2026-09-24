import app.moltbook_interaction as mi


def test_v36_default_budget_is_50(monkeypatch):
    monkeypatch.delenv("MOLTBOOK_AI_REQUEST_BUDGET", raising=False)
    monkeypatch.delenv("MOLTBOOK_AI_BATCH_SIZE", raising=False)
    # Discover the defaults from the cycle function without making network calls.
    # The actual constant behavior is asserted by source-level configuration below.
    import inspect
    src = inspect.getsource(mi.discover_and_analyze)
    assert 'MOLTBOOK_AI_REQUEST_BUDGET", "50"' in src
    assert 'MOLTBOOK_AI_BATCH_SIZE", "5"' in src


def test_v36_ai_prompt_is_compact(monkeypatch):
    monkeypatch.setattr(mi, "AI_KEY", "")
    assert mi._ai_json_batch([]) == {}
    import inspect
    src = inspect.getsource(mi._ai_json_batch)
    assert '[:2200]' in src
    assert '"520"' in src
