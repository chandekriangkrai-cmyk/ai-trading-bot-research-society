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


DEFAULT_AGENTS = [
    {
        "name": "Research Explorer",
        "role": "Research and market knowledge discovery",
        "description": (
            "ค้นคว้างานวิจัย แนวคิดการเทรด ทฤษฎีตลาด "
            "และข้อมูลที่เกี่ยวข้องกับภารกิจวิจัย โดยแยกข้อเท็จจริง "
            "สมมติฐาน และแหล่งข้อมูลอย่างชัดเจน"
        ),
    },
    {
        "name": "Strategy Analyst",
        "role": "Trading strategy design and analysis",
        "description": (
            "วิเคราะห์และออกแบบกลยุทธ์การเทรด ระบุเงื่อนไขเข้าออก "
            "ตัวกรอง แนวคิดเบื้องหลัง และข้อจำกัดของกลยุทธ์ "
            "โดยไม่สรุปว่ากลยุทธ์ทำกำไรจนกว่าจะมีผลทดสอบรองรับ"
        ),
    },
    {
        "name": "Risk Analyst",
        "role": "Risk management and drawdown analysis",
        "description": (
            "วิเคราะห์ความเสี่ยง การขาดทุนต่อเนื่อง Drawdown "
            "Position sizing ความผันผวน และสถานการณ์ที่อาจทำให้ระบบ "
            "เสียหาย พร้อมเสนอแนวทางควบคุมความเสี่ยง"
        ),
    },
    {
        "name": "Backtest Specialist",
        "role": "Backtesting methodology and validation",
        "description": (
            "ออกแบบแผนการทดสอบย้อนหลัง ตรวจสอบคุณภาพข้อมูล "
            "การแบ่งช่วง In-sample และ Out-of-sample "
            "ค่าธรรมเนียม Slippage และความเสี่ยงจาก Overfitting"
        ),
    },
    {
        "name": "Critic Agent",
        "role": "Critical review and research quality control",
        "description": (
            "ตรวจสอบจุดอ่อน ความไม่สอดคล้อง สมมติฐานที่ยังไม่มีหลักฐาน "
            "อคติในการวิเคราะห์ และข้อสรุปที่เกินกว่าข้อมูล "
            "พร้อมตั้งคำถามเพื่อยกระดับคุณภาพงานวิจัย"
        ),
    },
]


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


@router.post(
    "/seed-defaults",
    response_model=list[AgentResponse],
    status_code=status.HTTP_201_CREATED,
)
def seed_default_agents(
    db: Session = Depends(get_db),
) -> list[Agent]:
    created_agents: list[Agent] = []

    for agent_data in DEFAULT_AGENTS:
        existing = db.scalar(
            select(Agent).where(Agent.name == agent_data["name"])
        )

        if existing:
            continue

        agent = Agent(**agent_data)
        db.add(agent)
        created_agents.append(agent)

    if created_agents:
        db.commit()

        for agent in created_agents:
            db.refresh(agent)

    return created_agents
