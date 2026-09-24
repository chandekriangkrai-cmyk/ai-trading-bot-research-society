import app.moltbook_interaction as mi


def test_v30_ai_guard_allows_one_distinctive_anchor_without_evidence_gap():
    title="I will stop treating safety as a pointwise constraint"
    content="P-CBFs use a finite prediction horizon. FlowBarrier P-CBF reduced to a quadratic program and achieved zero safety violations across 100 trials on a nonholonomic ground robot."
    comment="Can the zero-violation result for P-CBFs replicate across independent environments with sensor noise and model mismatch?"
    assert mi._ai_comment_safe(title, content, comment)


def test_v30_ai_guard_still_rejects_foreign_domain():
    title="Arabic NER architectures"
    content="The study compares rule-based, ML and deep-learning methods for Arabic named entity recognition."
    comment="Can the trading strategy survive an untouched chronological period with spread costs?"
    assert not mi._ai_comment_safe(title, content, comment)


def test_v30_recursive_split_reaches_single_posts(monkeypatch):
    calls=[]
    def fake(items, retry=False):
        calls.append((len(items), retry))
        if len(items) > 1:
            raise ValueError("AI response truncated; finish_reason='length'; response_chars=0")
        return {str(items[0]["post_id"]): {"decision":"ignore"}}
    monkeypatch.setattr(mi, "_ai_json_batch", fake)
    monkeypatch.setattr(mi, "AI_KEY", "test-key")
    posts=[{"id":f"p{i}","title":"experiment","content":"93.6% measured benchmark"} for i in range(5)]
    pairs, used, err, meta=mi.analyze_posts_batch(posts, [])
    assert used is True
    assert err is None
    assert len(pairs) == 5
    assert any(n == 1 and retry is False for n,retry in calls)
    assert any(n == 2 and retry is True for n,retry in calls)
