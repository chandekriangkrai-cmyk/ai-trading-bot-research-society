"""V28 AI-first Moltbook comment pipeline."""
from __future__ import annotations
import json, os, re, urllib.error, urllib.request
from typing import Any

OPENROUTER_URL=os.getenv("OPENROUTER_BASE_URL","https://openrouter.ai/api/v1").rstrip("/")+"/chat/completions"
OPENROUTER_MODEL=os.getenv("OPENROUTER_MODEL","openrouter/free")
OPENROUTER_API_KEY=os.getenv("OPENROUTER_API_KEY","")
MAX_TOKENS=int(os.getenv("MOLTBOOK_AI_MAX_TOKENS","900"))
TIMEOUT=float(os.getenv("MOLTBOOK_AI_TIMEOUT_SECONDS","45"))
MAX_COMMENT_CHARS=int(os.getenv("MOLTBOOK_MAX_COMMENT_CHARS","420"))
GENERIC_WORDS={"agent","agents","model","models","system","systems","research","result","evidence","claim","question","performance","validation","testing","test","effect","metric","metrics","study","paper","data","method","methods","using","under","same","independent","whether","reported","report","reports","would","could","should","does","did","what","how","post","author"}

def _request(prompt:str,max_tokens:int=MAX_TOKENS)->tuple[dict[str,Any]|None,dict[str,Any]]:
    if not OPENROUTER_API_KEY:return None,{"error":"OPENROUTER_API_KEY not configured"}
    payload={"model":OPENROUTER_MODEL,"messages":[{"role":"system","content":"You are a research peer on Moltbook. Return ONLY valid JSON. No markdown."},{"role":"user","content":prompt}],"temperature":0.2,"max_tokens":max_tokens}
    req=urllib.request.Request(OPENROUTER_URL,data=json.dumps(payload,ensure_ascii=False).encode(),headers={"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json","HTTP-Referer":os.getenv("OPENROUTER_HTTP_REFERER","https://www.moltbook.com"),"X-Title":"AI Trading Bot Research Society"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=TIMEOUT) as resp: body=json.loads(resp.read().decode("utf-8","replace"))
    except urllib.error.HTTPError as exc:
        return None,{"error":f"HTTP {exc.code}","raw":exc.read().decode("utf-8","replace")[:1200]}
    except Exception as exc:return None,{"error":str(exc)}
    choice=(body.get("choices") or [{}])[0]; msg=choice.get("message") or {}; content=msg.get("content") or ""; finish=choice.get("finish_reason")
    meta={"finish_reason":finish,"response_chars":len(content),"usage":body.get("usage") or {},"model":body.get("model",OPENROUTER_MODEL)}
    if finish=="length":meta["error"]="finish_reason=length"; return None,meta
    try:return json.loads(content),meta
    except json.JSONDecodeError:
        starts=[x for x in (content.find("{"),content.find("[")) if x>=0]; start=min(starts,default=-1); end=max(content.rfind("}"),content.rfind("]"))
        if start>=0 and end>start:
            try:return json.loads(content[start:end+1]),{**meta,"parse_recovered":True}
            except Exception:pass
        return None,{**meta,"error":"invalid_json"}

def analyze_batch(posts:list[dict[str,Any]])->tuple[list[dict[str,Any]],dict[str,Any]]:
    compact=[{"post_id":str(p.get("post_id") or p.get("id") or ""),"title":str(p.get("title") or "")[:220],"content":str(p.get("content") or "")[:2400]} for p in posts]
    prompt="""Analyze these Moltbook posts as a research peer. For each post decide whether a specific evidence-gap question is justified. Ignore opinion-only, promotional, casual, or unsupported posts. Return ONLY {\"results\":[{\"post_id\":\"...\",\"decision\":\"comment\",\"question\":\"...\"},{\"post_id\":\"...\",\"decision\":\"ignore\"}]}. One result per input, same order. A comment question must use concrete evidence, metric, method, limitation, comparison, or unresolved question actually present. Do not summarize. Never start with For, You report, The post, or Interesting. Never invent facts. Question <=320 chars. Prefer falsifiable replication, uncertainty, boundary, ablation, transfer, or baseline tests.\n\nPOSTS:\n"""+json.dumps(compact,ensure_ascii=False)
    data,meta=_request(prompt)
    if not data:return [],meta
    results=data.get("results") if isinstance(data,dict) else data
    if not isinstance(results,list):return [],{**meta,"error":"missing_results"}
    valid={p["post_id"] for p in compact}; out=[]
    for item in results:
        if not isinstance(item,dict):continue
        pid=str(item.get("post_id") or ""); decision=str(item.get("decision") or "ignore").lower(); q=str(item.get("question") or "").strip()
        if pid not in valid:continue
        out.append({"post_id":pid,"decision":"comment","question":q[:320]} if decision=="comment" and q else {"post_id":pid,"decision":"ignore"})
    return out,meta

def _tokens(text:str)->set[str]:return {x for x in re.findall(r"[a-z][a-z0-9._-]{4,}",text.lower()) if x not in GENERIC_WORDS}

def source_domain_safe(title:str,content:str,question:str)->bool:
    if not question or len(question)>MAX_COMMENT_CHARS:return False
    q=question.lower().strip()
    if q.startswith(("for ","you report","the post","interesting")) or "how would you validate this claim on held-out or independent cases" in q:return False
    post=f"{title}\n{content}"; shared=_tokens(post)&_tokens(question)
    if len(shared)>=2:return True
    nums=set(re.findall(r"\b\d+(?:\.\d+)?%?\b",post)); return bool(nums & set(re.findall(r"\b\d+(?:\.\d+)?%?\b",question)))

def regenerate_comment(title:str,content:str,bad_question:str="")->tuple[str|None,dict[str,Any]]:
    prompt=f'''Write ONE concise research-peer question about this post.\nTitle: {title[:240]}\nContent: {content[:2800]}\nRejected draft: {bad_question[:420]}\nReturn ONLY {{"question":"..."}}. One sentence, <=300 characters. Use at least two concrete source anchors when possible. Ask about replication, uncertainty, boundary, ablation, baseline, transfer, or another falsifiable test. No summary. Never start with For, You report, The post, or Interesting. Never invent facts.'''
    data,meta=_request(prompt,max_tokens=450)
    if isinstance(data,dict):
        q=str(data.get("question") or "").strip(); return (q[:300] if q else None),meta
    return None,meta

def choose_final_comment(post:dict[str,Any],ai_question:str|None)->tuple[str|None,dict[str,Any]]:
    title=str(post.get("title") or ""); content=str(post.get("content") or "")
    if ai_question and source_domain_safe(title,content,ai_question):return ai_question.strip(),{"source":"ai"}
    regenerated,meta=regenerate_comment(title,content,ai_question or "")
    if regenerated and source_domain_safe(title,content,regenerated):return regenerated.strip(),{"source":"ai_regenerated",**meta}
    return None,{"source":"none","reason":"AI comment rejected after one regeneration",**meta}
