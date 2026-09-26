from contextlib import asynccontextmanager
import threading
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import Base, engine
from app import models, research_models, ea_files
from app.api import (health, agents, moltbook, interactions, discussion, v44_autonomous, v44_relationships)
from app import unified_research, discussion_watcher
from app.v44_engine import V44Engine

# V44.13 autonomous engine singleton.
v44_engine = V44Engine()


def _start_v44_worker():
    try:
        from app.v44_config import V44_ENABLED_DEFAULT

        if V44_ENABLED_DEFAULT:
            v44_engine.ensure_worker()
            print(
                "[V44.13] autonomous worker started",
                flush=True,
            )
        else:
            print(
                "[V44.13] autonomous worker disabled",
                flush=True,
            )
    except Exception as exc:
        print(
            f"[V44.13] worker startup blocked: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def _background_recovery():
    # Recovery can scan many experiment folders. Do not block Render's startup
    # probe while doing that work; the API must become reachable first.
    try:
        unified_research.recover_research_state()
    except Exception as exc:
        print(f"[startup-recovery] {type(exc).__name__}: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Keep startup lightweight so Render can pass its health check promptly.
    # Table creation is normally fast; recovery is deliberately moved out of
    # the critical startup path.
    Base.metadata.create_all(bind=engine)
    t = threading.Thread(target=_background_recovery, name="research-recovery", daemon=True)
    t.start()
    discussion_watcher.start_if_enabled()
    discussion_watcher.start_interaction_if_enabled()

    # V44 autonomous worker startup.
    # Environment V44_ENABLED remains the authoritative master switch.
    _start_v44_worker()

    yield

app=FastAPI(title=settings.app_name,version=settings.app_version,description="Unified EA + MT5 Backtest Research Engine",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[x.strip() for x in settings.cors_origins.split(",")],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
app.include_router(health.router,prefix="/api")
app.include_router(agents.router,prefix="/api")
app.include_router(unified_research.router,prefix="/api")
app.include_router(moltbook.router,prefix="/api")
app.include_router(interactions.router,prefix="/api")
app.include_router(discussion.router, prefix="/api")
app.include_router(v44_autonomous.router, prefix="/api")
app.include_router(v44_relationships.router, prefix="/api")
@app.get("/",tags=["System"])
def root(): return {"service":settings.app_name,"version":settings.app_version,"status":"running","docs":"/docs","research_flow":["upload","run","inspect","publish"]}
