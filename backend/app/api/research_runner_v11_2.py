from __future__ import annotations

import io
import json
import uuid
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult

router = APIRouter(
    prefix="/research/experiments",
    tags=["Research Engine v11.2 Robustness 3-Year"],
)

MIN_TRADES_DEFAULT = 20


def _read_csv(upload: UploadFile, label: str) -> pd.DataFrame:
    raw = upload.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail=f"Empty CSV upload: {label}")
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to parse {label}: {exc}") from exc
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def _require(df: pd.DataFrame, cols: set[str], label: str) -> None:
    missing = sorted(cols - set(df.columns))
    if missing:
        raise HTTPException(status_code=400, detail=f"{label} missing columns: {', '.join(missing)}")


def _parse_time(s: pd.Series, tz: str) -> pd.Series:
    x = pd.to_datetime(s, errors="coerce")
    if x.isna().all():
        raise HTTPException(status_code=400, detail="No valid timestamps found")
    if x.dt.tz is None:
        x = x.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    return x.dt.tz_convert("UTC")


def _pair_trades(deals: pd.DataFrame, tz: str) -> pd.DataFrame:
    _require(deals, {"time", "deal", "symbol", "type", "direction", "volume", "price", "commission", "swap", "profit"}, "deals")
    d = deals.copy()
    d["time"] = _parse_time(d["time"], tz)
    for c in ("volume", "price", "commission", "swap", "profit"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["direction"] = d["direction"].astype(str).str.lower().str.strip()
    d["symbol"] = d["symbol"].astype(str).str.strip()
    d["type"] = d["type"].astype(str).str.lower().str.strip()
    d = d.dropna(subset=["time", "symbol", "volume"]).sort_values(["time", "deal"], kind="stable")

    opens: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    rows: list[dict[str, Any]] = []
    for _, r in d.iterrows():
        direction = str(r["direction"])
        symbol = str(r["symbol"])
        volume = float(r["volume"] or 0)
        if volume <= 0:
            continue
        if direction == "in":
            opens[(symbol, str(r["type"]))].append({
                "entry_time": r["time"],
                "entry_type": str(r["type"]),
                "entry_volume": volume,
                "entry_commission": float(r["commission"] or 0),
                "entry_swap": float(r["swap"] or 0),
            })
            continue
        if direction != "out":
            continue
        opposite = "sell" if str(r["type"]) == "buy" else "buy"
        key = (symbol, opposite)
        remaining = volume
        exit_commission = float(r["commission"] or 0)
        exit_swap = float(r["swap"] or 0)
        exit_profit = float(r["profit"] or 0)
        while remaining > 1e-12 and opens[key]:
            o = opens[key][0]
            matched = min(remaining, float(o["entry_volume"]))
            frac_exit = matched / volume
            frac_entry = matched / float(o["entry_volume"])
            pnl = (
                exit_profit * frac_exit
                + float(o["entry_commission"]) * frac_entry
                + float(o["entry_swap"]) * frac_entry
                + exit_commission * frac_exit
                + exit_swap * frac_exit
            )
            rows.append({"entry_time": o["entry_time"], "exit_time": r["time"], "symbol": symbol, "type": o["entry_type"], "volume": matched, "profit": pnl})
            o["entry_volume"] -= matched
            remaining -= matched
            if o["entry_volume"] <= 1e-12:
                opens[key].popleft()
    out = pd.DataFrame(rows)
    if out.empty:
        raise HTTPException(status_code=400, detail="No completed trades reconstructed from deals")
    return out.sort_values("entry_time").reset_index(drop=True)


def _market(market: pd.DataFrame, tz: str, low_cut: float | None = None, high_cut: float | None = None) -> tuple[pd.DataFrame, float, float]:
    _require(market, {"time", "high", "low", "close"}, "market")
    m = market.copy()
    m["time"] = _parse_time(m["time"], tz)
    for c in ("high", "low", "close"):
        m[c] = pd.to_numeric(m[c], errors="coerce")
    m = m.dropna(subset=["time", "high", "low", "close"]).sort_values("time").drop_duplicates("time", keep="last")
    prev = m["close"].shift(1)
    tr = pd.concat([(m["high"] - m["low"]), (m["high"] - prev).abs(), (m["low"] - prev).abs()], axis=1).max(axis=1)
    m["atr_pct"] = tr.rolling(14, min_periods=14).mean() / m["close"].abs()
    valid = m["atr_pct"].dropna()
    if valid.empty:
        raise HTTPException(status_code=400, detail="Unable to calculate ATR(14)/close")
    lc = float(low_cut if low_cut is not None else valid.quantile(1 / 3))
    hc = float(high_cut if high_cut is not None else valid.quantile(2 / 3))
    m["regime"] = m["atr_pct"].map(lambda x: "unknown" if pd.isna(x) else ("low" if x <= lc else ("high" if x >= hc else "normal")))
    return m[["time", "regime"]], lc, hc


def _attach(trades: pd.DataFrame, regimes: pd.DataFrame) -> pd.DataFrame:
    return pd.merge_asof(trades.sort_values("entry_time"), regimes.sort_values("time"), left_on="entry_time", right_on="time", direction="backward").assign(regime=lambda x: x["regime"].fillna("unknown"))


def _summary(values: list[float]) -> dict[str, Any]:
    s = pd.Series(values, dtype=float)
    if s.empty:
        return {"trade_count": 0, "net_profit": 0.0, "profit_factor": None, "win_rate": None, "average_trade": None, "max_drawdown": 0.0}
    gp = float(s[s > 0].sum())
    gl = float(-s[s < 0].sum())
    eq = s.cumsum()
    dd = eq - eq.cummax()
    return {
        "trade_count": int(len(s)),
        "net_profit": float(s.sum()),
        "profit_factor": float(gp / gl) if gl > 0 else None,
        "win_rate": float((s > 0).mean()),
        "average_trade": float(s.mean()),
        "max_drawdown": float(dd.min()),
    }


def _year_metrics(trades: pd.DataFrame) -> dict[str, Any]:
    overall = _summary(trades["profit"].tolist())
    groups = {}
    for regime in ("low", "normal", "high", "unknown"):
        vals = trades.loc[trades["regime"] == regime, "profit"].tolist()
        if vals:
            groups[regime] = _summary(vals)
    return {"overall": overall, "by_entry_volatility": groups}


def _gate(baseline: dict[str, Any], unseen: dict[str, Any], min_trades: int) -> dict[str, Any]:
    y24, y25, y26 = baseline["2024"]["overall"], baseline["2025"]["overall"], unseen["overall"]
    positive = all((x.get("net_profit") is not None and x["net_profit"] > 0 and x.get("profit_factor") is not None and x["profit_factor"] > 1) for x in (y24, y25, y26))
    sufficient = all(int(x.get("trade_count") or 0) >= min_trades for x in (y24, y25, y26))
    return {
        "2024_trade_count": y24["trade_count"], "2025_trade_count": y25["trade_count"], "2026_trade_count": y26["trade_count"],
        "2024_net_profit": y24["net_profit"], "2025_net_profit": y25["net_profit"], "2026_net_profit": y26["net_profit"],
        "2024_profit_factor": y24["profit_factor"], "2025_profit_factor": y25["profit_factor"], "2026_profit_factor": y26["profit_factor"],
        "min_trades_required_each_year": min_trades, "sample_sufficient_all_years": sufficient,
        "positive_net_profit_and_pf_gt_1_all_years": positive,
        "robust_positive_3year": sufficient and positive,
    }


def _group_gate(years: dict[str, dict[str, Any]], min_trades: int) -> list[dict[str, Any]]:
    names = set(years["2024"]["by_entry_volatility"]) | set(years["2025"]["by_entry_volatility"]) | set(years["2026"]["by_entry_volatility"])
    rows = []
    for g in sorted(names):
        r = {str(y): years[str(y)]["by_entry_volatility"].get(g) for y in (2024, 2025, 2026)}
        available = [r[str(y)] for y in (2024, 2025, 2026)]
        sufficient = all(x is not None and x["trade_count"] >= min_trades for x in available)
        positive = sufficient and all(x["net_profit"] > 0 and x.get("profit_factor") is not None and x["profit_factor"] > 1 for x in available)
        rows.append({"group": g, "2024": r["2024"], "2025": r["2025"], "2026": r["2026"], "sample_sufficient_all_years": sufficient, "robust_positive_3year": positive})
    return rows


def _run_gate(experiment_id: str, is_deals: pd.DataFrame, is_market: pd.DataFrame, oos_deals: pd.DataFrame, oos_market: pd.DataFrame, unseen_deals: pd.DataFrame, unseen_market: pd.DataFrame, tz: str, min_trades: int) -> dict[str, Any]:
    t24, t25, t26 = _pair_trades(is_deals, tz), _pair_trades(oos_deals, tz), _pair_trades(unseen_deals, tz)
    # Thresholds are learned only from 2024+2025 baseline market data, then frozen for 2026.
    base_market = pd.concat([is_market, oos_market], ignore_index=True)
    base_regimes, low_cut, high_cut = _market(base_market, tz)
    r24, _, _ = _market(is_market, tz, low_cut, high_cut)
    r25, _, _ = _market(oos_market, tz, low_cut, high_cut)
    r26, _, _ = _market(unseen_market, tz, low_cut, high_cut)
    y24, y25, y26 = _attach(t24, r24), _attach(t25, r25), _attach(t26, r26)
    years = {"2024": _year_metrics(y24), "2025": _year_metrics(y25), "2026": _year_metrics(y26)}
    overall_gate = _gate(years, years["2026"], min_trades) if False else {
        "2024": years["2024"]["overall"], "2025": years["2025"]["overall"], "2026": years["2026"]["overall"],
    }
    yg = _gate({"2024": years["2024"], "2025": years["2025"]}, years["2026"], min_trades)
    groups = _group_gate(years, min_trades)
    robust_groups = [x for x in groups if x["robust_positive_3year"]]
    conclusion = "SUPPORTED_FOR_FURTHER_RESEARCH" if robust_groups else "NOT_ESTABLISHED"
    return {
        "experiment_id": experiment_id,
        "method": "v11.2 three-year robustness gate from supplied MT5 deals and OHLC; 2024 IS, 2025 OOS, 2026 unseen",
        "periods": {"is": "2024", "oos": "2025", "unseen": "2026"},
        "min_trades": min_trades,
        "overall": yg,
        "year_results": years,
        "groups": groups,
        "summary": {"group_count": len(groups), "robust_positive_groups": len(robust_groups), "conclusion": conclusion},
        "thresholds": {"atr_measure": "ATR(14)/close", "low_cut": low_cut, "high_cut": high_cut, "threshold_source": "2024+2025 baseline only"},
        "limitations": [
            "This gate reconstructs realized MT5 trades; it does not rerun the MQL5 EA.",
            "2026 is treated as unseen validation evidence and may be a partial year.",
            "ATR regime thresholds are frozen from 2024+2025 baseline market data; 2026 is not used to set thresholds.",
            "Minimum trade count is a screening rule, not a statistical significance test.",
            "Missing or unmatched deals are excluded from completed-trade reconstruction.",
            "No parameters or sizing policy are optimized or selected by this gate.",
        ],
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


async def robustness_gate_3year_from_inputs(
    experiment_id: str,
    is_deals_file: UploadFile,
    is_market_file: UploadFile,
    oos_deals_file: UploadFile,
    oos_market_file: UploadFile,
    unseen_deals_file: UploadFile,
    unseen_market_file: UploadFile,
    input_timezone: str = "UTC",
    min_trades: int = MIN_TRADES_DEFAULT,
) -> dict[str, Any]:
    db: Session = SessionLocal()
    try:
        exp = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not exp:
            raise HTTPException(status_code=404, detail="Experiment not found")
        analysis = _run_gate(
            experiment_id,
            _read_csv(is_deals_file, "is_deals"), _read_csv(is_market_file, "is_market"),
            _read_csv(oos_deals_file, "oos_deals"), _read_csv(oos_market_file, "oos_market"),
            _read_csv(unseen_deals_file, "deals"), _read_csv(unseen_market_file, "market"),
            input_timezone, min_trades,
        )
        result = ExperimentResult(
            id=str(uuid.uuid4()), experiment_id=exp.id,
            summary="Three-year robustness gate: 2024 IS, 2025 OOS, 2026 unseen validation.",
            metrics=json.dumps(analysis, ensure_ascii=False, default=str),
            evidence=json.dumps({"source_files": ["is_deals.csv", "is_market.csv", "oos_deals.csv", "oos_market.csv", "deals.csv", "market.csv"], "method": analysis["method"]}, ensure_ascii=False),
            limitations=json.dumps(analysis["limitations"], ensure_ascii=False),
            conclusion=analysis["summary"]["conclusion"],
        )
        db.add(result); exp.status = "completed"; exp.completed_at = datetime.utcnow(); db.commit(); db.refresh(result)
        return {"experiment_id": exp.id, "result_id": result.id, "status": "completed", "analysis": analysis}
    finally:
        db.close()


@router.post("/{experiment_id}/robustness-gate-3year-from-inputs")
async def robustness_gate_3year_from_inputs_endpoint(
    experiment_id: str,
    is_deals: UploadFile = File(...),
    is_market: UploadFile = File(...),
    oos_deals: UploadFile = File(...),
    oos_market: UploadFile = File(...),
    deals: UploadFile = File(...),
    market: UploadFile = File(...),
    input_timezone: str = Query("UTC"),
    min_trades: int = Query(MIN_TRADES_DEFAULT, ge=1, le=1000),
):
    return await robustness_gate_3year_from_inputs(experiment_id, is_deals, is_market, oos_deals, oos_market, deals, market, input_timezone, min_trades)
