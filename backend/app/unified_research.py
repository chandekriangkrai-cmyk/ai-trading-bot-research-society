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
    """Parse normal CSV or an MT5 Strategy Tester report exported as CSV.

    MT5 reports can contain many metadata rows before the actual Deals table.
    We deliberately locate the Deals header instead of assuming row 1 is data.
    """
    text = raw.decode("utf-8-sig", "replace")
    lines = text.splitlines()

    # Prefer the actual MT5 Deals table when present.
    header_idx = None
    for i, line in enumerate(lines):
        cols = [c.strip().strip('"') for c in line.split(",")]
        norm_cols = {norm(c) for c in cols if c}
        if {"time", "deal", "direction", "price", "profit"}.issubset(norm_cols):
            header_idx = i
            break

    if header_idx is not None:
        reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
        return [dict(r) for r in reader if any(str(v or "").strip() for v in r.values())]

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
    """Reconstruct completed trades from MT5 deals.

    MT5 Strategy Tester CSV exports often contain IN and OUT deal rows without a
    Position ID. In that case we pair FIFO by direction/volume: a BUY IN is
    closed by the next SELL OUT and vice versa. This is appropriate for the
    supplied single-symbol tester report and is explicitly recorded in lineage.
    """
    # Direct completed-trade tables are accepted only when the exact open/close
    # fields are present. Do not use fuzzy matching here because MT5 deal rows
    # contain a field named Time, which must NOT be mistaken for Open Time.
    direct = []
    for i, r in enumerate(rows, 1):
        keys = {norm(k) for k in r.keys()}
        if not ({"opentime", "closetime"} & keys or {"entrytime", "exittime"} <= keys):
            continue
        ot = parse_dt(r.get("Open Time") or r.get("Entry Time") or r.get("OpenTime") or r.get("EntryTime"))
        ct = parse_dt(r.get("Close Time") or r.get("Exit Time") or r.get("CloseTime") or r.get("ExitTime"))
        profit = num(r.get("Profit") or r.get("Net Profit") or r.get("P&L") or r.get("PnL"))
        entry = num(r.get("Entry Price") or r.get("Open Price") or r.get("EntryPrice") or r.get("OpenPrice"))
        exitp = num(r.get("Exit Price") or r.get("Close Price") or r.get("ExitPrice") or r.get("ClosePrice"))
        side = str(r.get("Type") or r.get("Direction") or r.get("Side") or r.get("Order Type") or "").lower()
        if ot and ct and profit is not None:
            direct.append({"trade_id": str(r.get("Position ID") or r.get("PositionID") or r.get("Ticket") or r.get("Deal") or i),
                           "entry_time": ot.isoformat(), "exit_time": ct.isoformat(), "entry_price": entry,
                           "exit_price": exitp, "profit": profit, "side": side,
                           "entry_deal": None, "exit_deal": None, "reconstruction": "direct"})
    if direct:
        return sorted(direct, key=lambda x: x["entry_time"])

    # MT5 deal table: use rows with explicit IN/OUT direction.
    events = []
    for i, r in enumerate(rows, 1):
        t = parse_dt(pick(r, "Time", "Date", "Timestamp"))
        if not t:
            continue
        direction = str(pick(r, "Direction", "Deal Direction", "Entry") or "").lower()
        typ = str(pick(r, "Type", "Order Type", "Side") or "").lower()
        if direction not in ("in", "out"):
            # Some exports call the field Entry.
            direction = "in" if "entry" in direction or "open" in direction else "out" if "exit" in direction or "close" in direction else direction
        if direction not in ("in", "out"):
            continue
        price = num(pick(r, "Price", "Entry Price", "Open Price"))
        volume = num(pick(r, "Volume", "Lots"))
        deal = str(pick(r, "Deal", "Ticket", "ID") or i)
        profit = num(pick(r, "Profit", "P&L", "PnL")) or 0.0
        commission = num(pick(r, "Commission")) or 0.0
        swap = num(pick(r, "Swap")) or 0.0
        # Type is buy/sell; Direction says whether the deal opened/closed it.
        side = "buy" if "buy" in typ else "sell" if "sell" in typ else typ
        events.append({"time": t, "direction": direction, "side": side, "price": price,
                       "volume": volume, "deal": deal, "profit": profit,
                       "commission": commission, "swap": swap})
    events.sort(key=lambda x: x["time"])

    # Separate FIFO books for long and short positions.
    books = {"buy": [], "sell": []}
    trades = []
    for ev in events:
        if ev["direction"] == "in":
            if ev["side"] in books:
                books[ev["side"]].append(ev)
            continue

        # A SELL OUT closes a BUY IN; BUY OUT closes a SELL IN.
        open_side = "buy" if ev["side"] == "sell" else "sell" if ev["side"] == "buy" else None
        if open_side is None or not books[open_side]:
            continue

        remaining = ev["volume"] if ev["volume"] is not None else None
        op = books[open_side][0]
        # Most tester exports here are one-in/one-out. If partial closes exist,
        # consume volume FIFO and keep the remainder in the book.
        matched_volume = remaining if remaining is not None and op["volume"] is not None else op["volume"]
        if matched_volume is None:
            matched_volume = 0.0
        trade_profit = ev["profit"] + ev["commission"] + ev["swap"] + op["commission"] + op["swap"]
        trade_id = f'{op["deal"]}->{ev["deal"]}'
        trades.append({
            "trade_id": trade_id,
            "entry_time": op["time"].isoformat(),
            "exit_time": ev["time"].isoformat(),
            "entry_price": op["price"],
            "exit_price": ev["price"],
            "profit": trade_profit,
            "side": open_side,
            "entry_deal": op["deal"],
            "exit_deal": ev["deal"],
            "volume": matched_volume,
            "reconstruction": "fifo_mt5_in_out"
        })
        if remaining is not None and op["volume"] is not None and op["volume"] > remaining + 1e-12:
            op["volume"] -= remaining
        else:
            books[open_side].pop(0)

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



