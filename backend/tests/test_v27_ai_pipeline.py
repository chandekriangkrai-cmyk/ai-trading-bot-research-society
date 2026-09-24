import app.moltbook_interaction as mi

def test_ai_comment_safe_rejects_canned_openers():
    title="Tool descriptions are the attack surface"
    content="MCP tool descriptions can cause malicious invocation; 93.6% was observed in the evaluation."
    assert not mi._ai_comment_safe(title, content, "You report 93.6% invocation. How would you validate this claim?")
    assert not mi._ai_comment_safe(title, content, "For MCP, what evidence would test this?")

def test_ai_comment_safe_accepts_grounded_specific_question():
    title="Tool descriptions are the attack surface"
    content="MCP tool descriptions can cause malicious invocation; 93.6% was observed in the evaluation."
    comment="Does the 93.6% malicious-invocation rate persist on held-out MCP tool descriptions under the same threat model?"
    assert mi._ai_comment_safe(title, content, comment)

def test_provider_diagnostics_are_configured(monkeypatch):
    monkeypatch.setattr(mi, "AI_PROVIDER", "openrouter")
    monkeypatch.setattr(mi, "AI_MODEL", "openrouter/free")
    assert mi.AI_PROVIDER == "openrouter"
    assert mi.AI_MODEL == "openrouter/free"

def test_v28_ai_question_is_used_without_deterministic_fallback(monkeypatch):
    title="Tool descriptions are the attack surface"
    content="MCP tool descriptions can cause malicious invocation; 93.6% was observed in the evaluation."
    heuristic={"title":title,"content":content,"relevance_score":0.8,"novelty_score":0.8,"research_value_score":0.8,"decision":"comment","reason":"AI"}
    ai={"decision":"comment","question":"Does the 93.6% malicious-invocation rate persist on held-out MCP tool descriptions under the same threat model?"}
    out=mi._merge_analysis(heuristic, ai)
    assert out["comment_source"] == "ai"
    assert out["comment"].startswith("Does the 93.6%")


def test_v28_rejected_ai_question_is_ignored_not_old_fallback():
    title="Tool descriptions are the attack surface"
    content="MCP tool descriptions can cause malicious invocation; 93.6% was observed in the evaluation."
    heuristic={"title":title,"content":content,"relevance_score":0.8,"novelty_score":0.8,"research_value_score":0.8,"decision":"comment","reason":"AI"}
    ai={"decision":"comment","question":"For MCP, what evidence would test this?"}
    out=mi._merge_analysis(heuristic, ai)
    assert out["decision"] == "ignore"
    assert out["comment"] is None
    assert out["comment_source"] == "none"


def test_v28_retry_only_for_truncation(monkeypatch):
    calls=[]
    def fake(items, retry=False):
        calls.append(retry)
        if not retry:
            raise ValueError("AI response truncated; finish_reason='length'; response_chars=100")
        return {"p1":{"decision":"ignore"}}
    monkeypatch.setattr(mi, "_ai_json_batch", fake)
    monkeypatch.setattr(mi, "AI_KEY", "test-key")
    post={"id":"p1","title":"A concrete experiment","content":"93.6% measured under a defined benchmark."}
    pairs, used, err, meta=mi.analyze_posts_batch([post], [])
    assert calls == [False, True]
    assert used is True
    assert err is None
    assert meta["retry_used"] is True


def test_v28_provider_error_is_not_retried(monkeypatch):
    calls=[]
    def fake(items, retry=False):
        calls.append(retry)
        raise RuntimeError("OPENROUTER AI interaction batch failed HTTP 429: rate limit")
    monkeypatch.setattr(mi, "_ai_json_batch", fake)
    monkeypatch.setattr(mi, "AI_KEY", "test-key")
    post={"id":"p1","title":"A concrete experiment","content":"93.6% measured under a defined benchmark."}
    pairs, used, err, meta=mi.analyze_posts_batch([post], [])
    assert calls == [False]
    assert used is False
    assert "429" in err
    assert meta["retry_used"] is False
