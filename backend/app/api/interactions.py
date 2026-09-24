from __future__ import annotations

import os
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
import json
from app.moltbook_interaction import discover_and_analyze, run_cycle, post_comment
from app.database import SessionLocal
from app.research_models import MoltbookInteractionLead, MoltbookInteraction, MoltbookInteractionFeedback

router=APIRouter(prefix="/moltbook-interactions", tags=["Moltbook AI↔AI Research Interaction"])



class FeedbackRequest(BaseModel):
    comment_id: str = Field(..., description="Moltbook comment ID", examples=["7b836b34-bebc-47c6-824f-e92fa5d6a028"])
    rating: str = Field(..., pattern="^(up|down|no_vote)$", description="up, down, or no_vote", examples=["up"])


def _auto_enabled() -> bool:
    return os.getenv("MOLTBOOK_AI_INTERACTION_AUTO_COMMENT_ENABLED", "false").lower() in {"1","true","yes","on"}


@router.post("/scan")
async def scan(limit: int = Query(20, ge=1, le=100), min_relevance: float = Query(0.30, ge=0, le=1)):
    try:
        return await __import__("asyncio").to_thread(discover_and_analyze, limit, min_relevance)
    except Exception as exc:
        raise HTTPException(502, {"message":"Moltbook interaction scan failed","error":str(exc)})


@router.post("/cycle")
async def cycle(auto_comment: bool | None = None, max_comments: int = Query(1, ge=0, le=1), min_relevance: float = Query(0.30, ge=0, le=1)):
    enabled = _auto_enabled() if auto_comment is None else auto_comment
    try:
        return await __import__("asyncio").to_thread(run_cycle, enabled, max_comments, min_relevance)
    except Exception as exc:
        raise HTTPException(502, {"message":"Moltbook interaction cycle failed","error":str(exc)})


@router.get("/leads")
def leads(limit: int = Query(50, ge=1, le=200)):
    db=SessionLocal()
    try:
        rows=db.query(MoltbookInteractionLead).order_by(MoltbookInteractionLead.updated_at.desc()).limit(limit).all()
        return {"count":len(rows),"leads":[{
            "id":r.id,"post_id":r.post_id,"author":r.author,"title":r.title,"url":r.url,
            "relevance_score":r.relevance_score,"novelty_score":r.novelty_score,
            "research_value_score":r.research_value_score,"decision":r.decision,"status":r.status,
            "reason":r.reason,"draft_comment":r.draft_comment,
            "created_at":r.discovered_at.isoformat() if r.discovered_at else None,
            "updated_at":r.updated_at.isoformat() if r.updated_at else None,
        } for r in rows]}
    finally: db.close()


@router.post("/leads/{lead_id}/approve")
async def approve(lead_id: str):
    db=SessionLocal()
    try:
        row=db.query(MoltbookInteractionLead).filter(MoltbookInteractionLead.id==lead_id).first()
        if not row: raise HTTPException(404,"Interaction lead not found")
        if not row.draft_comment: raise HTTPException(409,"No draft comment exists")
        row.status="approved"
        db.commit()
        return {"status":"approved","lead_id":lead_id,"post_id":row.post_id,"comment":row.draft_comment}
    finally: db.close()


@router.post("/leads/{lead_id}/publish")
async def publish_lead(lead_id: str):
    db=SessionLocal()
    try:
        row=db.query(MoltbookInteractionLead).filter(MoltbookInteractionLead.id==lead_id).first()
        if not row: raise HTTPException(404,"Interaction lead not found")
        if row.status not in {"approved","candidate"}: raise HTTPException(409,{"message":"Lead must be approved before publish","status":row.status})
        content=row.draft_comment
        post_id=row.post_id
    finally: db.close()
    try:
        body=await __import__("asyncio").to_thread(post_comment,post_id,content)
    except Exception as exc:
        raise HTTPException(502,{"message":"Moltbook comment failed","error":str(exc)})
    db=SessionLocal()
    try:
        posted=body.get("comment",body) if isinstance(body,dict) else {}
        cid=str(posted.get("id")) if isinstance(posted,dict) and posted.get("id") else None
        db.add(MoltbookInteraction(post_id=post_id,comment_id=cid,direction="outbound",content=content,classification="research_question",status="posted",reason="Human-approved interaction"))
        row=db.query(MoltbookInteractionLead).filter(MoltbookInteractionLead.id==lead_id).first()
        row.status="commented"
        db.commit()
        return {"status":"posted","lead_id":lead_id,"post_id":post_id,"comment_id":cid,"response":body}
    finally: db.close()


@router.post("/feedback")
def feedback(payload: FeedbackRequest):
    """Manual feedback endpoint retained for human labels; AI Judge does not require it."""
    db=SessionLocal()
    try:
        row=db.query(MoltbookInteraction).filter(MoltbookInteraction.comment_id==payload.comment_id).order_by(MoltbookInteraction.created_at.desc()).first()
        if not row:
            raise HTTPException(404, "Comment interaction not found")
        fb=MoltbookInteractionFeedback(post_id=row.post_id, comment_id=payload.comment_id, rating=payload.rating, source="human", confidence=1.0, reason="Manual human feedback", criteria_json="{}")
        db.add(fb); db.commit()
        return {"status":"recorded","comment_id":payload.comment_id,"post_id":row.post_id,"rating":payload.rating,"ai_requests_used":0}
    finally: db.close()


@router.get("/feedback/summary")
def feedback_summary(limit: int = Query(100, ge=1, le=500)):
    db=SessionLocal()
    try:
        rows=db.query(MoltbookInteractionFeedback).order_by(MoltbookInteractionFeedback.created_at.desc()).limit(limit).all()
        counts={"up":0,"down":0,"no_vote":0}
        sources={"ai_judge":0,"human":0}
        items=[]
        for r in rows:
            counts[r.rating]=counts.get(r.rating,0)+1
            sources[r.source]=sources.get(r.source,0)+1
            items.append({"id":r.id,"post_id":r.post_id,"comment_id":r.comment_id,"rating":r.rating,"source":r.source,"confidence":r.confidence,"reason":r.reason,"created_at":r.created_at.isoformat() if r.created_at else None})
        return {"count":len(rows),"counts":counts,"sources":sources,"items":items}
    finally: db.close()
