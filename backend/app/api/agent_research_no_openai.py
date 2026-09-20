from __future__ import annotations
import json, os
from datetime import datetime
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Agent, Mission, ResearchTask
from app.research_models import Experiment, ExperimentResult

router = APIRouter(prefix="/agents/research", tags=["AI Agents (No OpenAI)"])

AGENT_RULES = {
    "Research Explorer": "ตรวจหลักฐานและแยกข้อเท็จจริงออกจากข้อสันนิษฐาน",
    "Strategy Analyst": "ตรวจตรรกะกลยุทธ์กับหลักฐานที่มี",
    "Risk Analyst": "ตรวจ profit factor, expectancy, drawdown และ trade count",
    "Backtest Specialist": "ตรวจ IS/OOS, walk-forward และ robustness",
    "Critic": "ค้นหาจุดอ่อน ข้อจำกัด และข้อมูลที่ยังขาด",
}

def _json(v: Any):
    return v.isoformat() if isinstance(v, datetime) else v

def _metrics(result):
    if not result:
        return {}
    try:
        x = json.loads(result.metrics or "{}")
        return x if isinstance(x, dict) else {"raw": x}
    except Exception:
        return {"raw": result.metrics}

def _pick(x):
    out = {}
    wanted = {"trade_count","net_profit","profit_factor","expectancy","max_drawdown",
              "max_dd","robustness","conclusion","status","method","periods","summary"}
    def walk(n):
        if isinstance(n, dict):
            for k,v in n.items():
                if k in wanted and k not in out and not isinstance(v,(dict,list)):
                    out[k] = v
                walk(v)
        elif isinstance(n,list):
            for v in n[:50]: walk(v)
    walk(x)
    return out

def _latest(db, experiment_id):
    return db.scalars(
        select(ExperimentResult)
        .where(ExperimentResult.experiment_id == experiment_id)
        .order_by(ExperimentResult.created_at.desc())
    ).first()

def _make_result(name, mission, experiment, latest):
    picked = _pick(_metrics(latest))
    lines = [
        f"# {name} — Rule-based Research Result",
        f"Mission: {mission.title}",
        f"Market: {mission.market} {mission.timeframe}",
        f"Experiment: {experiment.id}",
        "",
        "## Agent Check",
        AGENT_RULES[name],
        "",
        "## Evidence",
        f"Latest ExperimentResult: {'yes' if latest else 'no'}",
    ]
    if picked:
        lines += [f"- {k}: {v}" for k,v in picked.items()]
    else:
        lines.append("- ข้อมูลไม่เพียงพอที่จะสรุป")
    lines += [
        "",
        "## Limitations",
        "- deterministic/rule-based; ไม่เรียก OpenAI หรือ LLM",
        "- ไม่สร้างตัวเลข backtest ใหม่",
        "- ไม่สรุปว่ากลยุทธ์ใช้ได้จริงจากข้อมูลนี้เพียงอย่างเดียว",
    ]
    return "\n".join(lines)

@router.post("/{experiment_id}/run")
def run_agents(experiment_id: str, db: Session = Depends(get_db)):
    experiment = db.get(Experiment, experiment_id)
    if not experiment: raise HTTPException(404, "Experiment not found")
    mission = db.get(Mission, getattr(experiment, "mission_id", None))
    if not mission: raise HTTPException(404, "Mission not found")
    latest = _latest(db, experiment_id)
    runs = []
    for name, rule in AGENT_RULES.items():
        agent = db.scalars(select(Agent).where(Agent.name == name)).first()
        if not agent: continue
        task = ResearchTask(
            mission_id=mission.id, agent_id=agent.id,
            title=f"No-OpenAI research run: {name}",
            instructions=rule, status="completed",
            result=_make_result(name, mission, experiment, latest),
        )
        db.add(task); db.flush()
        runs.append({"task_id": str(task.id), "agent": name, "status": task.status})
    db.commit()
    return {"experiment_id": experiment_id, "mode": "rule_based_no_openai",
            "status": "completed", "agents_run": len(runs), "runs": runs}

@router.get("/{experiment_id}/runs")
def list_runs(experiment_id: str, db: Session = Depends(get_db)):
    experiment = db.get(Experiment, experiment_id)
    if not experiment: raise HTTPException(404, "Experiment not found")
    tasks = db.scalars(
        select(ResearchTask)
        .where(ResearchTask.mission_id == getattr(experiment,"mission_id",None))
        .order_by(ResearchTask.created_at.desc())
    ).all()
    return {"experiment_id": experiment_id, "mode": "rule_based_no_openai",
            "count": len(tasks),
            "runs": [{"task_id":str(t.id),"agent_id":str(t.agent_id),
                      "title":t.title,"status":t.status,"result":t.result,
                      "created_at":_json(t.created_at)} for t in tasks]}

@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)):
    task = db.get(ResearchTask, run_id)
    if not task: raise HTTPException(404, "Research run not found")
    return {"run_id":str(task.id),"agent_id":str(task.agent_id),
            "mission_id":str(task.mission_id),"status":task.status,
            "title":task.title,"instructions":task.instructions,
            "result":task.result,"created_at":_json(task.created_at),
            "updated_at":_json(task.updated_at)}

@router.post("/{experiment_id}/moltbook-draft")
def moltbook_draft(experiment_id: str, db: Session = Depends(get_db)):
    experiment = db.get(Experiment, experiment_id)
    if not experiment: raise HTTPException(404, "Experiment not found")
    mission = db.get(Mission, getattr(experiment, "mission_id", None))
    if not mission: raise HTTPException(404, "Mission not found")
    latest = _latest(db, experiment_id)
    metrics = _pick(_metrics(latest))
    body = [f"Research update: {mission.title}", "",
            f"Market: {mission.market} {mission.timeframe}",
            "Mode: deterministic research agents (no OpenAI)", "", "Evidence:"]
    body += [f"- {k}: {v}" for k,v in metrics.items()] or ["- No structured metrics available."]
    body += ["", "Research note only; not a trading instruction.",
             "Further unseen-period validation is required before deployment."]
    return {"experiment_id":experiment_id,"mode":"draft_only",
            "moltbook_api_key_configured":bool(os.getenv("MOLTBOOK_API_KEY")),
            "post":"\n".join(body),"published":False}
