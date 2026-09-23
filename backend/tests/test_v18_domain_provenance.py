from app.moltbook_interaction import _evidence_gap_comment, _comment_provenance_safe


def test_graph_post_cannot_get_trading_comment():
    title = "I will stop trusting local neighbors. Boundaries need structure."
    content = "DISCO uses diffusion-induced spatial attention, sparse multi-head attention and Bernoulli-Poisson edge reconstruction for overlapping community detection."
    comment = _evidence_gap_comment(title, content)
    assert comment is None


def test_benchmark_post_stays_in_benchmark_domain():
    title = "A benchmark that drops tool failures is measuring a different agent"
    content = "Removing tool timeouts and permission denials from the denominator inflates completion rate. Every attempted run should count."
    comment = _evidence_gap_comment(title, content)
    assert comment is not None
    assert "benchmark" in comment.lower() or "tool" in comment.lower()
    assert "backtest" not in comment.lower()
    assert "execution costs" not in comment.lower()


def test_prediction_market_stays_in_market_domain():
    title = "Dota 2 Match Odds Collapse to 0.05%: Is This Certainty or Market Capture?"
    content = "Prediction market odds reached 99.95%, volume was $756k, liquidity was constrained, and order-flow transparency is missing."
    comment = _evidence_gap_comment(title, content)
    assert comment is not None
    assert "backtest" not in comment.lower()
    assert "market" in comment.lower() or "liquidity" in comment.lower() or "order" in comment.lower()


def test_stale_comment_fails_provenance():
    title = "Agent benchmark serving stack"
    content = "Retries and tool calls change reported completion rate; use a matched serving stack."
    stale = "For backtest performance, what evidence would directly test reported strategy result using untouched chronological period, and does the claim hold when testing whether the result survives an untouched chronological period with explicit execution costs?"
    assert not _comment_provenance_safe(title, content, stale)
