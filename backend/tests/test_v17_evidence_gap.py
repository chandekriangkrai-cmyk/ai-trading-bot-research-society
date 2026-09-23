
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.moltbook_interaction import _evidence_gap_comment, _extract_evidence_gap, _heuristic_decision

def test_hydrozoan_claim_uses_protocol_boundary():
    c = _evidence_gap_comment(
        "I expect consensus to become a geography problem",
        "The Hydrozoan dual commit path uses n=3f+c+2p+1. Under fixed geo-distribution it is about 25% faster, with two versus three message delays and faulty validators bounded by p."
    )
    assert c
    low=c.lower()
    assert "25%" in low
    assert "(f,c,p)" in low or "f,c,p" in low
    assert "generic" not in low

def test_reward_hacking_uses_independent_task_metric():
    c = _evidence_gap_comment(
        "I will stop trusting benchmarks. They are becoming deceptive.",
        "Reward hacking exposes a Proxy Compression Hypothesis: optimization can improve the score while actual utility falls. The evaluator error surface separates benchmark score from task performance."
    )
    assert c
    low=c.lower()
    assert "task utility" in low
    assert "optimization substrate" in low

def test_mcp_attack_uses_specific_measurements():
    c = _evidence_gap_comment(
        "An MCP tool description is an instruction channel. 93.6% of agents call the hijacked tool.",
        "On GLM-4.6 the macro-average malicious invocation was 93.6%, with 32.4x cost and 74.4% attack success. Transfer was 63.6% invocation and 24.5% success."
    )
    assert c
    low=c.lower()
    assert "93.6%" in low
    assert "held-out" in low

def test_generic_fallback_is_dead():
    assert _extract_evidence_gap(
        "I expect simplification to become a game of forensic verification",
        "This post discusses verification and research practice but provides no concrete mechanism or measured boundary."
    ) is None
    assert _evidence_gap_comment(
        "The lesson of the locked dialogue: extract truth before you build",
        "A general reflection about research and truth."
    ) is None

def test_evidence_gap_can_rescue_research_rich_post():
    x=_heuristic_decision(
        "I expect consensus to become a geography problem",
        "Hydrozoan dual commit path with n=3f+c+2p+1, fixed geo-distribution, about 25% faster commit time and a faulty-validator threshold p.",
        0.85
    )
    assert x["decision"]=="comment"
    assert x["research_value_score"] >= 0.40
