from __future__ import annotations

import json, os, re, statistics, math, traceback, concurrent.futures, threading
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, BackgroundTasks
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult, Hypothesis

router = APIRouter(prefix="/research", tags=["Research Engine — EA + Backtest"])
ROOT = Path(os.getenv("RESEARCH_INPUT_ROOT", "research_inputs")).resolve()
MAX_MB = int(os.getenv("RESEARCH_UPLOAD_MAX_MB", "512"))
MAX_BYTES = MAX_MB * 1024 * 1024
MIN_GROUP_N = int(os.getenv("RESEARCH_MIN_GROUP_N", "10"))
INITIAL_CAPITAL = float(os.getenv("RESEARCH_INITIAL_CAPITAL", "10000"))
# Background research executor. Using an explicit executor avoids relying on
# Starlette/FastAPI BackgroundTasks for long CPU/file jobs and makes the job
# lifecycle observable through the API. The job still lives in this process;
# for multi-worker deployments keep workers=1 unless a real external queue is used.
RESEARCH_WORKERS = max(1, int(os.getenv("RESEARCH_WORKERS", "2")))
RESEARCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=RESEARCH_WORKERS, thread_name_prefix="research")


def now(): return datetime.now(timezone.utc)

def safe_id(v):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v): raise HTTPException(400, "Invalid experiment_id")
    return v

def norm(s): return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())

def num(v):
    if v is None: return None
    try: return float(str(v).strip().replace(",", "").replace("%", ""))
    except: return None

def parse_dt(v):
    if v is None: return None
    s=str(v).strip()
    if not s: return None
    for f in ("%Y.%m.%d %H:%M:%S","%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M","%Y-%m-%dT%H:%M:%S","%Y-%m-%d"):
        try: return datetime.strptime(s,f).replace(tzinfo=timezone.utc)
        except ValueError: pass
    try: return datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.utc)
    except: return None

def pick(row,*names):
    m={norm(k):v for k,v in row.items()}
    for n in names:
        if norm(n) in m: return m[norm(n)]
    for k,v in m.items():
        if any(norm(n) in k or k in norm(n) for n in names): return v
    return None

def decode_bytes(raw):
    # MT5's own report exports are inconsistent: the Strategy Tester "Save as
    # Report" CSV is commonly UTF-16 (with or without a BOM), while other
    # exports are plain UTF-8. Guessing wrong silently corrupts every field,
    # which previously caused parsing to find zero rows/headers with no
    # visible error until the whole job crashed further downstream.
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try: return raw.decode("utf-16")
        except Exception: pass
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", "replace")
    # No BOM: heuristically detect UTF-16 by looking for the alternating
    # NUL-byte pattern typical of ASCII text stored as UTF-16 LE/BE.
    # LE stores ASCII as [char_byte, 0x00] -> zeros cluster at odd offsets.
    # BE stores ASCII as [0x00, char_byte] -> zeros cluster at even offsets.
    sample = raw[:2000]
    zeros_at_odd = sample[1::2].count(0); zeros_at_even = sample[0::2].count(0)
    if len(sample) >= 20 and max(zeros_at_odd, zeros_at_even) > len(sample) * 0.3:
        try: return raw.decode("utf-16-le" if zeros_at_odd > zeros_at_even else "utf-16-be")
        except Exception: pass
    try: return raw.decode("utf-8-sig")
    except Exception: return raw.decode("cp1252", "replace")

def parse_csv_bytes(raw):
    import csv, io
    text=decode_bytes(raw)
    try: d=csv.Sniffer().sniff(text[:8192],delimiters=",;\t")
    except: d=csv.excel
    return [dict(r) for r in csv.DictReader(io.StringIO(text),dialect=d)]

def parse_html(raw):
    from html.parser import HTMLParser
    class P(HTMLParser):
        def __init__(self): super().__init__(); self.tables=[]; self.t=None; self.r=None; self.c=None
        def handle_starttag(self,tag,attrs):
            if tag=="table": self.t=[]
            elif self.t is not None and tag=="tr": self.r=[]
            elif self.r is not None and tag in ("td","th"): self.c=[]
        def handle_data(self,d):
            if self.c is not None: self.c.append(d)
        def handle_endtag(self,tag):
            if tag in ("td","th") and self.r is not None and self.c is not None: self.r.append(" ".join("".join(self.c).split())); self.c=None
            elif tag=="tr" and self.t is not None and self.r is not None:
                if self.r:self.t.append(self.r)
                self.r=None
            elif tag=="table" and self.t is not None:
                if self.t:self.tables.append(self.t)
                self.t=None
    p=P(); p.feed(raw.decode("utf-8","replace")); out=[]
    for table in p.tables:
        if not table: continue
        hi=None
        for i,row in enumerate(table[:10]):
            j=" ".join(norm(x) for x in row)
            if any(x in j for x in ("time","profit","deal","positionid","ticket")): hi=i; break
        if hi is None: continue
        h=table[hi]
        for row in table[hi+1:]:
            if len(row)<2: continue
            row=row+[""]*max(0,len(h)-len(row)); out.append(dict(zip(h,row[:len(h)])))
    return out

