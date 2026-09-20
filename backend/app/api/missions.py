from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Mission, ResearchTask
from app.research_models import Experiment, ExperimentResult, Hypothesis
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
        # 1. Delete experiment results first because they reference experiments.
        experiment_ids = [
            experiment_id
            for experiment_id in db.scalars(
                select(Experiment.id).where(
                    Experiment.mission_id == mission_id
                )
            ).all()
        ]

        if experiment_ids:
            db.query(ExperimentResult).filter(
                ExperimentResult.experiment_id.in_(experiment_ids)
            ).delete(synchronize_session=False)

        # 2. Delete experiments belonging to this mission.
        db.query(Experiment).filter(
            Experiment.mission_id == mission_id
        ).delete(synchronize_session=False)

        # 3. Delete hypotheses belonging to this mission.
        # Research leads are kept because they are independent research sources.
        db.query(Hypothesis).filter(
            Hypothesis.mission_id == mission_id
        ).delete(synchronize_session=False)

        # 4. Delete research tasks.
        db.query(ResearchTask).filter(
            ResearchTask.mission_id == mission_id
        ).delete(synchronize_session=False)

        # 5. Delete EA files after experiments, because experiments may reference them.
        db.query(EAFile).filter(
            EAFile.mission_id == mission_id
        ).delete(synchronize_session=False)

        # 6. Delete the mission.
        db.delete(mission)
        db.commit()

    except Exception:
        db.rollback()
        raise
