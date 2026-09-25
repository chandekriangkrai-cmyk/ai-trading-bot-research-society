from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult, MoltbookPostLink, ResearchDiscussion


AI_BASE = os.getenv("RESEARCH_AI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
AI_KEY = os.getenv("RESEARCH_AI_API_KEY", "") or os.getenv("OPENROUTER_API_KEY", "")
AI_MODEL = os.getenv("RESEARCH_AI_MODEL", "google/gemini-3.8-flash")
AI_TIMEOUT = float(os.getenv("RESEARCH_AI_TIMEOUT_SECONDS", "45"))

_AI_LAST_META: dict[str, object] = {}


SYSTEM_PROMPT = """You are the independent research scientist for an AI Trading Bot Research Society.
Your personality is curious, skeptical, technically rigorous, and willing to disagree.
Do not merely agree with the commenter or praise the experiment. Look for the most informative
interpretation and the strongest alternative explanation. Distinguish OBSERVATION, INTERPRETATION,
HYPOTHESIS, and TEST. You may introduce a new research question when the evidence supports it.
Do not claim causality from a backtest. Do not invent missing market data, indicator values, exit reasons,
trade states, or statistical tests. If evidence is insufficient, say so plainly.
For numerical claims, use only numbers explicitly present in the supplied research context. Do not invent,
estimate, interpolate, or recall performance figures from outside the supplied context. This includes
percentages, win rates, returns, drawdowns, trade counts, prices, dates used as statistics, and benchmark
figures. If the supplied context does not contain a number needed to support a claim, state the claim
qualitatively or say that the evidence is insufficient. Never fabricate a precise number to make an argument
more convincing.
The EA is proprietary: never reveal, infer, reconstruct, or guess exact indicators, parameter values,
thresholds, entry/exit rules, source-code details, or other implementation secrets. Discuss only the
high-level architecture and evidence explicitly supplied in the research context.
Return ONLY one valid JSON object matching the requested output schema. Do not use markdown fences or add prose outside the JSON.
Keep replies conversational and useful for a research community, not promotional.
"""


def _normalize_numeric_token(token: str) -> str:
    token = token.strip()

    if not token:
        return ""

    # Normalize commas in large numbers.
    token = token.replace(",", "")

    # Normalize percentages while preserving the numeric value.
    if token.endswith("%"):
        token = token[:-1]

    try:
        value = float(token)

        # Decimal normalization:
        # 0.1000 -> 0.1
        # 475.0 -> 475
        if value.is_integer():
            return str(int(value))

        return format(value, ".15g")
    except Exception:
        return token.lower()


def _validate_reply_numeric_evidence(
    reply_text: str,
    context: Any,
) -> None:
    if not reply_text:
        return

    try:
        context_text = json.dumps(
            context,
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        context_text = str(context)

    # Extract explicit numeric tokens from the AI reply.
    reply_numbers = re.findall(
        r"(?<![A-Za-z])[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)%?",
        reply_text,
    )

    if not reply_numbers:
        return

    # Extract numeric tokens from the supplied research context.
    context_numbers = re.findall(
        r"(?<![A-Za-z])[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)%?",
        context_text,
    )

    normalized_context = {
        _normalize_numeric_token(x)
        for x in context_numbers
        if _normalize_numeric_token(x)
    }

    unsupported = []

    for token in reply_numbers:
        normalized = _normalize_numeric_token(token)

        if normalized and normalized not in normalized_context:
            unsupported.append(token)

    if unsupported:
        unique = list(dict.fromkeys(unsupported))

        raise RuntimeError(
            "AI reply contains unsupported numeric claim(s): "
            + ", ".join(unique[:20])
        )


def _post_json(url: str, payload: dict[str, Any]) -> str:
    if not AI_KEY:
        raise RuntimeError(
            "RESEARCH_AI_API_KEY/OPENROUTER_API_KEY is not configured"
        )

    body = json.dumps({
        "model": AI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        "max_tokens": min(
            int(os.getenv("RESEARCH_AI_MAX_OUTPUT_TOKENS", "700")),
            700,
        ),
        "reasoning": {
            "max_tokens": 220
        },
        "response_format": {
            "type": "json_object"
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{AI_BASE}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {AI_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/chandekriangkrai-cmyk/ai-trading-bot-research-society",
            "X-Title": "AI Trading Bot Research Society",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as r:
            data = json.loads(
                r.read().decode("utf-8", "replace")
            )
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"Research AI HTTP {e.code}: {raw[:1200]}"
        ) from e

    global _AI_LAST_META

    choices = data.get("choices") or []

    usage = data.get("usage") or {}
    choice_meta = {}

    if choices:
        choice_meta = choices[0] or {}

    completion_details = usage.get("completion_tokens_details") or {}

    _AI_LAST_META = {
        "finish_reason": choice_meta.get("finish_reason"),
        "usage": usage,
        "reasoning_tokens": (
            usage.get("reasoning_tokens")
            or completion_details.get("reasoning_tokens")
        ),
    }

    if choices:
        message = choices[0].get("message") or {}
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            return text.strip()

    raise RuntimeError("Research AI returned no text")

def _result_context(experiment_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        e = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        r = db.query(ExperimentResult).filter(ExperimentResult.experiment_id == experiment_id).order_by(ExperimentResult.created_at.desc()).first()
        if not e or not r:
            raise ValueError("Completed research result not found")
        raw = json.loads(r.metrics or "{}")
        a = raw.get("analysis", {}) if isinstance(raw, dict) else {}
        # Deliberately build a public-safe context. No source code or exact parameters are sent.
        hypotheses=[]
        for h in a.get("hypotheses", []) or []:
            if not isinstance(h, dict):
                continue
            clean={k:v for k,v in h.items() if k not in {"evidence"}}
            clean["evidence"]={"available": "supplied EA/backtest evidence", "note": "Internal indicator names and parameter values are intentionally withheld."}
            hypotheses.append(clean)
        return {
            "experiment_id": e.id,
            "symbol": e.symbol,
            "timeframe": e.timeframe,
            "scope": ["EA .mq5", "MT5 backtest"],
            "proprietary_boundary": "Exact indicators, parameters, thresholds, entry/exit rules and source logic are undisclosed.",
            "overall": a.get("overall", {}),
            "findings": a.get("findings", []),
            "hypotheses": hypotheses,
            "limitations": raw.get("limitations", []),
            "evidence_policy": a.get("evidence_policy", {}),
        }
    finally:
        db.close()


def _heuristic_classify(text: str) -> str:
    t = text.lower()
    if any(x in t for x in ("how", "why", "what", "could", "does", "can", "evidence", "hypothesis", "reproduce", "replicate")):
        return "research_question"
    if any(x in t for x in ("wrong", "not enough", "bias", "overfit", "cherry", "prove", "causal", "problem")):
        return "challenge"
    if any(x in t for x in ("interesting", "great", "nice", "thanks", "agree")):
        return "casual"
    return "other"


def _heuristic_reply(comment_text: str, context: dict[str, Any]) -> dict[str, Any]:
    classification = _heuristic_classify(comment_text)
    if classification == "casual":
        return {"classification": classification, "decision": "ignore", "reason": "Casual comment; no substantive research discussion detected.", "reply": None}
    if classification == "other":
        return {"classification": classification, "decision": "ignore", "reason": "No clear research question or challenge detected.", "reply": None}

    reply = (
        "That is a useful research question. I do not want to infer or invent missing performance figures. "
        "The realized-performance observation should be reported from the verified backtest, and any benchmark comparison "
        "should use the same instrument, period, starting point, and clearly defined return methodology. "
        "The research record should be updated once those figures are verified."
    )
    return {
        "classification": classification,
        "decision": "reply",
        "reason": "Substantive research discussion detected; deterministic fallback used.",
        "reply": reply,
    }


def generate_reply(experiment_id: str, comment_text: str, author: str = "unknown", thread_context: str = "") -> dict[str, Any]:
    try:
        context = _result_context(experiment_id)
    except Exception as exc:
        return {
            "classification": "other",
            "decision": "ignore",
            "reason": "Research context is unavailable; no reply generated.",
            "reply": None,
            "ai_status": "context_error",
            "ai_error": str(exc)[:1200],
        }

    if len(comment_text.strip()) < 8:
        return {
            "classification": "other",
            "decision": "ignore",
            "reason": "Comment is too short to support substantive research discussion.",
            "reply": None,
            "ai_status": "not_needed",
        }

    if AI_KEY:
        payload = {
            "task": "Analyze one Moltbook comment and decide whether a research reply is useful.",
            "comment_author": author,
            "comment": comment_text[:6000],
            "thread_context": thread_context[:5000],
            "research_context": context,
            "required_output": {
                "classification": "one of research_question, challenge, alternative_hypothesis, replication_request, casual, other",
                "decision": "reply or ignore",
                "reason": "short explanation",
                "reply": "A concise evidence-grounded reply if decision=reply; otherwise empty",
                "next_research_question": "optional question for a future experiment",
            },
                "format_rules": [
                    "Return ONLY one valid JSON object.",
                    "Do not use markdown code fences.",
                    "Do not add prose before or after the JSON.",
                    "The reply field must contain the actual research reply, not JSON or metadata.",
                ],
        }

        raw = ""
        try:
            raw = _post_json(f"{AI_BASE}/chat/completions", payload)
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
                cleaned = re.sub(r"\s*```$", "", cleaned).strip()
            try:
                data = json.loads(cleaned)
            except Exception:
                match = re.search(r"\{.*\}", cleaned, re.S)
                if not match:
                    raise RuntimeError("Research AI returned invalid JSON")
                data = json.loads(match.group(0))
            if not isinstance(data, dict):
                raise RuntimeError("Research AI returned JSON that is not an object")
            data.setdefault("classification", "research_question")
            data.setdefault("decision", "reply")
            data.setdefault("reason", "AI research assessment")
            data["reply"] = str(data.get("reply") or "").strip() or None
            if data.get("reply"):
                _validate_reply_numeric_evidence(
                    data["reply"],
                    context,
                )
            data["ai_status"] = "ok"
            return data
        except Exception as exc:
            fallback = _heuristic_reply(comment_text, context)
            fallback["ai_status"] = "fallback"
            fallback["ai_error"] = str(exc)[:1200]

            # V43.0 diagnostic: expose only bounded model output.
            # Never expose API keys, headers, or credentials.
            if raw:
                fallback["ai_raw_length"] = len(raw)
                fallback["ai_raw_preview"] = raw[:2000]

            if _AI_LAST_META:
                fallback["ai_response_meta"] = _AI_LAST_META

            return fallback

    fallback = _heuristic_reply(comment_text, context)
    fallback["ai_status"] = "not_configured"
    return fallback
