from __future__ import annotations
import asyncio, json, os, urllib.error, urllib.request, uuid
from datetime import datetime, timezone
from typing import Any
from fastapi import APIRouter, HTTPException
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

def _fmt_money_pct(ev):
    """Compact research evidence for a public Moltbook post.
    Full JSON remains in the stored research result; the public post only
    carries decision-relevant fields so it stays readable and auditable.
    """
    if not isinstance(ev, dict):
        return str(ev)[:800]
    if "BUY" in ev and "SELL" in ev:
        out={}
        for side in ("BUY","SELL"):
            x=ev.get(side,{})
            out[side]={k:x.get(k) for k in ("n","wins","losses","win_rate","win_rate_95ci","net_profit","net_profit_pct_initial_capital","profit_factor","avg_hold_minutes","max_drawdown_absolute","max_drawdown_pct_initial_capital") if k in x}
        return json.dumps(out,ensure_ascii=False)
    if "groups" in ev and isinstance(ev["groups"],dict):
        groups={}
        for k,x in ev["groups"].items():
            groups[k]={kk:x.get(kk) for kk in ("n","wins","losses","win_rate","win_rate_95ci","net_profit","net_profit_pct_initial_capital","profit_factor","avg_hold_minutes") if kk in x}
        return json.dumps({"groups":groups,"highest":ev.get("highest"),"lowest":ev.get("lowest")},ensure_ascii=False)
    if "monthly" in ev and isinstance(ev["monthly"],dict):
        months={}
        for k,x in ev["monthly"].items():
            months[k]={kk:x.get(kk) for kk in ("n","wins","losses","win_rate","win_rate_95ci","net_profit","net_profit_pct_initial_capital","profit_factor","avg_hold_minutes") if kk in x}
        return json.dumps({"monthly":months,"highest_win_rate_period":ev.get("highest_win_rate_period"),"lowest_win_rate_period":ev.get("lowest_win_rate_period")},ensure_ascii=False)
    if "winning_trades" in ev and "losing_trades" in ev:
        return json.dumps(ev,ensure_ascii=False)
    return json.dumps(ev,ensure_ascii=False,default=str)[:1200]


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
    return title,"\n".join(lines)

async def publish(title,content):
    key=os.getenv("MOLTBOOK_API_KEY","")
    if not key: raise HTTPException(503,"MOLTBOOK_API_KEY is not configured")
    if key.startswith("moltdev_"): raise HTTPException(400,"Use the bot agent API key for publishing, not moltdev_.")
    payload={"title":title,"content":content,**(await resolve_submolt())}
    status,body=await asyncio.to_thread(req,"POST",f"{BASE}/posts",{"Authorization":f"Bearer {key}","Content-Type":"application/json"},payload)
    if status>=400: raise HTTPException(502,{"message":"Moltbook publish failed","status_code":status,"response":body})
    return body

@router.get("/config")
async def config():
    key=os.getenv("MOLTBOOK_API_KEY","")
    return {"api_base":BASE,"api_key_configured":bool(key),"api_key_type":"developer_app_key" if key.startswith("moltdev_") else "agent_key" if key else None,"submolt_configured":bool(SUBMOLT)}

@router.get("/research/{experiment_id}/preview")
async def preview(experiment_id:str):
    e,r=load_result(experiment_id); title,content=build_post(e,r); return {"status":"preview","experiment_id":experiment_id,"title":title,"content":content}

@router.post("/research/{experiment_id}/publish")
async def publish_research(experiment_id:str):
    e,r=load_result(experiment_id); title,content=build_post(e,r); out=await publish(title,content); return {"status":"published","experiment_id":experiment_id,"title":title,"moltbook":out}

@router.post("/research/{experiment_id}/publish-manual-verify")
async def publish_manual_verify(experiment_id:str):
    e,r=load_result(experiment_id); title,content=build_post(e,r); out=await publish(title,content)
    return {"status":"published_pending_verification","experiment_id":experiment_id,"title":title,"post_id":out.get("id") or out.get("post",{}).get("id"),"verification_question":out.get("verification_question") or out.get("post",{}).get("verification_question"),"verification_code":out.get("verification_code") or out.get("post",{}).get("verification_code"),"moltbook":out}

@router.post("/post/{post_id}/verify")
async def verify_post(post_id:str,payload:dict[str,Any]):
    key=os.getenv("MOLTBOOK_API_KEY","")
    if not key: raise HTTPException(503,"MOLTBOOK_API_KEY is not configured")
    if "answer" not in payload: raise HTTPException(400,"Provide the human-solved verification answer")
    body={"answer":payload["answer"]}
    if payload.get("verification_code") is not None: body["verification_code"]=payload["verification_code"]
    status,res=await asyncio.to_thread(req,"POST",f"{BASE}/posts/{post_id}/verify",{"Authorization":f"Bearer {key}","Content-Type":"application/json"},body)
    if status>=400: raise HTTPException(502,{"message":"Moltbook verification failed","status_code":status,"response":res})
    return {"status":"verified","post_id":post_id,"response":res}
