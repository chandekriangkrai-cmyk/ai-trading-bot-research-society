import csv, io, re
from pathlib import Path
import pandas as pd
import numpy as np

def _read_csv(path, nrows=None):
    # Try normal CSV first, then common MT5 separators/encodings.
    for sep in [",",";","\t"]:
        try:
            df = pd.read_csv(path, sep=sep, nrows=nrows, encoding="utf-8-sig", low_memory=False)
            if df.shape[1] >= 3:
                return df
        except Exception:
            pass
    return pd.read_csv(path, nrows=nrows, encoding="latin1", low_memory=False)

def _norm(s):
    return re.sub(r"[^a-z0-9]","",str(s).lower())

def pick(df, names):
    norm={_norm(c):c for c in df.columns}
    for n in names:
        if _norm(n) in norm: return norm[_norm(n)]
    for c in df.columns:
        nc=_norm(c)
        for n in names:
            if _norm(n) in nc or nc in _norm(n):
                return c
    return None

def load_bars(path):
    df=_read_csv(path)
    t=pick(df,["time","datetime","date"])
    o=pick(df,["open"])
    h=pick(df,["high"])
    l=pick(df,["low"])
    c=pick(df,["close"])
    if not all([t,o,h,l,c]):
        raise ValueError(f"Bars missing required columns. Found: {list(df.columns)}")
    out=pd.DataFrame({
        "time":pd.to_datetime(df[t], errors="coerce"),
        "open":pd.to_numeric(df[o],errors="coerce"),
        "high":pd.to_numeric(df[h],errors="coerce"),
        "low":pd.to_numeric(df[l],errors="coerce"),
        "close":pd.to_numeric(df[c],errors="coerce"),
    }).dropna().sort_values("time").drop_duplicates("time")
    return out.reset_index(drop=True)

def load_deals(path):
    df=_read_csv(path)
    t=pick(df,["time","datetime","date"])
    typ=pick(df,["type","deal type"])
    direction=pick(df,["direction"])
    price=pick(df,["price"])
    profit=pick(df,["profit"])
    deal=pick(df,["deal","deal #","ticket"])
    symbol=pick(df,["symbol"])
    if not t:
        raise ValueError(f"Backtest missing time column. Found: {list(df.columns)}")
    out=pd.DataFrame({"time":pd.to_datetime(df[t],errors="coerce")})
    out["type"]=df[typ].astype(str) if typ else ""
    out["direction"]=df[direction].astype(str) if direction else ""
    out["price"]=pd.to_numeric(df[price],errors="coerce") if price else np.nan
    out["profit"]=pd.to_numeric(df[profit],errors="coerce") if profit else np.nan
    out["deal"]=df[deal].astype(str) if deal else [str(i) for i in range(len(df))]
    out["symbol"]=df[symbol].astype(str) if symbol else ""
    out=out.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    return out

def normalize_direction(x):
    s=str(x).lower()
    if "buy" in s: return "BUY"
    if "sell" in s: return "SELL"
    return ""

def build_trades(deals):
    d=deals.copy()
    d["side"]=d["direction"].map(normalize_direction)
    # Prefer explicit IN/OUT in Direction/Type; otherwise infer alternating same-side records.
    d["is_in"]=d.apply(lambda r: any(k in (str(r["type"]).lower()+" "+str(r["direction"]).lower()) for k in ["in","entry"]),axis=1)
    d["is_out"]=d.apply(lambda r: any(k in (str(r["type"]).lower()+" "+str(r["direction"]).lower()) for k in ["out","exit"]),axis=1)

    entries=d[d["is_in"]].copy()
    exits=d[d["is_out"]].copy()

    # If MT5 CSV has explicit IN/OUT this pairing is reliable enough for research.
    rows=[]
    for side, group in entries.groupby("side"):
        ex=exits[exits["side"].eq(side)].sort_values("time")
        for _,e in group.sort_values("time").iterrows():
            after=ex[ex["time"]>=e["time"]]
            if after.empty: continue
            x=after.iloc[0]
            rows.append({
                "entry_time":e["time"],"exit_time":x["time"],"side":side,
                "entry_price":e["price"],"exit_price":x["price"],
                "profit":x["profit"] if pd.notna(x["profit"]) else np.nan,
                "entry_deal":e["deal"],"exit_deal":x["deal"],
                "symbol":e["symbol"]
            })
            ex=ex[ex["deal"].astype(str)!=str(x["deal"])]
    if not rows:
        # Fallback: pair chronological records by adjacent IN/OUT rows.
        allrows=d.sort_values("time")
        pending=None
        for _,r in allrows.iterrows():
            if r["is_in"]:
                pending=r
            elif r["is_out"] and pending is not None:
                rows.append({
                    "entry_time":pending["time"],"exit_time":r["time"],
                    "side":pending["side"] or r["side"],
                    "entry_price":pending["price"],"exit_price":r["price"],
                    "profit":r["profit"],"entry_deal":pending["deal"],
                    "exit_deal":r["deal"],"symbol":pending["symbol"]
                })
                pending=None
    return pd.DataFrame(rows)

