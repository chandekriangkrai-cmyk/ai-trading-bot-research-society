from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.backtest_importer import analyze_mt5_trades
from app.database import get_db
from app.models import Mission, ResearchTask
from app.research_models import Experiment, ExperimentResult


router = APIRouter(prefix="/research", tags=["Research Engine"])


@router.post("/experiments/{experiment_id}/import-mt5-trades")
async def import_mt5_trade_results(
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

    if not trades_file.filename or not trades_file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=400,
            detail="trades_file must be a CSV file",
        )

    trade_bytes = await trades_file.read()
    market_bytes = None

    if market_file is not None:
        if (
            market_file.filename
            and not market_file.filename.lower().endswith(".csv")
        ):
            raise HTTPException(
                status_code=400,
                detail="market_file must be a CSV file",
            )
        market_bytes = await market_file.read()

    try:
        analysis = analyze_mt5_trades(
            trade_csv=trade_bytes,
            ohlc_csv=market_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = ExperimentResult(
        id=__import__("uuid").uuid4().hex,
        experiment_id=experiment.id,
        summary=(
            f"Imported {analysis['overall']['trade_count']} trade results "
            f"from MT5/export CSV."
        ),
        metrics=json.dumps(
            analysis,
            ensure_ascii=False,
            default=str,
        ),
        evidence=json.dumps(
            {
                "trades_filename": trades_file.filename,
                "market_filename": (
                    market_file.filename if market_file else None
                ),
                "source_type": "user_supplied_mt5_or_export_csv",
            },
            ensure_ascii=False,
        ),
        limitations=json.dumps(
            analysis.get("limitations", []),
            ensure_ascii=False,
        ),
        conclusion=(
            "Deterministic metrics were calculated from the supplied "
            "trade results. This result is an evidence record, not an "
            "independent execution of the MQL5 EA."
        ),
    )

    db.add(result)
    experiment.status = "completed"
    experiment.completed_at = __import__("datetime").datetime.utcnow()
    db.commit()
    db.refresh(result)

    return {
        "experiment_id": experiment.id,
        "result_id": result.id,
        "status": experiment.status,
        "analysis": analysis,
    }