def stream_tick_contexts(trades: list[dict[str, Any]], tick_path: Path, filename: str) -> dict[str, dict[str, Any]]:
    """One-pass streaming tick analysis; never materializes the tick file.

    Computes only pre-entry context and post-entry MFE/MAE for supplied trades.
    This keeps a 600+ MB MT5 tick CSV from expanding into multi-GB Python objects.
    """
    if not trades or not tick_path.is_file():
        return {}
    ordered = sorted(enumerate(trades), key=lambda z: z[1]["entry_time"])
    windows = []
    for idx, t in ordered:
        et = datetime.fromisoformat(t["entry_time"])
        xt = datetime.fromisoformat(t["exit_time"])
        windows.append({
            "idx": idx, "pre_start": et - timedelta(minutes=TICK_PRE_WINDOW_MINUTES),
            "entry": et, "exit": xt, "pre_prices": [], "spreads": [],
            "pre_first": None, "pre_last": None, "pre_min": None, "pre_max": None,
            "post_mfe": None, "post_mae": None, "pre_count": 0
        })

    # Header detection and delimiter detection without reading the whole file.
    with tick_path.open("rb") as fb:
        raw_sample = fb.read(16384)
        sample = raw_sample.decode("utf-8-sig", "replace")
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except Exception:
            dialect = csv.excel

    active = []
    next_start = 0
    finished = {}
    with tick_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f, dialect=dialect)
        for row in reader:
            t = parse_dt(pick(row, "Time", "Date", "Datetime", "Timestamp"))
            if not t:
                continue
            bid = num(pick(row, "Bid")); ask = num(pick(row, "Ask")); last = num(pick(row, "Last", "Price"))
            price = ((bid + ask) / 2) if bid is not None and ask is not None else (last if last is not None else bid if bid is not None else ask)
            if price is None:
                continue

            while next_start < len(windows) and windows[next_start]["pre_start"] <= t:
                active.append(windows[next_start]); next_start += 1

            # Remove windows that ended before this tick.
            active = [w for w in active if w["exit"] >= t]
            for w in active:
                if t < w["pre_start"]:
                    continue
                side_sign = -1 if ("sell" in str(trades[w["idx"]].get("side","")).lower() or "short" in str(trades[w["idx"]].get("side","")).lower()) else 1
                if w["pre_start"] <= t < w["entry"]:
                    w["pre_count"] += 1
                    if w["pre_first"] is None: w["pre_first"] = price
                    w["pre_last"] = price
                    w["pre_min"] = price if w["pre_min"] is None else min(w["pre_min"], price)
                    w["pre_max"] = price if w["pre_max"] is None else max(w["pre_max"], price)
                    if bid is not None and ask is not None: w["spreads"].append(ask-bid)
                elif w["entry"] <= t <= w["exit"] and trades[w["idx"]].get("entry_price") is not None:
                    move = (price - trades[w["idx"]]["entry_price"]) * side_sign
                    w["post_mfe"] = move if w["post_mfe"] is None else max(w["post_mfe"], move)
                    w["post_mae"] = move if w["post_mae"] is None else min(w["post_mae"], move)

    for w in windows:
        if w["pre_count"]:
            pre = {
                "pre_window_minutes": TICK_PRE_WINDOW_MINUTES,
                "tick_count": w["pre_count"],
                "price_change": w["pre_last"] - w["pre_first"],
                "price_change_abs": abs(w["pre_last"] - w["pre_first"]),
                "range": w["pre_max"] - w["pre_min"],
                "spread_median": statistics.median(w["spreads"]) if w["spreads"] else None,
                "spread_max": max(w["spreads"]) if w["spreads"] else None,
            }
        else:
            pre = None
        finished[w["idx"]] = {"tick_context": pre, "mfe_tick": w["post_mfe"], "mae_tick": w["post_mae"]}
    return finished

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
        "wick_imbalance": ((upper-lower)/rng) if rng else None,
        "candle_close_location": ((c["close"]-c["low"])/rng) if rng else None,
        "trade_side_alignment": (
            "aligned" if ((side_sign > 0 and direction == "bullish") or (side_sign < 0 and direction == "bearish"))
            else "opposed" if direction != "doji" else "neutral"
        ),
        "mfe_bar": mfe_bar,
        "mae_bar": mae_bar,
        "mfe_tick": mfe_tick,
        "mae_tick": mae_tick,
        "tick_context": tick_context,
    }



