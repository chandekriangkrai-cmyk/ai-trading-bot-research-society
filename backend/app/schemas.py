from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


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
