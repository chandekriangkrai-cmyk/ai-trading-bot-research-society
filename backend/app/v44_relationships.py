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


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_relationships() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

    conn = _db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_relationships (
                agent_id TEXT PRIMARY KEY,
                agent_name TEXT,
                status TEXT NOT NULL DEFAULT 'unknown',
                followed_at TEXT,
                unfollowed_at TEXT,
                last_interaction_at TEXT,
                last_positive_feedback_at TEXT,
                last_negative_feedback_at TEXT,
                interaction_count INTEGER NOT NULL DEFAULT 0,
                positive_count INTEGER NOT NULL DEFAULT 0,
                negative_count INTEGER NOT NULL DEFAULT 0,
                last_reason TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS relationship_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                response_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.commit()
    finally:
        conn.close()


def get_relationship(agent_id: str) -> dict[str, Any] | None:
    init_relationships()

    conn = _db()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM agent_relationships
            WHERE agent_id=?
            """,
            (str(agent_id),),
        ).fetchone()

        if not row:
            return None

        result = dict(row)

        try:
            result["metadata"] = json.loads(
                result.pop("metadata_json") or "{}"
            )
        except Exception:
            result["metadata"] = {}

        return result
    finally:
        conn.close()


def record_observation(
    agent_id: str,
    agent_name: str = "",
    interaction: bool = False,
    feedback: str | None = None,
) -> dict[str, Any]:
    init_relationships()

    agent_id = str(agent_id)
    now = _now()

    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM agent_relationships WHERE agent_id=?",
            (agent_id,),
        ).fetchone()

        if row:
            interaction_count = int(row["interaction_count"] or 0)
            positive_count = int(row["positive_count"] or 0)
            negative_count = int(row["negative_count"] or 0)

            if interaction:
                interaction_count += 1

            if feedback == "up":
                positive_count += 1
            elif feedback == "down":
                negative_count += 1

            conn.execute(
                """
                UPDATE agent_relationships
                SET
                    agent_name=?,
                    last_interaction_at=CASE
                        WHEN ? THEN ?
                        ELSE last_interaction_at
                    END,
                    last_positive_feedback_at=CASE
                        WHEN ?='up' THEN ?
                        ELSE last_positive_feedback_at
                    END,
                    last_negative_feedback_at=CASE
                        WHEN ?='down' THEN ?
                        ELSE last_negative_feedback_at
                    END,
                    interaction_count=?,
                    positive_count=?,
                    negative_count=?,
                    updated_at=?
                WHERE agent_id=?
                """,
                (
                    agent_name,
                    bool(interaction),
                    now,
                    feedback,
                    now,
                    feedback,
                    now,
                    interaction_count,
                    positive_count,
                    negative_count,
                    now,
                    agent_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO agent_relationships (
                    agent_id,
                    agent_name,
                    status,
                    last_interaction_at,
                    last_positive_feedback_at,
                    last_negative_feedback_at,
                    interaction_count,
                    positive_count,
                    negative_count,
                    updated_at
                )
                VALUES (?, ?, 'unknown', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    agent_name,
                    now if interaction else None,
                    now if feedback == "up" else None,
                    now if feedback == "down" else None,
                    1 if interaction else 0,
                    1 if feedback == "up" else 0,
                    1 if feedback == "down" else 0,
                    now,
                ),
            )

        conn.commit()
        return get_relationship(agent_id) or {}
    finally:
        conn.close()


def relationship_policy(agent_id: str) -> dict[str, Any]:
    """
    Deterministic relationship policy.

    Important:
    We do not guess Moltbook follow/unfollow endpoints.
    Actual external actions remain disabled until explicitly configured.
    """

    row = get_relationship(agent_id)

    if not row:
        return {
            "eligible": False,
            "action": "observe",
            "reason": "No relationship history",
        }

    interactions = int(row.get("interaction_count") or 0)
    positive = int(row.get("positive_count") or 0)
    negative = int(row.get("negative_count") or 0)
    status = row.get("status") or "unknown"

    if status == "following":
        if negative >= 3 and negative > positive:
            return {
                "eligible": True,
                "action": "unfollow_candidate",
                "reason": "Repeated negative feedback",
            }

        return {
            "eligible": False,
            "action": "keep_following",
            "reason": "No deterministic unfollow trigger",
        }

    if interactions >= 2 and positive >= 1 and negative == 0:
        return {
            "eligible": True,
            "action": "follow_candidate",
            "reason": "Repeated useful interaction with positive feedback",
        }

    return {
        "eligible": False,
        "action": "observe",
        "reason": "Insufficient evidence",
    }


def external_follow_enabled() -> bool:
    return (
        os.getenv("MOLTBOOK_FOLLOW_ENABLED", "0")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


def external_unfollow_enabled() -> bool:
    return (
        os.getenv("MOLTBOOK_UNFOLLOW_ENABLED", "0")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )


def execute_follow_or_unfollow(
    agent_id: str,
    action: str,
) -> dict[str, Any]:
    """
    Safety adapter.

    The API path is intentionally NOT guessed.
    Set the exact endpoint only after verifying the current Moltbook API.
    """

    if action == "follow":
        enabled = external_follow_enabled()
        env_name = "MOLTBOOK_FOLLOW_PATH"
    elif action == "unfollow":
        enabled = external_unfollow_enabled()
        env_name = "MOLTBOOK_UNFOLLOW_PATH"
    else:
        return {
            "status": "blocked",
            "action": action,
            "reason": "Unsupported relationship action",
        }

    path = os.getenv(env_name, "").strip()

    if not enabled:
        return {
            "status": "dry_blocked",
            "action": action,
            "agent_id": str(agent_id),
            "reason": f"{env_name.replace('_PATH','_ENABLED')} is disabled",
        }

    if not path:
        return {
            "status": "blocked",
            "action": action,
            "agent_id": str(agent_id),
            "reason": f"{env_name} is not configured",
        }

    return {
        "status": "blocked",
        "action": action,
        "agent_id": str(agent_id),
        "reason": (
            "Relationship endpoint is intentionally not executed by this "
            "adapter until explicitly implemented and verified."
        ),
    }


def status() -> dict[str, Any]:
    init_relationships()

    conn = _db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM agent_relationships"
        ).fetchone()[0]

        following = conn.execute(
            "SELECT COUNT(*) FROM agent_relationships WHERE status='following'"
        ).fetchone()[0]

        positive = conn.execute(
            "SELECT COALESCE(SUM(positive_count),0) FROM agent_relationships"
        ).fetchone()[0]

        negative = conn.execute(
            "SELECT COALESCE(SUM(negative_count),0) FROM agent_relationships"
        ).fetchone()[0]

        return {
            "total_agents_observed": int(total or 0),
            "following": int(following or 0),
            "positive_feedback": int(positive or 0),
            "negative_feedback": int(negative or 0),
            "follow_enabled": external_follow_enabled(),
            "unfollow_enabled": external_unfollow_enabled(),
            "follow_path_configured": bool(
                os.getenv("MOLTBOOK_FOLLOW_PATH", "").strip()
            ),
            "unfollow_path_configured": bool(
                os.getenv("MOLTBOOK_UNFOLLOW_PATH", "").strip()
            ),
        }
    finally:
        conn.close()