def load_rows(path):
    raw=Path(path).read_bytes(); low=Path(path).name.lower()
    if low.endswith((".html",".htm")): return parse_html(raw)
    if low.endswith(".xml"):
        import xml.etree.ElementTree as ET
        root=ET.fromstring(raw); out=[]
        for e in root.iter():
            ch=list(e)
            if ch and all(len(list(x))==0 for x in ch): out.append({c.tag.split('}')[-1]:(c.text or '') for c in ch})
        return out
    if low.endswith(".zip"):
        import zipfile, io
        out=[]; z=zipfile.ZipFile(io.BytesIO(raw))
        for n in z.namelist():
            if n.lower().endswith((".csv",".html",".htm",".xml")):
                b=z.read(n)
                if n.lower().endswith((".html",".htm")): out.extend(parse_html(b))
                else: out.extend(parse_csv_bytes(b))
        return out
    # MT5 Strategy Tester CSV reports can contain several sections in one CSV,
    # and can be comma, semicolon, or tab delimited depending on locale/export
    # method — sniff the real delimiter instead of assuming a comma.
    import csv, io
    text=decode_bytes(raw)
    try: dialect=csv.Sniffer().sniff(text[:8192],delimiters=",;\t")
    except Exception: dialect=csv.excel
    matrix=list(csv.reader(io.StringIO(text),dialect=dialect))
    sections=[]
    for i,row in enumerate(matrix):
        h=[norm(x) for x in row]
        if "time" in h and "deal" in h and "profit" in h and ("direction" in h or "type" in h):
            headers=row
            for rr in matrix[i+1:]:
                if not any(str(x).strip() for x in rr):
                    break
                # stop when a new section starts rather than treating summary rows as deals
                if len(rr)>0 and norm(rr[0]) in {"summary","orders","deals","results","settings"}:
                    break
                rr=rr+[""]*max(0,len(headers)-len(rr))
                sections.append(dict(zip(headers,rr[:len(headers)])))
            if sections: break
    if sections: return sections
    return parse_csv_bytes(raw)

def parse_ea(source):
    low=source.lower(); lines=source.splitlines()
    indicators=[]
    for name,patterns in {
        "ATR":["iATR","atr("],"RSI":["iRSI","rsi("],"ADX":["iADX","adx("],"MACD":["iMACD","macd("],
        "EMA/SMA":["iMA","ema","sma"],"Bollinger":["iBands","bollinger"],"Stochastic":["iStochastic","stochastic"],
        "CCI":["iCCI","cci("],"Momentum":["iMomentum","momentum("],"Fractals":["iFractals","fractal"],
    }.items():
        if any(p.lower() in low for p in patterns): indicators.append(name)
    order_calls=sorted(set(re.findall(r"(?:trade\.)?(Buy|Sell|BuyLimit|SellLimit|BuyStop|SellStop)\s*\(",source,re.I)))
    exits=sorted(set(re.findall(r"(?:PositionClose|PositionClosePartial|OrderClose|CloseBy|Trailing|Break.?Even)",source,re.I)))
    inputs=[]
    for line in lines:
        if re.search(r"\binput\b",line,re.I):
            inputs.append(line.strip()[:240])
    functions=sorted(set(re.findall(r"\b(OnInit|OnTick|OnTimer|OnTrade|OnTradeTransaction|OnDeinit)\s*\(",source,re.I)))
    return {
        "line_count":len(lines), "event_handlers":functions, "indicators":indicators,
        "order_calls":order_calls, "exit_logic_tokens":exits, "input_declarations":inputs[:100],
        "has_risk_guard":any(x in low for x in ("maxdailyloss","maxtotalloss","riskguard","drawdown")),
        "logic_sections":extract_logic_sections(source),
        "parameters":extract_parameters(source),
    }

def extract_parameters(source):
    out={}
    for line in source.splitlines():
        m=re.search(r"\binput\s+([A-Za-z0-9_]+)\s+([A-Za-z0-9_]+)\s*=\s*([^;]+)",line)
        if m: out[m.group(2)]={"type":m.group(1),"default":m.group(3).strip()}
    return out

def extract_logic_sections(source):
    low=source.lower(); out=[]
    patterns=[
        ("entry_buy",r"(?:trade\.)?buy\s*\("),("entry_sell",r"(?:trade\.)?sell\s*\("),
        ("stop_loss",r"\b(?:sl|stoploss|stop_loss)\b"),("take_profit",r"\b(?:tp|takeprofit|take_profit)\b"),
        ("trailing_stop",r"trailing"),("break_even",r"break.?even"),
        ("risk_management",r"riskpercent|maxdailyloss|maxtotalloss|riskguard"),
        ("breakout",r"breakout"),("support_resistance",r"support|resistance"),
    ]
    for name,p in patterns:
        matches=list(re.finditer(p,low,re.I))
        if matches: out.append({"section":name,"occurrences":len(matches),"line_examples":[source[:m.start()].count("\n")+1 for m in matches[:10]]})
    return out

