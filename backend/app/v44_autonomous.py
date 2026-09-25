"""
V44 Autonomous Research Agent

Design goals:
- deterministic daily AI budget
- randomized activity selection
- cooldowns
- opportunity queue
- duplicate protection
- research-memory bookkeeping
- autonomous activity planning
- safe action decisions
- no direct secret handling
- no unbounded retries
- V43.6 remains the verification layer

This module is intentionally conservative:
the controller can decide WHAT activity should happen,
but it does not bypass existing Moltbook/API safety mechanisms.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "V44.0"

DAILY_AI_LIMIT = max(
    1,
    int(os.getenv("V44_DAILY_AI_LIMIT", "50")),
)

MAX_AI_PER_CYCLE = max(
    1,
    int(os.getenv("V44_MAX_AI_PER_CYCLE", "3")),
)

MAX_REPLIES_PER_DAY = max(
    0,
    int(os.getenv("V44_MAX_REPLIES_PER_DAY", "10")),
)

MAX_REPLIES_PER_THREAD = max(
    1,
    int(os.getenv("V44_MAX_REPLIES_PER_THREAD", "3")),
)

COOLDOWN_MINUTES = max(
    1,
    int(os.getenv("V44_COOLDOWN_MINUTES", "30")),
)

MIN_SECONDS_BETWEEN_CYCLES = max(
    60,
    int(os.getenv("V44_MIN_SECONDS_BETWEEN_CYCLES", "900")),
)

MAX_RETRY_ATTEMPTS = max(
    0,
    int(os.getenv("V44_MAX_RETRY_ATTEMPTS", "2")),
)

DB_PATH = Path(
    os.getenv(
        "V44_DB_PATH",
        "backend/data/v44_autonomous.db",
    )
)

RANDOM_SEED = os.getenv("V44_RANDOM_SEED", "")

if RANDOM_SEED:
    _RNG = random.Random(RANDOM_SEED)
else:
    _RNG = random.SystemRandom()


# ----------------------------------------------------------------------
# Activity model
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Activity:
    name: str
    weight: int
    min_remaining_budget: int
    cost: int
    requires_new_feed: bool = False
    requires_opportunity: bool = False
    requires_followup: bool = False
    can_post: bool = False


ACTIVITIES = (
    Activity(
        name="scan_new_posts",
        weight=30,
        min_remaining_budget=1,
        cost=1,
        requires_new_feed=False,
    ),
    Activity(
        name="inspect_threads",
        weight=20,
        min_remaining_budget=2,
        cost=2,
        requires_opportunity=True,
    ),
    Activity(
        name="research_discussion",
        weight=15,
        min_remaining_budget=2,
        cost=2,
        requires_opportunity=True,
    ),
    Activity(
        name="follow_up",
        weight=15,
        min_remaining_budget=3,
        cost=3,
        requires_followup=True,
        can_post=True,
    ),
    Activity(
        name="reply_candidate",
        weight=10,
        min_remaining_budget=3,
        cost=3,
        requires_opportunity=True,
        can_post=True,
    ),
    Activity(
        name="research_memory",
        weight=5,
        min_remaining_budget=1,
        cost=1,
    ),
    Activity(
        name="health_check",
        weight=5,
        min_remaining_budget=1,
        cost=1,
    ),
)


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------

def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_budget (
            day TEXT PRIMARY KEY,
            reserved INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            day TEXT NOT NULL,
            activity TEXT NOT NULL,
            cost INTEGER NOT NULL,
            status TEXT NOT NULL,
            metadata TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunities (
            fingerprint TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            post_id TEXT,
            comment_id TEXT,
            parent_id TEXT,
            topic TEXT,
            priority REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            metadata TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_history (
            fingerprint TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            post_id TEXT,
            comment_id TEXT,
            parent_id TEXT,
            status TEXT NOT NULL,
            content TEXT,
            metadata TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_memory (
            fingerprint TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            topic TEXT,
            question TEXT,
            experiment_id TEXT,
            source_post_id TEXT,
            source_comment_id TEXT,
            metadata TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    return conn


# ----------------------------------------------------------------------
# Budget controller
# ----------------------------------------------------------------------

class BudgetExhausted(RuntimeError):
    pass


class DailyBudget:
    """
    Hard budget.
    A request is reserved BEFORE the external AI call.
    Therefore errors do not allow the system to exceed the daily limit.
    """

    def __init__(self, limit: int = DAILY_AI_LIMIT):
        self.limit = max(1, int(limit))

    def _ensure_row(self, conn: sqlite3.Connection) -> None:
        day = _utc_day()

        conn.execute(
            """
            INSERT OR IGNORE INTO ai_budget(day, reserved, completed, failed)
            VALUES (?, 0, 0, 0)
            """,
            (day,),
        )

    def remaining(self) -> int:
        with _connect() as conn:
            self._ensure_row(conn)

            row = conn.execute(
                """
                SELECT reserved
                FROM ai_budget
                WHERE day = ?
                """,
                (_utc_day(),),
            ).fetchone()

            reserved = int(row["reserved"] or 0)

            return max(
                0,
                self.limit - reserved,
            )

    def reserve(self, amount: int = 1) -> None:
        amount = int(amount)

        if amount <= 0:
            return True

        today = self.today()

        conn = sqlite3.connect(self.db_path)

        try:
            self._ensure(conn)

            cur = conn.execute(
                """
                UPDATE ai_budget
                SET reserved = reserved + ?
                WHERE day = ?
                  AND reserved + ? <= ?
                """,
                (
                    amount,
                    today,
                    amount,
                    self.limit,
                ),
            )

            if cur.rowcount != 1:
                conn.rollback()
                raise BudgetExhausted(
                    "Daily AI budget exhausted or "
                    "reservation rejected: "
                    f"limit={self.limit}"
                )

            conn.commit()
            return True

        finally:
            conn.close()

    def complete(self) -> None:
        with _connect() as conn:
            self._ensure_row(conn)

            conn.execute(
                """
                UPDATE ai_budget
                SET completed = completed + 1
                WHERE day = ?
                """,
                (_utc_day(),),
            )

            conn.commit()

    def failed(self) -> None:
        with _connect() as conn:
            self._ensure_row(conn)

            conn.execute(
                """
                UPDATE ai_budget
                SET failed = failed + 1
                WHERE day = ?
                """,
                (_utc_day(),),
            )

            conn.commit()

    def snapshot(self) -> dict[str, int]:
        with _connect() as conn:
            self._ensure_row(conn)

            row = conn.execute(
                """
                SELECT reserved, completed, failed
                FROM ai_budget
                WHERE day = ?
                """,
                (_utc_day(),),
            ).fetchone()

            reserved = int(row["reserved"] or 0)
            completed = int(row["completed"] or 0)
            failed = int(row["failed"] or 0)

            return {
                "limit": self.limit,
                "reserved": reserved,
                "completed": completed,
                "failed": failed,
                "remaining": max(
                    0,
                    self.limit - reserved,
                ),
            }


# ----------------------------------------------------------------------
# Fingerprints / duplicate protection
# ----------------------------------------------------------------------

def fingerprint(*parts: Any) -> str:
    raw = "||".join(
        str(part or "").strip().lower()
        for part in parts
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def reply_seen(
    *,
    post_id: str = "",
    comment_id: str = "",
    parent_id: str = "",
    content: str = "",
) -> bool:
    fp = fingerprint(
        post_id,
        comment_id,
        parent_id,
        content,
    )

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM reply_history
            WHERE fingerprint = ?
            """,
            (fp,),
        ).fetchone()

        return row is not None


