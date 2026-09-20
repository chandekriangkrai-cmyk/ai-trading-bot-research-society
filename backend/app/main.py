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
)

from app.config import settings
from app.database import Base, engine

# ลงทะเบียนโมเดล EAFile ให้ SQLAlchemy รู้จัก
from app.ea_files import EAFile


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


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
