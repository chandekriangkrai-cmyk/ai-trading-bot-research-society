from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.mt5_deals_importer import analyze_mt5_deals
from app.research_models import Experiment, ExperimentResult

router = APIRouter(prefix="/research", tags=["Research Engine"])


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    profits = [float(t["profit"]) for t in trades]
    trade_count = len(profits)
    if not profits:
        return {
            "trade_count": 0,
            "net_profit": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "expectancy": 0.0,
            "win_rate": 0.0,
            "max_drawdown_absolute": 0.0,
        }

    gross_profit = sum(p for p in profits if p > 0)
    gross_loss = sum(p for p in profits if p < 0)
    net_profit = sum(profits)
    profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
    expectancy = net_profit / trade_count
    win_rate = sum(1 for p in profits if p > 0) / trade_count

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in profits:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "trade_count": trade_count,
        "net_profit": net_profit,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "win_rate": win_rate,
        "max_drawdown_absolute": max_dd,
    }


def _split(trades: list[dict[str, Any]]) -> tuple[int | None, dict[str, list[dict[str, Any]]]]:
    # Use the year represented by the supplied dataset. The auto pipeline now
    # also processes unseen-year datasets such as 2026, not only the original
    # 2025 fixture.
    years = [datetime.fromisoformat(t["time"]).year for t in trades]
    target_year = max(set(years), key=years.count) if years else None
    is_trades = []
    oos_trades = []

    for t in trades:
        dt = datetime.fromisoformat(t["time"])
        if target_year is None or dt.year != target_year:
            continue
        if dt.month <= 6:
            is_trades.append(t)
        else:
            oos_trades.append(t)

    return target_year, {"is": is_trades, "oos": oos_trades}


@router.post("/experiments/{experiment_id}/import-mt5-deals-is-oos")
async def import_mt5_deals_is_oos(
    experiment_id: str,
    deals_file: UploadFile = File(...),
    market_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    if not deals_file.filename or not deals_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="deals_file must be CSV")

    if market_file is not None and market_file.filename and not market_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="market_file must be CSV")

    deal_bytes = await deals_file.read()
    market_bytes = await market_file.read() if market_file is not None else None

    try:
        analysis = analyze_mt5_deals(
            deal_csv=deal_bytes,
            ohlc_csv=market_bytes,
            symbol=experiment.symbol,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    closed = analysis.get("closed_trades", [])
    if not closed:
        raise HTTPException(
            status_code=400,
            detail="No timestamped reconstructed closed trades available",
        )

    # Recompute deterministic ATR regime for each trade using the same source
    # data through the v5 analyzer's existing aggregate labels is not enough
    # for period/regime intersection, so derive the regime by invoking the
    # market-data layer directly.
    if market_bytes:
        from app.market_data_engine import add_volatility_regimes, parse_ohlc_csv, regime_for_time
        bars = add_volatility_regimes(parse_ohlc_csv(market_bytes))
        for t in closed:
            dt = datetime.fromisoformat(t["time"])
            t["regime"] = regime_for_time(bars, dt)
    else:
        for t in closed:
            t["regime"] = "unknown"

    target_year, periods = _split(closed)
    period_analysis: dict[str, Any] = {}

    for period_name, period_trades in periods.items():
        period_analysis[period_name] = {
            "period": (
                f"{target_year}-01-01 through {target_year}-06-30"
                if period_name == "is"
                else f"{target_year}-07-01 through {target_year}-12-31"
            ),
            "overall": _metrics(period_trades),
            "by_regime": {
                regime: _metrics([t for t in period_trades if t["regime"] == regime])
                for regime in ("high", "normal", "low")
            },
        }

    result_analysis = {
        "experiment_scope": {
            "symbol": experiment.symbol,
            "timeframe": experiment.timeframe,
            "in_sample": f"{target_year}-01-01 through {target_year}-06-30",
            "out_of_sample": f"{target_year}-07-01 through {target_year}-12-31",
        },
        "periods": period_analysis,
        "accounting": analysis["accounting"],
        "data_source": analysis["data_source"],
        "method": {
            "accounting": "entry profit + exit profit + entry commission + exit commission + entry swap + exit swap",
            "regime": "ATR(14) rolling percentile: bottom third=low, middle third=normal, top third=high",
            "split": "closed-trade close timestamp within the dataset year: Jan-Jun IS, Jul-Dec OOS",
        },
        "limitations": analysis.get("limitations", []) + [
            "This is a deterministic historical split, not a proof of future performance.",
            "The volatility regime is externally defined by ATR(14), not the EA's internal logic.",
            "FIFO by symbol is used because the MT5 export lacks position ID.",
        ],
    }

    result = ExperimentResult(
        id=str(uuid.uuid4()),
        experiment_id=experiment.id,
        summary="Accounting-aware EURUSD M30 2025 H1/H2 IS/OOS analysis.",
        metrics=json.dumps(result_analysis, ensure_ascii=False, default=str),
        evidence=json.dumps(
            {
                "deals_filename": deals_file.filename,
                "market_filename": market_file.filename if market_file else None,
                "source_type": "user_supplied_mt5_deals_csv",
                "accounting_method": analysis["accounting"]["method"],
                "matching_method": analysis["accounting"]["matching_method"],
                "is_period": f"{target_year}-01-01 through {target_year}-06-30",
                "oos_period": f"{target_year}-07-01 through {target_year}-12-31",
            },
            ensure_ascii=False,
        ),
        limitations=json.dumps(result_analysis["limitations"], ensure_ascii=False),
        conclusion=(
            "Deterministic accounting-aware IS/OOS metrics were reconstructed "
            "from supplied MT5 Deals; no EA parameters were changed using OOS data."
        ),
    )

    db.add(result)
    experiment.status = "completed"
    experiment.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(result)

    return {
        "experiment_id": experiment.id,
        "result_id": result.id,
        "status": experiment.status,
        "analysis": result_analysis,
    }
