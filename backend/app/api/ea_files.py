import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Mission
from app.ea_files import EAFile
from app.schemas import (
    EAFileCreate,
    EAFileResponse,
    EAAnalysisResponse
)


router = APIRouter(
    prefix="/ea-files",
    tags=["EA Files"]
)


def analyze_mq5_code(source_code: str) -> dict:
    """
    วิเคราะห์โค้ด MQL5 จากเนื้อหาจริง
    ไม่รัน EA และไม่ส่งคำสั่งซื้อขาย
    """

    def has(pattern: str) -> bool:
        return re.search(
            pattern,
            source_code,
            flags=re.IGNORECASE
        ) is not None

    def extract_input(name: str):
        pattern = (
            r"\binput\s+[^;]*\b"
            + re.escape(name)
            + r"\s*=\s*([^;]+)"
        )

        match = re.search(
            pattern,
            source_code,
            flags=re.IGNORECASE
        )

        if match:
            return match.group(1).strip()

        return None

    features = {
        "uses_trade_class": has(r"#include\s*[<\"]Trade\\Trade\.mqh[>\"]"),
        "has_buy_order": has(r"\.Buy\s*\("),
        "has_sell_order": has(r"\.Sell\s*\("),
        "has_atr": has(r"\bATR\b|iATR|GetATR"),
        "has_support_resistance": has(
            r"Support|Resistance|GetSupportResistanceRange"
        ),
        "has_breakout_filter": has(
            r"Breakout|IsValidBreakoutCandle"
        ),
        "has_risk_guard": has(
            r"RiskGuard|MaxDailyLoss|MaxTotalLoss"
        ),
        "has_news_filter": has(
            r"NewsGuard|IsNewsTime"
        ),
        "has_friday_filter": has(
            r"Friday|CheckFridayClose"
        ),
        "has_rollover_filter": has(
            r"Rollover|IsRolloverTime"
        ),
        "has_lot_calculation": has(
            r"CalculateLotSize|NormalizeVolume"
        ),
        "inputs": {
            "RiskPercent": extract_input("RiskPercent"),
            "SRLookbackBars": extract_input("SRLookbackBars"),
            "SLRangePercent": extract_input("SLRangePercent"),
            "TargetRR": extract_input("TargetRR"),
            "MaxSpreadPoints": extract_input("MaxSpreadPoints"),
            "MinBreakoutBodyPct": extract_input(
                "MinBreakoutBodyPct"
            ),
            "ATRPeriod": extract_input("ATRPeriod"),
            "MinCandleATR": extract_input("MinCandleATR"),
            "MaxCandleATR": extract_input("MaxCandleATR"),
            "MaxDailyLossPct": extract_input(
                "MaxDailyLossPct"
            ),
            "MaxTotalLossPct": extract_input(
                "MaxTotalLossPct"
            )
        }
    }

    return features


@router.post(
    "",
    response_model=EAFileResponse
)
def upload_ea_file(
    payload: EAFileCreate,
    db: Session = Depends(get_db)
):
    mission = db.query(Mission).filter(
        Mission.id == payload.mission_id
    ).first()

    if not mission:
        raise HTTPException(
            status_code=404,
            detail="Mission not found"
        )

    if not payload.filename.lower().endswith(".mq5"):
        raise HTTPException(
            status_code=400,
            detail="Only .mq5 files are supported"
        )

    if not payload.source_code.strip():
        raise HTTPException(
            status_code=400,
            detail="Source code cannot be empty"
        )

    ea_file = EAFile(
        id=str(uuid.uuid4()),
        mission_id=payload.mission_id,
        filename=payload.filename,
        file_type="mq5",
        source_code=payload.source_code,
        line_count=len(payload.source_code.splitlines()),
        analysis_status="uploaded"
    )

    db.add(ea_file)
    db.commit()
    db.refresh(ea_file)

    return ea_file


@router.get(
    "",
    response_model=list[EAFileResponse]
)
def list_ea_files(
    db: Session = Depends(get_db)
):
    return db.query(EAFile).order_by(
        EAFile.created_at.desc()
    ).all()


@router.get(
    "/{ea_file_id}/analyze",
    response_model=EAAnalysisResponse
)
def analyze_ea_file(
    ea_file_id: str,
    db: Session = Depends(get_db)
):
    ea_file = db.query(EAFile).filter(
        EAFile.id == ea_file_id
    ).first()

    if not ea_file:
        raise HTTPException(
            status_code=404,
            detail="EA file not found"
        )

    detected_features = analyze_mq5_code(
        ea_file.source_code
    )

    ea_file.analysis_status = "analyzed"

    db.commit()

    return {
        "ea_file_id": ea_file.id,
        "filename": ea_file.filename,
        "line_count": ea_file.line_count,
        "detected_features": detected_features,
        "analysis_status": ea_file.analysis_status
  }
