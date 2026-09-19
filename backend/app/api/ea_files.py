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
    EAAnalysisResponse,
)

router = APIRouter(
    prefix="/ea-files",
    tags=["EA Files"],
)


def analyze_mq5_code(source_code: str) -> dict:
    """
    ตรวจหาองค์ประกอบสำคัญในโค้ด MQL5
    """

    def contains(*terms: str) -> bool:
        return any(term.lower() in source_code.lower() for term in terms)

    def extract_number(name: str):
        pattern = rf"\b{name}\s*=\s*([0-9]+(?:\.[0-9]+)?)"
        match = re.search(pattern, source_code)

        if match:
            value = match.group(1)
            return float(value) if "." in value else int(value)

        return None

    return {
        "has_trade_library": contains(
            "Trade\\Trade.mqh",
            "Trade/Trade.mqh"
        ),
        "has_buy_logic": contains(".Buy(", "trade.Buy"),
        "has_sell_logic": contains(".Sell(", "trade.Sell"),
        "has_atr_filter": contains("ATR", "iATR", "GetATR"),
        "has_breakout_logic": contains(
            "Breakout",
            "IsValidBreakoutCandle"
        ),
        "has_support_resistance": contains(
            "Support",
            "Resistance",
            "GetSupportResistanceRange"
        ),
        "has_risk_guard": contains(
            "RiskGuard",
            "MaxDailyLoss",
            "MaxTotalLoss"
        ),
        "has_news_guard": contains(
            "NewsGuard",
            "IsNewsTime"
        ),
        "has_friday_filter": contains(
            "Friday",
            "CheckFridayClose"
        ),
        "has_rollover_filter": contains(
            "Rollover",
            "IsRolloverTime"
        ),
        "has_lot_calculation": contains(
            "CalculateLotSize",
            "NormalizeVolume"
        ),
        "parameters": {
            "RiskPercent": extract_number("RiskPercent"),
            "SRLookbackBars": extract_number("SRLookbackBars"),
            "SLRangePercent": extract_number("SLRangePercent"),
            "TargetRR": extract_number("TargetRR"),
            "MaxSpreadPoints": extract_number("MaxSpreadPoints"),
            "MinBreakoutBodyPct": extract_number(
                "MinBreakoutBodyPct"
            ),
            "ATRPeriod": extract_number("ATRPeriod"),
            "MinCandleATR": extract_number("MinCandleATR"),
            "MaxCandleATR": extract_number("MaxCandleATR"),
            "MaxDailyLossPct": extract_number(
                "MaxDailyLossPct"
            ),
            "MaxTotalLossPct": extract_number(
                "MaxTotalLossPct"
            ),
        },
    }


@router.post(
    "",
    response_model=EAFileResponse,
    summary="บันทึกไฟล์ EA จาก Source Code",
)
def create_ea_file(
    payload: EAFileCreate,
    db: Session = Depends(get_db),
):
    mission = db.query(Mission).filter(
        Mission.id == payload.mission_id
    ).first()

    if not mission:
        raise HTTPException(
            status_code=404,
            detail="Mission not found",
        )

    if not payload.filename.lower().endswith(".mq5"):
        raise HTTPException(
            status_code=400,
            detail="รองรับเฉพาะไฟล์ .mq5",
        )

    ea_file = EAFile(
        id=str(uuid.uuid4()),
        mission_id=payload.mission_id,
        filename=payload.filename,
        file_type="mq5",
        source_code=payload.source_code,
        line_count=len(payload.source_code.splitlines()),
        analysis_status="uploaded",
    )

    db.add(ea_file)
    db.commit()
    db.refresh(ea_file)

    return ea_file


@router.get(
    "",
    response_model=list[EAFileResponse],
    summary="แสดงรายการไฟล์ EA ทั้งหมด",
)
def list_ea_files(
    db: Session = Depends(get_db),
):
    return db.query(EAFile).order_by(
        EAFile.created_at.desc()
    ).all()


@router.get(
    "/{ea_file_id}/analyze",
    response_model=EAAnalysisResponse,
    summary="วิเคราะห์โครงสร้างไฟล์ EA",
)
def analyze_ea_file(
    ea_file_id: str,
    db: Session = Depends(get_db),
):
    ea_file = db.query(EAFile).filter(
        EAFile.id == ea_file_id
    ).first()

    if not ea_file:
        raise HTTPException(
            status_code=404,
            detail="EA file not found",
        )

    detected_features = analyze_mq5_code(
        ea_file.source_code
    )

    ea_file.analysis_status = "analyzed"

    db.commit()
    db.refresh(ea_file)

    return {
        "ea_file_id": ea_file.id,
        "filename": ea_file.filename,
        "line_count": ea_file.line_count,
        "detected_features": detected_features,
        "analysis_status": ea_file.analysis_status,
    }
