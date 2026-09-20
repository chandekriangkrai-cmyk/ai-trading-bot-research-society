from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any

from fastapi import UploadFile
from app.database import SessionLocal
from app.research_models import Experiment

from app.api.research_runner import import_trade_results
from app.api.research_runner_v3 import import_mt5_trade_results
from app.api.research_runner_v4 import import_major_fx_trade_results
from app.api.research_runner_v5 import import_mt5_deals
from app.api.research_runner_v6 import import_mt5_deals_is_oos
from app.api.research_runner_v7 import import_mt5_deals_context
from app.api.research_runner_v8 import import_mt5_deals_entry_context
from app.api.research_runner_v9 import walk_forward_entry_context
from app.api.research_runner_v10 import robustness_gate
from app.api.research_runner_v11 import regime_sizing_simulation
from app.api.research_runner_v11_2 import robustness_gate_3year


INPUT_ROOT = Path(os.getenv("RESEARCH_INPUT_ROOT", "./research_inputs"))
STATE_ROOT = Path(os.getenv("RESEARCH_AUTO_STATE_ROOT", "./research_auto_state"))
INTERVAL_SECONDS = max(5, int(os.getenv("RESEARCH_AUTO_INTERVAL", "30")))
MIN_TRADES = max(1, int(os.getenv("RESEARCH_AUTO_MIN_TRADES", "20")))
INPUT_TIMEZONE = os.getenv("RESEARCH_INPUT_TIMEZONE", "UTC")
POLICIES = os.getenv(
    "RESEARCH_V11_POLICIES",
    "flat,high_defensive,low_defensive,high_low_defensive",
)

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
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"experiment_id": experiment_id, "completed_stages": [], "blocked": None}


def _save_state(data: dict[str, Any]) -> None:
    p = _state_file(str(data["experiment_id"]))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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


async def _run_v11_2_auto(experiment_id: str) -> Any:
    """Run the locked 2024/2025 + unseen 2026 gate for the current experiment."""
    baseline_id = os.getenv(
        "RESEARCH_BASELINE_2024_2025_EXPERIMENT_ID",
        "1e2e17b9-534f-4346-aafe-f0266cd80afe",
    )
    return robustness_gate_3year(
        experiment_id=experiment_id,
        baseline_2024_2025_experiment_id=baseline_id,
        min_trades=MIN_TRADES,
    )