def build_trades(rows):
    trades=[]
    # 1) Complete trade rows, when a backtest export already provides entry/exit fields.
    for i,r in enumerate(rows,1):
        keys={norm(k) for k in r.keys()}
        has_entry_time=any(k in keys for k in ("opentime","entrytime"))
        has_exit_time=any(k in keys for k in ("closetime","exittime"))
        if not (has_entry_time and has_exit_time):
            continue
        ot=parse_dt(pick(r,"Open Time","Entry Time","EntryTime","OpenTime")); ct=parse_dt(pick(r,"Close Time","Exit Time","ExitTime","CloseTime"))
        p=num(pick(r,"Profit","Net Profit","P&L","PnL"))
        if ot and ct and p is not None:
            trades.append({"trade_id":str(pick(r,"Position ID","PositionID","Deal","Ticket","Order") or i),"entry_time":ot,"exit_time":ct,"entry_price":num(pick(r,"Entry Price","Open Price","EntryPrice","OpenPrice")),"exit_price":num(pick(r,"Exit Price","Close Price","ExitPrice","ClosePrice")),"profit":p,"side":str(pick(r,"Type","Direction","Side","Order Type") or "").lower(),"comment":str(pick(r,"Comment","Comments") or ""),"raw":{str(k):str(v) for k,v in r.items() if v not in (None,"")}})
    if trades:return sorted(trades,key=lambda x:x["entry_time"])

    # 2) MT5 Deals table: pair chronological IN with the next OUT.
    # This matches the common MT5 tester deal export used by this project, where
    # Position ID is absent but Direction explicitly says in/out.
    ordered=[]
    for i,r in enumerate(rows,1):
        t=parse_dt(pick(r,"Time","Date","Timestamp"))
        if not t: continue
        direction=str(pick(r,"Direction","Entry","Deal Entry","Entry Type") or "").lower()
        typ=str(pick(r,"Type","Side","Order Type") or "").lower()
        if direction not in ("in","out","entry","exit","open","close"):
            continue
        ordered.append((t,r,direction,typ))
    ordered.sort(key=lambda x:x[0])
    pending=None
    for idx,(t,r,direction,typ) in enumerate(ordered):
        if direction in ("in","entry","open"):
            pending=(t,r,typ)
            continue
        if direction in ("out","exit","close") and pending is not None:
            et,er,side=pending
            p=num(pick(r,"Profit","Net Profit","P&L","PnL"))
            if p is None: p=0.0
            trades.append({
                "trade_id":str(pick(er,"Order","Deal","Ticket","Position ID","PositionID") or pick(r,"Order","Deal","Ticket","Position ID","PositionID") or len(trades)+1),
                "entry_time":et,"exit_time":t,
                "entry_price":num(pick(er,"Price","Entry Price","Open Price")),
                "exit_price":num(pick(r,"Price","Exit Price","Close Price")),
                "profit":p,"side":side,"comment":str(pick(er,"Comment","Comments") or pick(r,"Comment","Comments") or ""),
                "raw":{str(k):str(v) for k,v in er.items() if v not in (None,"")}
            })
            pending=None
    return sorted(trades,key=lambda x:x["entry_time"])

def binom_ci(w,n):
    if not n:return [None,None]
    p=w/n; z=1.96; den=1+z*z/n; cen=(p+z*z/(2*n))/den; half=z*math.sqrt((p*(1-p)/n)+(z*z/(4*n*n)))/den
    return [round(max(0,cen-half),4),round(min(1,cen+half),4)]

def stats(items):
    n=len(items); wins=sum(x["profit"]>0 for x in items); losses=sum(x["profit"]<0 for x in items); net=sum(x["profit"] for x in items)
    gross_win=sum(x["profit"] for x in items if x["profit"]>0); gross_loss=-sum(x["profit"] for x in items if x["profit"]<0)
    hold=[(x["exit_time"]-x["entry_time"]).total_seconds()/60 for x in items]
    return {
        "n":n,"wins":wins,"losses":losses,"flats":n-wins-losses,
        "win_rate":round(wins/n,4) if n else None,"win_rate_95ci":binom_ci(wins,n),
        "net_profit":round(net,8),
        "net_profit_pct_initial_capital":round((net/INITIAL_CAPITAL)*100,6) if INITIAL_CAPITAL else None,
        "avg_profit":round(net/n,8) if n else None,
        "avg_profit_pct_initial_capital":round((net/n/INITIAL_CAPITAL)*100,6) if n and INITIAL_CAPITAL else None,
        "median_profit":round(statistics.median([x["profit"] for x in items]),8) if items else None,
        "profit_factor":round(gross_win/gross_loss,4) if gross_loss else None,
        "avg_hold_minutes":round(statistics.mean(hold),2) if hold else None,
        "median_hold_minutes":round(statistics.median(hold),2) if hold else None
    }

