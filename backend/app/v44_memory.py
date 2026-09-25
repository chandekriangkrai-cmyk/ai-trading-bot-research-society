"""
V44.9 Persistent Memory + Statistics

SQLite is intentionally used so memory survives:
- Render process restart
- application restart
- daily reset
- deployment, provided persistent storage is used by the deployment

The system does NOT let AI directly mutate safety-critical state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class V44Memory:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()

        Path(self.db_path).parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._init_db()

    # ------------------------------------------------------------------
    # DB
    # ------------------------------------------------------------------

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row

        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")

        return conn

    def _init_db(self):
        with self._lock:
            conn = self._connect()

            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS agent_state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS ai_budget (
                        day_key TEXT PRIMARY KEY,
                        reserved INTEGER NOT NULL DEFAULT 0,
                        completed INTEGER NOT NULL DEFAULT 0,
                        failed INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS activities (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        day_key TEXT NOT NULL,
                        hour INTEGER NOT NULL,
                        activity TEXT NOT NULL,
                        ai_reserved INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        duration_ms INTEGER NOT NULL DEFAULT 0,
                        details TEXT
                    );

                    CREATE TABLE IF NOT EXISTS opportunities (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        external_id TEXT UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        source TEXT,
                        author TEXT,
                        post_id TEXT,
                        comment_id TEXT,
                        title TEXT,
                        topic TEXT,
                        relevance REAL DEFAULT 0,
                        status TEXT DEFAULT 'new',
                        metadata TEXT
                    );

                    CREATE TABLE IF NOT EXISTS reply_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        day_key TEXT NOT NULL,
                        post_id TEXT,
                        parent_comment_id TEXT,
                        reply_id TEXT,
                        author TEXT,
                        status TEXT,
                        verified INTEGER DEFAULT 0,
                        dry_run INTEGER DEFAULT 1,
                        content_hash TEXT,
                        metadata TEXT
                    );

                    CREATE TABLE IF NOT EXISTS research_memory (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        memory_type TEXT NOT NULL,
                        subject TEXT,
                        content TEXT NOT NULL,
                        evidence TEXT,
                        confidence REAL DEFAULT 0,
                        source TEXT,
                        external_id TEXT,
                        metadata TEXT
                    );

                    CREATE TABLE IF NOT EXISTS thread_memory (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        thread_id TEXT UNIQUE,
                        post_id TEXT,
                        author TEXT,
                        summary TEXT,
                        last_comment TEXT,
                        our_last_reply TEXT,
                        state TEXT DEFAULT 'active',
                        interaction_count INTEGER DEFAULT 0,
                        last_seen_at TEXT,
                        metadata TEXT
                    );

                    CREATE TABLE IF NOT EXISTS behavior_stats (
                        activity TEXT PRIMARY KEY,
                        attempts INTEGER DEFAULT 0,
                        successes INTEGER DEFAULT 0,
                        failures INTEGER DEFAULT 0,
                        useful INTEGER DEFAULT 0,
                        reward REAL DEFAULT 0,
                        weight REAL DEFAULT 1.0,
                        updated_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS time_window_stats (
                        hour INTEGER PRIMARY KEY,
                        attempts INTEGER DEFAULT 0,
                        successes INTEGER DEFAULT 0,
                        failures INTEGER DEFAULT 0,
                        useful INTEGER DEFAULT 0,
                        ai_requests INTEGER DEFAULT 0,
                        replies INTEGER DEFAULT 0,
                        verified INTEGER DEFAULT 0,
                        reward REAL DEFAULT 0,
                        updated_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS failures (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        category TEXT NOT NULL,
                        activity TEXT,
                        message TEXT,
                        retry_count INTEGER DEFAULT 0,
                        resolved INTEGER DEFAULT 0,
                        metadata TEXT
                    );

                    CREATE TABLE IF NOT EXISTS daily_reports (
                        day_key TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        report TEXT NOT NULL,
                        metadata TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_activity_day
                        ON activities(day_key);

                    CREATE INDEX IF NOT EXISTS idx_memory_type
                        ON research_memory(memory_type);

                    CREATE INDEX IF NOT EXISTS idx_failures_day
                        ON failures(timestamp);

                    CREATE INDEX IF NOT EXISTS idx_opportunities_status
                        ON opportunities(status);

                    CREATE INDEX IF NOT EXISTS idx_replies_day
                        ON reply_history(day_key);
                    """
                )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # STATE
    # ------------------------------------------------------------------

    def get_state(
        self,
        key: str,
        default: Optional[str] = None,
    ) -> Optional[str]:

        with self._lock:
            conn = self._connect()

            try:
                row = conn.execute(
                    """
                    SELECT value
                    FROM agent_state
                    WHERE key = ?
                    """,
                    (key,),
                ).fetchone()

                if row is None:
                    return default

                return row["value"]

            finally:
                conn.close()

    def set_state(
        self,
        key: str,
        value: Any,
    ):

        if not isinstance(value, str):
            value = json.dumps(
                value,
                ensure_ascii=False,
            )

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO agent_state(
                        key,
                        value,
                        updated_at
                    )
                    VALUES (?, ?, ?)
                    ON CONFLICT(key)
                    DO UPDATE SET
                        value=excluded.value,
                        updated_at=excluded.updated_at
                    """,
                    (
                        key,
                        value,
                        utc_now(),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # BUDGET
    # ------------------------------------------------------------------

    def get_budget(self, day_key: str) -> Dict[str, int]:

        with self._lock:
            conn = self._connect()

            try:
                row = conn.execute(
                    """
                    SELECT reserved, completed, failed
                    FROM ai_budget
                    WHERE day_key = ?
                    """,
                    (day_key,),
                ).fetchone()

                if row is None:
                    return {
                        "reserved": 0,
                        "completed": 0,
                        "failed": 0,
                    }

                return {
                    "reserved": int(row["reserved"]),
                    "completed": int(row["completed"]),
                    "failed": int(row["failed"]),
                }

            finally:
                conn.close()

    def reserve_ai(
        self,
        day_key: str,
        limit: int,
        amount: int = 1,
    ) -> bool:

        if amount <= 0:
            return True

        with self._lock:
            conn = self._connect()

            try:
                row = conn.execute(
                    """
                    SELECT reserved
                    FROM ai_budget
                    WHERE day_key = ?
                    """,
                    (day_key,),
                ).fetchone()

                if row is None:
                    reserved = 0

                    conn.execute(
                        """
                        INSERT INTO ai_budget(
                            day_key,
                            reserved,
                            completed,
                            failed,
                            updated_at
                        )
                        VALUES (?, 0, 0, 0, ?)
                        """,
                        (
                            day_key,
                            utc_now(),
                        ),
                    )
                else:
                    reserved = int(row["reserved"])

                if reserved + amount > limit:
                    conn.commit()
                    return False

                conn.execute(
                    """
                    UPDATE ai_budget
                    SET reserved = reserved + ?,
                        updated_at = ?
                    WHERE day_key = ?
                    """,
                    (
                        amount,
                        utc_now(),
                        day_key,
                    ),
                )

                conn.commit()

                return True

            finally:
                conn.close()

    def finish_ai(
        self,
        day_key: str,
        success: bool,
        amount: int = 1,
    ):

        with self._lock:
            conn = self._connect()

            try:
                if success:
                    conn.execute(
                        """
                        UPDATE ai_budget
                        SET completed = completed + ?,
                            updated_at = ?
                        WHERE day_key = ?
                        """,
                        (
                            amount,
                            utc_now(),
                            day_key,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE ai_budget
                        SET failed = failed + ?,
                            updated_at = ?
                        WHERE day_key = ?
                        """,
                        (
                            amount,
                            utc_now(),
                            day_key,
                        ),
                    )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # ACTIVITY
    # ------------------------------------------------------------------

    def record_activity(
        self,
        day_key: str,
        hour: int,
        activity: str,
        status: str,
        ai_reserved: int = 0,
        duration_ms: int = 0,
        details: Optional[Dict[str, Any]] = None,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO activities(
                        timestamp,
                        day_key,
                        hour,
                        activity,
                        ai_reserved,
                        status,
                        duration_ms,
                        details
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        day_key,
                        hour,
                        activity,
                        ai_reserved,
                        status,
                        duration_ms,
                        json.dumps(
                            details or {},
                            ensure_ascii=False,
                        ),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # BEHAVIOR
    # ------------------------------------------------------------------

    def ensure_activity(
        self,
        activity: str,
        weight: float,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO behavior_stats(
                        activity,
                        weight,
                        updated_at
                    )
                    VALUES (?, ?, ?)
                    ON CONFLICT(activity)
                    DO NOTHING
                    """,
                    (
                        activity,
                        weight,
                        utc_now(),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    def get_behavior_stats(self) -> List[Dict[str, Any]]:

        with self._lock:
            conn = self._connect()

            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM behavior_stats
                    ORDER BY activity
                    """
                ).fetchall()

                return [
                    dict(row)
                    for row in rows
                ]

            finally:
                conn.close()

    def update_behavior(
        self,
        activity: str,
        success: bool,
        useful: bool,
        reward: float,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO behavior_stats(
                        activity,
                        attempts,
                        successes,
                        failures,
                        useful,
                        reward,
                        weight,
                        updated_at
                    )
                    VALUES (?, 1, ?, ?, ?, ?, 1.0, ?)
                    ON CONFLICT(activity)
                    DO UPDATE SET
                        attempts =
                            behavior_stats.attempts + 1,

                        successes =
                            behavior_stats.successes
                            + excluded.successes,

                        failures =
                            behavior_stats.failures
                            + excluded.failures,

                        useful =
                            behavior_stats.useful
                            + excluded.useful,

                        reward =
                            behavior_stats.reward
                            + excluded.reward,

                        updated_at =
                            excluded.updated_at
                    """,
                    (
                        activity,
                        1 if success else 0,
                        0 if success else 1,
                        1 if useful else 0,
                        reward,
                        utc_now(),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    def set_behavior_weight(
        self,
        activity: str,
        weight: float,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    UPDATE behavior_stats
                    SET weight = ?,
                        updated_at = ?
                    WHERE activity = ?
                    """,
                    (
                        max(0.05, float(weight)),
                        utc_now(),
                        activity,
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # TIME WINDOWS
    # ------------------------------------------------------------------

    def update_time_window(
        self,
        hour: int,
        success: bool,
        useful: bool,
        ai_requests: int = 0,
        replies: int = 0,
        verified: int = 0,
        reward: float = 0.0,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO time_window_stats(
                        hour,
                        attempts,
                        successes,
                        failures,
                        useful,
                        ai_requests,
                        replies,
                        verified,
                        reward,
                        updated_at
                    )
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(hour)
                    DO UPDATE SET
                        attempts =
                            time_window_stats.attempts + 1,

                        successes =
                            time_window_stats.successes
                            + excluded.successes,

                        failures =
                            time_window_stats.failures
                            + excluded.failures,

                        useful =
                            time_window_stats.useful
                            + excluded.useful,

                        ai_requests =
                            time_window_stats.ai_requests
                            + excluded.ai_requests,

                        replies =
                            time_window_stats.replies
                            + excluded.replies,

                        verified =
                            time_window_stats.verified
                            + excluded.verified,

                        reward =
                            time_window_stats.reward
                            + excluded.reward,

                        updated_at =
                            excluded.updated_at
                    """,
                    (
                        hour,
                        1 if success else 0,
                        0 if success else 1,
                        1 if useful else 0,
                        ai_requests,
                        replies,
                        verified,
                        reward,
                        utc_now(),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    def get_time_windows(self):

        with self._lock:
            conn = self._connect()

            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM time_window_stats
                    ORDER BY hour
                    """
                ).fetchall()

                return [
                    dict(row)
                    for row in rows
                ]

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # RESEARCH MEMORY
    # ------------------------------------------------------------------

    def remember(
        self,
        memory_type: str,
        content: str,
        subject: Optional[str] = None,
        evidence: Optional[str] = None,
        confidence: float = 0.0,
        source: Optional[str] = None,
        external_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO research_memory(
                        created_at,
                        updated_at,
                        memory_type,
                        subject,
                        content,
                        evidence,
                        confidence,
                        source,
                        external_id,
                        metadata
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        utc_now(),
                        memory_type,
                        subject,
                        content,
                        evidence,
                        max(
                            0.0,
                            min(1.0, float(confidence)),
                        ),
                        source,
                        external_id,
                        json.dumps(
                            metadata or {},
                            ensure_ascii=False,
                        ),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    def search_memory(
        self,
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:

        query = f"%{query}%"

        with self._lock:
            conn = self._connect()

            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM research_memory
                    WHERE content LIKE ?
                       OR subject LIKE ?
                       OR evidence LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (
                        query,
                        query,
                        query,
                        limit,
                    ),
                ).fetchall()

                return [
                    dict(row)
                    for row in rows
                ]

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # THREAD MEMORY
    # ------------------------------------------------------------------

    def remember_thread(
        self,
        thread_id: str,
        post_id: Optional[str],
        author: Optional[str],
        summary: Optional[str],
        last_comment: Optional[str],
        our_last_reply: Optional[str],
        state: str,
        interaction_count: int,
        metadata: Optional[Dict[str, Any]] = None,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO thread_memory(
                        thread_id,
                        post_id,
                        author,
                        summary,
                        last_comment,
                        our_last_reply,
                        state,
                        interaction_count,
                        last_seen_at,
                        metadata
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id)
                    DO UPDATE SET
                        post_id = excluded.post_id,
                        author = excluded.author,
                        summary = excluded.summary,
                        last_comment = excluded.last_comment,
                        our_last_reply = excluded.our_last_reply,
                        state = excluded.state,
                        interaction_count =
                            excluded.interaction_count,
                        last_seen_at =
                            excluded.last_seen_at,
                        metadata =
                            excluded.metadata
                    """,
                    (
                        thread_id,
                        post_id,
                        author,
                        summary,
                        last_comment,
                        our_last_reply,
                        state,
                        interaction_count,
                        utc_now(),
                        json.dumps(
                            metadata or {},
                            ensure_ascii=False,
                        ),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # REPLY HISTORY
    # ------------------------------------------------------------------

    def count_replies_today(self, day_key: str) -> int:

        with self._lock:
            conn = self._connect()

            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM reply_history
                    WHERE day_key = ?
                      AND status = 'published'
                    """,
                    (day_key,),
                ).fetchone()

                return int(row["c"])

            finally:
                conn.close()

    def count_thread_replies(
        self,
        parent_comment_id: str,
    ) -> int:

        with self._lock:
            conn = self._connect()

            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM reply_history
                    WHERE parent_comment_id = ?
                      AND status = 'published'
                    """,
                    (parent_comment_id,),
                ).fetchone()

                return int(row["c"])

            finally:
                conn.close()

    def record_reply(
        self,
        day_key: str,
        post_id: Optional[str],
        parent_comment_id: Optional[str],
        reply_id: Optional[str],
        author: Optional[str],
        status: str,
        verified: bool,
        dry_run: bool,
        content_hash: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO reply_history(
                        timestamp,
                        day_key,
                        post_id,
                        parent_comment_id,
                        reply_id,
                        author,
                        status,
                        verified,
                        dry_run,
                        content_hash,
                        metadata
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        day_key,
                        post_id,
                        parent_comment_id,
                        reply_id,
                        author,
                        status,
                        1 if verified else 0,
                        1 if dry_run else 0,
                        content_hash,
                        json.dumps(
                            metadata or {},
                            ensure_ascii=False,
                        ),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # FAILURES
    # ------------------------------------------------------------------

    def record_failure(
        self,
        category: str,
        message: str,
        activity: Optional[str] = None,
        retry_count: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO failures(
                        timestamp,
                        category,
                        activity,
                        message,
                        retry_count,
                        metadata
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        category,
                        activity,
                        message,
                        retry_count,
                        json.dumps(
                            metadata or {},
                            ensure_ascii=False,
                        ),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # OPPORTUNITIES
    # ------------------------------------------------------------------

    def upsert_opportunity(
        self,
        external_id: str,
        source: str,
        author: Optional[str],
        post_id: Optional[str],
        comment_id: Optional[str],
        title: Optional[str],
        topic: Optional[str],
        relevance: float,
        status: str = "new",
        metadata: Optional[Dict[str, Any]] = None,
    ):

        with self._lock:
            conn = self._connect()

            try:
                now = utc_now()

                conn.execute(
                    """
                    INSERT INTO opportunities(
                        external_id,
                        created_at,
                        updated_at,
                        source,
                        author,
                        post_id,
                        comment_id,
                        title,
                        topic,
                        relevance,
                        status,
                        metadata
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(external_id)
                    DO UPDATE SET
                        updated_at = excluded.updated_at,
                        relevance = excluded.relevance,
                        status = excluded.status,
                        metadata = excluded.metadata
                    """,
                    (
                        external_id,
                        now,
                        now,
                        source,
                        author,
                        post_id,
                        comment_id,
                        title,
                        topic,
                        max(
                            0.0,
                            min(1.0, float(relevance)),
                        ),
                        status,
                        json.dumps(
                            metadata or {},
                            ensure_ascii=False,
                        ),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    def get_opportunities(
        self,
        limit: int = 20,
    ):

        with self._lock:
            conn = self._connect()

            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM opportunities
                    WHERE status IN ('new', 'candidate', 'follow_up')
                    ORDER BY relevance DESC,
                             updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

                return [
                    dict(row)
                    for row in rows
                ]

            finally:
                conn.close()

    def mark_opportunity(
        self,
        external_id: str,
        status: str,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    UPDATE opportunities
                    SET status = ?,
                        updated_at = ?
                    WHERE external_id = ?
                    """,
                    (
                        status,
                        utc_now(),
                        external_id,
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # DAILY REPORT
    # ------------------------------------------------------------------

    def save_report(
        self,
        day_key: str,
        report: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):

        with self._lock:
            conn = self._connect()

            try:
                conn.execute(
                    """
                    INSERT INTO daily_reports(
                        day_key,
                        created_at,
                        report,
                        metadata
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(day_key)
                    DO UPDATE SET
                        created_at = excluded.created_at,
                        report = excluded.report,
                        metadata = excluded.metadata
                    """,
                    (
                        day_key,
                        utc_now(),
                        report,
                        json.dumps(
                            metadata or {},
                            ensure_ascii=False,
                        ),
                    ),
                )

                conn.commit()

            finally:
                conn.close()

    # ------------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------------

    def summary(self, day_key: str):

        budget = self.get_budget(day_key)

        with self._lock:
            conn = self._connect()

            try:
                activities = conn.execute(
                    """
                    SELECT
                        activity,
                        COUNT(*) AS count
                    FROM activities
                    WHERE day_key = ?
                    GROUP BY activity
                    ORDER BY count DESC
                    """,
                    (day_key,),
                ).fetchall()

                replies = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(
                            CASE
                                WHEN verified = 1
                                THEN 1 ELSE 0
                            END
                        ) AS verified
                    FROM reply_history
                    WHERE day_key = ?
                    """,
                    (day_key,),
                ).fetchone()

                return {
                    "budget": budget,
                    "activities": [
                        dict(row)
                        for row in activities
                    ],
                    "replies": {
                        "total": int(
                            replies["total"] or 0
                        ),
                        "verified": int(
                            replies["verified"] or 0
                        ),
                    },
                    "behavior": self.get_behavior_stats(),
                    "time_windows": self.get_time_windows(),
                }

            finally:
                conn.close()
