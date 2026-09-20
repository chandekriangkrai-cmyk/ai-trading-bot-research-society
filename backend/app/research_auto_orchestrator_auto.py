from __future__ import annotations

"""Database-aware, no-hardcoded-ID research orchestrator.

Flow:
    Mission + EAFile -> ResearchLead -> Hypothesis -> Experiment
    -> v1/v3/v4/v5/v6/v7/v8/v9/v10/v11 -> v11.2

Swagger remains an admin/debug surface. This module discovers work from DB.
CSV inputs are still supplied through the existing upload endpoint/folder.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any

from fastapi import UploadFile

from app.database import SessionLocal
from app.ea_files import EAFile
from app.models import Mission
from app.research_models import Experiment, ExperimentResult, Hypothesis, ResearchLead

from app.api.research_runner import import_trade_results
from app.api.research_runner_v3 import import_mt5_trade_results
from app.api.research_runner_v4 import import_major_fx_trade_results
from app.api.research_runner_v5 import import_mt5_deals
from app.api.research_runner_v6 import import_mt5_deals_is_oos
from app.api.research_runner_v7 import import_mt5_deals_context
from app.api.research_runner_v8 import import_mt5_deals_entry_context
from app.api.research_runner_v9 import walk_forward_entry_context
from app.api.research_runner_v10 import robustness_gate

INPUT_ROOT = Path(os.getenv("RESEARCH_INPUT_ROOT", "./research_inputs"))
STATE_ROOT = Path(os.getenv("RESEARCH_AUTO_STATE_ROOT", "./research_auto_state"))
INTERVAL_SECONDS = max(5, int(os.getenv("RESEARCH_AUTO_INTERVAL", "30")))
MIN_TRADES = max(1, int(os.getenv("RESEARCH_AUTO_MIN_TRADES", "20")))
INPUT_TIMEZONE = os.getenv("RESEARCH_INPUT_TIMEZONE", "UTC")
POLICIES = os.getenv(
    "RESEARCH_V11_POLICIES",
    "flat,high_defensive,low_defensive,high_low_defensive",
)
AUTO_EXPERIMENT_TYPE = "auto_research_pipeline"

STAGES = [
    "v1_import_trades",
    "v3_import_mt5_trades",
    "v4_major_fx",
    "v5_import_mt5_deals",
    "v6_is_oos",
    "v7_context",
    "v8_entry_context",
    "v9_walk_forward",
    "v10_robustness",
    "v11_sizing",
    "v11_2_three_year",
]

_state: dict[str, Any] = {
    "enabled": True,
    "running": False,
    "started_at": None,
    "last_cycle_at": None,
    "last_error": None,
    "last_cycle": None,
    "processed_experiments": 0,
    "blocked_experiments": 0,
    "completed_experiments": 0,
    "auto_created_experiments": 0,
}
_lock = asyncio.Lock()
_stop = asyncio.Event()
_task: asyncio.Task | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_file(experiment_id: str) -> Path:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    return STATE_ROOT / f"{experiment_id}.json"


def _load_state(experiment_id: str) -> dict[str, Any]:
    p = _state_file(experiment_id)
    if not p.exists():
        return {"experiment_id": experiment_id, "completed_stages": [], "blocked": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("experiment_id") != experiment_id:
            raise ValueError("state experiment mismatch")
        return data
    except Exception:
        return {"experiment_id": experiment_id, "completed_stages": [], "blocked": None}


def _save_state(data: dict[str, Any]) -> None:
    p = _state_file(str(data["experiment_id"]))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def _upload(path: Path) -> UploadFile:
    f = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    with path.open("rb") as src:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    f.seek(0)
    return UploadFile(file=f, filename=path.name)


def _find_file(folder: Path, *names: str) -> Path | None:
    for name in names:
        p = folder / name
        if p.exists() and p.is_file():
            return p
    return None


def _inputs(experiment_id: str) -> dict[str, Path | None]:
    folder = INPUT_ROOT / experiment_id
    return {
        "folder": folder,
        "trades": _find_file(folder, "trades.csv", "trade_results.csv"),
        "market": _find_file(folder, "market.csv", "ohlc.csv"),
        "deals": _find_file(folder, "deals.csv", "mt5_deals.csv"),
        "is_deals": _find_file(folder, "is_deals.csv", "2024_deals.csv"),
        "is_market": _find_file(folder, "is_market.csv", "2024_market.csv"),
        "oos_deals": _find_file(folder, "oos_deals.csv", "2025_deals.csv"),
        "oos_market": _find_file(folder, "oos_market.csv", "2025_market.csv"),
    }


def _missing_for(stage: str, files: dict[str, Path | None]) -> list[str]:
    required = {
        "v1_import_trades": ["trades"],
        "v3_import_mt5_trades": ["trades"],
        "v4_major_fx": ["trades"],
        "v5_import_mt5_deals": ["deals"],
        "v6_is_oos": ["deals"],
        "v7_context": ["deals", "market"],
        "v8_entry_context": ["deals", "market"],
        "v9_walk_forward": ["is_deals", "is_market", "oos_deals", "oos_market"],
        "v10_robustness": [],
        "v11_sizing": ["deals", "market"],
        "v11_2_three_year": [],
    }
    return [x for x in required[stage] if files.get(x) is None]


def _experiment_exists_for_mission(db, mission_id: str) -> Experiment | None:
    return (
        db.query(Experiment)
        .filter(
            Experiment.mission_id == mission_id,
            Experiment.experiment_type == AUTO_EXPERIMENT_TYPE,
        )
        .order_by(Experiment.created_at.desc())
        .first()
    )


def _ensure_research_chain() -> list[str]:
    """Create the minimum Lead -> Hypothesis -> Experiment chain per mission.

    Existing research is reused. Nothing is hardcoded to a particular UUID.
    """
    db = SessionLocal()
    created: list[str] = []
    try:
        missions = db.query(Mission).all()
        for mission in missions:
            ea = (
                db.query(EAFile)
                .filter(EAFile.mission_id == mission.id)
                .order_by(EAFile.created_at.desc())
                .first()
            )
            if not ea:
                continue

            exp = _experiment_exists_for_mission(db, str(mission.id))
            if exp:
                continue

            lead = ResearchLead(
                source="system",
                external_id=f"mission:{mission.id}:auto",
                title=f"Auto research: {mission.title}",
                author="research-orchestrator",
                content=(
                    f"Automatic research lead generated from mission '{mission.title}'. "
                    f"EA: {ea.filename}."
                ),
                market=mission.market,
                timeframe=mission.timeframe,
            )
            db.add(lead)
            db.flush()

            hypothesis = Hypothesis(
                research_lead_id=lead.id,
                mission_id=mission.id,
                title=f"General research validation: {ea.filename}",
                statement=(
                    f"Evaluate whether {ea.filename} shows reproducible performance "
                    f"on {mission.market} {mission.timeframe} across available periods "
                    "without changing the EA parameters during validation."
                ),
                assumptions=(
                    "Use supplied MT5 data; preserve accounting-aware realized P/L; "
                    "separate in-sample/out-of-sample evidence where available; "
                    "do not treat regime differences as causal edges without validation."
                ),
                status="proposed",
            )
            db.add(hypothesis)
            db.flush()

            experiment = Experiment(
                hypothesis_id=hypothesis.id,
                mission_id=mission.id,
                ea_file_id=ea.id,
                symbol=mission.market,
                timeframe=mission.timeframe,
                experiment_type=AUTO_EXPERIMENT_TYPE,
                specification=(
                    "Automatic research pipeline: v1 baseline metrics; v3/v4 MT5 validation; "
                    "v5 accounting-aware deals; v6 period split; v7 close context; "
                    "v8 entry context; v9 walk-forward; v10 robustness; v11 sizing; "
                    "v11.2 three-year gate when compatible 2026 unseen-year evidence exists."
                ),
                baseline="Existing EA rules and parameters; no OOS parameter adjustment.",
                status="planned",
            )
            db.add(experiment)
            db.commit()
            created.append(str(experiment.id))
        return created
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _run_v11_2_auto() -> Any:
    """Run v11.2 by discovering compatible 2026 + 2024/2025 results."""
    from app.api.research_runner_v11_2 import robustness_gate_3year

    db = SessionLocal()
    try:
        experiments = db.query(Experiment).all()
        latest: dict[str, ExperimentResult] = {}
        for exp in experiments:
            rows = (
                db.query(ExperimentResult)
                .filter(ExperimentResult.experiment_id == exp.id)
                .order_by(ExperimentResult.created_at.desc())
                .all()
            )
            for row in rows:
                try:
                    metrics = json.loads(row.metrics or "{}")
                except Exception:
                    continue
                method = str(metrics.get("method", ""))
                if "v11.2 three-year robustness gate" not in method:
                    latest[str(exp.id)] = row
                    break

        unseen: list[tuple[Experiment, ExperimentResult]] = []
        baselines: list[tuple[Experiment, ExperimentResult]] = []
        for exp in experiments:
            row = latest.get(str(exp.id))
            if not row:
                continue
            try:
                metrics = json.loads(row.metrics or "{}")
            except Exception:
                continue
            periods = metrics.get("periods") or {}
            text_blob = json.dumps(metrics, ensure_ascii=False)
            if periods.get("unseen") == "2026" or "2026" in text_blob:
                unseen.append((exp, row))
            if periods.get("is") == "2024" and periods.get("oos") == "2025":
                baselines.append((exp, row))

        runs = []
        for current_exp, current_result in unseen:
            baseline = next(
                (
                    (exp, row)
                    for exp, row in baselines
                    if str(exp.mission_id) == str(current_exp.mission_id)
                    and str(exp.id) != str(current_exp.id)
                ),
                None,
            )
            if not baseline:
                continue
            baseline_exp, _ = baseline
            source_id = str(current_result.id)

            existing = (
                db.query(ExperimentResult)
                .filter(ExperimentResult.experiment_id == current_exp.id)
                .all()
            )
            already = False
            for row in existing:
                try:
                    metrics = json.loads(row.metrics or "{}")
                except Exception:
                    continue
                if (
                    "v11.2 three-year robustness gate" in str(metrics.get("method", ""))
                    and str(metrics.get("source_result_id")) == source_id
                ):
                    already = True
                    break
            if already:
                runs.append({"experiment_id": str(current_exp.id), "status": "already_current"})
                continue

            result = robustness_gate_3year(
                experiment_id=str(current_exp.id),
                baseline_2024_2025_experiment_id=str(baseline_exp.id),
                min_trades=MIN_TRADES,
            )
            runs.append({
                "experiment_id": str(current_exp.id),
                "baseline_experiment_id": str(baseline_exp.id),
                "source_result_id": source_id,
                "status": "completed",
                "result": result,
            })
        return {"status": "completed", "runs": runs}
    finally:
        db.close()


async def _run_stage(experiment_id: str, stage: str, files: dict[str, Path | None]) -> Any:
    db = SessionLocal()
    try:
        if stage == "v1_import_trades":
            return await import_trade_results(experiment_id, _upload(files["trades"]), db)
        if stage == "v3_import_mt5_trades":
            market = _upload(files["market"]) if files["market"] else None
            return await import_mt5_trade_results(experiment_id, _upload(files["trades"]), market, db)
        if stage == "v4_major_fx":
            market = _upload(files["market"]) if files["market"] else None
            return await import_major_fx_trade_results(experiment_id, _upload(files["trades"]), market, db)
        if stage == "v5_import_mt5_deals":
            market = _upload(files["market"]) if files["market"] else None
            return await import_mt5_deals(experiment_id, _upload(files["deals"]), market, db)
        if stage == "v6_is_oos":
            market = _upload(files["market"]) if files["market"] else None
            return await import_mt5_deals_is_oos(experiment_id, _upload(files["deals"]), market, db)
        if stage == "v7_context":
            return await import_mt5_deals_context(
                experiment_id, _upload(files["deals"]), _upload(files["market"]), INPUT_TIMEZONE, db
            )
        if stage == "v8_entry_context":
            return await import_mt5_deals_entry_context(
                experiment_id, _upload(files["deals"]), _upload(files["market"]), INPUT_TIMEZONE, db
            )
        if stage == "v9_walk_forward":
            return await walk_forward_entry_context(
                experiment_id,
                _upload(files["is_deals"]), _upload(files["is_market"]),
                _upload(files["oos_deals"]), _upload(files["oos_market"]),
                INPUT_TIMEZONE, db,
            )
        if stage == "v10_robustness":
            return robustness_gate(experiment_id, MIN_TRADES)
        if stage == "v11_sizing":
            from app.api import research_runner_v11
            runner = getattr(research_runner_v11, "regime_sizing_simulation", None)
            if runner is None:
                return {"status": "skipped", "reason": "v11 regime_sizing_simulation unavailable"}
            return await runner(
                experiment_id, _upload(files["deals"]), _upload(files["market"]), INPUT_TIMEZONE, POLICIES
            )
        raise RuntimeError(f"Unknown stage: {stage}")
    finally:
        db.close()


def _experiment_ids() -> list[str]:
    db = SessionLocal()
    try:
        return [str(x.id) for x in db.query(Experiment).all()]
    finally:
        db.close()


async def _process_experiment(experiment_id: str) -> dict[str, Any]:
    state = _load_state(experiment_id)
    files = _inputs(experiment_id)
    state["last_seen_at"] = _now()
    state["input_folder"] = str(files["folder"])
    completed = list(state.get("completed_stages", []))
    results = list(state.get("results", []))

    for stage in STAGES[:-1]:
        if stage in completed:
            continue
        missing = _missing_for(stage, files)
        if missing:
            state["blocked"] = {
                "stage": stage,
                "missing_files": missing,
                "message": "Upload the missing data; the worker will resume automatically.",
                "updated_at": _now(),
            }
            _save_state(state)
            return {"experiment_id": experiment_id, "status": "blocked", "stage": stage, "missing_files": missing}

        state["blocked"] = None
        state["current_stage"] = stage
        _save_state(state)
        try:
            result = await _run_stage(experiment_id, stage, files)
            completed.append(stage)
            results.append({"stage": stage, "completed_at": _now(), "result": result})
            state["completed_stages"] = completed
            state["results"] = results
            state["current_stage"] = None
            _save_state(state)
        except Exception as exc:
            state["blocked"] = {"stage": stage, "error": f"{type(exc).__name__}: {exc}", "updated_at": _now()}
            _save_state(state)
            return {"experiment_id": experiment_id, "status": "error", "stage": stage, "error": str(exc)}

    state["status"] = "completed"
    state["completed_at"] = state.get("completed_at") or _now()
    state["blocked"] = None
    _save_state(state)
    return {"experiment_id": experiment_id, "status": "completed", "completed_stages": completed}


async def run_cycle() -> dict[str, Any]:
    async with _lock:
        _state["running"] = True
        _state["last_cycle_at"] = _now()
        _state["last_error"] = None
        try:
            created = _ensure_research_chain()
            _state["auto_created_experiments"] += len(created)
            cycle = [await _process_experiment(x) for x in _experiment_ids()]
            try:
                gate = await _run_v11_2_auto()
            except Exception as exc:
                gate = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            _state["last_cycle"] = {"created_experiments": created, "experiments": cycle, "v11_2": gate}
            _state["processed_experiments"] = len(cycle)
            _state["blocked_experiments"] = sum(1 for x in cycle if x.get("status") == "blocked")
            _state["completed_experiments"] = sum(1 for x in cycle if x.get("status") == "completed")
            return _state["last_cycle"]
        except Exception as exc:
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
            return {"status": "error", "error": str(exc)}
        finally:
            _state["running"] = False


async def _loop() -> None:
    _state["started_at"] = _now()
    await run_cycle()
    while not _stop.is_set():
        try:
            await asyncio.wait_for(_stop.wait(), timeout=INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            await run_cycle()


def start() -> None:
    global _task
    if _task is None or _task.done():
        _stop.clear()
        _state["enabled"] = True
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    _stop.set()
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    _state["running"] = False


def status() -> dict[str, Any]:
    return {
        **_state,
        "interval_seconds": INTERVAL_SECONDS,
        "input_root": str(INPUT_ROOT),
        "state_root": str(STATE_ROOT),
        "stages": STAGES,
        "auto_experiment_type": AUTO_EXPERIMENT_TYPE,
    }


async def run_now() -> dict[str, Any]:
    return await run_cycle()
