from __future__ import annotations

import bisect
import csv
import io
import json
import math
import os
import re
import statistics
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
MAX_MB = int(os.getenv("RESEARCH_UPLOAD_MAX_MB", "2048"))
MAX_BYTES = MAX_MB * 1024 * 1024
MIN_GROUP_N = int(os.getenv("RESEARCH_MIN_GROUP_N", "20"))
PATTERN_GAP = float(os.getenv("RESEARCH_PATTERN_GAP", "0.15"))
TICK_PRE_WINDOW_MINUTES = int(os.getenv("RESEARCH_TICK_PRE_WINDOW_MINUTES", "30"))


def now():
    return datetime.now(timezone.utc)


def safe_id(v: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v):
        raise HTTPException(400, "Invalid experiment_id")
    return v


def parse_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    s = s.replace(".", "-", 2)
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
    ]
    for f in fmts:
        try:
            return datetime.strptime(s, f).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def num(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    s = s.replace("%", "")
    try:
        return float(s)
    except Exception:
        return None


def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def pick(row: dict[str, Any], *names: str) -> Any:
    nmap = {norm(k): v for k, v in row.items()}
    for name in names:
        if norm(name) in nmap:
            return nmap[norm(name)]
    for k, v in nmap.items():
        for name in names:
            nn = norm(name)
            if nn and (nn in k or k in nn):
                return v
    return None


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "table":
            self._table = []
        elif self._table is not None and tag.lower() == "tr":
            self._row = []
        elif self._row is not None and tag.lower() in ("td", "th"):
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif t == "tr" and self._table is not None and self._row is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif t == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


def parse_html_tables(raw: bytes) -> list[dict[str, Any]]:
    p = TableParser()
    p.feed(raw.decode("utf-8", "replace"))
    out = []
    for table in p.tables:
        if not table:
            continue
        header_i = None
        for i, row in enumerate(table[:8]):
            joined = " ".join(norm(x) for x in row)
            if any(x in joined for x in ("time", "profit", "positionid", "deal", "bid", "ask", "open")):
                header_i = i
                break
        if header_i is None:
            continue
        headers = table[header_i]
        for row in table[header_i + 1 :]:
            if len(row) < 2:
                continue
            if len(row) < len(headers):
                row = row + [""] * (len(headers) - len(row))
            out.append(dict(zip(headers, row[: len(headers)])))
    return out


def parse_csv(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8-sig", "replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except Exception:
        dialect = csv.excel
    return [dict(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]


def parse_xml(raw: bytes) -> list[dict[str, Any]]:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(raw)
    rows = []
    for el in root.iter():
        children = list(el)
        if children and all(len(list(c)) == 0 for c in children):
            row = {c.tag.split("}")[-1]: (c.text or "") for c in children}
            if len(row) >= 3:
                rows.append(row)
    return rows


def parse_blob(name: str, data: bytes) -> list[dict[str, Any]]:
    low = name.lower()
    if low.endswith((".html", ".htm")):
        return parse_html_tables(data)
    if low.endswith(".xml"):
        return parse_xml(data)
    return parse_csv(data)


def classify_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "unknown"
    score = {"ticks": 0, "bars": 0, "deals": 0}
    for r in rows[:100]:
        keys = " ".join(norm(k) for k in r.keys())
        if any(x in keys for x in ("bid", "ask", "last")):
            score["ticks"] += 3
        if all(pick(r, x) is not None for x in ("Open", "High", "Low", "Close")):
            score["bars"] += 3
        if any(x in keys for x in ("positionid", "deal", "profit", "entry", "ordertype")):
            score["deals"] += 2
    return max(score, key=score.get) if max(score.values()) else "unknown"


def extract_files(raw: bytes, filename: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return deal/trade-source rows, OHLC bars, ticks, and source inventory.

    A single ZIP may contain all three data types. Separate bars/ticks uploads are
    also accepted by the endpoint. Classification is schema-based, not filename-only.
    """
    blobs: list[tuple[str, bytes]] = []
    if filename.lower().endswith(".zip"):
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
            for n in z.namelist():
                if n.endswith("/"):
                    continue
                low = n.lower()
                if low.endswith((".csv", ".html", ".htm", ".xml")):
                    blobs.append((n, z.read(n)))
        except zipfile.BadZipFile as e:
            raise HTTPException(400, "Invalid data ZIP") from e
    else:
        blobs = [(filename, raw)]

    deal_rows, ohlc, ticks, inventory = [], [], [], []
    for name, data in blobs:
        try:
            rows = parse_blob(name, data)
        except Exception:
            rows = []
        kind = classify_rows(rows)
        inventory.append({"file": name, "rows": len(rows), "detected_type": kind})
        if kind == "bars":
            ohlc.extend(rows)
        elif kind == "ticks":
            ticks.extend(rows)
        elif kind == "deals":
            deal_rows.extend(rows)
    return deal_rows, ohlc, ticks, {"files": inventory}


def parse_ea(source: str) -> dict[str, Any]:
    low = source.lower()

    def has(*x):
        return any(t.lower() in low for t in x)

    params = {}
    for name in ("RiskPercent", "TargetRR", "ATRPeriod", "MinCandleATR", "MaxCandleATR", "MaxSpreadPoints", "SRLookbackBars"):
        m = re.search(rf"\b{name}\s*=\s*([0-9]+(?:\.[0-9]+)?)", source)
        if m:
            params[name] = float(m.group(1))
    return {
        "line_count": len(source.splitlines()),
        "has_buy": has(".Buy(", "trade.Buy"),
        "has_sell": has(".Sell(", "trade.Sell"),
        "has_atr": has("iATR", "ATR"),
        "has_breakout": has("breakout"),
        "has_support_resistance": has("support", "resistance"),
        "has_risk_guard": has("MaxDailyLoss", "MaxTotalLoss", "RiskGuard"),
        "parameters": params,
    }


def build_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trades = []
    for i, r in enumerate(rows, 1):
        keys = {norm(k) for k in r.keys()}
        has_open = any(k in keys for k in ("opentime", "entrytime"))
        has_close = any(k in keys for k in ("closetime", "exittime"))
        if not (has_open and has_close):
            continue
        ot = parse_dt(pick(r, "Open Time", "Entry Time", "EntryTime", "OpenTime"))
        ct = parse_dt(pick(r, "Close Time", "Exit Time", "ExitTime", "CloseTime"))
        profit = num(pick(r, "Profit", "Net Profit", "P&L", "PnL"))
        entry = num(pick(r, "Entry Price", "Open Price", "EntryPrice", "OpenPrice"))
        exitp = num(pick(r, "Exit Price", "Close Price", "ExitPrice", "ClosePrice"))
        side = str(pick(r, "Type", "Direction", "Side", "Order Type") or "").lower()
        if ot and ct and profit is not None:
            trades.append({"trade_id": str(pick(r, "Position ID", "PositionID", "Deal", "Ticket") or i), "entry_time": ot.isoformat(), "exit_time": ct.isoformat(), "entry_price": entry, "exit_price": exitp, "profit": profit, "side": side})
    if trades:
        return trades

    groups = defaultdict(list)
    for i, r in enumerate(rows, 1):
        t = parse_dt(pick(r, "Time", "Date", "Timestamp"))
        if not t:
            continue
        pos = str(pick(r, "Position ID", "PositionID", "Position", "Ticket") or "")
        entry = str(pick(r, "Entry", "Deal Entry", "Entry Type") or "").lower()
        side = str(pick(r, "Type", "Direction", "Side") or "").lower()
        groups[pos or f"row{i}"].append((t, r, entry, side))
    for j, (pos, items) in enumerate(groups.items(), 1):
        items.sort(key=lambda x: x[0])
        ins = [x for x in items if any(k in x[2] for k in ("in", "entry", "open"))]
        outs = [x for x in items if any(k in x[2] for k in ("out", "exit", "close"))]
        if not ins or not outs:
            continue
        a, b = ins[0], outs[-1]
        profit = sum(num(pick(x[1], "Profit", "P&L", "PnL")) or 0 for x in items)
        trades.append({"trade_id": pos or str(j), "entry_time": a[0].isoformat(), "exit_time": b[0].isoformat(), "entry_price": num(pick(a[1], "Price", "Entry Price")), "exit_price": num(pick(b[1], "Price", "Exit Price")), "profit": profit, "side": a[3]})
    return trades


def build_market(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        t = parse_dt(pick(r, "Time", "Date", "Datetime", "Timestamp"))
        o = num(pick(r, "Open", "Open Price")); h = num(pick(r, "High")); l = num(pick(r, "Low")); c = num(pick(r, "Close"))
        if t and None not in (o, h, l, c):
            out.append({"time": t, "open": o, "high": h, "low": l, "close": c})
    return sorted(out, key=lambda x: x["time"])


def build_ticks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        t = parse_dt(pick(r, "Time", "Date", "Datetime", "Timestamp"))
        bid = num(pick(r, "Bid")); ask = num(pick(r, "Ask")); last = num(pick(r, "Last", "Price"))
        volume = num(pick(r, "Volume", "Tick Volume", "Real Volume"))
        price = ((bid + ask) / 2) if bid is not None and ask is not None else (last if last is not None else bid if bid is not None else ask)
        if t and price is not None:
            out.append({"time": t, "bid": bid, "ask": ask, "last": last, "price": price, "volume": volume})
    return sorted(out, key=lambda x: x["time"])


def timeframe_seconds(tf: str, candles: list[dict[str, Any]]) -> int:
    s = str(tf or "").upper().strip()
    m = re.search(r"(\d+)", s)
    if "M" in s and m:
        return int(m.group(1)) * 60
    if "H" in s and m:
        return int(m.group(1)) * 3600
    if "D" in s and m:
        return int(m.group(1)) * 86400
    if "W" in s and m:
        return int(m.group(1)) * 7 * 86400
    if len(candles) >= 2:
        diffs = [(candles[i]["time"] - candles[i-1]["time"]).total_seconds() for i in range(1, min(len(candles), 500))]
        diffs = [d for d in diffs if d > 0]
        if diffs:
            return max(1, int(statistics.median(diffs)))
    return 1800


def atr14(candles):
    tr = []
    out = [None] * len(candles)
    for i, c in enumerate(candles):
        prev = candles[i-1]["close"] if i else c["close"]
        tr.append(max(c["high"]-c["low"], abs(c["high"]-prev), abs(c["low"]-prev)))
        if i >= 13:
            out[i] = sum(tr[i-13:i+1]) / 14
    return out


def _tick_slice(ticks, times, start, end):
    a = bisect.bisect_left(times, start.timestamp())
    b = bisect.bisect_right(times, end.timestamp())
    return ticks[a:b]


def enrich_trade(t, candles, atrs, timeframe, ticks=None, tick_times=None):
    et = datetime.fromisoformat(t["entry_time"])
    xt = datetime.fromisoformat(t["exit_time"])
    interval = timeframe_seconds(timeframe, candles)
    # Strict anti-look-ahead: only a bar whose close time is <= entry is eligible.
    idx = None
    for i in range(len(candles)-1, -1, -1):
        if candles[i]["time"] + timedelta(seconds=interval) <= et:
            idx = i
            break
    if idx is None:
        return None
    c = candles[idx]
    body = abs(c["close"]-c["open"]); rng = max(c["high"]-c["low"], 1e-12)
    upper = c["high"]-max(c["open"], c["close"]); lower = min(c["open"], c["close"])-c["low"]
    direction = "bullish" if c["close"] > c["open"] else "bearish" if c["close"] < c["open"] else "doji"
    streak = 1
    for j in range(idx-1, -1, -1):
        d = "bullish" if candles[j]["close"] > candles[j]["open"] else "bearish" if candles[j]["close"] < candles[j]["open"] else "doji"
        if d == direction and direction != "doji": streak += 1
        else: break
    side = t.get("side", "")
    side_sign = -1 if ("sell" in side or "short" in side) else 1
    momentum_3 = None
    if atrs[idx] and idx >= 3:
        momentum_3 = (c["close"]-candles[idx-3]["close"]) / atrs[idx]
    regime = "unknown"
    if atrs[idx]:
        rr = rng / atrs[idx]
        regime = "low_volatility" if rr < .75 else "high_volatility" if rr > 1.5 else "normal_volatility"

    entry = t.get("entry_price")
    mfe_bar = mae_bar = None
    if entry is not None:
        window = [x for x in candles if et <= x["time"] <= xt]
        if window:
            mfe_bar = max((x["high"]-entry)*side_sign for x in window)
            mae_bar = min((x["low"]-entry)*side_sign for x in window)

    tick_context = None
    mfe_tick = mae_tick = None
    if ticks is not None and tick_times is not None:
        pre = _tick_slice(ticks, tick_times, et - timedelta(minutes=TICK_PRE_WINDOW_MINUTES), et)
        post = _tick_slice(ticks, tick_times, et, xt)
        if pre:
            prices = [x["price"] for x in pre]
            spreads = [x["ask"]-x["bid"] for x in pre if x["ask"] is not None and x["bid"] is not None]
            first, last = prices[0], prices[-1]
            tick_context = {
                "pre_window_minutes": TICK_PRE_WINDOW_MINUTES,
                "tick_count": len(pre),
                "price_change": last-first,
                "price_change_abs": abs(last-first),
                "range": max(prices)-min(prices),
                "spread_median": statistics.median(spreads) if spreads else None,
                "spread_max": max(spreads) if spreads else None,
            }
        if entry is not None and post:
            fav = [(x["price"]-entry)*side_sign for x in post]
            mfe_tick = max(fav)
            mae_tick = min(fav)

    return {
        "trade": t,
        "candle_index": idx,
        "context_candle_time": c["time"].isoformat(),
        "context_candle_closed_before_entry": True,
        "direction": direction,
        "body_pct_range": body/rng,
        "range": rng,
        "upper_wick_pct": upper/rng,
        "lower_wick_pct": lower/rng,
        "streak": streak,
        "atr14": atrs[idx],
        "range_atr": (rng/atrs[idx] if atrs[idx] else None),
        "momentum_3_atr": momentum_3,
        "volatility_regime": regime,
        "mfe_bar": mfe_bar,
        "mae_bar": mae_bar,
        "mfe_tick": mfe_tick,
        "mae_tick": mae_tick,
        "tick_context": tick_context,
    }


def analyze(trades, contexts, ea, tick_available=False, bar_available=False):
    n = len(trades); wins = [t for t in trades if (t.get("profit") or 0) > 0]; losses = [t for t in trades if (t.get("profit") or 0) < 0]
    findings = []; insufficient = []
    reference = {"trade_count": n, "positive_trade_count": len(wins), "negative_trade_count": len(losses)}
    if not contexts:
        return {"reference": reference, "findings": [], "insufficient_evidence": ["MARKET_OHLC_NOT_AVAILABLE_FROM_INPUT_DATA"], "mfe_mae": None, "tick_analysis": None}
    pairs = [c for c in contexts if c]
    for feature, label, fmt in [("direction", "prior closed candle direction", lambda x:x), ("streak", "consecutive prior candle streak", lambda x:("3+" if x >= 3 else str(x))), ("volatility_regime", "prior-candle volatility regime", lambda x:x)]:
        buckets = defaultdict(list)
        for c in pairs: buckets[fmt(c[feature])].append(c)
        for bucket, items in buckets.items():
            pos = sum(1 for c in items if c["trade"]["profit"] > 0); total = len(items)
            if total < MIN_GROUP_N:
                insufficient.append({"feature": label, "bucket": bucket, "n": total, "reason": "below_minimum_sample"}); continue
            other = [c for c in pairs if c not in items]
            if len(other) < MIN_GROUP_N:
                insufficient.append({"feature": label, "bucket": bucket, "n": total, "reason": "comparison_group_too_small"}); continue
            r1 = pos/total; r2 = sum(1 for c in other if c["trade"]["profit"] > 0)/len(other)
            if abs(r1-r2) >= PATTERN_GAP:
                findings.append({"status":"OBSERVED_PATTERN","feature":label,"condition":bucket,"n":total,"reference_outcome_rate":round(r1,4),"comparison_n":len(other),"comparison_outcome_rate":round(r2,4),"gap":round(r1-r2,4),"evidence":{"trade_ids":[c["trade"]["trade_id"] for c in items[:50]],"context_candle_times":[c["context_candle_time"] for c in items[:50]]},"interpretation":"Observed association in supplied backtest data; not evidence of causation."})
    mom = [c for c in pairs if c.get("momentum_3_atr") is not None]
    if len(mom) >= 2*MIN_GROUP_N:
        for condition, items in (("negative_momentum", [c for c in mom if c["momentum_3_atr"] < -.5]), ("neutral_momentum", [c for c in mom if -.5 <= c["momentum_3_atr"] <= .5]), ("positive_momentum", [c for c in mom if c["momentum_3_atr"] > .5])):
            if len(items) < MIN_GROUP_N: continue
            other = [c for c in mom if c not in items]
            if len(other) < MIN_GROUP_N: continue
            r1 = sum(c["trade"]["profit"] > 0 for c in items)/len(items); r2 = sum(c["trade"]["profit"] > 0 for c in other)/len(other)
            if abs(r1-r2) >= PATTERN_GAP:
                findings.append({"status":"OBSERVED_PATTERN","feature":"3-candle momentum in ATR units","condition":condition,"n":len(items),"reference_outcome_rate":round(r1,4),"comparison_n":len(other),"comparison_outcome_rate":round(r2,4),"gap":round(r1-r2,4),"evidence":{"trade_ids":[c["trade"]["trade_id"] for c in items[:50]]},"interpretation":"Observed association in supplied backtest data; not evidence of causation."})
    for feature, label in [("range_atr", "prior closed candle range / ATR14"), ("body_pct_range", "prior closed candle body / range")]:
        vals = [c for c in pairs if c.get(feature) is not None]
        if len(vals) < 2*MIN_GROUP_N:
            insufficient.append({"feature": label, "n": len(vals), "reason": "insufficient_total_sample"}); continue
        vals_sorted = sorted(vals, key=lambda c: c[feature]); med = vals_sorted[len(vals_sorted)//2][feature]
        low = [c for c in vals if c[feature] <= med]; high = [c for c in vals if c[feature] > med]
        if len(low) < MIN_GROUP_N or len(high) < MIN_GROUP_N: continue
        rl = sum(c["trade"]["profit"] > 0 for c in low)/len(low); rh = sum(c["trade"]["profit"] > 0 for c in high)/len(high)
        if abs(rl-rh) >= PATTERN_GAP:
            findings.append({"status":"OBSERVED_PATTERN","feature":label,"condition":f"<=median({med:.4g}) vs >median","n_low":len(low),"n_high":len(high),"outcome_rate_low":round(rl,4),"outcome_rate_high":round(rh,4),"gap":round(rl-rh,4),"evidence":{"low_trade_ids":[c["trade"]["trade_id"] for c in low[:50]],"high_trade_ids":[c["trade"]["trade_id"] for c in high[:50]]},"interpretation":"Observed association in supplied backtest data; not evidence of causation."})
    bar_mfe = [c["mfe_bar"] for c in pairs if c.get("mfe_bar") is not None]; bar_mae = [c["mae_bar"] for c in pairs if c.get("mae_bar") is not None]
    tick_mfe = [c["mfe_tick"] for c in pairs if c.get("mfe_tick") is not None]; tick_mae = [c["mae_tick"] for c in pairs if c.get("mae_tick") is not None]
    tick_ctx = [c for c in pairs if c.get("tick_context")]
    return {"reference":reference,"findings":findings,"insufficient_evidence":insufficient,"mfe_mae":{"bar_sample":len(bar_mfe),"bar_mfe_median":statistics.median(bar_mfe) if bar_mfe else None,"bar_mae_median":statistics.median(bar_mae) if bar_mae else None,"tick_sample":len(tick_mfe),"tick_mfe_median":statistics.median(tick_mfe) if tick_mfe else None,"tick_mae_median":statistics.median(tick_mae) if tick_mae else None},"tick_analysis":{"available":tick_available,"pre_entry_context_sample":len(tick_ctx),"pre_entry_tick_count_median":statistics.median([c["tick_context"]["tick_count"] for c in tick_ctx]) if tick_ctx else None}}


def make_experiment(db: Session, experiment_id: str, symbol: str, timeframe: str, ea_name: str) -> Experiment:
    e = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if e: return e
    h = Hypothesis(id=str(uuid4()), title=f"Unified research: {ea_name}", statement="Evaluate pre-entry market context and post-entry behavior from supplied EA/backtest evidence without look-ahead bias.", assumptions="Only supplied files are evidence. Missing OHLC or tick data limits the corresponding research layer.", status="active")
    db.add(h); db.flush()
    e = Experiment(id=experiment_id, hypothesis_id=h.id, symbol=symbol or "UNKNOWN", timeframe=timeframe or "UNKNOWN", experiment_type="unified_backtest_research", specification="EA .mq5 + MT5 trades/deals + optional OHLC bars + optional tick data; normalize once, analyze once, evidence-gate findings.", baseline="", status="uploaded")
    db.add(e); db.commit(); return e


async def _save_upload_stream(upload: UploadFile | None, dest: Path) -> tuple[str, int] | None:
    if upload is None or not upload.filename:
        return None
    total = 0
    chunk_size = 8 * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                try: dest.unlink()
                except FileNotFoundError: pass
                raise HTTPException(413, f"{upload.filename} exceeds {MAX_MB} MB limit")
            out.write(chunk)
    return upload.filename, total


@router.post("/data/upload", summary="Upload EA + MT5 trades/deals + optional OHLC bars + optional ticks")
async def upload_data(
    experiment_id: str = Form(None),
    symbol: str = Form(""),
    timeframe: str = Form(""),
    ea_file: UploadFile = File(...),
    backtest_file: UploadFile = File(...),
    bars_file: UploadFile | None = File(None),
    ticks_file: UploadFile | None = File(None),
):
    if not ea_file.filename or not ea_file.filename.lower().endswith(".mq5"):
        raise HTTPException(400, "ea_file must be .mq5")
    allowed = (".csv", ".html", ".htm", ".xml", ".zip")
    for u, label in ((backtest_file, "backtest_file"), (bars_file, "bars_file"), (ticks_file, "ticks_file")):
        if u is not None and (not u.filename or not u.filename.lower().endswith(allowed)):
            raise HTTPException(400, f"{label} must be CSV/HTML/XML/ZIP")

    eid = safe_id(experiment_id) if experiment_id else str(uuid4())
    folder = ROOT / eid; folder.mkdir(parents=True, exist_ok=True)
    ea_saved = await _save_upload_stream(ea_file, folder/"ea.mq5")
    bt_saved = await _save_upload_stream(backtest_file, folder/"backtest")
    manifest = {"ea_filename": ea_saved[0] if ea_saved else None, "backtest_filename": bt_saved[0] if bt_saved else None, "bars_filename": None, "ticks_filename": None, "sizes_bytes": {"ea": ea_saved[1] if ea_saved else 0, "backtest": bt_saved[1] if bt_saved else 0, "bars": 0, "ticks": 0}}
    if bars_file:
        saved = await _save_upload_stream(bars_file, folder/"bars")
        manifest["bars_filename"] = saved[0]; manifest["sizes_bytes"]["bars"] = saved[1]
    if ticks_file:
        saved = await _save_upload_stream(ticks_file, folder/"ticks")
        manifest["ticks_filename"] = saved[0]; manifest["sizes_bytes"]["ticks"] = saved[1]
    (folder/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    db = SessionLocal()
    try:
        make_experiment(db, eid, symbol, timeframe, ea_file.filename)
        return {"status":"uploaded","experiment_id":eid,"files":{"ea":ea_file.filename,"backtest":backtest_file.filename,"bars":manifest["bars_filename"],"ticks":manifest["ticks_filename"]},"data_modes":{"backtest_bundle_can_contain":"deals + bars + ticks","separate_bars":bool(bars_file),"separate_ticks":bool(ticks_file)},"next":"POST /api/research/{experiment_id}/run"}
    finally:
        db.close()


@router.get("/{experiment_id}")
def get_research(experiment_id: str):
    eid = safe_id(experiment_id); db = SessionLocal()
    try:
        e = db.query(Experiment).filter(Experiment.id == eid).first()
        if not e: raise HTTPException(404, "Experiment not found")
        r = db.query(ExperimentResult).filter(ExperimentResult.experiment_id == eid).order_by(ExperimentResult.created_at.desc()).first()
        return {"experiment_id":eid,"status":e.status,"symbol":e.symbol,"timeframe":e.timeframe,"result":json.loads(r.metrics) if r else None,"result_id":r.id if r else None}
    finally:
        db.close()


@router.post("/{experiment_id}/run")
def run_research(experiment_id: str):
    eid = safe_id(experiment_id); folder = ROOT/eid
    if not (folder/"ea.mq5").is_file() or not (folder/"backtest").is_file():
        raise HTTPException(409, "Upload EA and backtest first")
    db = SessionLocal()
    try:
        e = db.query(Experiment).filter(Experiment.id == eid).first()
        if not e: raise HTTPException(404, "Experiment not found")
        ea = (folder/"ea.mq5").read_text("utf-8", errors="replace")
        manifest = json.loads((folder/"manifest.json").read_text("utf-8")) if (folder/"manifest.json").is_file() else {}
        bt_name = manifest.get("backtest_filename", "backtest.csv")
        deal_rows, ohlc_rows, tick_rows, inv_bt = extract_files((folder/"backtest").read_bytes(), bt_name)
        inventory = inv_bt
        if (folder/"bars").is_file():
            d, b, t, inv = extract_files((folder/"bars").read_bytes(), manifest.get("bars_filename", "bars.csv")); ohlc_rows.extend(b); tick_rows.extend(t); deal_rows.extend(d); inventory["files"].extend(inv["files"])
        if (folder/"ticks").is_file():
            d, b, t, inv = extract_files((folder/"ticks").read_bytes(), manifest.get("ticks_filename", "ticks.csv")); ohlc_rows.extend(b); tick_rows.extend(t); deal_rows.extend(d); inventory["files"].extend(inv["files"])

        trades = build_trades(deal_rows); candles = build_market(ohlc_rows); ticks = build_ticks(tick_rows); atrs = atr14(candles)
        tick_times = [x["time"].timestamp() for x in ticks]
        contexts = [enrich_trade(t, candles, atrs, e.timeframe, ticks, tick_times) for t in trades] if candles else []
        analysis = analyze(trades, contexts, parse_ea(ea), tick_available=bool(ticks), bar_available=bool(candles))
        limitations = []
        if not candles: limitations.append("MT5 backtest input did not expose usable OHLC bars; pre-entry bar price-action/regime analysis is NOT_AVAILABLE_FROM_INPUT_DATA.")
        if not ticks: limitations.append("Tick data was not supplied or could not be parsed; tick-level pre-entry context and tick-level MFE/MAE are NOT_AVAILABLE_FROM_INPUT_DATA.")
        if not trades: limitations.append("No completed trades could be reconstructed from the supplied backtest input.")
        if analysis["findings"] == []: limitations.append("No pattern passed the evidence gate; this is not evidence that no relationship exists.")
        result_obj = {
            "engine":"unified_research_v2",
            "status":"completed",
            "experiment_id":eid,
            "input_lineage":{"ea_file":"ea.mq5","source_inventory":inventory,"roles":{"trades":"MT5 deals/trades","bars":"OHLC primary market context","ticks":"high-resolution supporting context"}},
            "ea_analysis":parse_ea(ea),
            "data_quality":{"parsed_trade_count":len(trades),"bar_count":len(candles),"tick_count":len(ticks),"market_context_coverage":round(len(contexts)/len(trades),4) if trades else 0,"time_range":{"bars_start":candles[0]["time"].isoformat() if candles else None,"bars_end":candles[-1]["time"].isoformat() if candles else None,"ticks_start":ticks[0]["time"].isoformat() if ticks else None,"ticks_end":ticks[-1]["time"].isoformat() if ticks else None}},
            "analysis":analysis,
            "limitations":limitations,
            "evidence_policy":{"min_group_n":MIN_GROUP_N,"pattern_gap":PATTERN_GAP,"primary_market_context":"OHLC bars","tick_role":"supporting high-resolution context and tick-level MFE/MAE","lookahead_policy":"Pre-entry bar context uses only candles whose full close time is <= entry. Pre-entry tick context uses only ticks strictly before entry. Post-entry MFE/MAE is explicitly separated from pre-entry evidence.","causality":"not_claimed"},
            "generated_at":now().isoformat(),
        }
        r = ExperimentResult(id=str(uuid4()), experiment_id=e.id, summary="Unified EA + MT5 trades/deals + bars + ticks research", metrics=json.dumps(result_obj, ensure_ascii=False, default=str), evidence=json.dumps({"finding_count":len(analysis["findings"]),"data_sources":["EA","trades/deals","OHLC bars"] + (["ticks"] if ticks else [])}, ensure_ascii=False), limitations=json.dumps(limitations, ensure_ascii=False), conclusion="Research findings are evidence-gated observations from supplied data; no causal or trading recommendation claim is made.")
        db.add(r); e.status="completed"; e.completed_at=now(); db.commit()
        return result_obj
    finally:
        db.close()