def equity_drawdown(items):
    balance=INITIAL_CAPITAL; peak=balance; max_dd=0.0
    for t in sorted(items,key=lambda x:x["entry_time"]):
        balance += t["profit"]
        peak=max(peak,balance)
        max_dd=max(max_dd,peak-balance)
    return {
        "max_drawdown_absolute":round(max_dd,8),
        "max_drawdown_pct_initial_capital":round((max_dd/INITIAL_CAPITAL)*100,6) if INITIAL_CAPITAL else None
    }

def sequence_stats(trades):
    ordered=sorted(trades,key=lambda x:x["entry_time"]); runs=[]; cur=None; length=0
    transitions=Counter(); prev=None
    for t in ordered:
        outcome="W" if t["profit"]>0 else "L" if t["profit"]<0 else "F"
        if prev: transitions[prev+outcome]+=1
        if outcome==cur:length+=1
        else:
            if cur:runs.append((cur,length))
            cur=outcome; length=1
        prev=outcome
    if cur:runs.append((cur,length))
    return {"max_win_streak":max([n for c,n in runs if c=="W"],default=0),"max_loss_streak":max([n for c,n in runs if c=="L"],default=0),"transitions":dict(transitions)}

def code_behavior_alignment(ea,trades):
    # Only claims structural alignment when the backtest exposes the same fields.
    comments=[t["comment"] for t in trades if t.get("comment")]
    side_counts=Counter("BUY" if "buy" in t["side"] else "SELL" if "sell" in t["side"] else "OTHER" for t in trades)
    return {
        "ea_declares_buy_sell": {"BUY": "BUY" in ea["order_calls"], "SELL":"SELL" in ea["order_calls"]},
        "realized_trade_sides":dict(side_counts),
        "comment_evidence_available":bool(comments),
        "distinct_trade_comments":sorted(set(comments))[:50],
        "alignment_note":"This layer reports observable structural alignment only. It does not pretend to know which internal boolean condition triggered each trade unless the backtest exposes that evidence."
    }

