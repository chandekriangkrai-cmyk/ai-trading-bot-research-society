from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


# -------------------------
# Agent Schemas
# -------------------------

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


# -------------------------
# Mission Schemas
# -------------------------

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


# -------------------------
# Research Task Schemas
# -------------------------

class ResearchTaskBase(BaseModel):
    mission_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    title: str = Field(min_length=3, max_length=255)
    instructions: str = Field(min_length=3)


class ResearchTaskCreate(ResearchTaskBase):
    pass


class ResearchTaskUpdate(BaseModel):
    status: Literal[
        "pending",
        "in_progress",
        "completed",
        "failed",
        "cancelled",
    ]

    result: str | None = None


class ResearchTaskResponse(ResearchTaskBase):
    id: str
    status: str
    result: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