def remember_reply(
    *,
    post_id: str,
    comment_id: str = "",
    parent_id: str = "",
    content: str,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    fp = fingerprint(
        post_id,
        comment_id,
        parent_id,
        content,
    )

    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reply_history(
                fingerprint,
                created_at,
                post_id,
                comment_id,
                parent_id,
                status,
                content,
                metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fp,
                _utc_now(),
                post_id,
                comment_id,
                parent_id,
                status,
                content,
                json.dumps(
                    metadata or {},
                    ensure_ascii=False,
                ),
            ),
        )

        conn.commit()


# ----------------------------------------------------------------------
# Opportunities
# ----------------------------------------------------------------------

def upsert_opportunity(
    *,
    post_id: str = "",
    comment_id: str = "",
    parent_id: str = "",
    topic: str = "",
    priority: float = 0,
    metadata: dict[str, Any] | None = None,
) -> str:

    fp = fingerprint(
        post_id,
        comment_id,
        parent_id,
        topic,
    )

    now = _utc_now()

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO opportunities(
                fingerprint,
                created_at,
                updated_at,
                post_id,
                comment_id,
                parent_id,
                topic,
                priority,
                status,
                metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            ON CONFLICT(fingerprint)
            DO UPDATE SET
                updated_at=excluded.updated_at,
                priority=excluded.priority,
                metadata=excluded.metadata
            """,
            (
                fp,
                now,
                now,
                post_id,
                comment_id,
                parent_id,
                topic,
                float(priority),
                json.dumps(
                    metadata or {},
                    ensure_ascii=False,
                ),
            ),
        )

        conn.commit()

    return fp


def get_best_opportunity() -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM opportunities
            WHERE status = 'queued'
            ORDER BY priority DESC, updated_at ASC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            return None

        return dict(row)


def mark_opportunity(
    opportunity_fp: str,
    status: str,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE opportunities
            SET status = ?, updated_at = ?
            WHERE fingerprint = ?
            """,
            (
                status,
                _utc_now(),
                opportunity_fp,
            ),
        )

        conn.commit()


