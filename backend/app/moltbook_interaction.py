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

# Explicit provider selection. Render's AI_PROVIDER/AI_MODEL now control the
# provider used by the Moltbook interaction brain.
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").strip().lower()
AI_KEY = (
    os.getenv("OPENROUTER_API_KEY", "") if AI_PROVIDER == "openrouter"
    else (os.getenv("GEMINI_API_KEY", "") if AI_PROVIDER == "gemini"
          else (os.getenv("RESEARCH_AI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")))
)
AI_MODEL = os.getenv("AI_MODEL", "").strip() or (
    os.getenv("GEMINI_MODEL", "") if AI_PROVIDER == "gemini"
    else (os.getenv("OPENROUTER_MODEL", "openrouter/free") if AI_PROVIDER == "openrouter"
          else (os.getenv("RESEARCH_AI_MODEL", "gpt-5.6-luna") or os.getenv("OPENAI_MODEL", "gpt-5.6-luna")))
)
AI_BASE = os.getenv("RESEARCH_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
if AI_PROVIDER == "openrouter":
    AI_BASE = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
elif AI_PROVIDER == "gemini":
    AI_BASE = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
AI_TIMEOUT = float(os.getenv("RESEARCH_AI_TIMEOUT_SECONDS", "45"))

TOPICS = tuple(x.strip().lower() for x in os.getenv(
    "MOLTBOOK_INTERACTION_TOPICS",
    "trading,backtest,backtesting,forex,quant,quantitative,research,ai,agent,agents,benchmark,benchmarking,evaluation,experiment,methodology,reproducibility,replication,simulation,evidence,dataset,model,inference,verification,robustness,walk-forward,out-of-sample,risk,drawdown,spread,ea,expert advisor"
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


def _ai_json_batch(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Analyze many posts in ONE LLM request.

    Returns a map keyed by post_id. This is intentionally fail-closed: on a
    provider error (especially HTTP 429) callers keep the deterministic
    heuristic result instead of retrying every post and burning quota.
    """
    if not AI_KEY or not items:
        return {}

    system = """You are the research interaction brain for an AI trading research agent on Moltbook.
Be skeptical, concise, and evidence-driven. Decide whether a public comment adds research value.
Do not flatter, spam, promote, give trading signals, or invent evidence. Do not reveal proprietary EA
source code, exact indicators, thresholds, parameters, entry/exit rules, secrets, credentials, or private data.
Prefer one precise question, falsifiable challenge, replication idea, or evidence comparison.
A post may be relevant even when it is not directly about trading: research methodology, AI/agent evaluation,
benchmark design, simulation validity, reproducibility, evidence quality, statistical inference, or experimental
design can provide transferable research methods for an AI trading research society. Prefer posts with a concrete
claim, measurement, benchmark, experiment, limitation, or falsifiable question. Ignore purely social, promotional,
poetic, political, or generic opinion posts.
Return ONLY a JSON array. One object per input post, preserving the exact post_id.
Each object must contain: post_id, relevance_score, novelty_score, research_value_score,
classification, decision (comment|ignore), reason, comment.
Scores must be numbers from 0 to 1. Keep comment <= 500 characters and self-contained.
"""
    user_text = json.dumps({"posts": items, "topics": TOPICS}, ensure_ascii=False)
    max_tokens = int(os.getenv("RESEARCH_AI_MAX_OUTPUT_TOKENS", "3000"))

    if AI_PROVIDER == "openrouter":
        body = json.dumps({
            "model": AI_MODEL or "openrouter/free",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": max_tokens,
        }).encode("utf-8")
        endpoint = f"{AI_BASE}/chat/completions"
        request_headers = {
            "Authorization": f"Bearer {AI_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://ai-trading-bot-research-society-1.onrender.com"),
            "X-Title": os.getenv("OPENROUTER_APP_NAME", "AI Trading Bot Research Society"),
        }
    else:
        body = json.dumps({
            "model": AI_MODEL,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
            ],
            "max_output_tokens": max_tokens,
        }).encode("utf-8")
        endpoint = f"{AI_BASE}/responses"
        request_headers = {
            "Authorization": f"Bearer {AI_KEY}",
            "Content-Type": "application/json",
        }

    request = urllib.request.Request(endpoint, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=AI_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # Never retry here. OpenRouter documents low free-tier limits and
        # failed attempts can still count toward the daily allowance.
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:1000]
        except Exception:
            pass
        raise RuntimeError(f"{AI_PROVIDER.upper()} AI interaction batch failed HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{AI_PROVIDER.upper()} AI interaction batch failed: {e}") from e

    if AI_PROVIDER == "openrouter":
        choices = data.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    else:
        text = data.get("output_text") or ""
        if not text:
            chunks=[]
            for item in data.get("output", []) or []:
                for c in item.get("content", []) or []:
                    if isinstance(c, dict) and c.get("text"):
                        chunks.append(str(c["text"]))
            text="\n".join(chunks)

    # Be tolerant of markdown fences or a leading/trailing explanation.
    m=re.search(r"\[.*\]", text, re.S)
    if not m:
        return {}
    try:
        parsed=json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    valid_ids={str(x.get("post_id")) for x in items if x.get("post_id")}
    for row in parsed:
        if not isinstance(row, dict):
            continue
        pid=str(row.get("post_id") or "")
        if pid and pid in valid_ids:
            out[pid]=row
    return out


def _heuristic_decision(title: str, content: str, novelty: float) -> dict[str, Any]:
    relevance = _keyword_relevance(title, content)
    text = content.lower()
    value_terms=(
        "evidence", "sample", "backtest", "out-of-sample", "oos", "replicate", "replication",
        "reproducib", "robust", "benchmark", "evaluation", "experiment", "methodology",
        "simulation", "dataset", "hypothesis", "statistical", "measurement", "verification",
        "drawdown", "spread", "walk-forward"
    )
    value = min(1.0, sum(1 for x in value_terms if x in text) / 5)
    # Candidate screening is intentionally permissive; the LLM remains the final
    # semantic judge. This prevents useful research-method posts from being
    # discarded before the AI sees them.
    decision = relevance >= 0.30 and novelty >= 0.30 and value >= 0.30
    comment = _contextual_research_comment(title, content) if decision else None
    return {"relevance_score": relevance, "novelty_score": novelty, "research_value_score": value,
            "classification": "research_question" if decision else "other",
            "decision": "comment" if decision else "ignore",
            "reason": "Heuristic research relevance gate", "comment": comment}


def _contextual_research_comment(title: str, content: str) -> str:
    """Create a deterministic, post-specific research comment without an LLM.

    This is deliberately conservative: it asks one evidence-oriented question
    tied to signals actually present in the post. It exists so the agent remains
    useful while Gemini/OpenAI credentials are unavailable, without repeating a
    single canned sentence across unrelated posts.
    """
    text = f"{title} {content}".lower()

    if any(k in text for k in ("sample size", "sample", "n=", "statistical", "significant", "p-value", "confidence interval")):
        return ("What sample size and uncertainty measure support the reported effect, "
                "and does it remain material when the confidence interval or multiple-testing risk is considered?")

    if any(k in text for k in ("benchmark", "baseline", "buy-and-hold", "comparison", "compare", "control")):
        return ("What baseline and evaluation period are you using for the comparison, "
                "and are the same data, costs, and success criteria applied to both methods?")

    if any(k in text for k in ("replication", "reproduce", "reproducib", "independent", "holdout", "out-of-sample", "walk-forward")):
        return ("Can the result be reproduced on an untouched chronological holdout, "
                "and were the evaluation rules fixed before that data was examined?")

    if any(k in text for k in ("agent", "llm", "model", "ai", "evaluation", "eval", "validation")):
        return ("What predefined evaluation criteria distinguish a real improvement from a change in behavior, "
                "and were those criteria tested on cases the system had not seen during development?")

    if any(k in text for k in ("dataset", "data leakage", "leakage", "selection bias", "bias", "dataset")):
        return ("How did you check for selection or data leakage, and does the finding persist on a separately sourced or time-separated dataset?")

    if any(k in text for k in ("trading", "backtest", "backtesting", "forex", "ea", "expert advisor", "strategy", "return", "profit", "drawdown")):
        return ("What out-of-sample period and transaction-cost assumptions were fixed before evaluating this result, "
                "and does the conclusion survive those assumptions?")

    if any(k in text for k in ("risk", "drawdown", "volatility", "sharpe", "loss", "tail")):
        return ("Which risk measure is decisive for the claim, and does the result remain consistent across different market or stress periods rather than only the aggregate sample?")

    if any(k in text for k in ("method", "methodology", "experiment", "hypothesis", "threshold", "acceptance", "criterion", "evidence")):
        return ("What acceptance criterion was fixed before observing the outcome, and what result would have counted as a failure of the hypothesis?")

    return ("What concrete measurement would falsify the main claim, and has that test been run on data or cases kept separate from the evidence used to develop it?")


def _comment_similarity(a: str, b: str) -> float:
    aw = set(re.findall(r"[a-z0-9]{3,}", (a or "").lower()))
    bw = set(re.findall(r"[a-z0-9]{3,}", (b or "").lower()))
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(1, len(aw | bw))


def _recent_outbound_comments(db, limit: int = 30) -> list[str]:
    rows = (db.query(MoltbookInteraction.content)
            .filter(MoltbookInteraction.direction == "outbound")
            .order_by(MoltbookInteraction.created_at.desc())
            .limit(limit).all())
    return [str(row[0]) for row in rows if row and row[0]]


def _merge_analysis(heuristic: dict[str, Any], ai: dict[str, Any] | None) -> dict[str, Any]:
    if not ai:
        return heuristic
    result={**heuristic, **ai}
    for k in ("relevance_score","novelty_score","research_value_score"):
        try:
            result[k]=max(0.0,min(1.0,float(result.get(k, heuristic[k]))))
        except Exception:
            result[k]=heuristic[k]
    result["decision"] = "comment" if str(result.get("decision","ignore")).lower() == "comment" else "ignore"
    if result.get("comment"):
        result["comment"] = sanitize_public_text(str(result["comment"]))[:500]
    return result


def analyze_post(post: dict[str, Any], recent_texts: list[str]) -> dict[str, Any]:
    """Legacy single-post helper retained for compatibility; no provider call."""
    title=str(post.get("title") or "")
    content=_post_text(post)
    novelty=_novelty(f"{title}\n{content}", recent_texts)
    return _heuristic_decision(title, content, novelty)


def analyze_posts_batch(posts: list[dict[str, Any]], recent_texts: list[str]) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Build heuristics for all posts, then optionally make one batch AI call."""
    prepared=[]
    heuristics={}
    for post in posts:
        pid=_post_id(post)
        if not pid:
            continue
        title=str(post.get("title") or "")
        content=_post_text(post)
        novelty=_novelty(f"{title}\n{content}", recent_texts)
        heuristic=_heuristic_decision(title, content, novelty)
        heuristics[pid]=heuristic
        prepared.append({
            "post_id": pid,
            "author": _author_name(post),
            "title": title,
            "content": content[:6000],
            "heuristic": heuristic,
        })

    if not prepared or not AI_KEY:
        return [(p, heuristics.get(_post_id(p), {})) for p in posts if _post_id(p)], False, None

    try:
        ai_map=_ai_json_batch(prepared)
        merged=[(p, _merge_analysis(heuristics.get(_post_id(p), {}), ai_map.get(_post_id(p))))
                for p in posts if _post_id(p)]
        return merged, bool(ai_map), None
    except Exception as exc:
        # Keep the scan useful and, critically, do not retry per post.
        merged=[(p, heuristics.get(_post_id(p), {})) for p in posts if _post_id(p)]
        return merged, False, str(exc)


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


def discover_and_analyze(limit: int = 40, min_relevance: float = 0.30) -> dict[str, Any]:
    source, cards, meta=discover_feed(limit=limit)
    me=_self_name()
    recent_texts=[]
    db=SessionLocal()
    try:
        recent_texts=[x.content for x in db.query(MoltbookInteractionLead).order_by(MoltbookInteractionLead.discovered_at.desc()).limit(50).all()]
    finally: db.close()

    # Fetch full posts first; this is Moltbook traffic, not AI traffic.
    full_posts=[]
    results=[]
    for card in cards:
        pid=_post_id(card)
        if not pid or (me and _author_name(card).lower()==me.lower()):
            continue
        try:
            full_posts.append(fetch_full_post(pid))
        except Exception as exc:
            results.append({"post_id":pid,"status":"read_failed","error":str(exc)})

    batch_size=max(1, int(os.getenv("MOLTBOOK_AI_BATCH_SIZE", "20")))
    all_pairs=[]
    ai_batches=0
    ai_error=None
    for i in range(0, len(full_posts), batch_size):
        pairs, ai_used, err=analyze_posts_batch(full_posts[i:i+batch_size], recent_texts)
        all_pairs.extend(pairs)
        ai_batches += 1 if ai_used else 0
        if err and ai_error is None:
            ai_error=err

    for full, analysis in all_pairs:
        if analysis.get("relevance_score",0) >= min_relevance:
            lead_status="candidate" if analysis.get("decision")=="comment" else "screened"
            lead_id=persist_lead(full,analysis,lead_status)
            results.append({"post_id":_post_id(full),"lead_id":lead_id,"title":full.get("title"),"author":_author_name(full),**analysis})

    return {
        "status":"scanned",
        "source":source,
        "posts_seen":len(cards),
        "posts_analyzed":len(full_posts),
        "ai_batches":ai_batches,
        "ai_used":ai_batches>0,
        "ai_error":ai_error,
        "ai_batch_size":batch_size,
        "candidates":sum(1 for x in results if x.get("decision")=="comment"),
        "results":results,
        "feed_meta":meta,
    }


def post_comment(post_id: str, content: str, parent_id: str | None = None) -> dict[str, Any]:
    payload={"content":sanitize_public_text(content)[:1000]}
    if parent_id: payload["parent_id"]=parent_id
    status, body=req("POST",f"{BASE}/posts/{post_id}/comments",headers(),payload)
    if status>=400:
        raise RuntimeError(f"Moltbook comment failed HTTP {status}: {body}")
    return body if isinstance(body,dict) else {"raw":body}


def run_cycle(auto_comment: bool = False, max_comments: int = 2, min_relevance: float = 0.30) -> dict[str, Any]:
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
                # Duplicate guard: never post the same canned/near-identical
                # research comment repeatedly across unrelated posts.
                recent_comments = _recent_outbound_comments(db, limit=30)
                duplicate_threshold = float(os.getenv("MOLTBOOK_COMMENT_SIMILARITY_THRESHOLD", "0.72"))
                duplicate = next((c for c in recent_comments
                                  if _comment_similarity(lead.draft_comment, c) >= duplicate_threshold), None)
                if duplicate:
                    lead.status="comment_skipped_duplicate"
                    lead.reason=(lead.reason+" Duplicate/near-duplicate comment suppressed.").strip()
                    db.commit()
                    outputs.append({"post_id":pid,"status":"skipped_duplicate","similarity":round(_comment_similarity(lead.draft_comment, duplicate),4)})
                    continue

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
