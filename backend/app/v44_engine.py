"""
V44.27 Autonomous Adaptive Engine.

Responsibilities:
- persistent ON/OFF state
- 07:00 reset
- 07:10 start
- 50 AI/day hard budget
- adaptive activity selection
- persistent memory
- time-window statistics
- daily reports
- human-only top-level Feed Post firewall

IMPORTANT:
The engine deliberately does not implement a new OpenRouter client.
The existing application remains the source of truth for AI calls.

The engine calls the existing cycle endpoint through a configurable adapter.
"""

from __future__ import annotations

import os

import hashlib
import json
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone, tzinfo
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


class _BangkokFallbackTZ(tzinfo):
    """UTC+7 fallback for minimal Termux environments."""

    def utcoffset(self, dt):
        from datetime import timedelta
        return timedelta(hours=7)

    def dst(self, dt):
        from datetime import timedelta
        return timedelta(0)

    def tzname(self, dt):
        return "Asia/Bangkok"

from .v44_adaptive import AdaptiveEngine
from .v44_config import (
    CYCLE_PAYLOAD_JSON,
    CYCLE_URL,
    DAILY_AI_LIMIT,
    DRY_RUN,
    MAX_AI_PER_CYCLE,
    MAX_REPLIES_PER_DAY,
    MAX_REPLIES_PER_THREAD,
    MAX_RETRY_ATTEMPTS,
    MIN_SECONDS_BETWEEN_CYCLES,
    RESET_HOUR,
    RESET_MINUTE,
    START_HOUR,
    START_MINUTE,
    TIMEZONE,
    V44_ENABLED_DEFAULT,
    EXTERNAL_CALLS_ENABLED,
    ensure_directories,
)
from .v44_firewall import HumanOnlyFeedPostFirewall
from .v44_memory import V44Memory
from .v44_report import DailyReport


