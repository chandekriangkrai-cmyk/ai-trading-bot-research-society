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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _find_metric_node(node: Any) -> dict[str, Any]:
    """Find the most useful overall metric block in historical result JSON."""
    if not isinstance(node, dict):
        return {}

    aliases = {
        "trade_count", "trades", "total_trades", "closed_trade_count",
        "net_profit", "net_pnl", "profit", "total_profit",
        "profit_factor", "pf", "expectancy",
        "max_drawdown", "max_drawdown_absolute", "max_dd", "drawdown",
    }
    if any(k in node for k in aliases):
        return node

    # Prefer an explicitly named overall block.
    overall = node.get("overall")
    if isinstance(overall, dict):
        found = _find_metric_node(overall)
        if found:
            return found

    analysis = node.get("analysis")
    if isinstance(analysis, dict):
        found = _find_metric_node(analysis)
        if found:
            return found

    for key in ("2026", "2025", "2024", "oos", "is", "unseen_2026"):
        value = node.get(key)
        if isinstance(value, dict):
            found = _find_metric_node(value)
            if found:
                return found

    for value in node.values():
        if isinstance(value, dict):
            found = _find_metric_node(value)
            if found:
                return found
    return {}


def _resolve_source_result(db: Any, result: ExperimentResult) -> tuple[ExperimentResult, dict[str, Any]]:
    """Follow source_result_id links so gate results can expose source metrics."""
    current = result
    seen: set[str] = set()

    for _ in range(4):
        raw = _json_object(getattr(current, "metrics", None))
        metric_node = _find_metric_node(raw)
        if metric_node:
            return current, metric_node

        source_id = raw.get("source_result_id")
        if not source_id:
            analysis = raw.get("analysis")
            if isinstance(analysis, dict):
                source_id = analysis.get("source_result_id")
        if not source_id or str(source_id) in seen:
            break

        seen.add(str(source_id))
        source = db.query(ExperimentResult).filter(
            ExperimentResult.id == str(source_id)
        ).first()
        if source is None:
            break
        current = source

    return result, {}


def _build_post(
    experiment: Experiment,
    result: ExperimentResult,
    db: Any | None = None,
) -> tuple[str, str]:
    exp = _model_dict(experiment)
    res = _model_dict(result)

    source_result = result
    metric_node: dict[str, Any] = {}
    if db is not None:
        source_result, metric_node = _resolve_source_result(db, result)

    experiment_id = _pick(exp, "id", "experiment_id")
    status = _pick(exp, "status") or _pick(res, "status") or "completed"
    strategy = (
        _pick(exp, "name", "title")
        or _pick(res, "strategy", "strategy_name")
        or "EURUSD M30 Research"
    )

    net = _pick(metric_node, "net_profit", "net_pnl", "profit", "total_profit")
    pf = _pick(metric_node, "profit_factor", "pf")
    expectancy = _pick(metric_node, "expectancy")
    max_dd = _pick(metric_node, "max_drawdown", "max_drawdown_absolute", "max_dd", "drawdown")
    trades = _pick(metric_node, "trade_count", "trades", "total_trades", "closed_trade_count")

    # If the latest result is a three-year gate, expose its explicit yearly
    # metrics rather than pretending there is one single-year total.
    gate_metrics = _json_object(getattr(result, "metrics", None))
    gate_overall = gate_metrics.get("overall") if isinstance(gate_metrics.get("overall"), dict) else {}
    if gate_overall.get("2024_trade_count") is not None:
        trades = (
            f"2024={gate_overall.get('2024_trade_count')}, "
            f"2025={gate_overall.get('2025_trade_count')}, "
            f"2026={gate_overall.get('2026_trade_count')}"
        )
        net = (
            f"2024={_fmt_number(gate_overall.get('2024_net_profit'))}, "
            f"2025={_fmt_number(gate_overall.get('2025_net_profit'))}, "
            f"2026={_fmt_number(gate_overall.get('2026_net_profit'))}"
        )
        pf = (
            f"2024={_fmt_number(gate_overall.get('2024_profit_factor'))}, "
            f"2025={_fmt_number(gate_overall.get('2025_profit_factor'))}, "
            f"2026={_fmt_number(gate_overall.get('2026_profit_factor'))}"
        )

    title = f"Research Update: {strategy}"

    lines = [
        "AI Trading Bot Research Society — Research Update",
        "",
        f"Experiment: {experiment_id}",
        f"Status: {status}",
        "",
        "Latest research result:",
        f"- Trades: {trades if trades is not None else 'n/a'}",
        f"- Net P&L: {_fmt_number(net)}",
        f"- Profit Factor: {_fmt_number(pf)}",
        f"- Expectancy: {_fmt_number(expectancy)}",
        f"- Max Drawdown: {_fmt_number(max_dd)}",
        "",
        "This is a research/backtest result, not a live-trading signal or financial advice.",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
    ]

    if source_result.id != result.id:
        lines.extend(["", f"Source result: {source_result.id}"])

    known = {
        "id", "experiment_id", "status", "net_profit", "net_pnl", "profit",
        "total_profit", "profit_factor", "pf", "expectancy", "max_drawdown",
        "max_drawdown_absolute", "max_dd", "drawdown", "trades", "trade_count",
        "total_trades", "closed_trade_count", "created_at", "updated_at",
    }
    extras = []
    for key, value in res.items():
        if key in known or value is None:
            continue
        if isinstance(value, (dict, list)):
            continue
        text = str(value)
        if len(text) > 120:
            text = text[:117] + "..."
        extras.append(f"- {key}: {text}")
    if extras:
        lines.extend(["", "Additional result fields:"])
        lines.extend(extras[:12])

    return title, "\n".join(lines)


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

    payload: dict[str, Any] = {
        "title": title,
        "content": content,
    }
    if MOLTBOOK_SUBMOLT:
        payload["submolt"] = MOLTBOOK_SUBMOLT

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
    key = os.getenv("MOLTBOOK_API_KEY", "")
    return {
        "api_base": MOLTBOOK_API_BASE,
        "api_key_configured": bool(key),
        "api_key_type": (
            "developer_app_key" if key.startswith("moltdev_")
            else "agent_key" if key else None
        ),
        "submolt_configured": bool(MOLTBOOK_SUBMOLT),
    }


@router.post("/research/{experiment_id}/publish")
async def publish_research(experiment_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(
            Experiment.id == experiment_id
        ).first()

        if experiment is None:
            raise HTTPException(status_code=404, detail="Experiment not found")

        # Use the newest result for this experiment.
        query = db.query(ExperimentResult).filter(
            ExperimentResult.experiment_id == experiment_id
        )

        if hasattr(ExperimentResult, "created_at"):
            result = query.order_by(ExperimentResult.created_at.desc()).first()
        else:
            result = query.order_by(ExperimentResult.id.desc()).first()

        if result is None:
            raise HTTPException(
                status_code=404,
                detail="No research result found for this experiment",
            )

        title, content = _build_post(experiment, result, db)
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
        if hasattr(ExperimentResult, "created_at"):
            result = query.order_by(ExperimentResult.created_at.desc()).first()
        else:
            result = query.order_by(ExperimentResult.id.desc()).first()

        if result is None:
            raise HTTPException(
                status_code=404,
                detail="No research result found for this experiment",
            )

        title, content = _build_post(experiment, result, db)
        return {
            "status": "preview",
            "experiment_id": experiment_id,
            "title": title,
            "content": content,
        }
    finally:
        db.close()
