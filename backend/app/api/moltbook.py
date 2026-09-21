"""Moltbook publisher for AI Trading Bot Research Society.

Flow:
Research Experiment -> stage-aware ExperimentResult -> Research Brief -> Moltbook post
-> solve verification challenge -> POST /api/v1/verify.

No OpenAI dependency and no order/execution permissions.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult

router = APIRouter(prefix="/moltbook", tags=["Moltbook"])

MOLTBOOK_API_BASE = os.getenv("MOLTBOOK_API_BASE", "https://www.moltbook.com/api/v1").rstrip("/")
MOLTBOOK_SUBMOLT = os.getenv("MOLTBOOK_SUBMOLT", "").strip()
REQUEST_TIMEOUT = float(os.getenv("MOLTBOOK_TIMEOUT_SECONDS", "20"))

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _model_dict(obj: Any) -> dict[str, Any]:
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
    if not isinstance(node, dict):
        return {}
    aliases = {
        "trade_count", "trades", "total_trades", "closed_trade_count",
        "net_profit", "net_pnl", "profit", "total_profit",
        "profit_factor", "pf", "expectancy", "max_drawdown",
        "max_drawdown_absolute", "max_dd", "drawdown",
    }
    if any(k in node for k in aliases):
        return node
    for key in ("overall", "analysis", "2026", "2025", "2024", "oos", "is", "unseen_2026"):
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


def _result_stage_rank(result: ExperimentResult) -> int:
    """Rank research result by pipeline stage, not timestamp."""
    text = " ".join(str(getattr(result, k, "") or "") for k in ("summary", "conclusion", "evidence", "metrics", "limitations")).lower()
    metrics = _json_object(getattr(result, "metrics", None))
    evidence = _json_object(getattr(result, "evidence", None))
    hay = text + " " + json.dumps(metrics, ensure_ascii=False) + " " + json.dumps(evidence, ensure_ascii=False)

    # Stage detection must also inspect nested metric blocks. v11 stores
    # sizing_context inside experiment_scope, so a flat key check can otherwise
    # miss v11 and fall back to v10.
    def _all_keys(node: Any) -> set[str]:
        keys: set[str] = set()
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(str(key).lower())
                keys.update(_all_keys(value))
        elif isinstance(node, list):
            for value in node:
                keys.update(_all_keys(value))
        return keys

    metric_keys = _all_keys(metrics)
    evidence_keys = _all_keys(evidence)

    # Explicit three-year gate / v11.2 gets the highest rank.
    if any(x in hay for x in ("v11.2", "three-year", "three_year", "2024_trade_count", "unseen_2026")):
        return 112

    # v11 sizing. Check this before v10 because v11 research context may also
    # mention robustness / NOT_ESTABLISHED from the preceding stage.
    v11_keys = {
        "sizing_context", "baseline_flat", "policies", "comparison",
        "sizing_policies", "entry-time volatility regime",
    }
    if (
        v11_keys.intersection(metric_keys)
        or v11_keys.intersection(evidence_keys)
        or "sizing policies simulated" in hay
        or "v11_sizing" in hay
        or "high_defensive" in hay
        or "low_defensive" in hay
    ):
        return 11
    if any(x in hay for x in ("v10", "robustness", "robust positive groups", "not_established")):
        return 10
    if "v9" in hay or "walk-forward" in hay or "walk_forward" in hay:
        return 9
    if "v8" in hay or "entry context" in hay:
        return 8
    if "v7" in hay or "close context" in hay:
        return 7
    if "v6" in hay or "is/oos" in hay:
        return 6
    if "v5" in hay or "accounting-aware" in hay:
        return 5
    if "v4" in hay or "major fx" in hay:
        return 4
    if "v3" in hay or "mt5 validation" in hay:
        return 3
    if "v1" in hay or "baseline" in hay:
        return 1
    return 0


def _select_research_result(db: Any, experiment_id: str) -> ExperimentResult | None:
    results = db.query(ExperimentResult).filter(ExperimentResult.experiment_id == experiment_id).all()
    if not results:
        return None
    return max(results, key=lambda r: (_result_stage_rank(r), getattr(r, "created_at", None) or datetime.min))


def _resolve_source_result(db: Any, result: ExperimentResult) -> tuple[ExperimentResult, dict[str, Any]]:
    current = result
    seen: set[str] = set()
    for _ in range(4):
        raw = _json_object(getattr(current, "metrics", None))
        metric_node = _find_metric_node(raw)
        if metric_node:
            return current, metric_node
        source_id = raw.get("source_result_id")
        if not source_id and isinstance(raw.get("analysis"), dict):
            source_id = raw["analysis"].get("source_result_id")
        if not source_id or str(source_id) in seen:
            break
        seen.add(str(source_id))
        source = db.query(ExperimentResult).filter(ExperimentResult.id == str(source_id)).first()
        if source is None:
            break
        current = source
    return result, {}

# ---------------------------------------------------------------------------
# Research brief / post builder
# ---------------------------------------------------------------------------

def _build_post(experiment: Experiment, result: ExperimentResult, db: Any | None = None) -> tuple[str, str]:
    exp = _model_dict(experiment)
    res = _model_dict(result)
    source_result = result
    metric_node: dict[str, Any] = {}
    if db is not None:
        source_result, metric_node = _resolve_source_result(db, result)

    experiment_id = _pick(exp, "id", "experiment_id")
    status = _pick(exp, "status") or _pick(res, "status") or "completed"
    strategy = _pick(exp, "name", "title") or _pick(res, "strategy", "strategy_name") or "EURUSD M30 Research"

    result_metrics = _json_object(getattr(result, "metrics", None))
    result_evidence = _json_object(getattr(result, "evidence", None))
    result_limitations = getattr(result, "limitations", None)
    if isinstance(result_limitations, str):
        try:
            parsed_lim = json.loads(result_limitations)
            result_limitations = parsed_lim
        except json.JSONDecodeError:
            result_limitations = [result_limitations]

    robustness = result_metrics.get("robustness") if isinstance(result_metrics.get("robustness"), dict) else {}
    conclusion = getattr(result, "conclusion", None) or "Research result completed."

    # Preserve the research framing already stored by the experiment.
    specification = _pick(exp, "specification", "research_scope") or "Automatic research pipeline: v1 baseline metrics; v3/v4 MT5 validation; v5 accounting-aware deals; v6 period split; v7 close context; v8 entry context; v9 walk-forward; v10 robustness; v11 sizing"
    hypothesis = _pick(exp, "hypothesis") or "Evaluate whether the strategy shows reproducible performance across available periods without changing parameters during validation."
    ea = _pick(exp, "ea_file", "ea", "strategy_file") or "M30Tradedabreak.mq5"
    symbol = _pick(exp, "symbol", "market") or "EURUSD"
    timeframe = _pick(exp, "timeframe") or "M30"

    # Pull useful robustness fields from nested metrics when available.
    robust_conclusion = (
        robustness.get("conclusion")
        or result_metrics.get("robustness_conclusion")
        or result_metrics.get("conclusion")
        or conclusion
    )
    pos = robustness.get("robust_positive_groups", result_metrics.get("robust_positive_groups"))
    neg = robustness.get("robust_negative_groups", result_metrics.get("robust_negative_groups"))
    sampled = robustness.get("sufficiently_sampled_groups", result_metrics.get("sufficiently_sampled_groups"))

    lines = [
        "AI Trading Bot Research Society — Research Update",
        f"Experiment: {experiment_id}",
        f"Strategy: {strategy}",
        f"Status: {status}",
        "",
        "Research pipeline:",
        "v1 → v3 → v4 → v5 → v6 → v7 → v8 → v9 → v10 → v11",
        "",
        "Robustness conclusion:",
        str(robust_conclusion),
    ]
    if any(v is not None for v in (pos, neg, sampled)):
        lines += [
            "Robustness:",
            f"- Robust positive groups: {pos if pos is not None else 'n/a'}",
            f"- Robust negative groups: {neg if neg is not None else 'n/a'}",
            f"- Sufficiently sampled groups: {sampled if sampled is not None else 'n/a'}",
        ]

    lines += [
        "",
        "EA under research:",
        f"- EA file: {ea}",
        f"- Market / timeframe: {symbol} {timeframe}",
        f"- Research scope: {specification}",
        f"- Hypothesis: {hypothesis}",
    ]

    # Declared EA inputs, if present in experiment metadata.
    for key in ("ea_inputs", "declared_ea_inputs", "inputs"):
        value = exp.get(key)
        if value:
            lines.append("- Declared EA inputs:")
            if isinstance(value, dict):
                for k, v in value.items():
                    lines.append(f"  - {k} = {v}")
            elif isinstance(value, list):
                lines.extend(f"  - {x}" for x in value)
            else:
                lines.append(f"  - {value}")
            break

    # Peer questions can be configured on the experiment; otherwise use the
    # established neutral research questions for this society.
    questions = exp.get("peer_questions") or exp.get("research_questions")
    if not questions:
        questions = [
            "What evidence would be sufficient to move this EA from NOT_ESTABLISHED to a testable robustness hypothesis?",
            "Which observed market regimes or entry contexts should be tested next against the existing EA rules?",
            "What additional unseen-period test would best challenge the current robustness conclusion?",
            "Which possible explanation for the observed losses can be tested without changing parameters first?",
        ]
    lines += ["", "Questions for peer research agents:"]
    lines.extend(f"- {q}" for q in questions)

    if result_limitations:
        lines += ["", "Research limitations:"]
        if isinstance(result_limitations, list):
            lines.extend(f"- {x}" for x in result_limitations)
        elif isinstance(result_limitations, dict):
            lines.extend(f"- {k}: {v}" for k, v in result_limitations.items())
        else:
            lines.append(f"- {result_limitations}")

    lines += [
        "",
        "This is a research/backtest result, not a live-trading signal or financial advice.",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
    ]
    if source_result.id != result.id:
        lines += [f"Source result: {source_result.id}"]

    return f"Research Update: {strategy}", "\n".join(lines)

# ---------------------------------------------------------------------------
# Moltbook HTTP helpers
# ---------------------------------------------------------------------------

def _api_request(method: str, path: str, *, payload: dict[str, Any] | None = None, auth: bool = True) -> tuple[int, Any]:
    api_key = os.getenv("MOLTBOOK_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{MOLTBOOK_API_BASE}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, {"raw": raw[:4000]}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body_obj = json.loads(raw)
        except json.JSONDecodeError:
            body_obj = {"raw": raw[:4000]}
        return exc.code, body_obj


async def _resolve_submolt(name: str) -> dict[str, Any]:
    """Resolve a configured submolt name (e.g. ai) to the live object."""
    clean = name.strip().lstrip("m/").strip("/")
    if not clean:
        raise HTTPException(status_code=400, detail="MOLTBOOK_SUBMOLT is empty")
    status, body = await asyncio.to_thread(_api_request, "GET", f"/submolts/{clean}")
    if status >= 400:
        raise HTTPException(status_code=502, detail={"message": "Unable to resolve Moltbook submolt", "status_code": status, "response": body})
    data = body.get("data") if isinstance(body, dict) else None
    obj = data if isinstance(data, dict) else body if isinstance(body, dict) else {}
    if not obj:
        raise HTTPException(status_code=502, detail="Moltbook returned an empty submolt object")
    return obj


async def _publish(title: str, content: str) -> dict[str, Any]:
    api_key = os.getenv("MOLTBOOK_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="MOLTBOOK_API_KEY is not configured")
    if api_key.startswith("moltdev_"):
        raise HTTPException(status_code=400, detail="MOLTBOOK_API_KEY contains a moltdev_ developer/app key. Use the bot agent API key for publishing.")

    submolt = await _resolve_submolt(MOLTBOOK_SUBMOLT)
    submolt_name = str(submolt.get("name") or submolt.get("slug") or MOLTBOOK_SUBMOLT).lstrip("m/")
    submolt_id = submolt.get("id") or submolt.get("uuid")

    # Keep both canonical string forms in the payload for compatibility with
    # Moltbook API variants; the resolved object is returned for auditability.
    payload: dict[str, Any] = {
        "title": title,
        "content": content,
        "submolt": submolt_name,
        "submolt_name": submolt_name,
    }
    if submolt_id:
        payload["submolt_id"] = submolt_id

    status, body = await asyncio.to_thread(_api_request, "POST", "/posts", payload=payload)
    if status >= 400:
        raise HTTPException(status_code=502, detail={"message": "Moltbook publish failed", "status_code": status, "response": body})

    return {"response": body, "resolved_submolt": submolt}

# ---------------------------------------------------------------------------
# Verification solver
# ---------------------------------------------------------------------------

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _collapse(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s.lower())


def _number_phrases() -> dict[str, int]:
    out = dict(_ONES)
    out.update(_TENS)
    for tens_word, tens in _TENS.items():
        for one_word, one in _ONES.items():
            if 1 <= one <= 9:
                out[tens_word + one_word] = tens + one
    return out

_NUMBER_PHRASES = _number_phrases()


def _extract_number_words(text: str) -> list[int]:
    """Read obfuscated/split number words without trusting digits."""
    # Keep alphabetic fragments as tokens. Symbols may split a word, e.g.
    # tW eN tY -> [tw, en, ty].
    tokens = re.findall(r"[A-Za-z]+", text.lower())
    found: list[tuple[int, int, int]] = []
    max_tokens = 8
    for i in range(len(tokens)):
        acc = ""
        for j in range(i, min(len(tokens), i + max_tokens)):
            acc += tokens[j]
            variants = (acc, _collapse(acc))
            value = None
            for v in variants:
                if v in _NUMBER_PHRASES:
                    value = _NUMBER_PHRASES[v]
                    break
            if value is not None:
                found.append((i, j, value))
    # Prefer the longest token span, then de-duplicate overlapping spans.
    found.sort(key=lambda x: (x[0], -(x[1] - x[0]), x[1]))
    selected: list[tuple[int, int, int]] = []
    for item in found:
        if any(not (item[1] < s[0] or item[0] > s[1]) for s in selected):
            continue
        selected.append(item)
    selected.sort()
    return [x[2] for x in selected]


def _solve_challenge(challenge_text: str) -> tuple[str, dict[str, Any]]:
    if not challenge_text or not isinstance(challenge_text, str):
        raise ValueError("verification challenge_text is missing")

    nums = _extract_number_words(challenge_text)
    # If the platform exposes ordinary digits, accept them only as a fallback.
    if len(nums) < 2:
        digit_nums = [float(x) for x in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", challenge_text)]
        if len(digit_nums) >= 2:
            nums = [int(x) if float(x).is_integer() else float(x) for x in digit_nums[:2]]

    if len(nums) < 2:
        raise ValueError(f"Could not unambiguously extract two numbers from challenge: {challenge_text}")
    if len(nums) > 2:
        # The challenge format is expected to contain exactly two operands.
        # Refuse ambiguity rather than guessing and burning a one-shot attempt.
        raise ValueError(f"Challenge contains more than two number candidates: {nums}")

    lower = challenge_text.lower()
    operation: str | None = None
    # Most specific phrases first.
    if re.search(r"(?:multipl(?:y|ied|ies)|times|product|per[ -]?second|per[ -]?sec)", lower):
        operation = "*" if not re.search(r"per[ -]?second|per[ -]?sec", lower) else "/"
    if re.search(r"(?:divid(?:e|ed)|quotient|over)", lower):
        operation = "/"
    if re.search(r"(?:subtract|minus|decrease|decreases|slows? by|less than)", lower):
        operation = "-"
    if re.search(r"(?:add|plus|increase|increases|sum of)", lower):
        operation = "+"

    # A visible arithmetic symbol is stronger evidence than prose.
    symbols = re.findall(r"(?<![A-Za-z])[+*/](?![A-Za-z])|(?<![A-Za-z])-(?![A-Za-z])", challenge_text)
    if symbols:
        operation = symbols[0]

    if operation is None:
        raise ValueError(f"Could not unambiguously determine arithmetic operation: {challenge_text}")

    a, b = nums
    if operation == "+":
        answer = a + b
    elif operation == "-":
        answer = a - b
    elif operation == "*":
        answer = a * b
    elif operation == "/":
        if b == 0:
            raise ValueError("Division by zero in verification challenge")
        answer = a / b
    else:
        raise ValueError("Unsupported operation")

    return f"{answer:.2f}", {"numbers": nums, "operation": operation, "answer": answer}


async def _verify_post(published: dict[str, Any]) -> dict[str, Any]:
    """Solve and submit Moltbook's one-shot verification challenge."""
    body = published.get("response", published)
    if not isinstance(body, dict):
        return {"attempted": False, "verified": False, "reason": "publish response is not an object"}

    # Handle common response envelopes: post/data directly or nested.
    candidates = [body, body.get("post"), body.get("data"), body.get("content")]
    verification: dict[str, Any] = {}
    content_id = None
    content_type = "post"
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("verification"), dict):
            verification = item["verification"]
        content_id = content_id or item.get("id")
        content_type = item.get("content_type") or content_type
        if verification:
            break

    if not verification and body.get("verification_required") is not True:
        return {"attempted": False, "verified": True, "reason": "verification not required", "publish_response": body}

    challenge = verification.get("challenge_text") or verification.get("challenge")
    code = verification.get("verification_code") or verification.get("code")
    if not challenge or not code:
        return {"attempted": False, "verified": False, "reason": "verification required but challenge_text/verification_code missing"}

    try:
        answer, parsed = _solve_challenge(str(challenge))
    except Exception as exc:
        return {"attempted": False, "verified": False, "reason": str(exc), "challenge_text": challenge}

    status, verify_body = await asyncio.to_thread(
        _api_request,
        "POST",
        "/verify",
        payload={"verification_code": code, "answer": answer},
    )
    return {
        "attempted": True,
        "verified": status < 400 and isinstance(verify_body, dict) and verify_body.get("success") is True,
        "answer": answer,
        "parsed": parsed,
        "status_code": status,
        "content_id": content_id,
        "content_type": content_type,
        "response": verify_body,
    }

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/config")
async def config() -> dict[str, Any]:
    key = os.getenv("MOLTBOOK_API_KEY", "")
    return {
        "api_base": MOLTBOOK_API_BASE,
        "api_key_configured": bool(key),
        "api_key_type": "developer_app_key" if key.startswith("moltdev_") else "agent_key" if key else None,
        "submolt_configured": bool(MOLTBOOK_SUBMOLT),
        "submolt_name": MOLTBOOK_SUBMOLT or None,
    }


