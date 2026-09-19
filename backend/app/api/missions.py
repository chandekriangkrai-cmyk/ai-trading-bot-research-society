from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Mission
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