def analyze(ea_source, trades):
    ea=parse_ea(ea_source); findings=[]; insuff=[]; hypotheses=[]
    def hypothesis(topic, statement, evidence, missing, alternatives=None):
        hypotheses.append({
            "status":"HYPOTHESIS",
            "topic":topic,
            "statement":statement,
            "evidence":evidence,
            "missing_evidence":missing,
            "alternative_explanations":alternatives or [],
            "confidence":"LOW_TO_MODERATE",
            "note":"Inference from supplied EA/backtest only; not a validated causal claim."
        })
    overall=stats(trades)
    overall.update(equity_drawdown(trades))
    # Directional comparison
    groups=defaultdict(list)
    for t in trades:
        if "buy" in t["side"]: groups["BUY"].append(t)
        elif "sell" in t["side"]: groups["SELL"].append(t)
    if all(len(groups[k])>=MIN_GROUP_N for k in ("BUY","SELL")):
        a,b=stats(groups["BUY"]),stats(groups["SELL"])
        a.update(equity_drawdown(groups["BUY"])); b.update(equity_drawdown(groups["SELL"]))
        findings.append({"status":"OBSERVED_PATTERN","question":"Does realized performance differ by trade direction?","evidence":{"BUY":a,"SELL":b},"interpretation":"Observed association in supplied backtest; no market-cause claim."})
    else: insuff.append({"topic":"BUY_vs_SELL","reason":"Each comparison group needs at least the minimum sample.","groups":{k:len(v) for k,v in groups.items()}})
    if groups.get("BUY") and groups.get("SELL") and (len(groups["BUY"]) < MIN_GROUP_N or len(groups["SELL"]) < MIN_GROUP_N):
        hypothesis("BUY_vs_SELL", "The observed directional difference may be real, but the current sample is too thin to treat it as stable.", {"BUY_n":len(groups["BUY"]),"SELL_n":len(groups["SELL"])}, ["larger repeated samples"], ["sample imbalance","time-period concentration"])
    elif groups.get("BUY") and groups.get("SELL"):
        aa,bb=stats(groups["BUY"]),stats(groups["SELL"])
        if aa["win_rate"] is not None and bb["win_rate"] is not None and abs(aa["win_rate"]-bb["win_rate"]) >= 0.10:
            hypothesis("directional_sensitivity", "The EA may have directional sensitivity because realized BUY and SELL outcomes differ materially in this backtest.", {"BUY":aa,"SELL":bb}, ["trade-level entry-condition values"], ["period mix","different trade counts","execution effects"])
    # Monthly stability / change
    months=defaultdict(list)
    for t in trades: months[t["entry_time"].strftime("%Y-%m")].append(t)
    month_stats={m:stats(v) for m,v in sorted(months.items()) if len(v)>=MIN_GROUP_N}
    if len(month_stats)>=2:
        best=max(month_stats.items(),key=lambda kv:kv[1]["win_rate"]); worst=min(month_stats.items(),key=lambda kv:kv[1]["win_rate"])
        findings.append({"status":"OBSERVED_PATTERN","question":"Did realized EA behavior change across backtest periods?","evidence":{"monthly":month_stats,"highest_win_rate_period":best[0],"lowest_win_rate_period":worst[0]},"interpretation":"Temporal difference is observed; it does not prove a market-regime cause."})
    else: insuff.append({"topic":"time_stability","reason":"Not enough sampled periods with minimum trade count."})
    if len(month_stats) >= 2:
        vals=list(month_stats.items())
        first=vals[0][1]; last=vals[-1][1]
        if first.get("win_rate") is not None and last.get("win_rate") is not None and abs(first["win_rate"]-last["win_rate"]) >= 0.10:
            hypothesis("temporal_change", "The strategy may be sensitive to changing conditions because realized performance differs across backtest periods; the supplied data cannot identify the external market cause.", {"first_period":vals[0][0],"first":first,"last_period":vals[-1][0],"last":last}, ["market-price context","regime labels from external OHLC/tick data"], ["random variation","trade mix","parameter or execution effects"])
    # Hour/day patterns: genuinely beyond standard MT5 summary, but only if enough data.
    for label,keyfn in [("entry_hour",lambda t:t["entry_time"].hour),("entry_weekday",lambda t:t["entry_time"].weekday())]:
        g=defaultdict(list)
        for t in trades:g[keyfn(t)].append(t)
        valid={str(k):stats(v) for k,v in g.items() if len(v)>=MIN_GROUP_N}
        if len(valid)>=2:
            hi=max(valid.items(),key=lambda kv:kv[1]["win_rate"]); lo=min(valid.items(),key=lambda kv:kv[1]["win_rate"])
            findings.append({"status":"OBSERVED_PATTERN","question":f"Is realized performance uneven across {label}?","evidence":{"groups":valid,"highest":hi,"lowest":lo},"interpretation":"Association in trade timing; not a claim that clock/day causes performance."})
    # Sequence and outcome transition behavior.
    seq=sequence_stats(trades)
    if seq["max_loss_streak"]>=4 or seq["max_win_streak"]>=4:
        findings.append({"status":"OBSERVED_PATTERN","question":"Does the EA show notable outcome clustering in sequence?","evidence":seq,"interpretation":"Sequence structure is observable in the backtest and is not itself evidence of market causation."})
    # Holding time vs outcome.
    by_out=defaultdict(list)
    for t in trades: by_out["WIN" if t["profit"]>0 else "LOSS" if t["profit"]<0 else "FLAT"].append(t)
    hold_compare={k:stats(v)["avg_hold_minutes"] for k,v in by_out.items() if v}
    if len(by_out.get("WIN",[]))>=MIN_GROUP_N and len(by_out.get("LOSS",[]))>=MIN_GROUP_N:
        win_stats=stats(by_out["WIN"]); loss_stats=stats(by_out["LOSS"])
        # This comparison is outcome-conditioned by construction. Do not expose
        # win_rate=1/0 as if it were an independent performance statistic.
        findings.append({"status":"OBSERVED_PATTERN","question":"Do winning and losing trades differ in holding duration?","evidence":{
            "winning_trades":{"n":win_stats["n"],"avg_hold_minutes":win_stats["avg_hold_minutes"],"median_hold_minutes":win_stats["median_hold_minutes"]},
            "losing_trades":{"n":loss_stats["n"],"avg_hold_minutes":loss_stats["avg_hold_minutes"],"median_hold_minutes":loss_stats["median_hold_minutes"]},
            "difference_avg_hold_minutes":round(win_stats["avg_hold_minutes"]-loss_stats["avg_hold_minutes"],2)
        },"interpretation":"Outcome-conditioned descriptive comparison; it does not show that holding longer causes a winning trade."})
    # Profit concentration / tail behavior.
    profits=sorted([t["profit"] for t in trades],reverse=True)
    if profits:
        top=max(1,math.ceil(len(profits)*.10)); top_share=sum(profits[:top])/sum(p for p in profits if p>0) if sum(p for p in profits if p>0)>0 else None
        findings.append({"status":"OBSERVED_PATTERN","question":"How concentrated is realized gross profit among the top winning trades?","evidence":{"top_10_percent_win_profit_share":round(top_share,4) if top_share is not None else None,"winning_trade_count":sum(p>0 for p in profits)},"interpretation":"A robustness diagnostic: a small number of trades may account for a large share of gross winning profit."})
    # Deliberate inference: if EA declares a mechanism but the backtest does not expose its per-trade state,
    # produce a hypothesis rather than fabricating a direct relationship.
    if ea.get("indicators") and not any(t.get("raw",{}).get("RSI") or t.get("raw",{}).get("ATR") or t.get("raw",{}).get("ADX") for t in trades):
        hypothesis("EA_internal_conditions", "Some EA indicator/filter logic may explain realized differences, but the supplied backtest does not expose per-trade indicator state, so the relationship remains a hypothesis.", {"ea_indicators":ea.get("indicators"),"trade_count":len(trades)}, ["per-trade indicator/filter values","which internal branch triggered each trade"], ["trade timing","direction","exit behavior"])
    if trades and ea.get("exit_logic_tokens"):
        hypothesis("exit_behavior", "Observed holding-time and profit differences may partly reflect the EA's exit logic because exit mechanisms are present in the source code.", {"ea_exit_logic_tokens":ea.get("exit_logic_tokens"),"trade_count":len(trades)}, ["per-trade exit reason / exact branch"], ["entry quality","trade duration","execution conditions"])

    return {
        "scope":["EA .mq5","Backtest"],
        "account_context":{"initial_capital":INITIAL_CAPITAL,"currency":"USD","context_label":"research account configuration"},
        "overall":overall,
        "findings":findings,
        "hypotheses":hypotheses,
        "insufficient_evidence":insuff,
        "sequence":seq,
        "direction_stats":{k:stats(v) for k,v in groups.items()},
        "time_stats":month_stats,
        "ea_analysis":ea,
        "ea_backtest_alignment":code_behavior_alignment(ea,trades),
        "evidence_policy":{"min_group_n":MIN_GROUP_N,"causality":"not claimed","lookahead":"No external market data is introduced; analysis is limited to fields actually present in EA/backtest.","capital_normalization":"Dollar results are additionally normalized to the configured initial capital; this does not imply future return."}
    }

