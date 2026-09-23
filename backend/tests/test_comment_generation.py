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



def test_claim_aware_comments_track_specific_post_focus():
    parser = _contextual_research_comment(
        "The replication bottleneck may be your JSON parser",
        "A parser change alters replication outcomes on ambiguous JSON inputs.",
    )
    vla = _contextual_research_comment(
        "Internal VLA representations encode failure signals the controller ignores",
        "The controller ignores internal failure signals during evaluation.",
    )
    historical = _contextual_research_comment(
        "Historical style is not historical evidence",
        "Historical-style patterns may not be historical effects.",
    )
    assert "parser" in parser.lower()
    assert "controller" in vla.lower()
    assert "historical" in historical.lower()
    assert len({parser, vla, historical}) == 3


def test_v12_domain_claims_do_not_collapse_to_agent_template():
    path = _contextual_research_comment(
        "Distinguishing path planning from true autonomy",
        "The system plans routes but may not recover autonomously from unexpected obstacles.",
    )
    resilience = _contextual_research_comment(
        "Resilience is not a discrete state",
        "The system's recovery behavior varies under perturbations and faults.",
    )
    simulation = _contextual_research_comment(
        "I will stop trusting photorealism",
        "Policy-oriented simulation preserves curb geometry and temporal consistency rather than appearance.",
    )
    assert path != resilience != simulation
    assert "autonomy" in path.lower()
    assert "resilience" in resilience.lower()
    assert "simulation" in simulation.lower()
