from contextlib import asynccontextmanager

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
    research_runner_v11,
    research_runner_v11_2,
)

from app.config import settings
from app.database import Base, engine

# ลงทะเบียนโมเดล EAFile ให้ SQLAlchemy รู้จัก
from app.ea_files import EAFile

from app import research_auto_orchestrator_auto as research_auto_orchestrator
from app import research_input_upload


print("[startup] AI Trading Research Society booting", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[startup] creating database tables", flush=True)
    Base.metadata.create_all(bind=engine)

    print("[startup] starting research auto orchestrator", flush=True)
    research_auto_orchestrator.start()
    print(
        f"[startup] research auto orchestrator status: "
        f"{research_auto_orchestrator.status()}",
        flush=True,
    )

    try:
        yield
    finally:
        print("[shutdown] stopping research auto orchestrator", flush=True)
        await research_auto_orchestrator.stop()
        print("[shutdown] research auto orchestrator stopped", flush=True)


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
app.include_router(research_runner_v11.router, prefix="/api")
app.include_router(research_runner_v11_2.router, prefix="/api")

# One-click CSV upload for the full research pipeline.
app.include_router(
    research_input_upload.router,
    prefix="/api",
)


# =========================================================
# FULL RESEARCH AUTO PIPELINE
# =========================================================

@app.get("/api/auto-run/status", tags=["System"])
async def auto_run_status() -> dict:
    return research_auto_orchestrator.status()


@app.post("/api/auto-run/run-now", tags=["System"])
async def auto_run_now() -> dict:
    return await research_auto_orchestrator.run_now()


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