def make_experiment(db,eid,symbol,timeframe,ea_name):
    e=db.query(Experiment).filter(Experiment.id==eid).first()
    if e:return e
    h=Hypothesis(id=str(uuid4()),title=f"EA + Backtest research: {ea_name}",statement="Study realized EA behavior and its relationship to the strategy logic exposed in the supplied .mq5 and backtest data.",assumptions="Only supplied EA and backtest fields are evidence. No OHLC/tick/market data is assumed.",status="active")
    db.add(h);db.flush()
    e=Experiment(id=eid,hypothesis_id=h.id,symbol=symbol or "UNKNOWN",timeframe=timeframe or "UNKNOWN",experiment_type="ea_backtest_research",specification="EA .mq5 + MT5 backtest only. No bars, ticks, or external market data.",baseline="",status="uploaded")
    db.add(e);db.commit();return e

async def save_upload(upload,dest):
    total=0; dest.parent.mkdir(parents=True,exist_ok=True)
    with dest.open("wb") as f:
        while True:
            chunk=await upload.read(8*1024*1024)
            if not chunk:break
            total+=len(chunk)
            if total>MAX_BYTES:
                dest.unlink(missing_ok=True);raise HTTPException(413,f"{upload.filename} exceeds {MAX_MB} MB")
            f.write(chunk)
    return upload.filename,total

def _delete_unused(folder):
    # Important: prevent stale bars/ticks from older deployments from silently entering research.
    for name in ("bars","ticks","bars.csv","ticks.csv"):
        p=folder/name
        if p.exists() and p.is_file(): p.unlink()

def _run_job(eid, allow_existing=False):
    folder=ROOT/eid; db=SessionLocal()
    try:
        e=db.query(Experiment).filter(Experiment.id==eid).first()
        if not e:return
        # A result already on disk/DB means this job has already completed.
        existing=db.query(ExperimentResult).filter(ExperimentResult.experiment_id==eid).first()
        if existing and not allow_existing:
            e.status="completed"; e.completed_at=e.completed_at or now(); db.commit(); return
        e.status="running"; db.commit()

        # Validate inputs before doing any heavy work. This makes a failed job
        # explicit instead of silently leaving the experiment at "uploaded".
        ea_path=folder/"ea.mq5"; bt_path=folder/"backtest"
        if not ea_path.is_file() or not bt_path.is_file():
            raise FileNotFoundError("Research inputs are missing: ea.mq5 and/or backtest")

        ea=ea_path.read_text("utf-8",errors="replace")
        rows=load_rows(bt_path)
        trades=build_trades(rows)
        analysis=analyze(ea,trades)
        limitations=[]
        if not rows: limitations.append("The supplied backtest file produced zero parsed rows.")
        if not trades: limitations.append("No completed trades could be reconstructed from the supplied backtest.")
        if analysis["findings"]==[]: limitations.append("No evidence-backed pattern was established; absence of a finding is not evidence that no relationship exists.")
        research_run_id=f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
        result={"engine":"ea_backtest_research_v3","status":"completed","research_run_id":research_run_id,"experiment_id":eid,"input_lineage":{"sources":["ea.mq5","backtest"],"explicitly_excluded":["OHLC bars","all ticks","external market data"]},"data_quality":{"raw_backtest_rows":len(rows),"reconstructed_trades":len(trades),"initial_capital":INITIAL_CAPITAL},"analysis":analysis,"limitations":limitations,"generated_at":now().isoformat()}

        # Write the durable artifact first. If the DB transaction fails, startup
        # recovery can rebuild ExperimentResult from this file.
        artifact={"summary":"EA + Backtest evidence research","metrics":result,"evidence":{"finding_count":len(analysis["findings"])},"limitations":limitations,"conclusion":"Evidence-backed observations from supplied EA/backtest only; no market-causal claim or trading recommendation."}
        tmp=folder/"result.json.tmp"
        tmp.write_text(json.dumps(artifact,ensure_ascii=False,default=str),encoding="utf-8")
        tmp.replace(folder/"result.json")

        r=ExperimentResult(id=str(uuid4()),experiment_id=e.id,summary=artifact["summary"],metrics=json.dumps(result,ensure_ascii=False,default=str),evidence=json.dumps(artifact["evidence"],ensure_ascii=False),limitations=json.dumps(limitations,ensure_ascii=False),conclusion=artifact["conclusion"])
        db.add(r);e.status="completed";e.completed_at=now();db.commit()
    except Exception as ex:
        # Persist the real traceback and make the failure visible through GET /research.
        try:
            folder.mkdir(parents=True,exist_ok=True)
            (folder/"error.txt").write_text(f"{ex}\n\n{traceback.format_exc()}",encoding="utf-8")
        except Exception: pass
        try:
            e=db.query(Experiment).filter(Experiment.id==eid).first()
            if e:
                e.status="failed"; e.completed_at=now(); db.commit()
        except Exception: pass
        raise
    finally:
        db.close()