def add_market_features(b):
    x=b.copy()
    x["range"]=x.high-x.low
    x["body"]=abs(x.close-x.open)
    x["body_ratio"]=np.where(x["range"]>0,x["body"]/x["range"],0)
    x["direction"]=np.sign(x.close-x.open)

    prev=x.close.shift(1)
    tr=pd.concat([(x.high-x.low),(x.high-prev).abs(),(x.low-prev).abs()],axis=1).max(axis=1)
    x["atr14"]=tr.rolling(14).mean()
    x["ema20"]=x.close.ewm(span=20,adjust=False).mean()
    x["ema50"]=x.close.ewm(span=50,adjust=False).mean()
    x["ema20_slope"]=x.ema20.diff(5)
    x["ema50_slope"]=x.ema50.diff(5)

    # ADX-style directional strength, computed without future bars.
    up=x.high.diff()
    down=-x.low.diff()
    plus=np.where((up>down)&(up>0),up,0.0)
    minus=np.where((down>up)&(down>0),down,0.0)
    atr=tr.rolling(14).mean().replace(0,np.nan)
    pdi=100*pd.Series(plus,index=x.index).rolling(14).mean()/atr
    mdi=100*pd.Series(minus,index=x.index).rolling(14).mean()/atr
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    x["adx14"]=dx.rolling(14).mean()

    # Volatility percentile is rolling-history only.
    x["atr_pct"]=x.atr14.rolling(100,min_periods=30).rank(pct=True)

    # Directional consistency: fraction of bullish/bearish candles in previous 10 bars.
    x["bull_ratio10"]=(x.direction.gt(0).rolling(10).mean())
    x["bear_ratio10"]=(x.direction.lt(0).rolling(10).mean())

    # No future information: regime uses the completed candle at/before entry.
    x["trend"] = np.select([
        (x.ema20>x.ema50)&(x.ema20_slope>0),
        (x.ema20<x.ema50)&(x.ema20_slope<0)
    ],["UPTREND","DOWNTREND"],default="NEUTRAL")

    x["trend_strength"]=pd.cut(
        x.adx14,[-np.inf,15,25,40,np.inf],
        labels=["WEAK","MODERATE","STRONG","VERY_STRONG"]
    ).astype(str)

    x["volatility"]=pd.cut(
        x.atr_pct,[-np.inf,.25,.75,1.0],
        labels=["LOW","NORMAL","HIGH"]
    ).astype(str)

    x["regime"]=x["trend"].astype(str)+"_"+x["trend_strength"].astype(str)+"_"+x["volatility"].astype(str)
    return x

def attach_context(trades,bars):
    if trades.empty: return trades
    b=add_market_features(bars)
    # merge_asof means only the last completed M30 bar at/before entry is used.
    t=trades.sort_values("entry_time").copy()
    ctx=b.sort_values("time").copy()
    cols=["time","range","body_ratio","atr14","atr_pct","adx14","ema20","ema50",
          "ema20_slope","ema50_slope","bull_ratio10","bear_ratio10",
          "trend","trend_strength","volatility","regime"]
    out=pd.merge_asof(t,ctx[cols],left_on="entry_time",right_on="time",direction="backward")
    return out.drop(columns=["time"],errors="ignore")
