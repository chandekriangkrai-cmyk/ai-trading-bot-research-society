"""
V44.9 Adaptive Behavior Engine

The agent does NOT blindly randomize.
It combines:
- historical reward
- exploration
- current opportunity
- time-window history

No political or financial decision-making is performed here.
This is only behavior selection for the research interaction agent.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Dict, List

from .v44_config import (
    DEFAULT_ACTIVITY_WEIGHTS,
    EXPLORATION_RATE,
    LEARNING_RATE,
    MIN_SAMPLES_FOR_ADAPTATION,
)
from .v44_memory import V44Memory


class AdaptiveEngine:

    def __init__(
        self,
        memory: V44Memory,
    ):
        self.memory = memory

        for name, weight in DEFAULT_ACTIVITY_WEIGHTS.items():
            self.memory.ensure_activity(
                name,
                weight,
            )

    def _base_weights(self) -> Dict[str, float]:

        rows = self.memory.get_behavior_stats()

        result = dict(
            DEFAULT_ACTIVITY_WEIGHTS
        )

        for row in rows:
            activity = row["activity"]

            if activity not in result:
                continue

            weight = float(
                row.get("weight", 1.0)
            )

            if weight <= 0:
                weight = 0.05

            result[activity] = weight

        return result

    def choose(
        self,
        eligible: List[str],
    ) -> str:

        if not eligible:
            return "health_check"

        weights = self._base_weights()

        # Exploration:
        # deliberately select from eligible activities
        # sometimes instead of always exploiting history.
        if random.random() < EXPLORATION_RATE:
            return random.choice(eligible)

        candidates = [
            activity
            for activity in eligible
            if activity in weights
        ]

        if not candidates:
            return random.choice(eligible)

        values = [
            max(
                0.05,
                float(weights.get(activity, 1.0)),
            )
            for activity in candidates
        ]

        return random.choices(
            candidates,
            weights=values,
            k=1,
        )[0]

    def learn(
        self,
        activity: str,
        success: bool,
        useful: bool,
        reward: float,
    ):

        self.memory.update_behavior(
            activity=activity,
            success=success,
            useful=useful,
            reward=reward,
        )

        rows = self.memory.get_behavior_stats()

        target = None

        for row in rows:
            if row["activity"] == activity:
                target = row
                break

        if target is None:
            return

        attempts = int(
            target["attempts"]
        )

        if attempts < MIN_SAMPLES_FOR_ADAPTATION:
            return

        current = float(
            target.get("weight", 1.0)
        )

        reward_signal = max(
            -1.0,
            min(1.0, reward),
        )

        desired = current * (
            1.0
            + LEARNING_RATE * reward_signal
        )

        self.memory.set_behavior_weight(
            activity,
            max(
                0.05,
                min(100.0, desired),
            ),
        )

    def explain(self):

        rows = self.memory.get_behavior_stats()

        result = []

        for row in rows:
            attempts = int(
                row["attempts"]
            )

            useful = int(
                row["useful"]
            )

            useful_rate = (
                useful / attempts
                if attempts
                else 0.0
            )

            result.append(
                {
                    "activity": row["activity"],
                    "attempts": attempts,
                    "successes": int(
                        row["successes"]
                    ),
                    "failures": int(
                        row["failures"]
                    ),
                    "useful": useful,
                    "useful_rate": round(
                        useful_rate,
                        4,
                    ),
                    "weight": round(
                        float(row["weight"]),
                        4,
                    ),
                }
            )

        return result
