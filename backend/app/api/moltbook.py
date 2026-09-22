from __future__ import annotations
import asyncio, json, os, re, urllib.error, urllib.request, uuid
from datetime import datetime, timezone
from typing import Any
from fastapi import APIRouter, HTTPException, Query
from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult

router=APIRouter(prefix="/moltbook",tags=["Moltbook"])
BASE=os.getenv("MOLTBOOK_API_BASE","https://www.moltbook.com/api/v1").rstrip("/")
SUBMOLT=os.getenv("MOLTBOOK_SUBMOLT","").strip()
TIMEOUT=float(os.getenv("MOLTBOOK_TIMEOUT_SECONDS","20"))

def req(method,url,headers=None,payload=None):
    body=json.dumps(payload).encode() if payload is not None else None
    r=urllib.request.Request(url,data=body,headers=headers or {},method=method)
    try:
        with urllib.request.urlopen(r,timeout=TIMEOUT) as x:
            raw=x.read().decode("utf-8","replace")
            try: return x.status,json.loads(raw)
            except: return x.status,{"raw":raw[:2000]}
    except urllib.error.HTTPError as e:
        raw=e.read().decode("utf-8","replace")
        try: b=json.loads(raw)
        except: b={"raw":raw[:2000]}
        return e.code,b

async def resolve_submolt():
    if not SUBMOLT: raise HTTPException(503,"MOLTBOOK_SUBMOLT is not configured")
    status,body=await asyncio.to_thread(req,"GET",f"{BASE}/submolts")
    if status>=400 or not isinstance(body,dict): raise HTTPException(502,{"message":"Could not resolve Moltbook submolt","status_code":status,"response":body})
    items=body.get("submolts")
    if not isinstance(items,list): raise HTTPException(502,"Moltbook /submolts response did not contain a submolts list")
    m=next((x for x in items if isinstance(x,dict) and (SUBMOLT.lower() in {str(x.get("name","")).lower(),str(x.get("display_name","")).lower()} or SUBMOLT==str(x.get("id","")))),None)
    if not m: raise HTTPException(400,{"message":"Configured Moltbook submolt was not found","configured_submolt":SUBMOLT})
    sid=str(m.get("id") or ""); name=str(m.get("name") or "")
    try: uuid.UUID(sid)
    except: raise HTTPException(502,f"Invalid submolt UUID: {sid}")
    return {"submolt":name,"submolt_name":name,"submolt_id":sid}

def load_result(eid):
    db=SessionLocal()
    try:
        e=db.query(Experiment).filter(Experiment.id==eid).first()
        if not e: raise HTTPException(404,"Experiment not found")
        rows=db.query(ExperimentResult).filter(ExperimentResult.experiment_id==eid).order_by(ExperimentResult.created_at.desc()).all()
        r=next((x for x in rows if _is_unified(x)),rows[0] if rows else None)
        if not r: raise HTTPException(404,"No research result found for this experiment")
        return e,r
    finally: db.close()

def _is_unified(r):
    try:
        engine = json.loads(r.metrics or "{}").get("engine")
        return engine in {"unified_research_v1", "unified_research_v2", "ea_backtest_research_v1", "ea_backtest_research_v2"}
    except Exception:
        return False

def _drop_degenerate_win_rate(d):
    """Strip win_rate / win_rate_95ci from a stats dict.

    Same principle already applied to the WIN-vs-LOSS holding-duration
    finding below (see unified_research.py): once a subgroup has been
    selected by an extremum (strongest/weakest month, highest/lowest
    net-profit hour/weekday, first/last period) rather than representing
    the full population, a small sample can trivially land on
    win_rate == 1.0 or 0.0. Showing that bare number next to real
    aggregate stats misleadingly reads as a performance signal, so we
    omit it here and keep n/wins/losses (from which a reader can judge
    sample size for themselves) instead.
    """
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if k not in ("win_rate", "win_rate_95ci")}

def _is_degenerate_group(x):
    """True when a stats dict is an outcome-carved (all-win or all-loss)
    subgroup, so its win_rate is tautological (1.0 or 0.0) by construction
    rather than a real performance signal."""
    if not isinstance(x, dict):
        return False
    n, wins, losses = x.get("n"), x.get("wins"), x.get("losses")
    if isinstance(n, (int, float)) and n:
        if isinstance(wins, (int, float)) and wins == n:
            return True
        if isinstance(losses, (int, float)) and losses == n:
            return True
    return x.get("win_rate") in (0, 0.0, 1, 1.0)

