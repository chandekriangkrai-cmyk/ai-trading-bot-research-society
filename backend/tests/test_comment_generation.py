import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.moltbook_interaction import _contextual_research_comment, _comment_similarity


def test_contextual_comments_differ_by_topic():
    benchmark = _contextual_research_comment(
        "Benchmark comparison",
        "Compare the strategy against a buy-and-hold baseline over the same period.",
    )
    agent = _contextual_research_comment(
        "Agent validation",
        "The AI agent was evaluated on unseen cases after development.",
    )
    assert benchmark != agent
    assert "baseline" in benchmark.lower()
    assert "evaluation" in agent.lower()


def test_near_duplicate_similarity_is_high():
    a = "What baseline and evaluation period are you using for the comparison, and are the same data, costs, and success criteria applied to both methods?"
    b = "What baseline and evaluation period are you using for the comparison, and are the same data, costs, and success criteria applied to both methods?"
    assert _comment_similarity(a, b) == 1.0
