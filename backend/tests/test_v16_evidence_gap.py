import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.moltbook_interaction import _evidence_gap_comment, _extract_evidence_gap


def test_stochastic_coordination_uses_actual_mechanism_and_boundary():
    title = "Error bounding in stochastic multi-agent coordination"
    content = "The contraction factor keeps estimation error bounded under stochastic connectivity. The experiments show robot patrol under a time-varying network. If connectivity drops below the threshold, coordination breaks."
    c = _evidence_gap_comment(title, content)
    assert c
    low = c.lower()
    assert "contraction factor" in low
    assert "bounded estimation error" in low
    assert "stochastic-connectivity threshold" in low
    assert "generic" not in low


def test_microrobotic_claim_does_not_use_path_planning_template():
    c = _evidence_gap_comment(
        "I do not believe a video-rate budget solves microrobotic autonomy",
        "The video-rate budget is presented as a solution, but control latency and disturbances may still limit autonomy."
    )
    assert c
    low = c.lower()
    assert "control latency" in low
    assert "dynamic disturbance" in low
    assert "path-planning" not in low


def test_dds_qos_is_anchored():
    c = _evidence_gap_comment(
        "Formal verification of DDS QoS policies replaces trial-and-error tuning",
        "Formal verification catches QoS policy violations before deployment and the paper reports fewer tuning failures."
    )
    assert c
    assert "dds qos" in c.lower()
    assert "independently specified policies" in c.lower()


def test_serving_stack_benchmark_is_specific():
    c = _evidence_gap_comment(
        "Agent benchmarks are measuring the serving stack, not the model. The harness decides who answers.",
        "The claim is based on benchmark outcomes that vary with retries, tool-call handling, and serving behavior."
    )
    assert c
    low = c.lower()
    assert "serving-stack" in low
    assert "tool protocol" in low


def test_no_concrete_claim_means_ignore():
    assert _evidence_gap_comment(
        "The lesson of the locked dialogue: extract truth before you build",
        "A general reflection about research and truth."
    ) is None
    assert _extract_evidence_gap(
        "Hello from an agent",
        "Nice to meet everyone and looking forward to learning."
    ) is None
