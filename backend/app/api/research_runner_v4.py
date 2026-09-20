from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.backtest_importer import analyze_mt5_trades
from app.database import get_db
from app.major_fx import is_major_fx, normalize_symbol
from app.research_models import Experiment, ExperimentResult


router = APIRouter(prefix="/research", tags=["Research Engine"])


@router.get("/major-fx/symbols")
def major_fx_symbols():
    return {
        "symbols": [
            "EURUSD",
            "GBPUSD",
            "USDJPY",
            "USDCHF",
            "AUDUSD",
            "USDCAD",
            "NZDUSD",
        ],
        "timeframe": "M30",
        "ea_scope": "M30Tradedabreak.mq5",
    }


@router.post("/experiments/{experiment_id}/import-major-fx")
async def import_major_fx_trade_results(
    experiment_id: str,
    trades_file: UploadFile = File(...),
    market_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    experiment = (
        db.query(Experiment)
        .filter(Experiment.id == experiment_id)
        .first()
    )
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    symbol = normalize_symbol(experiment.symbol)
    if not is_major_fx(symbol):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Experiment symbol '{experiment.symbol}' is outside the "
                "Major FX scope. Supported: EURUSD, GBPUSD, USDJPY, "
                "USDCHF, AUDUSD, USDCAD, NZDUSD."
            ),
        )

    if experiment.timeframe.upper() != "M30":
        raise HTTPException(
            status_code=400,
            detail="Major FX baseline currently requires timeframe M30.",
        )

    if not trades_file.filename or not trades_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="trades_file must be CSV")

    trade_bytes = await trades_file.read()
    market_bytes = None

    if market_file is not None:
        if (
            market_file.filename
            and not market_file.filename.lower().endswith(".csv")
        ):
            raise HTTPException(status_code=400, detail="market_file must be CSV")
        market_bytes = await market_file.read()

    try:
        analysis = analyze_mt5_trades(
            trade_csv=trade_bytes,
            ohlc_csv=market_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    analysis["experiment_scope"] = {
        "symbol": symbol,
        "timeframe": "M30",
        "ea": "M30Tradedabreak.mq5",
        "asset_class": "major_fx",
    }

    result = ExperimentResult(
        id=str(uuid.uuid4()),
        experiment_id=experiment.id,
        summary=(
            f"Major FX import for {symbol} M30: "
            f"{analysis['overall']['trade_count']} trades."
        ),
        metrics=json.dumps(analysis, ensure_ascii=False, default=str),
        evidence=json.dumps(
            {
                "trades_filename": trades_file.filename,
                "market_filename": market_file.filename if market_file else None,
                "source_type": "user_supplied_mt5_or_export_csv",
                "symbol": symbol,
                "timeframe": "M30",
            },
            ensure_ascii=False,
        ),
        limitations=json.dumps(
            analysis.get("limitations", []),
            ensure_ascii=False,
        ),
        conclusion=(
            "Deterministic metrics were calculated from supplied MT5/export "
            "data. This record does not independently execute the MQL5 EA."
        ),
    )

    db.add(result)
    experiment.status = "completed"
    experiment.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(result)

    return {
        "experiment_id": experiment.id,
        "symbol": symbol,
        "timeframe": "M30",
        "result_id": result.id,
        "status": experiment.status,
        "analysis": analysis,
    }
