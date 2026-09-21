from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import Base, engine
from app import models, research_models, ea_files
from app.api import health, agents, moltbook
from app import unified_research

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield

app=FastAPI(title=settings.app_name,version="2.0.0",description="Unified EA + MT5 Backtest Research Engine",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[x.strip() for x in settings.cors_origins.split(",")],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
app.include_router(health.router,prefix="/api")
app.include_router(agents.router,prefix="/api")
app.include_router(unified_research.router,prefix="/api")
app.include_router(moltbook.router,prefix="/api")
@app.get("/",tags=["System"])
def root(): return {"service":settings.app_name,"version":"2.0.0","status":"running","docs":"/docs","research_flow":["upload","run","inspect","publish"]}
