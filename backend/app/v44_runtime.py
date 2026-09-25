"""
V44.6 Autonomous Runtime Controller

Architecture:

    V44 deterministic controller
          |
          +-- hard daily AI budget
          +-- per-cycle AI budget
          +-- cooldown
          +-- duplicate protection
          +-- posting limits
          |
          v
    Existing interaction pipeline
          |
          v
    Existing AI / Moltbook logic
          |
          v
    Existing verification system

IMPORTANT:
    External calls are OFF by default.

    Set:
        V44_EXTERNAL_CALLS_ENABLED=1

    only after the runtime has been inspected and the deployment
    environment is intentionally ready.

This module does not create a second OpenRouter client.
It does not bypass V43.6 verification.
"""

from __future__ import annotations

import inspect
import json
import os
import time
from typing import Any


VERSION = "V44.6"


def external_enabled() -> bool:
    return os.getenv(
        "V44_EXTERNAL_CALLS_ENABLED",
        "0",
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def runtime_mode() -> str:
    return (
        "LIVE"
        if external_enabled()
        else "SAFE"
    )


def _load_controller():
    try:
        from . import v44_autonomous
        return v44_autonomous
    except Exception as exc:
        return {
            "error": repr(exc),
        }


def _load_interactions():
    try:
        from .api import interactions
        return interactions
    except Exception as exc:
        return {
            "error": repr(exc),
        }


def _callable(obj: Any, name: str):
    if isinstance(obj, dict):
        return None

    value = getattr(obj, name, None)

    if callable(value):
        return value

    return None


def _function_info(fn: Any) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)

        required = []

        for p in sig.parameters.values():

            if p.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue

            if p.default is inspect.Parameter.empty:
                required.append(p.name)

        return {
            "name": getattr(fn, "__name__", None),
            "signature": str(sig),
            "required_parameters": required,
            "callable": True,
        }

    except Exception as exc:
        return {
            "name": getattr(fn, "__name__", None),
            "signature": None,
            "required_parameters": [],
            "callable": True,
            "inspection_error": repr(exc),
        }


def discover() -> dict[str, Any]:
    controller = _load_controller()
    interactions = _load_interactions()

    result = {
        "version": VERSION,
        "mode": runtime_mode(),
        "external_calls_enabled": external_enabled(),
        "controller": {},
        "interactions": {},
    }

    if isinstance(controller, dict):
        result["controller"]["import_error"] = controller["error"]
    else:
        for name in (
            "DailyBudget",
            "choose_activity",
            "plan_cycle",
            "posting_gate",
            "status",
            "self_test",
        ):
            fn = getattr(controller, name, None)

            if fn is not None:
                if callable(fn):
                    result["controller"][name] = (
                        _function_info(fn)
                    )
                else:
                    result["controller"][name] = {
                        "type": type(fn).__name__
                    }

    if isinstance(interactions, dict):
        result["interactions"]["import_error"] = (
            interactions["error"]
        )
    else:
        for name in (
            "scan",
            "cycle",
            "leads",
            "approve",
            "publish_lead",
            "feedback",
        ):
            fn = getattr(interactions, name, None)

            if fn is not None:
                result["interactions"][name] = (
                    _function_info(fn)
                )

    return result


def _snapshot_budget(controller) -> dict[str, Any]:
    try:
        budget = controller.DailyBudget()

        snap = budget.snapshot()

        if isinstance(snap, dict):
            return snap

        return {
            "snapshot": snap,
        }

    except Exception as exc:
        return {
            "error": repr(exc),
        }


