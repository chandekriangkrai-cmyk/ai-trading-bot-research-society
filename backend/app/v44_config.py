"""
V44.9 Autonomous Adaptive Research Engine
Central configuration.

Important:
- Top-level Feed Post is ALWAYS human-only.
- Autonomous interaction is limited to comments/replies.
"""

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = Path(
    os.getenv(
        "V44_DATA_DIR",
        str(ROOT / "data"),
    )
)

REPORT_DIR = Path(
    os.getenv(
        "V44_REPORT_DIR",
        str(ROOT / "reports"),
    )
)

LOG_DIR = Path(
    os.getenv(
        "V44_LOG_DIR",
        str(ROOT / "logs"),
    )
)

DB_PATH = Path(
    os.getenv(
        "V44_DB_PATH",
        str(DATA_DIR / "v44_autonomous.db"),
    )
)


# ---------------------------------------------------------------------------
# MASTER CONTROL
# ---------------------------------------------------------------------------

V44_ENABLED_DEFAULT = (
    os.getenv("V44_ENABLED", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)

DRY_RUN = (
    os.getenv("V44_DRY_RUN", "1").strip().lower()
    in {"1", "true", "yes", "on"}
)


# ---------------------------------------------------------------------------
# HARD SAFETY LIMITS
# ---------------------------------------------------------------------------

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

MIN_SECONDS_BETWEEN_CYCLES = max(
    60,
    int(os.getenv("V44_MIN_SECONDS_BETWEEN_CYCLES", "900")),
)

COOLDOWN_MINUTES = max(
    0,
    int(os.getenv("V44_COOLDOWN_MINUTES", "30")),
)

MAX_RETRY_ATTEMPTS = max(
    0,
    int(os.getenv("V44_MAX_RETRY_ATTEMPTS", "2")),
)


# ---------------------------------------------------------------------------
# SCHEDULE
# Thailand = UTC+7
# 07:00 Thailand = 00:00 UTC
# 07:10 Thailand = 00:10 UTC
# ---------------------------------------------------------------------------

RESET_HOUR = 7
RESET_MINUTE = 0

START_HOUR = 7
START_MINUTE = 10

TIMEZONE = os.getenv(
    "V44_TIMEZONE",
    "Asia/Bangkok",
)


# ---------------------------------------------------------------------------
# EXISTING APP ROUTES
#
# These are intentionally configurable because the existing project
# remains the source of truth for actual Moltbook operations.
# ---------------------------------------------------------------------------

CYCLE_URL = os.getenv(
    "V44_CYCLE_URL",
    "http://127.0.0.1:8000/api/moltbook-interactions/cycle",
)

STATUS_URL = os.getenv(
    "V44_STATUS_URL",
    "http://127.0.0.1:8000/api/v44-autonomous/status",
)


# Empty JSON means:
# POST {}
#
# If the existing cycle endpoint requires a body, Render can override:
#
# V44_CYCLE_PAYLOAD_JSON='{"...":"..."}'
#
CYCLE_PAYLOAD_JSON = os.getenv(
    "V44_CYCLE_PAYLOAD_JSON",
    "{}",
)


# ---------------------------------------------------------------------------
# ACTIVITY WEIGHTS
# ---------------------------------------------------------------------------

DEFAULT_ACTIVITY_WEIGHTS = {
    "scan_new_posts": 30.0,
    "inspect_threads": 20.0,
    "research_discussion": 15.0,
    "follow_up": 15.0,
    "reply_candidate": 10.0,
    "research_memory": 5.0,
    "health_check": 5.0,
}


# ---------------------------------------------------------------------------
# LEARNING
# ---------------------------------------------------------------------------

MIN_SAMPLES_FOR_ADAPTATION = max(
    1,
    int(os.getenv("V44_MIN_SAMPLES_FOR_ADAPTATION", "10")),
)

LEARNING_RATE = min(
    1.0,
    max(
        0.01,
        float(os.getenv("V44_LEARNING_RATE", "0.15")),
    ),
)

EXPLORATION_RATE = min(
    1.0,
    max(
        0.0,
        float(os.getenv("V44_EXPLORATION_RATE", "0.20")),
    ),
)


# ---------------------------------------------------------------------------
# HUMAN ONLY POST FIREWALL
# ---------------------------------------------------------------------------

HUMAN_ONLY_TOP_LEVEL_POST = True

FORBIDDEN_AUTONOMOUS_ACTIONS = {
    "create_post",
    "publish_post",
    "top_level_post",
    "feed_post",
    "repost_as_new_post",
    "schedule_feed_post",
}


def ensure_directories():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
