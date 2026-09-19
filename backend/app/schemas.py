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
# EA Files
# =========================================================

class EAFileCreate(BaseModel):
    """
    ใช้สำหรับบันทึกไฟล์ EA จาก source code โดยตรง
    """

    mission_id: str = Field(
        min_length=1,
        description="UUID ของ Mission ที่ต้องการเชื่อมโยง"
    )

    filename: str = Field(
        min_length=1,
        max_length=255,
        description="ชื่อไฟล์ EA เช่น M30Tradedabreak.mq5"
    )

    source_code: str = Field(
        min_length=1,
        description="Source code ภาษา MQL5 ทั้งหมด"
    )


class EAFileResponse(BaseModel):
    """
    ข้อมูลสรุปของไฟล์ EA ที่ถูกอัปโหลด
    """

    id: str
    mission_id: str
    filename: str
    file_type: str
    line_count: int
    analysis_status: str
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class EAAnalysisResponse(BaseModel):
    """
    ผลการวิเคราะห์โครงสร้างเบื้องต้นของ EA
    """

    ea_file_id: str
    filename: str
    line_count: int
    detected_features: dict
    analysis_status: str
