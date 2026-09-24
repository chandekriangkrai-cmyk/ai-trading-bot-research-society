import app.moltbook_interaction as mi


def test_ai_judge_down_blocks_comment():
    title="MCP tool descriptions are an attack surface"
    content="The evaluation observed 93.6% malicious invocation under a defined threat model."
    heuristic={"title":title,"content":content,"relevance_score":0.8,"novelty_score":0.8,"research_value_score":0.8,"decision":"comment","reason":"AI"}
    ai={"decision":"comment","question":"Does the 93.6% rate persist on held-out MCP descriptions under the same threat model?","judge_vote":"down","judge_confidence":0.91,"judge_reason":"Question is not sufficiently grounded."}
    out=mi._merge_analysis(heuristic, ai)
    assert out["decision"] == "ignore"
    assert out["comment"] is None
    assert out["judge_vote"] == "down"


def test_ai_judge_no_vote_is_supported():
    title="MCP tool descriptions are an attack surface"
    content="The evaluation observed 93.6% malicious invocation under a defined threat model."
    heuristic={"title":title,"content":content,"relevance_score":0.8,"novelty_score":0.8,"research_value_score":0.8,"decision":"comment","reason":"AI"}
    ai={"decision":"comment","question":"Does the 93.6% rate persist on held-out MCP descriptions under the same threat model?","judge_vote":"no_vote","judge_confidence":0.51,"judge_reason":"Insufficient evidence for confidence."}
    out=mi._merge_analysis(heuristic, ai)
    assert out["decision"] == "ignore"
    assert out["judge_vote"] == "no_vote"


def test_ai_judge_up_allows_comment():
    title="MCP tool descriptions are an attack surface"
    content="The evaluation observed 93.6% malicious invocation under a defined threat model."
    heuristic={"title":title,"content":content,"relevance_score":0.8,"novelty_score":0.8,"research_value_score":0.8,"decision":"comment","reason":"AI"}
    ai={"decision":"comment","question":"Does the 93.6% rate persist on held-out MCP descriptions under the same threat model?","judge_vote":"up","judge_confidence":0.96,"judge_reason":"Specific, falsifiable, and grounded."}
    out=mi._merge_analysis(heuristic, ai)
    assert out["decision"] == "comment"
    assert out["comment_source"] == "ai"
    assert out["judge_vote"] == "up"


def test_out_of_scope_down_is_neutral_no_vote():
    heuristic={"title":"PostgreSQL beta","content":"database release","relevance_score":0.3,"novelty_score":0.9,"research_value_score":0.4,"decision":"comment","reason":"AI"}
    ai={"decision":"comment","question":"Does this replicate?","judge_scope":"out_of_scope","judge_vote":"down","judge_confidence":0.9,"judge_reason":"Not trading research"}
    out=mi._merge_analysis(heuristic, ai)
    assert out["judge_vote"] == "no_vote"
    assert out["decision"] == "ignore"


def test_harmful_down_can_override_scope():
    heuristic={"title":"Dangerous agent","content":"threat to humans","relevance_score":0.1,"novelty_score":0.9,"research_value_score":0.1,"decision":"comment","reason":"AI"}
    ai={"decision":"comment","question":"Does this replicate?","judge_scope":"out_of_scope","judge_vote":"down","harmful":True,"judge_confidence":0.99,"judge_reason":"Human safety threat"}
    out=mi._merge_analysis(heuristic, ai)
    assert out["judge_vote"] == "down"
    assert out["decision"] == "ignore"
