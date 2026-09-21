"""
Moltbook publisher for AI Trading Bot Research Society.

Research flow:
EA/Mission -> Experiment evidence -> strategy-aware Moltbook research brief -> peer-agent feedback intake.

No order/execution permissions. Moltbook feedback is research input only; it never changes EA parameters automatically.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import asyncio
import json
import urllib.error
import urllib.request
import uuid
from fastapi import APIRouter, HTTPException

from app.database import SessionLocal
from app.models import Mission
from app.ea_files import EAFile
from app.research_models import Experiment, ExperimentResult, Hypothesis, ResearchLead


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



def _strategy_context(db, experiment: Experiment) -> dict[str, Any]:
    """Load the actual mission/EA/hypothesis attached to this experiment.

    This is intentionally evidence-only: the Moltbook post must describe the
    EA from database records, not invent trading rules from aggregate metrics.
    """
    mission = None
    if getattr(experiment, "mission_id", None):
        mission = db.get(Mission, str(experiment.mission_id))

    ea = None
    if getattr(experiment, "ea_file_id", None):
        ea = db.get(EAFile, str(experiment.ea_file_id))
    if ea is None and mission is not None:
        ea = (
            db.query(EAFile)
            .filter(EAFile.mission_id == mission.id)
            .order_by(EAFile.created_at.desc())
            .first()
        )

    hypothesis = None
    if getattr(experiment, "hypothesis_id", None):
        hypothesis = db.get(Hypothesis, str(experiment.hypothesis_id))

    return {"mission": mission, "ea": ea, "hypothesis": hypothesis}


def _compact_ea_inputs(source_code: str | None, limit: int = 8) -> list[str]:
    """Extract only explicit MQL5 input declarations; never infer rules."""
    if not source_code:
        return []
    rows: list[str] = []
    pattern = re.compile(r"^\s*input\s+(.+?);\s*(?://.*)?$", re.IGNORECASE)
    for raw in source_code.splitlines():
        line = raw.strip()
        match = pattern.match(line)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip()
        if len(value) > 180:
            value = value[:177] + "..."
        rows.append(value)
        if len(rows) >= limit:
            break
    return rows


def _append_strategy_context(lines: list[str], context: dict[str, Any], metrics: dict[str, Any]) -> None:
    mission = context.get("mission")
    ea = context.get("ea")
    hypothesis = context.get("hypothesis")

    lines.extend(["", "EA under research:"])
    if ea is not None:
        lines.append(f"- EA file: {getattr(ea, 'filename', 'n/a')}")
    else:
        lines.append("- EA file: not attached to this experiment")

    market = getattr(mission, "market", None) if mission is not None else None
    timeframe = getattr(mission, "timeframe", None) if mission is not None else None
    if market or timeframe:
        lines.append(f"- Market / timeframe: {market or 'n/a'} {timeframe or ''}".strip())

    specification = getattr(context.get("experiment"), "specification", None)
    if not specification:
        specification = getattr(context.get("experiment"), "baseline", None)
    if specification:
        text = re.sub(r"\s+", " ", str(specification)).strip()
        if len(text) > 320:
            text = text[:317] + "..."
        lines.append(f"- Research scope: {text}")

    if hypothesis is not None and getattr(hypothesis, "statement", None):
        text = re.sub(r"\s+", " ", str(hypothesis.statement)).strip()
        if len(text) > 320:
            text = text[:317] + "..."
        lines.append(f"- Hypothesis: {text}")

    inputs = _compact_ea_inputs(getattr(ea, "source_code", None) if ea is not None else None)
    if inputs:
        lines.append("- Declared EA inputs (from source code):")
        lines.extend([f"  - {item}" for item in inputs])

    # Turn the actual evidence state into questions for peer agents. These are
    # research questions only; they do not authorize strategy changes.
    questions = [
        "Which observed market regimes or entry contexts should be tested next against the existing EA rules?",
        "What additional unseen-period test would best challenge the current robustness conclusion?",
        "Which possible explanation for the observed losses can be tested without changing parameters first?",
    ]
    if "NOT_ESTABLISHED" in json.dumps(metrics, ensure_ascii=False).upper():
        questions.insert(0, "What evidence would be sufficient to move this EA from NOT_ESTABLISHED to a testable robustness hypothesis?")
    lines.extend(["", "Questions for peer research agents:"])
    lines.extend([f"- {q}" for q in questions[:4]])

def _build_post(
    experiment: Experiment,
    result: ExperimentResult,
    related_results: list[ExperimentResult] | None = None,
    db=None,
) -> tuple[str, str]:
    exp = _model_dict(experiment)
    res = _model_dict(result)
    metrics = _json_dict(res.get("metrics"))
    context = _strategy_context(db, experiment) if db is not None else {"mission": None, "ea": None, "hypothesis": None}
    context["experiment"] = experiment

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

    is_v11_sizing = (
        "baseline_flat" in metrics
        or "position-sizing simulation" in str(getattr(result, "summary", "") or "").lower()
        or "sizing policies simulated" in str(getattr(result, "conclusion", "") or "").lower()
    )

    lines = [
        "AI Trading Bot Research Society — Research Update",
        "",
        f"Experiment: {experiment_id}",
        f"Strategy: {strategy}",
        f"Status: {status}",
        "",
        "Research pipeline:",
        "v1 → v3 → v4 → v5 → v6 → v7 → v8 → v9 → v10 → v11",
    ]

    if is_v11_sizing:
        lines.extend([
            "",
            "v11 sizing simulation:",
            str(conclusion),
        ])
        baseline = metrics.get("baseline_flat")
        if isinstance(baseline, dict):
            overall = baseline.get("overall") if isinstance(baseline.get("overall"), dict) else baseline
            if isinstance(overall, dict):
                lines.extend(["", "v11 baseline (flat):"])
                lines.extend(_metric_text(overall)[:5])

        comparison = metrics.get("comparison")
        if isinstance(comparison, dict):
            lines.extend(["", "v11 policy comparisons:"])
            for name, row in comparison.items():
                if not isinstance(row, dict):
                    continue
                lines.append(
                    f"- {name}: delta net profit={row.get('delta_net_profit_vs_flat')}, "
                    f"delta max drawdown={row.get('delta_max_drawdown_vs_flat')}, "
                    f"profit factor={row.get('profit_factor')}"
                )
    else:
        lines.extend([
            "",
            "Robustness conclusion:",
            str(conclusion),
        ])

    # If the selected result references an earlier result, use that source for walk-forward metrics.
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

    _append_strategy_context(lines, context, metrics)

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


_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100,
}

def _collapse_repeated_letters(word: str) -> str:
    return re.sub(r"(.)\1+", r"\1", word.lower())

def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j-1] + (ca != cb)))
        prev = cur
    return prev[-1]

def _challenge_text_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?", str(text or "").lower())

def _fuzzy_number_word(word: str):
    clean = _collapse_repeated_letters(word)
    if clean in _NUMBER_WORDS:
        return _NUMBER_WORDS[clean]
    candidates = [
        (name, value) for name, value in _NUMBER_WORDS.items()
        if len(name) >= 4 and abs(len(name) - len(clean)) <= 2
    ]
    if not candidates:
        return None
    name, value = min(candidates, key=lambda item: _edit_distance(clean, item[0]))
    distance = _edit_distance(clean, name)
    return value if distance <= max(1, len(name) // 4) else None

def _number_from_words(words: list[str], i: int):
    if i >= len(words):
        return None, i
    if re.fullmatch(r"\d+(?:\.\d+)?", words[i]):
        return float(words[i]), i + 1

    # First, join 2-4 obfuscated chunks into a single number word.
    first = None
    first_end = i
    for n in range(1, min(4, len(words) - i) + 1):
        parts = words[i:i+n]
        if any(re.fullmatch(r"\d+(?:\.\d+)?", x) for x in parts):
            continue
        joined = "".join(_collapse_repeated_letters(x) for x in parts)
        if joined in _NUMBER_WORDS:
            first = _NUMBER_WORDS[joined]
            first_end = i + n
            break
        # Only use fuzzy matching for a single chunk; otherwise ordinary prose
        # can accidentally form a number.
        if n == 1:
            fuzzy = _fuzzy_number_word(parts[0])
            if fuzzy is not None:
                first = fuzzy
                first_end = i + 1

    if first is None:
        return None, i

    # English compound numbers: "twenty five" = 25.
    if first in {20,30,40,50,60,70,80,90} and first_end < len(words):
        second = _fuzzy_number_word(words[first_end])
        if second is not None and 0 < second < 10:
            return float(first + second), first_end + 1

    return float(first), first_end

def _extract_number_tokens(text: str) -> list[float]:
    """Extract numeric values from heavily obfuscated Moltbook challenge text.

    Handles normal digits, number words, repeated letters, mixed case, and
    punctuation inserted inside words (for example ``tW/eN tY fIvE``).
    """
    raw = re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?", str(text or ""))
    values: list[float] = []
    i = 0
    while i < len(raw):
        value, next_i = _number_from_words(raw, i)
        if value is not None:
            values.append(float(value))
            i = next_i
        else:
            i += 1
    return values


def _arithmetic_operation(text: str) -> str | None:
    """Return the operation expressed by the challenge, if any."""
    original_normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
    original_normalized = re.sub(r"\s+", " ", original_normalized).strip()
    normalized = _collapse_repeated_letters(original_normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    # Long phrases first so e.g. 'divided by' is not mistaken for something else.
    patterns: list[tuple[str, str]] = [
        (r"\b(divided by|divide by|division|quotient|over)\b", "divide"),
        (r"\b(multiplied by|multiply by|multiplied|multiply|times)\b", "multiply"),
        (r"\b(subtracted by|subtract|minus|take away|decreased by|decreases by|slows by|loses|loss of)\b", "subtract"),
        (r"\b(added to|add to|adds|add|plus|increased by|increases by|gains|gain)\b", "add"),
    ]
    for pattern, op in patterns:
        if re.search(pattern, original_normalized) or re.search(pattern, normalized):
            return op

    # Repeated-letter collapsing can turn words such as ``adds`` into ``ads``
    # (the obfuscator may write ``aDdDs``). Keep a few safe aliases for that
    # transformation rather than treating the altered word as unknown.
    if re.search(r"\b(ads|ad)\b", normalized):
        return "add"

    # Mathematical symbols are only treated as operators when they occur
    # between actual numeric expressions. Moltbook also inserts '/' and '-'
    # as random obfuscation punctuation inside words, so a bare slash must
    # never automatically mean division.
    compact = str(text or "")
    if re.search(r"\d\s*[×*]\s*\d", compact):
        return "multiply"
    if re.search(r"\d\s*[÷/]\s*\d", compact):
        return "divide"
    if re.search(r"\d\s*\+\s*\d", compact):
        return "add"
    if re.search(r"\d\s*-\s*\d", compact):
        return "subtract"
    return None


def _solve_challenge(challenge_text: str) -> str:
    """Solve Moltbook's lightweight arithmetic verification challenge.

    The challenge generator may obfuscate words with random case, repeated
    letters, and punctuation. Do not hard-code operand values. We extract the
    values actually present in the challenge and apply the stated operation.

    Supported forms include:
      25 + 7
      twenty five adds seven
      100 minus 25
      8 multiplied by 6
      100 divided by 4
      "it gains five ... how fast is it now?" -> 5.00 when one operand is
      explicitly supplied and the wording describes a gain from an implicit
      zero/baseline.
    """
    text = str(challenge_text or "").strip()
    if not text:
        raise ValueError("Empty verification challenge")

    nums = _extract_number_tokens(text)
    op = _arithmetic_operation(text)

    if not nums:
        raise ValueError(f"No numeric operand found in challenge: {text!r}")

    # Some Moltbook templates provide a single explicit amount with a verb such
    # as 'gains five' and ask for the resulting amount. There is no second
    # numeric operand in that template, so treating it as the supplied delta is
    # preferable to inventing a hidden starting number.
    if len(nums) == 1:
        if op in {"add", "subtract", "multiply", "divide"}:
            result = nums[0]
            if op == "divide" and nums[0] == 0:
                raise ValueError("Division by zero challenge")
            return f"{result:.2f}"
        # A bare single number is also a valid answer only when the challenge
        # explicitly asks for that amount rather than an operation.
        if re.search(r"\b(how fast|what is|what s|how much|answer)\b", _collapse_repeated_letters(text.lower())):
            return f"{nums[0]:.2f}"
        raise ValueError(f"Only one numeric operand found and no solvable template: {text!r}")

    # Normal two-or-more operand arithmetic. The first two operands are the
    # operands for Moltbook's current verification templates. If a challenge
    # contains a chain such as 10 + 5 + 2, evaluate left-to-right for the same
    # operation rather than silently ignoring later values.
    if op is None:
        raise ValueError(f"Could not determine arithmetic operation: {text!r}")

    result = nums[0]
    for value in nums[1:]:
        if op == "add":
            result += value
        elif op == "subtract":
            result -= value
        elif op == "multiply":
            result *= value
        elif op == "divide":
            if value == 0:
                raise ValueError("Division by zero challenge")
            result /= value

    return f"{result:.2f}"

async def _verify_post(verification: dict[str, Any]) -> dict[str, Any]:
    code=str(verification.get("verification_code") or "").strip()
    challenge=str(verification.get("challenge_text") or "").strip()
    if not code or not challenge:
        return {"attempted": False, "verified": False,
                "error":"Missing verification_code or challenge_text"}
    try:
        answer=_solve_challenge(challenge)
    except Exception as exc:
        return {"attempted": False, "verified": False, "error": str(exc)}
    api_key=os.getenv("MOLTBOOK_API_KEY")
    if not api_key:
        return {"attempted": False, "verified": False, "error":"MOLTBOOK_API_KEY is not configured"}
    status, body = await asyncio.to_thread(
        _request_json, "POST", f"{MOLTBOOK_API_BASE}/verify",
        {"Authorization":f"Bearer {api_key}","Content-Type":"application/json"},
        {"verification_code":code,"answer":answer},
    )
    return {
        "attempted": True,
        "verified": status < 400 and isinstance(body, dict) and body.get("success", True) is True,
        "answer": answer,
        "status_code": status,
        "response": body,
    }


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

    published = body if isinstance(body, dict) else {"response": body}
    post = published.get("post") if isinstance(published, dict) else None
    verification = post.get("verification") if isinstance(post, dict) else None
    if isinstance(verification, dict):
        published["verification"] = await _verify_post(verification)
    elif published.get("already_existed"):
        published["verification"] = {
            "attempted": False,
            "verified": False,
            "error": "Existing post returned; no fresh verification challenge was issued. Use republish=true for a fresh post."
        }
    return published


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
async def publish_research(experiment_id: str, republish: bool = False) -> dict[str, Any]:
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

        # Select the latest research STAGE, not merely the newest timestamp.
        # v11 ExperimentResult can have a missing/identical created_at on older
        # database schemas, so timestamp-only ordering can incorrectly return v10.
        def _result_stage_rank(row: ExperimentResult) -> int:
            metrics = _json_dict(getattr(row, "metrics", None))
            summary = str(getattr(row, "summary", "") or "").lower()
            conclusion = str(getattr(row, "conclusion", "") or "").lower()
            method = metrics.get("method")
            method_text = json.dumps(method, ensure_ascii=False).lower() if isinstance(method, (dict, list)) else str(method or "").lower()

            # v11 sizing signature: baseline_flat + policies + comparison +
            # entry-time volatility/sizing method.
            if (
                "baseline_flat" in metrics
                or ("policies" in metrics and "comparison" in metrics and "sizing_context" in json.dumps(metrics.get("experiment_scope", {})).lower())
                or "position-sizing simulation" in summary
                or "sizing policies simulated" in conclusion
                or "realized trade p/l" in method_text
            ):
                return 11

            # v11.2 / three-year robustness, if present.
            if (
                "v11.2" in method_text
                or "three-year" in method_text
                or "three_year" in json.dumps(metrics, ensure_ascii=False).lower()
            ):
                return 112

            if _is_v10_result(row):
                return 10
            return 0

        result = max(
            all_results,
            key=lambda row: (
                _result_stage_rank(row),
                getattr(row, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc),
            ),
        ) if all_results else None

        if result is None:
            raise HTTPException(
                status_code=404,
                detail="No research result found for this experiment",
            )

        title, content = _build_post(experiment, result, all_results, db)
        if republish:
            # Moltbook deduplicates identical content. Add a unique run marker
            # only when explicitly requested, so normal publishing stays idempotent.
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            title = f"{title} — Research Run {run_id}"
            content = f"{content}\nResearch Run: {run_id}"
        published = await _publish(title, content)

        return {
            "status": "published",
            "experiment_id": experiment_id,
            "title": title,
            "moltbook": published,
        }
    finally:
        db.close()



@router.get("/inbox")
async def moltbook_inbox(limit: int = 50) -> dict[str, Any]:
    """Read the agent home feed so Moltbook feedback can become research input.

    This endpoint does not modify the EA, create orders, or change parameters.
    It only retrieves the current Moltbook home payload for later filtering.
    """
    api_key = os.getenv("MOLTBOOK_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="MOLTBOOK_API_KEY is not configured")
    limit = max(1, min(int(limit), 100))
    status, body = await asyncio.to_thread(
        _request_json,
        "GET",
        f"{MOLTBOOK_API_BASE}/home?limit={limit}",
        {"Authorization": f"Bearer {api_key}"},
    )
    if status >= 400:
        raise HTTPException(
            status_code=502,
            detail={"message": "Moltbook home fetch failed", "status_code": status, "response": body},
        )
    return {"status": "ok", "items": body}


@router.get("/research/{experiment_id}/research-brief")
async def research_brief(experiment_id: str) -> dict[str, Any]:
    """Preview the strategy-aware research brief without publishing it."""
    db = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if experiment is None:
            raise HTTPException(status_code=404, detail="Experiment not found")
        all_results = db.query(ExperimentResult).filter(ExperimentResult.experiment_id == experiment_id).all()
        def rank(row: ExperimentResult) -> tuple[int, Any]:
            metrics = _json_dict(getattr(row, "metrics", None))
            summary = str(getattr(row, "summary", "") or "").lower()
            conclusion = str(getattr(row, "conclusion", "") or "").lower()
            method = metrics.get("method")
            method_text = json.dumps(method, ensure_ascii=False).lower() if isinstance(method, (dict, list)) else str(method or "").lower()
            stage = 11 if ("baseline_flat" in metrics or "position-sizing simulation" in summary or "sizing policies simulated" in conclusion or "realized trade p/l" in method_text) else (112 if "three-year" in json.dumps(metrics, ensure_ascii=False).lower() or "v11.2" in method_text else (10 if _is_v10_result(row) else 0))
            return stage, getattr(row, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc)
        result = max(all_results, key=rank) if all_results else None
        if result is None:
            raise HTTPException(status_code=404, detail="No research result found for this experiment")
        title, content = _build_post(experiment, result, all_results, db)
        return {"status": "research_brief", "experiment_id": experiment_id, "title": title, "content": content}
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

        # Select the latest research STAGE, not merely the newest timestamp.
        # v11 ExperimentResult can have a missing/identical created_at on older
        # database schemas, so timestamp-only ordering can incorrectly return v10.
        def _result_stage_rank(row: ExperimentResult) -> int:
            metrics = _json_dict(getattr(row, "metrics", None))
            summary = str(getattr(row, "summary", "") or "").lower()
            conclusion = str(getattr(row, "conclusion", "") or "").lower()
            method = metrics.get("method")
            method_text = json.dumps(method, ensure_ascii=False).lower() if isinstance(method, (dict, list)) else str(method or "").lower()

            # v11 sizing signature: baseline_flat + policies + comparison +
            # entry-time volatility/sizing method.
            if (
                "baseline_flat" in metrics
                or ("policies" in metrics and "comparison" in metrics and "sizing_context" in json.dumps(metrics.get("experiment_scope", {})).lower())
                or "position-sizing simulation" in summary
                or "sizing policies simulated" in conclusion
                or "realized trade p/l" in method_text
            ):
                return 11

            # v11.2 / three-year robustness, if present.
            if (
                "v11.2" in method_text
                or "three-year" in method_text
                or "three_year" in json.dumps(metrics, ensure_ascii=False).lower()
            ):
                return 112

            if _is_v10_result(row):
                return 10
            return 0

        result = max(
            all_results,
            key=lambda row: (
                _result_stage_rank(row),
                getattr(row, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc),
            ),
        ) if all_results else None

        if result is None:
            raise HTTPException(
                status_code=404,
                detail="No research result found for this experiment",
            )

        title, content = _build_post(experiment, result, all_results, db)
        return {
            "status": "preview",
            "experiment_id": experiment_id,
            "title": title,
            "content": content,
        }
    finally:
        db.close()
