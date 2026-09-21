from __future__ import annotations

import csv, io, json, math, os, re, statistics, zipfile
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult, Hypothesis

router = APIRouter(prefix="/research", tags=["Unified Research Engine"])

ROOT = Path(os.getenv("RESEARCH_INPUT_ROOT", "research_inputs")).resolve()
MAX_MB = int(os.getenv("RESEARCH_UPLOAD_MAX_MB", "100"))
MAX_BYTES = MAX_MB * 1024 * 1024
MIN_GROUP_N = int(os.getenv("RESEARCH_MIN_GROUP_N", "20"))
PATTERN_GAP = float(os.getenv("RESEARCH_PATTERN_GAP", "0.15"))


def now(): return datetime.now(timezone.utc)


def safe_id(v: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v):
        raise HTTPException(400, "Invalid experiment_id")
    return v


def parse_dt(v: Any) -> datetime | None:
    if v is None: return None
    s = str(v).strip()
    if not s: return None
    s = s.replace(".", "-", 2)
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"]
    for f in fmts:
        try: return datetime.strptime(s, f).replace(tzinfo=timezone.utc)
        except ValueError: pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception: return None


def num(v: Any) -> float | None:
    if v is None: return None
    s = str(v).strip().replace(",", "")
    if not s: return None
    s = s.replace("%", "")
    try: return float(s)
    except Exception: return None


