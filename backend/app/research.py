import numpy as np
import pandas as pd
from .data import load_deals, load_bars, build_trades, attach_context
from .ea_analysis import analyze_ea

def _safe(x):
    if pd.isna(x): return None
    if isinstance(x,(np.integer,np.floating)): return float(x)
    return x

def group_stats(df, key):
    rows=[]
    for k,g in df.groupby(key,dropna=False):
        n=len(g); wins=int((g.profit>0).sum()); losses=int((g.profit<0).sum())
        total=float(g.profit.sum())
        avg=float(g.profit.mean()) if n else 0
        wr=wins/n if n else 0
        rows.append({
            key:str(k),"n":n,"wins":wins,"losses":losses,
            "win_rate":round(wr,4),"net_profit":round(total,4),
            "avg_trade":round(avg,4)
        })
    return rows

def significance_note(a,b):
    # Practical evidence gate, deliberately conservative.
    if a["n"] < 10 or b["n"] < 10:
        return "INSUFFICIENT_EVIDENCE"
    diff=abs(a["win_rate"]-b["win_rate"])
    pooled=(a["wins"]+b["wins"])/(a["n"]+b["n"])
    se=(pooled*(1-pooled)*(1/a["n"]+1/b["n"]))**0.5 if 0<pooled<1 else 0
    z=diff/se if se else 0
    if diff>=0.15 and z>=2:
        return "VALIDATED_PATTERN"
    if diff>=0.10:
        return "OBSERVED_PATTERN"
    return "INSUFFICIENT_EVIDENCE"

def run_research(ea_path, backtest_path, bars_path):
    ea=analyze_ea(ea_path)
    deals=load_deals(backtest_path)
    trades=build_trades(deals)
    if trades.empty:
        return {"status":"INSUFFICIENT_EVIDENCE","reason":"No completed entry/exit trades could be reconstructed from Backtest.","ea":ea}

    bars=load_bars(bars_path)
    t=attach_context(trades,bars)
    t["result"]=np.where(t.profit>0,"WIN",np.where(t.profit<0,"LOSS","FLAT"))
    t["duration_minutes"]=(t.exit_time-t.entry_time).dt.total_seconds()/60
    t["period"]=t.entry_time.dt.to_period("M").astype(str)

    # Overall
    overall={
        "trades":len(t),
        "wins":int((t.profit>0).sum()),
        "losses":int((t.profit<0).sum()),
        "win_rate":round(float((t.profit>0).mean()),4),
        "net_profit":round(float(t.profit.sum()),4),
        "avg_trade":round(float(t.profit.mean()),4)
    }

    regime_stats=group_stats(t,"regime")
    trend_stats=group_stats(t,"trend")
    volatility_stats=group_stats(t,"volatility")
    side_stats=group_stats(t,"side")
    period_stats=group_stats(t,"period")

    findings=[]

    # Compare best/worst sufficiently sampled regimes.
    sampled=[r for r in regime_stats if r["n"]>=10]
    if len(sampled)>=2:
        hi=max(sampled,key=lambda r:r["win_rate"])
        lo=min(sampled,key=lambda r:r["win_rate"])
        status=significance_note(hi,lo)
        findings.append({
            "status":status,
            "finding":"EA outcomes differ across observed market regimes.",
            "evidence":{"higher_win_rate_regime":hi,"lower_win_rate_regime":lo},
            "interpretation":"This is an association between entry-time regime and realized trade outcomes; it does not prove causation."
        })
    else:
        findings.append({"status":"INSUFFICIENT_EVIDENCE","finding":"Not enough trades per market regime to validate a regime-performance difference.","evidence":{"minimum_trades_per_regime":10}})

    # Trend vs non-trend.
    trend_groups={r["trend"]:r for r in trend_stats}
    if "UPTREND" in trend_groups and "DOWNTREND" in trend_groups:
        findings.append({
            "status":significance_note(trend_groups["UPTREND"],trend_groups["DOWNTREND"]),
            "finding":"BUY/SELL strategy outcomes can be compared across directional trend states.",
            "evidence":{"uptrend":trend_groups["UPTREND"],"downtrend":trend_groups["DOWNTREND"]},
            "interpretation":"Direction is an entry-time M30 condition, not a claim about why the trade won or lost."
        })

    # Time change: compare first half and second half of observations.
    if len(t)>=40:
        t2=t.sort_values("entry_time").reset_index(drop=True)
        mid=len(t2)//2
        first=t2.iloc[:mid]; second=t2.iloc[mid:]
        a={"n":len(first),"wins":int((first.profit>0).sum()),"win_rate":float((first.profit>0).mean())}
        b={"n":len(second),"wins":int((second.profit>0).sum()),"win_rate":float((second.profit>0).mean())}
        findings.append({
            "status":significance_note(a,b),
            "finding":"EA performance was compared between the earlier and later halves of the backtest to detect regime sensitivity over time.",
            "evidence":{"earlier":a,"later":b},
            "interpretation":"A difference indicates temporal performance change; it does not by itself establish that market regime caused the change."
        })

    # Loss clustering.
    t["loss"]=t.profit<0
    runs=[]; cur=0
    for v in t.sort_values("entry_time").loss:
        cur=cur+1 if v else 0
        if cur: runs.append(cur)
    max_loss_run=max(runs) if runs else 0
    findings.append({
        "status":"OBSERVED_PATTERN" if max_loss_run>=4 else "INSUFFICIENT_EVIDENCE",
        "finding":"Consecutive loss clustering was checked.",
        "evidence":{"maximum_consecutive_losses":max_loss_run},
        "interpretation":"Clustering alone does not identify a market cause."
    })

    # Feature summary by result, only descriptive.
    feature_means={}
    for c in ["adx14","atr_pct","body_ratio10","body_ratio","bull_ratio10","bear_ratio10","duration_minutes"]:
        if c in t:
            feature_means[c]={
                "WIN":_safe(t.loc[t.result=="WIN",c].mean()),
                "LOSS":_safe(t.loc[t.result=="LOSS",c].mean())
            }

    return {
        "status":"completed",
        "research_scope":["EA","Backtest","M30"],
        "overall":overall,
        "ea_analysis":ea,
        "market_regime_definition":{
            "trend":"EMA20 vs EMA50 and 5-bar EMA slope",
            "trend_strength":"ADX14 bands",
            "volatility":"rolling ATR percentile",
            "entry_context":"last completed M30 candle at or before entry"
        },
        "regime_stats":regime_stats,
        "trend_stats":trend_stats,
        "volatility_stats":volatility_stats,
        "side_stats":side_stats,
        "period_stats":period_stats,
        "findings":findings,
        "win_loss_context_means":feature_means,
        "evidence_rules":{
            "no_lookahead":True,
            "mfe_mae_as_preentry_evidence":False,
            "causation_claims":False,
            "minimum_regime_trades":10,
            "lineage":"Every finding is tied to reconstructed trade timestamps and entry-time M30 context."
        }
    }
