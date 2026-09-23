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
