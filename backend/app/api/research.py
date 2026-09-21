import os, uuid, traceback
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks
from ..storage import exp_dir, copy_upload, load_state, save_state
from ..research import run_research

router=APIRouter()

def _run_job(exp_id):
    state=load_state(exp_id)
    state["status"]="running"
    save_state(exp_id,state)
    try:
        d=exp_dir(exp_id)
        result=run_research(str(d/"ea.mq5"),str(d/"backtest.csv"),str(d/"bars.csv"))
        state.update({"status":"completed","result":result,"error":None})
    except Exception as e:
        state.update({"status":"failed","error":str(e),"traceback":traceback.format_exc()})
    save_state(exp_id,state)

@router.post("/data/upload")
async def upload_data(
    experiment_id: str = Form(...),
    ea_file: UploadFile = File(...),
    backtest_file: UploadFile = File(...),
    bars_file: UploadFile = File(...)
):
    d=exp_dir(experiment_id)
    for old in ["ea.mq5","backtest.csv","bars.csv"]:
        p=d/old
        if p.exists(): p.unlink()

    for upload,name in [(ea_file,"ea.mq5"),(backtest_file,"backtest.csv"),(bars_file,"bars.csv")]:
        data=await upload.read()
        (d/name).write_bytes(data)

    state={
        "experiment_id":experiment_id,
        "status":"uploaded",
        "files":{
            "ea":ea_file.filename,
            "backtest":backtest_file.filename,
            "bars":bars_file.filename
        },
        "result":None,
        "error":None
    }
    save_state(experiment_id,state)
    return state

@router.post("/{experiment_id}/run")
async def run(experiment_id:str, background_tasks:BackgroundTasks):
    state=load_state(experiment_id)
    if state.get("status")=="missing":
        raise HTTPException(404,"Experiment not found. Upload EA + Backtest + M30 Bars first.")
    if state.get("status")=="running":
        return {"experiment_id":experiment_id,"status":"running"}
    background_tasks.add_task(_run_job,experiment_id)
    state["status"]="queued"
    save_state(experiment_id,state)
    return {
        "experiment_id":experiment_id,
        "status":"queued",
        "message":"Research started in background. Poll GET /api/research/{experiment_id}.",
        "scope":["EA","Backtest","M30"]
    }

@router.get("/{experiment_id}")
def get_result(experiment_id:str):
    state=load_state(experiment_id)
    if state.get("status")=="missing":
        raise HTTPException(404,"Experiment not found.")
    return state
