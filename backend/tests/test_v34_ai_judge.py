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