def _submit_research_job(eid, allow_existing=False):
    """Submit a research job outside the request lifecycle.

    FastAPI BackgroundTasks is excellent for short post-response work, but EA/CSV
    analysis can be CPU/file intensive. A dedicated executor prevents the job from
    being coupled to the response task and gives us a Future we can observe/log.
    """
    future=RESEARCH_EXECUTOR.submit(_run_job,eid,allow_existing)
    def _done(f):
        try: f.result()
        except Exception: traceback.print_exc()
    future.add_done_callback(_done)
    return future

def recover_research_state():
    """Rebuild Experiment/ExperimentResult rows from the on-disk research_inputs
    folder for any experiment_id that has files on disk but no row in the database.

    This covers the common failure mode on platforms like Render: the SQLite file
    (or even a fresh Postgres schema) can be reset by a redeploy or restart while
    RESEARCH_INPUT_ROOT, if it lives on an attached persistent disk, survives.
    Without this, GET/preview/publish return 404 "Experiment not found" for
    experiments that a user believes already completed.

    This is NOT a substitute for attaching a persistent disk — if ROOT itself is
    wiped (no disk attached), there is nothing here to recover from and the
    EA + backtest must be re-uploaded.
    """
    if not ROOT.exists(): return
    db=SessionLocal()
    try:
        for folder in sorted(p for p in ROOT.iterdir() if p.is_dir()):
            eid=folder.name
            manifest_path=folder/"manifest.json"
            if not manifest_path.is_file(): continue
            try: manifest=json.loads(manifest_path.read_text("utf-8"))
            except Exception: continue
            e=db.query(Experiment).filter(Experiment.id==eid).first()
            if not e:
                e=make_experiment(db,eid,manifest.get("symbol"),manifest.get("timeframe"),manifest.get("ea_filename") or "recovered")
            result_path=folder/"result.json"
            if result_path.is_file():
                has_result=db.query(ExperimentResult).filter(ExperimentResult.experiment_id==eid).first()
                if not has_result:
                    try: saved=json.loads(result_path.read_text("utf-8"))
                    except Exception: saved=None
                    if saved:
                        r=ExperimentResult(id=str(uuid4()),experiment_id=eid,summary=saved.get("summary","EA + Backtest evidence research"),metrics=json.dumps(saved.get("metrics",{}),ensure_ascii=False,default=str),evidence=json.dumps(saved.get("evidence",{}),ensure_ascii=False),limitations=json.dumps(saved.get("limitations",[]),ensure_ascii=False),conclusion=saved.get("conclusion",""))
                        db.add(r)
                if e.status!="completed":
                    e.status="completed"; e.completed_at=e.completed_at or now()
                db.commit()
    finally: db.close()