def _scrub_evidence(ev):
    """Recursively strip win_rate / win_rate_95ci from any degenerate
    (all-win or all-loss) subgroup found anywhere in an evidence tree.

    Stored research results can come from different versions of the
    analysis engine (older experiments keep whatever shape was computed
    at the time), so this does not assume any particular evidence layout
    the way the shape-specific branches below do — it is a safety net
    that applies regardless of which engine version wrote the data.
    """
    if isinstance(ev, dict):
        cleaned = {k: _scrub_evidence(v) for k, v in ev.items()}
        if _is_degenerate_group(cleaned):
            cleaned = _drop_degenerate_win_rate(cleaned)
        return cleaned
    if isinstance(ev, list):
        return [_scrub_evidence(x) for x in ev]
    return ev

def _fmt_hold_duration(win_n, win_hold, loss_n, loss_hold):
    """Human-readable holding-duration comparison, replacing the raw
    outcome-conditioned WIN/LOSS stat blocks (see _is_degenerate_group)."""
    parts=[f"Winning trades: n={win_n}, avg hold={win_hold} min",
           f"Losing trades: n={loss_n}, avg hold={loss_hold} min"]
    if isinstance(win_hold,(int,float)) and isinstance(loss_hold,(int,float)):
        parts.append(f"Difference: {win_hold-loss_hold:+.1f} min")
    return " | ".join(parts)

def _fmt_money_pct(ev):
    """Create a compact, auditable public evidence representation.

    Full evidence remains in the stored research result. Public Moltbook posts
    should expose the signal, sample size and key limitation without dumping
    hundreds of monthly/hourly rows into one post.
    """
    if not isinstance(ev, dict):
        return str(ev)[:800]
    ev=_scrub_evidence(ev)
    # Holding-duration comparison: outcome-conditioned by construction, so
    # render it as a plain comparison rather than dumping the WIN/LOSS stat
    # blocks (which would otherwise show a tautological win_rate of 1.0/0.0).
    # Handles both the current engine's "winning_trades"/"losing_trades"
    # shape and older stored results that still embed full "WIN"/"LOSS" blocks.
    if "winning_trades" in ev and "losing_trades" in ev:
        w,l=ev.get("winning_trades") or {},ev.get("losing_trades") or {}
        return _fmt_hold_duration(w.get("n"),w.get("avg_hold_minutes"),l.get("n"),l.get("avg_hold_minutes"))
    if "WIN" in ev and "LOSS" in ev and isinstance(ev.get("WIN"),dict) and isinstance(ev.get("LOSS"),dict):
        w,l=ev["WIN"],ev["LOSS"]
        return _fmt_hold_duration(
            w.get("n"), w.get("avg_hold_minutes", ev.get("WIN_avg_hold_minutes")),
            l.get("n"), l.get("avg_hold_minutes", ev.get("LOSS_avg_hold_minutes")),
        )
    if "BUY" in ev and "SELL" in ev:
        out={}
        for side in ("BUY","SELL"):
            x=ev.get(side,{})
            out[side]={k:x.get(k) for k in (
                "n","wins","losses","win_rate","win_rate_95ci",
                "net_profit","net_profit_pct_initial_capital","profit_factor",
                "avg_hold_minutes","max_drawdown_absolute","max_drawdown_pct_initial_capital"
            ) if k in x}
        return json.dumps(out,ensure_ascii=False)
    if "monthly" in ev and isinstance(ev["monthly"],dict):
        months=ev["monthly"]
        ranked=[]
        for k,x in months.items():
            if not isinstance(x,dict): continue
            ranked.append((str(k),x))
        ranked.sort()
        # Public post: first/last plus strongest/weakest by net profit, while
        # retaining the total period count. Full monthly table stays private.
        selected=[]
        if ranked:
            selected.extend(ranked[:1])
            if len(ranked)>1: selected.append(ranked[-1])
            if len(ranked)>2:
                strongest=max(ranked,key=lambda z: float(z[1].get("net_profit",0) or 0))
                weakest=min(ranked,key=lambda z: float(z[1].get("net_profit",0) or 0))
                for item in (strongest,weakest):
                    if item not in selected: selected.append(item)
        # win_rate deliberately excluded: these periods are extrema-selected
        # (first/last/strongest/weakest), not the full population, so a thin
        # period can trivially show 1.0/0.0. See _drop_degenerate_win_rate.
        compact={k:{kk:x.get(kk) for kk in ("n","wins","losses","net_profit","net_profit_pct_initial_capital","profit_factor") if kk in x} for k,x in selected}
        return json.dumps({"period_count":len(ranked),"selected_periods":compact},ensure_ascii=False)
    if "groups" in ev and isinstance(ev["groups"],dict):
        groups=ev["groups"]
        items=[(str(k),x) for k,x in groups.items() if isinstance(x,dict)]
        if not items: return json.dumps(ev,ensure_ascii=False)[:1600]
        # Do not publish every hour/day group. Show sample size and extrema.
        hi=max(items,key=lambda z: float(z[1].get("net_profit",0) or 0))
        lo=min(items,key=lambda z: float(z[1].get("net_profit",0) or 0))
        return json.dumps({
            "group_count":len(items),
            "highest_net_profit_group":{hi[0]:_drop_degenerate_win_rate(hi[1])},
            "lowest_net_profit_group":{lo[0]:_drop_degenerate_win_rate(lo[1])},
        },ensure_ascii=False)
    if "winning_trades" in ev and "losing_trades" in ev:
        return json.dumps({
            "winning_trades":ev.get("winning_trades"),
            "losing_trades":ev.get("losing_trades"),
            "difference_avg_hold_minutes":ev.get("difference_avg_hold_minutes")
        },ensure_ascii=False)
    return json.dumps(ev,ensure_ascii=False,default=str)[:1600]