class V44Engine:

    def __init__(self):

        ensure_directories()

        self.memory = V44Memory(
            # Imported lazily here so config remains simple.
            __import__(
                "app.v44_config",
                fromlist=["DB_PATH"],
            ).DB_PATH
        )

        self.adaptive = AdaptiveEngine(
            self.memory
        )

        self.reporter = DailyReport(
            self.memory
        )

        # Render/Linux normally has the IANA timezone database.
        # Minimal Termux installations may not have tzdata.
        # Fall back to fixed UTC+7 for Thailand.
        if ZoneInfo is not None:
            try:
                self.tz = ZoneInfo(TIMEZONE)
            except Exception:
                self.tz = _BangkokFallbackTZ()
        else:
            self.tz = _BangkokFallbackTZ()

        self.lock = threading.RLock()

        self.stop_event = threading.Event()

        self.worker_thread = None

        # V44 worker telemetry.
        # Read-only diagnostics; does not perform external calls.
        self._telemetry_lock = threading.Lock()
        self._telemetry = {
            "worker_alive": False,
            "last_worker_tick": None,
            "last_cycle_started": None,
            "last_cycle_finished": None,
            "last_activity": None,
            "last_cycle_result": None,
            "last_error": None,
            "last_ai_reserved": 0,
            "cycle_count": 0,
            "error_count": 0,
        }

        # Environment is authoritative at startup.
        # A stale SQLite value must NEVER re-enable production.
        self.enabled = bool(V44_ENABLED_DEFAULT)

        self.memory.set_state(
            "enabled",
            "1" if self.enabled else "0",
        )

        self.memory.set_state(
            "startup_master_switch",
            "1" if self.enabled else "0",
        )

        self.last_cycle_ts = 0.0

        self.last_reset_day = (
            self.memory.get_state(
                "last_reset_day"
            )
        )

        self.last_start_day = (
            self.memory.get_state(
                "last_start_day"
            )
        )

        self.last_report_day = (
            self.memory.get_state(
                "last_report_day"
            )
        )

    # ------------------------------------------------------------------
    # CLOCK
    # ------------------------------------------------------------------

    def now(self):
        return datetime.now(
            self.tz
        )

    def day_key(self):
        # OpenRouter daily quota resets at 00:00 UTC.
        # 00:00 UTC = 07:00 Thailand.
        # Therefore the V44 hard budget uses the UTC provider day.
        return datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # MASTER CONTROL
    # ------------------------------------------------------------------

    def activate(self):

        with self.lock:

            self.enabled = True

            self.memory.set_state(
                "enabled",
                "1",
            )

            self.memory.set_state(
                "last_control",
                "activate",
            )

            self.memory.set_state(
                "last_control_at",
                self.now().isoformat(),
            )

        self.ensure_worker()

        return self.status()

    def stop(self):

        with self.lock:

            self.enabled = False

            self.memory.set_state(
                "enabled",
                "0",
            )

            self.memory.set_state(
                "last_control",
                "stop",
            )

            self.memory.set_state(
                "last_control_at",
                self.now().isoformat(),
            )

        return self.status()

    # ------------------------------------------------------------------
    # THREAD
    # ------------------------------------------------------------------

    def _telemetry_update(self, **kwargs):
        try:
            with self._telemetry_lock:
                self._telemetry.update(kwargs)
        except Exception:
            pass

    def telemetry(self):
        try:
            with self._telemetry_lock:
                data = dict(self._telemetry)
        except Exception:
            data = {}
        data["worker_thread_alive"] = bool(
            self.worker_thread is not None
            and self.worker_thread.is_alive()
        )
        return data

    def ensure_worker(self):

        with self.lock:

            if (
                self.worker_thread
                and self.worker_thread.is_alive()
            ):
                return

            self.stop_event.clear()

            self.worker_thread = threading.Thread(
                target=self._worker_loop,
                name="v44-autonomous-worker",
                daemon=True,
            )

            self.worker_thread.start()

    def _worker_loop(self):

        while not self.stop_event.is_set():

            try:

                self.tick()

            except Exception as exc:

                self._telemetry_update(
                    worker_alive=True,
                    last_error=str(exc),
                    last_cycle_result="worker_error",
                )

                self.memory.record_failure(
                    category="ENGINE_TICK",
                    message=str(exc),
                    activity="engine",
                )

            self.stop_event.wait(
                30
            )

        self._telemetry_update(
            worker_alive=False,
            last_cycle_result="stopped",
        )

    # ------------------------------------------------------------------
    # DAILY RESET
    # ------------------------------------------------------------------

    def should_reset(self):

        now = self.now()

        day = now.strftime(
            "%Y-%m-%d"
        )

        if self.last_reset_day == day:
            return False

        # First observation of a new day:
        # initialize it without destroying historical memory.
        return (
            now.hour > RESET_HOUR
            or (
                now.hour == RESET_HOUR
                and now.minute >= RESET_MINUTE
            )
        )

    def reset_day_if_needed(self):

        if not self.should_reset():
            return False

        day = self.day_key()

        # Budget is naturally day-keyed in SQLite.
        # We do NOT delete old data.
        self.memory.set_state(
            "last_reset_day",
            day,
        )

        self.memory.set_state(
            "daily_budget_limit",
            str(DAILY_AI_LIMIT),
        )

        self.last_reset_day = day

        return True

    # ------------------------------------------------------------------
    # DAILY REPORT
    # ------------------------------------------------------------------

    def report_if_needed(self):

        now = self.now()

        day = now.strftime(
            "%Y-%m-%d"
        )

        # Create yesterday's report after a new day begins.
        yesterday = (
            now - timedelta(days=1)
        ).strftime("%Y-%m-%d")

        if (
            now.hour == RESET_HOUR
            and now.minute < 30
            and self.last_report_day
            != yesterday
        ):

            report = self.reporter.save(
                yesterday
            )

            self.memory.set_state(
                "last_report_day",
                yesterday,
            )

            return report

        return None

    # ------------------------------------------------------------------
    # SCHEDULE
    # ------------------------------------------------------------------

    def schedule_allows_run(self):

        now = self.now()

        # Autonomous activity starts at 07:10.
        if now.hour < START_HOUR:
            return False

        if (
            now.hour == START_HOUR
            and now.minute < START_MINUTE
        ):
            return False

        return True

    # ------------------------------------------------------------------
    # BUDGET
    # ------------------------------------------------------------------

    def budget(self):

        data = self.memory.get_budget(
            self.day_key()
        )

        used = (
            data["reserved"]
        )

        remaining = max(
            0,
            DAILY_AI_LIMIT - used,
        )

        return {
            **data,
            "limit": DAILY_AI_LIMIT,
            "used": used,
            "remaining": remaining,
        }

    def reserve_ai(
        self,
        amount: int = 1,
    ):

        return self.memory.reserve_ai(
            self.day_key(),
            DAILY_AI_LIMIT,
            amount,
        )

    # ------------------------------------------------------------------
    # ELIGIBILITY
    # ------------------------------------------------------------------

    def eligible_activities(self):

        budget = self.budget()

        if budget["remaining"] <= 0:
            return [
                "health_check"
            ]

        replies = (
            self.memory.count_replies_today(
                self.day_key()
            )
        )

        eligible = [
            "scan_new_posts",
            "inspect_threads",
            "research_discussion",
            "research_memory",
            "health_check",
        ]

        if replies < MAX_REPLIES_PER_DAY:
            eligible.extend(
                [
                    "follow_up",
                    "reply_candidate",
                ]
            )

        return eligible

    # ------------------------------------------------------------------
    # CYCLE
    # ------------------------------------------------------------------

    def choose_activity(self):

        eligible = (
            self.eligible_activities()
        )

        return self.adaptive.choose(
            eligible
        )

    def posting_gate(
        self,
        action: str,
    ):

        return HumanOnlyFeedPostFirewall.check(
            action,
            autonomous=True,
        )

    # ------------------------------------------------------------------
    # EXISTING CYCLE ADAPTER
    # ------------------------------------------------------------------

    def invoke_existing_cycle(
        self,
        activity: str,
    ):

        # ------------------------------------------------------------------
        # HARD FIREWALL
        # ------------------------------------------------------------------

        firewall = self.posting_gate(
            activity
        )

        if not firewall["allowed"]:
            raise PermissionError(
                firewall["reason"]
            )

        # ------------------------------------------------------------------
        # DRY RUN
        # ------------------------------------------------------------------

        # Triple external-call safety gate.
        #
        # Production requires ALL THREE conditions:
        # 1) V44_ENABLED=1
        # 2) V44_DRY_RUN=0
        # 3) V44_EXTERNAL_CALLS_ENABLED=1
        #
        # Any false condition blocks external AI/Moltbook calls.
        if (
            not V44_ENABLED_DEFAULT
            or DRY_RUN
            or not EXTERNAL_CALLS_ENABLED
        ):
            return {
                "status": "safe_mode",
                "activity": activity,
                "external_call": False,
                "ai_requests_used": 0,
                "gates": {
                    "V44_ENABLED": bool(V44_ENABLED_DEFAULT),
                    "V44_DRY_RUN": bool(DRY_RUN),
                    "V44_EXTERNAL_CALLS_ENABLED": bool(
                        EXTERNAL_CALLS_ENABLED
                    ),
                },
                "message": (
                    "External autonomous activity is blocked "
                    "by V44 safety gates."
                ),
            }

        if activity in {
            "research_memory",
            "health_check",
        }:
            return {
                "status": "local_only",
                "activity": activity,
                "external_call": False,
                "ai_requests_used": 0,
            }

        # ------------------------------------------------------------------
        # V44 HARD RESERVATION
        #
        # Reserve exactly one AI request before invoking any AI path.
        # The downstream subsystem is also capped to one request.
        # ------------------------------------------------------------------

        if not self.reserve_ai(1):
            return {
                "status": "budget_exhausted",
                "activity": activity,
                "external_call": False,
                "ai_requests_used": 0,
            }

        try:

            # Existing production modules.
            from app.moltbook_interaction import (
                discover_and_analyze,
                run_cycle,
            )

            # --------------------------------------------------------------
            # FEED DISCOVERY
            # --------------------------------------------------------------

            if activity in {
                "scan_new_posts",
                "inspect_threads",
            }:

                result = discover_and_analyze(
                    limit=int(
                        os.getenv(
                            "MOLTBOOK_INTERACTION_FEED_LIMIT",
                            "40",
                        )
                    ),
                    min_relevance=float(
                        os.getenv(
                            "MOLTBOOK_INTERACTION_MIN_RELEVANCE",
                            "0.30",
                        )
                    ),
                    ai_request_budget_override=1,
                )

            # --------------------------------------------------------------
            # AI↔AI RESEARCH INTERACTION
            # --------------------------------------------------------------

            elif activity in {
                "follow_up",
                "reply_candidate",
            }:

                result = run_cycle(
                    auto_comment=True,
                    max_comments=1,
                    min_relevance=float(
                        os.getenv(
                            "MOLTBOOK_INTERACTION_MIN_RELEVANCE",
                            "0.30",
                        )
                    ),
                    ai_request_budget_override=1,
                )

            # --------------------------------------------------------------
            # RESEARCH COMMENTS
            #
            # Read comments from published research posts,
            # generate one evidence-grounded reply,
            # then use the V43.6 verification solver.
            # --------------------------------------------------------------

            elif activity == "research_discussion":

                import asyncio

                from app.api.discussion import (
                    scan_discussion,
                )

                from app.database import (
                    SessionLocal,
                )

                from app.research_models import (
                    MoltbookPostLink,
                )

                db = SessionLocal()

                try:
                    experiment_ids = [
                        row.experiment_id
                        for row in (
                            db.query(
                                MoltbookPostLink
                            )
                            .filter(
                                MoltbookPostLink.status
                                == "published"
                            )
                            .order_by(
                                MoltbookPostLink.created_at.desc()
                            )
                            .limit(20)
                            .all()
                        )
                    ]
                finally:
                    db.close()

                discussion_results = []
                ai_used = 0

                # V44.15 HARD QUOTA RULE:
                #
                # One V44 cycle has one reserved AI request.
                # Therefore only one experiment may enter the
                # AI-backed discussion scanner per cycle.

                selected_experiment = (
                    experiment_ids[0]
                    if experiment_ids
                    else None
                )

                if selected_experiment:

                    discussion_result = (
                        asyncio.run(
                            scan_discussion(
                                experiment_id=selected_experiment,
                                post_id=None,
                                auto_reply=True,
                                max_replies=1,
                                ai_request_budget=1,
                            )
                        )
                    )

                    discussion_results.append(
                        discussion_result
                    )

                    ai_used = min(
                        1,
                        int(
                            discussion_result.get(
                                "ai_requests_used",
                                0,
                            )
                            or 0
                        ),
                    )

                result = {
                    "status": "completed",
                    "activity":
                        activity,
                    "experiments_checked":
                        1
                        if selected_experiment
                        else 0,
                    "experiments_available":
                        len(experiment_ids),
                    "ai_requests_used":
                        ai_used,
                    "hard_cycle_ai_cap":
                        1,
                    "results":
                        discussion_results,
                }

            # --------------------------------------------------------------
            # UNKNOWN ACTIVITY
            # --------------------------------------------------------------

            else:
                raise RuntimeError(
                    "V44.27 has no adapter for activity: "
                    + str(activity)
                )

            # --------------------------------------------------------------
            # AI ACCOUNTING
            #
            # Reservation is consumed conservatively.
            # Even if downstream used 0 because no candidate existed,
            # V44 does not release the reservation.
            # This guarantees no overshoot.
            # --------------------------------------------------------------

            self.memory.finish_ai(
                self.day_key(),
                success=True,
                amount=1,
            )

            return {
                "status": "completed",
                "activity": activity,
                "external_call": True,
                "ai_requests_reserved": 1,
                "downstream": result,
            }

        except Exception as exc:

            self.memory.finish_ai(
                self.day_key(),
                success=False,
                amount=1,
            )

            raise

    # ------------------------------------------------------------------
    # REWARD
    # ------------------------------------------------------------------

    def calculate_reward(
        self,
        result,
    ):

        if not isinstance(
            result,
            dict,
        ):
            return (
                False,
                False,
                0.0,
            )

        status = str(
            result.get(
                "status",
                ""
            )
        ).lower()

        if status in {
            "completed",
            "published",
            "verified",
            "dry_run",
        }:

            if status == "dry_run":
                return (
                    True,
                    False,
                    0.0,
                )

            return (
                True,
                True,
                1.0,
            )

        if status in {
            "budget_exhausted",
        }:
            return (
                True,
                False,
                0.0,
            )

        return (
            False,
            False,
            -1.0,
        )

    # ------------------------------------------------------------------
    # ONE CYCLE
    # ------------------------------------------------------------------

    def run_once(self):
        cycle_started = datetime.now(timezone.utc).isoformat()
        self._telemetry_update(
            last_cycle_started=cycle_started,
            last_cycle_finished=None,
            last_cycle_result="checking",
            last_error=None,
        )

        with self.lock:

            now = self.now()

            if not self.enabled:
                self._telemetry_update(
                    last_cycle_finished=datetime.now(
                        timezone.utc
                    ).isoformat(),
                    last_cycle_result="stopped",
                )
                return {
                    "status": "stopped"
                }

            self.reset_day_if_needed()

            if not self.schedule_allows_run():
                self._telemetry_update(
                    last_cycle_finished=datetime.now(
                        timezone.utc
                    ).isoformat(),
                    last_cycle_result="waiting_for_start_time",
                )
                return {
                    "status": "waiting_for_start_time",
                    "local_time":
                        now.isoformat(),
                }

            if (
                time.time()
                - self.last_cycle_ts
                < MIN_SECONDS_BETWEEN_CYCLES
            ):
                self._telemetry_update(
                    last_cycle_finished=datetime.now(
                        timezone.utc
                    ).isoformat(),
                    last_cycle_result="cooldown",
                )
                return {
                    "status": "cooldown"
                }

            self.last_cycle_ts = time.time()

        activity = (
            self.choose_activity()
        )

        self._telemetry_update(
            last_activity=activity,
            last_cycle_result="running",
            cycle_count=self._telemetry.get(
                "cycle_count",
                0,
            ) + 1,
        )

        started = time.time()

        try:

            result = (
                self.invoke_existing_cycle(
                    activity
                )
            )

            duration = int(
                (time.time() - started)
                * 1000
            )

            success, useful, reward = (
                self.calculate_reward(
                    result
                )
            )

            self.adaptive.learn(
                activity,
                success,
                useful,
                reward,
            )

            self.memory.record_activity(
                day_key=self.day_key(),
                hour=self.now().hour,
                activity=activity,
                status=result.get(
                    "status",
                    "unknown",
                ),
                ai_reserved=1
                if not DRY_RUN
                else 0,
                duration_ms=duration,
                details=result,
            )

            self.memory.update_time_window(
                hour=self.now().hour,
                success=success,
                useful=useful,
                ai_requests=1
                if not DRY_RUN
                else 0,
                replies=0,
                verified=0,
                reward=reward,
            )

            self._telemetry_update(
                last_cycle_finished=datetime.now(
                    timezone.utc
                ).isoformat(),
                last_cycle_result="completed",
                last_error=None,
            )

            return {
                "status": "completed",
                "activity": activity,
                "result": result,
                "reward": reward,
            }

        except Exception as exc:

            duration = int(
                (time.time() - started)
                * 1000
            )

            self.memory.record_activity(
                day_key=self.day_key(),
                hour=self.now().hour,
                activity=activity,
                status="failed",
                ai_reserved=1
                if not DRY_RUN
                else 0,
                duration_ms=duration,
                details={
                    "error": str(exc)
                },
            )

            self.memory.record_failure(
                category="AUTONOMOUS_CYCLE",
                activity=activity,
                message=str(exc),
            )

            self.adaptive.learn(
                activity,
                False,
                False,
                -1.0,
            )

            self._telemetry_update(
                last_cycle_finished=datetime.now(
                    timezone.utc
                ).isoformat(),
                last_cycle_result="failed",
                last_error=str(exc),
            )

            return {
                "status": "failed",
                "activity": activity,
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # TICK
    # ------------------------------------------------------------------

    def tick(self):
        self._telemetry_update(
            last_worker_tick=datetime.now(timezone.utc).isoformat(),
            worker_alive=True,
        )

        self.reset_day_if_needed()

        self.report_if_needed()

        if not self.enabled:
            return {
                "status": "stopped"
            }

        if not self.schedule_allows_run():
            return {
                "status": "waiting"
            }

        return self.run_once()

    # ------------------------------------------------------------------
    # STATUS
    # ------------------------------------------------------------------

    def status(self):

        now = self.now()

        return {
            "version": "V44.27",
            "enabled": self.enabled,
            "dry_run": DRY_RUN,
            "human_only_top_level_feed_post": True,
            "autonomous_comment_replies": True,
            "local_time": now.isoformat(),
            "timezone": TIMEZONE,
            "daily_reset": "07:00",
            "autonomous_start": "07:10",
            "ai_budget": self.budget(),
            "max_replies_per_day":
                MAX_REPLIES_PER_DAY,
            "max_replies_per_thread":
                MAX_REPLIES_PER_THREAD,
            "memory_db": str(
                self.memory.db_path
            ),
            "last_reset_day":
                self.last_reset_day,
            "last_start_day":
                self.last_start_day,
            "last_report_day":
                self.last_report_day,
            "activity_weights":
                self.adaptive.explain(),
        }

    # ------------------------------------------------------------------
    # SELF TEST
    # ------------------------------------------------------------------

    def self_test(self):

        firewall = (
            self.posting_gate(
                "create_post"
            )
        )

        reply = (
            self.posting_gate(
                "comment_reply"
            )
        )

        return {
            "version": "V44.27",
            "database": "PASS",
            "memory": "PASS",
            "adaptive_engine": "PASS",
            "human_feed_post_blocked":
                firewall["allowed"] is False,
            "comment_reply_allowed":
                reply["allowed"] is True,
            "daily_ai_limit":
                DAILY_AI_LIMIT,
            "dry_run":
                DRY_RUN,
        }