async def _run_stage(experiment_id: str, stage: str, files: dict[str, Path | None]) -> Any:
    db = SessionLocal()
    try:
        if stage == "v1_import_trades":
            return await import_trade_results(
                experiment_id, _upload(files["trades"]), db
            )

        if stage == "v3_import_mt5_trades":
            market = _upload(files["market"]) if files["market"] else None
            return await import_mt5_trade_results(
                experiment_id, _upload(files["trades"]), market, db
            )

        if stage == "v4_major_fx":
            market = _upload(files["market"]) if files["market"] else None
            return await import_major_fx_trade_results(
                experiment_id, _upload(files["trades"]), market, db
            )

        if stage == "v5_import_mt5_deals":
            market = _upload(files["market"]) if files["market"] else None
            return await import_mt5_deals(
                experiment_id, _upload(files["deals"]), market, db
            )

        if stage == "v6_is_oos":
            # v6's implementation derives its 2025 H1/H2 split from the
            # supplied deals file; it accepts the same deals/market inputs.
            market = _upload(files["market"]) if files["market"] else None
            return await import_mt5_deals_is_oos(
                experiment_id, _upload(files["deals"]), market, db
            )

        if stage == "v7_context":
            return await import_mt5_deals_context(
                experiment_id,
                _upload(files["deals"]),
                _upload(files["market"]),
                INPUT_TIMEZONE,
                db,
            )

        if stage == "v8_entry_context":
            return await import_mt5_deals_entry_context(
                experiment_id,
                _upload(files["deals"]),
                _upload(files["market"]),
                INPUT_TIMEZONE,
                db,
            )

        if stage == "v9_walk_forward":
            return await walk_forward_entry_context(
                experiment_id,
                _upload(files["is_deals"]),
                _upload(files["is_market"]),
                _upload(files["oos_deals"]),
                _upload(files["oos_market"]),
                INPUT_TIMEZONE,
                db,
            )

        if stage == "v10_robustness":
            return robustness_gate(experiment_id, MIN_TRADES)

        if stage == "v11_sizing":
            return await regime_sizing_simulation(
                experiment_id,
                _upload(files["deals"]),
                _upload(files["market"]),
                INPUT_TIMEZONE,
                POLICIES,
            )

        if stage == "v11_2_three_year":
            return await _run_v11_2_auto(experiment_id)

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

    # A stage is only marked complete after its existing runner returns.
    # This makes restarts resume from the first unfinished stage.
    for stage in STAGES:
        if stage in completed:
            continue

        missing = _missing_for(stage, files)
        if missing:
            state["blocked"] = {
                "stage": stage,
                "missing_files": missing,
                "message": (
                    "Place the missing CSV files in the experiment input folder "
                    "and the worker will continue automatically."
                ),
                "updated_at": _now(),
            }
            _save_state(state)
            return {
                "experiment_id": experiment_id,
                "status": "blocked",
                "stage": stage,
                "missing_files": missing,
            }

        state["blocked"] = None
        state["current_stage"] = stage
        _save_state(state)

        try:
            result = await _run_stage(experiment_id, stage, files)
            completed.append(stage)
            results.append({
                "stage": stage,
                "completed_at": _now(),
                "result": result,
            })
            state["completed_stages"] = completed
            state["results"] = results
            state["current_stage"] = None
            _save_state(state)
        except Exception as exc:
            state["blocked"] = {
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
                "updated_at": _now(),
            }
            _save_state(state)
            return {
                "experiment_id": experiment_id,
                "status": "error",
                "stage": stage,
                "error": str(exc),
            }

    state["status"] = "completed"
    state["completed_at"] = state.get("completed_at") or _now()
    state["blocked"] = None
    _save_state(state)
    return {
        "experiment_id": experiment_id,
        "status": "completed",
        "completed_stages": completed,
    }


async def run_cycle() -> dict[str, Any]:
    async with _lock:
        _state["running"] = True
        _state["last_cycle_at"] = _now()
        _state["last_error"] = None
        try:
            ids = _experiment_ids()
            cycle = []

            # Always attempt the locked three-year gate first. It needs no
            # Swagger input and reads the existing 2024/2025 + 2026 results.
            for experiment_id in ids:
                cycle.append(await _process_experiment(experiment_id))
                try:
                    cycle.append({
                        "experiment_id": experiment_id,
                        "stage": "v11_2_three_year",
                        "status": "completed",
                        "result": await _run_v11_2_auto(experiment_id),
                    })
                except Exception as exc:
                    cycle.append({
                        "experiment_id": experiment_id,
                        "stage": "v11_2_three_year",
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    })

            _state["last_cycle"] = cycle
            _state["processed_experiments"] = len(cycle)
            _state["blocked_experiments"] = sum(
                1 for x in cycle if x.get("status") == "blocked"
            )
            _state["completed_experiments"] = sum(
                1 for x in cycle if x.get("status") == "completed"
            )
            return {"status": "completed", "experiments": cycle}
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
        "input_layout": {
            "<experiment_id>/trades.csv": "v1/v3/v4",
            "<experiment_id>/deals.csv": "v5/v6/v7/v8/v11",
            "<experiment_id>/market.csv": "v3/v4/v5/v6/v7/v8/v11",
            "<experiment_id>/is_deals.csv": "v9 2024 IS",
            "<experiment_id>/is_market.csv": "v9 2024 IS market",
            "<experiment_id>/oos_deals.csv": "v9 2025 OOS",
            "<experiment_id>/oos_market.csv": "v9 2025 OOS market",
        },
    }


async def run_now() -> dict[str, Any]:
    return await run_cycle()
