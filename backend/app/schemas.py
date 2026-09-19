from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# =========================================================
# Health
# =========================================================

class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


# =========================================================
# Agents
# =========================================================

class AgentBase(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    role: str = Field(min_length=2, max_length=255)
    description: str = Field(min_length=2)


class AgentCreate(AgentBase):
    pass


class AgentResponse(AgentBase):
    id: str
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# =========================================================
# Missions
# =========================================================

class MissionBase(BaseModel):
    title: str = Field(min_length=3, max_length=255)
    description: str = Field(min_length=3)
    market: str = Field(min_length=2, max_length=80)
    timeframe: str = Field(min_length=1, max_length=30)


class MissionCreate(MissionBase):
    pass


class MissionResponse(MissionBase):
    id: str
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# =========================================================
# Research Tasks
# =========================================================

class ResearchTaskBase(BaseModel):
    mission_id: str = Field(
        min_length=1,
        description="UUID ของ Mission"
    )

    agent_id: str = Field(
        min_length=1,
        description="UUID ของ Agent"
    )

    title: str = Field(
        min_length=3,
        max_length=255,
        description="ชื่อภารกิจย่อย"
    )

    instructions: str = Field(
        min_length=3,
        description="รายละเอียดและคำสั่งของภารกิจย่อย"
    )


class ResearchTaskCreate(ResearchTaskBase):
    pass


class ResearchTaskUpdate(BaseModel):
    status: Optional[str] = Field(
        default=None,
        description="สถานะของ Task"
    )

    result: Optional[str] = Field(
        default=None,
        description="ผลลัพธ์จาก Agent"
    )


class ResearchTaskResponse(ResearchTaskBase):
    id: str
    status: str
    result: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# =========================================================
# EA Files
# =========================================================

class EAFileCreate(BaseModel):
    mission_id: str = Field(
        min_length=1,
        description="UUID ของ Mission"
    )

    filename: str = Field(
        min_length=1,
        max_length=255,
        description="ชื่อไฟล์ EA เช่น M30Tradedabreak.mq5"
    )

    source_code: str = Field(
        min_length=1,
        description="Source code ภาษา MQL5"
    )


class EAFileResponse(BaseModel):
    id: str
    mission_id: str
    filename: str
    file_type: str
    line_count: int
    analysis_status: str
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class EAAnalysisResponse(BaseModel):
    ea_file_id: str
    filename: str
    line_count: int
    detected_features: dict
    analysis_status: str
