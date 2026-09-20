from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile

from app.database import SessionLocal
from app.research_models import Experiment
from app import research_auto_orchestrator_auto as research_auto_orchestrator


router = APIRouter(
    prefix="/research-inputs",
    tags=["Research CSV Upload"],
)

INPUT_ROOT = Path(
    os.getenv("RESEARCH_INPUT_ROOT", "research_inputs")
).resolve()

MAX_FILE_BYTES = int(
    os.getenv("RESEARCH_UPLOAD_MAX_MB", "100")
) * 1024 * 1024

ALLOWED_FIELDS = {
    "trades",
    "market",
    "deals",
    "is_deals",
    "is_market",
    "oos_deals",
    "oos_market",
}

FIELD_TO_FILENAME = {
    "trades": "trades.csv",
    "market": "market.csv",
    "deals": "deals.csv",
    "is_deals": "is_deals.csv",
    "is_market": "is_market.csv",
    "oos_deals": "oos_deals.csv",
    "oos_market": "oos_market.csv",
}


def _safe_experiment_id(value: str) -> str:
    if not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}",
        value,
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid experiment_id.",
        )
    return value



OptionalUpload = Optional[Union[UploadFile, str]]


def _normalize_upload(upload: OptionalUpload) -> Optional[UploadFile]:
    # Swagger/OpenAPI may submit an empty string for an untouched optional
    # file input. Treat that as no upload.
    if upload is None:
        return None
    if isinstance(upload, str):
        if not upload.strip():
            return None
        raise HTTPException(status_code=400, detail="Invalid optional file upload.")
    return upload


async def _save_csv(
    upload: UploadFile,
    destination: Path,
) -> dict:
    if not upload.filename:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file has no filename.",
        )

    if not upload.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=400,
            detail=f"{upload.filename}: only CSV files are allowed.",
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    tmp = destination.with_suffix(".uploading")

    total = 0
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break

                total += len(chunk)

                if total > MAX_FILE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"{upload.filename}: file exceeds "
                            f"{MAX_FILE_BYTES // (1024 * 1024)} MB limit."
                        ),
                    )

                out.write(chunk)

        if total == 0:
            raise HTTPException(
                status_code=400,
                detail=f"{upload.filename}: empty CSV.",
            )

        tmp.replace(destination)

        return {
            "saved_as": destination.name,
            "original_filename": upload.filename,
            "bytes": total,
        }

    except HTTPException:
        if tmp.exists():
            tmp.unlink()
        raise
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _experiment_exists(experiment_id: str) -> Experiment:
    db = SessionLocal()
    try:
        experiment = (
            db.query(Experiment)
            .filter(Experiment.id == experiment_id)
            .first()
        )

        if not experiment:
            raise HTTPException(
                status_code=404,
                detail="Experiment not found.",
            )

        return experiment
    finally:
        db.close()


def _folder(experiment_id: str) -> Path:
    return INPUT_ROOT / experiment_id


@router.get("/{experiment_id}")
def input_status(experiment_id: str) -> dict:
    trades = _normalize_upload(trades)
    market = _normalize_upload(market)
    deals = _normalize_upload(deals)
    is_deals = _normalize_upload(is_deals)
    is_market = _normalize_upload(is_market)
    oos_deals = _normalize_upload(oos_deals)
    oos_market = _normalize_upload(oos_market)

    experiment_id = _safe_experiment_id(experiment_id)
    _experiment_exists(experiment_id)

    folder = _folder(experiment_id)

    files = {}
    for field, filename in FIELD_TO_FILENAME.items():
        path = folder / filename
        files[field] = {
            "filename": filename,
            "exists": path.is_file(),
            "bytes": path.stat().st_size if path.is_file() else 0,
        }

    return {
        "experiment_id": experiment_id,
        "input_folder": str(folder),
        "files": files,
    }


@router.post("/{experiment_id}/upload")
async def upload_research_csv(
    experiment_id: str,
    background_tasks: BackgroundTasks,
    trades: OptionalUpload = File(None),
    market: OptionalUpload = File(None),
    deals: OptionalUpload = File(None),
    is_deals: OptionalUpload = File(None),
    is_market: OptionalUpload = File(None),
    oos_deals: OptionalUpload = File(None),
    oos_market: OptionalUpload = File(None),
) -> dict:
    experiment_id = _safe_experiment_id(experiment_id)
    _experiment_exists(experiment_id)

    incoming = {
        "trades": trades,
        "market": market,
        "deals": deals,
        "is_deals": is_deals,
        "is_market": is_market,
        "oos_deals": oos_deals,
        "oos_market": oos_market,
    }

    selected = {
        field: upload
        for field, upload in incoming.items()
        if upload is not None
    }

    if not selected:
        raise HTTPException(
            status_code=400,
            detail="Upload at least one CSV file.",
        )

    folder = _folder(experiment_id)
    folder.mkdir(parents=True, exist_ok=True)

    saved = {}

    for field, upload in selected.items():
        saved[field] = await _save_csv(
            upload,
            folder / FIELD_TO_FILENAME[field],
        )

    # Trigger the same full orchestrator used by the 30-second background loop.
    # The upload request itself returns immediately.
    background_tasks.add_task(
        research_auto_orchestrator.run_now
    )

    return {
        "status": "uploaded",
        "experiment_id": experiment_id,
        "input_folder": str(folder),
        "saved": saved,
        "next_action": (
            "Research Auto Runner will process the experiment automatically. "
            "Check /api/auto-run/status for progress."
        ),
    }
