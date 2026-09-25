"""
V44.9 Daily Research Report
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from .v44_memory import V44Memory


class DailyReport:

    def __init__(
        self,
        memory: V44Memory,
    ):
        self.memory = memory

    @staticmethod
    def _bar(value: float, width: int = 10) -> str:

        value = max(
            0.0,
            min(1.0, value),
        )

        filled = int(
            round(value * width)
        )

        return (
            "█" * filled
            + "░" * (width - filled)
        )

    def build(
        self,
        day_key: str,
    ) -> str:

        summary = self.memory.summary(
            day_key
        )

        budget = summary["budget"]

        reserved = budget["reserved"]
        completed = budget["completed"]
        failed = budget["failed"]

        replies = summary["replies"]

        lines = []

        lines.append(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        lines.append(
            "AI TRADING BOT RESEARCH SOCIETY"
        )
        lines.append(
            "V44.9 DAILY AUTONOMOUS REPORT"
        )
        lines.append(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            f"DATE: {day_key}"
        )

        lines.append("")

        lines.append("AI BUDGET")
        lines.append(
            f"Reserved : {reserved}"
        )
        lines.append(
            f"Completed: {completed}"
        )
        lines.append(
            f"Failed   : {failed}"
        )

        lines.append("")

        lines.append("REPLIES")
        lines.append(
            f"Published: {replies['total']}"
        )
        lines.append(
            f"Verified : {replies['verified']}"
        )

        lines.append("")

        lines.append("ACTIVITY")

        for row in summary["activities"]:
            lines.append(
                f"- {row['activity']}: "
                f"{row['count']}"
            )

        lines.append("")

        lines.append("BEHAVIOR")

        for row in summary["behavior"]:
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

            lines.append(
                f"- {row['activity']}: "
                f"weight={float(row['weight']):.2f} "
                f"n={attempts} "
                f"useful={useful_rate:.0%} "
                f"{self._bar(useful_rate)}"
            )

        lines.append("")

        lines.append("TIME WINDOWS")

        for row in summary["time_windows"]:

            attempts = int(
                row["attempts"]
            )

            successes = int(
                row["successes"]
            )

            rate = (
                successes / attempts
                if attempts
                else 0.0
            )

            lines.append(
                f"- {int(row['hour']):02d}:00 "
                f"n={attempts} "
                f"success={rate:.0%} "
                f"AI={int(row['ai_requests'])} "
                f"reply={int(row['replies'])} "
                f"verified={int(row['verified'])}"
            )

        lines.append("")

        lines.append(
            "HUMAN-ONLY FEED POST:"
        )
        lines.append(
            "Top-level Feed publishing is disabled "
            "for Autonomous mode."
        )

        lines.append(
            "The owner must create Feed Posts manually."
        )

        lines.append("")

        lines.append(
            "Memory is persistent and continues "
            "across autonomous cycles."
        )

        lines.append(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        return "\n".join(lines)

    def save(
        self,
        day_key: str,
    ) -> str:

        report = self.build(
            day_key
        )

        self.memory.save_report(
            day_key,
            report,
        )

        return report