def plan_safe_cycle() -> dict[str, Any]:
    """
    Plan only.

    No external API call.
    No AI request.
    No Moltbook request.
    """

    controller = _load_controller()

    if isinstance(controller, dict):
        return {
            "status": "controller_import_error",
            "error": controller["error"],
        }

    try:
        planner = getattr(
            controller,
            "plan_cycle",
            None,
        )

        if callable(planner):

            plan = planner()

            return {
                "status": "planned",
                "mode": runtime_mode(),
                "external_calls": False,
                "plan": plan,
            }

        chooser = getattr(
            controller,
            "choose_activity",
            None,
        )

        if callable(chooser):

            activity = chooser()

            return {
                "status": "planned",
                "mode": runtime_mode(),
                "external_calls": False,
                "activity": activity,
            }

        return {
            "status": "no_planner",
            "mode": runtime_mode(),
            "external_calls": False,
        }

    except Exception as exc:
        return {
            "status": "planning_error",
            "mode": runtime_mode(),
            "external_calls": False,
            "error": repr(exc),
        }


def _invoke_existing_cycle():
    """
    Invoke the EXISTING cycle only when:

        V44_EXTERNAL_CALLS_ENABLED=1

    The function is inspected first.

    If required parameters exist, the runtime refuses to guess
    their values. This prevents accidental or malformed calls.
    """

    if not external_enabled():
        return {
            "status": "safe_mode",
            "message": (
                "External calls are disabled. "
                "Cycle was not executed."
            ),
        }

    interactions = _load_interactions()

    if isinstance(interactions, dict):
        return {
            "status": "interaction_import_error",
            "error": interactions["error"],
        }

    fn = _callable(
        interactions,
        "cycle",
    )

    if fn is None:
        return {
            "status": "cycle_not_found",
        }

    info = _function_info(fn)

    required = info.get(
        "required_parameters",
        [],
    )

    if required:
        return {
            "status": "cycle_requires_parameters",
            "required_parameters": required,
            "signature": info.get("signature"),
            "executed": False,
        }

    try:

        result = fn()

        if inspect.isawaitable(result):
            # The runtime is intentionally conservative.
            # Async execution belongs to the FastAPI event loop.
            return {
                "status": "async_cycle_detected",
                "executed": False,
                "message": (
                    "Existing cycle is async. "
                    "Use the FastAPI route/event loop rather "
                    "than executing it from synchronous code."
                ),
            }

        return {
            "status": "cycle_executed",
            "executed": True,
            "result": result,
        }

    except Exception as exc:

        return {
            "status": "cycle_failed",
            "executed": False,
            "error": repr(exc),
        }


def run_once() -> dict[str, Any]:
    """
    One autonomous iteration.

    SAFE mode:
        planning only.

    LIVE mode:
        may call the existing cycle, but never guesses
        required parameters.
    """

    started = time.time()

    discovery = discover()
    plan = plan_safe_cycle()

    if runtime_mode() == "SAFE":

        return {
            "status": "safe_planned",
            "version": VERSION,
            "mode": "SAFE",
            "discovery": discovery,
            "plan": plan,
            "external_calls": False,
            "elapsed_seconds": round(
                time.time() - started,
                3,
            ),
        }

    result = _invoke_existing_cycle()

    return {
        "status": result.get(
            "status",
            "completed",
        ),
        "version": VERSION,
        "mode": "LIVE",
        "discovery": discovery,
        "plan": plan,
        "execution": result,
        "elapsed_seconds": round(
            time.time() - started,
            3,
        ),
    }


def health() -> dict[str, Any]:
    controller = _load_controller()

    controller_ok = not isinstance(
        controller,
        dict,
    )

    interactions = _load_interactions()

    interactions_ok = not isinstance(
        interactions,
        dict,
    )

    return {
        "version": VERSION,
        "mode": runtime_mode(),
        "external_calls_enabled": external_enabled(),
        "controller_import": controller_ok,
        "interactions_import": interactions_ok,
        "timestamp": int(time.time()),
    }


if __name__ == "__main__":

    print(
        json.dumps(
            health(),
            indent=2,
            ensure_ascii=False,
        )
    )

    print()

    print(
        json.dumps(
            discover(),
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )

    print()

    print(
        json.dumps(
            run_once(),
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
