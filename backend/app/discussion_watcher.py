from __future__ import annotations

import asyncio
import os
import threading
import time
import traceback

from app.database import SessionLocal
from app.research_models import MoltbookPostLink
from app.api.discussion import scan_discussion


_started = False
_lock = threading.Lock()


def enabled() -> bool:
    return os.getenv("MOLTBOOK_COMMENT_WATCH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def auto_reply_enabled() -> bool:
    return os.getenv("MOLTBOOK_AUTO_REPLY_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def _experiment_ids() -> list[str]:
    db = SessionLocal()
    try:
        return [x.experiment_id for x in db.query(MoltbookPostLink).filter(MoltbookPostLink.status == "published").all()]
    finally:
        db.close()


def run_cycle() -> dict:
    ids = _experiment_ids()
    max_replies = max(0, int(os.getenv("MOLTBOOK_MAX_AUTO_REPLIES_PER_CYCLE", "2")))
    results = []
    for eid in ids:
        try:
            result = asyncio.run(scan_discussion(eid, None, auto_reply_enabled(), max_replies))
            results.append(result)
        except Exception as exc:
            results.append({"experiment_id": eid, "status": "error", "error": str(exc)})
    return {"experiments": len(ids), "results": results}


def _loop() -> None:
    interval = max(300, int(os.getenv("MOLTBOOK_COMMENT_WATCH_INTERVAL_SECONDS", "900")))
    while enabled():
        try:
            run_cycle()
        except Exception:
            traceback.print_exc()
        time.sleep(interval)


def start_if_enabled() -> None:
    global _started
    if not enabled():
        return
    with _lock:
        if _started:
            return
        _started = True
        t = threading.Thread(target=_loop, name="moltbook-research-discussion", daemon=True)
        t.start()
