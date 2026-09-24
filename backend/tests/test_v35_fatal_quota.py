import app.moltbook_interaction as mi


def test_v35_openrouter_daily_quota_is_fatal(monkeypatch):
    calls = []

    def fake(items, retry=False):
        calls.append((len(items), retry))
        raise RuntimeError(
            'OPENROUTER AI interaction batch failed HTTP 429: '
            '{"error":{"code":429,"message":"Rate limit exceeded: free-models-per-day",'
            '"metadata":{"headers":{"X-RateLimit-Remaining":"0"},'
            '"limit_source":"openrouter_free_tier_daily"}}}'
        )

    monkeypatch.setattr(mi, "_ai_json_batch", fake)
    monkeypatch.setattr(mi, "AI_KEY", "test-key")
    monkeypatch.setattr(mi, "AI_PROVIDER", "openrouter")

    budget = {"limit": 48, "used": 0, "exhausted": False,
              "quota_exhausted": False, "quota_error": None}
    posts = [{"id": f"p{i}", "title": "experiment",
              "content": "93.6% measured benchmark"} for i in range(5)]

    pairs, used, err, meta = mi.analyze_posts_batch(posts, [], budget=budget)

    assert calls == [(5, False)]
    assert budget["used"] == 1
    assert budget["quota_exhausted"] is True
    assert used is False
    assert meta["quota_exhausted"] is True
    assert meta["ai_stop_reason"] == "openrouter_daily_quota"
    assert err and "free-models-per-day" in err


def test_v35_normal_truncation_still_retries(monkeypatch):
    calls = []

    def fake(items, retry=False):
        calls.append((len(items), retry))
        if not retry:
            raise ValueError("AI response truncated; finish_reason='length'; response_chars=0")
        return {str(items[0]["post_id"]): {"decision": "ignore"}} if len(items) == 1 else {
            str(x["post_id"]): {"decision": "ignore"} for x in items
        }

    monkeypatch.setattr(mi, "_ai_json_batch", fake)
    monkeypatch.setattr(mi, "AI_KEY", "test-key")
    monkeypatch.setattr(mi, "AI_PROVIDER", "openrouter")

    posts = [{"id": f"p{i}", "title": "experiment",
              "content": "93.6% measured benchmark"} for i in range(3)]
    pairs, used, err, meta = mi.analyze_posts_batch(posts, [])

    assert used is True
    assert err is None
    assert len(pairs) == 3
    assert calls[0] == (3, False)
    assert calls[1] == (3, True)


def test_v35_ai_judge_three_states_are_still_enforced():
    title = "MCP tool descriptions are an attack surface"
    content = "The evaluation observed 93.6% malicious invocation under a defined threat model."
    heuristic = {
        "title": title, "content": content, "relevance_score": 0.8,
        "novelty_score": 0.8, "research_value_score": 0.8,
        "decision": "comment", "reason": "AI"
    }
    for vote in ("up", "down", "no_vote"):
        ai = {
            "decision": "comment",
            "question": "Does the 93.6% rate persist on held-out MCP descriptions under the same threat model?",
            "judge_vote": vote, "judge_confidence": 0.9,
            "judge_reason": "Specific research judgment."
        }
        out = mi._merge_analysis(heuristic, ai)
        assert out["judge_vote"] == vote
        if vote == "up":
            assert out["decision"] == "comment"
        else:
            assert out["decision"] == "ignore"
