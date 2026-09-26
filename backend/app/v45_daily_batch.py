"""
V45 Daily Batch Engine

Purpose
-------
Compress the normal autonomous V44 activity flow into a single
bounded execution window.

Design goals
------------
- Preserve V44 safety gates.
- Reuse V44 existing activity adapters.
- Do NOT implement a second AI client.
- Do NOT bypass Moltbook posting firewall.
- Do NOT increase daily AI quota.
- Maximum wall-clock execution window is configurable.
- Progress is checkpointed after every stage.
- A failed stage does not automatically destroy the whole batch.
- Batch can be triggered through FastAPI and continue server-side.

Important
---------
This is an execution scheduler, not a replacement for V44.
V44 remains the source of truth for:
    - AI calls
    - quota reservation
    - Moltbook interaction
    - posting firewall
    - adaptive learning
    - memory
    - telemetry
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class V45DailyBatch:
    """
    Bounded daily execution layer.

    The batch delegates each cycle to the V44 adaptive execution path.
    """

    VERSION = "V45.6"

    DEFAULT_MAX_MINUTES = 5

    # Do not start a new cycle when too little time remains.
    # This protects the hard batch deadline from a long final cycle.
    MIN_REMAINING_SECONDS = 2.0

    # Local/DRY-RUN pacing. External API latency remains the
    # dominant pacing factor in production.
    LOOP_PACING_SECONDS = 0.25

    # Persist checkpoint periodically instead of rewriting the
    # entire growing stage_results list after every cycle.
    CHECKPOINT_EVERY_CYCLES = 10

    # Keep only recent cycle results in the persistent checkpoint.
    # The full V44 telemetry/adaptive state remains authoritative.
    MAX_CHECKPOINT_RESULTS = 100

    # Retained for backward compatibility with the existing API/state
    # shape. V45.1 execution is adaptive rather than fixed-stage.
    STAGES = (
        "scan_new_posts",
        "inspect_threads",
        "research_discussion",
        "follow_up",
        "reply_candidate",
        "research_memory",
        "health_check",
    )

    def __init__(self, engine):
        self.engine = engine

        self.lock = threading.RLock()

        self.running = False
        self.thread = None

        self.started_at = None
        self.finished_at = None

        self.batch_id = None
        self.status_value = "idle"

        self.current_stage = None
        self.stage_index = 0

        self.stage_results = []

        self.error = None
        self.stop_reason = None

        self.started_monotonic = 0.0

        self.max_minutes = max(
            1,
            int(
                os.getenv(
                    "V45_BATCH_MAX_MINUTES",
                    str(self.DEFAULT_MAX_MINUTES),
                )
            ),
        )

        self.max_seconds = self.max_minutes * 60

        # We intentionally keep the checkpoint in the same V44 data
        # directory so local development and Render use the same
        # configured V44_DATA_DIR.
        try:
            from app.v44_config import DATA_DIR

            self.state_dir = Path(DATA_DIR)
        except Exception:
            self.state_dir = (
                Path(__file__).resolve().parents[1]
                / "data"
            )

        self.state_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.state_file = (
            self.state_dir
            / "v45_daily_batch_state.json"
        )

        self._load_checkpoint()

    # ==============================================================
    # TIME
    # ==============================================================

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return self.now().isoformat()

    def elapsed(self) -> float:
        if not self.started_monotonic:
            return 0.0

        return max(
            0.0,
            time.monotonic()
            - self.started_monotonic,
        )

    def remaining_seconds(self) -> float:
        return max(
            0.0,
            self.max_seconds
            - self.elapsed(),
        )

    def time_exceeded(self) -> bool:
        return (
            self.elapsed()
            >= self.max_seconds
        )

    def deadline_safe_to_start_cycle(self) -> bool:
        """
        Return True only when enough batch time remains to start
        another V44 cycle.

        The hard deadline itself remains authoritative. This guard
        prevents starting a potentially slow external cycle when
        only a few seconds remain.
        """
        return (
            self.remaining_seconds()
            > self.MIN_REMAINING_SECONDS
        )

    # ==============================================================
    # CHECKPOINT
    # ==============================================================

    def _safe_json(self, value):
        try:
            json.dumps(value)
            return value
        except Exception:
            return str(value)

    def _checkpoint_payload(self):
        return {
            "version": self.VERSION,
            "batch_id": self.batch_id,
            "status": self.status_value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_stage": self.current_stage,
            "stage_index": self.stage_index,
            "total_cycles": self.stage_index,
            "stage_count": None,
            "stage_results": self._safe_json(
                self.stage_results[
                    -self.MAX_CHECKPOINT_RESULTS:
                ]
            ),
            "error": self.error,
            "stop_reason": self.stop_reason,
            "max_minutes": self.max_minutes,
            "checkpoint_every_cycles":
                self.CHECKPOINT_EVERY_CYCLES,
            "max_checkpoint_results":
                self.MAX_CHECKPOINT_RESULTS,
            "updated_at": self.now_iso(),
        }

    def _trim_stage_results(self):
        """Keep in-memory batch results bounded to the checkpoint limit."""
        limit = int(self.MAX_CHECKPOINT_RESULTS)
        if limit <= 0:
            self.stage_results = []
            return
        if len(self.stage_results) > limit:
            self.stage_results = self.stage_results[-limit:]

    def _save_checkpoint(self):
        self._trim_stage_results()
        payload = self._checkpoint_payload()

        tmp = self.state_file.with_suffix(
            ".tmp"
        )

        try:
            with open(
                tmp,
                "w",
                encoding="utf-8",
            ) as fh:
                json.dump(
                    payload,
                    fh,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )

            os.replace(
                tmp,
                self.state_file,
            )

        except Exception as exc:
            # Checkpoint failure must never crash the batch.
            self.error = (
                f"checkpoint_error: {exc}"
            )

    def _load_checkpoint(self):
        if not self.state_file.exists():
            return

        try:
            with open(
                self.state_file,
                "r",
                encoding="utf-8",
            ) as fh:
                data = json.load(fh)

            self.batch_id = data.get(
                "batch_id"
            )

            self.status_value = data.get(
                "status",
                "idle",
            )

            self.started_at = data.get(
                "started_at"
            )

            self.finished_at = data.get(
                "finished_at"
            )

            self.current_stage = data.get(
                "current_stage"
            )

            self.stage_index = int(
                data.get(
                    "stage_index",
                    0,
                )
            )

            self.stage_results = data.get(
                "stage_results",
                [],
            )

            self.error = data.get(
                "error"
            )

            self.stop_reason = data.get(
                "stop_reason"
            )

        except Exception:
            # Corrupt checkpoint must not prevent startup.
            self.status_value = "checkpoint_recovery_required"

    # ==============================================================
    # STATE
    # ==============================================================

    def status(self) -> dict[str, Any]:
        with self.lock:
            if self.running:
                elapsed = self.elapsed()
                remaining = self.remaining_seconds()
            elif self.started_at and self.finished_at:
                try:
                    started_dt = datetime.fromisoformat(
                        self.started_at.replace("Z", "+00:00")
                    )
                    finished_dt = datetime.fromisoformat(
                        self.finished_at.replace("Z", "+00:00")
                    )

                    elapsed = max(
                        0.0,
                        (
                            finished_dt - started_dt
                        ).total_seconds(),
                    )

                    remaining = max(
                        0.0,
                        self.max_seconds - elapsed,
                    )
                except Exception:
                    elapsed = 0.0
                    remaining = 0.0
            else:
                elapsed = 0.0
                remaining = max(
                    0,
                    self.max_seconds,
                )

            completed = sum(
                1
                for item in self.stage_results
                if item.get("status")
                in {
                    "completed",
                    "local_only",
                    "budget_exhausted",
                    "skipped",
                }
            )

            failed = sum(
                1
                for item in self.stage_results
                if item.get("status")
                == "failed"
            )

            return {
                "version": self.VERSION,
                "running": self.running,
                "status": self.status_value,
                "batch_id": self.batch_id,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "current_stage": self.current_stage,
                "stage_index": self.stage_index,
                "total_cycles": self.stage_index,
                "stage_count": None,
                "execution_mode": "adaptive_v44_batch",
                "hard_deadline_seconds": self.max_seconds,
                "min_remaining_seconds":
                    self.MIN_REMAINING_SECONDS,
                "loop_pacing_seconds":
                    self.LOOP_PACING_SECONDS,
                "checkpoint_every_cycles":
                    self.CHECKPOINT_EVERY_CYCLES,
                "max_checkpoint_results":
                    self.MAX_CHECKPOINT_RESULTS,
                "stages_completed": completed,
                "stages_failed": failed,
                "elapsed_seconds": round(
                    elapsed,
                    3,
                ),
                "remaining_seconds": round(
                    remaining,
                    3,
                ),
                "max_minutes": self.max_minutes,
                "stop_reason": self.stop_reason,
                "error": self.error,
                "stage_results": list(
                    self.stage_results
                ),
                "checkpoint_file": str(
                    self.state_file
                ),
            }

    # ==============================================================
    # DAILY GUARD
    # ==============================================================

    def _provider_day(self):
        """
        V44 uses the UTC provider day because OpenRouter quota
        resets at 00:00 UTC.
        """
        return datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d")

    def _today_completed(self):
        """
        The checkpoint is only considered completed if it belongs
        to the current provider day.
        """
        if self.status_value != "completed":
            return False

        if not self.finished_at:
            return False

        try:
            finished = datetime.fromisoformat(
                self.finished_at
            )

            return (
                finished.astimezone(
                    timezone.utc
                ).strftime("%Y-%m-%d")
                == self._provider_day()
            )

        except Exception:
            return False

    # ==============================================================
    # START
    # ==============================================================

    def start(
        self,
        force: bool = False,
    ):
        with self.lock:

            if self.running:
                return {
                    "status": "already_running",
                    "batch": self.status(),
                }

            if (
                self._today_completed()
                and not force
            ):
                return {
                    "status": "already_completed_today",
                    "batch": self.status(),
                }

            # ------------------------------------------------------
            # Environment gates.
            # ------------------------------------------------------

            enabled = bool(
                getattr(
                    self.engine,
                    "enabled",
                    False,
                )
            )

            if not enabled:
                return {
                    "status": "blocked",
                    "reason": (
                        "V44 engine is disabled. "
                        "Set V44_ENABLED=1 before "
                        "running production batch."
                    ),
                    "batch": self.status(),
                }

            self.running = True

            self.status_value = "running"

            self.batch_id = (
                "V45-"
                + self.now().strftime(
                    "%Y%m%d-%H%M%S"
                )
            )

            self.started_at = (
                self.now_iso()
            )

            self.finished_at = None

            self.current_stage = None

            self.stage_index = 0

            self.stage_results = []

            self.error = None

            self.stop_reason = None

            self.started_monotonic = (
                time.monotonic()
            )

            self._save_checkpoint()

            self.thread = threading.Thread(
                target=self._run,
                name="v45-daily-batch",
                daemon=True,
            )

            self.thread.start()

            return {
                "status": "started",
                "batch": self.status(),
            }

    # ==============================================================
    # STOP
    # ==============================================================

    def reset(self):
        """
        Reset V45 Daily Batch state.

        Safety rules:
        - Never reset while a batch is running.
        - Do not touch V44 budget.
        - Do not call AI.
        - Do not make external calls.
        - Clear in-memory batch state.
        - Persist the clean idle state.
        """

        with self.lock:
            if self.running:
                return {
                    "status": "reset_blocked",
                    "reason": "batch_running",
                    "batch": self.status(),
                }

            self.running = False

            self.status_value = "idle"

            self.batch_id = None
            self.started_at = None
            self.finished_at = None

            self.current_stage = None
            self.stage_index = 0
            self.total_cycles = 0
            self.stage_count = None

            self.stages_completed = 0
            self.stages_failed = 0

            self.stop_reason = None
            self.error = None

            self.stage_results = []

            # Reset timing state.
            self.started_monotonic = None

            # Persist a clean checkpoint.
            self._save_checkpoint()

            return {
                "status": "reset",
                "batch": self.status(),
            }

    def stop(self, reason="manual_stop"):
        with self.lock:
            if not self.running:
                return {
                    "status": "not_running",
                    "batch": self.status(),
                }

            self.stop_reason = reason
            self.status_value = "stopping"

            self._save_checkpoint()

            return {
                "status": "stop_requested",
                "batch": self.status(),
            }

    # ==============================================================
    # STAGE EXECUTION
    # ==============================================================

    def _stage_result(
        self,
        stage,
        status,
        result=None,
        error=None,
        duration_ms=0,
    ):
        return {
            "stage": stage,
            "status": status,
            "duration_ms": duration_ms,
            "result": self._safe_json(
                result
            ),
            "error": error,
            "timestamp": self.now_iso(),
        }

    def _run_stage(
        self,
        stage: str,
    ):
        """
        Reuse the exact V44 adapter.

        We deliberately do not call Moltbook or AI directly here.
        """

        started = time.monotonic()

        try:

            result = (
                self.engine.invoke_existing_cycle(
                    stage
                )
            )

            duration_ms = int(
                (
                    time.monotonic()
                    - started
                )
                * 1000
            )

            status = str(
                result.get(
                    "status",
                    "completed",
                )
            )

            return self._stage_result(
                stage=stage,
                status=status,
                result=result,
                duration_ms=duration_ms,
            )

        except Exception as exc:

            duration_ms = int(
                (
                    time.monotonic()
                    - started
                )
                * 1000
            )

            return self._stage_result(
                stage=stage,
                status="failed",
                error=(
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
                duration_ms=duration_ms,
            )

    # ==============================================================
    # MAIN LOOP
    # ==============================================================

    def _run(self):
        """
        Execute adaptive V44 cycles until the V45 hard deadline.

        V45 is a time-boxed controller, not a second autonomous engine.

        Every cycle goes through:

            V44.choose_activity()
                ->
            V44.invoke_existing_cycle()
                ->
            V44.calculate_reward()
                ->
            V44.adaptive.learn()
                ->
            V44.memory telemetry

        The only scheduler-level change is that V45 uses
        run_once_for_batch(), which intentionally does not enforce the
        normal 15-minute inter-cycle cooldown.

        Hard deadline:
            V45_BATCH_MAX_MINUTES * 60 seconds

        The loop is best-effort. It cannot guarantee a fixed number of
        AI calls because external API latency is variable.
        """

        try:

            cycle_count = 0

            while True:

                # ------------------------------------------------------
                # HARD TIME CHECK BEFORE EVERY CYCLE
                # ------------------------------------------------------

                if self.time_exceeded():

                    with self.lock:
                        self.status_value = "timeout"
                        self.stop_reason = (
                            "batch_deadline_reached"
                        )
                        self.finished_at = (
                            self.now_iso()
                        )
                        self.running = False
                        self.current_stage = None
                        self._save_checkpoint()

                    return

                # ------------------------------------------------------
                # MANUAL STOP
                # ------------------------------------------------------

                with self.lock:

                    if self.stop_reason == "manual_stop":
                        self.status_value = "stopped"
                        self.finished_at = (
                            self.now_iso()
                        )
                        self.running = False
                        self.current_stage = None
                        self._save_checkpoint()

                        return

                    # Generic stop request.
                    if self.status_value == "stopping":
                        self.status_value = "stopped"

                        if not self.stop_reason:
                            self.stop_reason = (
                                "stop_requested"
                            )

                        self.finished_at = (
                            self.now_iso()
                        )
                        self.running = False
                        self.current_stage = None
                        self._save_checkpoint()

                        return

                # ------------------------------------------------------
                # DAILY AI BUDGET
                # ------------------------------------------------------

                try:
                    budget = self.engine.budget()
                except Exception as exc:

                    with self.lock:
                        self.error = (
                            "budget_check_failed: "
                            f"{exc}"
                        )
                        self.status_value = "failed"
                        self.stop_reason = (
                            "budget_check_failed"
                        )
                        self.finished_at = (
                            self.now_iso()
                        )
                        self.running = False
                        self.current_stage = None
                        self._save_checkpoint()

                    return

                # ------------------------------------------------------
                # DAILY AI BUDGET
                #
                # Once the daily AI budget is exhausted, do not spin
                # through unlimited local health cycles. The batch has
                # completed its useful autonomous work for the day.
                # ------------------------------------------------------

                if (
                    budget.get(
                        "remaining",
                        0,
                    )
                    <= 0
                ):

                    with self.lock:
                        self.status_value = "completed"
                        self.stop_reason = (
                            "budget_exhausted"
                        )
                        self.finished_at = (
                            self.now_iso()
                        )
                        self.running = False
                        self.current_stage = None
                        self._save_checkpoint()

                    return

                # ------------------------------------------------------
                # HARD DEADLINE START GUARD
                #
                # Do not begin another potentially slow cycle if the
                # remaining time is inside the safety window.
                # ------------------------------------------------------

                if not self.deadline_safe_to_start_cycle():

                    with self.lock:
                        self.status_value = "timeout"
                        self.stop_reason = (
                            "batch_deadline_reached"
                        )
                        self.finished_at = (
                            self.now_iso()
                        )
                        self.running = False
                        self.current_stage = None
                        self._save_checkpoint()

                    return

                # ------------------------------------------------------
                # NORMAL ADAPTIVE V44 CYCLE
                # ------------------------------------------------------

                with self.lock:
                    self.current_stage = (
                        "adaptive_cycle"
                    )

                cycle_started = time.monotonic()

                try:

                    result = (
                        self.engine.run_once_for_batch()
                    )

                except Exception as exc:

                    result = {
                        "status": "failed",
                        "error": str(exc),
                        "batch_mode": True,
                    }

                cycle_duration_ms = int(
                    (
                        time.monotonic()
                        - cycle_started
                    )
                    * 1000
                )

                result["duration_ms"] = (
                    cycle_duration_ms
                )

                # ------------------------------------------------------
                # Record cycle progress.
                #
                # Checkpoint periodically instead of serializing the
                # entire growing result list on every cycle.
                # ------------------------------------------------------

                with self.lock:

                    self.stage_results.append(
                        result
                    )

                    cycle_count += 1

                    self.stage_index = (
                        cycle_count
                    )

                    if (
                        cycle_count
                        % self.CHECKPOINT_EVERY_CYCLES
                        == 0
                    ):
                        self._save_checkpoint()

                # ------------------------------------------------------
                # If a cycle itself consumed the remaining time,
                # terminate before starting another one.
                # ------------------------------------------------------

                if self.time_exceeded():

                    with self.lock:
                        self.status_value = "timeout"
                        self.stop_reason = (
                            "batch_deadline_reached"
                        )
                        self.finished_at = (
                            self.now_iso()
                        )
                        self.running = False
                        self.current_stage = None
                        self._save_checkpoint()

                    return

                # ------------------------------------------------------
                # Failed V44 cycles do not destroy the entire batch.
                # V44 has already recorded the failure and updated
                # adaptive learning.
                # ------------------------------------------------------

                if result.get("status") == "failed":
                    # Persist failed-cycle progress before continuing.
                    with self.lock:
                        self._save_checkpoint()

                    # Continue to the next adaptive cycle.
                    time.sleep(
                        self.LOOP_PACING_SECONDS
                    )
                    continue

                # ------------------------------------------------------
                # Safety yield.
                #
                # Prevent a tight CPU loop when a local-only operation
                # returns almost instantly.
                # ------------------------------------------------------

                time.sleep(self.LOOP_PACING_SECONDS)

        except Exception as exc:

            with self.lock:

                self.error = (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

                self.stop_reason = (
                    "batch_engine_exception"
                )

                self.status_value = (
                    "failed"
                )

                self.finished_at = (
                    self.now_iso()
                )

                self.running = False
                self.current_stage = None

                self._save_checkpoint()


    # ==============================================================
    # LEGACY RESET ALIAS
    # ==============================================================

    def reset_checkpoint(self):
        """
        Backward-compatible alias.

        The canonical reset implementation is reset().
        Keep this method only so older callers do not break.
        """
        return self.reset()
