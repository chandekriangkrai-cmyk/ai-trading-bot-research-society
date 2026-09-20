from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.database import SessionLocal
from app.ea_files import EAFile
from app.models import Mission
from app import research_auto_orchestrator

router = APIRouter(prefix="/research", tags=["One Click Research"])

INPUT_ROOT = Path(os.getenv("RESEARCH_INPUT_ROOT", "research_inputs")).resolve()
MAX_FILE_BYTES = int(os.getenv("RESEARCH_UPLOAD_MAX_MB", "100")) * 1024 * 1024


async def _read_limited(upload: UploadFile, *, allow_ext: tuple[str, ...]) -> bytes:
    if not upload or not upload.filename:
        raise HTTPException(400, "Missing uploaded file.")

    if not upload.filename.lower().endswith(allow_ext):
        raise HTTPException(400, f"{upload.filename}: unsupported file type.")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise HTTPException(413, f"{upload.filename}: file exceeds upload limit.")
        chunks.append(chunk)

    if not chunks:
        raise HTTPException(400, f"{upload.filename}: empty file.")
    return b"".join(chunks)


async def _save_csv(upload: Optional[UploadFile], destination: Path) -> dict | None:
    if upload is None:
        return None
    data = await _read_limited(upload, allow_ext=(".csv",))
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".uploading")
    tmp.write_bytes(data)
    tmp.replace(destination)
    return {"filename": upload.filename, "saved_as": destination.name, "bytes": len(data)}


@router.post("/start")
async def start_research(
    mission_id: str = Form(...),
    ea: UploadFile = File(...),
    trades: Optional[UploadFile] = File(None),
    market: Optional[UploadFile] = File(None),
    deals: Optional[UploadFile] = File(None),
    is_deals: Optional[UploadFile] = File(None),
    is_market: Optional[UploadFile] = File(None),
    oos_deals: Optional[UploadFile] = File(None),
    oos_market: Optional[UploadFile] = File(None),
) -> dict:
    """Single-action research intake.

    Upload EA + any available MT5/market CSVs. The background orchestrator
    creates/reuses the research chain and runs the available stages.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", mission_id):
        raise HTTPException(400, "Invalid mission_id.")

    ea_bytes = await _read_limited(ea, allow_ext=(".mq5", ".mq4"))

    db = SessionLocal()
    try:
        mission = db.get(Mission, mission_id)
        if not mission:
            raise HTTPException(404, "Mission not found.")

        filename = ea.filename or "EA.mq5"
        source = ea_bytes.decode("utf-8-sig", errors="replace")
        line_count = len(source.splitlines())

        ea_row = EAFile(
            id=str(uuid4()),
            mission_id=mission_id,
            filename=filename,
            file_type=Path(filename).suffix.lstrip(".").lower() or "mq5",
            source_code=source,
            line_count=line_count,
            analysis_status="uploaded",
        )
        db.add(ea_row)
        db.commit()
        db.refresh(ea_row)
        ea_id = str(ea_row.id)
    finally:
        db.close()

    # Create a deterministic intake folder keyed by the EA/mission upload.
    # The orchestrator will discover the DB chain and continue from here.
    intake_key = f"mission-{mission_id}"
    folder = INPUT_ROOT / intake_key
    folder.mkdir(parents=True, exist_ok=True)

    saved: dict = {}
    for field, upload, filename in (
        ("trades", trades, "trades.csv"),
        ("market", market, "market.csv"),
        ("deals", deals, "deals.csv"),
        ("is_deals", is_deals, "is_deals.csv"),
        ("is_market", is_market, "is_market.csv"),
        ("oos_deals", oos_deals, "oos_deals.csv"),
        ("oos_market", oos_market, "oos_market.csv"),
    ):
        result = await _save_csv(upload, folder / filename)
        if result:
            saved[field] = result

    # The existing orchestrator works on Experiment IDs. Ensure the chain now,
    # then move the uploaded data folder to the resulting experiment ID.
    created = await research_auto_orchestrator.ensure_research_chain_for_mission(
        mission_id
    )

    if not created:
        raise HTTPException(
            409,
            "A research experiment already exists for this mission. "
            "Use the existing experiment intake endpoint or create a new mission "
            "for a new research run.",
        )

    experiment_id = created[-1]
    experiment_folder = INPUT_ROOT / experiment_id
    experiment_folder.mkdir(parents=True, exist_ok=True)

    for path in folder.glob("*.csv"):
        path.replace(experiment_folder / path.name)

    try:
        folder.rmdir()
    except OSError:
        pass

    result = await research_auto_orchestrator.run_now()

    return {
        "status": "started",
        "mission_id": mission_id,
        "ea_file_id": ea_id,
        "experiment_id": experiment_id,
        "uploaded": saved,
        "orchestrator": result,
        "message": "Research started. The background worker will continue automatically.",
    }
