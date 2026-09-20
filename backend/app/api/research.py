from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.ea_files import EAFile
from app.models import Mission
from app.research_models import Experiment, ExperimentResult, Hypothesis, ResearchLead


router = APIRouter(prefix="/research", tags=["Research Engine"])


class ResearchLeadCreate(BaseModel):
    source: str = Field(default="manual", min_length=1, max_length=50)
    external_id: str | None = None
    url: str | None = None
    title: str = Field(min_length=1, max_length=500)
    author: str | None = None
    content: str = ""
    market: str | None = None
    timeframe: str | None = None


class ResearchLeadResponse(ResearchLeadCreate):
    id: str
    status: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class HypothesisCreate(BaseModel):
    research_lead_id: str | None = None
    mission_id: str | None = None
    title: str = Field(min_length=1, max_length=500)
    statement: str = Field(min_length=1)
    assumptions: str = ""
    status: str = "proposed"


class HypothesisResponse(HypothesisCreate):
    id: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ExperimentCreate(BaseModel):
    hypothesis_id: str
    mission_id: str | None = None
    ea_file_id: str | None = None
    symbol: str = Field(min_length=1, max_length=100)
    timeframe: str = Field(min_length=1, max_length=50)
    experiment_type: str = "backtest"
    specification: str = Field(min_length=1)
    baseline: str = ""
    status: str = "planned"


class ExperimentResponse(ExperimentCreate):
    id: str
    created_at: datetime
    completed_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)


class ExperimentResultCreate(BaseModel):
    experiment_id: str
    summary: str = Field(min_length=1)
    metrics: str = ""
    evidence: str = ""
    limitations: str = ""
    conclusion: str = ""


class ExperimentResultResponse(ExperimentResultCreate):
    id: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ExperimentDetailResponse(BaseModel):
    experiment: ExperimentResponse
    hypothesis: HypothesisResponse
    result: ExperimentResultResponse | None = None


@router.post("/leads", response_model=ResearchLeadResponse, status_code=status.HTTP_201_CREATED)
def create_research_lead(payload: ResearchLeadCreate, db: Session = Depends(get_db)):
    lead = ResearchLead(**payload.model_dump())
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


@router.get("/leads", response_model=list[ResearchLeadResponse])
def list_research_leads(
    lead_status: str | None = None,
    source: str | None = None,
    db: Session = Depends(get_db),
):
    statement = select(ResearchLead).order_by(ResearchLead.created_at.desc())
    if lead_status:
        statement = statement.where(ResearchLead.status == lead_status)
    if source:
        statement = statement.where(ResearchLead.source == source)
    return list(db.scalars(statement).all())


@router.get("/leads/{lead_id}", response_model=ResearchLeadResponse)
def get_research_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(ResearchLead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Research lead not found.")
    return lead


@router.post("/hypotheses", response_model=HypothesisResponse, status_code=status.HTTP_201_CREATED)
def create_hypothesis(payload: HypothesisCreate, db: Session = Depends(get_db)):
    if payload.research_lead_id and not db.get(ResearchLead, payload.research_lead_id):
        raise HTTPException(status_code=404, detail="Research lead not found.")
    if payload.mission_id and not db.get(Mission, payload.mission_id):
        raise HTTPException(status_code=404, detail="Mission not found.")
    hypothesis = Hypothesis(**payload.model_dump())
    db.add(hypothesis)
    db.commit()
    db.refresh(hypothesis)
    return hypothesis


@router.get("/hypotheses", response_model=list[HypothesisResponse])
def list_hypotheses(
    mission_id: str | None = None,
    research_lead_id: str | None = None,
    hypothesis_status: str | None = None,
    db: Session = Depends(get_db),
):
    statement = select(Hypothesis).order_by(Hypothesis.created_at.desc())
    if mission_id:
        statement = statement.where(Hypothesis.mission_id == mission_id)
    if research_lead_id:
        statement = statement.where(Hypothesis.research_lead_id == research_lead_id)
    if hypothesis_status:
        statement = statement.where(Hypothesis.status == hypothesis_status)
    return list(db.scalars(statement).all())


@router.get("/hypotheses/{hypothesis_id}", response_model=HypothesisResponse)
def get_hypothesis(hypothesis_id: str, db: Session = Depends(get_db)):
    hypothesis = db.get(Hypothesis, hypothesis_id)
    if not hypothesis:
        raise HTTPException(status_code=404, detail="Hypothesis not found.")
    return hypothesis


@router.post("/experiments", response_model=ExperimentResponse, status_code=status.HTTP_201_CREATED)
def create_experiment(payload: ExperimentCreate, db: Session = Depends(get_db)):
    if not db.get(Hypothesis, payload.hypothesis_id):
        raise HTTPException(status_code=404, detail="Hypothesis not found.")
    if payload.mission_id and not db.get(Mission, payload.mission_id):
        raise HTTPException(status_code=404, detail="Mission not found.")
    if payload.ea_file_id and not db.get(EAFile, payload.ea_file_id):
        raise HTTPException(status_code=404, detail="EA file not found.")
    experiment = Experiment(**payload.model_dump())
    db.add(experiment)
    db.commit()
    db.refresh(experiment)
    return experiment


@router.get("/experiments", response_model=list[ExperimentResponse])
def list_experiments(
    mission_id: str | None = None,
    hypothesis_id: str | None = None,
    experiment_status: str | None = None,
    db: Session = Depends(get_db),
):
    statement = select(Experiment).order_by(Experiment.created_at.desc())
    if mission_id:
        statement = statement.where(Experiment.mission_id == mission_id)
    if hypothesis_id:
        statement = statement.where(Experiment.hypothesis_id == hypothesis_id)
    if experiment_status:
        statement = statement.where(Experiment.status == experiment_status)
    return list(db.scalars(statement).all())


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetailResponse)
def get_experiment(experiment_id: str, db: Session = Depends(get_db)):
    experiment = db.get(Experiment, experiment_id)
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    hypothesis = db.get(Hypothesis, experiment.hypothesis_id)
    result = db.scalar(
        select(ExperimentResult)
        .where(ExperimentResult.experiment_id == experiment_id)
        .order_by(ExperimentResult.created_at.desc())
    )
    return {"experiment": experiment, "hypothesis": hypothesis, "result": result}


@router.post(
    "/experiment-results",
    response_model=ExperimentResultResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_experiment_result(payload: ExperimentResultCreate, db: Session = Depends(get_db)):
    experiment = db.get(Experiment, payload.experiment_id)
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found.")

    result = ExperimentResult(**payload.model_dump())
    experiment.status = "completed"
    experiment.completed_at = datetime.now(timezone.utc)

    db.add(result)
    db.commit()
    db.refresh(result)
    return result
