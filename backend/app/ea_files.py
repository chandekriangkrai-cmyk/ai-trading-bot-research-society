from sqlalchemy import Column, String, Text, Integer, DateTime, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.database import Base


class EAFile(Base):
    __tablename__ = "ea_files"

    id = Column(String, primary_key=True)
    mission_id = Column(String, ForeignKey("missions.id"), nullable=False)

    filename = Column(String, nullable=False)
    file_type = Column(String, nullable=False, default="mq5")
    source_code = Column(Text, nullable=False)

    line_count = Column(Integer, nullable=False, default=0)
    analysis_status = Column(
        String,
        nullable=False,
        default="uploaded"
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    mission = relationship("Mission")
