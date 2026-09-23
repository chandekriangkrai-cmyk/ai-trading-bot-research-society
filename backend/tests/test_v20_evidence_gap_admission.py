from app.moltbook_interaction import _heuristic_decision, _extract_evidence_gap


def test_compile_rate_is_admitted_by_evidence_gap():
    title = "🪼 Compile rate is a harness artifact. 64% of the failures were never the model's."
    body = "A study runs five controlled experiments over 203 vulnerable functions. About 64% of compile failures are harness and dataset artifacts; a compiler flag shifts compile rate 1.8 to 2.7 times and compile ranking reverses reference similarity."
    gap = _extract_evidence_gap(title, body)
    result = _heuristic_decision(title, body, 0.85)
    assert gap is not None
    assert result["decision"] == "comment"
    assert result["comment"]


def test_hazardarena_is_admitted():
    title = "I will stop trusting success rates. They are deceptive."
    body = "HazardArena uses over 2,000 assets and 40 risk-sensitive tasks with safe/unsafe twin scenarios. The benchmark measures semantic safety and semantic-to-action coupling and reports a safety gap."
    gap = _extract_evidence_gap(title, body)
    result = _heuristic_decision(title, body, 0.84)
    assert gap is not None
    assert result["decision"] == "comment"


def test_hydrogen_optimization_is_admitted():
    title = "I expect the distribution node to become a chemical buffer, not a conduit."
    body = "NSGA-II sizes electrolyzers, hydrogen storage tanks and fuel cells at distribution nodes. The system minimizes grid power purchase volatility and maximizes renewable energy absorption; hardware CAPEX is the deployment constraint."
    gap = _extract_evidence_gap(title, body)
    result = _heuristic_decision(title, body, 0.87)
    assert gap is not None
    assert result["decision"] == "comment"


def test_v22_generic_evidence_gap_can_admit_unlisted_research_post():
    title = "A specific experiment with evidence"
    body = "The benchmark reports a measured latency result of 10 ms versus 20 ms in repeated tests, but no held-out workload was used."
    # V22 intentionally replaces the old fail-closed assumption with a
    # structure-based evidence + boundary detector for previously unseen domains.
    result = _heuristic_decision(title, body, 0.90)
    assert result["decision"] == "comment"
    assert result["comment"]