def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _two_prop_test(a: int, n1: int, b: int, n2: int) -> dict[str, float | None]:
    if min(n1, n2) <= 0:
        return {"p_value": None, "z": None, "gap": None}
    p1, p2 = a/n1, b/n2
    pooled = (a+b)/(n1+n2)
    se = math.sqrt(max(pooled*(1-pooled)*(1/n1+1/n2), 1e-15))
    z = (p1-p2)/se
    p = 2*(1-_normal_cdf(abs(z)))
    return {"p_value": p, "z": z, "gap": p1-p2}


def _wilson(rate: float, n: int) -> tuple[float, float]:
    if n <= 0: return (None, None)
    z = 1.959963984540054
    den = 1 + z*z/n
    center = (rate + z*z/(2*n))/den
    half = z*math.sqrt(rate*(1-rate)/n + z*z/(4*n*n))/den
    return (max(0.0, center-half), min(1.0, center+half))


def analyze(trades, contexts, ea, tick_available=False, bar_available=False):
    """Evidence-first research.

    A finding is promoted only when sample size, effect size and a statistical
    comparison all clear the gate. Multiple tests are Bonferroni-adjusted.
    The engine never calls association causation and never treats post-entry
    behavior as pre-entry evidence.
    """
    n = len(trades)
    wins = [t for t in trades if (t.get("profit") or 0) > 0]
    losses = [t for t in trades if (t.get("profit") or 0) < 0]
    pairs = [c for c in contexts if c]
    reference = {
        "trade_count": n,
        "positive_trade_count": len(wins),
        "negative_trade_count": len(losses),
        "context_trade_count": len(pairs),
        "baseline_positive_rate": round(len(wins)/n, 6) if n else None
    }
    if not pairs:
        return {"reference": reference, "findings": [], "insufficient_evidence":[
            {"status":"INSUFFICIENT_EVIDENCE","reason":"MARKET_OHLC_NOT_AVAILABLE_FROM_INPUT_DATA"}],
            "mfe_mae": None, "tick_analysis": {"available": tick_available}}

    # Pre-registered feature families. Avoid testing dozens of arbitrary thresholds.
    feature_specs = [
        ("direction", "prior closed candle direction"),
        ("volatility_regime", "prior-candle volatility regime"),
        ("trade_side_alignment", "trade-side alignment with prior candle"),
        ("streak_bucket", "consecutive prior candle streak"),
        ("momentum_bucket", "3-candle momentum in ATR units"),
        ("range_atr_bucket", "prior candle range / ATR14"),
        ("body_pct_bucket", "prior candle body / range"),
        ("wick_imbalance_bucket", "prior candle upper-vs-lower wick imbalance"),
        ("close_location_bucket", "prior candle close location"),
    ]
    prepared = []
    for c in pairs:
        c["streak_bucket"] = "3+" if c.get("streak",0) >= 3 else str(c.get("streak",0))
        m = c.get("momentum_3_atr")
        c["momentum_bucket"] = "negative" if m is not None and m < -0.5 else "neutral" if m is not None and m <= 0.5 else "positive" if m is not None else None
        r = c.get("range_atr")
        c["range_atr_bucket"] = "small(<0.75)" if r is not None and r < .75 else "large(>1.5)" if r is not None and r > 1.5 else "normal" if r is not None else None
        b = c.get("body_pct_range")
        c["body_pct_bucket"] = "weak(<0.35)" if b is not None and b < .35 else "strong(>0.65)" if b is not None and b > .65 else "mid" if b is not None else None
        w = c.get("wick_imbalance")
        c["wick_imbalance_bucket"] = "upper" if w is not None and w > .25 else "lower" if w is not None and w < -.25 else "balanced" if w is not None else None
        cl = c.get("candle_close_location")
        c["close_location_bucket"] = "lower(<0.35)" if cl is not None and cl < .35 else "upper(>0.65)" if cl is not None and cl > .65 else "middle" if cl is not None else None
        prepared.append(c)

    candidates = []
    for feature, label in feature_specs:
        buckets = defaultdict(list)
        for c in prepared:
            v = c.get(feature)
            if v is not None: buckets[v].append(c)
        for bucket, items in buckets.items():
            other = [c for c in prepared if c.get(feature) is not None and c.get(feature) != bucket]
            if len(items) < MIN_GROUP_N or len(other) < MIN_GROUP_N:
                continue
            a = sum(c["trade"]["profit"] > 0 for c in items)
            b = sum(c["trade"]["profit"] > 0 for c in other)
            test = _two_prop_test(a, len(items), b, len(other))
            rate = a/len(items); ref = b/len(other)
            candidates.append({
                "feature": label, "condition": bucket, "n": len(items), "comparison_n": len(other),
                "outcome_rate": rate, "comparison_outcome_rate": ref, "gap": rate-ref,
                "p_value_raw": test["p_value"], "z": test["z"],
                "trade_ids": [c["trade"]["trade_id"] for c in items[:100]],
                "context_candle_times": [c["context_candle_time"] for c in items[:100]]
            })

    # Bonferroni gate: effect >= configured gap AND adjusted p < .05.
    mtests = max(1, len(candidates))
    findings = []
    for c in sorted(candidates, key=lambda x: (x["p_value_raw"] if x["p_value_raw"] is not None else 1, -abs(x["gap"]))):
        p_adj = min(1.0, (c["p_value_raw"] or 1.0) * mtests)
        ci_lo, ci_hi = _wilson(c["outcome_rate"], c["n"])
        if abs(c["gap"]) >= PATTERN_GAP and p_adj < 0.05:
            findings.append({
                "status":"VALIDATED_PATTERN",
                "feature":c["feature"], "condition":c["condition"],
                "n":c["n"], "comparison_n":c["comparison_n"],
                "outcome_rate":round(c["outcome_rate"],4),
                "comparison_outcome_rate":round(c["comparison_outcome_rate"],4),
                "gap":round(c["gap"],4),
                "p_value_raw":round(c["p_value_raw"],8),
                "p_value_bonferroni":round(p_adj,8),
                "wilson_95ci": [round(ci_lo,4), round(ci_hi,4)],
                "evidence":{"trade_ids":c["trade_ids"],"context_candle_times":c["context_candle_times"]},
                "interpretation":"Statistically supported association under the pre-registered evidence gate; it is not proof of causation and may not generalize outside this sample."
            })
        elif abs(c["gap"]) >= PATTERN_GAP:
            # Keep near-misses auditable, but do not publish them as findings.
            pass

    # Add explicit insufficiency when no candidate clears the gate.
    insufficient = []
    if not findings:
        insufficient.append({
            "status":"INSUFFICIENT_EVIDENCE",
            "reason":"No pre-entry feature passed both effect-size and multiple-testing statistical gates.",
            "tests_considered":mtests,
            "minimum_group_n":MIN_GROUP_N,
            "minimum_absolute_rate_gap":PATTERN_GAP
        })

    bar_mfe = [c["mfe_bar"] for c in pairs if c.get("mfe_bar") is not None]
    bar_mae = [c["mae_bar"] for c in pairs if c.get("mae_bar") is not None]
    tick_mfe = [c["mfe_tick"] for c in pairs if c.get("mfe_tick") is not None]
    tick_mae = [c["mae_tick"] for c in pairs if c.get("mae_tick") is not None]
    tick_ctx = [c for c in pairs if c.get("tick_context")]

    return {
        "reference":reference,
        "findings":findings,
        "insufficient_evidence":insufficient,
        "mfe_mae":{
            "bar_sample":len(bar_mfe),
            "bar_mfe_median":statistics.median(bar_mfe) if bar_mfe else None,
            "bar_mae_median":statistics.median(bar_mae) if bar_mae else None,
            "tick_sample":len(tick_mfe),
            "tick_mfe_median":statistics.median(tick_mfe) if tick_mfe else None,
            "tick_mae_median":statistics.median(tick_mae) if tick_mae else None
        },
        "tick_analysis":{
            "available":tick_available,
            "pre_entry_context_sample":len(tick_ctx),
            "pre_entry_tick_count_median":statistics.median([c["tick_context"]["tick_count"] for c in tick_ctx]) if tick_ctx else None
        }
    }

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
    eid = safe_id(experiment_id)
    folder = ROOT / eid
    if not (folder/"ea.mq5").is_file() or not (folder/"backtest").is_file():
        raise HTTPException(409, "Upload EA and backtest first")

    db = SessionLocal()
    try:
        e = db.query(Experiment).filter(Experiment.id == eid).first()
        if not e:
            raise HTTPException(404, "Experiment not found")

        ea = (folder/"ea.mq5").read_text("utf-8", errors="replace")
        manifest = json.loads((folder/"manifest.json").read_text("utf-8")) if (folder/"manifest.json").is_file() else {}
        bt_name = manifest.get("backtest_filename", "backtest.csv")

        # Backtest may be a normal Deals CSV OR a full MT5 Strategy Tester report.
        backtest_bytes = (folder/"backtest").read_bytes()
        deal_rows, ohlc_rows, _, inv_bt = extract_files(backtest_bytes, bt_name)
        inventory = inv_bt

        if (folder/"bars").is_file():
            d, b, _, inv = extract_files((folder/"bars").read_bytes(), manifest.get("bars_filename", "bars.csv"))
            ohlc_rows.extend(b); deal_rows.extend(d)
            inventory["files"].extend(inv["files"])

        trades = build_trades(deal_rows)
        candles = build_market(ohlc_rows)
        atrs = atr14(candles)

        # First build all pre-entry OHLC context. This is the primary evidence layer.
        contexts = [enrich_trade(t, candles, atrs, e.timeframe) for t in trades] if candles else []
        contexts = [c for c in contexts if c]

        # Tick layer is deliberately streaming. A 600+ MB tick file is never
        # converted into a Python list and never duplicated in RAM.
        tick_available = False
        tick_stats = {}
        tick_path = folder / "ticks"
        if tick_path.is_file() and trades:
            tick_available = tick_path.stat().st_size > 0
            if tick_available:
                tick_stats = stream_tick_contexts(trades, tick_path, manifest.get("ticks_filename", "ticks.csv"))
                by_trade = {c["trade"]["trade_id"]: c for c in contexts}
                for i, t in enumerate(trades):
                    c = by_trade.get(t["trade_id"])
                    s = tick_stats.get(i)
                    if c and s:
                        c["tick_context"] = s["tick_context"]
                        c["mfe_tick"] = s["mfe_tick"]
                        c["mae_tick"] = s["mae_tick"]

        analysis = analyze(
            trades, contexts, parse_ea(ea),
            tick_available=tick_available,
            bar_available=bool(candles)
        )

        limitations = []
        if not candles:
            limitations.append("NOT_AVAILABLE_FROM_INPUT_DATA: usable OHLC bars were not parsed.")
        if not tick_available:
            limitations.append("NOT_AVAILABLE_FROM_INPUT_DATA: tick file absent or empty.")
        elif len(tick_stats) < len(trades):
            limitations.append("INSUFFICIENT_EVIDENCE: some trades had no usable tick window.")
        if not trades:
            limitations.append("INSUFFICIENT_EVIDENCE: no completed trades could be reconstructed from MT5 deals.")
        if not analysis["findings"]:
            limitations.append("INSUFFICIENT_EVIDENCE: no pattern passed the evidence gate; absence of a finding is not evidence that no relationship exists.")

        result_obj = {
            "engine": "unified_research_v3_evidence_first_streaming",
            "status": "completed",
            "experiment_id": eid,
            "input_lineage": {
                "ea_file": "ea.mq5",
                "source_inventory": inventory,
                "roles": {
                    "trades": "MT5 deals/trades; full Strategy Tester CSV Reports are parsed from their Deals section",
                    "bars": "OHLC primary pre-entry market context",
                    "ticks": "streamed high-resolution context; never loaded as a full in-memory row list"
                },
                "trade_reconstruction": "FIFO pairing of MT5 IN/OUT deals when Position ID is unavailable; explicitly traceable by entry_deal->exit_deal"
            },
            "ea_analysis": parse_ea(ea),
            "data_quality": {
                "raw_deal_rows": len(deal_rows),
                "completed_trade_count": len(trades),
                "bar_count": len(candles),
                "tick_file_bytes": tick_path.stat().st_size if tick_path.is_file() else 0,
                "tick_processing": "streaming_one_pass",
                "market_context_coverage": round(len(contexts)/len(trades), 4) if trades else 0,
                "time_range": {
                    "bars_start": candles[0]["time"].isoformat() if candles else None,
                    "bars_end": candles[-1]["time"].isoformat() if candles else None,
                    "trades_start": trades[0]["entry_time"] if trades else None,
                    "trades_end": trades[-1]["exit_time"] if trades else None
                }
            },
            "analysis": analysis,
            "limitations": limitations,
            "evidence_policy": {
                "minimum_group_n": MIN_GROUP_N,
                "minimum_absolute_rate_gap": PATTERN_GAP,
                "statistical_gate": "two-proportion test with Bonferroni correction across pre-registered feature/bucket tests; adjusted p < 0.05 plus minimum effect size",
                "primary_market_context": "completed OHLC candles only; no partial candle at entry",
                "tick_role": "pre-entry microstructure context and post-entry MFE/MAE; never used to create pre-entry evidence after the entry timestamp",
                "lookahead_policy": "pre-entry features use only information available strictly before entry; post-entry MFE/MAE is stored separately",
                "causality": "not_claimed",
                "publication_rule": "Only VALIDATED_PATTERN findings pass the research publication layer; near-miss associations remain excluded."
            },
            "generated_at": now().isoformat(),
        }

        evidence = {
            "finding_count": len(analysis["findings"]),
            "validated_finding_ids": [
                f"F-{i+1:03d}" for i, _ in enumerate(analysis["findings"])
            ],
            "data_sources": ["EA", "MT5 deals/trades", "OHLC bars"] + (["ticks_stream"] if tick_available else []),
            "lineage_note": "Each finding carries trade IDs and context-candle timestamps; MT5 FIFO trades carry entry_deal and exit_deal."
        }

        r = ExperimentResult(
            id=str(uuid4()),
            experiment_id=e.id,
            summary="Evidence-first EA + MT5 market-context research with streaming tick analysis",
            metrics=json.dumps(result_obj, ensure_ascii=False, default=str),
            evidence=json.dumps(evidence, ensure_ascii=False),
            limitations=json.dumps(limitations, ensure_ascii=False),
            conclusion="Only evidence-gated associations are promoted. No causal or trading recommendation claim is made."
        )
        db.add(r)
        e.status = "completed"
        e.completed_at = now()
        db.commit()
        return result_obj
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        # Return a useful application error instead of allowing Render's proxy
        # to surface an opaque 502.
        raise HTTPException(500, f"Research engine error: {type(exc).__name__}: {str(exc)[:500]}")
    finally:
        db.close()
