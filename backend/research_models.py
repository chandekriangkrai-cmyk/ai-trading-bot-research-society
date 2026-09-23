from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchLead(Base):
    __tablename__ = "research_leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    market: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timeframe: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Hypothesis(Base):
    __tablename__ = "research_hypotheses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    research_lead_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("research_leads.id"), nullable=True, index=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("missions.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    assumptions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class Experiment(Base):
    __tablename__ = "research_experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    hypothesis_id: Mapped[str] = mapped_column(String(36), ForeignKey("research_hypotheses.id"), nullable=False, index=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("missions.id"), nullable=True, index=True)
    ea_file_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ea_files.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(50), nullable=False)
    experiment_type: Mapped[str] = mapped_column(String(50), nullable=False, default="backtest")
    specification: Mapped[str] = mapped_column(Text, nullable=False)
    baseline: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="planned")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentResult(Base):
    __tablename__ = "research_experiment_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    experiment_id: Mapped[str] = mapped_column(String(36), ForeignKey("research_experiments.id"), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    limitations: Mapped[str] = mapped_column(Text, nullable=False, default="")
    conclusion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

class MoltbookInteractionLead(Base):
    __tablename__ = "moltbook_interaction_leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    post_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    relevance_score: Mapped[float] = mapped_column(nullable=False, default=0.0)
    novelty_score: Mapped[float] = mapped_column(nullable=False, default=0.0)
    research_value_score: Mapped[float] = mapped_column(nullable=False, default=0.0)
    decision: Mapped[str] = mapped_column(String(40), nullable=False, default="ignore")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="discovered")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    draft_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class MoltbookInteraction(Base):
    __tablename__ = "moltbook_interactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    post_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    comment_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    parent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    direction: Mapped[str] = mapped_column(String(30), nullable=False, default="outbound")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    classification: Mapped[str] = mapped_column(String(60), nullable=False, default="other")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="draft")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

class MoltbookPostLink(Base):
    __tablename__ = "moltbook_post_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    experiment_id: Mapped[str] = mapped_column(String(36), ForeignKey("research_experiments.id"), nullable=False, unique=True, index=True)
    post_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="published")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ResearchDiscussion(Base):
    __tablename__ = "research_discussions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    experiment_id: Mapped[str] = mapped_column(String(36), ForeignKey("research_experiments.id"), nullable=False, index=True)
    post_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    comment_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    parent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author: Mapped[str] = mapped_column(String(255), nullable=False, default="unknown")
    comment_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    classification: Mapped[str] = mapped_column(String(60), nullable=False, default="other")
    decision: Mapped[str] = mapped_column(String(40), nullable=False, default="ignore")
    draft_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_comment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