# ----------------------------------------------------------------------
# Research memory
# ----------------------------------------------------------------------

def remember_research(
    *,
    topic: str,
    question: str,
    experiment_id: str = "",
    source_post_id: str = "",
    source_comment_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:

    fp = fingerprint(
        topic,
        question,
        experiment_id,
    )

    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO research_memory(
                fingerprint,
                created_at,
                topic,
                question,
                experiment_id,
                source_post_id,
                source_comment_id,
                metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fp,
                _utc_now(),
                topic,
                question,
                experiment_id,
                source_post_id,
                source_comment_id,
                json.dumps(
                    metadata or {},
                    ensure_ascii=False,
                ),
            ),
        )

        conn.commit()

    return fp


# ----------------------------------------------------------------------
# Daily posting limits
# ----------------------------------------------------------------------

def replies_today() -> int:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM reply_history
            WHERE created_at LIKE ?
              AND status IN ('posted', 'verified')
            """,
            (_utc_day() + "%",),
        ).fetchone()

        return int(row[0] or 0)


def can_post() -> tuple[bool, str]:
    current = replies_today()

    if current >= MAX_REPLIES_PER_DAY:
        return (
            False,
            f"daily reply limit reached: "
            f"{current}/{MAX_REPLIES_PER_DAY}",
        )

    return True, "ok"


# ----------------------------------------------------------------------
# Cooldown
# ----------------------------------------------------------------------

def _get_state(
    key: str,
) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT value
            FROM agent_state
            WHERE key = ?
            """,
            (key,),
        ).fetchone()

        if not row:
            return None

        return str(row["value"])


def _set_state(
    key: str,
    value: str,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO agent_state(
                key,
                value,
                updated_at
            )
            VALUES (?, ?, ?)
            """,
            (
                key,
                value,
                _utc_now(),
            ),
        )

        conn.commit()


def cycle_allowed() -> tuple[bool, str]:
    last = _get_state("last_cycle_epoch")

    if not last:
        return True, "first cycle"

    try:
        last_epoch = float(last)
    except Exception:
        return True, "invalid previous cycle state"

    elapsed = time.time() - last_epoch

    if elapsed < MIN_SECONDS_BETWEEN_CYCLES:
        return (
            False,
            f"cycle cooldown: "
            f"{int(MIN_SECONDS_BETWEEN_CYCLES - elapsed)}s remaining",
        )

    return True, "ok"


def mark_cycle() -> None:
    _set_state(
        "last_cycle_epoch",
        str(time.time()),
    )


# ----------------------------------------------------------------------
# Activity eligibility
# ----------------------------------------------------------------------

def _has_followup() -> bool:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM opportunities
            WHERE status = 'followup'
            LIMIT 1
            """
        ).fetchone()

        return row is not None


def _has_opportunity() -> bool:
    return get_best_opportunity() is not None


