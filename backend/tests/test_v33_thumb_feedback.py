import app.moltbook_interaction as mi
from app.research_models import MoltbookInteraction, MoltbookCommentFeedback
from app.database import engine, SessionLocal


def test_feedback_examples_empty_without_table_breaks_ai_prompt(monkeypatch):
    assert mi._feedback_examples() == {"up": [], "down": []}


def test_feedback_examples_reads_up_and_down(monkeypatch):
    MoltbookInteraction.__table__.create(bind=engine, checkfirst=True)
    MoltbookCommentFeedback.__table__.create(bind=engine, checkfirst=True)
    db=SessionLocal()
    try:
        db.query(MoltbookCommentFeedback).delete()
        db.commit()
        db.add(MoltbookInteraction(post_id="p1", comment_id="c1", content="Good question?", status="posted", direction="outbound"))
        db.add(MoltbookInteraction(post_id="p2", comment_id="c2", content="Bad question?", status="posted", direction="outbound"))
        db.commit()
        db.add(MoltbookCommentFeedback(comment_id="c1", post_id="p1", rating="up", comment_text="Good question?", post_title="Research A"))
        db.add(MoltbookCommentFeedback(comment_id="c2", post_id="p2", rating="down", comment_text="Bad question?", post_title="Research B"))
        db.commit()
    finally:
        db.close()
    out=mi._feedback_examples()
    assert out["up"][0]["comment"] == "Good question?"
    assert out["down"][0]["comment"] == "Bad question?"


def test_feedback_budget_is_not_part_of_feedback_path():
    out=mi._feedback_examples()
    assert isinstance(out, dict)
