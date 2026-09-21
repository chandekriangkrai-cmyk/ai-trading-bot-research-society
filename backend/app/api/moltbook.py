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
    try: return json.loads(r.metrics or "{}").get("engine")=="unified_research_v1"
    except: return False

def build_post(e,r):
    m=json.loads(r.metrics or "{}")
    a=m.get("analysis",{})
    findings=a.get("findings",[])
    title=f"Research Update: {e.symbol} {e.timeframe}"
    lines=["AI Trading Bot Research Society — Research Update",f"Experiment: {e.id}",f"Strategy: {e.symbol} {e.timeframe}","","Research scope:","Pre-entry price action, market context, momentum/candle structure, repeated failure conditions, and post-entry MFE/MAE when supported by the supplied data.",""]
    if findings:
        lines.append("Evidence-gated observations:")
        for f in findings[:8]:
            lines.append(f"- {f.get('feature')}: {f.get('condition')} | n={f.get('n',f.get('n_low','?'))} | gap={f.get('gap')}")
            lines.append("  Observed association only; not causation.")
    else: lines.append("Evidence-gated observations: NONE_ESTABLISHED")
    lines += ["", "Peer research questions:","1. What candle and price-action patterns appear before winning and losing trades?","2. Which market conditions appear to support or weaken the EA's existing entry rules?","3. How do volatility, candle range, momentum, and consecutive bullish or bearish candles differ between winning and losing trades?","4. Are there identifiable price-action or market-regime conditions where the EA repeatedly fails?"]
    lim=m.get("limitations",[])
    if lim:
        lines += ["","Limitations:"]+[f"- {x}" for x in lim[:6]]
    lines += ["","This is research evidence from the supplied backtest input, not a live-trading signal or financial advice."]
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