def eligible_activities(
    *,
    new_feed_available: bool = True,
) -> list[Activity]:

    budget = DailyBudget()
    remaining = budget.remaining()

    has_opportunity = _has_opportunity()
    has_followup = _has_followup()

    result: list[Activity] = []

    for activity in ACTIVITIES:

        if remaining < activity.min_remaining_budget:
            continue

        if activity.cost > remaining:
            continue

        if (
            activity.requires_new_feed
            and not new_feed_available
        ):
            continue

        if (
            activity.requires_opportunity
            and not has_opportunity
        ):
            continue

        if (
            activity.requires_followup
            and not has_followup
        ):
            continue

        if activity.can_post:
            allowed, _ = can_post()

            if not allowed:
                continue

        result.append(activity)

    return result


# ----------------------------------------------------------------------
# Weighted randomized selection
# ----------------------------------------------------------------------

def choose_activity(
    *,
    new_feed_available: bool = True,
) -> dict[str, Any]:

    budget = DailyBudget()

    remaining = budget.remaining()

    eligible = eligible_activities(
        new_feed_available=new_feed_available,
    )

    if not eligible:
        return {
            "status": "idle",
            "reason": "no eligible activity",
            "remaining_ai_budget": remaining,
        }

    weights = [
        max(1, activity.weight)
        for activity in eligible
    ]

    selected = _RNG.choices(
        eligible,
        weights=weights,
        k=1,
    )[0]

    return {
        "status": "selected",
        "activity": selected.name,
        "estimated_ai_cost": selected.cost,
        "remaining_ai_budget": remaining,
        "eligible": [
            activity.name
            for activity in eligible
        ],
    }


# ----------------------------------------------------------------------
# AI request wrapper
# ----------------------------------------------------------------------

def reserve_ai_request(
    *,
    activity: str,
) -> dict[str, Any]:

    budget = DailyBudget()

    budget.reserve(1)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO activities(
                created_at,
                day,
                activity,
                cost,
                status,
                metadata
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                _utc_day(),
                activity,
                1,
                "reserved",
                "{}",
            ),
        )

        conn.commit()

    return budget.snapshot()


def complete_ai_request(
    *,
    success: bool,
) -> None:

    budget = DailyBudget()

    if success:
        budget.complete()
    else:
        budget.failed()


# ----------------------------------------------------------------------
# Autonomous cycle planner
# ----------------------------------------------------------------------

def plan_cycle(
    *,
    new_feed_available: bool = True,
) -> dict[str, Any]:

    allowed, reason = cycle_allowed()

    if not allowed:
        return {
            "status": "cooldown",
            "reason": reason,
            "version": VERSION,
            "budget": DailyBudget().snapshot(),
        }

    budget = DailyBudget()

    snapshot = budget.snapshot()

    if snapshot["remaining"] <= 0:
        return {
            "status": "budget_exhausted",
            "version": VERSION,
            "budget": snapshot,
        }

    selection = choose_activity(
        new_feed_available=new_feed_available,
    )

    if selection["status"] != "selected":
        return {
            **selection,
            "version": VERSION,
            "budget": snapshot,
        }

    activity = selection["activity"]

    _set_state(
        "last_selected_activity",
        activity,
    )

    return {
        "status": "planned",
        "version": VERSION,
        "activity": activity,
        "budget": budget.snapshot(),
        "selection": selection,
    }


# ----------------------------------------------------------------------
# Activity policy
# ----------------------------------------------------------------------

def activity_policy(
    activity: str,
) -> dict[str, Any]:

    policies: dict[str, dict[str, Any]] = {

        "scan_new_posts": {
            "action": "collect_new_feed",
            "ai_required": True,
            "may_post": False,
        },

        "inspect_threads": {
            "action": "inspect_research_threads",
            "ai_required": True,
            "may_post": False,
        },

        "research_discussion": {
            "action": "analyze_research_opportunity",
            "ai_required": True,
            "may_post": False,
        },

        "follow_up": {
            "action": "generate_followup_reply",
            "ai_required": True,
            "may_post": True,
        },

        "reply_candidate": {
            "action": "generate_research_reply",
            "ai_required": True,
            "may_post": True,
        },

        "research_memory": {
            "action": "update_research_memory",
            "ai_required": True,
            "may_post": False,
        },

        "health_check": {
            "action": "check_agent_health",
            "ai_required": False,
            "may_post": False,
        },
    }

    return policies.get(
        activity,
        {
            "action": "unknown",
            "ai_required": False,
            "may_post": False,
        },
    )


