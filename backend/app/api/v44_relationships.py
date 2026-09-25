from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..v44_relationships import RelationshipManager


router = APIRouter(prefix="/v44-relationships", tags=["V44 Relationships"])


_manager = RelationshipManager()


class ObserveRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=200)
    observation: dict[str, Any] = Field(default_factory=dict)


class ActionRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=50)


def _manager_status() -> dict[str, Any]:
    fn = getattr(_manager, "status", None)

    if callable(fn):
        result = fn()
        return result if isinstance(result, dict) else {"status": result}

    return {
        "status": "ready",
        "manager": "v44_relationships",
    }


@router.get("/status")
async def relationship_status():
    return _manager_status()


@router.get("/{agent_id}")
async def relationship_get(agent_id: str):
    fn = getattr(_manager, "get_relationship", None)

    if callable(fn):
        result = fn(agent_id)
        return result if isinstance(result, dict) else {"agent_id": agent_id, "result": result}

    fn = getattr(_manager, "relationship", None)

    if callable(fn):
        result = fn(agent_id)
        return result if isinstance(result, dict) else {"agent_id": agent_id, "result": result}

    return {
        "agent_id": agent_id,
        "status": "unknown",
        "message": "Relationship record is not available through the current manager API.",
    }


@router.post("/{agent_id}/observe")
async def relationship_observe(
    agent_id: str,
    payload: ObserveRequest,
):
    if payload.agent_id != agent_id:
        raise HTTPException(
            status_code=400,
            detail="agent_id in body must match path agent_id",
        )

    fn = getattr(_manager, "record_observation", None)

    if not callable(fn):
        raise HTTPException(
            status_code=500,
            detail="RelationshipManager.record_observation is unavailable",
        )

    try:
        result = fn(agent_id, payload.observation)

        return {
            "status": "recorded",
            "agent_id": agent_id,
            "result": result,
        }

    except TypeError:
        try:
            result = fn(
                agent_id=agent_id,
                observation=payload.observation,
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
    fn = getattr(_manager, "execute_follow_or_unfollow", None)

    if not callable(fn):
        return {
            "status": "blocked",
            "agent_id": agent_id,
            "action": action,
            "reason": "External Follow/Unfollow API is not implemented or verified.",
        }

    try:
        result = fn(agent_id, action)

        if isinstance(result, dict):
            return result

        return {
            "status": "blocked",
            "agent_id": agent_id,
            "action": action,
            "result": result,
        }

    except Exception as exc:
        return {
            "status": "blocked",
            "agent_id": agent_id,
            "action": action,
            "error": str(exc),
        }
