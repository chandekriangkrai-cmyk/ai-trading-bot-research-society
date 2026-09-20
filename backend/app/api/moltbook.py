"""
Moltbook publisher for AI Trading Bot Research Society.

MVP flow:
Research Experiment -> latest ExperimentResult -> Moltbook post.

No OpenAI dependency and no order/execution permissions.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import asyncio
import json
import urllib.error
import urllib.request
import uuid
from fastapi import APIRouter, HTTPException

from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult


router = APIRouter(prefix="/moltbook", tags=["Moltbook"])

MOLTBOOK_API_BASE = os.getenv(
    "MOLTBOOK_API_BASE",
    "https://www.moltbook.com/api/v1",
).rstrip("/")
MOLTBOOK_SUBMOLT = os.getenv("MOLTBOOK_SUBMOLT", "").strip()
REQUEST_TIMEOUT = float(os.getenv("MOLTBOOK_TIMEOUT_SECONDS", "20"))


def _request_json(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    """Small stdlib JSON HTTP helper used for Moltbook API calls."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                parsed: dict[str, Any] | str = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw[:1000]}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:1000]}
        return exc.code, parsed


async def _resolve_submolt() -> dict[str, str]:
    """Resolve configured submolt name/UUID to the fields required by POST /posts."""
    configured = MOLTBOOK_SUBMOLT
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="MOLTBOOK_SUBMOLT is not configured",
        )

    # Moltbook's posts API currently requires all three fields:
    # submolt (string), submolt_name (string), and submolt_id (UUID).
    status, body = await asyncio.to_thread(
        _request_json,
        "GET",
        f"{MOLTBOOK_API_BASE}/submolts",
    )
    if status >= 400 or not isinstance(body, dict):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Could not resolve Moltbook submolt",
                "status_code": status,
                "response": body,
            },
        )

    submolts = body.get("submolts")
    if not isinstance(submolts, list):
        raise HTTPException(
            status_code=502,
            detail="Moltbook /submolts response did not contain a submolts list",
        )

    configured_lower = configured.lower()
    match = None
    for item in submolts:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        item_name = str(item.get("name") or "")
        item_display = str(item.get("display_name") or "")
        if (
            configured_lower == item_name.lower()
            or configured_lower == item_display.lower()
            or configured == item_id
        ):
            match = item
            break

    if match is None:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Configured Moltbook submolt was not found",
                "configured_submolt": configured,
            },
        )

    submolt_id = str(match.get("id") or "")
    submolt_name = str(match.get("name") or "")
    if not submolt_id or not submolt_name:
        raise HTTPException(
            status_code=502,
            detail="Resolved Moltbook submolt is missing id or name",
        )

    try:
        uuid.UUID(submolt_id)
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail=f"Moltbook returned an invalid submolt UUID: {submolt_id}",
        )

    return {
        "submolt": submolt_name,
        "submolt_name": submolt_name,
        "submolt_id": submolt_id,
    }


def _model_dict(obj: Any) -> dict[str, Any]:
    """Serialize SQLAlchemy model columns without exposing secrets."""
    result: dict[str, Any] = {}
    table = getattr(obj, "__table__", None)
    columns = getattr(table, "columns", []) if table is not None else []
    for column in columns:
        value = getattr(obj, column.name, None)
        if isinstance(value, datetime):
            value = value.isoformat()
        result[column.name] = value
    return result