def build_post(e,r):
    m=json.loads(r.metrics or "{}")
    a=m.get("analysis",{})
    findings=a.get("findings",[])
    hypotheses=a.get("hypotheses",[])
    title=f"Research Update: {e.symbol} {e.timeframe}"
    account=a.get("account_context",{})
    lines=[
        "AI Trading Bot Research Society — Research Update",
        f"Experiment: {e.id}",
        f"Strategy: {e.symbol} {e.timeframe}",
        "",
        "Research scope:",
        "EA .mq5 + MT5 Backtest only.",
        "No OHLC bars, tick data, or external market data were used.",
    ]
    if account.get("initial_capital") is not None:
        lines.append(f"Initial capital configuration: ${float(account['initial_capital']):,.2f}")
    overall=a.get("overall",{})
    if overall.get("n"):
        lines += [
            f"Completed trades analyzed: {overall['n']}",
            f"Net profit: ${overall.get('net_profit',0):,.2f} ({overall.get('net_profit_pct_initial_capital',0):+.2f}% of initial capital)",
        ]
        if overall.get("max_drawdown_absolute") is not None:
            lines.append(f"Observed max drawdown: ${overall['max_drawdown_absolute']:,.2f} ({overall.get('max_drawdown_pct_initial_capital',0):.2f}% of initial capital)")
    lines.append("")
    if findings:
        lines.append("Evidence-backed observations:")
        for f in findings[:8]:
            q=f.get("question",f.get("feature","Research finding"))
            lines.append(f"- {q}")
            lines.append("  Evidence: "+_fmt_money_pct(f.get("evidence",{})))
            lines.append("  Interpretation: "+str(f.get("interpretation") or "Observed association in supplied backtest; not causation."))
    else:
        lines.append("Evidence-backed observations: NONE_ESTABLISHED")
    if hypotheses:
        lines += ["","Research hypotheses (not validated findings):"]
        for h in hypotheses[:6]:
            if not h.get("statement"): continue
            lines.append(f"- {h['statement']}")
            if h.get("missing_evidence"):
                lines.append("  Missing evidence: "+json.dumps(h["missing_evidence"],ensure_ascii=False))
            if h.get("alternative_explanations"):
                lines.append("  Alternatives: "+json.dumps(h["alternative_explanations"],ensure_ascii=False))
    critique=[]
    if m.get("limitations"): critique.extend(m.get("limitations",[])[:4])
    if a.get("insufficient_evidence"): critique.extend([x.get("reason","") for x in a.get("insufficient_evidence",[])[:4] if x.get("reason")])
    if critique:
        lines += ["","Research limitations / critique:"]+[f"- {x}" for x in critique]
    lines += [
        "",
        "Peer research questions:",
        "1. Can this EA/backtest pattern be reproduced in another experiment?",
        "2. Which part of the EA logic should be tested next?",
        "3. Does the observed behavior remain stable across different backtest periods?",
        "4. What evidence would falsify the observed pattern?",
        "5. Which hypothesis should be tested against a new backtest?",
        "",
        "This post reports supplied-data evidence only. It is not a live-trading signal or financial advice."
    ]
    content="\n".join(lines)
    # Keep public posts comfortably below common API/feed limits while preserving
    # the core evidence and limitations. Stored research remains complete.
    max_chars=int(os.getenv("MOLTBOOK_PUBLIC_MAX_CHARS","12000"))
    if len(content)>max_chars:
        marker="\n\n[Public post compacted; full evidence remains in the research result.]\n"
        content=content[:max(0,max_chars-len(marker))].rsplit("\n",1)[0]+marker
    return title,content