def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def pick(row: dict[str, Any], *names: str) -> Any:
    nmap = {norm(k): v for k,v in row.items()}
    for name in names:
        if norm(name) in nmap: return nmap[norm(name)]
    for k,v in nmap.items():
        for name in names:
            nn = norm(name)
            if nn and (nn in k or k in nn): return v
    return None


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.tables=[]; self._table=None; self._row=None; self._cell=None
    def handle_starttag(self, tag, attrs):
        if tag.lower()=="table": self._table=[]
        elif self._table is not None and tag.lower()=="tr": self._row=[]
        elif self._row is not None and tag.lower() in ("td","th"): self._cell=[]
    def handle_data(self, data):
        if self._cell is not None: self._cell.append(data)
    def handle_endtag(self, tag):
        t=tag.lower()
        if t in ("td","th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split())); self._cell=None
        elif t=="tr" and self._table is not None and self._row is not None:
            if self._row: self._table.append(self._row)
            self._row=None
        elif t=="table" and self._table is not None:
            if self._table: self.tables.append(self._table)
            self._table=None


def parse_html_tables(raw: bytes) -> list[dict[str,Any]]:
    p=TableParser(); p.feed(raw.decode("utf-8", "replace")); out=[]
    for table in p.tables:
        if not table: continue
        header_i=None
        for i,row in enumerate(table[:8]):
            joined=" ".join(norm(x) for x in row)
            if any(x in joined for x in ("time","profit","positionid","deal")):
                header_i=i; break
        if header_i is None: continue
        headers=table[header_i]
        for row in table[header_i+1:]:
            if len(row)<2: continue
            if len(row)<len(headers): row=row+[""]*(len(headers)-len(row))
            out.append(dict(zip(headers,row[:len(headers)])))
    return out


def parse_csv(raw: bytes) -> list[dict[str,Any]]:
    text=raw.decode("utf-8-sig", "replace")
    sample=text[:8192]
    try: dialect=csv.Sniffer().sniff(sample, delimiters=",;\t")
    except Exception: dialect=csv.excel
    return [dict(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]


def parse_xml(raw: bytes) -> list[dict[str,Any]]:
    import xml.etree.ElementTree as ET
    root=ET.fromstring(raw)
    rows=[]
    for el in root.iter():
        children=list(el)
        if children and all(len(list(c))==0 for c in children):
            row={c.tag.split("}")[-1]: (c.text or "") for c in children}
            if len(row)>=3: rows.append(row)
    return rows


def extract_files(raw: bytes, filename: str) -> tuple[list[dict[str,Any]], list[dict[str,Any]], dict[str,Any]]:
    """Return deal/trade rows, OHLC rows, and source inventory."""
    blobs=[]
    if filename.lower().endswith(".zip"):
        try:
            z=zipfile.ZipFile(io.BytesIO(raw))
            for n in z.namelist():
                if n.endswith("/"): continue
                low=n.lower()
                if low.endswith((".csv",".html",".htm",".xml")):
                    blobs.append((n,z.read(n)))
        except zipfile.BadZipFile as e: raise HTTPException(400,"Invalid backtest ZIP") from e
    else: blobs=[(filename,raw)]
    deal_rows=[]; ohlc=[]; inventory=[]
    for name,data in blobs:
        low=name.lower()
        try:
            rows=parse_html_tables(data) if low.endswith((".html",".htm")) else parse_xml(data) if low.endswith(".xml") else parse_csv(data)
        except Exception:
            rows=[]
        inventory.append({"file":name,"rows":len(rows)})
        for r in rows:
            keys=" ".join(norm(k) for k in r.keys())
            o=num(pick(r,"Open","Open Price")); h=num(pick(r,"High")); l=num(pick(r,"Low")); c=num(pick(r,"Close"))
            if all(x is not None for x in (h,l,c)) and (o is not None):
                ohlc.append(r)
            elif any(x in keys for x in ("positionid","profit","deal","entry","ordertype")) and any(x in keys for x in ("time","opentime","closetime")):
                deal_rows.append(r)
    return deal_rows, ohlc, {"files":inventory}


def parse_ea(source: str) -> dict[str,Any]:
    low=source.lower()
    def has(*x): return any(t.lower() in low for t in x)
    params={}
    for name in ("RiskPercent","TargetRR","ATRPeriod","MinCandleATR","MaxCandleATR","MaxSpreadPoints","SRLookbackBars"):
        m=re.search(rf"\b{name}\s*=\s*([0-9]+(?:\.[0-9]+)?)", source)
        if m: params[name]=float(m.group(1))
    return {"line_count":len(source.splitlines()),"has_buy":has(".Buy(","trade.Buy"),"has_sell":has(".Sell(","trade.Sell"),"has_atr":has("iATR","ATR"),"has_breakout":has("breakout"),"has_support_resistance":has("support","resistance"),"has_risk_guard":has("MaxDailyLoss","MaxTotalLoss","RiskGuard"),"parameters":params}


def build_trades(rows: list[dict[str,Any]]) -> list[dict[str,Any]]:
    trades=[]
    # First: one-row completed trade formats.
    for i,r in enumerate(rows,1):
        keys={norm(k) for k in r.keys()}
        has_open=any(k in keys for k in ("opentime","entrytime"))
        has_close=any(k in keys for k in ("closetime","exittime"))
        if not (has_open and has_close):
            continue
        ot=parse_dt(pick(r,"Open Time","Entry Time","EntryTime","OpenTime")); ct=parse_dt(pick(r,"Close Time","Exit Time","ExitTime","CloseTime"))
        profit=num(pick(r,"Profit","Net Profit","P&L","PnL"))
        entry=num(pick(r,"Entry Price","Open Price","EntryPrice","OpenPrice")); exitp=num(pick(r,"Exit Price","Close Price","ExitPrice","ClosePrice"))
        side=str(pick(r,"Type","Direction","Side","Order Type") or "").lower()
        if ot and ct and profit is not None:
            trades.append({"trade_id":str(pick(r,"Position ID","PositionID","Deal","Ticket") or i),"entry_time":ot.isoformat(),"exit_time":ct.isoformat(),"entry_price":entry,"exit_price":exitp,"profit":profit,"side":side})
    if trades: return trades
    # Deal format: pair IN/OUT by position id.
    groups=defaultdict(list)
    for i,r in enumerate(rows,1):
        t=parse_dt(pick(r,"Time","Date","Timestamp"));
        if not t: continue
        pos=str(pick(r,"Position ID","PositionID","Position","Ticket") or "")
        entry=str(pick(r,"Entry","Deal Entry","Entry Type") or "").lower()
        side=str(pick(r,"Type","Direction","Side") or "").lower()
        groups[pos or f"row{i}"].append((t,r,entry,side))
    for j,(pos,items) in enumerate(groups.items(),1):
        items.sort(key=lambda x:x[0]); ins=[x for x in items if any(k in x[2] for k in ("in","entry","open"))]; outs=[x for x in items if any(k in x[2] for k in ("out","exit","close"))]
        if not ins or not outs: continue
        a=ins[0]; b=outs[-1]
        profit=sum(num(pick(x[1],"Profit","P&L","PnL")) or 0 for x in items)
        trades.append({"trade_id":pos or str(j),"entry_time":a[0].isoformat(),"exit_time":b[0].isoformat(),"entry_price":num(pick(a[1],"Price","Entry Price")),"exit_price":num(pick(b[1],"Price","Exit Price")),"profit":profit,"side":a[3]})
    return trades


def build_market(rows: list[dict[str,Any]]) -> list[dict[str,Any]]:
    out=[]
    for r in rows:
        t=parse_dt(pick(r,"Time","Date","Datetime","Timestamp")); o=num(pick(r,"Open","Open Price")); h=num(pick(r,"High")); l=num(pick(r,"Low")); c=num(pick(r,"Close"))
        if t and None not in (o,h,l,c): out.append({"time":t,"open":o,"high":h,"low":l,"close":c})
    return sorted(out,key=lambda x:x["time"])


def atr14(candles):
    tr=[]; out=[None]*len(candles)
    for i,c in enumerate(candles):
        prev=candles[i-1]["close"] if i else c["close"]
        tr.append(max(c["high"]-c["low"],abs(c["high"]-prev),abs(c["low"]-prev)))
        if i>=13: out[i]=sum(tr[i-13:i+1])/14
    return out


def enrich_trade(t, candles, atrs):
    et=datetime.fromisoformat(t["entry_time"]); idx=None
    for i,c in enumerate(candles):
        if c["time"] <= et: idx=i
        else: break
    if idx is None: return None
    # Conservative: exclude candle containing entry if its close is after entry.
    c=candles[idx]
    body=abs(c["close"]-c["open"]); rng=max(c["high"]-c["low"],1e-12); upper=c["high"]-max(c["open"],c["close"]); lower=min(c["open"],c["close"])-c["low"]
    direction="bullish" if c["close"]>c["open"] else "bearish" if c["close"]<c["open"] else "doji"
    streak=1
    for j in range(idx-1,-1,-1):
        d="bullish" if candles[j]["close"]>candles[j]["open"] else "bearish" if candles[j]["close"]<candles[j]["open"] else "doji"
        if d==direction and direction!="doji": streak+=1
        else: break
    side=t.get("side","")
    if "sell" in side or "short" in side: side_sign=-1
    else: side_sign=1
    momentum_3=None
    if atrs[idx] and idx>=3:
        momentum_3=(c["close"]-candles[idx-3]["close"])/atrs[idx]
    regime="unknown"
    if atrs[idx]:
        rr=rng/atrs[idx]
        regime="low_volatility" if rr<0.75 else "high_volatility" if rr>1.5 else "normal_volatility"
    entry=t.get("entry_price")
    mfe=mae=None
    xt=datetime.fromisoformat(t["exit_time"])
    if entry is not None:
        window=[x for x in candles if et <= x["time"] <= xt]
        if window:
            fav=max((x["high"]-entry)*side_sign for x in window)
            adv=min((x["low"]-entry)*side_sign for x in window)
            mfe=fav; mae=adv
    return {"trade":t,"candle_index":idx,"direction":direction,"body_pct_range":body/rng,"range":rng,"upper_wick_pct":upper/rng,"lower_wick_pct":lower/rng,"streak":streak,"atr14":atrs[idx],"range_atr":(rng/atrs[idx] if atrs[idx] else None),"momentum_3_atr":momentum_3,"volatility_regime":regime,"mfe":mfe,"mae":mae}


def pct(v): return round(v*100,2)

def analyze(trades, contexts, ea):
    n=len(trades); wins=[t for t in trades if (t.get("profit") or 0)>0]; losses=[t for t in trades if (t.get("profit") or 0)<0]
    findings=[]; insufficient=[]
    # Reference-only accounting, not presented as research findings.
    reference={"trade_count":n,"positive_trade_count":len(wins),"negative_trade_count":len(losses)}
    if not contexts:
        return {"reference":reference,"findings":[],"insufficient_evidence":["MARKET_OHLC_NOT_AVAILABLE_FROM_BACKTEST_INPUT"],"mfe_mae":None}
    pairs=[c for c in contexts if c]
    for feature,label,fmt in [
        ("direction","prior closed candle direction",lambda x:x),
        ("streak","consecutive prior candle streak",lambda x:("3+" if x>=3 else str(x))),
        ("volatility_regime","prior-candle volatility regime",lambda x:x),
    ]:
        buckets=defaultdict(list)
        for c in pairs: buckets[fmt(c[feature])].append(c)
        for bucket,items in buckets.items():
            pos=sum(1 for c in items if c["trade"]["profit"]>0); total=len(items)
            if total<MIN_GROUP_N: insufficient.append({"feature":label,"bucket":bucket,"n":total,"reason":"below_minimum_sample"}); continue
            # Compare against all other contexts.
            other=[c for c in pairs if c not in items]
            if len(other)<MIN_GROUP_N: insufficient.append({"feature":label,"bucket":bucket,"n":total,"reason":"comparison_group_too_small"}); continue
            r1=pos/total; r2=sum(1 for c in other if c["trade"]["profit"]>0)/len(other)
            if abs(r1-r2)>=PATTERN_GAP:
                findings.append({"status":"OBSERVED_PATTERN","feature":label,"condition":bucket,"n":total,"reference_outcome_rate":round(r1,4),"comparison_n":len(other),"comparison_outcome_rate":round(r2,4),"gap":round(r1-r2,4),"evidence":{"trade_ids":[c["trade"]["trade_id"] for c in items[:50]]},"interpretation":"Observed association in supplied backtest data; not evidence of causation."})
    # Momentum buckets are computed only from candles preceding the entry.
    mom=[c for c in pairs if c.get("momentum_3_atr") is not None]
    if len(mom)>=2*MIN_GROUP_N:
        for condition,items in (("negative_momentum",[c for c in mom if c["momentum_3_atr"]< -0.5]),("neutral_momentum",[c for c in mom if -0.5<=c["momentum_3_atr"]<=0.5]),("positive_momentum",[c for c in mom if c["momentum_3_atr"]>0.5])):
            if len(items)<MIN_GROUP_N: continue
            other=[c for c in mom if c not in items]
            if len(other)<MIN_GROUP_N: continue
            r1=sum(c["trade"]["profit"]>0 for c in items)/len(items); r2=sum(c["trade"]["profit"]>0 for c in other)/len(other)
            if abs(r1-r2)>=PATTERN_GAP:
                findings.append({"status":"OBSERVED_PATTERN","feature":"3-candle momentum in ATR units","condition":condition,"n":len(items),"reference_outcome_rate":round(r1,4),"comparison_n":len(other),"comparison_outcome_rate":round(r2,4),"gap":round(r1-r2,4),"evidence":{"trade_ids":[c["trade"]["trade_id"] for c in items[:50]]},"interpretation":"Observed association in supplied backtest data; not evidence of causation."})
    # Continuous features: range/ATR and candle body/range.
    for feature,label in [("range_atr","prior candle range / ATR14"),("body_pct_range","prior candle body / range")]:
        vals=[c for c in pairs if c.get(feature) is not None]
        if len(vals)<2*MIN_GROUP_N: insufficient.append({"feature":label,"n":len(vals),"reason":"insufficient_total_sample"}); continue
        vals_sorted=sorted(vals,key=lambda c:c[feature]); cut=vals_sorted[len(vals_sorted)//2]; med=cut[feature]
        low=[c for c in vals if c[feature]<=med]; high=[c for c in vals if c[feature]>med]
        if len(low)<MIN_GROUP_N or len(high)<MIN_GROUP_N: continue
        rl=sum(c["trade"]["profit"]>0 for c in low)/len(low); rh=sum(c["trade"]["profit"]>0 for c in high)/len(high)
        if abs(rl-rh)>=PATTERN_GAP:
            findings.append({"status":"OBSERVED_PATTERN","feature":label,"condition":f"<=median({med:.4g}) vs >median","n_low":len(low),"n_high":len(high),"outcome_rate_low":round(rl,4),"outcome_rate_high":round(rh,4),"gap":round(rl-rh,4),"evidence":{"low_trade_ids":[c["trade"]["trade_id"] for c in low[:50]],"high_trade_ids":[c["trade"]["trade_id"] for c in high[:50]]},"interpretation":"Observed association in supplied backtest data; not evidence of causation."})
    mfe=[c["mfe"] for c in pairs if c.get("mfe") is not None]; mae=[c["mae"] for c in pairs if c.get("mae") is not None]
    return {"reference":reference,"findings":findings,"insufficient_evidence":insufficient,"mfe_mae":{"sample":len(mfe),"mfe_median":statistics.median(mfe) if mfe else None,"mae_median":statistics.median(mae) if mae else None}}


def make_experiment(db: Session, experiment_id: str, symbol: str, timeframe: str, ea_name: str) -> Experiment:
    e=db.query(Experiment).filter(Experiment.id==experiment_id).first()
    if e: return e
    h=Hypothesis(id=str(uuid4()),title=f"Unified research: {ea_name}",statement="Evaluate pre-entry market context and post-entry behavior from supplied EA/backtest evidence without look-ahead bias.",assumptions="Only supplied files are evidence. Missing OHLC means market-context findings are unavailable.",status="active")
    db.add(h); db.flush()
    e=Experiment(id=experiment_id,hypothesis_id=h.id,symbol=symbol or "UNKNOWN",timeframe=timeframe or "UNKNOWN",experiment_type="unified_backtest_research",specification="EA .mq5 + MT5 backtest input; normalize once, analyze once, evidence-gate findings.",baseline="",status="uploaded")
    db.add(e); db.commit(); return e


@router.post("/data/upload", summary="Upload EA + MT5 backtest bundle")
async def upload_data(experiment_id: str = Form(None), symbol: str = Form(""), timeframe: str = Form(""), ea_file: UploadFile = File(...), backtest_file: UploadFile = File(...)):
    if not ea_file.filename or not ea_file.filename.lower().endswith(".mq5"): raise HTTPException(400,"ea_file must be .mq5")
    if not backtest_file.filename or not backtest_file.filename.lower().endswith((".csv",".html",".htm",".xml",".zip")): raise HTTPException(400,"backtest_file must be CSV/HTML/XML/ZIP")
    eid=safe_id(experiment_id) if experiment_id else str(uuid4())
    folder=ROOT/eid; folder.mkdir(parents=True,exist_ok=True)
    ea=await ea_file.read(); bt=await backtest_file.read()
    if len(ea)>MAX_BYTES or len(bt)>MAX_BYTES: raise HTTPException(413,f"File exceeds {MAX_MB} MB limit")
    (folder/"ea.mq5").write_bytes(ea); (folder/"backtest").write_bytes(bt); (folder/"manifest.json").write_text(json.dumps({"ea_filename": ea_file.filename, "backtest_filename": backtest_file.filename}, ensure_ascii=False), encoding="utf-8")
    db=SessionLocal()
    try:
        make_experiment(db,eid,symbol,timeframe,ea_file.filename)
        return {"status":"uploaded","experiment_id":eid,"files":{"ea":"ea.mq5","backtest":backtest_file.filename},"next":"POST /api/research/{experiment_id}/run"}
    finally: db.close()


@router.get("/{experiment_id}")
def get_research(experiment_id: str):
    eid=safe_id(experiment_id); db=SessionLocal()
    try:
        e=db.query(Experiment).filter(Experiment.id==eid).first()
        if not e: raise HTTPException(404,"Experiment not found")
        r=db.query(ExperimentResult).filter(ExperimentResult.experiment_id==eid).order_by(ExperimentResult.created_at.desc()).first()
        return {"experiment_id":eid,"status":e.status,"symbol":e.symbol,"timeframe":e.timeframe,"result":json.loads(r.metrics) if r else None,"result_id":r.id if r else None}
    finally: db.close()


@router.post("/{experiment_id}/run")
def run_research(experiment_id: str):
    eid=safe_id(experiment_id); folder=ROOT/eid
    if not (folder/"ea.mq5").is_file() or not (folder/"backtest").is_file(): raise HTTPException(409,"Upload EA and backtest first")
    db=SessionLocal()
    try:
        e=db.query(Experiment).filter(Experiment.id==eid).first()
        if not e: raise HTTPException(404,"Experiment not found")
        ea=(folder/"ea.mq5").read_text("utf-8",errors="replace"); bt=(folder/"backtest").read_bytes()
        manifest=json.loads((folder/"manifest.json").read_text("utf-8")) if (folder/"manifest.json").is_file() else {"backtest_filename":"backtest.csv"}
        deal_rows,ohlc_rows,inventory=extract_files(bt,manifest.get("backtest_filename","backtest.csv"))
        trades=build_trades(deal_rows); candles=build_market(ohlc_rows); atrs=atr14(candles)
        contexts=[enrich_trade(t,candles,atrs) for t in trades] if candles else []
        analysis=analyze(trades,contexts,parse_ea(ea))
        limitations=[]
        if not candles: limitations.append("MT5 backtest input did not expose usable OHLC candles; pre-entry price-action/regime analysis is NOT_AVAILABLE_FROM_INPUT_DATA.")
        if not trades: limitations.append("No completed trades could be reconstructed from the supplied backtest input.")
        if analysis["findings"]==[]: limitations.append("No pattern passed the evidence gate; this is not evidence that no relationship exists.")
        result_obj={"engine":"unified_research_v1","status":"completed","experiment_id":eid,"input_lineage":{"ea_file":"ea.mq5","backtest_files":inventory},"ea_analysis":parse_ea(ea),"data_quality":{"parsed_trade_count":len(trades),"market_candle_count":len(candles),"market_context_coverage":round(len(contexts)/len(trades),4) if trades else 0},"analysis":analysis,"limitations":limitations,"evidence_policy":{"min_group_n":MIN_GROUP_N,"pattern_gap":PATTERN_GAP,"lookahead_policy":"Only candles at or before entry timestamp are used for pre-entry context; current unclosed candle is not used when its close is after entry.","causality":"not_claimed"},"generated_at":now().isoformat()}
        r=ExperimentResult(id=str(uuid4()),experiment_id=e.id,summary="Unified EA + MT5 backtest research",metrics=json.dumps(result_obj,ensure_ascii=False,default=str),evidence=json.dumps({"trade_ids":[t["trade_id"] for t in trades],"market_candles":len(candles)},ensure_ascii=False),limitations=json.dumps(limitations,ensure_ascii=False),conclusion="FINDINGS_PUBLISHED_ONLY_IF_EVIDENCE_GATE_PASSED")
        db.add(r); e.status="completed"; e.completed_at=now(); db.commit(); db.refresh(r)
        return {"status":"completed","experiment_id":eid,"result_id":r.id,"research":result_obj}
    finally: db.close()
