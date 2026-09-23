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

# Separate AI↔AI discovery loop. It never changes the existing research-comment
# watcher behavior and remains disabled unless explicitly enabled.
def interaction_enabled() -> bool:
    return os.getenv("MOLTBOOK_AI_INTERACTION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def run_interaction_cycle() -> dict:
    from app.moltbook_interaction import run_cycle
    auto = os.getenv("MOLTBOOK_AI_INTERACTION_AUTO_COMMENT_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    max_comments = max(0, int(os.getenv("MOLTBOOK_INTERACTION_MAX_COMMENTS_PER_CYCLE", "2")))
    min_relevance = float(os.getenv("MOLTBOOK_INTERACTION_MIN_RELEVANCE", "0.80"))
    return run_cycle(auto_comment=auto, max_comments=max_comments, min_relevance=min_relevance)


def start_interaction_if_enabled() -> None:
    global _started
    if not interaction_enabled():
        return
    with _lock:
        # Reuse the existing process-level lock so only one interaction worker
        # is created. The research-comment watcher has its own startup path;
        # this guard is intentionally conservative for a single-service deploy.
        t = threading.Thread(target=_interaction_loop, name="moltbook-ai-ai-interaction", daemon=True)
        t.start()


def _interaction_loop() -> None:
    interval = max(600, int(os.getenv("MOLTBOOK_AI_INTERACTION_INTERVAL_SECONDS", "1800")))
    while interaction_enabled():
        try:
            run_interaction_cycle()
        except Exception:
            traceback.print_exc()
        time.sleep(interval)
