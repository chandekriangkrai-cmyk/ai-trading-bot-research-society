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
