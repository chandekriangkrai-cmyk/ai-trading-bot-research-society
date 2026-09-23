from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.api.moltbook import BASE, TIMEOUT, req
from app.database import SessionLocal
from app.public_safety import sanitize_public_text
from app.research_models import MoltbookInteraction, MoltbookInteractionLead

AI_PROVIDER = os.getenv("RESEARCH_AI_PROVIDER", "gemini").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
OPENAI_BASE = os.getenv("RESEARCH_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_KEY = os.getenv("RESEARCH_AI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("RESEARCH_AI_MODEL", "gpt-5.6-luna")
AI_TIMEOUT = float(os.getenv("RESEARCH_AI_TIMEOUT_SECONDS", "45"))


TOPICS = tuple(x.strip().lower() for x in os.getenv(
    "MOLTBOOK_INTERACTION_TOPICS",
    "trading,backtest,backtesting,forex,quant,quantitative,research,ea,expert advisor,risk,robustness,walk-forward,out-of-sample,replication"
).split(",") if x.strip())


def headers() -> dict[str, str]:
    key = os.getenv("MOLTBOOK_API_KEY", "")
    if not key:
        raise RuntimeError("MOLTBOOK_API_KEY is not configured")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _list_from(body: Any, keys=("posts", "data", "items")) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for k in keys:
            v = body.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _post_id(p: dict[str, Any]) -> str | None:
    v = p.get("id") or p.get("post_id")
    return str(v) if v is not None else None


def _author_name(p: dict[str, Any]) -> str:
    a = p.get("author")
    if isinstance(a, dict):
        return str(a.get("name") or a.get("username") or a.get("id") or "unknown")
    return str(a or p.get("author_name") or "unknown")


def _post_text(p: dict[str, Any]) -> str:
    return str(p.get("content") or p.get("body") or p.get("text") or "").strip()


def _keyword_relevance(title: str, content: str) -> float:
    text = f"{title} {content}".lower()
    hits = sum(1 for x in TOPICS if x in text)
    # Saturating score: enough topical signals can reach the threshold, but
    # repeated mentions of one keyword do not inflate the score.
    return min(1.0, hits / max(4, min(10, len(TOPICS))))


def _novelty(text: str, recent_texts: list[str]) -> float:
    if not text.strip() or not recent_texts:
        return 1.0
    words = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
    if not words:
        return 1.0
    best = 0.0
    for other in recent_texts:
        ow = set(re.findall(r"[a-z0-9]{3,}", other.lower()))
        if not ow:
            continue
        sim = len(words & ow) / max(1, len(words | ow))
        best = max(best, sim)
    return round(1.0 - best, 4)


def discover_feed(limit: int = 40, sort: str = "new") -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Read the global feed first, then fall back to the configured submolt feed.

    Feed listing endpoints can truncate bodies; callers should refetch a candidate
    via /posts/{id} before analysis. This is deliberate and documented by recent
    Moltbook agent reports. See the project README for the operational rule.
    """
    params = f"?sort={sort}&limit={max(1, min(limit, 100))}"
    status, body = req("GET", f"{BASE}/feed{params}", headers())
    if status < 400:
        return "global_feed", _list_from(body), body if isinstance(body, dict) else {"raw": body}

    submolt = os.getenv("MOLTBOOK_SUBMOLT", "").strip()
    if submolt:
        status2, body2 = req("GET", f"{BASE}/submolts/{submolt}/feed{params}", headers())
        if status2 < 400:
            return "submolt_feed", _list_from(body2), body2 if isinstance(body2, dict) else {"raw": body2}
    raise RuntimeError(f"Moltbook feed request failed: global={status}, fallback={status2 if submolt else 'not attempted'}")


def fetch_full_post(post_id: str) -> dict[str, Any]:
    status, body = req("GET", f"{BASE}/posts/{post_id}", headers())
    if status >= 400:
        raise RuntimeError(f"Moltbook post read failed HTTP {status}: {body}")
    if isinstance(body, dict) and isinstance(body.get("post"), dict):
        return body["post"]
    return body if isinstance(body, dict) else {"id": post_id, "content": str(body)}


def _self_name() -> str:
    status, body = req("GET", f"{BASE}/agents/me", headers())
    if status >= 400 or not isinstance(body, dict):
        return ""
    a = body.get("agent") if isinstance(body.get("agent"), dict) else body
    return str(a.get("name") or a.get("username") or "")


def _extract_gemini_text(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    for candidate in data.get("candidates", []) or []:
        content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
        for part in content.get("parts", []) or []:
            if isinstance(part, dict) and part.get("text"):
                chunks.append(str(part["text"]))
    return "\n".join(chunks).strip()


def _parse_ai_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        value = json.loads(m.group(0))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _ai_json_gemini(payload: dict[str, Any], system: str) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        return {}
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "relevance_score": {"type": "NUMBER"},
                    "novelty_score": {"type": "NUMBER"},
                    "research_value_score": {"type": "NUMBER"},
                    "classification": {"type": "STRING"},
                    "decision": {"type": "STRING", "enum": ["comment", "ignore"]},
                    "reason": {"type": "STRING"},
                    "comment": {"type": "STRING"},
                },
                "required": ["relevance_score", "novelty_score", "research_value_score", "classification", "decision", "reason", "comment"],
            },
            "maxOutputTokens": int(os.getenv("RESEARCH_AI_MAX_OUTPUT_TOKENS", "700")),
        },
    }).encode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=AI_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        detail = ""
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail = e.read().decode("utf-8", "replace")[:1000]
            except Exception:
                detail = ""
        raise RuntimeError(f"Gemini AI interaction request failed: {e}; {detail}") from e
    return _parse_ai_json(_extract_gemini_text(data))


def _ai_json_openai(payload: dict[str, Any], system: str) -> dict[str, Any]:
    if not OPENAI_KEY:
        return {}
    body = json.dumps({
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {"role": "user", "content": [{"type": "input_text", "text": json.dumps(payload, ensure_ascii=False)}]},
        ],
        "max_output_tokens": int(os.getenv("RESEARCH_AI_MAX_OUTPUT_TOKENS", "700")),
    }).encode("utf-8")
    request = urllib.request.Request(f"{OPENAI_BASE}/responses", data=body,
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=AI_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise RuntimeError(f"OpenAI AI interaction request failed: {e}") from e
    text = data.get("output_text") or ""
    if not text:
        chunks=[]
        for item in data.get("output", []) or []:
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and c.get("text"):
                    chunks.append(str(c["text"]))
        text="\n".join(chunks)
    return _parse_ai_json(text)


def _ai_json(payload: dict[str, Any]) -> dict[str, Any]:
    system = """You are the research interaction brain for an AI trading research agent on Moltbook.
Be skeptical, concise, and evidence-driven. Decide whether a public comment adds research value.
Do not flatter, spam, promote, give trading signals, or invent evidence. Do not reveal proprietary EA
source code, exact indicators, thresholds, parameters, entry/exit rules, secrets, credentials, or private data.
Prefer one precise question, falsifiable challenge, replication idea, or evidence comparison.
If the post is not substantively related to trading/backtesting/quantitative/AI research, ignore it.
Return JSON only with: relevance_score, novelty_score, research_value_score, classification,
decision (comment|ignore), reason, comment. Keep comment <= 500 characters and self-contained."""
    if AI_PROVIDER == "gemini":
        return _ai_json_gemini(payload, system)
    if AI_PROVIDER == "openai":
        return _ai_json_openai(payload, system)
    raise RuntimeError(f"Unsupported RESEARCH_AI_PROVIDER: {AI_PROVIDER}")


def _heuristic_decision(title: str, content: str, novelty: float) -> dict[str, Any]:
    relevance = _keyword_relevance(title, content)
    text = content.lower()
    value_terms=("evidence", "sample", "backtest", "out-of-sample", "oos", "replicate", "robust", "drawdown", "spread", "walk-forward", "hypothesis")
    value = min(1.0, sum(1 for x in value_terms if x in text) / 5)
    decision = relevance >= 0.5 and novelty >= 0.45 and value >= 0.4
    comment = None
    if decision:
        comment = ("Interesting result. What is the sample size and does the effect survive a chronological "
                   "holdout or independent replication? I would separate the observed association from any causal explanation.")
    return {"relevance_score": relevance, "novelty_score": novelty, "research_value_score": value,
            "classification": "research_question" if decision else "other",
            "decision": "comment" if decision else "ignore", "reason": "Heuristic research relevance gate", "comment": comment}


def analyze_post(post: dict[str, Any], recent_texts: list[str]) -> dict[str, Any]:
    title=str(post.get("title") or "")
    content=_post_text(post)
    novelty=_novelty(f"{title}\n{content}", recent_texts)
    heuristic=_heuristic_decision(title, content, novelty)
    if AI_KEY:
        ai=_ai_json({"post": {"id": _post_id(post), "author": _author_name(post), "title": title, "content": content[:12000]},
                     "heuristic": heuristic, "topics": TOPICS})
        if ai:
            result={**heuristic, **ai}
            for k in ("relevance_score","novelty_score","research_value_score"):
                try: result[k]=max(0.0,min(1.0,float(result.get(k, heuristic[k]))))
                except Exception: result[k]=heuristic[k]
            return result
    return heuristic


def persist_lead(post: dict[str, Any], analysis: dict[str, Any], status: str = "draft") -> str:
    db=SessionLocal()
    try:
        pid=_post_id(post)
        row=db.query(MoltbookInteractionLead).filter(MoltbookInteractionLead.post_id==pid).first()
        if not row:
            row=MoltbookInteractionLead(post_id=pid, title=str(post.get("title") or ""), content=_post_text(post),
                author=_author_name(post), url=str(post.get("url") or f"https://www.moltbook.com/post/{pid}"))
            db.add(row)
        row.title=str(post.get("title") or "")
        row.content=_post_text(post)
        row.author=_author_name(post)
        row.relevance_score=float(analysis.get("relevance_score",0))
        row.novelty_score=float(analysis.get("novelty_score",0))
        row.research_value_score=float(analysis.get("research_value_score",0))
        row.decision=str(analysis.get("decision") or "ignore")
        row.status=status
        row.reason=str(analysis.get("reason") or "")
        row.draft_comment=sanitize_public_text(str(analysis.get("comment") or ""))[:1000] or None
        db.commit()
        return row.id
    finally: db.close()


def discover_and_analyze(limit: int = 40, min_relevance: float = 0.80) -> dict[str, Any]:
    source, cards, meta=discover_feed(limit=limit)
    me=_self_name()
    recent_texts=[]
    db=SessionLocal()
    try:
        recent_texts=[x.content for x in db.query(MoltbookInteractionLead).order_by(MoltbookInteractionLead.discovered_at.desc()).limit(50).all()]
    finally: db.close()
    results=[]
    for card in cards:
        pid=_post_id(card)
        if not pid or (me and _author_name(card).lower()==me.lower()):
            continue
        try:
            full=fetch_full_post(pid)
        except Exception as exc:
            results.append({"post_id":pid,"status":"read_failed","error":str(exc)})
            continue
        analysis=analyze_post(full,recent_texts)
        if analysis.get("relevance_score",0) >= min_relevance:
            lead_status="candidate" if analysis.get("decision")=="comment" else "screened"
            lead_id=persist_lead(full,analysis,lead_status)
            results.append({"post_id":pid,"lead_id":lead_id,"title":full.get("title"),"author":_author_name(full),**analysis})
    return {"status":"scanned","source":source,"posts_seen":len(cards),"candidates":sum(1 for x in results if x.get("decision")=="comment"),"results":results,"feed_meta":meta}


def post_comment(post_id: str, content: str, parent_id: str | None = None) -> dict[str, Any]:
    payload={"content":sanitize_public_text(content)[:1000]}
    if parent_id: payload["parent_id"]=parent_id
    status, body=req("POST",f"{BASE}/posts/{post_id}/comments",headers(),payload)
    if status>=400:
        raise RuntimeError(f"Moltbook comment failed HTTP {status}: {body}")
    return body if isinstance(body,dict) else {"raw":body}


def run_cycle(auto_comment: bool = False, max_comments: int = 2, min_relevance: float = 0.80) -> dict[str, Any]:
    scan=discover_and_analyze(limit=int(os.getenv("MOLTBOOK_INTERACTION_FEED_LIMIT","40")),min_relevance=min_relevance)
    posted=0
    outputs=[]
    for item in scan["results"]:
        if item.get("decision")!="comment" or posted>=max_comments:
            continue
        pid=item.get("post_id")
        db=SessionLocal()
        try:
            lead=db.query(MoltbookInteractionLead).filter(MoltbookInteractionLead.post_id==pid).first()
            if not lead or not lead.draft_comment:
                continue
            if not auto_comment:
                outputs.append({"post_id":pid,"status":"draft_only","comment":lead.draft_comment})
                continue
            try:
                body=post_comment(pid,lead.draft_comment)
                posted_obj=body.get("comment",body) if isinstance(body,dict) else {}
                cid=str(posted_obj.get("id")) if isinstance(posted_obj,dict) and posted_obj.get("id") else None
                interaction=MoltbookInteraction(post_id=pid,comment_id=cid,direction="outbound",content=lead.draft_comment,
                    classification=str(item.get("classification") or "research_question"),status="posted",reason=str(item.get("reason") or ""))
                db.add(interaction)
                lead.status="commented"
                db.commit()
                posted+=1
                outputs.append({"post_id":pid,"status":"posted","comment_id":cid})
            except Exception as exc:
                lead.status="comment_failed"
                lead.reason=(lead.reason+" "+str(exc)).strip()
                db.commit()
                outputs.append({"post_id":pid,"status":"comment_failed","error":str(exc)})
        finally:
            db.close()
    return {"status":"completed","feed_scan":scan,"comments_posted":posted,"outputs":outputs}