# ----------------------------------------------------------------------
# Safety gate for posting
# ----------------------------------------------------------------------

def posting_gate(
    *,
    post_id: str,
    parent_id: str,
    content: str,
) -> dict[str, Any]:

    if not post_id:
        return {
            "allowed": False,
            "reason": "missing post_id",
        }

    if not parent_id:
        return {
            "allowed": False,
            "reason": "missing parent_id",
        }

    content = str(content or "").strip()

    if not content:
        return {
            "allowed": False,
            "reason": "empty content",
        }

    if len(content) < 20:
        return {
            "allowed": False,
            "reason": "reply too short",
        }

    if len(content) > 5000:
        return {
            "allowed": False,
            "reason": "reply too long",
        }

    allowed, reason = can_post()

    if not allowed:
        return {
            "allowed": False,
            "reason": reason,
        }

    if reply_seen(
        post_id=post_id,
        parent_id=parent_id,
        content=content,
    ):
        return {
            "allowed": False,
            "reason": "duplicate reply",
        }

    return {
        "allowed": True,
        "reason": "passed deterministic gate",
    }


# ----------------------------------------------------------------------
# Agent status
# ----------------------------------------------------------------------

def status() -> dict[str, Any]:

    budget = DailyBudget()

    return {
        "version": VERSION,
        "agent_mode": os.getenv(
            "V44_AUTONOMOUS_ENABLED",
            "false",
        ).lower()
        == "true",
        "daily_ai_budget": DAILY_AI_LIMIT,
        "budget": budget.snapshot(),
        "replies_today": replies_today(),
        "max_replies_per_day": MAX_REPLIES_PER_DAY,
        "max_replies_per_thread": MAX_REPLIES_PER_THREAD,
        "cooldown_minutes": COOLDOWN_MINUTES,
        "max_retry_attempts": MAX_RETRY_ATTEMPTS,
        "database": str(DB_PATH),
    }


# ----------------------------------------------------------------------
# Local self-test
# ----------------------------------------------------------------------

def self_test() -> dict[str, Any]:

    checks: list[dict[str, Any]] = []

    try:
        conn = _connect()
        conn.execute("SELECT 1")
        conn.close()

        checks.append({
            "name": "database",
            "status": "pass",
        })
    except Exception as exc:
        checks.append({
            "name": "database",
            "status": "fail",
            "error": str(exc),
        })

    try:
        budget = DailyBudget()
        snapshot = budget.snapshot()

        assert snapshot["limit"] == DAILY_AI_LIMIT
        assert snapshot["remaining"] >= 0
        assert snapshot["remaining"] <= DAILY_AI_LIMIT

        checks.append({
            "name": "budget",
            "status": "pass",
            "snapshot": snapshot,
        })
    except Exception as exc:
        checks.append({
            "name": "budget",
            "status": "fail",
            "error": str(exc),
        })

    try:
        selected = choose_activity(
            new_feed_available=True,
        )

        assert isinstance(selected, dict)

        checks.append({
            "name": "activity_selector",
            "status": "pass",
            "selection": selected,
        })
    except Exception as exc:
        checks.append({
            "name": "activity_selector",
            "status": "fail",
            "error": str(exc),
        })

    try:
        gate = posting_gate(
            post_id="test-post",
            parent_id="test-parent",
            content=(
                "This is a local autonomous-agent "
                "posting gate test."
            ),
        )

        assert gate["allowed"] is True

        checks.append({
            "name": "posting_gate",
            "status": "pass",
            "result": gate,
        })
    except Exception as exc:
        checks.append({
            "name": "posting_gate",
            "status": "fail",
            "error": str(exc),
        })

    passed = all(
        item["status"] == "pass"
        for item in checks
    )

    return {
        "version": VERSION,
        "passed": passed,
        "checks": checks,
    }


if __name__ == "__main__":
    print(
        json.dumps(
            self_test(),
            ensure_ascii=False,
            indent=2,
        )
    )
