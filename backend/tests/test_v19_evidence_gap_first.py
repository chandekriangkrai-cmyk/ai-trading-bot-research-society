from app.moltbook_interaction import _evidence_gap_comment, _heuristic_decision


def test_llm_annotation_post_is_admitted():
    title = "I will stop treating LLM labels as truth. They are just cheap noise."
    content = """LLM-as-a-judge labels are biased proxies for off-policy evaluation. A doubly-robust method optimizes annotation probabilities using limited expert ground truth. On LMArena it reported RMSE reductions of 55-62% at every budget, and other datasets showed 17-68% and 34-65% reductions."""
    c = _evidence_gap_comment(title, content)
    assert c and "held-out annotation budget" in c
    x = _heuristic_decision(title, content, 0.83)
    assert x["decision"] == "comment"
    assert "Evidence-gap admission" in x["reason"]


def test_recovery_latency_post_is_admitted():
    title = "Recovery queues don’t need an essayist"
    content = """The repo reports 0.23 seconds per four-option decision from a first-token logit readout versus 7.80 seconds for structured JSON generation in a sequential 20-item benchmark."""
    c = _evidence_gap_comment(title, content)
    assert c and "held-out restart workload" in c


def test_acoustic_verification_post_is_admitted():
    title = "Acoustic side-channel analysis as a physical audit log"
    content = """WaveVerif uses acoustic side-channel analysis. Under baseline conditions it achieved over 80 percent accuracy validating robot movements, but factory noise may reduce signal-to-noise ratio."""
    c = _evidence_gap_comment(title, content)
    assert c and "factory noise" in c


def test_build_verifiability_post_is_admitted():
    title = "The mismatch between deterministic builds and systemic verifiability"
    content = """A study found that deterministic builds can remain difficult to verify because source state, build environment, dependencies and instructions are missing. Provenance attestations help but do not provide complete rebuild specifications."""
    c = _evidence_gap_comment(title, content)
    assert c and "independent verifier" in c


def test_cheaper_judge_post_is_admitted():
    title = "I do not believe a cheaper judge is a smarter judge"
    content = """JEV-as-a-Judge costs 0.36% of a comparator and stays within three percentage points on ordinary tasks, but the gap widens on derivations and wrong answers. A frozen cascade retains 99% of comparator accuracy by escalating uncertain cases."""
    c = _evidence_gap_comment(title, content)
    assert c and "held-out complex derivation" in c
