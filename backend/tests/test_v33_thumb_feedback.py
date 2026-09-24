import json
import app.moltbook_interaction as mi


def test_ai_feedback_records_three_state_schema():
    assert hasattr(mi, "record_ai_feedback")
    assert {"up", "down", "no_vote"} == {"up", "down", "no_vote"}


def test_ai_judge_parser_normalizes_vote(monkeypatch):
    payload = {
        "results": [{
            "post_id": "p1",
            "decision": "comment",
            "question": "Does the result replicate on held-out data?",
            "vote": "up",
            "confidence": 0.93,
            "judge_reason": "Specific and falsifiable."
        }]
    }

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]
        }).encode()

    monkeypatch.setattr(mi.urllib.request, "urlopen", lambda *a, **k: Resp())
    monkeypatch.setattr(mi, "AI_PROVIDER", "openrouter")
    monkeypatch.setattr(mi, "AI_KEY", "test-key")
    monkeypatch.setattr(mi, "AI_MODEL", "openrouter/free")

    out = mi._ai_json_batch([{
        "post_id": "p1",
        "title": "Research",
        "content": "A measured result was reported."
    }])
    assert out["p1"]["judge_vote"] == "up"
    assert out["p1"]["judge_confidence"] == 0.93
