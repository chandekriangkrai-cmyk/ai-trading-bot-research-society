from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any


DB_PATH = os.getenv(
    "V44_DB_PATH",
    "backend/data/v44_autonomous.db",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_deadletter() -> None:
    conn = _db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS v44_dead_letter (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity TEXT NOT NULL,
                post_id TEXT,
                comment_id TEXT,
                error_type TEXT,
                error_message TEXT,
                attempts INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'open',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_failure(
    activity: str,
    error: Exception | str,
    post_id: str | None = None,
    comment_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    init_deadletter()

    if isinstance(error, Exception):
        error_type = type(error).__name__
        error_message = str(error)
    else:
        error_type = "RuntimeError"
        error_message = str(error)

    now = _now()

    conn = _db()
    try:
        cur = conn.execute(
            """
            INSERT INTO v44_dead_letter (
                activity,
                post_id,
                comment_id,
                error_type,
                error_message,
                attempts,
                status,
                payload_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, 'open', ?, ?, ?)
            """,
            (
                str(activity),
                str(post_id) if post_id else None,
                str(comment_id) if comment_id else None,
                error_type,
                error_message[:4000],
                json.dumps(payload or {}, ensure_ascii=False)[:10000],
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def status(limit: int = 20) -> dict[str, Any]:
    init_deadletter()

    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM v44_dead_letter
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()

        return {
            "open_count": int(
                conn.execute(
                    "SELECT COUNT(*) FROM v44_dead_letter WHERE status='open'"
                ).fetchone()[0]
            ),
            "items": [dict(x) for x in rows],
        }
    finally:
        conn.close()
