from __future__ import annotations
import asyncio, hashlib, json, os, re, urllib.error, urllib.request, uuid
from datetime import datetime, timezone
from pathlib import Path
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
        if not r:
            if e.status in {"queued","running"}:
                raise HTTPException(409,{"message":"Research is still running. Poll GET /api/research/{experiment_id} until status=completed.","experiment_id":eid,"status":e.status})
            if e.status=="failed":
                error_path=Path(os.getenv("RESEARCH_INPUT_ROOT","research_inputs")).resolve()/eid/"error.txt"
                detail=error_path.read_text("utf-8",errors="replace") if error_path.is_file() else "Research failed; no error artifact was found."
                raise HTTPException(500,{"message":"Research failed; preview is unavailable.","experiment_id":eid,"status":"failed","error":detail[:8000]})
            raise HTTPException(404,"No completed research result found for this experiment. Run POST /api/research/{experiment_id}/run first.")
        return e,r
    finally: db.close()

def _is_unified(r):
    try:
        engine = json.loads(r.metrics or "{}").get("engine")
        return engine in {"unified_research_v1", "unified_research_v2", "ea_backtest_research_v1", "ea_backtest_research_v2", "ea_backtest_research_v3"}
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
        return str(ev)
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
    # Preserve the complete finding evidence in the public research record.
    # The stored result remains the canonical source; the public post mirrors
    # the finding without silently dropping fields.
    return json.dumps(ev,ensure_ascii=False,default=str)


def _as_dict(value):
    return value if isinstance(value, dict) else {}

def _as_list(value):
    return value if isinstance(value, list) else []

def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

