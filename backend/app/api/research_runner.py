import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.research_models import Experiment, ExperimentResult
from app.backtest_runner import analyze_trades


router = APIRouter(prefix="/research", tags=["Research Engine"])


@router.post("/experiments/{experiment_id}/import-trades")
async def import_trade_results(
    experiment_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    experiment = db.get(Experiment, experiment_id)

    if not experiment:
        raise HTTPException(
            status_code=404,
            detail="Experiment not found.",
        )

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=400,
            detail="Please upload a CSV trade-results file.",
        )

    content = await file.read()

    try:
        analysis = analyze_trades(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    result = ExperimentResult(
        experiment_id=experiment_id,
        summary=(
            f"Imported {analysis['overall']['trade_count']} trades "
            f"for {experiment.symbol} {experiment.timeframe}."
        ),
        metrics=json.dumps(
            {
                "overall": analysis["overall"],
                "by_regime": analysis["by_regime"],
            },
            ensure_ascii=False,
        ),
        evidence=(
            f"Source file: {file.filename}. "
            "Metrics were calculated from the supplied trade CSV."
        ),
        limitations=json.dumps(
            analysis["limitations"],
            ensure_ascii=False,
        ),
        conclusion=(
            "Result imported and calculated. "
            "This is not an independent verification of the EA execution."
        ),
    )

    experiment.status = "completed"

    db.add(result)
    db.commit()
    db.refresh(result)

    return {
        "experiment_id": experiment_id,
        "result_id": result.id,
        "status": experiment.status,
        "analysis": analysis,
    }
