from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Agent
from app.schemas import AgentCreate, AgentResponse


router = APIRouter(
    prefix="/agents",
    tags=["Agents"],
)


@router.get(
    "",
    response_model=list[AgentResponse],
)
def list_agents(
    db: Session = Depends(get_db),
) -> list[Agent]:
    statement = select(Agent).order_by(Agent.created_at.desc())

    return list(db.scalars(statement).all())


@router.post(
    "",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_agent(
    payload: AgentCreate,
    db: Session = Depends(get_db),
) -> Agent:
    existing = db.scalar(
        select(Agent).where(Agent.name == payload.name)
    )

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An agent with this name already exists.",
        )

    agent = Agent(
        name=payload.name,
        role=payload.role,
        description=payload.description,
    )

    db.add(agent)
    db.commit()
    db.refresh(agent)

    return agent
