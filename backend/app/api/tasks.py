from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Agent, Mission, ResearchTask
from app.schemas import (
    ResearchTaskCreate,
    ResearchTaskResponse,
    ResearchTaskUpdate,
)


router = APIRouter(
    prefix="/tasks",
    tags=["Research Tasks"],
)


# --------------------------------------------------
# Task templates for the five research agents
# --------------------------------------------------

TASK_TEMPLATES = [
    {
        "agent_name": "Research Explorer",
        "title": "ศึกษางานวิจัยและแนวคิดเกี่ยวกับ Breakout Trading",
        "instructions": (
            "ศึกษาหลักการ Breakout Trading ที่เกี่ยวข้องกับภารกิจนี้ "
            "โดยเน้นแนวคิดเรื่องแนวรับ แนวต้าน การทะลุกรอบราคา "
            "การยืนยัน Breakout และปัจจัยที่ทำให้เกิด False Breakout "
            "ให้สรุปเป็นหลักการที่สามารถนำไปตรวจสอบกับ EA "
            "M30Tradedabreak.mq5 ได้ "
            "ห้ามสร้างแหล่งอ้างอิงหรือผลการทดลองที่ไม่มีหลักฐาน"
        ),
    },
    {
        "agent_name": "Strategy Analyst",
        "title": "ถอดกฎการทำงานของ EA M30Tradedabreak.mq5",
        "instructions": (
            "วิเคราะห์ไฟล์ EA M30Tradedabreak.mq5 อย่างเป็นระบบ "
            "ถอดกฎการเข้าออเดอร์ เงื่อนไข Breakout "
            "การกำหนด Stop Loss และ Take Profit "
            "ตัวกรองตลาด ตัวกรองเวลา และเงื่อนไขป้องกันการเทรด "
            "ให้แยกสิ่งที่พบจากโค้ดจริงออกจากข้อสันนิษฐาน "
            "ห้ามเดากฎที่ไม่มีอยู่ในไฟล์"
        ),
    },
    {
        "agent_name": "Risk Analyst",
        "title": "วิเคราะห์ความเสี่ยงและโครงสร้าง Drawdown",
        "instructions": (
            "วิเคราะห์ระบบบริหารความเสี่ยงของ EA "
            "M30Tradedabreak.mq5 จากโค้ดจริง "
            "ตรวจสอบความเสี่ยงต่อออเดอร์ "
            "การควบคุมขาดทุนรายวัน "
            "การควบคุมขาดทุนรวม "
            "จำนวนออเดอร์สูงสุด "
            "ความเสี่ยงจาก Spread, Slippage, Gap และ False Breakout "
            "เสนอประเด็นที่ควรนำไปทดสอบเพิ่มเติม "
            "โดยไม่สรุปว่าแนวทางใดใช้ได้จริงจนกว่าจะมีผลทดสอบ"
        ),
    },
    {
        "agent_name": "Backtest Specialist",
        "title": "ออกแบบแผนการทดสอบย้อนหลังและ Forward Test",
        "instructions": (
            "ออกแบบแผนทดสอบ EA M30Tradedabreak.mq5 "
            "ให้สามารถตรวจสอบความแข็งแรงของกลยุทธ์ได้ "
            "ระบุข้อมูลที่ต้องใช้ ช่วงเวลา ตลาด "
            "ตัวแปรที่ต้องทดสอบ Metrics ที่ต้องบันทึก "
            "เช่น Net Profit, Profit Factor, Expectancy, Max Drawdown "
            "จำนวนการเทรด และผลแยกตามสภาวะตลาด "
            "ต้องแยก In-sample, Out-of-sample และ Forward Test "
            "ห้ามสร้างตัวเลขผลทดสอบขึ้นเอง"
        ),
    },
    {
        "agent_name": "Critic Agent",
        "title": "ตรวจสอบจุดอ่อนและความเสี่ยงเชิงระบบ",
        "instructions": (
            "ทำหน้าที่เป็นผู้วิพากษ์ระบบอย่างเข้มงวด "
            "ตรวจสอบข้อจำกัดของแนวคิด Breakout "
            "ความเสี่ยงจากตลาด Sideway "
            "False Breakout "
            "Overfitting "
            "การเลือกช่วงเวลา "
            "ความแตกต่างระหว่างผล Backtest กับการเทรดจริง "
            "รวมทั้งตรวจสอบว่าข้อสรุปของ Agent อื่นมีหลักฐานเพียงพอหรือไม่ "
            "ให้ระบุข้อเท็จจริง ข้อสันนิษฐาน และประเด็นที่ยังต้องพิสูจน์แยกจากกัน"
        ),
    },
]