def _pick(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _fmt_number(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}
    return {}


def _is_v10_result(result: ExperimentResult) -> bool:
    metrics = _json_dict(getattr(result, "metrics", None))
    method = str(metrics.get("method", ""))
    return method.startswith("v10 robustness gate") or (
        "robust_positive_groups" in metrics and "robust_negative_groups" in metrics
    )


def _find_metric_row(node: Any) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    keys = {"trade_count", "net_profit", "profit_factor", "expectancy", "max_drawdown_absolute"}
    if not any(k in node for k in keys):
        return None
    return node


def _find_year_rows(node: Any, year: str | None = None) -> dict[str, dict[str, Any]]:
    """Find year-labelled metric rows without assuming one exact result schema."""
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(node, dict):
        return found

    for key, value in node.items():
        next_year = year
        key_text = str(key)
        if key_text in {"2024", "2025", "2026", "2027", "2028"}:
            next_year = key_text
        if isinstance(value, dict):
            row = _find_metric_row(value)
            if row is not None and next_year:
                found[next_year] = row
            found.update(_find_year_rows(value, next_year))
    return found


def _metric_text(row: dict[str, Any]) -> list[str]:
    trades = _pick(row, "trade_count", "trades", "total_trades")
    net = _pick(row, "net_profit", "net_pnl", "profit", "total_profit")
    pf = _pick(row, "profit_factor", "pf")
    expectancy = _pick(row, "expectancy")
    dd = _pick(row, "max_drawdown_absolute", "max_drawdown", "max_dd", "drawdown")
    return [
        f"- Trades: {trades if trades is not None else 'n/a'}",
        f"- Net P&L: {_fmt_number(net)}",
        f"- Profit Factor: {_fmt_number(pf)}",
        f"- Expectancy: {_fmt_number(expectancy)}",
        f"- Max Drawdown: {_fmt_number(dd)}",
    ]


def _build_post(
    experiment: Experiment,
    result: ExperimentResult,
    related_results: list[ExperimentResult] | None = None,
) -> tuple[str, str]:
    exp = _model_dict(experiment)
    res = _model_dict(result)
    metrics = _json_dict(res.get("metrics"))

    experiment_id = _pick(exp, "id", "experiment_id")
    status = _pick(exp, "status") or _pick(res, "status") or "completed"
    strategy = (
        _pick(exp, "name", "title")
        or _pick(res, "strategy", "strategy_name")
        or "EURUSD M30 Research"
    )

    title = f"Research Update: {strategy}"
    conclusion = (
        metrics.get("summary", {}).get("conclusion")
        if isinstance(metrics.get("summary"), dict)
        else None
    ) or metrics.get("conclusion") or _pick(res, "conclusion") or "NOT_ESTABLISHED"

    lines = [
        "AI Trading Bot Research Society — Research Update",
        "",
        f"Experiment: {experiment_id}",
        f"Strategy: {strategy}",
        f"Status: {status}",
        "",
        "Research pipeline:",
        "v1 → v3 → v4 → v5 → v6 → v7 → v8 → v9 → v10",
        "",
        "Robustness conclusion:",
        str(conclusion),
    ]

    # Prefer the v10 result's source (v9) for the walk-forward headline metrics.
    source_id = metrics.get("source_result_id")
    source_metrics: dict[str, Any] = {}
    if related_results:
        for item in related_results:
            if str(getattr(item, "id", "")) == str(source_id):
                source_metrics = _json_dict(getattr(item, "metrics", None))
                break

    overall_is = source_metrics.get("overall_is")
    overall_oos = source_metrics.get("overall_oos")
    if isinstance(overall_is, dict) or isinstance(overall_oos, dict):
        lines.extend(["", "Walk-forward reference:"])
        if isinstance(overall_is, dict):
            lines.append("2024 IS:")
            lines.extend(_metric_text(overall_is)[:3])
        if isinstance(overall_oos, dict):
            lines.append("2025 OOS:")
            lines.extend(_metric_text(overall_oos)[:3])

    summary = metrics.get("summary") if isinstance(metrics.get("summary"), dict) else {}
    if summary:
        lines.extend([
            "",
            "Robustness:",
            f"- Robust positive groups: {summary.get('robust_positive_groups', 0)}",
            f"- Robust negative groups: {summary.get('robust_negative_groups', 0)}",
            f"- Sufficiently sampled groups: {summary.get('sufficiently_sampled_groups', 0)}",
        ])

    # If another result in this experiment contains explicit year-labelled rows,
    # include them as historical references. Do not invent missing years.
    if related_results:
        year_rows: dict[str, dict[str, Any]] = {}
        for item in related_results:
            year_rows.update(_find_year_rows(_json_dict(getattr(item, "metrics", None))))
        for year in ("2024", "2025", "2026"):
            row = year_rows.get(year)
            if row and not ((year == "2024" and isinstance(overall_is, dict)) or (year == "2025" and isinstance(overall_oos, dict))):
                lines.extend(["", f"{year} reference:"] + _metric_text(row))

    limitations = metrics.get("limitations")
    if isinstance(limitations, list) and limitations:
        lines.extend(["", "Research limitations:"])
        for item in limitations[:4]:
            lines.append(f"- {item}")

    lines.extend([
        "",
        "This is a research/backtest result, not a live-trading signal or financial advice.",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Source result: {source_id}" if source_id else "",
    ])
    return title, "\n".join(line for line in lines if line != "")


async def _publish(title: str, content: str) -> dict[str, Any]:
    api_key = os.getenv("MOLTBOOK_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="MOLTBOOK_API_KEY is not configured",
        )

    # Agent API keys are used as Bearer credentials. Developer/app keys
    # (moltdev_...) are a separate Moltbook capability.
    if api_key.startswith("moltdev_"):
        raise HTTPException(
            status_code=400,
            detail=(
                "MOLTBOOK_API_KEY contains a moltdev_ developer/app key. "
                "Use the bot agent API key for publishing."
            ),
        )

    submolt = await _resolve_submolt()

    payload: dict[str, Any] = {
        "title": title,
        "content": content,
        **submolt,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body_bytes = json.dumps(payload).encode("utf-8")

    def _request() -> tuple[int, dict[str, Any] | str, bool]:
        request = urllib.request.Request(
            f"{MOLTBOOK_API_BASE}/posts",
            data=body_bytes,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
                try:
                    parsed: dict[str, Any] | str = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"raw": raw[:1000]}
                return response.status, parsed, False
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw[:1000]}
            return exc.code, parsed, exc.code in (301, 302, 307, 308)

    status_code, body, redirected = await asyncio.to_thread(_request)

    if redirected:
        raise HTTPException(
            status_code=502,
            detail="Moltbook redirected the API request; check MOLTBOOK_API_BASE.",
        )

    if status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Moltbook publish failed",
                "status_code": status_code,
                "response": body,
            },
        )

    return body if isinstance(body, dict) else {"response": body}


