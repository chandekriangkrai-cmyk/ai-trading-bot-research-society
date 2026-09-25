from __future__ import annotations

import os
from typing import Any

from app.api.moltbook import BASE, req, _solve_challenge
from app.public_safety import sanitize_public_text, sanitize_public_payload


def _key() -> str:
    key = os.getenv("MOLTBOOK_API_KEY", "")
    if not key:
        raise RuntimeError("MOLTBOOK_API_KEY is not configured")
    return key


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
    }


def _extract_posted(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        value = response.get("comment", response)
        if isinstance(value, dict):
            return value
    return {}


def post_comment_auto_verify_sync(
    post_id: str,
    content: str,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """
    Universal autonomous Moltbook comment writer.

    HARD RULE:
      create comment -> obtain challenge -> V43.6 solve -> /verify

    This function NEVER creates a top-level feed post.
    It only creates a COMMENT on an existing post.
    """

    if not post_id:
        raise ValueError("post_id is required")

    safe_content = sanitize_public_text(str(content or "").strip())[:10000]
    if not safe_content:
        raise ValueError("content is empty")

    payload: dict[str, Any] = {
        "content": safe_content,
    }

    if parent_id:
        payload["parent_id"] = str(parent_id)

    status, response = req(
        "POST",
        f"{BASE}/posts/{post_id}/comments",
        _headers(),
        payload,
    )

    if status >= 400:
        raise RuntimeError(
            f"Moltbook comment creation failed HTTP {status}: {response}"
        )

    posted = _extract_posted(response)

    if not posted:
        return sanitize_public_payload({
            "status": "posted_pending_verification",
            "verified": False,
            "published": False,
            "post_id": str(post_id),
            "parent_id": str(parent_id) if parent_id else None,
            "comment": response,
            "verification": {
                "attempted": False,
                "verified": False,
                "reason": "Unexpected Moltbook comment response shape",
            },
        })

    comment_id = posted.get("id")

    verification = posted.get("verification")
    if not isinstance(verification, dict):
        verification = None

    # Some Moltbook responses can represent an already-existing /
    # deduplicated comment without returning a fresh challenge.
    if not verification:
        return sanitize_public_payload({
            "status": "posted_pending_verification",
            "verified": False,
            "published": False,
            "post_id": str(post_id),
            "parent_id": str(parent_id) if parent_id else None,
            "comment_id": str(comment_id) if comment_id else None,
            "comment": posted,
            "verification": {
                "attempted": False,
                "verified": False,
                "reason": "No fresh verification challenge returned",
            },
        })

    challenge = (
        verification.get("challenge_text")
        or verification.get("challenge")
    )

    verification_code = (
        verification.get("verification_code")
        or verification.get("code")
    )

    if not comment_id or not challenge or not verification_code:
        return sanitize_public_payload({
            "status": "posted_pending_verification",
            "verified": False,
            "published": False,
            "post_id": str(post_id),
            "parent_id": str(parent_id) if parent_id else None,
            "comment_id": str(comment_id) if comment_id else None,
            "comment": posted,
            "verification": {
                "attempted": False,
                "verified": False,
                "reason": "Incomplete verification data",
                "challenge_text": challenge,
            },
        })

    try:
        answer, parsed = _solve_challenge(str(challenge))
    except Exception as exc:
        return sanitize_public_payload({
            "status": "verification_failed",
            "verified": False,
            "published": False,
            "post_id": str(post_id),
            "parent_id": str(parent_id) if parent_id else None,
            "comment_id": str(comment_id),
            "comment": posted,
            "verification": {
                "attempted": False,
                "verified": False,
                "reason": f"V43.6 solver failed safely: {exc}",
                "challenge_text": challenge,
            },
        })

    verify_body = {
        "answer": answer,
        "verification_code": str(verification_code),
    }

    verify_status, verify_response = req(
        "POST",
        f"{BASE}/verify",
        _headers(),
        verify_body,
    )

    verified = (
        verify_status < 400
        and isinstance(verify_response, dict)
        and verify_response.get("success") is True
    )

    if verified:
        final_status = "verified"
    else:
        final_status = "verification_failed"

    result = {
        "status": final_status,
        "verified": verified,
        "published": verified,
        "post_id": str(post_id),
        "parent_id": str(parent_id) if parent_id else None,
        "comment_id": str(comment_id),
        "comment": posted,
        "verification": {
            "attempted": True,
            "verified": verified,
            "answer": answer,
            "parsed": parsed,
            "status_code": verify_status,
            "challenge_text": challenge,
            "response": verify_response,
        },
    }

    # Never expose verification_code.
    return sanitize_public_payload(result)


def is_verified_result(result: dict[str, Any]) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") == "verified"
        and result.get("verified") is True
        and result.get("published") is True
    )


def is_pending_result(result: dict[str, Any]) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") == "posted_pending_verification"
        and result.get("verified") is False
    )


def is_failed_result(result: dict[str, Any]) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") in {
            "verification_failed",
            "post_failed",
        }
    )
