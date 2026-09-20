from contextlib import asynccontextmanager
import asyncio
import inspect
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    agents,
    health,
    missions,
    tasks,
    ea_files,
    research,
    research_runner,
    research_runner_v3,
    research_runner_v4,
    research_runner_v5,
    research_runner_v6,
    research_runner_v7,
    research_runner_v8,
    research_runner_v9,
    research_runner_v10,
)

from app.config import settings
from app.database import Base, engine

# ลงทะเบียนโมเดล EAFile ให้ SQLAlchemy รู้จัก
from app.ea_files import EAFile


logger = logging.getLogger("auto_runner")

AUTO_RUN_ENABLED = os.getenv("AUTO_RUN_ENABLED", "true").lower() in {
    "1", "true", "yes", "on"
}
AUTO_RUN_INTERVAL = max(5, int(os.getenv("AUTO_RUN_INTERVAL", "30")))

_auto_runner_task: asyncio.Task | None = None
_auto_runner_running = False


async def _call_runner_function():
    """เรียกฟังก์ชัน auto-run ของ runner ล่าสุด ถ้ามี โดยไม่แก้ API เดิม."""
    global _auto_runner_running

    if _auto_runner_running:
        return

    candidates = (
        "auto_run", "run_auto", "run_once", "process_next_task",
        "execute_next_task", "run_pending", "run_next", "run_cycle",
    )
    runner = next(
        (getattr(research_runner_v10, name)
         for name in candidates
         if callable(getattr(research_runner_v10, name, None))),
        None,
    )

    if runner is None:
        logger.warning(
            "AUTO RUN เปิดอยู่ แต่ research_runner_v10 ยังไม่มี "
            "ฟังก์ชัน auto-run ที่รองรับ"
        )
        return

    _auto_runner_running = True
    try:
        result = runner()
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Auto Runner ทำงานผิดพลาด")
    finally:
        _auto_runner_running = False


async def _auto_runner_loop():
    """วนตรวจ/สั่ง runner อัตโนมัติจนกว่า FastAPI จะ shutdown."""
    logger.info("AUTO RUN STARTED | interval=%ss", AUTO_RUN_INTERVAL)
    while True:
        try:
            await _call_runner_function()
        except asyncio.CancelledError:
            logger.info("AUTO RUN STOPPED")
            raise
        except Exception:
            logger.exception("AUTO RUN LOOP ERROR")
        await asyncio.sleep(AUTO_RUN_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _auto_runner_task

    Base.metadata.create_all(bind=engine)

    if AUTO_RUN_ENABLED:
        _auto_runner_task = asyncio.create_task(_auto_runner_loop())
        logger.info("Auto Runner ถูกเปิดใช้งานแล้ว")
    else:
        logger.info("Auto Runner ถูกปิดด้วย AUTO_RUN_ENABLED")

    try:
        yield
    finally:
        if _auto_runner_task is not None:
            _auto_runner_task.cancel()
            try:
                await _auto_runner_task
            except asyncio.CancelledError:
                pass
            _auto_runner_task = None


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Backend foundation for the AI Trading Bot "
        "Research Society."
    ),
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.cors_origins.split(",")
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# API Routers
# =========================================================

app.include_router(health.router, prefix="/api")
app.include_router(agents.router, prefix="/api")
app.include_router(missions.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(ea_files.router, prefix="/api")
app.include_router(research.router, prefix="/api")
app.include_router(research_runner.router, prefix="/api")
app.include_router(research_runner_v3.router, prefix="/api")
app.include_router(research_runner_v4.router, prefix="/api")
app.include_router(research_runner_v5.router, prefix="/api")
app.include_router(research_runner_v6.router, prefix="/api")
app.include_router(research_runner_v7.router, prefix="/api")
app.include_router(research_runner_v8.router, prefix="/api")
app.include_router(research_runner_v9.router, prefix="/api")
app.include_router(research_runner_v10.router, prefix="/api")


# =========================================================
# Auto Runner Status
# =========================================================

@app.get("/api/auto-run/status", tags=["System"])
def auto_run_status() -> dict[str, object]:
    return {
        "enabled": AUTO_RUN_ENABLED,
        "running": _auto_runner_task is not None and not _auto_runner_task.done(),
        "interval_seconds": AUTO_RUN_INTERVAL,
        "current_job_running": _auto_runner_running,
        "runner": "research_runner_v10",
    }


# =========================================================
# Root Endpoint
# =========================================================

@app.get("/", tags=["System"])
def root() -> dict[str, str]:
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "status": "running",
    }
