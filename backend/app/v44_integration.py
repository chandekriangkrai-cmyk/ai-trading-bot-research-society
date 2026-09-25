"""
V44.1 Autonomous Integration Adapter

Purpose:
    Connect the deterministic V44 controller to the existing
    Moltbook / Research Discussion system without duplicating
    the V43.6 verification solver.

Important:
    This module performs NO external API calls by itself.

    It is an adapter/state boundary for the next runtime layer.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Optional


VERSION = "V44.1"


@dataclass
class IntegrationStatus:
    version: str
    controller_available: bool
    moltbook_module_available: bool
    research_module_available: bool
    solver_available: bool
    external_calls_enabled: bool
    status: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _db_path() -> Path:
    value = os.getenv(
        "V44_DB_PATH",
        str(_repo_root() / "backend" / "data" / "v44_autonomous.db"),
    )
    return Path(value)


def _safe_imports() -> dict[str, Any]:
    result: dict[str, Any] = {}

    try:
        from . import v44_autonomous
        result["controller"] = v44_autonomous
    except Exception as exc:
        result["controller_error"] = repr(exc)

    try:
        from .api import moltbook
        result["moltbook"] = moltbook
    except Exception as exc:
        result["moltbook_error"] = repr(exc)

    try:
        from . import research_discussion
        result["research"] = research_discussion
    except Exception as exc:
        result["research_error"] = repr(exc)

    return result


def discover_functions(module: Any) -> list[str]:
    if module is None:
        return []

    names: list[str] = []

    for name, value in inspect.getmembers(module):
        if name.startswith("_"):
            continue

        if inspect.isfunction(value):
            names.append(name)

    return sorted(names)


def discover_routes(module: Any) -> list[dict[str, Any]]:
    """
    Discover FastAPI/Starlette route-like objects without invoking them.
    """

    routes: list[dict[str, Any]] = []

    if module is None:
        return routes

    router_objects = []

    for attr_name in ("router", "app"):
        obj = getattr(module, attr_name, None)
        if obj is not None:
            router_objects.append(obj)

    for router in router_objects:
        for route in getattr(router, "routes", []) or []:
            path = getattr(route, "path", None)

            methods = getattr(route, "methods", None)

            endpoint = getattr(route, "endpoint", None)
            endpoint_name = getattr(endpoint, "__name__", None)

            if path:
                routes.append(
                    {
                        "path": str(path),
                        "methods": sorted(list(methods or [])),
                        "endpoint": endpoint_name,
                    }
                )

    return routes


def solver_available(module: Any) -> bool:
    if module is None:
        return False

    return callable(getattr(module, "_solve_challenge", None))


def integration_status() -> IntegrationStatus:
    modules = _safe_imports()

    controller = modules.get("controller")
    moltbook = modules.get("moltbook")
    research = modules.get("research")

    external_calls = os.getenv(
        "V44_EXTERNAL_CALLS_ENABLED",
        "0",
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    return IntegrationStatus(
        version=VERSION,
        controller_available=controller is not None,
        moltbook_module_available=moltbook is not None,
        research_module_available=research is not None,
        solver_available=solver_available(moltbook),
        external_calls_enabled=external_calls,
        status="ready" if (
            controller is not None
            and moltbook is not None
            and research is not None
            and solver_available(moltbook)
        ) else "inspection_only",
    )


def inspect_system() -> dict[str, Any]:
    modules = _safe_imports()

    controller = modules.get("controller")
    moltbook = modules.get("moltbook")
    research = modules.get("research")

    result: dict[str, Any] = {
        "version": VERSION,
        "timestamp": int(time.time()),
        "status": asdict(integration_status()),
        "functions": {
            "controller": discover_functions(controller),
            "moltbook": discover_functions(moltbook),
            "research_discussion": discover_functions(research),
        },
        "routes": {
            "moltbook": discover_routes(moltbook),
            "research_discussion": discover_routes(research),
        },
    }

    errors = {}

    for key in (
        "controller_error",
        "moltbook_error",
        "research_error",
    ):
        if key in modules:
            errors[key] = modules[key]

    if errors:
        result["import_errors"] = errors

    return result


def controller_db_exists() -> bool:
    return _db_path().exists()


def read_agent_status() -> dict[str, Any]:
    """
    Read only V44 local state.

    This function intentionally does not contact Moltbook,
    OpenRouter, Render, or any external service.
    """

    db = _db_path()

    if not db.exists():
        return {
            "status": "database_not_created",
            "path": str(db),
        }

    result: dict[str, Any] = {
        "status": "ok",
        "path": str(db),
    }

    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        tables = [
            "ai_budget",
            "activities",
            "opportunities",
            "reply_history",
            "research_memory",
            "agent_state",
        ]

        counts = {}

        for table in tables:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table}"
                ).fetchone()
                counts[table] = int(row["c"])
            except Exception:
                counts[table] = None

        result["counts"] = counts
        conn.close()

    except Exception as exc:
        result["status"] = "read_error"
        result["error"] = repr(exc)

    return result


def external_calls_allowed() -> bool:
    return os.getenv(
        "V44_EXTERNAL_CALLS_ENABLED",
        "0",
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def require_external_calls_enabled() -> None:
    if not external_calls_allowed():
        raise RuntimeError(
            "V44 external calls are disabled. "
            "Set V44_EXTERNAL_CALLS_ENABLED=1 only when the "
            "autonomous runtime is intentionally enabled."
        )


if __name__ == "__main__":
    import json

    print(
        json.dumps(
            inspect_system(),
            indent=2,
            ensure_ascii=False,
        )
    )

    print()
    print(
        json.dumps(
            read_agent_status(),
            indent=2,
            ensure_ascii=False,
        )
    )
