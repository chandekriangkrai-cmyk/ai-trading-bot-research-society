from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import agents, health, missions, tasks
from app.config import settings
from app.database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create database tables when the application starts.
    # This includes the research_tasks table.
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
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------
# API Routers
# -------------------------

app.include_router(
    health.router,
    prefix="/api",
)

app.include_router(
    agents.router,
    prefix="/api",
)

app.include_router(
    missions.router,
    prefix="/api",
)

app.include_router(
    tasks.router,
    prefix="/api",
)


# -------------------------
# Root Endpoint
# -------------------------

@app.get("/", tags=["System"])
def root() -> dict[str, str]:
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "status": "running",
    }