async def publish(title,content,republish=False):
    key=os.getenv("MOLTBOOK_API_KEY","")
    if not key: raise HTTPException(503,"MOLTBOOK_API_KEY is not configured")
    if key.startswith("moltdev_"): raise HTTPException(400,"Use the bot agent API key for publishing, not moltdev_.")
    payload={"title":title,"content":content,**(await resolve_submolt())}
    if republish:
        run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload["title"]=f"{title} — Research Run {run_id}"
        payload["content"]=f"{content}\nResearch Run: {run_id}"
    status,body=await asyncio.to_thread(req,"POST",f"{BASE}/posts",{"Authorization":f"Bearer {key}","Content-Type":"application/json"},payload)
    if status>=400: raise HTTPException(502,{"message":"Moltbook publish failed","status_code":status,"response":body})
    return body

# ---------------------------------------------------------------------------
# Verification solver: challenges can obfuscate number words with case,
# punctuation and repeated letters. We solve only when the challenge yields a
# deterministic arithmetic operation; otherwise we abstain instead of guessing.
# ---------------------------------------------------------------------------
_ONES = {"zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,"eighteen":18,"nineteen":19}
_TENS = {"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,"seventy":70,"eighty":80,"ninety":90}

def _collapse(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s.lower())

def _number_phrases() -> dict[str,int]:
    out=dict(_ONES); out.update(_TENS)
    for tw,tv in _TENS.items():
        for ow,ov in _ONES.items():
            if 1 <= ov <= 9: out[tw+ow]=tv+ov
    return out
_NUMBER_PHRASES=_number_phrases()

def _extract_number_words(text: str) -> list[int]:
    tokens=re.findall(r"[A-Za-z]+", text.lower())
    found=[]
    for i in range(len(tokens)):
        acc=""
        for j in range(i,min(len(tokens),i+8)):
            acc+=tokens[j]
            for v in (acc,_collapse(acc)):
                if v in _NUMBER_PHRASES:
                    found.append((i,j,_NUMBER_PHRASES[v])); break
    found.sort(key=lambda x:(x[0],-(x[1]-x[0]),x[1]))
    selected=[]
    for item in found:
        if any(not(item[1]<a[0] or item[0]>a[1]) for a in selected): continue
        selected.append(item)
    selected.sort()
    return [x[2] for x in selected]

_OP_PATTERNS=[
    (r"(?:multipl(?:y|ied|ies)|times|product)","*",False),
    (r"(?:per[ -]?second|per[ -]?sec)","/",False),
    (r"(?:divid(?:e|ed)|quotient|over)","/",False),
    # Order-sensitive phrasings: "6 less than 10" and "subtract 6 from 10" both
    # mean 10 - 6, even though the numbers are extracted in the order 6, 10.
    # Treating every "-" phrasing as nums[0]-nums[1] silently answered these
    # backwards (e.g. -4 instead of 4) -- a wrong answer that fails Moltbook's
    # one-shot verification permanently for that post.
    (r"subtract\b.*?\bfrom\b","-",True),
    (r"less than","-",True),
    (r"(?:subtract|minus|decrease|decreases|slows? by)","-",False),
    (r"(?:add|plus|increase|increases|sum of|gain|gains)","+",False),
]

def _fmt_answer(x):
    """Render the numeric answer without a spurious '.00' on whole numbers.

    Division always yields a float in Python even for evenly-divisible
    inputs (12/2 -> 6.0). If Moltbook compares the submitted answer as an
    exact string, sending '6.00' instead of '6' can fail verification --
    and since verification is one-shot per post, that failure is permanent.
    """
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    if isinstance(x, int):
        return str(x)
    return f"{x:.2f}"

def _solve_challenge(challenge_text: str):
    nums=_extract_number_words(challenge_text)
    if len(nums)<2:
        digits=[float(x) for x in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?",challenge_text)]
        if len(digits)>=2: nums=[int(x) if float(x).is_integer() else x for x in digits[:2]]
    if len(nums)!=2: raise ValueError(f"Could not unambiguously extract two numbers from challenge: {challenge_text}")
    lower=challenge_text.lower(); collapsed=_collapse(challenge_text)
    operation=None; reverse=False; best_pos=None
    for pattern,op,rev in _OP_PATTERNS:
        # Check the literal text first, then the de-obfuscated (case-duplication
        # collapsed) form: Moltbook challenges can garble the operation word the
        # same way they garble numbers (e.g. "<GaAiInSs>" decodes to "gains").
        # Checking only `lower` here was the reason auto-verify silently failed
        # ("Could not unambiguously determine arithmetic operation") whenever a
        # challenge obfuscated the operation word instead of (or as well as)
        # the numbers.
        # Also: take whichever matching pattern appears EARLIEST in the text
        # rather than whichever is last in this list -- a challenge that
        # happens to contain two operation-ish words should be read the way a
        # person reads it, left to right, not by our list's arbitrary order.
        for text in (lower,collapsed):
            m=re.search(pattern,text)
            if m and (best_pos is None or m.start()<best_pos):
                best_pos=m.start(); operation=op; reverse=rev
    symbols=re.findall(r"(?<![A-Za-z])[+*/](?![A-Za-z])|(?<![A-Za-z])-(?![A-Za-z])",challenge_text)
    if symbols: operation=symbols[0]; reverse=False
    if operation is None: raise ValueError("Could not unambiguously determine arithmetic operation")
    a,b=nums
    if reverse: a,b=b,a
    if operation=="+": answer=a+b
    elif operation=="-": answer=a-b
    elif operation=="*": answer=a*b
    elif operation=="/":
        if b==0: raise ValueError("Division by zero")
        answer=a/b
    return _fmt_answer(answer),{"numbers":nums,"operation":operation,"reversed":reverse,"answer":answer}

async def _verify_post(published):
    body=published.get("post",published) if isinstance(published,dict) else {}
    if isinstance(published,dict) and isinstance(published.get("response"),dict): body=published["response"]
    candidates=[body,body.get("post") if isinstance(body,dict) else None,body.get("data") if isinstance(body,dict) else None]
    verification={}; post_id=None
    for item in candidates:
        if not isinstance(item,dict): continue
        post_id=post_id or item.get("id")
        if isinstance(item.get("verification"),dict): verification=item["verification"]; break
    if not verification:
        return {"attempted":False,"verified":True,"reason":"verification not required or challenge not returned"}
    challenge=verification.get("challenge_text") or verification.get("challenge")
    code=verification.get("verification_code") or verification.get("code")
    if not challenge:
        return {"attempted":False,"verified":False,"reason":"verification required but challenge text is missing"}
    try: answer,parsed=_solve_challenge(str(challenge))
    except Exception as exc: return {"attempted":False,"verified":False,"reason":str(exc),"challenge_text":challenge}
    if not post_id:
        return {"attempted":False,"verified":False,"reason":"published post id is missing"}
    payload={"answer":answer}
    if code is not None: payload["verification_code"]=code
    key=os.getenv("MOLTBOOK_API_KEY","")
    status,res=await asyncio.to_thread(req,"POST",f"{BASE}/posts/{post_id}/verify",{"Authorization":f"Bearer {key}","Content-Type":"application/json"},payload)
    return {"attempted":True,"verified":status<400 and isinstance(res,dict) and res.get("success") is True,"answer":answer,"parsed":parsed,"status_code":status,"post_id":post_id,"response":res}

@router.get("/config")
async def config():
    key=os.getenv("MOLTBOOK_API_KEY","")
    return {"api_base":BASE,"api_key_configured":bool(key),"api_key_type":"developer_app_key" if key.startswith("moltdev_") else "agent_key" if key else None,"submolt_configured":bool(SUBMOLT),"submolt_name":SUBMOLT or None}

@router.get("/research/{experiment_id}/preview")
async def preview(experiment_id:str):
    e,r=load_result(experiment_id); title,content=build_post(e,r); return {"status":"preview","experiment_id":experiment_id,"title":title,"content":content}

@router.post("/research/{experiment_id}/publish")
async def publish_research(experiment_id:str, republish:bool=Query(False)):
    e,r=load_result(experiment_id)
    title,content=build_post(e,r)
    out=await publish(title,content,republish=republish)
    verification=await _verify_post(out)
    return {"status":"published" if verification.get("verified") else "published_pending_verification","experiment_id":experiment_id,"title":out.get("title") or title,"post_id":out.get("id") or (out.get("post") or {}).get("id"),"verification":verification,"moltbook":out}

@router.post("/research/{experiment_id}/publish-manual-verify")
async def publish_manual_verify(experiment_id:str, republish:bool=Query(False)):
    e,r=load_result(experiment_id); title,content=build_post(e,r); out=await publish(title,content,republish=republish)
    post=out.get("post") if isinstance(out,dict) and isinstance(out.get("post"),dict) else out
    verification=post.get("verification") if isinstance(post,dict) else None
    challenge=(verification or {}).get("challenge_text") or (verification or {}).get("challenge") or (post.get("verification_question") if isinstance(post,dict) else None)
    verification_code=(verification or {}).get("verification_code") or (verification or {}).get("code") or (post.get("verification_code") if isinstance(post,dict) else None)
    # Try the same auto-solver used by /publish, but only to SUGGEST an
    # answer here -- never submit it. Verification is one-shot per post
    # (a wrong submission to /post/{post_id}/verify fails permanently), so
    # this exists purely so a human doesn't have to decode the obfuscated
    # challenge text by hand before their one real attempt.
    suggested_answer=None; solver_note=None
    if challenge:
        try:
            suggested_answer,parsed=_solve_challenge(str(challenge))
            solver_note=f"auto-solver read this as: {parsed['numbers'][0]} {parsed['operation']} {parsed['numbers'][1]} = {suggested_answer}"
        except Exception as exc:
            solver_note=f"auto-solver could not parse this challenge on its own: {exc}"
    return {
        "status":"published_pending_verification",
        "experiment_id":experiment_id,
        "title":post.get("title") or title,
        "post_id":post.get("id") if isinstance(post,dict) else out.get("id"),
        "verification_question":challenge,
        "verification_code":verification_code,
        "suggested_answer":suggested_answer,
        "solver_note":solver_note,
        "warning":"Moltbook verification is one-shot per post: submitting a wrong answer to POST /moltbook/post/{post_id}/verify fails permanently for this post (you would need to republish to get a new challenge). Double-check suggested_answer against verification_question yourself before submitting -- do not trust it blindly.",
        "moltbook":out,
    }

@router.post("/post/{post_id}/verify")
async def verify_post(post_id:str,payload:dict[str,Any]):
    key=os.getenv("MOLTBOOK_API_KEY","")
    if not key: raise HTTPException(503,"MOLTBOOK_API_KEY is not configured")
    if "answer" not in payload: raise HTTPException(400,"Provide the verification answer")
    body={"answer":payload["answer"]}
    if payload.get("verification_code") is not None: body["verification_code"]=payload["verification_code"]
    status,res=await asyncio.to_thread(req,"POST",f"{BASE}/posts/{post_id}/verify",{"Authorization":f"Bearer {key}","Content-Type":"application/json"},body)
    if status>=400: raise HTTPException(502,{"message":"Moltbook verification failed","status_code":status,"response":res})
    return {"status":"verified","post_id":post_id,"response":res}