# --------------------------------------------------
# List all tasks
# --------------------------------------------------

@router.get(
    "",
    response_model=list[ResearchTaskResponse],
)
def list_tasks(
    mission_id: str | None = None,
    agent_id: str | None = None,
    task_status: str | None = None,
    db: Session = Depends(get_db),
) -> list[ResearchTask]:

    statement = select(ResearchTask).order_by(
        ResearchTask.created_at.desc()
    )

    if mission_id:
        statement = statement.where(
            ResearchTask.mission_id == mission_id
        )

    if agent_id:
        statement = statement.where(
            ResearchTask.agent_id == agent_id
        )

    if task_status:
        statement = statement.where(
            ResearchTask.status == task_status
        )

    return list(db.scalars(statement).all())


# --------------------------------------------------
# Get one task
# --------------------------------------------------

@router.get(
    "/{task_id}",
    response_model=ResearchTaskResponse,
)
def get_task(
    task_id: str,
    db: Session = Depends(get_db),
) -> ResearchTask:

    task = db.get(ResearchTask, task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Research task not found.",
        )

    return task


# --------------------------------------------------
# Create one task manually
# --------------------------------------------------

@router.post(
    "",
    response_model=ResearchTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_task(
    payload: ResearchTaskCreate,
    db: Session = Depends(get_db),
) -> ResearchTask:

    mission = db.get(Mission, payload.mission_id)

    if not mission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Mission not found.",
        )

    agent = db.get(Agent, payload.agent_id)

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found.",
        )

    task = ResearchTask(
        mission_id=payload.mission_id,
        agent_id=payload.agent_id,
        title=payload.title,
        instructions=payload.instructions,
    )

    db.add(task)
    db.commit()
    db.refresh(task)

    return task


# --------------------------------------------------
# Update task status and result
# --------------------------------------------------

@router.patch(
    "/{task_id}",
    response_model=ResearchTaskResponse,
)
def update_task(
    task_id: str,
    payload: ResearchTaskUpdate,
    db: Session = Depends(get_db),
) -> ResearchTask:

    task = db.get(ResearchTask, task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Research task not found.",
        )

    task.status = payload.status

    if payload.result is not None:
        task.result = payload.result

    db.commit()
    db.refresh(task)

    return task


# --------------------------------------------------
# Automatically create five tasks for one mission
# --------------------------------------------------

@router.post(
    "/seed-for-mission/{mission_id}",
    response_model=list[ResearchTaskResponse],
    status_code=status.HTTP_201_CREATED,
)
def seed_tasks_for_mission(
    mission_id: str,
    db: Session = Depends(get_db),
) -> list[ResearchTask]:

    mission = db.get(Mission, mission_id)

    if not mission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Mission not found.",
        )

    created_tasks: list[ResearchTask] = []

    for template in TASK_TEMPLATES:

        agent = db.scalar(
            select(Agent).where(
                Agent.name == template["agent_name"]
            )
        )

        if not agent:
            continue

        existing = db.scalar(
            select(ResearchTask).where(
                ResearchTask.mission_id == mission_id,
                ResearchTask.agent_id == agent.id,
            )
        )

        if existing:
            continue

        task = ResearchTask(
            mission_id=mission_id,
            agent_id=agent.id,
            title=template["title"],
            instructions=template["instructions"],
            status="pending",
        )

        db.add(task)
        created_tasks.append(task)

    if created_tasks:
        db.commit()

        for task in created_tasks:
            db.refresh(task)

    return created_tasks
