"""
V44.8 Autonomous FastAPI Gate

SAFE BY DEFAULT.

This router exposes only planning/status operations initially.

No Moltbook post is made by these endpoints.
No OpenRouter request is made by these endpoints.

The purpose is to establish a stable FastAPI integration boundary
before enabling autonomous execution.
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter

from .. import v44_autonomous as controller


router = APIRouter(
    prefix="/v44-autonomous",
    tags=["V44 Autonomous Agent"],
)


def _external_enabled() -> bool:
    return os.getenv(
        "V44_EXTERNAL_CALLS_ENABLED",
        "0",
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@router.get("/status")
def autonomous_status() -> dict[str, Any]:
    """
    Return deterministic controller status.

    No external call.
    """

    try:
        status = controller.status()
    except Exception as exc:
        status = {
            "status_error": repr(exc),
        }

    return {
        "version": "V44.8",
        "mode": (
            "LIVE"
            if _external_enabled()
            else "SAFE"
        ),
        "external_calls_enabled": _external_enabled(),
        "timestamp": int(time.time()),
        "controller": status,
    }


@router.get("/plan")
def autonomous_plan() -> dict[str, Any]:
    """
    Plan one autonomous activity.

    This endpoint does NOT execute the activity.
    """

    try:
        plan = controller.plan_cycle()

        return {
            "status": "planned",
            "version": "V44.8",
            "external_calls": False,
            "plan": plan,
        }

    except Exception as exc:

        return {
            "status": "planning_error",
            "version": "V44.8",
            "external_calls": False,
            "error": repr(exc),
        }


@router.post("/tick")
def autonomous_safe_tick() -> dict[str, Any]:
    """
    Execute exactly one V44 engine tick in SAFE mode.

    HARD SAFETY CONTRACT:
    - V44_ENABLED must be false
    - V44_DRY_RUN must be true
    - V44_EXTERNAL_CALLS_ENABLED must be false
    - no AI request
    - no Moltbook request
    - no autonomous posting
    - no production activation

    This endpoint exists only to prove that the actual V44
    engine tick reaches the engine safely without external calls.
    """

    enabled = os.getenv(
        "V44_ENABLED",
        "0",
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    dry_run = os.getenv(
        "V44_DRY_RUN",
        "1",
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    external_enabled = _external_enabled()

    # ----------------------------------------------------------
    # HARD ROUTER FIREWALL
    # ----------------------------------------------------------
    # The endpoint itself refuses to execute unless the
    # environment is explicitly in SAFE mode.
    #
    # This prevents an accidental deployment configuration
    # from turning /tick into a live execution endpoint.
    # ----------------------------------------------------------

    if enabled or not dry_run or external_enabled:
        return {
            "status": "blocked",
            "version": "V44.27",
            "external_calls": False,
            "ai_requests": 0,
            "moltbook_requests": 0,
            "reason": (
                "SAFE TICK requires "
                "V44_ENABLED=0, "
                "V44_DRY_RUN=1, "
                "V44_EXTERNAL_CALLS_ENABLED=0"
            ),
            "gates": {
                "V44_ENABLED": enabled,
                "V44_DRY_RUN": dry_run,
                "V44_EXTERNAL_CALLS_ENABLED": external_enabled,
            },
        }

    try:
        from ..v44_engine import V44Engine

        engine = V44Engine()

        # ------------------------------------------------------
        # SECOND FIREWALL
        # ------------------------------------------------------
        # Do not allow stale SQLite state to activate the engine.
        # The engine startup must remain environment-authoritative.
        # ------------------------------------------------------

        if engine.enabled:
            return {
                "status": "blocked",
                "version": "V44.27",
                "external_calls": False,
                "ai_requests": 0,
                "moltbook_requests": 0,
                "reason": (
                    "Engine startup reported enabled=True "
                    "while SAFE environment is active."
                ),
            }

        result = engine.tick()

        return {
            "status": "safe_tick_completed",
            "version": "V44.27",
            "external_calls": False,
            "ai_requests": 0,
            "moltbook_requests": 0,
            "production": False,
            "engine_enabled": engine.enabled,
            "result": result,
            "gates": {
                "V44_ENABLED": False,
                "V44_DRY_RUN": True,
                "V44_EXTERNAL_CALLS_ENABLED": False,
            },
        }

    except Exception as exc:
        return {
            "status": "safe_tick_error",
            "version": "V44.27",
            "external_calls": False,
            "ai_requests": 0,
            "moltbook_requests": 0,
            "production": False,
            "error": repr(exc),
        }


@router.post("/self-test")
def autonomous_self_test() -> dict[str, Any]:
    """
    Run deterministic V44 controller self-test.

    No external calls.
    """

    try:
        result = controller.self_test()

        return {
            "status": "completed",
            "version": "V44.8",
            "external_calls": False,
            "result": result,
        }

    except Exception as exc:

        return {
            "status": "failed",
            "version": "V44.8",
            "external_calls": False,
            "error": repr(exc),
        }