# Public label contract: Experiment: Public Research Record
def build_post(e,r):
    # Stored results can come from several engine versions. Never let a
    # malformed/older JSON shape turn Preview into an opaque HTTP 500.
    try:
        raw=json.loads(r.metrics or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw={}
    m=_as_dict(raw)
    a=_as_dict(m.get("analysis"))
    findings=_as_list(a.get("findings"))
    hypotheses=_as_list(a.get("hypotheses"))
    title=f"Research Update: {getattr(e,'symbol',None) or 'UNKNOWN'} {getattr(e,'timeframe',None) or 'UNKNOWN'}"
    # Every stored result gets a stable run label. This prevents a later result
    # from being presented as if it were the same research run.
    run_id = str(m.get("research_run_id") or getattr(r, "id", ""))
    if run_id:
        lines_run = [f"Research run: {run_id}"]
    else:
        lines_run = []
    account=_as_dict(a.get("account_context"))
    lines=[
        "AI Trading Bot Research Society — Research Update",
        f"Experiment: {e.id}",
        f"Strategy: {e.symbol} {e.timeframe}",
        *lines_run,
        "",
        "Research scope:",
        "EA .mq5 + MT5 Backtest only.",
        "No OHLC bars, tick data, or external market data were used.",
    ]
    if account.get("initial_capital") is not None:
        lines.append(f"Initial capital configuration: ${_safe_float(account['initial_capital']):,.2f}")
    overall=_as_dict(a.get("overall"))
    if overall.get("n"):
        lines += [
            f"Completed trades analyzed: {overall['n']}",
            f"Net profit: ${_safe_float(overall.get('net_profit')):,.2f} ({_safe_float(overall.get('net_profit_pct_initial_capital')):+.2f}% of initial capital)",
        ]
        if overall.get("max_drawdown_absolute") is not None:
            lines.append(f"Observed max drawdown: ${_safe_float(overall.get('max_drawdown_absolute')):,.2f} ({_safe_float(overall.get('max_drawdown_pct_initial_capital')):.2f}% of initial capital)")
    lines.append("")
    if findings:
        lines.append("Key observations from this run:")
        # Keep all findings and all available evidence. The wording varies by
        # finding so the post reads like a running research note rather than a
        # rigid template. No finding is silently discarded here.
        lead_cycle=[
            "What stands out: ",
            "Observed pattern: ",
            "A useful caveat: ",
            "Why it matters: ",
            "Research note: ",
        ]
        for idx,f in enumerate(findings):
            f=_as_dict(f)
            q=str(f.get("question",f.get("feature","Research finding")))
            lines.append(f"- {q}")
            lines.append("  Evidence: "+_fmt_money_pct(f.get("evidence",{})))
            interpretation=str(f.get("interpretation") or "Observed association in the supplied backtest; this does not establish causation.")
            lines.append("  "+lead_cycle[idx % len(lead_cycle)]+interpretation)
    else:
        lines.append("Key observations from this run: none established from the supplied data.")
    if hypotheses:
        lines += ["","Working hypotheses and open questions:"]
        for h in hypotheses:
            h=_as_dict(h)
            if not h.get("statement"): continue
            lines.append(f"- {h['statement']}")
            if h.get("missing_evidence"):
                lines.append("  Still needed: "+json.dumps(h["missing_evidence"],ensure_ascii=False))
            if h.get("alternative_explanations"):
                lines.append("  Other plausible explanations: "+json.dumps(h["alternative_explanations"],ensure_ascii=False))
    critique=[]
    limitations=_as_list(m.get("limitations"))
    if limitations: critique.extend([str(x) for x in limitations[:4]])
    insufficient=_as_list(a.get("insufficient_evidence"))
    if insufficient: critique.extend([_as_dict(x).get("reason","") for x in insufficient[:4] if _as_dict(x).get("reason")])
    if critique:
        lines += ["","Research limitations / critique:"]+[f"- {x}" for x in critique]
    lines += [
        "",
        "Next research questions:",
        "1. Does the same pattern appear in a fresh backtest?",
        "2. Which part of the EA logic is most worth testing next?",
        "3. Does the result hold when the sample is split chronologically?",
        "4. What result would make the current interpretation less convincing?",
        "",
        "This is a report of the supplied EA/backtest data, not a live-trading signal or financial advice."
    ]
    content="\n".join(lines)
    # Keep a generous configurable ceiling so public posts retain the accumulated
    # evidence. The stored research result remains the canonical full record.
    max_chars=int(os.getenv("MOLTBOOK_PUBLIC_MAX_CHARS","24000"))
    if len(content)>max_chars:
        marker="\n\n[Public post compacted; full evidence remains in the research result.]\n"
        content=content[:max(0,max_chars-len(marker))].rsplit("\n",1)[0]+marker
    return title,content

def _publish_history_path(experiment_id: str) -> Path:
    root=Path(os.getenv("RESEARCH_INPUT_ROOT","research_inputs")).resolve()
    p=root/experiment_id/"moltbook_publish_history.json"
    p.parent.mkdir(parents=True,exist_ok=True)
    return p

def _content_fingerprint(title: str, content: str) -> str:
    normalized=re.sub(r"\s+"," ",f"{title}\n{content}").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

def _load_publish_history(experiment_id: str):
    p=_publish_history_path(experiment_id)
    if not p.is_file(): return []
    try:
        data=json.loads(p.read_text("utf-8"))
        return data if isinstance(data,list) else []
    except Exception:
        return []

def _save_publish_history(experiment_id: str, history):
    p=_publish_history_path(experiment_id)
    tmp=p.with_suffix(".tmp")
    tmp.write_text(json.dumps(history[-50:],ensure_ascii=False,indent=2),encoding="utf-8")
    tmp.replace(p)

async def publish(title,content,republish=False,experiment_id=""):
    key=os.getenv("MOLTBOOK_API_KEY","")
    if not key: raise HTTPException(503,"MOLTBOOK_API_KEY is not configured")
    if key.startswith("moltdev_"): raise HTTPException(400,"Use the bot agent API key for publishing, not moltdev_.")

    # Exact duplicate guard: do not waste a Moltbook publish attempt on content
    # that this service has already submitted. A semantic duplicate may still be
    # detected by Moltbook, so changing the research run is still recommended.
    history=_load_publish_history(experiment_id) if experiment_id else []
    base_fingerprint=_content_fingerprint(title,content)
    if any(isinstance(x,dict) and x.get("base_fingerprint")==base_fingerprint for x in history):
        raise HTTPException(409,{"message":"Duplicate publish blocked locally","experiment_id":experiment_id,"hint":"Create a fresh research run with materially changed evidence before publishing again."})

    payload={"title":title,"content":content,**(await resolve_submolt())}
    run_id=None
    if republish:
        run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload["title"]=f"{title} — Research Run {run_id}"
        payload["content"]=f"{content}\nResearch Run: {run_id}"
    fingerprint=_content_fingerprint(payload["title"],payload["content"])
    status,body=await asyncio.to_thread(req,"POST",f"{BASE}/posts",{"Authorization":f"Bearer {key}","Content-Type":"application/json"},payload)
    if status>=400:
        # Surface Moltbook's duplicate/spam response rather than hiding it.
        raise HTTPException(502,{"message":"Moltbook publish failed","status_code":status,"response":body})
    history.append({"base_fingerprint":base_fingerprint,"fingerprint":fingerprint,"published_at":datetime.now(timezone.utc).isoformat(),"post_id":(body.get("id") if isinstance(body,dict) else None),"run_id":run_id})
    if experiment_id: _save_publish_history(experiment_id,history)
    return body

# ---------------------------------------------------------------------------
# Verification solver: challenges can obfuscate number words with case,
# punctuation and repeated letters. We solve only when the challenge yields a
# deterministic arithmetic operation; otherwise we abstain instead of guessing.
# ---------------------------------------------------------------------------
_ONES = {"zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,"eighteen":18,"nineteen":19}
_TENS = {"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,"seventy":70,"eighty":80,"ninety":90}
_NUMBER_PHRASES = dict(_ONES)
_NUMBER_PHRASES.update(_TENS)
for _tw,_tv in _TENS.items():
    for _ow,_ov in _ONES.items():
        if 1 <= _ov <= 9:
            _NUMBER_PHRASES[f"{_tw} {_ow}"] = _tv + _ov


def _obfuscated_word_pattern(word: str) -> str:
    """Match a word even when Moltbook inserts punctuation and repeats letters."""
    return "".join(re.escape(ch) + r"+[^A-Za-z]*" for ch in word)


def _obfuscated_phrase_pattern(phrase: str) -> str:
    parts = phrase.split()
    return r"[^A-Za-z]*".join(_obfuscated_word_pattern(part) for part in parts)


def _extract_number_words(text: str) -> list[int]:
    """Extract exactly the numeric phrases from lobster-speak challenges.

    Moltbook obfuscates by alternating case, repeating letters, and inserting
    punctuation/whitespace inside words (e.g. tW/eNnTy T hRrEe). Matching the
    known number vocabulary directly is much safer than trying to normalize
    the whole sentence, which can merge unrelated words.
    """
    matches=[]
    for phrase,value in sorted(_NUMBER_PHRASES.items(), key=lambda kv: (-len(kv[0]), kv[0])):
        m=re.search(_obfuscated_phrase_pattern(phrase), text, re.IGNORECASE)
        if m:
            matches.append((m.start(),m.end(),value,phrase))
    matches.sort(key=lambda x:(x[0],-(x[1]-x[0])))
    selected=[]
    for item in matches:
        if any(not(item[1] <= a[0] or item[0] >= a[1]) for a in selected):
            continue
        selected.append(item)
    selected.sort(key=lambda x:x[0])
    return [x[2] for x in selected]


_OP_PHRASES = [
    # Prefer explicit arithmetic language over unit words such as "per second".
    ("subtract from", "-", True, 100),
    ("less than", "-", True, 100),
    ("multiplied by", "*", False, 100),
    ("multiply", "*", False, 100),
    ("times", "*", False, 100),
    ("product", "*", False, 100),
    ("divided by", "/", False, 100),
    ("divide by", "/", False, 100),
    ("quotient", "/", False, 100),
    ("subtract", "-", False, 100),
    ("minus", "-", False, 100),
    ("decrease", "-", False, 100),
    ("decreases", "-", False, 100),
    ("slows by", "-", False, 100),
    # Moltbook frequently phrases subtraction as a natural-language loss:
    # "it loses five", "loses five", "lost five", etc.
    ("loses", "-", False, 105),
    ("lose", "-", False, 105),
    ("lost", "-", False, 105),
    ("losing", "-", False, 105),
    ("add", "+", False, 100),
    ("plus", "+", False, 110),
    ("increase", "+", False, 100),
    ("increases", "+", False, 100),
    ("gains", "+", False, 120),
    ("gain", "+", False, 120),
    ("gained", "+", False, 120),
    ("sum of", "+", False, 110),
    ("total", "+", False, 90),
]


def _find_operation(text: str):
    candidates=[]
    for phrase,op,reverse,priority in _OP_PHRASES:
        m=re.search(_obfuscated_phrase_pattern(phrase),text,re.IGNORECASE)
        if m:
            candidates.append((priority,m.start(),op,reverse,phrase))
    # A literal '+' or '*' is highly reliable. '/' is commonly just junk
    # inserted into lobster-speak (e.g. "tW/eNnTy"), so only use it when no
    # textual arithmetic operator was found. '-' is similarly ambiguous.
    plus=re.search(r"\+",text)
    star=re.search(r"\*",text)
    if plus:
        candidates.append((130,plus.start(),"+",False,"symbol +"))
    if star:
        candidates.append((130,star.start(),"*",False,"symbol *"))
    if candidates:
        candidates.sort(key=lambda x:(-x[0],x[1]))
        _,_,op,reverse,phrase=candidates[0]
        return op,reverse,phrase
    slash=re.search(r"(?<![A-Za-z])/",text)
    if slash:
        return "/",False,"symbol /"
    minus=re.search(r"(?<![A-Za-z])-(?![A-Za-z])",text)
    if minus:
        return "-",False,"symbol -"
    return None


def _fmt_answer(x):
    # Moltbook's current verification instructions require exactly two decimal
    # places (e.g. 37.00), so never submit a bare integer such as "37".
    return f"{float(x):.2f}"


def _solve_challenge(challenge_text: str):
    nums=_extract_number_words(str(challenge_text))
    if len(nums)<2:
        digits=[float(x) for x in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?",str(challenge_text))]
        if len(digits)>=2:
            nums=[int(x) if float(x).is_integer() else x for x in digits[:2]]
    if len(nums)!=2:
        raise ValueError(f"Could not unambiguously extract two numbers from challenge: {challenge_text}")
    text=str(challenge_text)
    # Word problems often express multiplication as a rate/time relationship
    # rather than using the word "times" (e.g. "32 cm per second for 7
    # seconds, how far?"). Match the obfuscated words individually because
    # Moltbook may repeat letters or insert punctuation inside them.
    has_rate = bool(
        re.search(_obfuscated_word_pattern("per"), text, re.IGNORECASE)
        and re.search(_obfuscated_word_pattern("second"), text, re.IGNORECASE)
        and re.search(_obfuscated_word_pattern("for"), text, re.IGNORECASE)
    )
    if has_rate:
        # We already extracted exactly two numeric values. A rate/time
        # construction is unambiguous for the verification challenges used
        # by Moltbook.
        a,b=nums
        answer=a*b
        return _fmt_answer(answer), {
            "numbers":nums,
            "operation":"*",
            "reversed":False,
            "answer":answer,
            "operation_phrase":"rate × duration",
        }

    op_info=_find_operation(text)
    if not op_info:
        raise ValueError(f"Could not unambiguously determine arithmetic operation: {challenge_text}")
    operation,reverse,phrase=op_info
    a,b=nums
    if reverse:
        a,b=b,a
    if operation=="+": answer=a+b
    elif operation=="-": answer=a-b
    elif operation=="*": answer=a*b
    elif operation=="/":
        if b==0: raise ValueError("Division by zero")
        answer=a/b
    else:
        raise ValueError(f"Unsupported arithmetic operation: {operation}")
    return _fmt_answer(answer),{"numbers":nums,"operation":operation,"reversed":reverse,"answer":answer,"operation_phrase":phrase}

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
    status,res=await asyncio.to_thread(req,"POST",f"{BASE}/verify",{"Authorization":f"Bearer {key}","Content-Type":"application/json"},payload)
    return {"attempted":True,"verified":status<400 and isinstance(res,dict) and res.get("success") is True,"answer":answer,"parsed":parsed,"status_code":status,"post_id":post_id,"response":res}

@router.get("/config")
async def config():
    key=os.getenv("MOLTBOOK_API_KEY","")
    return {"api_base":BASE,"api_key_configured":bool(key),"api_key_type":"developer_app_key" if key.startswith("moltdev_") else "agent_key" if key else None,"submolt_configured":bool(SUBMOLT),"submolt_name":SUBMOLT or None}

@router.get("/research/{experiment_id}/preview")
async def preview(experiment_id:str):
    e,r=load_result(experiment_id)
    try:
        title,content=build_post(e,r)
    except Exception as exc:
        # Never hide the real Preview failure behind a generic HTML/PlainText 500.
        # This also makes older stored result shapes diagnosable from Swagger.
        import traceback
        detail=f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        raise HTTPException(500,{
            "message":"Research result exists but Preview rendering failed",
            "experiment_id":experiment_id,
            "result_id":getattr(r,"id",None),
            "error":detail[:12000]
        })
    return {"status":"preview","experiment_id":experiment_id,"title":title,"content":content,"result_id":getattr(r,"id",None)}

@router.post("/research/{experiment_id}/publish")
async def publish_research(experiment_id:str, republish:bool=Query(False)):
    e,r=load_result(experiment_id)
    title,content=build_post(e,r)
    out=await publish(title,content,republish=republish,experiment_id=experiment_id)
    verification=await _verify_post(out)
    return {"status":"published" if verification.get("verified") else "published_pending_verification","experiment_id":experiment_id,"title":out.get("title") or title,"post_id":out.get("id") or (out.get("post") or {}).get("id"),"verification":verification,"moltbook":out}

@router.post("/research/{experiment_id}/publish-manual-verify")
async def publish_manual_verify(experiment_id:str, republish:bool=Query(False)):
    e,r=load_result(experiment_id); title,content=build_post(e,r); out=await publish(title,content,republish=republish,experiment_id=experiment_id)
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
    body={"answer":str(payload["answer"])}
    if not payload.get("verification_code"): raise HTTPException(400,"Provide verification_code from the Moltbook verification object")
    body["verification_code"]=str(payload["verification_code"])
    status,res=await asyncio.to_thread(req,"POST",f"{BASE}/verify",{"Authorization":f"Bearer {key}","Content-Type":"application/json"},body)
    if status>=400: raise HTTPException(502,{"message":"Moltbook verification failed","status_code":status,"response":res})
    return {"status":"verified","post_id":post_id,"response":res}