@router.get("/config")
async def config() -> dict[str, Any]:
    """Show Moltbook config plus the exact resolved submolt object."""
    key = os.getenv("MOLTBOOK_API_KEY", "")
    result: dict[str, Any] = {
        "api_base": MOLTBOOK_API_BASE,
        "api_key_configured": bool(key),
        "api_key_type": (
            "developer_app_key" if key.startswith("moltdev_")
            else "agent_key" if key else None
        ),
        "submolt_configured": bool(MOLTBOOK_SUBMOLT),
        "submolt_configured_value": MOLTBOOK_SUBMOLT or None,
        "submolt_resolved": False,
        "resolved_submolt": None,
    }

    if not MOLTBOOK_SUBMOLT:
        result["resolve_error"] = "MOLTBOOK_SUBMOLT is not configured"
        return result

    try:
        resolved = await _resolve_submolt()
    except HTTPException as exc:
        result["resolve_error"] = exc.detail
        return result

    result["submolt_resolved"] = True
    result["resolved_submolt"] = resolved
    return result


@router.post("/research/{experiment_id}/publish")
async def publish_research(experiment_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(
            Experiment.id == experiment_id
        ).first()

        if experiment is None:
            raise HTTPException(status_code=404, detail="Experiment not found")

        query = db.query(ExperimentResult).filter(
            ExperimentResult.experiment_id == experiment_id
        )
        all_results = query.all()
        result = next((row for row in all_results if _is_v10_result(row)), None)
        if result is None:
            result = max(
                all_results,
                key=lambda row: getattr(row, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc),
            ) if all_results else None

        if result is None:
            raise HTTPException(
                status_code=404,
                detail="No research result found for this experiment",
            )

        title, content = _build_post(experiment, result, all_results)
        published = await _publish(title, content)

        return {
            "status": "published",
            "experiment_id": experiment_id,
            "title": title,
            "moltbook": published,
        }
    finally:
        db.close()


@router.get("/research/{experiment_id}/preview")
async def preview_research(experiment_id: str) -> dict[str, Any]:
    """Preview the exact post body without publishing."""
    db = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(
            Experiment.id == experiment_id
        ).first()
        if experiment is None:
            raise HTTPException(status_code=404, detail="Experiment not found")

        query = db.query(ExperimentResult).filter(
            ExperimentResult.experiment_id == experiment_id
        )
        all_results = query.all()
        result = next((row for row in all_results if _is_v10_result(row)), None)
        if result is None:
            result = max(
                all_results,
                key=lambda row: getattr(row, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc),
            ) if all_results else None

        if result is None:
            raise HTTPException(
                status_code=404,
                detail="No research result found for this experiment",
            )

        title, content = _build_post(experiment, result, all_results)
        return {
            "status": "preview",
            "experiment_id": experiment_id,
            "title": title,
            "content": content,
        }
    finally:
        db.close()
