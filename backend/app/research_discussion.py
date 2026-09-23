from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult, MoltbookPostLink, ResearchDiscussion


AI_BASE = os.getenv("RESEARCH_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_KEY = os.getenv("RESEARCH_AI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
AI_MODEL = os.getenv("RESEARCH_AI_MODEL", "gpt-5.6-luna")
AI_TIMEOUT = float(os.getenv("RESEARCH_AI_TIMEOUT_SECONDS", "45"))


SYSTEM_PROMPT = """You are the independent research scientist for an AI Trading Bot Research Society.
Your personality is curious, skeptical, technically rigorous, and willing to disagree.
Do not merely agree with the commenter or praise the experiment. Look for the most informative
interpretation and the strongest alternative explanation. Distinguish OBSERVATION, INTERPRETATION,
HYPOTHESIS, and TEST. You may introduce a new research question when the evidence supports it.
Do not claim causality from a backtest. Do not invent missing market data, indicator values, exit reasons,
trade states, or statistical tests. If evidence is insufficient, say so plainly.
The EA is proprietary: never reveal, infer, reconstruct, or guess exact indicators, parameter values,
thresholds, entry/exit rules, source-code details, or other implementation secrets. Discuss only the
high-level architecture and evidence explicitly supplied in the research context.
Keep replies conversational and useful for a research community, not promotional.
"""


def _post_json(url: str, payload: dict[str, Any]) -> str:
    if not AI_KEY:
        raise RuntimeError("RESEARCH_AI_API_KEY/OPENAI_API_KEY is not configured")
    body = json.dumps({
        "model": AI_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": json.dumps(payload, ensure_ascii=False)}]},
        ],
        "max_output_tokens": int(os.getenv("RESEARCH_AI_MAX_OUTPUT_TOKENS", "700")),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{AI_BASE}/responses",
        data=body,
        headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Research AI HTTP {e.code}: {raw[:1200]}") from e
    text = data.get("output_text")
    if text:
        return str(text).strip()
    # Defensive parsing for compatible Responses implementations.
    chunks = []
    for item in data.get("output", []) or []:
        for c in item.get("content", []) or []:
            if isinstance(c, dict) and c.get("text"):
                chunks.append(str(c["text"]))
    if chunks:
        return "\n".join(chunks).strip()
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
        }
        try:
            raw = _post_json(f"{AI_BASE}/responses", payload)
            try:
                match = re.search(r"\{.*\}", raw, re.S)
                data = json.loads(match.group(0) if match else raw)
            except Exception:
                data = {
                    "classification": "research_question",
                    "decision": "reply",
                    "reason": "AI produced a prose research response.",
                    "reply": raw,
                }
            data.setdefault("classification", "research_question")
            data.setdefault("decision", "reply")
            data.setdefault("reason", "AI research assessment")
            data["reply"] = str(data.get("reply") or "").strip() or None
            data["ai_status"] = "ok"
            return data
        except Exception as exc:
            fallback = _heuristic_reply(comment_text, context)
            fallback["ai_status"] = "fallback"
            fallback["ai_error"] = str(exc)[:1200]
            return fallback

    fallback = _heuristic_reply(comment_text, context)
    fallback["ai_status"] = "not_configured"
    return fallback
