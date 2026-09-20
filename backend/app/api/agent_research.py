from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_runner import run_experiment_agent_panel
from app.database import SessionLocal
from app.ea_files import EAFile
from app.models import Agent, Mission
from app.research_models import AgentRun, Experiment, ExperimentResult, Hypothesis


router = APIRouter(prefix="/agents/research", tags=["AI Research Agents"])


DEFAULT_AGENT_NAMES = [
    "Research Explorer",
    "Strategy Analyst",
    "Risk Analyst",
    "Backtest Specialist",
    "Critic Agent",
]


def _get_latest_result(db: Session, experiment_id: str) -> ExperimentResult | None:
    return db.scalar(
        select(ExperimentResult)
        .where(ExperimentResult.experiment_id == experiment_id)
        .order_by(ExperimentResult.created_at.desc())
    )


@router.post("/{experiment_id}/run")
def run_experiment_agents(experiment_id: str) -> dict:
    """Run the five research agents and synthesize one report.

    Read-only research action. It cannot place, modify, or close trades.
    """
    db = SessionLocal()
    try:
        experiment = db.get(Experiment, experiment_id)
        if not experiment:
            raise HTTPException(404, "Experiment not found.")

        result = _get_latest_result(db, experiment_id)
        if not result:
            raise HTTPException(
                409,
                "No ExperimentResult exists yet. Run the research pipeline first.",
            )

        hypothesis = db.get(Hypothesis, experiment.hypothesis_id)
        mission = db.get(Mission, experiment.mission_id) if experiment.mission_id else None
        if not hypothesis:
            raise HTTPException(404, "Hypothesis not found.")

        agents = list(
            db.scalars(
                select(Agent).where(Agent.name.in_(DEFAULT_AGENT_NAMES))
            ).all()
        )
        by_name = {a.name: a for a in agents}
        missing = [name for name in DEFAULT_AGENT_NAMES if name not in by_name]
        if missing:
            raise HTTPException(
                409,
                f"Missing default agents: {', '.join(missing)}. "
                "Call POST /api/agents/seed-defaults first.",
            )
        agents = [by_name[name] for name in DEFAULT_AGENT_NAMES]

        ea = db.get(EAFile, experiment.ea_file_id) if experiment.ea_file_id else None

        run = AgentRun(
            experiment_id=experiment_id,
            status="running",
            report="",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        try:
            payload = run_experiment_agent_panel(
                experiment=experiment,
                hypothesis=hypothesis,
                result=result,
                mission=mission,
                ea_filename=ea.filename if ea else None,
                ea_source=ea.source_code if ea else None,
                agents=agents,
            )
            run.status = "completed"
            run.report = json.dumps(payload, ensure_ascii=False)
            db.commit()
            db.refresh(run)
        except Exception as exc:
            db.rollback()
            run = db.get(AgentRun, run.id)
            if run:
                run.status = "failed"
                run.report = json.dumps(
                    {"error": str(exc)},
                    ensure_ascii=False,
                )
                db.commit()
            raise HTTPException(500, f"Agent panel failed: {exc}") from exc

        return {
            "status": run.status,
            "run_id": run.id,
            "experiment_id": experiment_id,
            "agents": DEFAULT_AGENT_NAMES,
            "report": payload["report"],
            "note": "Research-only. No trade execution capability.",
        }
    finally:
        db.close()


@router.get("/{experiment_id}/runs")
def list_experiment_agent_runs(experiment_id: str) -> list[dict]:
    db = SessionLocal()
    try:
        if not db.get(Experiment, experiment_id):
            raise HTTPException(404, "Experiment not found.")
        rows = db.scalars(
            select(AgentRun)
            .where(AgentRun.experiment_id == experiment_id)
            .order_by(AgentRun.created_at.desc())
        ).all()
        return [
            {
                "run_id": row.id,
                "experiment_id": row.experiment_id,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    finally:
        db.close()


@router.get("/runs/{run_id}")
def get_agent_run(run_id: str) -> dict:
    db = SessionLocal()
    try:
        row = db.get(AgentRun, run_id)
        if not row:
            raise HTTPException(404, "Agent run not found.")
        try:
            report = json.loads(row.report) if row.report else {}
        except json.JSONDecodeError:
            report = {"raw": row.report}
        return {
            "run_id": row.id,
            "experiment_id": row.experiment_id,
            "status": row.status,
            "created_at": row.created_at,
            "report": report,
        }
    finally:
        db.close()


@router.post("/{experiment_id}/moltbook-draft")
def create_moltbook_draft(experiment_id: str) -> dict:
    """Prepare a Moltbook-ready post without calling Moltbook.

    Publishing is deliberately separated until Moltbook credentials/API
    contract are configured. This endpoint never sends external requests.
    """
    db = SessionLocal()
    try:
        if not db.get(Experiment, experiment_id):
            raise HTTPException(404, "Experiment not found.")
        row = db.scalar(
            select(AgentRun)
            .where(
                AgentRun.experiment_id == experiment_id,
                AgentRun.status == "completed",
            )
            .order_by(AgentRun.created_at.desc())
        )
        if not row:
            raise HTTPException(
                409,
                "Run the research agents first.",
            )
        payload = json.loads(row.report)
        report = payload.get("report", "")
        title = f"AI Trading Research Report — {experiment_id}"
        body = f"{title}\\n\\n{report}"
        return {
            "status": "draft_ready",
            "platform": "moltbook",
            "title": title,
            "body": body,
            "publish_mode": "manual_until_credentials_are_configured",
        }
    finally:
        db.close()
