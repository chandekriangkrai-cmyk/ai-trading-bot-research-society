"""
V44.9 Human-only Feed Post Firewall.

This is deliberately independent of AI output.
If an action represents creation of a new top-level Feed post,
Autonomous mode is forbidden from executing it.

Comment replies are NOT top-level Feed posts.
"""

from __future__ import annotations

from typing import Any, Dict


FORBIDDEN = {
    "create_post",
    "publish_post",
    "top_level_post",
    "feed_post",
    "repost_as_new_post",
    "schedule_feed_post",
}


class HumanOnlyFeedPostFirewall:

    @staticmethod
    def is_top_level_post_action(
        action: str,
    ) -> bool:

        normalized = (
            str(action or "")
            .strip()
            .lower()
        )

        return normalized in FORBIDDEN

    @classmethod
    def check(
        cls,
        action: str,
        autonomous: bool = True,
    ) -> Dict[str, Any]:

        if autonomous and cls.is_top_level_post_action(
            action
        ):
            return {
                "allowed": False,
                "reason": "TOP_LEVEL_FEED_POST_REQUIRES_HUMAN",
                "action": action,
            }

        return {
            "allowed": True,
            "reason": "ALLOWED",
            "action": action,
        }

    @classmethod
    def assert_allowed(
        cls,
        action: str,
        autonomous: bool = True,
    ):

        result = cls.check(
            action,
            autonomous=autonomous,
        )

        if not result["allowed"]:
            raise PermissionError(
                result["reason"]
            )

        return result