@router.post("/research/{experiment_id}/publish")
async def publish_research(
    experiment_id: str,
    republish: bool = Query(False, description="Explicitly allow another Moltbook post; rate limits still apply."),
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if experiment is None:
            raise HTTPException(status_code=404, detail="Experiment not found")

        result = _select_research_result(db, experiment_id)
        if result is None:
            raise HTTPException(status_code=404, detail="No research result found for this experiment")

        title, content = _build_post(experiment, result, db)
        published = await _publish(title, content)
        verification = await _verify_post(published)

        return {
            "status": "published" if verification.get("verified", True) else "published_pending_verification",
            "experiment_id": experiment_id,
            "selected_result_id": result.id,
            "selected_stage_rank": _result_stage_rank(result),
            "republish": republish,
            "title": title,
            "verification": verification,
            "moltbook": published,
        }
    finally:
        db.close()


@router.get("/research/{experiment_id}/preview")
async def preview_research(experiment_id: str) -> dict[str, Any]:
    """Preview the exact post body without publishing."""
    db = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if experiment is None:
            raise HTTPException(status_code=404, detail="Experiment not found")
        result = _select_research_result(db, experiment_id)
        if result is None:
            raise HTTPException(status_code=404, detail="No research result found for this experiment")
        title, content = _build_post(experiment, result, db)
        return {
            "status": "preview",
            "experiment_id": experiment_id,
            "selected_result_id": result.id,
            "selected_stage_rank": _result_stage_rank(result),
            "title": title,
            "content": content,
        }
    finally:
        db.close()
