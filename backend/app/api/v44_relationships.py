from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import v44_relationships as relationships


router = APIRouter(
    prefix="/v44-relationships",
    tags=["V44 Relationships"],
)


class ObserveRequest(BaseModel):
    agent_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
    )

    observation: dict[str, Any] = Field(
        default_factory=dict,
    )


@router.get("/status")
async def relationship_status():
    """
    Return deterministic relationship state.

    No external Moltbook action.
    """
    try:
        return relationships.status()

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc


@router.get("/{agent_id}")
async def relationship_get(agent_id: str):
    """
    Read one relationship record.

    No external action.
    """
    try:
        result = relationships.get_relationship(agent_id)

        if result is None:
            return {
                "agent_id": agent_id,
                "status": "unknown",
                "message": "No relationship record found.",
            }

        return result

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc


@router.post("/{agent_id}/observe")
async def relationship_observe(
    agent_id: str,
    payload: ObserveRequest,
):
    """
    Store a deterministic relationship observation.

    Expected optional observation fields:

      agent_name: string
      interaction: boolean
      feedback: "up" | "down"

    Unknown fields are preserved only as metadata-safe input and
    do not trigger external actions.
    """

    if payload.agent_id != agent_id:
        raise HTTPException(
            status_code=400,
            detail="agent_id in body must match path agent_id",
        )

    observation = payload.observation or {}

    agent_name = str(
        observation.get("agent_name", "")
    )[:200]

    interaction = bool(
        observation.get("interaction", False)
    )

    feedback = observation.get("feedback")

    if feedback not in {None, "up", "down"}:
        raise HTTPException(
            status_code=400,
            detail='feedback must be null, "up", or "down"',
        )

    try:
        result = relationships.record_observation(
            agent_id=agent_id,
            agent_name=agent_name,
            interaction=interaction,
            feedback=feedback,
        )

        return {
            "status": "recorded",
            "agent_id": agent_id,
            "result": result,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc


@router.post("/{agent_id}/action/{action}")
async def relationship_action(
    agent_id: str,
    action: str,
):
    """
    Relationship action adapter.

    The current core intentionally blocks actual external
    Follow/Unfollow execution until a verified Moltbook endpoint
    is explicitly implemented.
    """

    try:
        result = relationships.execute_follow_or_unfollow(
            agent_id=agent_id,
            action=action,
        )

        return result

    except Exception as exc:
        return {
            "status": "blocked",
            "agent_id": agent_id,
            "action": action,
            "error": str(exc),
        }