@router.post("/data/upload",summary="Upload EA .mq5 + MT5 Backtest only")
async def upload_data(experiment_id:str=Form("random"),symbol:str=Form(""),timeframe:str=Form(""),ea_file:UploadFile=File(...),backtest_file:UploadFile=File(...)):
    if not ea_file.filename or not ea_file.filename.lower().endswith(".mq5"):raise HTTPException(400,"ea_file must be .mq5")
    allowed=(".csv",".html",".htm",".xml",".zip")
    if not backtest_file.filename or not backtest_file.filename.lower().endswith(allowed):raise HTTPException(400,"backtest_file must be CSV/HTML/XML/ZIP")
    # Experiment IDs can now be generated automatically. In Swagger, leave the
    # field as the default `random`, or enter `random`/`auto`/`new` explicitly.
    # UUID4 is used because the DB schema stores experiment IDs as String(36).
    raw_id=str(experiment_id or "").strip()
    if raw_id.lower() in {"", "random", "auto", "new", "uuid", "uuid4"}:
        eid=str(uuid4())
    else:
        eid=safe_id(raw_id)
    folder=ROOT/eid;folder.mkdir(parents=True,exist_ok=True);_delete_unused(folder)
    ea=await save_upload(ea_file,folder/"ea.mq5");bt=await save_upload(backtest_file,folder/"backtest")
    # symbol/timeframe/ea filename are persisted here too (not just in the DB row) so that
    # recover_research_state() can rebuild the Experiment row if the database is ever reset
    # but this folder survives (i.e. it lives on a persistent disk).
    manifest={"experiment_id":eid,"symbol":symbol or "UNKNOWN","timeframe":timeframe or "UNKNOWN","ea_filename":ea[0],"backtest_filename":bt[0],"sizes_bytes":{"ea":ea[1],"backtest":bt[1]},"excluded_inputs":["bars","ticks","external_market_data"]}
    (folder/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False),encoding="utf-8")
    db=SessionLocal()
    try:
        make_experiment(db,eid,symbol,timeframe,ea_file.filename)
        return {"status":"uploaded","experiment_id":eid,"files":{"ea":ea_file.filename,"backtest":backtest_file.filename},"research_scope":["EA .mq5","Backtest"],"excluded":["M30 Bars","All Tick","external market data"],"next":"POST /api/research/{experiment_id}/run"}
    finally:db.close()

@router.post("/{experiment_id}/run-again")
def run_research_again(experiment_id:str):
    """Create a fresh research result from the same persisted EA/backtest inputs.

    This does not overwrite prior results; the new result receives its own
    research_run_id. Use a new experiment_id when the public post itself must
    represent a materially different experiment.
    """
    eid=safe_id(experiment_id); folder=ROOT/eid
    if not (folder/"ea.mq5").is_file() or not (folder/"backtest").is_file():
        raise HTTPException(409,"Upload EA and backtest first")
    db=SessionLocal()
    try:
        e=db.query(Experiment).filter(Experiment.id==eid).first()
        if not e: raise HTTPException(404,"Experiment not found")
        if e.status in {"queued","running"}:
            raise HTTPException(409,{"message":"Research is already running","experiment_id":eid})
        e.status="queued"; e.completed_at=None; db.commit()
    finally: db.close()
    # The worker's existing-result guard must be bypassed for this explicit endpoint.
    _submit_research_job(eid, allow_existing=True)
    return {"experiment_id":eid,"status":"queued","message":"A fresh research run is being generated. Poll GET /api/research/{experiment_id}.","note":"Previous result records are retained."}

@router.get("/{experiment_id}")
def get_research(experiment_id:str):
    eid=safe_id(experiment_id);db=SessionLocal()
    try:
        e=db.query(Experiment).filter(Experiment.id==eid).first()
        if not e:raise HTTPException(404,"Experiment not found")
        r=db.query(ExperimentResult).filter(ExperimentResult.experiment_id==eid).order_by(ExperimentResult.created_at.desc()).first()
        error=None
        if e.status=="failed":
            error_path=ROOT/eid/"error.txt"
            if error_path.is_file(): error=error_path.read_text("utf-8",errors="replace")
        return {"experiment_id":eid,"status":e.status,"symbol":e.symbol,"timeframe":e.timeframe,"result":json.loads(r.metrics) if r else None,"result_id":r.id if r else None,"error":error}
    finally:db.close()

@router.post("/{experiment_id}/run")
def run_research(experiment_id:str):
    eid=safe_id(experiment_id);folder=ROOT/eid
    if not (folder/"ea.mq5").is_file() or not (folder/"backtest").is_file():raise HTTPException(409,"Upload EA and backtest first")
    db=SessionLocal()
    try:
        e=db.query(Experiment).filter(Experiment.id==eid).first()
        if not e:raise HTTPException(404,"Experiment not found")
        existing=db.query(ExperimentResult).filter(ExperimentResult.experiment_id==eid).order_by(ExperimentResult.created_at.desc()).first()
        if existing:
            if e.status!="completed": e.status="completed"; e.completed_at=e.completed_at or now(); db.commit()
            return {"experiment_id":eid,"status":"completed","message":"Research result already exists. Preview is ready.","result_id":existing.id,"scope":["EA .mq5","Backtest"]}
        if e.status in {"queued","running"}:
            return {"experiment_id":eid,"status":e.status,"message":"Research is already running. Poll GET /api/research/{experiment_id}.","scope":["EA .mq5","Backtest"]}
        e.status="queued"; e.completed_at=None; db.commit()
    finally:db.close()

    try:
        _submit_research_job(eid)
    except Exception as ex:
        db=SessionLocal()
        try:
            e=db.query(Experiment).filter(Experiment.id==eid).first()
            if e:e.status="failed";db.commit()
        finally:db.close()
        raise HTTPException(500,f"Could not queue research job: {ex}")

    return {"experiment_id":eid,"status":"queued","message":"Research is running in background. Poll GET /api/research/{experiment_id}.","scope":["EA .mq5","Backtest"]}
