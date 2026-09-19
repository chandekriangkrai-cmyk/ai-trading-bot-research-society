from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Mission, ResearchTask
from app.ea_files import EAFile
from app.schemas import MissionCreate, MissionResponse


router = APIRouter(
    prefix="/missions",
    tags=["Missions"],
)


@router.get(
    "",
    response_model=list[MissionResponse],
)
def list_missions(
    db: Session = Depends(get_db),
) -> list[Mission]:
    statement = select(Mission).order_by(Mission.created_at.desc())

    return list(db.scalars(statement).all())


@router.get(
    "/{mission_id}",
    response_model=MissionResponse,
)
def get_mission(
    mission_id: str,
    db: Session = Depends(get_db),
) -> Mission:
    mission = db.get(Mission, mission_id)

    if mission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Mission not found.",
        )

    return mission


@router.post(
    "",
    response_model=MissionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_mission(
    payload: MissionCreate,
    db: Session = Depends(get_db),
) -> Mission:
    mission = Mission(
        title=payload.title,
        description=payload.description,
        market=payload.market,
        timeframe=payload.timeframe,
    )

    db.add(mission)
    db.commit()
    db.refresh(mission)

    return mission


@router.delete(
    "/{mission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_mission(
    mission_id: str,
    db: Session = Depends(get_db),
) -> None:
    mission = db.get(Mission, mission_id)

    if mission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Mission not found.",
        )

    try:
        # -----------------------------------------
        # 1. ลบ Research Tasks ของ Mission ก่อน
        # -----------------------------------------
        db.query(ResearchTask).filter(
            ResearchTask.mission_id == mission_id
        ).delete(
            synchronize_session=False
        )

        # -----------------------------------------
        # 2. ลบ EA Files ของ Mission
        # -----------------------------------------
        db.query(EAFile).filter(
            EAFile.mission_id == mission_id
        ).delete(
            synchronize_session=False
        )

        # -----------------------------------------
        # 3. ลบ Mission
        # -----------------------------------------
        db.delete(mission)

        # -----------------------------------------
        # 4. Commit ทุกอย่างพร้อมกัน
        # -----------------------------------------
        db.commit()

    except Exception:
        db.rollback()
        raise
