from fastapi import FastAPI
from app.api.research import router

app = FastAPI(title="Fx Moly Research Engine", version="1.0.0")
app.include_router(router, prefix="/api/research")

@app.get("/")
def root():
    return {"service":"Fx Moly Research Engine","version":"1.0.0","focus":"EA + Backtest + M30 Market Regime Research"}

@app.get("/health")
def health():
    return {"status":"ok"}
