from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from app.database import SessionLocal
from app.research_models import Experiment, MoltbookPostLink, ResearchDiscussion
from app.api.moltbook import BASE, TIMEOUT, req
from app.research_discussion import generate_reply
from app.public_safety import sanitize_public_text, sanitize_public_payload

router = APIRouter(prefix="/research-discussion", tags=["Research AI Discussion"])


def _key():
    key = os.getenv("MOLTBOOK_API_KEY", "")
    if not key:
        raise HTTPException(503, "MOLTBOOK_API_KEY is not configured")
    return key


def _headers():
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}


def _extract_comments(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("comments", "data", "items"):
        value = body.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _comment_id(c: dict[str, Any]) -> str | None:
    return str(c.get("id")) if c.get("id") is not None else None


def _author(c: dict[str, Any]) -> str:
    a = c.get("author")
    if isinstance(a, dict):
        return str(a.get("name") or a.get("username") or "unknown")
    return str(a or c.get("author_name") or "unknown")


def _text(c: dict[str, Any]) -> str:
    return str(c.get("content") or c.get("body") or c.get("text") or "").strip()


def _parent(c: dict[str, Any]) -> str | None:
    value = c.get("parent_id") or c.get("parent_comment_id")
    return str(value) if value is not None else None


@router.get("/post/{post_id}/comments")
async def read_comments(post_id: str):
    status, body = await asyncio.to_thread(req, "GET", f"{BASE}/posts/{post_id}/comments", _headers())
    if status >= 400:
        raise HTTPException(502, {"message": "Moltbook comments request failed", "status_code": status, "response": body})
    comments = _extract_comments(body)
    return {"status": "ok", "post_id": post_id, "count": len(comments), "comments": comments}


@router.post("/research/{experiment_id}/comment-draft")
async def draft_reply(experiment_id: str, payload: dict[str, Any]):
    comment = str(payload.get("comment") or "").strip()
    if not comment:
        raise HTTPException(400, "Provide comment")
    author = str(payload.get("author") or "unknown")
    thread = str(payload.get("thread_context") or "")
    result = await asyncio.to_thread(generate_reply, experiment_id, comment, author, thread)
    if result.get("reply"):
        result["reply"] = sanitize_public_text(result["reply"])
    return sanitize_public_payload({"status": "drafted", "experiment_id": experiment_id, **result})


@router.post("/research/{experiment_id}/scan")
async def scan_discussion(experiment_id: str, post_id: str | None = Query(None), auto_reply: bool = Query(False), max_replies: int = Query(2, ge=0, le=10)):
    db = SessionLocal()
    try:
        e = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not e:
            raise HTTPException(404, "Experiment not found")
        link = db.query(MoltbookPostLink).filter(MoltbookPostLink.experiment_id == experiment_id).first()
        resolved_post = post_id or (link.post_id if link else None)
        if not resolved_post:
            raise HTTPException(404, "No Moltbook post is linked to this experiment")
    finally:
        db.close()

    status, body = await asyncio.to_thread(req, "GET", f"{BASE}/posts/{resolved_post}/comments", _headers())
    if status >= 400:
        raise HTTPException(502, {"message": "Moltbook comments request failed", "status_code": status, "response": body})
    comments = _extract_comments(body)
    results = []
    reply_count = 0

    for c in comments:
        cid = _comment_id(c)
        text = _text(c)
        if not cid or not text:
            continue
        db = SessionLocal()
        try:
            existing = db.query(ResearchDiscussion).filter(ResearchDiscussion.comment_id == cid).first()
            if existing:
                results.append({"comment_id": cid, "status": "already_processed", "decision": existing.decision, "reply_comment_id": existing.reply_comment_id})
                continue
            author = _author(c)
            parent = _parent(c)
            result = await asyncio.to_thread(generate_reply, experiment_id, text, author, "")
            if result.get("reply"):
                result["reply"] = sanitize_public_text(result["reply"])
            row = ResearchDiscussion(
                experiment_id=experiment_id,
                post_id=resolved_post,
                comment_id=cid,
                parent_id=parent,
                author=author,
                comment_text=text,
                classification=str(result.get("classification") or "other"),
                decision=str(result.get("decision") or "ignore"),
                draft_reply=result.get("reply"),
                reason=str(result.get("reason") or ""),
            )
            db.add(row)
            db.commit()
            item = {"comment_id": cid, "author": author, "classification": row.classification, "decision": row.decision, "reply": row.draft_reply}

            should_reply = auto_reply and row.decision == "reply" and row.draft_reply and reply_count < max_replies
            if should_reply:
                payload = {"content": row.draft_reply}
                if parent:
                    payload["parent_id"] = parent
                pstatus, pbody = await asyncio.to_thread(req, "POST", f"{BASE}/posts/{resolved_post}/comments", _headers(), payload)
                if pstatus >= 400:
                    row.decision = "reply_failed"
                    row.reason = (row.reason or "") + f" Moltbook reply failed with HTTP {pstatus}."
                    db.commit()
                    item["reply_status"] = "failed"
                    item["reply_response"] = pbody
                else:
                    posted = pbody.get("comment", pbody) if isinstance(pbody, dict) else {}
                    row.reply_comment_id = str(posted.get("id")) if isinstance(posted, dict) and posted.get("id") else None
                    row.decision = "replied"
                    db.commit()
                    reply_count += 1
                    item["reply_status"] = "posted"
                    item["reply_comment_id"] = row.reply_comment_id
            results.append(item)
        finally:
            db.close()

    return {"status": "scanned", "experiment_id": experiment_id, "post_id": resolved_post, "comments_seen": len(comments), "replies_posted": reply_count, "results": results}


@router.get("/research/{experiment_id}/discussions")
def list_discussions(experiment_id: str, limit: int = Query(50, ge=1, le=200)):
    db = SessionLocal()
    try:
        rows = db.query(ResearchDiscussion).filter(ResearchDiscussion.experiment_id == experiment_id).order_by(ResearchDiscussion.created_at.desc()).limit(limit).all()
        return {"experiment_id": experiment_id, "count": len(rows), "discussions": [
            {
                "id": r.id, "comment_id": r.comment_id, "parent_id": r.parent_id, "author": r.author,
                "comment": r.comment_text, "classification": r.classification, "decision": r.decision,
                "draft_reply": r.draft_reply, "reply_comment_id": r.reply_comment_id, "reason": r.reason,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows
        ]}
    finally:
        db.close()
