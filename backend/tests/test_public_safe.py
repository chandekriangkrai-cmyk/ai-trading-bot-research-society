import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.public_safety import sanitize_public_text, sanitize_public_payload

def test_text_hides_internal_ids_and_secrets():
    s = 'experiment_id=abc123 result_id=def456 post_id=ghi789 moltbook_xxxxxxxxxxxxxxxxx'
    out = sanitize_public_text(s)
    assert 'abc123' not in out
    assert 'def456' not in out
    assert 'ghi789' not in out

def test_payload_hides_internal_keys_by_default():
    old = os.environ.get("PUBLIC_EXPOSE_INTERNAL_IDS")
    os.environ["PUBLIC_EXPOSE_INTERNAL_IDS"] = "false"
    try:
        out = sanitize_public_payload({
            "experiment_id": "private-exp",
            "result_id": "private-result",
            "post_id": "public-post-id",
            "title": "Research Update"
        })
        assert "experiment_id" not in out
        assert "result_id" not in out
        assert "post_id" not in out
        assert out["title"] == "Research Update"
    finally:
        if old is None:
            os.environ.pop("PUBLIC_EXPOSE_INTERNAL_IDS", None)
        else:
            os.environ["PUBLIC_EXPOSE_INTERNAL_IDS"] = old

def test_public_record_label():
    # Contract test: public build_post must use a public research label.
    src = open("app/api/moltbook.py", encoding="utf-8").read()
    assert 'Experiment: Public Research Record' in src
